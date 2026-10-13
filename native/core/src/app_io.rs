//! Desktop entry-point IO. Settings secrets, upload bytes, and recovery transactions
//! stay in this process; the UI/temporary coordinator only supplies explicit inputs.
use crate::{model, storage, strict_json, Error, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

const MAX_UPLOAD: u64 = 50 * 1024 * 1024;
const MAX_CHUNK: usize = 1024 * 1024;

fn storage_error() -> Error {
    Error::new(
        "storage_failure",
        "本机应用记录无法读写；已有记录保留，请检查磁盘。",
    )
}
fn invalid_file() -> Error {
    Error::new(
        "invalid_arguments",
        "附件无效、未完整传输，或不属于本机已选择文件。",
    )
}
fn read(path: &Path, default: Value) -> Result<Value> {
    storage::private_read(&json!({"path":path,"default":default}))
}
fn write(path: &Path, value: &Value) -> Result<()> {
    storage::private_write(&json!({"path":path,"value":value}))?;
    Ok(())
}
fn regular(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| {
        if !m.is_file() || m.file_type().is_symlink() {
            return false;
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if m.file_attributes() & 0x400 != 0 {
                return false;
            }
        }
        true
    })
}
fn checked_directory(path: &Path) -> Result<()> {
    let m = fs::symlink_metadata(path).map_err(|_| storage_error())?;
    if !m.is_dir() || m.file_type().is_symlink() {
        return Err(storage_error());
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if m.file_attributes() & 0x400 != 0 {
            return Err(storage_error());
        }
    }
    Ok(())
}
fn root(input: &Value, initialize: bool) -> Result<PathBuf> {
    let path = PathBuf::from(input["root"].as_str().ok_or_else(Error::invalid)?);
    if !path.is_absolute() {
        return Err(Error::invalid());
    }
    if initialize {
        fs::create_dir_all(&path).map_err(|_| storage_error())?;
    }
    checked_directory(&path)?;
    let marker = path.join("school-desktop.json");
    if initialize && !marker.try_exists().map_err(|_| storage_error())? {
        for entry in fs::read_dir(&path).map_err(|_| storage_error())? {
            if entry.map_err(|_| storage_error())?.file_name() != "diagnostics" {
                return Err(Error::new(
                    "invalid_arguments",
                    "应用数据目录须为空或是本应用创建的目录。",
                ));
            }
        }
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&marker)
            .map_err(|_| storage_error())?;
        file.write_all(br#"{"application":"school-mcp-desktop","version":1}"#)
            .and_then(|_| file.sync_all())
            .map_err(|_| storage_error())?;
    }
    if !regular(&marker) || fs::metadata(&marker).map_err(|_| storage_error())?.len() > 4096 {
        return Err(storage_error());
    }
    let marker: Value = strict_json::parse(&fs::read(marker).map_err(|_| storage_error())?)
        .map_err(|_| storage_error())?;
    if marker["application"] != "school-mcp-desktop" {
        return Err(Error::new("invalid_arguments", "不是学校应用数据目录。"));
    }
    let profile = path.join("profile");
    if initialize {
        fs::create_dir_all(&profile).map_err(|_| storage_error())?;
    }
    checked_directory(&profile)?;
    Ok(path)
}
// Short cross-process lock for settings transitions and upload offsets. No lock is
// held while waiting on a provider, so cancellation/status stay responsive.
fn locked<T>(root: &Path, action: impl FnOnce() -> Result<T>) -> Result<T> {
    let mut db = rusqlite::Connection::open(root.join("app-coordination.sqlite3"))
        .map_err(|_| storage_error())?;
    db.busy_timeout(Duration::from_secs(5))
        .map_err(|_| storage_error())?;
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(|_| storage_error())?;
    let result = action()?;
    tx.commit().map_err(|_| storage_error())?;
    Ok(result)
}
struct ModelRoot<'a> {
    path: &'a Path,
    candidate: bool,
}
impl ModelRoot<'_> {
    fn settings(&self) -> PathBuf {
        self.path.join(if self.candidate {
            "school-model-candidate.dpapi"
        } else {
            "model-settings.dpapi"
        })
    }
    fn check(&self) -> PathBuf {
        self.path.join(if self.candidate {
            "school-model-candidate-check.dpapi"
        } else {
            "model-check.dpapi"
        })
    }
}
fn settings(root: &ModelRoot<'_>) -> Result<Value> {
    let value = read(&root.settings(), json!({}))?;
    if !value.is_object() {
        return Err(storage_error());
    }
    Ok(value)
}
fn public(value: &Value) -> Value {
    json!({"base_url":value["base_url"].as_str().unwrap_or(""),
        "model":value["model"].as_str().unwrap_or(""),
        "configured":value["api_key"].as_str().is_some_and(|s| !s.is_empty()),
        "tested":value["tested"].as_bool().unwrap_or(false)})
}
fn resolve(root: &ModelRoot<'_>, input: &Value) -> Result<Value> {
    let mut value = input.clone();
    let object = value.as_object_mut().ok_or_else(Error::invalid)?;
    if object
        .get("api_key")
        .is_some_and(|v| !v.is_string() && !v.is_null())
    {
        return Err(Error::invalid());
    }
    if object
        .get("api_key")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        let old = settings(root)?;
        let url =
            model::normalize_url(object.get("base_url").and_then(Value::as_str).unwrap_or(""))?;
        if old["base_url"] != url {
            return Err(Error::new(
                "invalid_arguments",
                "更换 API 地址时请重新填写密钥。",
            ));
        }
        object.insert("api_key".into(), old["api_key"].clone());
    }
    serde_json::to_value(model::Settings::parse(&value)?).map_err(|_| Error::invalid())
}
fn digest(value: &Value) -> Result<String> {
    Ok(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).map_err(|_| Error::invalid())?)
    ))
}
fn identifier(value: &Value) -> Result<&str> {
    value
        .as_str()
        .filter(|s| {
            s.len() == 32
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .ok_or_else(Error::invalid)
}
fn model_config(root: &ModelRoot<'_>) -> Result<Value> {
    let old = settings(root)?;
    if !old["api_key"].as_str().is_some_and(|s| !s.is_empty()) {
        return Err(Error::new("invalid_arguments", "请先保存模型 API 配置。"));
    }
    let input = json!({"base_url":old["base_url"],"model":old["model"],"api_key":old["api_key"]});
    serde_json::to_value(model::Settings::parse(&input)?).map_err(|_| Error::invalid())
}
fn test_begin(root: &ModelRoot<'_>) -> Result<Value> {
    locked(root.path, || {
        let config = model_config(root)?;
        let mut saved = config.clone();
        saved["tested"] = json!(false);
        write(&root.settings(), &saved)?;
        let id = uuid::Uuid::new_v4().simple().to_string();
        write(
            &root.check(),
            &json!({"id":id,"cancelled":false,"state":"running","settings_hash":digest(&config)?}),
        )?;
        Ok(json!({"id":id}))
    })
}
fn test_active(root: &ModelRoot<'_>, id: &str, config: &Value) -> Result<bool> {
    let state = read(&root.check(), json!({}))?;
    if state["id"] != id || state["state"] != "testing" || state["cancelled"] == true {
        return Ok(false);
    }
    if state["settings_hash"] != digest(config)? || model_config(root)? != *config {
        return Err(Error::new(
            "model_settings_changed",
            "模型配置已经改变，请重新测试当前配置。",
        ));
    }
    Ok(true)
}
fn stopped() -> Value {
    json!({"state":"stopped","connected":false,"school_data_sent":false,
        "note":"已停止模型连接测试；已发出的请求不会自动重试。"})
}
async fn checked_complete(
    root: &ModelRoot<'_>,
    id: &str,
    config: &Value,
    client: &model::Client,
    input: Value,
    deadline: tokio::time::Instant,
) -> Result<Option<Value>> {
    if !test_active(root, id, config)? {
        return Ok(None);
    }
    let mut future = std::pin::pin!(client.complete(&input));
    loop {
        if !test_active(root, id, config)? {
            return Ok(None);
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(Error::new(
                "model_timeout",
                "模型响应超时；不会自动重复请求。",
            ));
        }
        match tokio::time::timeout(Duration::from_millis(50), future.as_mut()).await {
            Ok(result) => {
                if !test_active(root, id, config)? {
                    return Ok(None);
                }
                return result.map(Some);
            }
            Err(_) => continue,
        }
    }
}
async fn test_connection(
    root: &ModelRoot<'_>,
    input: &Value,
    client: &model::Client,
) -> Result<Value> {
    let id = identifier(&input["id"])?;
    let config = model_config(root)?;
    let claimed = locked(root.path, || {
        let path = root.check();
        let mut state = read(&path, json!({}))?;
        if state["id"] != id || state["cancelled"] == true {
            return Ok(false);
        }
        if state["state"] != "running" {
            return Err(Error::new(
                "invalid_arguments",
                "这次模型测试已经开始或完成，不能重复执行。",
            ));
        }
        state["state"] = json!("testing");
        write(&path, &state)?;
        Ok(true)
    })?;
    if !claimed {
        return Ok(stopped());
    }
    let deadline = tokio::time::Instant::now() + Duration::from_secs(125);
    let tool = json!({"type":"function","function":{"name":"connection_check","description":"Return the connection check.",
        "parameters":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}}});
    let mut messages = json!([{"role":"user","content":"Call connection_check with ok=true exactly once. After receiving its result, reply with only the verification_code from the tool result. This is a compatibility test without personal data."}]);
    let first = checked_complete(
        root,
        id,
        &config,
        client,
        json!({"settings":config,"messages":messages,"tools":[tool]}),
        deadline,
    )
    .await?;
    let Some(first) = first else {
        return Ok(stopped());
    };
    let calls = first["tool_calls"].as_array();
    if !calls.is_some_and(|calls| {
        calls.len() == 1
            && calls[0]["function"]["name"] == "connection_check"
            && model::tool_arguments(&calls[0]["function"]["arguments"])
                .is_ok_and(|v| v == json!({"ok":true}))
    }) {
        return Err(Error::new(
            "model_tool_roundtrip",
            "模型未完成工具调用测试；请检查模型是否支持 function calling。",
        ));
    }
    let code = uuid::Uuid::new_v4().simple().to_string();
    let call_id = first["tool_calls"][0]["id"].clone();
    messages.as_array_mut().ok_or_else(Error::invalid)?.extend([
        first, json!({"role":"tool","tool_call_id":call_id,"content":json!({"ok":true,"verification_code":code}).to_string()})]);
    let second = checked_complete(
        root,
        id,
        &config,
        client,
        json!({"settings":config,"messages":messages,"tools":[tool]}),
        deadline,
    )
    .await?;
    let Some(second) = second else {
        return Ok(stopped());
    };
    if second.get("tool_calls").is_some() || second["content"].as_str().unwrap_or("").trim() != code
    {
        return Err(Error::new(
            "model_tool_roundtrip",
            "模型能调用工具，但未正确读取工具结果；请更换模型后测试。",
        ));
    }
    locked(root.path, || {
        if !test_active(root, id, &config)? {
            return Ok(stopped());
        }
        let mut saved = config.clone();
        saved["tested"] = json!(true);
        // Persist the spent test token before the green badge. If either write
        // fails, another invocation cannot replay the provider request.
        write(
            &root.check(),
            &json!({"id":id,"state":"completed","cancelled":false}),
        )?;
        write(&root.settings(), &saved)?;
        Ok(
            json!({"connected":true,"tool_calling":true,"tool_result_read":true,"school_data_sent":false}),
        )
    })
}

fn upload_name(value: &Value) -> Result<&str> {
    let name = value.as_str().ok_or_else(invalid_file)?;
    if name.is_empty()
        || name.chars().count() > 180
        || name.ends_with(['.', ' '])
        || name
            .chars()
            .any(|c| c.is_control() || "<>:\"/\\|?*".contains(c))
        || name.starts_with(".upload.")
        || matches!(name, "." | "..")
    {
        return Err(invalid_file());
    }
    let stem = name.split('.').next().unwrap_or("").to_ascii_uppercase();
    if matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && matches!(stem.as_bytes()[3], b'1'..=b'9'))
    {
        return Err(invalid_file());
    }
    Ok(name)
}
fn uploads(root: &Path) -> Result<PathBuf> {
    let path = root.join("profile").join("uploads");
    fs::create_dir_all(&path).map_err(|_| storage_error())?;
    checked_directory(&path)?;
    Ok(path)
}
fn upload_dir(root: &Path, input: &Value) -> Result<PathBuf> {
    let id = identifier(&input["id"])?;
    let path = uploads(root)?.join(id);
    checked_directory(&path)?;
    Ok(path)
}
fn upload_manifest(path: &Path) -> Result<Value> {
    let meta = path.join(".upload.dpapi");
    if !regular(&meta) {
        return Err(invalid_file());
    }
    let value = read(&meta, Value::Null)?;
    upload_name(&value["name"])?;
    if !value["size"]
        .as_u64()
        .is_some_and(|n| (1..=MAX_UPLOAD).contains(&n))
    {
        return Err(invalid_file());
    }
    Ok(value)
}
fn upload_begin(root: &Path, input: &Value) -> Result<Value> {
    let name = upload_name(&input["name"])?;
    let size = input["size"]
        .as_u64()
        .filter(|n| (1..=MAX_UPLOAD).contains(n))
        .ok_or_else(invalid_file)?;
    let hash = input["sha256"]
        .as_str()
        .filter(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(invalid_file)?;
    let id = uuid::Uuid::new_v4().simple().to_string();
    let dir = uploads(root)?.join(&id);
    fs::create_dir(&dir).map_err(|_| storage_error())?;
    let result = (|| {
        write(
            &dir.join(".upload.dpapi"),
            &json!({"name":name,"size":size,"sha256":hash.to_ascii_lowercase(),"state":"uploading"}),
        )?;
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(dir.join(".upload.part"))
            .map_err(|_| storage_error())?;
        Ok(json!({"id":id,"offset":0}))
    })();
    if result.is_err() {
        let _ = fs::remove_file(dir.join(".upload.dpapi"));
        let _ = fs::remove_file(dir.join(".upload.part"));
        let _ = fs::remove_dir(dir);
    }
    result
}
fn upload_chunk(root: &Path, input: &Value) -> Result<Value> {
    let encoded = input["content"]
        .as_str()
        .filter(|s| s.len() <= MAX_CHUNK.div_ceil(3) * 4)
        .ok_or_else(invalid_file)?;
    let bytes = STANDARD.decode(encoded).map_err(|_| invalid_file())?;
    if bytes.is_empty() || bytes.len() > MAX_CHUNK {
        return Err(invalid_file());
    }
    locked(root, || {
        let dir = upload_dir(root, input)?;
        let manifest = upload_manifest(&dir)?;
        let part = dir.join(".upload.part");
        if manifest["state"] != "uploading" || !regular(&part) {
            return Err(invalid_file());
        }
        let size = fs::metadata(&part).map_err(|_| storage_error())?.len();
        if input["offset"].as_u64() != Some(size)
            || size + bytes.len() as u64 > manifest["size"].as_u64().ok_or_else(invalid_file)?
        {
            return Err(invalid_file());
        }
        fs::OpenOptions::new()
            .append(true)
            .open(&part)
            .and_then(|mut f| f.write_all(&bytes).and_then(|_| f.sync_all()))
            .map_err(|_| storage_error())?;
        Ok(json!({"offset":size + bytes.len() as u64}))
    })
}
fn upload_finish(root: &Path, input: &Value) -> Result<Value> {
    locked(root, || {
        let dir = upload_dir(root, input)?;
        let mut manifest = upload_manifest(&dir)?;
        let target = dir.join(upload_name(&manifest["name"])?);
        let part = dir.join(".upload.part");
        if manifest["state"] != "uploading" || !regular(&part) || target.exists() {
            return Err(invalid_file());
        }
        let mut file = fs::File::open(&part).map_err(|_| storage_error())?;
        if file.metadata().map_err(|_| storage_error())?.len()
            != manifest["size"].as_u64().ok_or_else(invalid_file)?
        {
            return Err(invalid_file());
        }
        let mut hash = Sha256::new();
        let mut buf = [0_u8; 64 * 1024];
        loop {
            let count = file.read(&mut buf).map_err(|_| storage_error())?;
            if count == 0 {
                break;
            }
            hash.update(&buf[..count]);
        }
        if manifest["sha256"] != format!("{:x}", hash.finalize()) {
            return Err(invalid_file());
        }
        // Each append already fsyncs its writable handle. Windows cannot flush
        // this read-only verification handle (FlushFileBuffers needs write access).
        drop(file);
        // Persist the terminal gate first. A crash cannot authorize appending to a
        // file that may already have been published; abort never removes target.
        manifest["state"] = json!("finishing");
        write(&dir.join(".upload.dpapi"), &manifest)?;
        fs::rename(&part, &target).map_err(|_| storage_error())?;
        manifest["state"] = json!("completed");
        write(&dir.join(".upload.dpapi"), &manifest)?;
        Ok(json!({"name":manifest["name"],"path":target,"size":manifest["size"]}))
    })
}
fn upload_abort(root: &Path, input: &Value) -> Result<Value> {
    locked(root, || {
        let dir = upload_dir(root, input)?;
        let manifest = upload_manifest(&dir)?;
        if manifest["state"] != "uploading" {
            return Ok(json!({"aborted":false}));
        }
        // Never recurse or delete a completed user file; only our own staging files.
        for name in [".upload.part", ".upload.dpapi"] {
            let path = dir.join(name);
            if !regular(&path) {
                return Err(invalid_file());
            }
            fs::remove_file(path).map_err(|_| storage_error())?;
        }
        fs::remove_dir(dir).map_err(|_| storage_error())?;
        Ok(json!({"aborted":true}))
    })
}
fn validate_files(root: &Path, input: &Value) -> Result<Value> {
    let supplied = input["files"]
        .as_array()
        .filter(|a| a.len() <= 10)
        .ok_or_else(invalid_file)?;
    if supplied.is_empty() {
        return Ok(json!([]));
    }
    let directory = fs::canonicalize(uploads(root)?).map_err(|_| invalid_file())?;
    let mut paths = Vec::new();
    for value in supplied {
        let supplied = PathBuf::from(value.as_str().ok_or_else(invalid_file)?);
        let path = fs::canonicalize(&supplied).map_err(|_| invalid_file())?;
        if !path.starts_with(&directory)
            || !regular(&supplied)
            || path
                .file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with(".upload."))
        {
            return Err(invalid_file());
        }
        // Reject junctions/reparse points in every descendant, even if their final
        // target happens to be in the uploads tree.
        let mut ancestor = supplied.parent();
        while let Some(parent) = ancestor {
            checked_directory(parent)?;
            if fs::canonicalize(parent).is_ok_and(|p| p == directory) {
                break;
            }
            ancestor = parent.parent();
        }
        let normalized = path.to_string_lossy();
        paths.push(
            normalized
                .strip_prefix(r"\\?\")
                .unwrap_or(&normalized)
                .to_owned(),
        );
    }
    Ok(json!(paths))
}
fn recover(root: &Path, input: &Value, inspect: bool) -> Result<Value> {
    let mut db = rusqlite::Connection::open(root.join("profile/desktop-chats.sqlite3"))
        .map_err(|_| storage_error())?;
    db.busy_timeout(Duration::from_secs(10))
        .map_err(|_| storage_error())?;
    db.execute(
        "CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, payload BLOB NOT NULL)",
        [],
    )
    .map_err(|_| storage_error())?;
    let tx = db
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(|_| storage_error())?;
    let mut statement = tx
        .prepare("SELECT id,payload FROM chats")
        .map_err(|_| storage_error())?;
    let rows = statement
        .query_map([], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, Vec<u8>>(1)?))
        })
        .map_err(|_| storage_error())?;
    let mut count = 0;
    for row in rows {
        let (id, raw) = row.map_err(|_| storage_error())?;
        let mut chat =
            strict_json::parse(&storage::crypt(&raw, true)?).map_err(|_| storage_error())?;
        if chat["state"] != "running" {
            continue;
        }
        count += 1;
        if inspect {
            continue;
        }
        chat["state"] = json!("interrupted");
        chat["updated_at"] =
            json!(chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false));
        let events = chat["events"].as_array_mut().ok_or_else(storage_error)?;
        events.push(json!({"kind":"notice","text":"上次对话中断。先核实关联任务，不能重复提交。","detail":input["incident"]}));
        let encoded = serde_json::to_vec(&chat).map_err(|_| storage_error())?;
        if encoded.len() > 8 * 1024 * 1024 {
            return Err(storage_error());
        }
        tx.execute(
            "UPDATE chats SET payload=? WHERE id=?",
            rusqlite::params![storage::crypt(&encoded, false)?, id],
        )
        .map_err(|_| storage_error())?;
    }
    drop(statement);
    tx.commit().map_err(|_| storage_error())?;
    Ok(json!({"recovered":count}))
}

pub async fn dispatch(input: &Value, client: &model::Client) -> Result<Value> {
    let action = input["action"].as_str().ok_or_else(Error::invalid)?;
    let root = root(input, action == "initialize")?;
    let candidate = match input.get("slot").and_then(Value::as_str) {
        None | Some("primary") => false,
        Some("candidate") => true,
        _ => return Err(Error::invalid()),
    };
    let model_root = ModelRoot {
        path: &root,
        candidate,
    };
    match action {
        "initialize" => Ok(json!({"root":root,"profile":root.join("profile")})),
        "public_settings" => Ok(public(&settings(&model_root)?)),
        "resolve_settings" => resolve(&model_root, &input["value"]),
        "save_settings" => locked(&root, || {
            let mut config = resolve(&model_root, &input["value"])?;
            config["tested"] = json!(false);
            write(&model_root.settings(), &config)?;
            Ok(public(&config))
        }),
        "list_models" => {
            let mut value = input["value"].clone();
            if !value.is_object() {
                return Err(Error::invalid());
            }
            if value["model"].as_str().is_none_or(str::is_empty) {
                value["model"] = json!("model-discovery");
            }
            client
                .list(&json!({"settings":resolve(&model_root, &value)?}))
                .await
        }
        "test_begin" => test_begin(&model_root),
        "test_connection" => test_connection(&model_root, input, client).await,
        "test_cancel" => locked(&root, || {
            let id = identifier(&input["id"])?;
            let path = model_root.check();
            let mut state = read(&path, json!({}))?;
            if state["id"] == id && matches!(state["state"].as_str(), Some("running" | "testing")) {
                state["cancelled"] = json!(true);
                write(&path, &state)?;
            }
            Ok(json!({"stop_requested":true}))
        }),
        "commit_candidate" => locked(&root, || {
            if !candidate {
                return Err(Error::invalid());
            }
            let value = settings(&model_root)?;
            if value["tested"] != true {
                return Err(Error::new("invalid_arguments", "候选模型未完成连接测试。"));
            }
            model_config(&model_root)?;
            write(&root.join("model-settings.dpapi"), &value)?;
            Ok(public(&value))
        }),
        "discard_candidate" => locked(&root, || {
            if !candidate {
                return Err(Error::invalid());
            }
            for path in [model_root.settings(), model_root.check()] {
                if path.try_exists().map_err(|_| storage_error())? {
                    if !regular(&path) {
                        return Err(storage_error());
                    }
                    fs::remove_file(path).map_err(|_| storage_error())?;
                }
            }
            Ok(json!({"discarded":true}))
        }),
        "upload_begin" => upload_begin(&root, input),
        "upload_chunk" => upload_chunk(&root, input),
        "upload_finish" => upload_finish(&root, input),
        "upload_abort" => upload_abort(&root, input),
        "validate_files" => validate_files(&root, input),
        "recovery_pending" => recover(&root, input, true),
        "recover" => recover(&root, input, false),
        _ => Err(Error::invalid()),
    }
}
