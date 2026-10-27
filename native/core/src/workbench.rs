//! Account-bound course workspace. Remote facts and personal task state are separate.
use crate::{sessions, storage, Error, Result};
use chrono::Utc;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{path::Path, sync::Mutex};
static GATE: Mutex<()> = Mutex::new(());
fn bad(s: &str) -> Error {
    Error::new("study_invalid", s)
}
fn text(v: &Value, max: usize) -> Result<String> {
    let s = v.as_str().ok_or_else(Error::invalid)?.trim();
    if s.chars().count() > max || s.chars().any(|c| c.is_control() && c != '\n' && c != '\t') {
        return Err(Error::invalid());
    }
    Ok(s.into())
}
fn stamp() -> Value {
    json!(Utc::now().to_rfc3339())
}
pub fn owner(root: &Path) -> Result<String> {
    if root.join("locked").exists() {
        return Err(bad("请先登录学校身份。"));
    }
    let v = sessions::dispatch(
        "sessions_read",
        &json!({"profile":"identity","path":root.join("profile/ustc-identity.credentials.dpapi")}),
    )?;
    Ok(format!(
        "{:x}",
        Sha256::digest(
            v["username"]
                .as_str()
                .ok_or_else(Error::invalid)?
                .as_bytes()
        )
    ))
}
fn load(root: &Path, owner: &str) -> Result<Value> {
    let v = storage::private_read(
        &json!({"path":root.join("profile/course-workbench.dpapi"),"default":{"owner":owner,"revision":0,"courses":[],"tasks":[],"updated_at":null}}),
    )?;
    if v["owner"] != owner {
        return Err(bad("课程记录属于另一个账号。"));
    }
    Ok(v)
}
fn save(root: &Path, v: &mut Value) -> Result<()> {
    v["revision"] = json!(v["revision"].as_u64().unwrap_or(0) + 1);
    storage::private_write(&json!({"path":root.join("profile/course-workbench.dpapi"),"value":v}))?;
    Ok(())
}
pub fn read(root: &Path) -> Result<Value> {
    let _g = GATE.lock().map_err(|_| Error::invalid())?;
    let who = owner(root)?;
    let mut v = load(root, &who)?;
    v.as_object_mut().unwrap().remove("owner");
    Ok(v)
}
pub fn context(root: &Path, selector: &Value) -> Result<Value> {
    let v = read(root)?;
    let id = text(&selector["id"], 200)?;
    match selector["kind"].as_str() {
        Some("task") => {
            let t = v["tasks"]
                .as_array()
                .unwrap()
                .iter()
                .find(|t| t["id"] == id)
                .ok_or_else(|| bad("关联作业不存在。"))?;
            Ok(json!({"task":t,"revision":v["revision"],"content_is_untrusted":true}))
        }
        Some("course") => {
            let c = v["courses"]
                .as_array()
                .unwrap()
                .iter()
                .find(|c| c["id"] == id)
                .ok_or_else(|| bad("关联课程不存在。"))?;
            Ok(
                json!({"course_id":id,"course_title":c["title"],"course_page":c["path"],"content_is_untrusted":true}),
            )
        }
        _ => Err(Error::invalid()),
    }
}
fn find_mut<'a>(v: &'a mut Value, field: &str, id: &str) -> Result<&'a mut Value> {
    v[field]
        .as_array_mut()
        .ok_or_else(Error::invalid)?
        .iter_mut()
        .find(|x| x["id"] == id)
        .ok_or_else(|| bad("记录不存在，请刷新页面。"))
}
fn datetime(v: &Value) -> Result<Value> {
    if v.is_null() || v == "" {
        return Ok(Value::Null);
    }
    let s = text(v, 40)?;
    chrono::DateTime::parse_from_rfc3339(&s).map_err(|_| bad("时间须包含时区。"))?;
    Ok(json!(s))
}
fn checklist(v: &Value) -> Result<Value> {
    let a = v
        .as_array()
        .filter(|x| x.len() <= 60)
        .ok_or_else(Error::invalid)?;
    let mut items = vec![];
    for x in a {
        let label = text(&x["text"], 500)?;
        if label.is_empty() {
            return Err(bad("清单内容不能为空。"));
        }
        items.push(json!({"text":label,"done":x["done"].as_bool().ok_or_else(Error::invalid)?,"source":text(&x.get("source").cloned().unwrap_or(json!("本人整理")),1000)?}));
    }
    Ok(json!(items))
}
pub fn mutate(root: &Path, input: &Value, authorized: bool) -> Result<Value> {
    if input["action"] == "list" {
        return read(root);
    }
    if !authorized {
        return Err(Error::new(
            "authorization_required",
            "请确认保存这项任务修改。",
        ));
    }
    let _g = GATE.lock().map_err(|_| Error::invalid())?;
    let who = owner(root)?;
    let mut v = load(root, &who)?;
    if input["revision"] != v["revision"] {
        return Err(bad("记录已更新，请刷新后再保存；当前修改尚未写入。"));
    }
    let id = input["id"].as_str().unwrap_or("");
    match input["action"].as_str().unwrap_or("") {
        "create" => {
            if v["tasks"].as_array().unwrap().len() >= 2000 {
                return Err(bad("任务数量已达上限。"));
            }
            let title = text(&input["title"], 300)?;
            if title.is_empty() {
                return Err(bad("请填写任务名称。"));
            }
            let cid = text(&input["course_id"], 120)?;
            if !cid.is_empty()
                && !v["courses"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|c| c["id"] == cid)
            {
                return Err(bad("请选择已有课程。"));
            }
            let task = json!({"id":format!("manual:{}",uuid::Uuid::new_v4()),"course_id":cid,"source":"manual","title":title,"description":text(&input["description"],12000)?,"due_at":datetime(&input["due_at"])? ,"due_text":[],"progress":"not_started","platform_state":"not_applicable","notes":"","checklist":[],"materials":[],"created_at":stamp(),"changed":false});
            v["tasks"].as_array_mut().unwrap().push(task);
        }
        "update" => {
            let t = find_mut(&mut v, "tasks", id)?;
            if let Some(p) = input.get("progress") {
                if !["not_started", "in_progress", "completed"].contains(&p.as_str().unwrap_or(""))
                {
                    return Err(Error::invalid());
                }
                t["progress"] = p.clone();
            }
            if let Some(n) = input.get("notes") {
                t["notes"] = json!(text(n, 8000)?);
            }
            if let Some(c) = input.get("checklist") {
                t["checklist"] = checklist(c)?;
            }
            if let Some(p) = input.get("planned_at") {
                t["planned_at"] = datetime(p)?;
            }
            if input["acknowledge"] == true {
                t["changed"] = json!(false);
            }
            if input.get("title").is_some()
                || input.get("description").is_some()
                || input.get("due_at").is_some()
            {
                if t["source"] != "manual" {
                    return Err(bad("学校要求由原平台更新；个人安排请填写计划时间。"));
                }
                for (k, max) in [("title", 300), ("description", 12000)] {
                    if let Some(x) = input.get(k) {
                        let s = text(x, max)?;
                        if k == "title" && s.is_empty() {
                            return Err(Error::invalid());
                        }
                        t[k] = json!(s);
                    }
                }
                if let Some(x) = input.get("due_at") {
                    t["due_at"] = datetime(x)?;
                }
            }
            t["personal_updated_at"] = stamp();
        }
        "archive" => {
            let t = find_mut(&mut v, "tasks", id)?;
            t["archived"] = json!(input["archived"].as_bool().ok_or_else(Error::invalid)?);
        }
        "course" => {
            let c = find_mut(&mut v, "courses", id)?;
            if let Some(p) = input.get("pinned") {
                c["pinned"] = json!(p.as_bool().ok_or_else(Error::invalid)?);
            }
            if let Some(mapping) = input.get("timetable_key") {
                c["timetable_key"] = json!(text(mapping, 300)?);
            }
        }
        "material" => {
            let title = text(&input["title"], 300)?;
            let url = text(&input["url"], 2048)?;
            let u = url::Url::parse(&url).map_err(|_| Error::invalid())?;
            if title.is_empty()
                || u.scheme() != "https"
                || u.host_str().is_none()
                || !u.username().is_empty()
                || u.password().is_some()
            {
                return Err(bad("请填写材料名称和 HTTPS 链接。"));
            }
            let c = find_mut(&mut v, "courses", id)?;
            if !c["personal_materials"].is_array() {
                c["personal_materials"] = json!([]);
            }
            let a = c["personal_materials"].as_array_mut().unwrap();
            if a.len() >= 100 {
                return Err(bad("个人材料链接已达上限。"));
            }
            if !a.iter().any(|x| x["url"] == url) {
                a.push(json!({"title":title,"url":url,"source":"本人添加"}));
            }
        }
        _ => return Err(Error::invalid()),
    }
    save(root, &mut v)?;
    v.as_object_mut().unwrap().remove("owner");
    Ok(v)
}
/// Only the desktop's fixed read adapter may merge remote snapshots; not exposed to model/UI.
pub fn merge(root: &Path, expected_owner: &str, snapshot: &Value) -> Result<Value> {
    let _g = GATE.lock().map_err(|_| Error::invalid())?;
    let who = owner(root)?;
    if who != expected_owner {
        return Err(bad("账号发生变化，本次读取未保存。"));
    }
    let mut v = load(root, &who)?;
    merge_into(&mut v, snapshot)?;
    save(root, &mut v)?;
    let mut result=json!({"saved":true,"revision":v["revision"],"state":"completed"});
    if let Some(failure)=snapshot.get("verification_error") {
        result["state"]=json!("partial");
        result["operation_failed"]=json!(true);
        result["next_step"]=failure["message"].clone();
        result["error_id"]=failure["error_id"].clone();
    }
    Ok(result)
}
fn merge_into(v: &mut Value, s: &Value) -> Result<()> {
    let now = stamp();
    if let Some(courses) = s["courses"].as_array() {
        for c in courses {
            let id = text(&c["course_id"], 120)?;
            let a = v["courses"].as_array_mut().unwrap();
            let i = if let Some(i) = a.iter().position(|x| x["id"] == id) {
                i
            } else {
                a.push(json!({"id":id,"pinned":false,"personal_materials":[]}));
                a.len() - 1
            };
            a[i]["title"] = c["title"].clone();
            a[i]["path"] = c["path"].clone();
            a[i]["seen_at"] = now.clone();
        }
        v["updated_at"] = now.clone();
    }
    if let Some(cid) = s["course_id"].as_str() {
        let c = find_mut(v, "courses", cid)?;
        for k in ["page", "announcements", "catalog"] {
            if s.get(k).is_some() {
                c[k] = s[k].clone();
            }
        }
        if let Some(pages) = s["material_pages"].as_array() {
            if !c["material_pages"].is_array() {
                c["material_pages"] = json!([]);
            }
            let saved = c["material_pages"].as_array_mut().unwrap();
            for page in pages {
                if let Some(i) = saved.iter().position(|p| p["url"] == page["url"]) {
                    saved[i] = page.clone();
                } else {
                    if saved.len() >= 100 {
                        saved.remove(0);
                    }
                    saved.push(page.clone());
                }
            }
        }
        if let Some(errors) = s.get("errors") {
            c["errors"] = errors.clone();
        }
        c["checked_at"] = now.clone();
        if let Some(list) = s["catalog"]["assignments"].as_array() {
            let complete = s["catalog"]["truncated"] == false;
            let ids: Vec<_> = list
                .iter()
                .map(|x| format!("bb:{}:{}", cid, x["content_id"].as_str().unwrap_or("")))
                .collect();
            if complete {
                for t in v["tasks"]
                    .as_array_mut()
                    .unwrap()
                    .iter_mut()
                    .filter(|t| t["source"] == "bb" && t["course_id"] == cid)
                {
                    t["not_in_latest"] =
                        json!(!ids.contains(&t["id"].as_str().unwrap_or("").to_owned()));
                }
            }
            for entry in list {
                let id = format!("bb:{}:{}", cid, text(&entry["content_id"], 120)?);
                let a = v["tasks"].as_array_mut().unwrap();
                let i = if let Some(i) = a.iter().position(|t| t["id"] == id) {
                    i
                } else {
                    a.push(json!({"id":id,"course_id":cid,"source":"bb","progress":"not_started","notes":"","checklist":[],"platform_state":"not_checked","changed":false}));
                    a.len() - 1
                };
                let t = &mut a[i];
                let mut changes = vec![];
                for k in ["title", "description", "due_text"] {
                    if t.get(k).is_some() && t[k] != entry[k] {
                        changes.push(json!({"field":k,"before":t[k],"after":entry[k]}));
                    }
                }
                if !changes.is_empty() {
                    t["changed"] = json!(true);
                    t["changes"] = json!(changes);
                }
                for k in [
                    "title",
                    "description",
                    "due_text",
                    "description_truncated",
                    "entry_path",
                    "content_id",
                ] {
                    t[k] = entry[k].clone();
                }
                t["due_at"] = json!(deadline(&entry["due_text"]));
                t["updated_at"] = now.clone();
                t["not_in_latest"] = json!(false);
            }
        }
    }
    if let Some(failure) = s.get("verification_error") {
        let id = format!("bb:{}:{}", text(&s["course_id"],120)?, text(&s["content_id"],120)?);
        let t = find_mut(v, "tasks", &id)?;
        // A failed read is separate from the last verified school record.
        t["verification_error"] = failure.clone();
        t["verification_attempted_at"] = now.clone();
    }
    if let Some(detail) = s.get("verification") {
        let id = format!(
            "bb:{}:{}",
            text(&detail["course_id"], 120)?,
            text(&detail["content_id"], 120)?
        );
        let t = find_mut(v, "tasks", &id)?;
        t["platform_state"] = detail["submission_state"].clone();
        t["verification"] = detail.clone();
        t.as_object_mut().unwrap().remove("verification_error");
        t["verification_attempted_at"] = now.clone();
        t["verified_at"] = now;
        if detail["due_text_available"] == true {
            t["due_text"] = detail["due_text"].clone();
            t["due_at"] = json!(deadline(&detail["due_text"]));
        }
    }
    Ok(())
}
// Parse only unambiguous full numeric dates with a time. Retain original text otherwise.
fn deadline(lines: &Value) -> Option<String> {
    let re = regex::Regex::new(
        r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\s+(\d{1,2}):(\d{2})(?::\d{2})?\s*$",
    )
    .ok()?;
    let mut found = vec![];
    for s in lines.as_array()? {
        let s = s.as_str()?;
        if let Some(c) = re.captures(s) {
            let date = chrono::NaiveDate::from_ymd_opt(
                c[1].parse().ok()?,
                c[2].parse().ok()?,
                c[3].parse().ok()?,
            )?;
            let time = date.and_hms_opt(c[4].parse().ok()?, c[5].parse().ok()?, 0)?;
            found.push(format!("{}+08:00", time.format("%Y-%m-%dT%H:%M:%S")));
        }
    }
    found.sort();
    found.dedup();
    if found.len() == 1 {
        found.pop()
    } else {
        None
    }
}
pub fn schema() -> Value {
    json!({"type":"function","function":{"name":"school_workspace","description":"查询本机课程和作业任务；update保存个人进度、备注或有来源的清单。先list取得准确ID与revision，update需要办理许可。平台提交状态由学校核实。","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["list","update"]},"id":{"type":"string"},"revision":{"type":"integer"},"progress":{"type":"string","enum":["not_started","in_progress","completed"]},"notes":{"type":"string"},"checklist":{"type":"array","items":{"type":"object","properties":{"text":{"type":"string"},"done":{"type":"boolean"},"source":{"type":"string","description":"原始要求中的摘录或材料页码；个人建议标注建议"}},"required":["text","done","source"],"additionalProperties":false}}},"required":["action"],"additionalProperties":false}}})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn history_failure_preserves_receipt_and_success_clears_error() {
        let mut v=json!({"courses":[{"id":"_1_1"}],"tasks":[{"id":"bb:_1_1:_2_1","platform_state":"historical_attempt_present","verification":{"attempt_time_text":"old"},"verified_at":"old-time","notes":"my notes"}]});
        merge_into(&mut v,&json!({"course_id":"_1_1","content_id":"_2_1","verification_error":{"code":"timeout","message":"查询超时"}})).unwrap();
        let t=&v["tasks"][0];
        assert_eq!(t["platform_state"],"historical_attempt_present");
        assert_eq!(t["verification"]["attempt_time_text"],"old");
        assert_eq!(t["verified_at"],"old-time");
        assert_eq!(t["notes"],"my notes");
        assert_eq!(t["verification_error"]["code"],"timeout");
        merge_into(&mut v,&json!({"verification":{"course_id":"_1_1","content_id":"_2_1","submission_state":"unknown","due_text_available":false}})).unwrap();
        assert!(v["tasks"][0].get("verification_error").is_none());
        assert_eq!(v["tasks"][0]["platform_state"],"unknown");
        assert_eq!(v["tasks"][0]["notes"],"my notes");
    }
    #[test]
    fn merge_preserves_personal_and_flags_changed_requirements() {
        let mut v = json!({"courses":[],"tasks":[]});
        merge_into(
            &mut v,
            &json!({"courses":[{"course_id":"1","title":"统计","path":"/x"}]}),
        )
        .unwrap();
        let mut snap = json!({"course_id":"1","catalog":{"truncated":false,"assignments":[{"content_id":"2","title":"HW","description":"PDF","due_text":["截止 2030-01-02 23:59"]}]}});
        merge_into(&mut v, &snap).unwrap();
        v["tasks"][0]["progress"] = json!("completed");
        v["tasks"][0]["notes"] = json!("我的笔记");
        snap["catalog"]["assignments"][0]["description"] = json!("PDF与代码");
        merge_into(&mut v, &snap).unwrap();
        assert_eq!(v["tasks"][0]["progress"], "completed");
        assert_eq!(v["tasks"][0]["notes"], "我的笔记");
        assert_eq!(v["tasks"][0]["changed"], true);
        assert_eq!(v["tasks"][0]["due_at"], "2030-01-02T23:59:00+08:00");
        snap["catalog"]["assignments"] = json!([]);
        snap["catalog"]["truncated"] = json!(true);
        merge_into(&mut v, &snap).unwrap();
        assert_eq!(v["tasks"][0]["not_in_latest"], false);
        snap["catalog"]["truncated"] = json!(false);
        merge_into(&mut v, &snap).unwrap();
        assert_eq!(v["tasks"][0]["not_in_latest"], true);
    }
    #[test]
    fn uncertain_deadlines_and_checklist_validation() {
        assert_eq!(deadline(&json!(["明天", "10/23/26 11:59 PM"])), None);
        assert!(checklist(&json!([{"text":"","done":true}])).is_err());
    }
    #[test]
    fn owned_storage_conflicts_and_progress_do_not_set_submission() {
        let root = std::env::temp_dir().join(format!("school-workbench-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(root.join("profile")).unwrap();
        let identity = root.join("profile/ustc-identity.credentials.dpapi");
        sessions::dispatch(
            "sessions_identity_save",
            &json!({"profile":"identity","path":identity,"username":"study-test"}),
        )
        .unwrap();
        let req = json!({"action":"create","revision":0,"title":"手动任务","course_id":"","description":"报告","due_at":null});
        assert!(mutate(&root, &req, false).is_err());
        let created = mutate(&root, &req, true).unwrap();
        assert!(mutate(&root, &req, true).is_err());
        let id = created["tasks"][0]["id"].clone();
        let updated=mutate(&root,&json!({"action":"update","revision":1,"id":id,"progress":"completed","notes":"私人备注"}),true).unwrap();
        assert_eq!(updated["tasks"][0]["platform_state"], "not_applicable");
        assert_eq!(read(&root).unwrap()["tasks"][0]["notes"], "私人备注");
        let who = owner(&root).unwrap();
        assert!(merge(&root, "different", &json!({"courses":[]})).is_err());
        std::fs::write(root.join("locked"), b"locked").unwrap();
        assert!(read(&root).is_err());
        std::fs::remove_file(root.join("locked")).unwrap();
        std::fs::remove_file(&identity).unwrap();
        sessions::dispatch(
            "sessions_identity_save",
            &json!({"profile":"identity","path":identity,"username":"other-student"}),
        )
        .unwrap();
        assert_ne!(owner(&root).unwrap(), who);
        assert!(read(&root).is_err());
        std::fs::remove_file(root.join("profile/course-workbench.dpapi")).unwrap();
        std::fs::remove_file(identity).unwrap();
        std::fs::remove_file(root.join("profile/ustc-identity.credentials.dpapi.lock")).unwrap();
        std::fs::remove_dir(root.join("profile")).unwrap();
        std::fs::remove_dir(root).unwrap();
    }
}
