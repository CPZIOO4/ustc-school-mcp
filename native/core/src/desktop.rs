//! Owned loopback desktop server. HTTP capabilities, operation lifetimes and
//! account boundaries are native; remaining business adapters have fixed names.
use crate::{app_io, chat_io, login, model, sessions, storage, strict_json, Error, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};

const MAX_JSON: usize = 200_000;
const MAX_UPLOAD: usize = 50 * 1024 * 1024;
const MAX_RESPONSE: usize = 64 * 1024 * 1024;
const SERVICES: &[(&str, &str, &str)] = &[
    ("mail", "学校邮箱", ""),
    ("bb", "Blackboard", "bb.session.dpapi"),
    ("jw", "教务系统", "jw.session.dpapi"),
    ("young", "青春科大", "young-session.dpapi"),
    ("finance", "财务平台", "finance-session.dpapi"),
    ("library", "图书馆", "library.session.dpapi"),
    ("teach", "教务处通知", ""),
    ("icourse", "评课社区", "icourse-session.dpapi"),
    ("nan7", "南七集市", "nan7-session.dpapi"),
    ("portal", "一网通办", "portal-campus-session.dpapi"),
    ("student", "学工与奖学金", "student-campus-session.dpapi"),
    ("spaces", "学习空间", "spaces-campus-session.dpapi"),
    ("vlab", "Vlab 虚拟机", "vlab-campus-session.dpapi"),
    ("drive", "科大云盘", "drive-campus-session.dpapi"),
];

fn invalid(message: &str) -> Error {
    Error::new("invalid_arguments", message)
}
fn storage_error() -> Error {
    Error::new("storage_failure", "本机记录无法读写；已有数据保留。")
}
fn now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}
fn read(path: &Path, default: Value) -> Result<Value> {
    storage::private_read(&json!({"path":path,"default":default}))
}
fn write(path: &Path, value: &Value) -> Result<()> {
    storage::private_write(&json!({"path":path,"value":value})).map(|_| ())
}
fn runtime() -> Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| storage_error())
}
fn app_call(root: &Path, action: &str, mut input: Value) -> Result<Value> {
    input["root"] = json!(root);
    input["action"] = json!(action);
    runtime()?.block_on(async { app_io::dispatch(&input, &model::Client::new()?).await })
}
fn plain(path: &Path, directory: bool) -> bool {
    fs::symlink_metadata(path).is_ok_and(|m| {
        if m.file_type().is_symlink() || if directory { !m.is_dir() } else { !m.is_file() } {
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
fn remove_owned(path: &Path) -> Result<()> {
    if !path.try_exists().map_err(|_| storage_error())? {
        return Ok(());
    }
    if !plain(path, false) {
        return Err(storage_error());
    }
    fs::remove_file(path).map_err(|_| storage_error())
}
fn object_text<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key].as_str().ok_or_else(Error::invalid)
}
fn known_service(value: &str) -> bool {
    SERVICES.iter().any(|s| s.0 == value)
}

struct State {
    operation: Value,
    connections: BTreeMap<String, Value>,
    active_chat: Option<String>,
    busy: bool,
    closing: bool,
    model_check: Option<String>,
}
struct App {
    root: PathBuf,
    legacy: Arc<dyn chat_io::Legacy>,
    state: Mutex<State>,
    gate: Mutex<()>,
    stopped: AtomicBool,
    shutdown: AtomicBool,
}
impl App {
    fn new(root: &Path, legacy: Arc<dyn chat_io::Legacy>) -> Result<Arc<Self>> {
        let mut operation = read(&root.join("operation.dpapi"), json!({"state":"idle"}))?;
        let interrupted = operation["state"] == "running";
        if interrupted {
            operation = json!({"state":"interrupted","detail":"上次操作中断，请先查看原记录。"});
        }
        let app = Arc::new(Self {
            root: root.to_path_buf(),
            legacy,
            state: Mutex::new(State {
                operation,
                connections: BTreeMap::new(),
                active_chat: None,
                busy: false,
                closing: false,
                model_check: None,
            }),
            gate: Mutex::new(()),
            stopped: AtomicBool::new(false),
            shutdown: AtomicBool::new(false),
        });
        let pending = app_call(root, "recovery_pending", json!({}))?["recovered"]
            .as_u64()
            .unwrap_or(0);
        if interrupted || pending > 0 {
            let incident = app.incident("interrupted", "recovery");
            if interrupted {
                let mut state = app.state.lock().map_err(|_| storage_error())?;
                if let Some(fields) = incident.as_object() {
                    for (k, v) in fields {
                        state.operation[k] = v.clone();
                    }
                }
                write(&root.join("operation.dpapi"), &state.operation)?;
            }
            if pending > 0 {
                app_call(root, "recover", json!({"incident":incident}))?;
            }
        }
        Ok(app)
    }
    fn profile(&self) -> PathBuf {
        self.root.join("profile")
    }
    fn database(&self) -> PathBuf {
        self.profile().join("desktop-chats.sqlite3")
    }
    fn identity_record(&self) -> Result<Value> {
        sessions::dispatch(
            "sessions_read",
            &json!({"profile":"identity","path":self.profile().join("ustc-identity.credentials.dpapi")}),
        )
    }
    fn identity(&self) -> Value {
        match self.identity_record() {
            Ok(v) => {
                json!({"saved":true,"account":v["username"],"session_only":v["session_only"]==true,"email_verification":v["email_verification_enabled"]==true})
            }
            Err(_) => json!({"saved":false,"account":null,"email_verification":false}),
        }
    }
    fn require_login(&self) -> Result<()> {
        if self.root.join("locked").exists() || self.identity()["saved"] != true {
            return Err(invalid("请先登录学校统一身份。"));
        }
        Ok(())
    }
    fn accepting(&self) -> Result<()> {
        if self.state.lock().map_err(|_| storage_error())?.closing {
            return Err(invalid("应用正在退出，请重新打开后操作。"));
        }
        Ok(())
    }
    fn idle(&self) -> Result<()> {
        self.accepting()?;
        if self.state.lock().map_err(|_| storage_error())?.busy {
            return Err(invalid("当前有操作进行中，请先等待完成或停止。"));
        }
        Ok(())
    }
    fn legacy(&self, action: &str, input: Value) -> Result<Value> {
        self.legacy
            .call(action, input, &self.stopped, &|stage, detail| {
                self.notify(stage, detail)
            })
    }
    fn digest(&self, action: &str, mut input: Value) -> Result<Value> {
        // Identity and storage scope come from the host, never from page/model input.
        let identity = self.identity_record()?;
        let username = identity["username"].as_str().ok_or_else(Error::invalid)?;
        input["action"] = json!(action);
        input["root"] = json!(self.profile());
        input["owner"] = json!(format!(
            "{:x}",
            Sha256::digest(json!(username).to_string().as_bytes())
        ));
        crate::digest::dispatch(&input)
    }
    fn notify(&self, stage: &str, detail: &str) {
        if let Ok(mut state) = self.state.lock() {
            state.operation["stage"] = json!(stage);
            state.operation["detail"] = json!(detail);
            if write(&self.root.join("operation.dpapi"), &state.operation).is_err() {
                self.stopped.store(true, Ordering::SeqCst);
            }
        }
    }
    fn incident(&self, code: &str, stage: &str) -> Value {
        crate::safety_log::record(
            &self.root.join("diagnostics"),
            &json!({"code":code,"stage":stage,"outcome":"unknown"}),
        )
    }
    fn start(
        self: &Arc<Self>,
        kind: &str,
        action: impl FnOnce(&App) -> Result<Value> + Send + 'static,
    ) -> Result<Value> {
        let id = uuid::Uuid::new_v4().simple().to_string();
        {
            let mut state = self.state.lock().map_err(|_| storage_error())?;
            if state.closing || state.busy {
                return Err(invalid("当前操作未结束，请先等待完成或停止。"));
            }
            self.stopped.store(false, Ordering::SeqCst);
            state.operation = json!({"id":id,"kind":kind,"state":"running","stage":"starting","detail":"正在处理…","started_at":now()});
            write(&self.root.join("operation.dpapi"), &state.operation)?;
            state.busy = true;
        }
        let app = Arc::clone(self);
        let spawn = thread::Builder::new()
            .name("school-operation".into())
            .spawn(move || {
                let result =
                    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| action(&app)))
                        .unwrap_or_else(|_| {
                            Err(Error::new(
                                "internal_error",
                                "本次操作中断；请先核实原记录。",
                            ))
                        });
                let result = match result {
                    Ok(v) => Ok(v),
                    Err(e) => Err((app.incident(e.code, "background"), e)),
                };
                if let Ok(mut state) = app.state.lock() {
                    match result {
                        Ok(value) => {
                            let status = match value["state"].as_str() {
                                Some("interrupted") => "interrupted",
                                Some("stopped") => "stopped",
                                Some("waiting_for_approval") => "waiting_for_approval",
                                _ => {
                                    if value["operation_failed"] == true
                                        || value["requires_user_action"] == true
                                        || value["error_code"].is_string()
                                        || matches!(
                                            value["state"].as_str(),
                                            Some(
                                                "creation_attempted"
                                                    | "manual_required"
                                                    | "captured"
                                            )
                                        )
                                    {
                                        "attention"
                                    } else {
                                        "completed"
                                    }
                                }
                            };
                            state.operation["state"] = json!(status);
                            state.operation["detail"] = value
                                .get("next_step")
                                .or_else(|| value.get("note"))
                                .cloned()
                                .unwrap_or(json!("本次处理已结束，请查看结果。"));
                            for key in ["error_id", "diagnostic_saved", "diagnostic_note"] {
                                if let Some(v) = value.get(key) {
                                    state.operation[key] = v.clone();
                                }
                            }
                            state.operation["result"] = value;
                        }
                        Err((incident, error)) => {
                            state.operation["state"] =
                                json!(if matches!(error.code, "cancelled" | "stop_requested") {
                                    "stopped"
                                } else {
                                    "failed"
                                });
                            state.operation["detail"] = json!(error.message);
                            state.operation["error_code"] = json!(error.code);
                            if let Some(fields) = incident.as_object() {
                                for (key, value) in fields {
                                    state.operation[key] = value.clone();
                                }
                            }
                        }
                    }
                    if write(&app.root.join("operation.dpapi"), &state.operation).is_err() {
                        state.operation["state"] = json!("failed");
                        state.operation["detail"] =
                            json!("本机进度保存失败；先核实原任务，不重复执行。");
                    }
                    state.model_check = None;
                    state.busy = false;
                }
            });
        if spawn.is_err() {
            let mut state = self.state.lock().map_err(|_| storage_error())?;
            state.busy = false;
            state.operation["state"] = json!("failed");
            write(&self.root.join("operation.dpapi"), &state.operation)?;
            return Err(storage_error());
        }
        Ok(json!({"operation_id":id,"started":true}))
    }
    fn stop(&self) -> Result<Value> {
        self.stopped.store(true, Ordering::SeqCst);
        let id = self
            .state
            .lock()
            .map_err(|_| storage_error())?
            .model_check
            .clone();
        if let Some(id) = id {
            app_call(&self.root, "test_cancel", json!({"id":id}))?;
        }
        Ok(json!({"stop_requested":true,"note":"停止后续步骤，已发出请求仍需核实。"}))
    }
    fn status(&self) -> Result<Value> {
        let mail = self.legacy("metadata", json!({}))?["mail"].clone();
        let model = app_call(&self.root, "public_settings", json!({}))?;
        let mut identity = self.identity();
        let locked = self.root.join("locked").exists();
        if locked {
            identity["account"] = Value::Null;
        }
        let state = self.state.lock().map_err(|_| storage_error())?;
        let services:Vec<Value>=SERVICES.iter().map(|(service,label,file)| {
            let mut v=json!({"service":service,"label":label,"session_saved":!locked && (if *service=="mail" {mail["configured"]==true} else {!file.is_empty()&&self.profile().join(file).is_file()})});
            let c=state.connections.get(*service).cloned().unwrap_or(json!({"state":"unchecked"}));
            if let Some(fields)=c.as_object(){for(k,val)in fields{v[k]=val.clone();}}v
        }).collect();
        let chrome = [
            PathBuf::from(std::env::var_os("PROGRAMFILES").unwrap_or("C:/Program Files".into())),
            PathBuf::from(std::env::var_os("LOCALAPPDATA").unwrap_or_default()),
        ]
        .iter()
        .any(|p| p.join("Google/Chrome/Application/chrome.exe").is_file());
        Ok(
            json!({"identity":identity,"locked":locked,"mail":mail,"model":model,"model_providers":[
            {"id":"deepseek","label":"DeepSeek","base_url":"https://api.deepseek.com","model":"deepseek-flash"},
            {"id":"ustc","label":"科大大模型公共服务","base_url":"https://api.llm.ustc.edu.cn/v1","model":"deepseek-v4-flash"},
            {"id":"custom","label":"自定义兼容 API","base_url":"","model":""}],
            "chrome_installed":chrome,"busy":state.busy,"operation":if locked {json!({"state":"idle"})}else{state.operation.clone()},
            "services":services,"data_location":self.root,"version":"0.1.0","active_chat":if locked{None}else{state.active_chat.clone()}}),
        )
    }
    fn connection_result(&self, service: &str, value: &Value) -> Result<()> {
        let code = value["error_code"].as_str().unwrap_or("");
        let state = if value["connected"] == true {
            "connected"
        } else if matches!(code, "authentication_required" | "identity_required") {
            "login_required"
        } else if code == "manual_verification_required" {
            "verification_required"
        } else {
            "unavailable"
        };
        let mut connection = json!({"state":state,"checked_at":now()});
        for key in ["error_code", "next_step", "recovery"] {
            if let Some(v) = value.get(key) {
                connection[key] = v.clone();
            }
        }
        self.state
            .lock()
            .map_err(|_| storage_error())?
            .connections
            .insert(service.into(), connection);
        Ok(())
    }
    fn ensure_service(&self, service: &str, restore: bool) -> Result<Value> {
        self.require_login()?;
        if !known_service(service) {
            return Err(invalid("未知学校平台。"));
        }
        let mut value = self.legacy(
            "service_check",
            json!({"service":service,"force":false,"restore":false}),
        )?;
        if restore
            && value["connected"] != true
            && service != "mail"
            && service != "teach"
            && matches!(
                value["error_code"].as_str(),
                Some(
                    "authentication_required"
                        | "identity_required"
                        | "manual_verification_required"
                )
            )
        {
            value = runtime()?.block_on(login::run(
                &json!({"private_dir":self.profile(),"service":service,"headed":false}),
                &self.stopped,
                &|s, d| self.notify(s, d),
            ))?;
            if value["connected"] == true {
                value = self.legacy(
                    "service_check",
                    json!({"service":service,"force":false,"restore":false}),
                )?;
            }
        }
        self.connection_result(service, &value)?;
        Ok(value)
    }
    fn do_login(&self, service: &str) -> Result<Value> {
        if service != "identity" {
            self.require_login()?;
        }
        if matches!(service, "mail" | "mail_resume") {
            if service == "mail"
                && self.legacy("metadata", json!({}))?["mail"]["configured"] == true
            {
                let mut result = self.ensure_service("mail", false)?;
                if result["connected"] == true {
                    result["session_reused"] = json!(true);
                } else {
                    result["requires_user_action"] = json!(true);
                }
                return Ok(result);
            }
            return self.legacy("mail_bind", json!({"resume":service=="mail_resume"}));
        }
        if !matches!(service, "identity" | "icourse") {
            let value = self.ensure_service(service, false)?;
            if value["connected"] == true {
                return Ok(value);
            }
            if !matches!(
                value["error_code"].as_str(),
                Some(
                    "authentication_required"
                        | "identity_required"
                        | "manual_verification_required"
                )
            ) {
                let mut v = value;
                v["requires_user_action"] = json!(true);
                return Ok(v);
            }
        }
        let result = runtime()?.block_on(login::run(
            &json!({"private_dir":self.profile(),"service":service,"headed":true}),
            &self.stopped,
            &|s, d| self.notify(s, d),
        ))?;
        if result["connected"] == true {
            if service == "identity" {
                remove_owned(&self.root.join("locked"))?;
            }
            self.connection_result(if service == "identity" { "bb" } else { service }, &result)?;
        }
        Ok(result)
    }
    fn model_test(&self) -> Result<Value> {
        let begin = app_call(&self.root, "test_begin", json!({}))?;
        let id = object_text(&begin, "id")?.to_owned();
        self.state.lock().map_err(|_| storage_error())?.model_check = Some(id.clone());
        if self.stopped.load(Ordering::SeqCst) {
            app_call(&self.root, "test_cancel", json!({"id":id}))?;
        }
        self.notify("testing", "正在测试模型工具调用能力…");
        app_call(&self.root, "test_connection", json!({"id":id}))
    }
    fn model_list(&self, value: Value) -> Result<Value> {
        let input = json!({"root":self.root,"action":"list_models","value":value});
        runtime()?.block_on(async {
            let client = model::Client::new()?;
            let mut request = std::pin::pin!(app_io::dispatch(&input, &client));
            loop {
                if self.stopped.load(Ordering::SeqCst) {
                    return Ok(json!({"state":"stopped","note":"已停止获取模型列表。"}));
                }
                if let Ok(result) =
                    tokio::time::timeout(Duration::from_millis(50), request.as_mut()).await
                {
                    return result;
                }
            }
        })
    }
    fn new_chat(&self) -> Result<Value> {
        let chat = json!({"id":uuid::Uuid::new_v4().simple().to_string(),"title":"新的对话","created_at":now(),"updated_at":now(),"messages":[],"events":[],"tasks":[],"allowed_files":[],"pending":null,"state":"idle"});
        storage::chat_save(&json!({"database":self.database(),"chat":chat}))?;
        Ok(chat)
    }
    fn chat(&self, id: &str) -> Result<Value> {
        storage::chat_read(&json!({"database":self.database(),"id":id}))
    }
    fn send(self: &Arc<Self>, value: &Value) -> Result<Value> {
        self.require_login()?;
        self.idle()?;
        let text = object_text(value, "text")?;
        if text.trim().is_empty() || text.chars().count() > 12000 {
            return Err(invalid("请输入 1–12000 字的需求。"));
        }
        if value
            .get("execute_authorized")
            .is_some_and(|v| !v.is_boolean())
        {
            return Err(invalid("办理许可格式无效。"));
        }
        let files = app_call(
            &self.root,
            "validate_files",
            json!({"files":value.get("files").cloned().unwrap_or(json!([]))}),
        )?;
        if app_call(&self.root, "public_settings", json!({}))?["tested"] != true {
            return Err(invalid("请先在模型设置中保存并测试 API 连接。"));
        }
        let chat = match value
            .get("chat_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            Some(id) => self.chat(id)?,
            None => self.new_chat()?,
        };
        if !chat["pending"].is_null() {
            return Err(invalid("请先确认或取消当前待执行操作。"));
        }
        let id = object_text(&chat, "id")?.to_owned();
        let input = json!({"root":self.root,"chat_id":id,"text":text,"files":files,"execute_authorized":value["execute_authorized"]==true});
        self.state.lock().map_err(|_| storage_error())?.active_chat = Some(id.clone());
        let mut result = self.start("chat", move |app| {
            runtime()?.block_on(chat_io::run(
                &input,
                &app.stopped,
                &|s, d| app.notify(s, d),
                app.legacy.as_ref(),
            ))
        })?;
        result["chat_id"] = json!(id);
        Ok(result)
    }
    fn approval(self: &Arc<Self>, value: &Value) -> Result<Value> {
        self.require_login()?;
        self.idle()?;
        let id = object_text(value, "chat_id")?;
        let approval = object_text(value, "approval_id")?;
        let approved = value["approve"]
            .as_bool()
            .ok_or_else(|| invalid("确认值无效。"))?;
        let mut chat = self.chat(id)?;
        if chat["pending"].is_null() || chat["pending"]["id"] != approval {
            return Err(invalid("操作已变化，请刷新。"));
        }
        if !approved {
            let call_id = chat["pending"]["call_id"].clone();
            chat["turn_authorized"] = json!(false);
            chat["messages"].as_array_mut().ok_or_else(storage_error)?.push(json!({"role":"tool","tool_call_id":call_id,"content":"{\"state\":\"cancelled\",\"note\":\"用户取消了这项操作，不要自动重建或提交。\"}"}));
            chat["pending"] = Value::Null;
            chat["state"] = json!("idle");
            chat = crate::runner::close(&json!({"chat":chat,"state":"not_executed"}))?;
            storage::chat_save(&json!({"database":self.database(),"chat":chat}))?;
            return Ok(json!({"cancelled":true}));
        }
        let input = json!({"root":self.root,"chat_id":id,"approval":approval});
        self.start("chat", move |app| {
            runtime()?.block_on(chat_io::run(
                &input,
                &app.stopped,
                &|s, d| app.notify(s, d),
                app.legacy.as_ref(),
            ))
        })
    }
    fn upload(&self, name: &str, bytes: &[u8]) -> Result<Value> {
        self.accepting()?;
        self.require_login()?;
        let started = app_call(
            &self.root,
            "upload_begin",
            json!({"name":name,"size":bytes.len(),"sha256":format!("{:x}",Sha256::digest(bytes))}),
        )?;
        let id = object_text(&started, "id")?;
        let result = (|| {
            for (offset, chunk) in bytes.chunks(1024 * 1024).enumerate() {
                app_call(
                    &self.root,
                    "upload_chunk",
                    json!({"id":id,"offset":offset*1024*1024,"content":STANDARD.encode(chunk)}),
                )?;
            }
            app_call(&self.root, "upload_finish", json!({"id":id}))
        })();
        if result.is_err() {
            let _ = app_call(&self.root, "upload_abort", json!({"id":id}));
        }
        result
    }
    fn email_verification(&self, enabled: bool) -> Result<Value> {
        let path = self.profile().join("ustc-identity.credentials.dpapi");
        let mut identity = self.identity_record()?;
        if enabled {
            if self.legacy("metadata", json!({}))?["mail"]["configured"] != true {
                return Err(invalid("启用邮件验证前，请先在本机接入并验证邮箱。"));
            }
            let config_path = self.profile().join("mail.json");
            if !plain(&config_path, false)
                || fs::metadata(&config_path)
                    .map_err(|_| storage_error())?
                    .len()
                    > 65536
            {
                return Err(storage_error());
            }
            let mail = strict_json::parse(&fs::read(config_path).map_err(|_| storage_error())?)
                .map_err(|_| storage_error())?;
            let address = object_text(&mail, "address")?;
            if !address.contains('@') || address.len() > 254 {
                return Err(invalid("邮箱地址无效。"));
            }
            identity["email_verification_address"] = json!(address);
        } else if let Some(map) = identity.as_object_mut() {
            map.remove("email_verification_address");
        }
        identity["email_verification_enabled"] = json!(enabled);
        sessions::dispatch(
            "sessions_write",
            &json!({"profile":"identity","path":path,"value":identity}),
        )?;
        Ok(json!({"enabled":enabled}))
    }
    fn background_active(&self) -> Result<bool> {
        let status = self.legacy("background_status", json!({}))?;
        for key in [
            "digest_enabled",
            "digest_running",
            "login_workers_running",
            "adapter_uncertain",
            "adapter_running",
        ] {
            if status[key].as_bool() != Some(false) {
                return Ok(true);
            }
        }
        let database = self.profile().join("script-jobs.sqlite3");
        if database.exists() {
            if !plain(&database, false) {
                return Err(storage_error());
            }
            let db = rusqlite::Connection::open_with_flags(
                database,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .map_err(|_| storage_error())?;
            let mut statement = db
                .prepare("SELECT payload FROM jobs")
                .map_err(|_| storage_error())?;
            let rows = statement
                .query_map([], |r| r.get::<_, Vec<u8>>(0))
                .map_err(|_| storage_error())?;
            for row in rows {
                let value =
                    strict_json::parse(&storage::crypt(&row.map_err(|_| storage_error())?, true)?)
                        .map_err(|_| storage_error())?;
                if !matches!(
                    value["state"].as_str(),
                    Some("completed" | "review" | "failed" | "cancelled" | "expired")
                ) || value["effect_started"] == true
                {
                    return Ok(true);
                }
            }
        }
        let schedules = self.profile().join("young-schedules");
        if schedules.exists() {
            if !plain(&schedules, true) {
                return Err(storage_error());
            }
            for entry in fs::read_dir(schedules).map_err(|_| storage_error())? {
                let path = entry.map_err(|_| storage_error())?.path();
                if path.extension().is_none_or(|e| e != "json") {
                    continue;
                }
                if !plain(&path, false)
                    || fs::metadata(&path).map_err(|_| storage_error())?.len() > 1024 * 1024
                {
                    return Err(storage_error());
                }
                let value = strict_json::parse(&fs::read(path).map_err(|_| storage_error())?)
                    .map_err(|_| storage_error())?;
                if value["cancelled"] != true
                    && !matches!(
                        value["worker_state"].as_str(),
                        Some(
                            "completed"
                                | "cancelled"
                                | "failed"
                                | "expired"
                                | "verified"
                                | "stopped"
                        )
                    )
                {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }
    fn clear(&self, value: &Value) -> Result<Value> {
        self.idle()?;
        let scope = object_text(value, "scope")?;
        if !matches!(scope, "logout" | "mail" | "account" | "all") || value["confirmation"] != scope
        {
            return Err(invalid("请明确确认需要清理的范围。"));
        }
        if self.background_active()? {
            return Err(invalid(
                "还有运行中、待核实任务或已启用推送，请先暂停并核实再管理账号。",
            ));
        }
        if scope == "logout" {
            fs::write(self.root.join("locked"), b"locked").map_err(|_| storage_error())?;
            return Ok(json!({"locked":true,"note":"已锁定应用。重新登录后才能访问原账号记录。"}));
        }
        if scope == "mail" {
            self.require_login()?;
            let journal = self.profile().join("mail-browser-setup.dpapi");
            let binding = read(&journal, json!({}))?;
            if binding["state"] == "captured" {
                write(
                    &journal,
                    &json!({"state":"creation_attempted","label":binding["label"],"updated_at":now()}),
                )?;
            }
            for name in ["mail.json", "mail.credentials.dpapi"] {
                remove_owned(&self.profile().join(name))?;
            }
            self.email_verification(false)?;
            return Ok(
                json!({"unbound":true,"note":"已移除本机邮箱凭据并关闭验证码回退；网站客户端密码需在邮箱安全设置中撤销。"}),
            );
        }
        let profile = self.profile();
        validate_tree(&profile, &profile)?;
        fs::remove_dir_all(&profile).map_err(|_| storage_error())?;
        fs::create_dir(&profile).map_err(|_| storage_error())?;
        remove_owned(&self.root.join("locked"))?;
        if scope == "all" {
            for name in [
                "model-settings.dpapi",
                "school-model-candidate.dpapi",
                "model-check.dpapi",
                "school-model-candidate-check.dpapi",
            ] {
                remove_owned(&self.root.join(name))?;
            }
            crate::safety_log::clear(&self.root.join("diagnostics"))?;
        }
        let mut state = self.state.lock().map_err(|_| storage_error())?;
        state.connections.clear();
        state.active_chat = None;
        state.operation = json!({"state":"idle"});
        write(&self.root.join("operation.dpapi"), &state.operation)?;
        Ok(json!({"cleared":scope,"model_settings_preserved":scope=="account"}))
    }
    fn api(self: &Arc<Self>, path: &str, value: Value) -> Result<Value> {
        self.accepting()?;
        match path {
            "/api/stop" => self.stop(),
            "/api/shutdown" => {
                self.idle()?;
                self.state.lock().map_err(|_| storage_error())?.closing = true;
                self.shutdown.store(true, Ordering::SeqCst);
                Ok(json!({"closed":true}))
            }
            "/api/login" => {
                let service = object_text(&value, "service")?.to_owned();
                if !matches!(service.as_str(), "identity" | "mail_resume")
                    && (!known_service(&service) || service == "teach")
                {
                    return Err(invalid("不支持的登录入口。"));
                }
                if service != "identity" {
                    self.require_login()?;
                }
                self.start(&format!("login_{service}"), move |app| {
                    app.do_login(&service)
                })
            }
            "/api/model/save" => {
                self.idle()?;
                app_call(&self.root, "save_settings", json!({"value":value}))
            }
            "/api/model/test" => self.start("model_test", |app| app.model_test()),
            "/api/model/connect" => {
                self.idle()?;
                app_call(&self.root, "save_settings", json!({"value":value}))?;
                self.start("model_test", |app| app.model_test())
            }
            "/api/model/models" => self.start("model_list", move |app| app.model_list(value)),
            "/api/connect" => {
                self.require_login()?;
                let service = object_text(&value, "service")?.to_owned();
                if !known_service(&service) {
                    return Err(invalid("未知学校平台。"));
                }
                let restore = value["reconnect"] == true;
                self.start(&format!("connect_{service}"), move |app| {
                    app.ensure_service(&service, restore)
                })
            }
            "/api/email-verification" => {
                self.require_login()?;
                self.idle()?;
                self.email_verification(value["enabled"].as_bool().ok_or_else(Error::invalid)?)
            }
            "/api/chat/send" => self.send(&value),
            "/api/chat/approve" => self.approval(&value),
            "/api/clear" => self.clear(&value),
            "/api/diagnostics/clear" => {
                if value["confirmation"] != "diagnostics" {
                    return Err(invalid("请确认仅清理诊断日志。"));
                }
                crate::safety_log::clear(&self.root.join("diagnostics"))
            }
            "/api/diagnostics/ui-error" => Ok(self.incident("ui_error", "ui")),
            _ => {
                self.require_login()?;
                let (action, background) = match path {
                    "/api/campus/profile" => ("campus_profile_save", false),
                    "/api/campus/timetable/refresh" => ("campus_timetable_refresh", true),
                    "/api/digest/configure" => ("digest_configure", false),
                    "/api/digest/read" => ("digest_read", false),
                    "/api/digest/plan" => ("digest_plan", true),
                    "/api/digest/preview-report" => ("digest_preview_report", true),
                    "/api/digest/refresh" => ("digest_refresh", true),
                    "/api/digest/courses" => ("digest_courses", true),
                    "/api/digest/calendar" => ("digest_calendar", true),
                    "/api/digest/summarize" => ("digest_summarize", true),
                    "/api/model/school-keys" => ("school_model_keys", true),
                    "/api/model/school-connect" => ("school_model_import", true),
                    _ => return Err(invalid("未知操作。")),
                };
                if action == "digest_preview_report" {
                    self.start(action, move |app| app.digest("preview_report", value))
                } else if action == "digest_read" {
                    self.digest("mark_read", value)
                } else if background {
                    self.start(action, move |app| app.legacy(action, value))
                } else {
                    if action != "digest_read" {
                        self.idle()?;
                    }
                    self.legacy(action, value)
                }
            }
        }
    }
    fn get(&self, path: &str, query: &BTreeMap<String, String>) -> Result<Response> {
        let value = match path {
            "/api/status" => self.status()?,
            "/api/diagnostics" => crate::safety_log::report(&self.root.join("diagnostics")),
            "/api/diagnostics/export" => crate::safety_log::export(&self.root.join("diagnostics"))?,
            "/api/campus/sites" => self.legacy("campus_sites", json!({}))?,
            _ => {
                self.require_login()?;
                match path {
                    "/api/chats" => {
                        json!({"chats":storage::chat_list(&json!({"database":self.database()}))?})
                    }
                    "/api/chat" => chat_io::ui_view(
                        &self.chat(query.get("id").map(String::as_str).unwrap_or(""))?,
                    )?,
                    "/api/tasks" => {
                        let offset = query
                            .get("offset")
                            .map(String::as_str)
                            .unwrap_or("0")
                            .parse::<u32>()
                            .map_err(|_| Error::invalid())?;
                        if offset > 10000 {
                            return Err(Error::invalid());
                        }
                        self.legacy("tasks_list", json!({"offset":offset,"limit":20}))?
                    }
                    "/api/campus/profile" => self.legacy("campus_profile", json!({}))?,
                    "/api/campus/timetable" => self.legacy("campus_timetable", json!({}))?,
                    "/api/digest" => {
                        let mut digest = self.digest("overview", json!({}))?;
                        digest["worker_running"] =
                            self.legacy("background_status", json!({}))?["digest_running"].clone();
                        digest
                    }
                    "/api/artifact" => return self.artifact(query),
                    _ => return Ok(Response::json(404, json!({"error":"入口不存在。"}))),
                }
            }
        };
        Ok(Response::json(200, value))
    }
    fn artifact(&self, query: &BTreeMap<String, String>) -> Result<Response> {
        let chat = self.chat(query.get("chat").map(String::as_str).unwrap_or(""))?;
        let id = query.get("id").ok_or_else(Error::invalid)?;
        let record = chat["artifacts"]
            .as_array()
            .and_then(|a| a.iter().find(|a| a["id"] == *id))
            .ok_or_else(|| invalid("该文件不属于当前对话。"))?;
        let directory = object_text(record, "directory")?;
        if !matches!(
            directory,
            "calendar-exports"
                | "attachments"
                | "mail-exports"
                | "web-downloads"
                | "vlab-downloads"
                | "drive-downloads"
                | "generated"
        ) {
            return Err(Error::invalid());
        }
        let base = self.profile().join(directory);
        let path = PathBuf::from(object_text(record, "path")?);
        if !path.is_absolute()
            || !plain(&base, true)
            || !plain(&path, false)
            || !fs::canonicalize(&path)
                .map_err(|_| storage_error())?
                .starts_with(fs::canonicalize(&base).map_err(|_| storage_error())?)
        {
            return Err(invalid("文件已失效，请重新获取。"));
        }
        let canonical_base = fs::canonicalize(&base).map_err(|_| storage_error())?;
        for ancestor in path.ancestors().skip(1) {
            if !plain(ancestor, true) {
                return Err(invalid("文件目录已变化，请重新获取。"));
            }
            if fs::canonicalize(ancestor).is_ok_and(|p| p == canonical_base) {
                break;
            }
        }
        let mut bytes = Vec::new();
        fs::File::open(path)
            .map_err(|_| storage_error())?
            .take(60 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| storage_error())?;
        if bytes.len() > 60 * 1024 * 1024
            || record["sha256"] != format!("{:x}", Sha256::digest(&bytes))
        {
            return Err(invalid("文件内容已变化，请重新获取。"));
        }
        Ok(Response {
            status: 200,
            content_type: "application/octet-stream",
            body: bytes,
        })
    }
}

fn validate_tree(path: &Path, root: &Path) -> Result<()> {
    if !path.starts_with(root) || !plain(path, true) {
        return Err(storage_error());
    }
    for entry in fs::read_dir(path).map_err(|_| storage_error())? {
        let child = entry.map_err(|_| storage_error())?.path();
        if plain(&child, true) {
            validate_tree(&child, root)?;
        } else if !plain(&child, false) {
            return Err(storage_error());
        }
    }
    Ok(())
}

struct Request {
    method: String,
    path: String,
    query: BTreeMap<String, String>,
    headers: BTreeMap<String, String>,
    length: usize,
}
struct Response {
    status: u16,
    content_type: &'static str,
    body: Vec<u8>,
}
impl Response {
    fn json(status: u16, value: Value) -> Self {
        Self {
            status,
            content_type: "application/json; charset=utf-8",
            body: serde_json::to_vec(&value).unwrap_or_else(|_| b"{}".to_vec()),
        }
    }
    fn error(status: u16, message: &str) -> Self {
        Self::json(status, json!({"error":message}))
    }
    fn send(self, stream: &mut TcpStream) {
        let reason = match self.status {
            200 => "OK",
            400 => "Bad Request",
            403 => "Forbidden",
            404 => "Not Found",
            405 => "Method Not Allowed",
            413 => "Payload Too Large",
            415 => "Unsupported Media Type",
            429 => "Too Many Requests",
            _ => "Internal Server Error",
        };
        let header=format!("HTTP/1.1 {} {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'\r\n\r\n",self.status,reason,self.content_type,self.body.len());
        let _ = stream
            .write_all(header.as_bytes())
            .and_then(|_| stream.write_all(&self.body))
            .and_then(|_| stream.flush());
    }
}
fn line(
    reader: &mut BufReader<&mut TcpStream>,
    budget: &mut usize,
) -> std::result::Result<String, Response> {
    let mut bytes = Vec::new();
    reader
        .take((*budget + 1) as u64)
        .read_until(b'\n', &mut bytes)
        .map_err(|_| Response::error(400, "请求未完整传输。"))?;
    if bytes.len() > *budget || !bytes.ends_with(b"\r\n") {
        return Err(Response::error(400, "请求头格式无效。"));
    }
    *budget -= bytes.len();
    String::from_utf8(bytes[..bytes.len() - 2].to_vec())
        .map_err(|_| Response::error(400, "请求头格式无效。"))
}
fn parse(reader: &mut BufReader<&mut TcpStream>) -> std::result::Result<Request, Response> {
    let mut budget = 16 * 1024;
    let first = line(reader, &mut budget)?;
    let words: Vec<_> = first.split(' ').collect();
    if words.len() != 3 || !matches!(words[2], "HTTP/1.1" | "HTTP/1.0") {
        return Err(Response::error(400, "请求格式无效。"));
    }
    let method = words[0].to_owned();
    let target = words[1];
    if !matches!(method.as_str(), "GET" | "POST") {
        return Err(Response::error(405, "不支持此请求方法。"));
    }
    if target.len() > 2048
        || !target.starts_with('/')
        || target.starts_with("//")
        || target.contains(['\\', '#', '\r', '\n'])
    {
        return Err(Response::error(400, "请求地址无效。"));
    }
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    if method == "POST" && target.contains('?') {
        return Err(Response::error(400, "请求参数无效。"));
    }
    let keys: &[&str] = match path {
        "/api/artifact" => &["chat", "id"],
        "/api/chat" => &["id"],
        "/api/tasks" => &["offset"],
        _ => &[],
    };
    let mut params = BTreeMap::new();
    for (k, v) in url::form_urlencoded::parse(query.as_bytes()) {
        if !keys.contains(&k.as_ref()) || params.insert(k.into_owned(), v.into_owned()).is_some() {
            return Err(Response::error(400, "请求参数无效。"));
        }
    }
    let mut headers = BTreeMap::new();
    loop {
        let header = line(reader, &mut budget)?;
        if header.is_empty() {
            break;
        }
        let Some((key, value)) = header.split_once(':') else {
            return Err(Response::error(400, "请求头格式无效。"));
        };
        if key.is_empty()
            || !key
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&c))
            || value.bytes().any(|c| c < 32 && c != b'\t' || c == 127)
        {
            return Err(Response::error(400, "请求头格式无效。"));
        }
        if headers
            .insert(key.to_ascii_lowercase(), value.trim().to_owned())
            .is_some()
        {
            return Err(Response::error(400, "请求头不能重复。"));
        }
    }
    if headers.contains_key("transfer-encoding") || headers.contains_key("expect") {
        return Err(Response::error(400, "不支持此传输格式。"));
    }
    let length = match headers.get("content-length") {
        Some(v) if !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()) => v
            .parse::<usize>()
            .map_err(|_| Response::error(413, "请求大小超过限制。"))?,
        None if method == "GET" => 0,
        _ => return Err(Response::error(400, "请求长度无效。")),
    };
    if method == "GET" && length != 0 {
        return Err(Response::error(400, "读取请求不能携带正文。"));
    }
    Ok(Request {
        method,
        path: path.into(),
        query: params,
        headers,
        length,
    })
}
fn same_secret(a: &str, b: &str) -> bool {
    let mut difference = a.len() ^ b.len();
    for (i, x) in b.as_bytes().iter().enumerate() {
        difference |= usize::from(a.as_bytes().get(i).copied().unwrap_or(0) ^ x);
    }
    difference == 0
}
fn authorized(request: &Request, origin: &str, token: &str) -> bool {
    if request.headers.get("host").map(String::as_str) != origin.strip_prefix("http://")
        || !same_secret(
            request
                .headers
                .get("x-school-token")
                .map(String::as_str)
                .unwrap_or(""),
            token,
        )
    {
        return false;
    }
    request.method != "POST"
        || (request.headers.get("origin").map(String::as_str) == Some(origin)
            && request
                .headers
                .get("sec-fetch-site")
                .is_none_or(|s| s == "same-origin"))
}
fn public(path: &str) -> Option<Response> {
    let (body, content_type): (&[u8], &str) = match path {
        "/" => (
            include_bytes!("../../../src/school_mcp/desktop/web/index.html"),
            "text/html; charset=utf-8",
        ),
        "/transport.js" => (
            include_bytes!("../../../src/school_mcp/desktop/web/transport.js"),
            "text/javascript; charset=utf-8",
        ),
        "/app.js" => (
            include_bytes!("../../../src/school_mcp/desktop/web/app.js"),
            "text/javascript; charset=utf-8",
        ),
        "/style.css" => (
            include_bytes!("../../../src/school_mcp/desktop/web/style.css"),
            "text/css; charset=utf-8",
        ),
        _ => return None,
    };
    Some(Response {
        status: 200,
        content_type,
        body: body.to_vec(),
    })
}
fn filename(value: &str) -> Result<String> {
    let raw = value.as_bytes();
    let mut decoded = Vec::new();
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'%' {
            if i + 2 >= raw.len() {
                return Err(Error::invalid());
            }
            let byte = u8::from_str_radix(
                std::str::from_utf8(&raw[i + 1..i + 3]).map_err(|_| Error::invalid())?,
                16,
            )
            .map_err(|_| Error::invalid())?;
            decoded.push(byte);
            i += 3;
        } else {
            decoded.push(raw[i]);
            i += 1;
        }
    }
    String::from_utf8(decoded).map_err(|_| Error::invalid())
}
fn discard(reader: &mut BufReader<&mut TcpStream>, length: usize, maximum: usize) {
    if length == 0 || length > maximum {
        return;
    }
    let _ = reader
        .get_mut()
        .set_read_timeout(Some(Duration::from_secs(1)));
    let _ = std::io::copy(&mut reader.take(length as u64), &mut std::io::sink());
}
fn handle(mut stream: TcpStream, app: &Arc<App>, origin: &str, token: &str) {
    // Windows accepted sockets inherit the listener's nonblocking mode. Each
    // bounded request thread must wait for headers instead of rejecting a race.
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(30)));
    let mut reader = BufReader::new(&mut stream);
    let response = (|| -> std::result::Result<Response, Response> {
        let request = parse(&mut reader)?;
        if request.headers.get("host").map(String::as_str) != origin.strip_prefix("http://") {
            return Err(Response::error(403, "访问来源不正确。"));
        }
        if request.method == "GET" {
            if let Some(response) = public(&request.path) {
                return Ok(response);
            }
        }
        if !authorized(&request, origin, token) {
            discard(&mut reader, request.length, MAX_JSON);
            return Err(Response::error(403, "请求来源或访问凭证不正确。"));
        }
        let result = if request.method == "GET" {
            // Account-sensitive reads cannot race an authorized account clear.
            if request.path == "/api/status" {
                app.get(&request.path, &request.query)
            } else {
                let _guard = app
                    .gate
                    .lock()
                    .map_err(|_| Response::error(500, "本机状态暂不可用。"))?;
                app.get(&request.path, &request.query)
            }
        } else {
            let upload = request.path == "/api/upload";
            if request.length > if upload { MAX_UPLOAD } else { MAX_JSON } {
                discard(&mut reader, request.length, 1_000_000);
                return Err(Response::error(413, "请求大小超过限制。"));
            }
            if !upload
                && request
                    .headers
                    .get("content-type")
                    .and_then(|s| s.split(';').next())
                    != Some("application/json")
            {
                discard(&mut reader, request.length, MAX_JSON);
                return Err(Response::error(415, "请求格式不正确。"));
            }
            let mut bytes = vec![0_u8; request.length];
            reader
                .read_exact(&mut bytes)
                .map_err(|_| Response::error(400, "请求未完整传输。"))?;
            let _guard = if request.path == "/api/stop" {
                None
            } else {
                Some(
                    app.gate
                        .lock()
                        .map_err(|_| Response::error(500, "本机状态暂不可用。"))?,
                )
            };
            if upload {
                let name = filename(
                    request
                        .headers
                        .get("x-file-name")
                        .map(String::as_str)
                        .unwrap_or(""),
                )
                .map_err(|_| Response::error(400, "附件名称无效。"))?;
                app.upload(&name, &bytes).map(|v| Response::json(200, v))
            } else {
                let value = strict_json::parse(&bytes)
                    .map_err(|_| Response::error(400, "请求须为有效对象。"))?;
                if !value.is_object() {
                    return Err(Response::error(400, "请求须为对象。"));
                }
                app.api(&request.path, value)
                    .map(|v| Response::json(200, v))
            }
        };
        match result {
            Ok(r) => Ok(r),
            Err(e) => {
                let mut v = json!({"error":e.message,"error_code":e.code});
                if let Some(fields) = app.incident(e.code, "api").as_object() {
                    for (k, value) in fields {
                        v[k] = value.clone();
                    }
                }
                Ok(Response::json(
                    if e.code == "invalid_arguments" {
                        400
                    } else {
                        500
                    },
                    v,
                ))
            }
        }
    })()
    .unwrap_or_else(|e| e);
    drop(reader);
    if response.body.len() > MAX_RESPONSE {
        Response::error(500, "本机结果过大。").send(&mut stream);
    } else {
        response.send(&mut stream);
    }
}

fn handshake(url: &str, owned: bool, core: Value) -> Result<Value> {
    let url = url::Url::parse(url).map_err(|_| Error::invalid())?;
    let token = url.fragment().ok_or_else(Error::invalid)?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none_or(|p| p == 0)
        || !url.username().is_empty()
        || url.password().is_some()
        || !matches!(url.path(), "" | "/")
        || url.query().is_some()
        || !(32..=128).contains(&token.len())
        || !token
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
    {
        return Err(Error::invalid());
    }
    Ok(
        json!({"protocol":1,"origin":format!("http://127.0.0.1:{}",url.port().unwrap_or(0)),"token":token,"owned":owned,"core":core}),
    )
}
fn output(value: &Value) -> Result<()> {
    let mut out = std::io::stdout().lock();
    serde_json::to_writer(&mut out, value).map_err(|_| storage_error())?;
    out.write_all(b"\n")
        .and_then(|_| out.flush())
        .map_err(|_| storage_error())
}

struct InstanceLock {
    file: fs::File,
    #[cfg(windows)]
    overlapped: Box<windows_sys::Win32::System::IO::OVERLAPPED>,
}
impl InstanceLock {
    fn acquire(path: &Path) -> Result<Option<Self>> {
        if path.exists() && !plain(path, false) {
            return Err(storage_error());
        }
        // The legacy Python entry uses a+b, appends one byte, then locks byte 0.
        // Lock the same byte, not the whole file: its append must remain possible
        // so that an older launch can discover and reuse this owned server.
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .open(path)
            .map_err(|_| storage_error())?;
        if file.metadata().map_err(|_| storage_error())?.len() == 0 {
            file.write_all(b"0")
                .and_then(|_| file.flush())
                .map_err(|_| storage_error())?;
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::{
                Foundation::ERROR_LOCK_VIOLATION,
                Storage::FileSystem::{
                    LockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
                },
            };
            let mut overlapped: Box<windows_sys::Win32::System::IO::OVERLAPPED> =
                Box::new(unsafe { std::mem::zeroed() });
            let ok = unsafe {
                LockFileEx(
                    file.as_raw_handle(),
                    LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                    0,
                    1,
                    0,
                    overlapped.as_mut(),
                )
            };
            if ok == 0 {
                if std::io::Error::last_os_error().raw_os_error()
                    == Some(ERROR_LOCK_VIOLATION as i32)
                {
                    return Ok(None);
                }
                return Err(storage_error());
            }
            Ok(Some(Self { file, overlapped }))
        }
        #[cfg(not(windows))]
        {
            if file.try_lock().is_err() {
                return Ok(None);
            }
            Ok(Some(Self { file }))
        }
    }
}
impl Drop for InstanceLock {
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
                    self.overlapped.as_mut(),
                );
            }
        }
        #[cfg(not(windows))]
        {
            let _ = self.file.unlock();
        }
    }
}

/// Blocking entry point. Call before entering a Tokio runtime. The lock and the
/// parent pipe belong to this launch; never terminate an older owned instance.
pub fn run(root: &Path, native_stdio: bool, legacy: Arc<dyn chat_io::Legacy>) -> Result<()> {
    if !native_stdio {
        return Err(invalid("原生服务需要桌面宿主的私有管道。"));
    }
    let initialized = app_call(root, "initialize", json!({}))?;
    let root = PathBuf::from(object_text(&initialized, "root")?);
    let lock_path = root.join("desktop.lock");
    let Some(lock) = InstanceLock::acquire(&lock_path)? else {
        let old = read(&root.join("instance.dpapi"), json!({}))?;
        output(&handshake(
            object_text(&old, "url")?,
            false,
            old["core"].clone(),
        )?)?;
        return Ok(());
    };
    let app = App::new(&root, legacy)?;
    let listener = TcpListener::bind(("127.0.0.1", 0))
        .map_err(|_| Error::new("network_unavailable", "本机服务端口无法打开。"))?;
    listener
        .set_nonblocking(true)
        .map_err(|_| storage_error())?;
    let origin = format!(
        "http://127.0.0.1:{}",
        listener.local_addr().map_err(|_| storage_error())?.port()
    );
    let token = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let mut core = runtime()?.block_on(async {
        crate::Core::new()?
            .dispatch(json!({"operation":"status","input":{}}))
            .await
    })?;
    core["desktop_backend"] = json!("rust");
    let url = format!("{origin}/#{token}");
    let instance = json!({"url":url,"pid":std::process::id(),"core":core});
    write(&root.join("instance.dpapi"), &instance)?;
    output(&handshake(&url, true, core)?)?;
    let parent = Arc::clone(&app);
    thread::spawn(move || {
        let _ = std::io::copy(&mut std::io::stdin().lock(), &mut std::io::sink());
        let _ = parent.stop();
        if let Ok(mut state) = parent.state.lock() {
            state.closing = true;
        }
        loop {
            if parent.state.lock().is_ok_and(|s| !s.busy) {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
        parent.shutdown.store(true, Ordering::SeqCst);
    });
    let digest = Arc::clone(&app);
    thread::spawn(move || {
        let _ = digest
            .legacy
            .call("digest_resume", json!({}), &digest.stopped, &|_, _| {});
    });
    let active = Arc::new(AtomicUsize::new(0));
    while !app.shutdown.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((mut stream, address)) => {
                if !address.ip().is_loopback() {
                    continue;
                }
                if active.fetch_add(1, Ordering::SeqCst) >= 32 {
                    active.fetch_sub(1, Ordering::SeqCst);
                    Response::error(429, "本机请求过多，请稍后重试。").send(&mut stream);
                    continue;
                }
                let active = Arc::clone(&active);
                let app = Arc::clone(&app);
                let origin = origin.clone();
                let token = token.clone();
                thread::spawn(move || {
                    struct Count(Arc<AtomicUsize>);
                    impl Drop for Count {
                        fn drop(&mut self) {
                            self.0.fetch_sub(1, Ordering::SeqCst);
                        }
                    }
                    let _count = Count(active);
                    handle(stream, &app, &origin, &token);
                });
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(25))
            }
            Err(_) => {
                let _ = app.stop();
                return Err(storage_error());
            }
        }
    }
    // Let the owned shutdown response reach the host before exiting the process.
    // Existing requests are bounded and no further connections are accepted.
    drop(listener);
    while active.load(Ordering::SeqCst) > 0 {
        thread::sleep(Duration::from_millis(10));
    }
    // Only remove our own authenticated instance record. Another version's files
    // and any unknown/uncertain business state remain untouched.
    if read(&root.join("instance.dpapi"), json!({})).is_ok_and(|v| v == instance) {
        remove_owned(&root.join("instance.dpapi"))?;
    }
    drop(lock);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn parse_raw(raw: &str) -> std::result::Result<Request, u16> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let raw = raw.to_owned();
        let send = thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            stream.write_all(raw.as_bytes()).unwrap();
        });
        let (mut stream, _) = listener.accept().unwrap();
        let result = parse(&mut BufReader::new(&mut stream)).map_err(|e| e.status);
        send.join().unwrap();
        result
    }
    #[test]
    fn http_rejects_ambiguous_and_proxy_requests() {
        for raw in ["GET https://example.com/ HTTP/1.1\r\nHost: x\r\n\r\n","GET /api/status HTTP/1.1\r\nHost: x\r\nhost: y\r\n\r\n","POST /api/login HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nContent-Length: 0\r\n\r\n","GET /api/chat?id=a&id=b HTTP/1.1\r\nHost: x\r\n\r\n","POST /api/login?x=1 HTTP/1.1\r\nContent-Length: 0\r\n\r\n"]{assert!(parse_raw(raw).is_err());}
        assert!(parse_raw("GET /api/chat?id=abc HTTP/1.1\r\nHost: 127.0.0.1:1\r\n\r\n").is_ok());
    }
    #[test]
    fn authentication_requires_host_origin_and_per_launch_capability() {
        let mut r=parse_raw("POST /api/stop HTTP/1.1\r\nHost: 127.0.0.1:123\r\nContent-Length: 2\r\nOrigin: http://127.0.0.1:123\r\nX-School-Token: abc\r\n\r\n").unwrap();
        assert!(authorized(&r, "http://127.0.0.1:123", "abc"));
        r.headers
            .insert("origin".into(), "https://evil.example".into());
        assert!(!authorized(&r, "http://127.0.0.1:123", "abc"));
        assert!(!same_secret("abcx", "abc"));
        assert!(!same_secret("ab", "abc"));
    }
    #[test]
    fn owned_instance_handshake_never_trusts_remote_urls() {
        let token = "a".repeat(64);
        assert!(handshake(
            &format!("http://127.0.0.1:123/#{token}"),
            false,
            Value::Null
        )
        .is_ok());
        for url in [
            format!("http://localhost:123/#{token}"),
            format!("http://127.0.0.1:123/a#{token}"),
            format!("https://127.0.0.1:123/#{token}"),
            "http://127.0.0.1:123/#short".into(),
        ] {
            assert!(handshake(&url, false, Value::Null).is_err());
        }
    }
    #[test]
    fn filename_is_strict_utf8_and_preserves_plus() {
        assert_eq!(filename("a+b%20c.txt").unwrap(), "a+b c.txt");
        assert!(filename("%ff.txt").is_err());
        assert!(filename("a%2").is_err());
    }

    #[cfg(windows)]
    struct FakeLegacy {
        background: AtomicBool,
    }
    #[cfg(windows)]
    impl chat_io::Legacy for FakeLegacy {
        fn call(
            &self,
            action: &str,
            _input: Value,
            _stopped: &AtomicBool,
            _notify: &dyn Fn(&str, &str),
        ) -> Result<Value> {
            match action {
                "metadata" => Ok(json!({"mail":{"configured":false}})),
                "diagnostics_record" => Ok(json!({"diagnostic_saved":true})),
                "background_status" => Ok(
                    json!({"digest_enabled":self.background.load(Ordering::SeqCst),"digest_running":false,"login_workers_running":false,"adapter_uncertain":false,"adapter_running":false}),
                ),
                "diagnostics_clear" => Ok(json!({"cleared":true})),
                _ => Err(Error::invalid()),
            }
        }
    }
    #[cfg(windows)]
    struct Fixture {
        root: PathBuf,
        app: Arc<App>,
        legacy: Arc<FakeLegacy>,
    }
    #[cfg(windows)]
    impl Fixture {
        fn new() -> Self {
            let root =
                std::env::temp_dir().join(format!("school-desktop-test-{}", uuid::Uuid::new_v4()));
            app_call(&root, "initialize", json!({})).unwrap();
            let legacy = Arc::new(FakeLegacy {
                background: AtomicBool::new(false),
            });
            let app = App::new(&root, legacy.clone()).unwrap();
            Self { root, app, legacy }
        }
        fn login(&self) {
            sessions::dispatch("sessions_identity_save",&json!({"profile":"identity","path":self.root.join("profile/ustc-identity.credentials.dpapi"),"username":"synthetic_student"})).unwrap();
        }
    }
    #[cfg(windows)]
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = self.app.stop();
            while self.app.state.lock().is_ok_and(|s| s.busy) {
                thread::sleep(Duration::from_millis(5));
            }
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[cfg(windows)]
    #[test]
    fn digest_direct_routes_keep_host_identity_and_inbox_receipts() {
        // FakeLegacy rejects digest actions: these reads/writes must be native.
        let fixture = Fixture::new();
        let app = &fixture.app;
        assert!(app.get("/api/digest", &BTreeMap::new()).is_err());
        fixture.login();
        let view = app.get("/api/digest", &BTreeMap::new()).unwrap();
        let view: Value = serde_json::from_slice(&view.body).unwrap();
        assert_eq!(view["settings"]["email_enabled"], false);
        assert_eq!(view["worker_running"], false);
        let preview = app
            .digest(
                "preview_report",
                json!({"root":"ignored","owner":"foreign"}),
            )
            .unwrap();
        assert_eq!(preview["delivery"]["status"], "app_only");
        let result = app
            .api(
                "/api/digest/read",
                json!({"message_id":preview["message_id"],"root":"ignored","owner":"foreign"}),
            )
            .unwrap();
        assert_eq!(result["read"], true);
        let same = app.digest("preview_report", json!({})).unwrap();
        assert_eq!(same["message_id"], preview["message_id"]);
        let stored = app.digest("read", json!({})).unwrap();
        assert_eq!(stored["messages"][0]["read"], true);
        assert_ne!(stored["owner"], "foreign");
        assert!(fixture.root.join("profile/digest.sqlite3").is_file());
    }

    #[cfg(windows)]
    #[test]
    fn instance_byte_lock_allows_legacy_append_and_rejects_second_owner() {
        let fixture = Fixture::new();
        let path = fixture.root.join("desktop.lock");
        let first = InstanceLock::acquire(&path).unwrap().unwrap();
        fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"0")
            .unwrap();
        assert_eq!(fs::metadata(&path).unwrap().len(), 2);
        assert!(InstanceLock::acquire(&path).unwrap().is_none());
        drop(first);
        assert!(InstanceLock::acquire(&path).unwrap().is_some());
    }

    #[cfg(windows)]
    #[test]
    fn native_operation_gates_stop_and_persist_terminal_state() {
        let fixture = Fixture::new();
        let app = &fixture.app;
        let started = app
            .start("synthetic", |app| {
                app.notify("waiting", "等待本机测试停止");
                while !app.stopped.load(Ordering::SeqCst) {
                    thread::sleep(Duration::from_millis(5));
                }
                Ok(json!({"state":"stopped"}))
            })
            .unwrap();
        assert_eq!(started["started"], true);
        assert!(app.start("duplicate", |_| Ok(json!({}))).is_err());
        assert!(app.api("/api/shutdown", json!({})).is_err());
        assert_eq!(
            app.api("/api/stop", json!({})).unwrap()["stop_requested"],
            true
        );
        for _ in 0..200 {
            if !app.state.lock().unwrap().busy {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        assert!(!app.state.lock().unwrap().busy);
        assert_eq!(
            read(&fixture.root.join("operation.dpapi"), Value::Null).unwrap()["state"],
            "stopped"
        );
    }
    #[cfg(windows)]
    #[test]
    fn account_lock_background_guards_and_approval_cancellation_are_native() {
        let fixture = Fixture::new();
        fixture.login();
        let app = &fixture.app;
        fixture.legacy.background.store(true, Ordering::SeqCst);
        assert!(app
            .clear(&json!({"scope":"account","confirmation":"account"}))
            .is_err());
        assert_eq!(app.identity()["saved"], true);
        fixture.legacy.background.store(false, Ordering::SeqCst);
        let mut chat = app.new_chat().unwrap();
        let approval = uuid::Uuid::new_v4().simple().to_string();
        chat["pending"] = json!({"id":approval,"call_id":"synthetic-call"});
        chat["turn_authorized"] = json!(true);
        storage::chat_save(&json!({"database":app.database(),"chat":chat})).unwrap();
        let request = json!({"chat_id":chat["id"],"approval_id":approval,"approve":false});
        assert_eq!(app.approval(&request).unwrap()["cancelled"], true);
        assert!(app.approval(&request).is_err());
        let saved = app.chat(chat["id"].as_str().unwrap()).unwrap();
        assert!(saved["pending"].is_null());
        assert_eq!(saved["turn_authorized"], false);
        app.clear(&json!({"scope":"logout","confirmation":"logout"}))
            .unwrap();
        assert!(app.require_login().is_err());
        assert!(app.status().unwrap()["identity"]["account"].is_null());
        app.clear(&json!({"scope":"account","confirmation":"account"}))
            .unwrap();
        assert_eq!(app.identity()["saved"], false);
    }
    #[cfg(windows)]
    #[test]
    fn native_http_preserves_binary_fifty_mib_upload_without_python() {
        let fixture = Fixture::new();
        fixture.login();
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let app = Arc::clone(&fixture.app);
        let server_origin = origin.clone();
        let token = "synthetic-capability";
        let worker = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            handle(stream, &app, &server_origin, token);
        });
        let body = vec![0xa5; MAX_UPLOAD];
        let expected = format!("{:x}", Sha256::digest(&body));
        let result = runtime().unwrap().block_on(async {
            let response = reqwest::Client::builder()
                .no_proxy()
                .build()
                .unwrap()
                .post(format!("{origin}/api/upload"))
                .header("X-School-Token", token)
                .header("Origin", &origin)
                .header("Sec-Fetch-Site", "same-origin")
                .header("X-File-Name", "synthetic.bin")
                .header("Content-Type", "application/octet-stream")
                .body(body)
                .send()
                .await
                .unwrap();
            assert_eq!(response.status().as_u16(), 200);
            response.json::<Value>().await.unwrap()
        });
        worker.join().unwrap();
        assert_eq!(result["size"], MAX_UPLOAD);
        let path = PathBuf::from(result["path"].as_str().unwrap());
        assert!(path.starts_with(fixture.root.join("profile/uploads")));
        assert_eq!(
            format!("{:x}", Sha256::digest(fs::read(path).unwrap())),
            expected
        );
        assert!(fixture.app.upload("../outside", b"x").is_err());
    }
    #[cfg(windows)]
    #[test]
    fn native_http_rejects_origin_before_executing_and_duplicate_json() {
        let fixture = Fixture::new();
        let app = &fixture.app;
        for (origin_header, body, expected) in [
            ("https://invalid.example", br#"{}"#.as_slice(), 403),
            (
                "same",
                br#"{"enabled":true,"enabled":false}"#.as_slice(),
                400,
            ),
        ] {
            let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
            let origin = format!("http://{}", listener.local_addr().unwrap());
            let server_app = Arc::clone(app);
            let server_origin = origin.clone();
            let worker = thread::spawn(move || {
                let (stream, _) = listener.accept().unwrap();
                handle(stream, &server_app, &server_origin, "synthetic-capability");
            });
            let status = runtime().unwrap().block_on(async {
                reqwest::Client::builder()
                    .no_proxy()
                    .build()
                    .unwrap()
                    .post(format!("{origin}/api/email-verification"))
                    .header("X-School-Token", "synthetic-capability")
                    .header(
                        "Origin",
                        if origin_header == "same" {
                            &origin
                        } else {
                            origin_header
                        },
                    )
                    .header("Content-Type", "application/json")
                    .body(body.to_vec())
                    .send()
                    .await
                    .unwrap()
                    .status()
                    .as_u16()
            });
            worker.join().unwrap();
            assert_eq!(status, expected);
            assert_eq!(app.identity()["saved"], false);
        }
    }
}
