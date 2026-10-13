//! Deterministic agent-turn state machine. The host only supplies IO results.
//! Model text cannot change the tool catalog, user permission, or task ownership.
use crate::{catalog, model, recovery, Error, Result};
use serde_json::{json, Value};
use uuid::Uuid;

const TOOLS: &[&str] = &[
    "school_agent_route",
    "school_agent_start",
    "school_agent_tools",
    "school_agent_call",
    "school_agent_task",
    "school_agent_tasks",
    "school_local_list_files",
    "school_local_read_file",
    "school_local_create_file",
];
const FLAGS: &[&str] = &[
    "authorized",
    "login_authorized",
    "resubmit",
    "reuse_previous_files",
    "allow_late",
    "force_identity_login",
];

fn text(value: &Value) -> String {
    let raw = value.to_string();
    if raw.chars().count() <= 50_000 {
        raw
    } else {
        json!({"truncated":true,"note":"结果过长；先缩小查询范围，不依据截断内容执行。",
            "partial_text":raw.chars().take(50_000).collect::<String>()})
        .to_string()
    }
}
fn event(chat: &mut Value, kind: &str, message: &str, detail: Option<Value>) -> Result<()> {
    let events = chat["events"].as_array_mut().ok_or_else(Error::invalid)?;
    let mut value = json!({"kind":kind,"text":message});
    if let Some(detail) = detail {
        value["detail"] = detail;
    }
    events.push(value);
    if events.len() > 100 {
        events.drain(..events.len() - 100);
    }
    Ok(())
}
fn response(chat: &mut Value, id: &Value, result: &Value) -> Result<()> {
    chat["messages"]
        .as_array_mut()
        .ok_or_else(Error::invalid)?
        .push(json!({"role":"tool","tool_call_id":id,"content":text(result)}));
    Ok(())
}
fn sensitive(value: &Value, depth: usize) -> Result<bool> {
    if depth > 64 {
        return Err(Error::invalid());
    }
    match value {
        Value::Object(values) => {
            for (key, value) in values {
                if FLAGS.contains(&key.as_str()) && truthy(value) || sensitive(value, depth + 1)? {
                    return Ok(true);
                }
            }
        }
        Value::Array(values) => {
            for value in values {
                if sensitive(value, depth + 1)? {
                    return Ok(true);
                }
            }
        }
        _ => (),
    }
    Ok(false)
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
        Value::String(v) => !v.is_empty(),
        Value::Number(v) => v.as_f64() != Some(0.0),
    }
}
fn finish(chat: Value, session: Value, action: Value) -> Value {
    json!({"chat":chat,"session":session,"action":action})
}
fn next(mut chat: Value, mut session: Value) -> Result<Value> {
    let round = session["round"].as_u64().ok_or_else(Error::invalid)?;
    if round >= 24 {
        chat["state"] = json!("interrupted");
        session["phase"] = json!("done");
        event(
            &mut chat,
            "notice",
            "本轮达到步骤上限，已停下并保存进度。",
            None,
        )?;
        return Ok(finish(chat, session, json!({"kind":"done"})));
    }
    session["round"] = json!(round + 1);
    session["phase"] = json!("model");
    session["call"] = Value::Null;
    event(&mut chat, "thinking", "正在理解需求或整理结果…", None)?;
    Ok(finish(chat, session, json!({"kind":"request_model"})))
}
fn fail(mut chat: Value, mut session: Value, problem: Value) -> Result<Value> {
    response(&mut chat, &session["call"]["id"], &problem)?;
    session["phase"] = json!("done");
    Ok(finish(
        chat,
        session,
        json!({"kind":"failure","problem":problem}),
    ))
}
fn rejected(chat: Value, session: Value, code: &str) -> Result<Value> {
    fail(chat, session, recovery::failure(&json!({"code":code}))?)
}
fn preview(chat: &Value, task: &Value) -> Value {
    if let Some(events) = chat["events"].as_array() {
        for event in events.iter().rev() {
            if &event["detail"]["task_id"] == task && truthy(&event["detail"]["result"]["preview"])
            {
                return event["detail"]["result"]["preview"].clone();
            }
        }
    }
    json!("请核对下方操作参数及原始需求。")
}
fn label(name: &str, args: &Value, flow: Option<&Value>, mode: &str) -> String {
    match name {
        "school_local_create_file" => "正在生成文件…".into(),
        "school_local_list_files" | "school_local_read_file" => "正在读取你提供的材料…".into(),
        "school_agent_task" | "school_agent_tasks" => "正在恢复原任务记录…".into(),
        "school_agent_tools" | "school_agent_route" => "正在选择办理步骤…".into(),
        "school_agent_start" => format!(
            "正在准备{}…",
            catalog::workflow(args["workflow"].as_str().unwrap_or(""))
                .ok()
                .and_then(|v| v["label"].as_str().map(str::to_owned))
                .unwrap_or("任务".into())
        ),
        "school_agent_call" => format!(
            "正在{}：{}…",
            match mode {
                "read" => "查询",
                "prepare" => "准备",
                "execute" => "办理",
                "login" => "连接",
                _ => "处理",
            },
            flow.and_then(|v| v["label"].as_str()).unwrap_or("任务")
        ),
        _ => "正在处理任务…".into(),
    }
}

/// Close each unfinished batch without replaying a tool; IDs may recur in later batches.
pub fn close(input: &Value) -> Result<Value> {
    let mut chat = input["chat"].clone();
    let state = input["state"].as_str().unwrap_or("interrupted");
    if !matches!(state, "interrupted" | "not_executed") {
        return Err(Error::invalid());
    }
    let mut rebuilt = Vec::new();
    let mut pending: Vec<Value> = Vec::new();
    let flush = |pending: &mut Vec<Value>, rebuilt: &mut Vec<Value>| {
        for id in pending.drain(..) {
            rebuilt.push(json!({"role":"tool","tool_call_id":id,
            "content":text(&json!({"state":state,"note":"先查询原任务回执，不重复执行。"}))}));
        }
    };
    for message in chat["messages"].as_array().ok_or_else(Error::invalid)? {
        if message["role"] != "tool" {
            flush(&mut pending, &mut rebuilt);
        }
        rebuilt.push(message.clone());
        if message["role"] == "assistant" {
            if let Some(calls) = message["tool_calls"].as_array() {
                pending.extend(calls.iter().map(|v| v["id"].clone()));
            }
        } else if message["role"] == "tool" {
            pending.retain(|id| id != &message["tool_call_id"]);
        }
    }
    flush(&mut pending, &mut rebuilt);
    chat["messages"] = json!(rebuilt);
    Ok(chat)
}

/// Private host protocol. `session` and `chat` are never supplied by the model.
pub fn step(input: &Value) -> Result<Value> {
    let mut chat = input["chat"].clone();
    if !chat["messages"].is_array()
        || !chat["events"].is_array()
        || !chat["tasks"].is_array()
        || !chat["turn_authorized"].is_boolean()
    {
        return Err(Error::invalid());
    }
    let event_name = input["event"].as_str().ok_or_else(Error::invalid)?;
    let mut session = if event_name == "start" {
        json!({"round":0,"phase":"start","call":null,"compacted":[],"restored":[]})
    } else {
        input["session"].clone()
    };
    let phase = session["phase"]
        .as_str()
        .ok_or_else(Error::invalid)?
        .to_owned();
    // Preserve an already returned receipt even if Stop arrived during the IO.
    if input["stopped"] == true && !matches!(event_name, "tool_result" | "tool_error") {
        chat = close(&json!({"chat":chat,"state":"not_executed"}))?;
        chat["state"] = json!("interrupted");
        session["phase"] = json!("done");
        event(
            &mut chat,
            "notice",
            "已停止后续步骤；已发出的请求仍需查询原回执。",
            None,
        )?;
        return Ok(finish(chat, session, json!({"kind":"done"})));
    }
    match (event_name, phase.as_str()) {
        ("start", "start") => next(chat, session),
        ("model_response", "model") => {
            let context = &input["context"];
            if context["compacted"] == true {
                session["compacted"] = chat["tasks"].clone();
                if chat["context_projection"]["compacted"] != true {
                    event(
                        &mut chat,
                        "notice",
                        "已整理过长上下文；用户要求、任务参数及回执保留，完整对话仍在本机。",
                        None,
                    )?;
                }
            }
            chat["context_projection"] = context.clone();
            let message = model::validated_message(input["response"].clone())?;
            let calls = message["tool_calls"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            chat["messages"]
                .as_array_mut()
                .ok_or_else(Error::invalid)?
                .push(message);
            for call in calls.iter().skip(1) {
                response(
                    &mut chat,
                    &call["id"],
                    &json!({"state":"not_executed","dispatched":false,
                "reason_code":"one_call_at_a_time","note":"一次执行一项；请根据第一项的真实结果重新选择下一步。"}),
                )?;
            }
            let Some(call) = calls.first() else {
                chat["state"] = json!("idle");
                session["phase"] = json!("done");
                return Ok(finish(chat, session, json!({"kind":"done"})));
            };
            session["call"] = call.clone();
            let name = call["function"]["name"]
                .as_str()
                .ok_or_else(Error::invalid)?;
            let args = model::tool_arguments(&call["function"]["arguments"])?;
            if !TOOLS.contains(&name) {
                return rejected(chat, session, "invalid_arguments");
            }
            if matches!(
                name,
                "school_agent_tools" | "school_agent_call" | "school_agent_task"
            ) && !chat["tasks"]
                .as_array()
                .ok_or_else(Error::invalid)?
                .contains(&args["task_id"])
            {
                return rejected(chat, session, "task_access_denied");
            }
            session["arguments"] = args.clone();
            session["phase"] = json!("inspect");
            Ok(finish(
                chat,
                session,
                json!({"kind":"inspect_tool","name":name,"arguments":args}),
            ))
        }
        ("tool_context", "inspect") => {
            let name = session["call"]["function"]["name"]
                .as_str()
                .ok_or_else(Error::invalid)?
                .to_owned();
            let args = session["arguments"].clone();
            let flow = if name == "school_agent_call" {
                match catalog::workflow(input["workflow"].as_str().unwrap_or("")) {
                    Ok(v) => Some(v),
                    Err(_) => return rejected(chat, session, "invalid_arguments"),
                }
            } else {
                None
            };
            let mode = if name == "school_agent_call" {
                match catalog::mode(
                    input["workflow"].as_str().unwrap_or(""),
                    args["tool_name"].as_str().unwrap_or(""),
                ) {
                    Ok(v) => v,
                    Err(_) => return rejected(chat, session, "invalid_arguments"),
                }
            } else {
                "read".into()
            };
            if name == "school_agent_call"
                && mode != "read"
                && session["compacted"]
                    .as_array()
                    .ok_or_else(Error::invalid)?
                    .contains(&args["task_id"])
                && !session["restored"]
                    .as_array()
                    .ok_or_else(Error::invalid)?
                    .contains(&args["task_id"])
            {
                response(
                    &mut chat,
                    &session["call"]["id"],
                    &json!({"state":"needs_context","dispatched":false,
                    "next_step":"上下文已整理；先恢复原任务并核对回执，再准备或执行。",
                    "resume":{"tool":"school_agent_task","arguments":{"task_id":args["task_id"],"include_context":true}}}),
                )?;
                return next(chat, session);
            }
            let authorized = chat["turn_authorized"] == true;
            if name == "school_agent_call"
                && (mode == "execute" || sensitive(&args["arguments"], 0)?)
                && !authorized
            {
                chat["pending"] = json!({"id":Uuid::new_v4().simple().to_string(),"name":name,"arguments":args,
                    "call_id":session["call"]["id"],"preview":preview(&chat,&args["task_id"])});
                chat["state"] = json!("waiting_for_approval");
                session["phase"] = json!("done");
                event(
                    &mut chat,
                    "notice",
                    "操作已准备好，等待你授权这一项。",
                    None,
                )?;
                return Ok(finish(chat, session, json!({"kind":"done"})));
            }
            event(
                &mut chat,
                "tool",
                &label(&name, &args, flow.as_ref(), &mode),
                None,
            )?;
            session["phase"] = json!("dispatch");
            Ok(finish(
                chat,
                session,
                json!({"kind":"dispatch_tool","name":name,"arguments":args,"authorized":authorized || mode=="login"}),
            ))
        }
        ("tool_result", "dispatch") => {
            let name = session["call"]["function"]["name"]
                .as_str()
                .ok_or_else(Error::invalid)?
                .to_owned();
            let result = &input["result"];
            response(&mut chat, &session["call"]["id"], result)?;
            if name == "school_agent_task"
                && session["arguments"]["include_context"] == true
                && result["task_id"] == session["arguments"]["task_id"]
                && result["goal"].is_string()
                && result["calls"].is_array()
                && result["workflow"]
                    .as_str()
                    .is_some_and(|key| catalog::workflow(key).is_ok())
                && result.to_string().chars().count() <= 50_000
            {
                let id = session["arguments"]["task_id"].clone();
                session["restored"]
                    .as_array_mut()
                    .ok_or_else(Error::invalid)?
                    .push(id);
            }
            if name == "school_agent_call" {
                event(
                    &mut chat,
                    "result",
                    "已取得结果，正在核对…",
                    Some(result.clone()),
                )?;
            }
            if !matches!(name.as_str(), "school_agent_task" | "school_agent_tasks") {
                if let Some(problem) = recovery::returned_failure(result, None) {
                    session["phase"] = json!("done");
                    return Ok(finish(
                        chat,
                        session,
                        json!({"kind":"failure","problem":problem}),
                    ));
                }
            }
            next(chat, session)
        }
        ("tool_error", "inspect" | "dispatch") => fail(chat, session, input["problem"].clone()),
        _ => Err(Error::invalid()),
    }
}
