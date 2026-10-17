//! Bounded local support diagnostics, never a business journal or replay signal.
//! Every write and every export passes the same compiled field allowlist.
use crate::{catalog, recovery, Error, Result};
use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

include!("safety_log_files.rs");
const MAX_EVENTS: i64 = 500;
const RETENTION: i64 = 14 * 86400;
const MAX_BYTES: u64 = 8 * 1024 * 1024;
const FILES: &[&str] = &[
    "events.sqlite3",
    "events.sqlite3-journal",
    "events.sqlite3-wal",
    "events.sqlite3-shm",
];
const STAGES: &[&str] = &[
    "startup",
    "api",
    "model",
    "tool",
    "login",
    "connection",
    "document",
    "persistence",
    "background",
    "recovery",
    "ui",
];
const SERVICES: &[&str] = &[
    "app", "agent", "model", "mail", "bb", "jw", "young", "finance", "library", "teach", "icourse",
    "nan7", "portal", "student", "spaces", "drive", "vlab", "web", "digest",
];
const EXCEPTIONS: &[&str] = &[
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "OSError",
    "PermissionError",
    "FileNotFoundError",
    "TimeoutError",
    "RuntimeError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "HTTPStatusError",
    "ValidationError",
    "ModelError",
    "ServiceError",
    "ToolError",
    "OperationalError",
    "DatabaseError",
    "Error",
];
const CODES: &[&str] = &[
    "invalid_tool_arguments",
    "invalid_model_response",
    "empty_response",
    "model_refusal",
    "incomplete_model_response",
    "model_rate_limited",
    "model_authentication_failed",
    "model_unavailable",
    "model_timeout",
    "model_connection_failed",
    "tool_call_unsupported",
    "model_error",
    "startup_failed",
    "storage_failed",
    "interrupted",
    "ui_error",
    "model_list_invalid",
    "model_connection",
    "context_capacity",
    "model_tool_roundtrip",
    "model_auth",
    "model_forbidden",
    "model_not_found",
    "model_request_rejected",
    "model_incompatible",
    "model_authentication",
    "model_balance",
    "model_http_error",
    "missing_file",
    "unsupported_format",
    "file_too_large",
    "unreadable_file",
    "text_too_large",
    "storage_failure",
    "storage_capacity",
    "unsupported_platform",
    "model_settings_changed",
    "page_changed",
    "response_too_large",
];
const OUTCOMES: &[(&str, &str)] = &[
    ("not_started", "操作尚未开始。"),
    ("read_failed", "本次查询未完成。"),
    ("local_failed", "本机处理未完成。"),
    ("unknown", "操作可能已产生效果，先查询原回执，不重复提交。"),
    ("interrupted", "任务中断，先核实原任务再继续。"),
];
const WRITE_NOTE: &str =
    "诊断日志无法写入，请检查磁盘空间和目录权限；本次错误编号仅供当前界面使用。";
const READ_NOTE: &str = "诊断记录无法读取，请检查磁盘、目录权限或日志文件是否损坏。";
static WRITERS: Mutex<()> = Mutex::new(());

fn error() -> Error {
    Error::new("storage_failure", "诊断记录不可用；业务记录保留。")
}
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
        .min(i64::MAX as u64) as i64
}
fn identifier() -> String {
    format!(
        "ERR-{}",
        uuid::Uuid::new_v4().simple().to_string()[..16].to_ascii_uppercase()
    )
}
fn native_build() -> &'static str {
    static BUILD: OnceLock<String> = OnceLock::new();
    BUILD.get_or_init(|| {
        // Hash only our own executable, never scan the user's profile or log paths.
        let read = || -> std::io::Result<String> {
            let mut file = File::open(std::env::current_exe()?)?;
            if file.metadata()?.len() > 512 * 1024 * 1024 {
                return Err(std::io::ErrorKind::InvalidData.into());
            }
            let mut hash = Sha256::new();
            let mut chunk = [0; 65536];
            loop {
                let count = file.read(&mut chunk)?;
                if count == 0 {
                    break;
                }
                hash.update(&chunk[..count]);
            }
            Ok(format!("{:x}", hash.finalize()))
        };
        read().unwrap_or_else(|_| "unavailable".into())
    })
}
fn valid_id(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        s.len() == 20
            && s.starts_with("ERR-")
            && s[4..]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'A'..=b'F').contains(&b))
    })
}
fn choice<'a>(value: &'a Value, choices: &[&str], default: &'a str) -> &'a str {
    value
        .as_str()
        .filter(|v| choices.contains(v))
        .unwrap_or(default)
}
fn number(value: &Value, maximum: u64, default: u64) -> u64 {
    if let Some(n) = value.as_u64() {
        n.min(maximum)
    } else if value.as_i64().is_some() {
        0
    } else {
        default
    }
}
fn tool_allowed(tool: &str) -> bool {
    [
        "school_agent_call",
        "school_agent_start",
        "school_agent_tools",
        "school_agent_route",
        "school_agent_task",
        "school_agent_tasks",
        "school_local_list_files",
        "school_local_read_file",
        "school_local_create_file",
    ]
    .contains(&tool)
        || catalog::workflows()
            .ok()
            .and_then(|v| {
                v.as_object()
                    .map(|o| o.values().any(|flow| flow["tools"].get(tool).is_some()))
            })
            .unwrap_or(false)
}

/// Accept only fixed codes, source-relative whitelisted frames and bounded counts.
pub fn clean(event: &Value) -> Value {
    let code = event["code"].as_str().unwrap_or("");
    let recovery_code = recovery::failure(&json!({"code":code}))
        .ok()
        .is_some_and(|v| v["error_code"] == code);
    let frames: Vec<Value> = event["frames"]
        .as_array()
        .into_iter()
        .flatten()
        .take(8)
        .filter_map(|f| {
            let name = f["file"].as_str()?;
            if !SOURCE_FILES.contains(&name) || !(f["line"].is_i64() || f["line"].is_u64()) {
                return None;
            }
            Some(json!({"file":name,"line":number(&f["line"],100000,1).max(1)}))
        })
        .collect();
    let build = event["build"]
        .as_str()
        .filter(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .unwrap_or("unavailable");
    let tool = event["tool"]
        .as_str()
        .filter(|s| tool_allowed(s))
        .unwrap_or("");
    let outcome = event["outcome"]
        .as_str()
        .filter(|s| OUTCOMES.iter().any(|v| v.0 == *s))
        .unwrap_or("unknown");
    json!({"error_id":if valid_id(&event["error_id"]) {event["error_id"].clone()} else {json!(identifier())},
        "code":if recovery_code || CODES.contains(&code) {code} else {"unavailable"},
        "service":choice(&event["service"],SERVICES,"app"),"stage":choice(&event["stage"],STAGES,"tool"),
        "outcome":outcome,"tool":tool,"exception":choice(&event["exception"],EXCEPTIONS,"Exception"),
        "frames":frames,"first_at":number(&event["first_at"],4102444800,0),"last_at":number(&event["last_at"],4102444800,0),
        "count":number(&event["count"],1000000,1),"duration_ms":number(&event["duration_ms"],86400000,0),
        "build":build,"platform":choice(&event["platform"],&["win32","linux","darwin"],"other")})
}

// Python json.dumps(sort_keys=True) has ASCII names/values for signature fields.
// Preserve its spacing/order so mixed old/new writers aggregate the same incident.
fn signature_json(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut fields: Vec<_> = map.iter().collect();
            fields.sort_by_key(|(key, _)| *key);
            format!(
                "{{{}}}",
                fields
                    .iter()
                    .map(|(k, v)| format!("{}: {}", json!(k), signature_json(v)))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        Value::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(signature_json)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => value.to_string(),
    }
}
fn signature(event: &Value) -> String {
    let fields: serde_json::Map<String, Value> = [
        "code",
        "service",
        "stage",
        "tool",
        "outcome",
        "exception",
        "frames",
        "build",
    ]
    .iter()
    .map(|key| (key.to_string(), event[key].clone()))
    .collect();
    format!(
        "{:x}",
        Sha256::digest(signature_json(&Value::Object(fields)).as_bytes())
    )
}

fn plain(path: &Path, directory: bool) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            if meta.file_type().is_symlink()
                || (directory && !meta.is_dir())
                || (!directory && !meta.is_file())
            {
                return Err(error());
            }
            #[cfg(windows)]
            {
                use std::os::windows::fs::MetadataExt;
                if meta.file_attributes() & 0x400 != 0 {
                    return Err(error());
                }
            }
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(_) => Err(error()),
    }
}
fn validate_root(root: &Path) -> Result<()> {
    if !root.is_absolute()
        || root
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err(error());
    }
    for ancestor in root.ancestors() {
        plain(ancestor, true)?;
    }
    Ok(())
}
struct Lock {
    file: File,
    #[cfg(windows)]
    overlapped: Box<windows_sys::Win32::System::IO::OVERLAPPED>,
}
impl Lock {
    fn acquire(root: &Path) -> Result<Self> {
        validate_root(root)?;
        fs::create_dir_all(root).map_err(|_| error())?;
        validate_root(root)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(root, fs::Permissions::from_mode(0o700)).map_err(|_| error())?;
        }
        let path = root.join("events.lock");
        plain(&path, false)?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .open(path)
            .map_err(|_| error())?;
        if file.metadata().map_err(|_| error())?.len() == 0 {
            file.write_all(b"0")
                .and_then(|_| file.flush())
                .map_err(|_| error())?;
        }
        let deadline = Instant::now() + Duration::from_millis(600);
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::{
                LockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
            };
            let mut overlapped = Box::new(unsafe { std::mem::zeroed() });
            loop {
                if unsafe {
                    LockFileEx(
                        file.as_raw_handle(),
                        LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                        0,
                        1,
                        0,
                        &mut *overlapped,
                    )
                } != 0
                {
                    return Ok(Self { file, overlapped });
                }
                if Instant::now() >= deadline {
                    return Err(error());
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
        #[cfg(not(windows))]
        {
            loop {
                if file.try_lock().is_ok() {
                    return Ok(Self { file });
                }
                if Instant::now() >= deadline {
                    return Err(error());
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }
}
impl Drop for Lock {
    fn drop(&mut self) {
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            unsafe {
                windows_sys::Win32::Storage::FileSystem::UnlockFileEx(
                    self.file.as_raw_handle(),
                    0,
                    1,
                    0,
                    &mut *self.overlapped,
                );
            }
        }
        #[cfg(not(windows))]
        {
            let _ = self.file.unlock();
        }
    }
}
fn db<T>(root: &Path, at: i64, action: impl FnOnce(&Transaction<'_>) -> Result<T>) -> Result<T> {
    let _guard = WRITERS.lock().map_err(|_| error())?;
    let _lock = Lock::acquire(root)?;
    for name in FILES {
        plain(&root.join(name), false)?;
    }
    let path = root.join("events.sqlite3");
    if fs::metadata(&path).is_ok_and(|m| m.len() > MAX_BYTES) {
        return Err(error());
    }
    let mut connection = Connection::open(&path).map_err(|_| error())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).map_err(|_| error())?;
    }
    connection
        .busy_timeout(Duration::from_millis(500))
        .map_err(|_| error())?;
    connection.execute_batch("PRAGMA secure_delete=ON; PRAGMA journal_mode=DELETE; PRAGMA max_page_count=2048; CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, signature TEXT, last_at INTEGER, payload TEXT);").map_err(|_| error())?;
    let transaction = connection
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(|_| error())?;
    transaction
        .execute("DELETE FROM events WHERE last_at < ?", [at - RETENTION])
        .map_err(|_| error())?;
    let result = action(&transaction)?;
    transaction.commit().map_err(|_| error())?;
    Ok(result)
}

/// Returns a fresh incident ID even when diagnostics cannot be saved.
pub fn record(root: &Path, input: &Value) -> Value {
    record_at(root, input, now())
}
fn record_at(root: &Path, input: &Value, at: i64) -> Value {
    let id = identifier();
    let mut event = clean(input);
    if event["build"] == "unavailable" {
        event["build"] = json!(native_build());
    }
    event["error_id"] = json!(id);
    event["first_at"] = json!(at);
    event["last_at"] = json!(at);
    event["count"] = json!(1);
    event["platform"] = json!(if cfg!(windows) {
        "win32"
    } else if cfg!(target_os = "macos") {
        "darwin"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else {
        "other"
    });
    let signature = signature(&event);
    let saved = db(root, at, |db| {
        let row: Option<String>=db.query_row("SELECT payload FROM events WHERE signature=? AND last_at>=? ORDER BY last_at DESC LIMIT 1",params![signature,at-300],|row|row.get(0)).optional().map_err(|_|error())?;
        if let Some(row) = row {
            let previous = clean(&serde_json::from_str::<Value>(&row).map_err(|_| error())?);
            event["error_id"] = previous["error_id"].clone();
            event["first_at"] = previous["first_at"].clone();
            event["count"] = json!((previous["count"].as_u64().unwrap_or(1) + 1).min(1000000));
        }
        db.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?)",
            params![event["error_id"].as_str(), signature, at, event.to_string()],
        )
        .map_err(|_| error())?;
        db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY last_at DESC, rowid DESC LIMIT ?)",[MAX_EVENTS]).map_err(|_|error())?;
        Ok(())
    });
    if saved.is_ok() {
        json!({"error_id":event["error_id"],"diagnostic_saved":true})
    } else {
        json!({"error_id":id,"diagnostic_saved":false,"diagnostic_note":WRITE_NOTE})
    }
}
pub fn report(root: &Path) -> Value {
    report_at(root, now())
}
fn report_at(root: &Path, at: i64) -> Value {
    let result = db(root, at, |db| {
        let mut query = db
            .prepare("SELECT payload FROM events ORDER BY last_at DESC, rowid DESC LIMIT ?")
            .map_err(|_| error())?;
        let rows = query
            .query_map([MAX_EVENTS], |row| row.get::<_, String>(0))
            .map_err(|_| error())?;
        let mut events = Vec::new();
        for row in rows {
            let raw: Value =
                serde_json::from_str(&row.map_err(|_| error())?).map_err(|_| error())?;
            let mut event = clean(&raw);
            event["outcome_note"] = json!(OUTCOMES
                .iter()
                .find(|v| event["outcome"] == v.0)
                .map(|v| v.1)
                .unwrap_or(OUTCOMES[3].1));
            events.push(event);
        }
        Ok(events)
    });
    match result {
        Ok(events) => {
            json!({"schema_version":1,"available":true,"generated_at":at,"retention_days":14,"max_events":MAX_EVENTS,"events":events,"privacy":"仅含固定诊断字段；不含账号、凭据、网址、请求正文、对话、附件或截图。","automatic_upload":false})
        }
        Err(_) => {
            json!({"schema_version":1,"available":false,"events":[],"automatic_upload":false,"note":READ_NOTE})
        }
    }
}
pub fn summary(root: &Path) -> Value {
    let report = report(root);
    let events = report["events"].as_array().cloned().unwrap_or_default();
    json!({"available":report["available"],"incident_count":events.len(),"occurrence_count":events.iter().map(|v|v["count"].as_u64().unwrap_or(0)).sum::<u64>(),"latest_error_id":events.first().map(|v|v["error_id"].clone()),"note":report["note"],"automatic_upload":false})
}
pub fn export(root: &Path) -> Result<Value> {
    let value = report(root);
    if value["available"] != true {
        return Err(Error::new("storage_failure", READ_NOTE));
    }
    Ok(value)
}
pub fn clear(root: &Path) -> Result<Value> {
    let perform = || -> Result<()> {
        validate_root(root)?;
        if root.exists() {
            let _guard = WRITERS.lock().map_err(|_| error())?;
            let _lock = Lock::acquire(root)?;
            for name in FILES {
                plain(&root.join(name), false)?;
            }
            for name in FILES {
                match fs::remove_file(root.join(name)) {
                    Ok(()) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(_) => return Err(error()),
                }
            }
        }
        Ok(())
    };
    perform().map_err(|_| {
        Error::new(
            "storage_failure",
            "日志清理未完成，请检查磁盘和目录权限；业务记录保留。",
        )
    })?;
    Ok(json!({"cleared":true,"business_records_preserved":true}))
}
/// Enrich metadata only. Business failure, receipt and replay flags are unchanged.
pub fn attach(root: &Path, mut problem: Value, event: &Value) -> Value {
    if !problem.is_object() || valid_id(&problem["error_id"]) {
        return problem;
    }
    let mut fields = event.clone();
    if !fields.is_object() {
        fields = json!({});
    }
    fields["code"] = problem["error_code"].clone();
    let incident = record(root, &fields);
    for key in ["error_id", "diagnostic_saved", "diagnostic_note"] {
        if let Some(value) = incident.get(key) {
            problem[key] = value.clone();
        }
    }
    problem
}
pub fn dispatch(operation: &str, input: &Value) -> Result<Value> {
    if operation == "safety_log_clean" {
        return Ok(clean(&input["event"]));
    }
    let root = PathBuf::from(input["directory"].as_str().ok_or_else(Error::invalid)?);
    match operation {
        "safety_log_record" => Ok(record(&root, &input["event"])),
        "safety_log_report" => Ok(report(&root)),
        "safety_log_summary" => Ok(summary(&root)),
        "safety_log_export" => export(&root),
        "safety_log_clear" => clear(&root),
        _ => Err(Error::invalid()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Temp(PathBuf);
    impl Temp {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!(
                "school-diagnostics-{}",
                uuid::Uuid::new_v4().simple()
            )))
        }
        fn root(&self) -> PathBuf {
            self.0.join("diagnostics")
        }
    }
    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    const SECRET: &str = "synthetic-secret-canary-no-export";
    #[test]
    fn filters_input_and_resanitizes_tampered_rows() {
        let temp = Temp::new();
        let event = json!({"code":SECRET,"tool":SECRET,"service":SECRET,"stage":SECRET,"exception":SECRET,"frames":[{"file":SECRET,"line":1}],"build":SECRET,"message":SECRET});
        assert_eq!(record(&temp.root(), &event)["diagnostic_saved"], true);
        let bytes = fs::read(temp.root().join("events.sqlite3")).unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains(SECRET));
        let db = Connection::open(temp.root().join("events.sqlite3")).unwrap();
        db.execute("UPDATE events SET payload=?", [event.to_string()])
            .unwrap();
        drop(db);
        let report = export(&temp.root()).unwrap();
        assert!(!report.to_string().contains(SECRET));
        assert_eq!(report["events"][0]["code"], "unavailable");
        assert_eq!(report["events"][0]["frames"], json!([]));
    }
    #[test]
    fn aggregates_and_expires_with_the_legacy_schema() {
        let temp = Temp::new();
        let at = 1_800_000_000;
        let a = record_at(&temp.root(), &json!({"code":"timeout"}), at);
        let b = record_at(&temp.root(), &json!({"code":"timeout"}), at + 300);
        assert_eq!(a["error_id"], b["error_id"]);
        assert_eq!(report_at(&temp.root(), at + 300)["events"][0]["count"], 2);
        let c = record_at(&temp.root(), &json!({"code":"timeout"}), at + 601);
        assert_ne!(a["error_id"], c["error_id"]);
        assert_eq!(
            report_at(&temp.root(), at + 601 + RETENTION + 1)["events"],
            json!([])
        );
    }
    #[test]
    fn caps_records_and_numeric_fields() {
        let temp = Temp::new();
        let at = now();
        db(&temp.root(), at, |db| {
            for n in 0..510 {
                db.execute(
                    "INSERT INTO events VALUES (?,?,?,?)",
                    params![
                        format!("ERR-{n:016X}"),
                        format!("synthetic{n}"),
                        at,
                        json!({"error_id":format!("ERR-{n:016X}"),"count":u64::MAX}).to_string()
                    ],
                )
                .map_err(|_| error())?;
            }
            Ok(())
        })
        .unwrap();
        assert_eq!(
            record(&temp.root(), &json!({"duration_ms":u64::MAX}))["diagnostic_saved"],
            true
        );
        let report = report(&temp.root());
        assert_eq!(report["events"].as_array().unwrap().len(), 500);
        assert_eq!(report["events"][0]["duration_ms"], 86400000);
        assert_eq!(report["events"][1]["count"], 1000000);
        assert!(
            fs::metadata(temp.root().join("events.sqlite3"))
                .unwrap()
                .len()
                < MAX_BYTES
        );
    }
    #[test]
    fn corrupt_and_oversized_logs_fail_closed_and_clear_without_business_loss() {
        let temp = Temp::new();
        fs::create_dir_all(temp.root()).unwrap();
        fs::write(temp.0.join("receipt.json"), SECRET).unwrap();
        let path = temp.root().join("events.sqlite3");
        fs::write(&path, b"not a database").unwrap();
        let problem = json!({"error_code":"outcome_unknown","recovery":{"retry_write_allowed":false},"state":"needs_verification"});
        let enriched = attach(&temp.root(), problem.clone(), &json!({"outcome":"unknown"}));
        assert_eq!(enriched["diagnostic_saved"], false);
        assert!(valid_id(&enriched["error_id"]));
        assert_eq!(enriched["recovery"], problem["recovery"]);
        assert_eq!(report(&temp.root())["available"], false);
        assert!(export(&temp.root()).is_err());
        clear(&temp.root()).unwrap();
        assert_eq!(record(&temp.root(), &json!({}))["diagnostic_saved"], true);
        OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(MAX_BYTES + 1)
            .unwrap();
        assert_eq!(report(&temp.root())["available"], false);
        clear(&temp.root()).unwrap();
        assert_eq!(
            fs::read_to_string(temp.0.join("receipt.json")).unwrap(),
            SECRET
        );
    }
    #[test]
    fn concurrent_writers_keep_all_occurrences() {
        let temp = Temp::new();
        let threads: Vec<_> = (0..4)
            .map(|_| {
                let root = temp.root();
                std::thread::spawn(move || {
                    for _ in 0..5 {
                        assert_eq!(
                            record(&root, &json!({"code":"timeout"}))["diagnostic_saved"],
                            true
                        );
                    }
                })
            })
            .collect();
        for thread in threads {
            thread.join().unwrap();
        }
        assert_eq!(summary(&temp.root())["occurrence_count"], 20);
    }
    #[test]
    fn byte_lock_is_bounded_and_released() {
        let temp = Temp::new();
        let lock = Lock::acquire(&temp.root()).unwrap();
        let at = Instant::now();
        assert_eq!(record(&temp.root(), &json!({}))["diagnostic_saved"], false);
        assert!(at.elapsed() < Duration::from_secs(5));
        drop(lock);
        assert_eq!(record(&temp.root(), &json!({}))["diagnostic_saved"], true);
    }
    #[test]
    fn existing_incident_is_not_duplicated_and_untrusted_fields_are_ignored() {
        let temp = Temp::new();
        let incident = record(
            &temp.root(),
            &json!({"code":"startup_failed","stage":"startup"}),
        );
        let problem = json!({"error_id":incident["error_id"],"state":"needs_verification","diagnostic_saved":true});
        assert_eq!(
            attach(&temp.root(), problem.clone(), &json!({"message":SECRET})),
            problem
        );
        assert_eq!(summary(&temp.root())["occurrence_count"], 1);
        assert_eq!(report(&temp.root())["events"][0]["code"], "startup_failed");
    }
    #[test]
    fn frame_allowlist_and_integer_contract() {
        let event = clean(
            &json!({"error_id":"ERR-0123456789ABCDEF","frames":[{"file":"desktop/chat.py","line":u64::MAX},{"file":"desktop/chat.py","line":true},{"file":"../secret.py","line":3}],"count":true,"duration_ms":-5,"first_at":1.5,"platform":"private"}),
        );
        assert_eq!(
            event["frames"],
            json!([{"file":"desktop/chat.py","line":100000}])
        );
        assert_eq!(event["count"], 1);
        assert_eq!(event["duration_ms"], 0);
        assert_eq!(event["first_at"], 0);
        assert_eq!(event["platform"], "other");
    }
    #[cfg(unix)]
    #[test]
    fn refuses_symlinked_root_ancestor_and_database() {
        use std::os::unix::fs::symlink;
        let temp = Temp::new();
        fs::create_dir_all(temp.root()).unwrap();
        let target = temp.0.join("receipt.json");
        fs::write(&target, SECRET).unwrap();
        symlink(&target, temp.root().join("events.sqlite3")).unwrap();
        assert_eq!(record(&temp.root(), &json!({}))["diagnostic_saved"], false);
        assert!(clear(&temp.root()).is_err());
        symlink(temp.root(), temp.0.join("redirected")).unwrap();
        assert_eq!(
            record(&temp.0.join("redirected/child"), &json!({}))["diagnostic_saved"],
            false
        );
        assert_eq!(fs::read_to_string(target).unwrap(), SECRET);
    }
}
