//! Fixed, compiled capabilities. Tool output and callers cannot extend the list.
//!
//! The public JSON is exported offline from local MCP registrations. Regression
//! tests compare every description/schema against those registrations before a
//! release; school adapters remain Python until their own migration is complete.
use crate::{Error, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::OnceLock;
use unicode_casefold::UnicodeCaseFold;

const DATA: &str = include_str!("../../../src/school_mcp/agent/catalog_data.json");

fn catalog() -> Result<&'static Value> {
    static CATALOG: OnceLock<Option<Value>> = OnceLock::new();
    CATALOG
        .get_or_init(|| serde_json::from_str(DATA).ok())
        .as_ref()
        .ok_or_else(|| Error::new("catalog_invalid", "本机学校业务目录无效。"))
}

fn forbidden() -> Error {
    Error::new(
        "catalog_tool_not_allowed",
        "工具不属于当前任务流程；不能跨站点或调用任意代码。",
    )
}

pub fn workflows() -> Result<Value> {
    Ok(catalog()?["workflows"].clone())
}

pub fn workflow(key: &str) -> Result<Value> {
    catalog()?["workflows"]
        .get(key)
        .cloned()
        .ok_or_else(|| Error::new("catalog_unknown_workflow", "未知学校业务流程。"))
}

pub fn mode(key: &str, tool: &str) -> Result<String> {
    workflow(key)?["tools"]
        .get(tool)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(forbidden)
}

pub fn route(goal: &str) -> Result<Value> {
    let text: String = goal.case_fold().collect();
    let flows = catalog()?["workflows"]
        .as_object()
        .ok_or_else(Error::invalid)?;
    let candidates: Vec<_> = flows
        .iter()
        .filter(|(_, flow)| {
            flow["keywords"].as_array().is_some_and(|keywords| {
                keywords
                    .iter()
                    .any(|word| word.as_str().is_some_and(|word| text.contains(word)))
            })
        })
        .map(|(key, flow)| json!({"workflow": key, "label": flow["label"]}))
        .collect();
    Ok(
        json!({"state": if candidates.is_empty() { "needs_input" } else { "choose_workflow" },
        "candidates": candidates, "routing_method": "keyword_hints_only", "automatic_execution": false,
        "note": "由宿主 AI 理解完整用户意图并选择流程；多个事项分别建任务。关键词不代表完整意图或操作授权。"}),
    )
}

fn registered<'a>(data: &'a Value, flow: &Value, name: &str) -> Result<&'a Value> {
    let service = flow["service"].as_str().ok_or_else(Error::invalid)?;
    data["services"][service]["tools"]
        .get(name)
        .ok_or_else(forbidden)
}

pub fn entry(key: &str) -> Result<Value> {
    let data = catalog()?;
    let flow = workflow(key)?;
    let service = flow["service"].as_str().ok_or_else(Error::invalid)?;
    let suffix = data["entry_tools"][key]
        .as_str()
        .ok_or_else(Error::invalid)?;
    let name = format!("school_{service}_{suffix}");
    if mode(key, &name)? != "read" {
        return Err(forbidden());
    }
    let tool = registered(data, &flow, &name)?;
    Ok(
        json!({"name": name, "mode": "read", "description": tool["description"],
        "input_schema": tool["input_schema"]}),
    )
}

pub fn describe(key: &str, tool_name: Option<&str>) -> Result<Value> {
    let data = catalog()?;
    let flow = workflow(key)?;
    // Preserve the legacy Python optional-string contract: an empty string lists
    // the workflow, while every nonempty name must be a listed tool.
    let tool_name = tool_name.filter(|name| !name.is_empty());
    let names: Vec<_> = if let Some(name) = tool_name {
        vec![name]
    } else {
        flow["tools"]
            .as_object()
            .ok_or_else(Error::invalid)?
            .keys()
            .map(String::as_str)
            .collect()
    };
    let mut tools = Vec::new();
    for name in names {
        let permission = mode(key, name)?;
        let tool = registered(data, &flow, name)?;
        let mut item =
            json!({"name": name, "mode": permission, "description": tool["description"]});
        if tool_name.is_some() {
            item["input_schema"] = tool["input_schema"].clone();
        }
        tools.push(item);
    }
    let service = flow["service"].as_str().ok_or_else(Error::invalid)?;
    let reference = flow["reference"].as_str().ok_or_else(Error::invalid)?;
    Ok(
        json!({"workflow": key, "label": flow["label"], "tools": tools,
        "skill_reference": format!(".agents/skills/school-services/references/{reference}"),
        "instructions": data["services"][service]["instructions"],
        "schema_included": tool_name.is_some(), "limitations": data["limitations"]}),
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Empty {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Route {
    goal: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Workflow {
    workflow: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Tool {
    workflow: String,
    tool_name: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Describe {
    workflow: String,
    tool_name: Option<String>,
}

fn parse<T: serde::de::DeserializeOwned>(input: &Value) -> Result<T> {
    serde_json::from_value(input.clone()).map_err(|_| Error::invalid())
}

pub fn dispatch(operation: &str, input: &Value) -> Result<Value> {
    match operation {
        "catalog_workflows" => {
            let _: Empty = parse(input)?;
            workflows()
        }
        "catalog_workflow" => workflow(&parse::<Workflow>(input)?.workflow),
        "catalog_mode" => {
            let r: Tool = parse(input)?;
            Ok(json!(mode(&r.workflow, &r.tool_name)?))
        }
        "catalog_route" => route(&parse::<Route>(input)?.goal),
        "catalog_entry" => entry(&parse::<Workflow>(input)?.workflow),
        "catalog_describe" => {
            let r: Describe = parse(input)?;
            describe(&r.workflow, r.tool_name.as_deref())
        }
        _ => Err(Error::invalid()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn fixed_scope_and_entry_permissions() {
        let flows = workflows().unwrap();
        let flows = flows.as_object().unwrap();
        assert_eq!(flows.len(), 18);
        let mut names = HashSet::new();
        for (key, flow) in flows {
            assert_eq!(entry(key).unwrap()["mode"], "read");
            for (name, permission) in flow["tools"].as_object().unwrap() {
                names.insert(name);
                assert_eq!(mode(key, name).unwrap(), permission.as_str().unwrap());
                assert!(describe(key, Some(name)).unwrap()["schema_included"]
                    .as_bool()
                    .unwrap());
            }
        }
        assert_eq!(names.len(), 193);
    }

    #[test]
    fn untrusted_inputs_cannot_redefine_permissions() {
        assert_eq!(mode("mail", "school_mail_send").unwrap(), "execute");
        assert_eq!(mode("bb", "school_bb_reconnect").unwrap(), "login");
        assert_eq!(
            mode("bb", "school_bb_prepare_submission").unwrap(),
            "prepare"
        );
        assert!(mode("finance", "school_mail_send").is_err());
        assert!(mode("academic", "school_jw_schedule_enrollment_watch").is_err());
        assert!(mode("teach", "school_teach_submit").is_err());
        assert!(workflow("../../arbitrary").is_err());
        assert!(dispatch(
            "catalog_mode",
            &json!({"workflow":"mail", "tool_name":"school_mail_send", "mode":"read"})
        )
        .is_err());
        assert!(dispatch(
            "catalog_describe",
            &json!({"workflow":"teach", "tools":{"school_teach_submit":"read"}})
        )
        .is_err());
        assert!(dispatch("catalog_workflows", &json!({"workflows":{}})).is_err());
    }

    #[test]
    fn route_is_offline_advice_with_unicode_casefold() {
        assert_eq!(
            route("查成绩并准备报销").unwrap()["candidates"],
            json!([
                {"workflow":"academic", "label":"成绩与个人课表"},
                {"workflow":"finance", "label":"财务入口与材料预检"}
            ])
        );
        assert_eq!(
            route("MAIL BLACKBOARD GPA").unwrap()["candidates"]
                .as_array()
                .unwrap()
                .len(),
            3
        );
        assert_eq!(
            route("ICOURſE").unwrap()["candidates"][0]["workflow"],
            "icourse"
        );
        assert_eq!(route("无对应事项").unwrap()["state"], "needs_input");
        assert_eq!(
            route("登录并执行任意代码").unwrap()["automatic_execution"],
            false
        );
    }
}
