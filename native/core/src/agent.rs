//! Durable coordinator transactions. The adapter may execute only after claim commits.
use crate::{catalog, storage::crypt, Error, Result};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{fs, path::PathBuf, time::Duration};
use unicode_casefold::UnicodeCaseFold;

fn invalid(message: &str) -> Error {
    Error::new("invalid_arguments", message)
}
fn storage_error() -> Error {
    Error::new(
        "storage_failure",
        "本机任务记录不可用；保留原记录并核实回执，不能重发。",
    )
}
fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}
fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key].as_str().ok_or_else(Error::invalid)
}
fn identifier(value: &Value) -> Result<&str> {
    value
        .as_str()
        .filter(|s| {
            s.len() == 32
                && s.bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
        .ok_or_else(|| invalid("task_id 无效。"))
}
fn path(input: &Value) -> Result<PathBuf> {
    let path = PathBuf::from(string(input, "database")?);
    if !path.is_absolute() {
        return Err(Error::invalid());
    }
    Ok(path)
}
fn database(input: &Value, write: bool) -> Result<Connection> {
    let path = path(input)?;
    if write {
        fs::create_dir_all(path.parent().ok_or_else(Error::invalid)?)
            .map_err(|_| storage_error())?;
    } else if !path.try_exists().map_err(|_| storage_error())? {
        return Err(invalid("尚无智能体任务。"));
    }
    let flags = if write {
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE
    } else {
        OpenFlags::SQLITE_OPEN_READ_ONLY
    };
    let db = Connection::open_with_flags(path, flags).map_err(|_| storage_error())?;
    db.busy_timeout(Duration::from_secs(10))
        .map_err(|_| storage_error())?;
    if write {
        db.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload BLOB NOT NULL)",
            [],
        )
        .map_err(|_| storage_error())?;
    } else {
        db.pragma_update(None, "query_only", true)
            .map_err(|_| storage_error())?;
    }
    Ok(db)
}
fn decode(bytes: &[u8]) -> Result<Value> {
    let raw = crypt(bytes, true)?;
    if raw.len() > 2 * 1024 * 1024 {
        return Err(storage_error());
    }
    let task = crate::strict_json::parse(&raw).map_err(|_| storage_error())?;
    identifier(&task["task_id"])?;
    catalog::workflow(string(&task, "workflow")?)?;
    if !task["calls"].is_array()
        || !task["links"].is_array()
        || !task["goal"].is_string()
        || !(task["account"].is_null() || task["account"].is_string())
    {
        return Err(storage_error());
    }
    let calls = task["calls"].as_array().unwrap();
    for call in calls {
        if !call.is_object()
            || !call["arguments"].is_object()
            || !call["key"].is_string()
            || !call["status"].as_str().is_some_and(|s| {
                [
                    "in_flight",
                    "returned",
                    "failed_or_unknown",
                    "failed_before_write",
                ]
                .contains(&s)
            })
            || !(call["receipt"].is_null() || call["receipt"].is_object())
            || !(call["resolved_by"].is_null()
                || call["resolved_by"]
                    .as_u64()
                    .is_some_and(|i| i < calls.len() as u64))
        {
            return Err(storage_error());
        }
        let mode = catalog::mode(
            string(&task, "workflow")?,
            string(call, "tool").map_err(|_| storage_error())?,
        )
        .map_err(|_| storage_error())?;
        if call["mode"] != mode {
            return Err(storage_error());
        }
    }
    for link in task["links"].as_array().unwrap() {
        if !link.is_object() || !link["tool"].is_string() || !link["arguments"].is_object() {
            return Err(storage_error());
        }
    }
    Ok(task)
}
fn load(db: &Connection, id: &str) -> Result<Value> {
    let bytes: Option<Vec<u8>> = db
        .query_row("SELECT payload FROM tasks WHERE id=?", [id], |row| {
            row.get(0)
        })
        .optional()
        .map_err(|_| storage_error())?;
    let task = decode(&bytes.ok_or_else(|| invalid("任务不存在。"))?)?;
    if task["task_id"] != id {
        return Err(storage_error());
    }
    Ok(task)
}
fn save(db: &Connection, task: &mut Value) -> Result<()> {
    task["updated_at"] = json!(now());
    let bytes = serde_json::to_vec(task).map_err(|_| storage_error())?;
    if bytes.len() > 2 * 1024 * 1024 {
        return Err(invalid("任务上下文过大；缩小任务，不保存原始网页或附件。"));
    }
    db.execute(
        "INSERT OR REPLACE INTO tasks VALUES (?,?)",
        rusqlite::params![identifier(&task["task_id"])?, crypt(&bytes, false)?],
    )
    .map_err(|_| storage_error())?;
    Ok(())
}
fn account(value: &Value) -> Result<()> {
    if value.is_null()
        || value
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.len() <= 256)
    {
        Ok(())
    } else {
        Err(Error::invalid())
    }
}
pub fn create(input: &Value) -> Result<Value> {
    let workflow = string(input, "workflow")?;
    catalog::workflow(workflow)?;
    let goal = string(input, "goal")?;
    if goal.is_empty() || goal.chars().count() > 2000 {
        return Err(Error::invalid());
    }
    account(input.get("account").ok_or_else(Error::invalid)?)?;
    let mut task = json!({"task_id":uuid::Uuid::new_v4().simple().to_string(), "workflow":workflow,
        "goal":goal,"account":input["account"],"created_at":now(),"updated_at":now(),"state":"active","calls":[],"links":[]});
    let mut db = database(input, true)?;
    let tx = db
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| storage_error())?;
    save(&tx, &mut task)?;
    tx.commit().map_err(|_| storage_error())?;
    Ok(task)
}
pub fn read(input: &Value) -> Result<Value> {
    load(&database(input, false)?, identifier(&input["task_id"])?)
}
pub fn list(input: &Value) -> Result<Value> {
    let offset = input["offset"]
        .as_u64()
        .filter(|v| *v <= 10000)
        .ok_or_else(Error::invalid)?;
    let limit = input["limit"]
        .as_u64()
        .filter(|v| (1..=20).contains(v))
        .ok_or_else(Error::invalid)?;
    if !path(input)?.try_exists().map_err(|_| storage_error())? {
        return Ok(json!({"tasks":[],"next_offset":null}));
    }
    let db = database(input, false)?;
    let mut statement = db
        .prepare("SELECT payload FROM tasks ORDER BY rowid DESC LIMIT ? OFFSET ?")
        .map_err(|_| storage_error())?;
    let rows = statement
        .query_map(rusqlite::params![limit + 1, offset], |row| {
            row.get::<_, Vec<u8>>(0)
        })
        .map_err(|_| storage_error())?;
    let mut tasks = Vec::new();
    for row in rows {
        tasks.push(decode(&row.map_err(|_| storage_error())?)?);
    }
    let next = if tasks.len() as u64 > limit {
        Some(offset + limit)
    } else {
        None
    };
    tasks.truncate(limit as usize);
    Ok(json!({"tasks":tasks,"next_offset":next}))
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
fn guard(value: &Value, authorized: bool, depth: usize) -> Result<()> {
    if depth > 20 {
        return Err(invalid("参数嵌套过深。"));
    }
    match value {
        Value::Object(map) => {
            for (key, item) in map {
                if [
                    "password",
                    "cookie",
                    "cookies",
                    "token",
                    "access_token",
                    "client_secret",
                    "authorization",
                ]
                .contains(&key.case_fold().collect::<String>().as_str())
                {
                    return Err(invalid("凭据只通过本机接入流程管理，不传给智能体上下文。"));
                }
                if [
                    "authorized",
                    "login_authorized",
                    "resubmit",
                    "reuse_previous_files",
                    "allow_late",
                    "force_identity_login",
                ]
                .contains(&key.as_str())
                    && truthy(item)
                    && !authorized
                {
                    return Err(invalid("特殊操作参数需要已有用户授权。"));
                }
                guard(item, authorized, depth + 1)?;
            }
        }
        Value::Array(items) => {
            for item in items {
                guard(item, authorized, depth + 1)?;
            }
        }
        _ => {}
    }
    Ok(())
}
// Python json.dumps(sort_keys=True, ensure_ascii=False) uses spaces and repr(float).
// Normalize the shortest round-trip decimal to Python's exponent thresholds.
fn python_float(number: &serde_json::Number) -> String {
    let raw = number.to_string();
    let negative = raw.starts_with('-');
    let unsigned = raw.trim_start_matches('-');
    let (mantissa, exponent) = unsigned
        .split_once(['e', 'E'])
        .map(|(m, e)| (m, e.parse::<i32>().unwrap_or(0)))
        .unwrap_or((unsigned, 0));
    let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let mut digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let zeros = digits.bytes().take_while(|c| *c == b'0').count();
    if zeros == digits.len() {
        return if negative { "-0.0" } else { "0.0" }.to_owned();
    }
    digits.drain(..zeros);
    while digits.ends_with('0') {
        digits.pop();
    }
    let power = decimal + exponent - zeros as i32 - 1;
    let sign = if negative { "-" } else { "" };
    if !(-4..16).contains(&power) {
        let rest = if digits.len() > 1 {
            format!(".{}", &digits[1..])
        } else {
            String::new()
        };
        format!(
            "{sign}{}{rest}e{}{:02}",
            &digits[..1],
            if power < 0 { "-" } else { "+" },
            power.abs()
        )
    } else if power < 0 {
        format!("{sign}0.{}{}", "0".repeat((-power - 1) as usize), digits)
    } else {
        let split = power as usize + 1;
        if digits.len() <= split {
            format!("{sign}{}{}.0", digits, "0".repeat(split - digits.len()))
        } else {
            format!("{sign}{}.{}", &digits[..split], &digits[split..])
        }
    }
}
pub(crate) fn canonical(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            format!(
                "{{{}}}",
                keys.into_iter()
                    .map(|k| format!(
                        "{}: {}",
                        serde_json::to_string(k).unwrap(),
                        canonical(&map[k])
                    ))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        Value::Array(values) => format!(
            "[{}]",
            values.iter().map(canonical).collect::<Vec<_>>().join(", ")
        ),
        Value::Number(number) if number.is_f64() => python_float(number),
        _ => value.to_string(),
    }
}
fn key(tool: &str, arguments: &Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(canonical(&json!([tool, arguments])).as_bytes())
    )
}
const UNKNOWN: &[&str] = &[
    "uncertain",
    "executing",
    "submitting",
    "review",
    "interrupted",
    "unknown",
];
const VERIFIED: &[&str] = &[
    "school_bb_submit_assignment",
    "school_bb_submission_status",
    "school_icourse_publish_review",
    "school_icourse_review_status",
    "school_nan7_publish_offer",
    "school_nan7_offer_status",
    "school_young_run_registration",
    "school_young_registration_status",
    "school_drive_execute",
    "school_drive_status",
    "school_spaces_execute",
    "school_spaces_status",
    "school_vlab_execute",
    "school_vlab_status",
    "school_student_submit_application",
    "school_student_application_status",
];
fn state(value: &Value) -> &Value {
    value.get("state").unwrap_or(&value["status"])
}
fn unresolved(call: &Value) -> bool {
    if !call["resolved_by"].is_null() {
        return false;
    }
    if call["status"] == "in_flight" {
        return true;
    }
    if call["mode"] == "read" {
        return false;
    }
    call["status"] == "failed_or_unknown"
        || [state(&call["receipt"]), state(&call["receipt"]["result"])]
            .iter()
            .any(|v| v.as_str().is_some_and(|s| UNKNOWN.contains(&s)))
}
fn receipt(value: &Value, depth: usize) -> Value {
    let mut output = Map::new();
    if depth > 20 {
        return Value::Object(output);
    }
    for key in [
        "state",
        "status",
        "phase",
        "can_execute",
        "can_send",
        "safe_to_submit",
        "reason_code",
        "preparation_id",
        "plan_id",
        "draft_id",
        "job_id",
        "schedule_id",
        "content_sha256",
        "expected_sha256",
        "offer_id",
        "worker_running",
        "succeeded",
        "verified",
    ] {
        if let Some(v) = value.get(key) {
            if v.is_boolean()
                || v.is_i64()
                || v.is_u64()
                || v.as_str().is_some_and(|s| s.chars().count() <= 160)
            {
                output.insert(key.into(), v.clone());
            }
        }
    }
    for key in ["result", "preparation", "related_task"] {
        if value[key].is_object() {
            output.insert(key.into(), receipt(&value[key], depth + 1));
        }
    }
    Value::Object(output)
}
fn links(service: &str, value: &Value, depth: usize) -> Vec<Value> {
    if depth > 20 {
        return Vec::new();
    }
    let specifications: &[(&str, &str)] = match service {
        "bb" => &[
            ("preparation_id", "submission_status"),
            ("job_id", "script_status"),
        ],
        "young" => &[
            ("job_id", "registration_status"),
            ("schedule_id", "schedule_status"),
        ],
        "icourse" => &[("plan_id", "review_status")],
        "nan7" => &[("plan_id", "offer_status")],
        "mail" => &[
            ("draft_id", "send_status"),
            ("plan_id", "action_status"),
            ("job_id", "script_status"),
        ],
        "student" => &[("plan_id", "application_status")],
        "jw" => &[("job_id", "script_status")],
        "drive" | "spaces" | "vlab" => &[("plan_id", "status")],
        _ => &[],
    };
    let mut output = Vec::new();
    for (key, suffix) in specifications {
        if value[*key]
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.chars().count() <= 160)
        {
            let mut arguments = Map::new();
            arguments.insert((*key).into(), value[*key].clone());
            output.push(json!({"tool":format!("school_{service}_{suffix}"),"arguments":arguments}));
        }
    }
    for key in ["result", "preparation", "related_task"] {
        if value[key].is_object() {
            output.extend(links(service, &value[key], depth + 1));
        }
    }
    output
}
fn append_links(task: &mut Value, new: Vec<Value>) {
    let current = task["links"].as_array_mut().unwrap();
    for link in new {
        if !current.contains(&link) {
            current.push(link);
        }
    }
}
fn blocked(result: Value) -> Result<Value> {
    Ok(json!({"claimed":false,"result":result}))
}
pub fn claim(input: &Value) -> Result<Value> {
    let id = identifier(&input["task_id"])?;
    let tool = string(input, "tool")?;
    let authorized = input["authorized"]
        .as_bool()
        .ok_or_else(|| invalid("authorized 必须是布尔值。"))?;
    let mut arguments = input
        .get("arguments")
        .filter(|v| v.is_object())
        .ok_or_else(Error::invalid)?
        .clone();
    if canonical(&arguments).len() > 128 * 1024 {
        return Err(invalid("参数过大；附件用已授权文件路径，不传文件内容。"));
    }
    account(input.get("current_account").ok_or_else(Error::invalid)?)?;
    let mut db = database(input, true)?;
    let tx = db
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| storage_error())?;
    let mut task = load(&tx, id)?;
    let workflow = string(&task, "workflow")?;
    let service = string(&catalog::workflow(workflow)?, "service")?.to_owned();
    let mode = catalog::mode(workflow, tool)?;
    if !task["account"].is_null() && task["account"] != input["current_account"] {
        return Err(invalid(
            "当前账号与原任务不一致或身份不可用；恢复原账号后继续，不迁移原任务。",
        ));
    }
    if mode == "execute" && arguments.get("authorized").is_some() {
        arguments["authorized"] = json!(authorized);
    }
    guard(&arguments, authorized, 0)?;
    if matches!(mode.as_str(), "execute" | "login") && !authorized {
        return blocked(
            json!({"state":"authorization_required","dispatched":false,"tool":tool,
            "note":"只有用户已授权这个目标和具体参数时填 authorized=true；已有授权无需重复询问。"}),
        );
    }
    let calls = task["calls"].as_array().unwrap();
    if calls.len() >= 100 {
        return blocked(
            json!({"state":"step_limit","dispatched":false,"note":"任务达到100次调用上限；核实已有回执，不继续循环。"}),
        );
    }
    if mode != "read" && (truthy(&task["write_blocked"]) || calls.iter().any(unresolved)) {
        return blocked(
            json!({"state":"needs_verification","dispatched":false,"links":task["links"],"retry_write_allowed":false}),
        );
    }
    let key = key(tool, &arguments);
    if mode == "execute" {
        if let Some(previous) = calls
            .iter()
            .find(|c| c["key"] == key || c["tool"] == tool && c["arguments"] == arguments)
        {
            return blocked(
                json!({"state":"already_attempted","dispatched":false,"receipt":previous["receipt"],"links":task["links"],"retry_write_allowed":false}),
            );
        }
    }
    let index = calls.len();
    task["calls"].as_array_mut().unwrap().push(json!({"tool":tool,"arguments":arguments,"key":key,"mode":mode,"authorized":authorized,"started_at":now(),"status":"in_flight"}));
    if task["account"].is_null() {
        task["account"] = input["current_account"].clone();
    }
    task["state"] = json!("running");
    append_links(&mut task, links(&service, &arguments, 0));
    save(&tx, &mut task)?;
    tx.commit().map_err(|_| storage_error())?;
    Ok(
        json!({"claimed":true,"index":index,"key":key,"mode":mode,"service":service,"arguments":arguments}),
    )
}
fn claimed_call(task: &Value, input: &Value) -> Result<usize> {
    let index = input["index"]
        .as_u64()
        .and_then(|i| usize::try_from(i).ok())
        .ok_or_else(Error::invalid)?;
    let call = task["calls"]
        .as_array()
        .unwrap()
        .get(index)
        .ok_or_else(Error::invalid)?;
    if call["key"]
        != *input
            .get("key")
            .filter(|v| v.is_string())
            .ok_or_else(Error::invalid)?
        || call["status"] != "in_flight"
    {
        return Err(invalid("调用状态已变化；核实原回执，不能覆盖或重放。"));
    }
    let mode = catalog::mode(string(task, "workflow")?, string(call, "tool")?)?;
    if call["mode"] != mode {
        return Err(storage_error());
    }
    Ok(index)
}
fn resolve(
    task: &mut Value,
    service: &str,
    tool: &str,
    arguments: &Value,
    result: &Value,
    index: usize,
) {
    let before_claim = ["drive", "spaces", "vlab", "student"].contains(&service)
        && result["state"] == "ready"
        && result["can_execute"] == true;
    if !VERIFIED.contains(&tool) || result["state"] != "verified" && !before_claim {
        return;
    }
    let supplied = links(service, arguments, 0);
    let checked: Vec<_> = links(service, result, 0)
        .into_iter()
        .filter(|l| supplied.contains(l) && l["tool"] == tool)
        .collect();
    for previous in &mut task["calls"].as_array_mut().unwrap()[..index] {
        if previous["mode"] == "read"
            || !unresolved(previous)
            || before_claim && previous["status"] != "failed_or_unknown"
        {
            continue;
        }
        let mut references = links(service, &previous["arguments"], 0);
        references.extend(links(service, &previous["receipt"], 0));
        if checked.iter().any(|l| references.contains(l)) {
            previous["resolved_by"] = json!(index);
            previous["resolution"] = json!(if before_claim {
                "no_write_claimed"
            } else {
                "verified_receipt"
            });
        }
    }
}
pub fn finish(input: &Value) -> Result<Value> {
    let result = input
        .get("result")
        .filter(|v| v.is_object())
        .ok_or_else(Error::invalid)?;
    let mut db = database(input, true)?;
    let tx = db
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| storage_error())?;
    let mut task = load(&tx, identifier(&input["task_id"])?)?;
    let index = claimed_call(&task, input)?;
    let service = string(&catalog::workflow(string(&task, "workflow")?)?, "service")?.to_owned();
    let tool = string(&task["calls"][index], "tool")?.to_owned();
    let arguments = task["calls"][index]["arguments"].clone();
    task["calls"][index]["status"] = json!("returned");
    task["calls"][index]["receipt"] = receipt(result, 0);
    task["calls"][index]["finished_at"] = json!(now());
    append_links(&mut task, links(&service, result, 0));
    resolve(&mut task, &service, &tool, &arguments, result, index);
    let blocked = task["calls"].as_array().unwrap().iter().any(unresolved);
    task["write_blocked"] = json!(blocked);
    let status = state(result).as_str().unwrap_or("");
    let remote_waiting =
        service == "vlab" && status == "executing" && truthy(&result["result"]["remote_directory"]);
    task["state"] = json!(if remote_waiting {
        "waiting"
    } else if blocked {
        "needs_verification"
    } else if truthy(&result["input_guidance"])
        || [
            "needs_input",
            "authentication_required",
            "waiting_for_verification"
        ]
        .contains(&status)
    {
        "needs_input"
    } else if ["scheduled", "waiting", "monitoring", "running"].contains(&status) {
        "waiting"
    } else if status == "verified" && VERIFIED.contains(&tool.as_str()) {
        "verified"
    } else {
        "active"
    });
    save(&tx, &mut task)?;
    tx.commit().map_err(|_| storage_error())?;
    Ok(task)
}
pub fn failure(input: &Value) -> Result<Value> {
    let problem = input
        .get("problem")
        .filter(|v| v.is_object())
        .ok_or_else(Error::invalid)?;
    let mut db = database(input, true)?;
    let tx = db
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| storage_error())?;
    let mut task = load(&tx, identifier(&input["task_id"])?)?;
    let index = claimed_call(&task, input)?;
    let service = string(&catalog::workflow(string(&task, "workflow")?)?, "service")?.to_owned();
    let mode = string(&task["calls"][index], "mode")?;
    let local = mode == "prepare"
        && ["drive", "spaces", "vlab", "digest", "student"].contains(&service.as_str());
    let may_have_written = mode != "read" && !local;
    task["calls"][index]["status"] = json!(if may_have_written {
        "failed_or_unknown"
    } else {
        "failed_before_write"
    });
    // Keep only bounded status metadata, never raw exception text or tool bodies.
    let mut summary = receipt(problem, 0);
    for key in [
        "error_code",
        "error_id",
        "diagnostic_saved",
        "operation_failed",
        "completed",
    ] {
        if let Some(value) = problem.get(key) {
            if value.is_boolean() || value.as_str().is_some_and(|s| s.len() <= 160) {
                summary[key] = value.clone();
            }
        }
    }
    task["calls"][index]["receipt"] = summary;
    task["calls"][index]["finished_at"] = json!(now());
    task["state"] = json!(if may_have_written {
        "needs_verification"
    } else {
        "stopped"
    });
    task["write_blocked"] = json!(truthy(&task["write_blocked"]) || may_have_written);
    save(&tx, &mut task)?;
    tx.commit().map_err(|_| storage_error())?;
    Ok(task)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_canonical_float_and_unicode() {
        assert_eq!(canonical(&json!({"中":1e-7,"a":1.0,"z":-0.0,"é":1e16,"b":0.00001,"c":0.0001,"d":1e15})),
            "{\"a\": 1.0, \"b\": 1e-05, \"c\": 0.0001, \"d\": 1000000000000000.0, \"z\": -0.0, \"é\": 1e+16, \"中\": 1e-07}");
    }
    #[test]
    fn authoritative_links_only_resolve_same_reference() {
        let mut task = json!({"calls":[{"mode":"execute","status":"failed_or_unknown","key":"permanent","arguments":{"preparation_id":"a"}},{}]});
        resolve(
            &mut task,
            "bb",
            "school_bb_submission_status",
            &json!({"preparation_id":"b"}),
            &json!({"state":"verified","preparation_id":"b"}),
            1,
        );
        assert!(unresolved(&task["calls"][0]));
        resolve(
            &mut task,
            "bb",
            "school_bb_submission_status",
            &json!({"preparation_id":"a"}),
            &json!({"state":"verified","preparation_id":"a"}),
            1,
        );
        assert!(!unresolved(&task["calls"][0]));
        assert_eq!(task["calls"][0]["key"], "permanent");
    }
}
