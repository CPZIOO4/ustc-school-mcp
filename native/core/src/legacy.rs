//! Private, fixed-action bridge for business modules not migrated to Rust yet.
use crate::{chat_io::Legacy, strict_json, Error, Result};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Mutex,
    },
    time::{Duration, Instant},
};

const LIMIT: usize = 16 * 1024 * 1024;
const ACTIONS: &[&str] = &[
    "task_context",
    "tool_dispatch",
    "connect",
    "diagnostic",
    "metadata",
    "service_check",
    "mail_bind",
    "campus_sites",
    "campus_profile",
    "campus_profile_save",
    "campus_timetable",
    "campus_timetable_refresh",
    "digest_overview",
    "digest_plan",
    "digest_preview_report",
    "digest_configure",
    "digest_refresh",
    "digest_read",
    "digest_courses",
    "digest_calendar",
    "digest_summarize",
    "digest_resume",
    "diagnostics_report",
    "diagnostics_export",
    "diagnostics_clear",
    "diagnostics_record",
    "school_model_keys",
    "school_model_import",
    "background_status",
    "tasks_list",
    "study_read",
    "study_flow",
    "identity_restore",
];

fn unavailable() -> Error {
    Error::new(
        "runtime_failure",
        "兼容业务通信中断；先核实原回执，没有自动重试。",
    )
}

struct Worker {
    _child: Child,
    input: ChildStdin,
    output: mpsc::Receiver<Result<Value>>,
}

pub struct Bridge {
    root: PathBuf,
    read: Mutex<Option<Worker>>,
    business: Mutex<Option<Worker>>,
    uncertain: AtomicBool,
}

impl Bridge {
    pub fn new(root: &Path) -> Result<Self> {
        if !root.is_absolute() {
            return Err(Error::invalid());
        }
        Ok(Self {
            root: root.to_owned(),
            read: Mutex::new(None),
            business: Mutex::new(None),
            uncertain: AtomicBool::new(false),
        })
    }

    fn spawn(&self) -> Result<Worker> {
        let executable = std::env::var_os("SCHOOL_LEGACY_ENGINE")
            .map(PathBuf::from)
            .ok_or_else(unavailable)?;
        if !executable.is_absolute() || !executable.is_file() {
            return Err(unavailable());
        }
        let mut command = Command::new(&executable);
        if executable.file_name().and_then(|s| s.to_str()) == Some("USTC-School-Engine.exe") {
            command.arg("--adapter-stdio");
        } else {
            #[cfg(debug_assertions)]
            {
                let root = Path::new(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .and_then(Path::parent)
                    .ok_or_else(unavailable)?;
                let expected = root.join(".venv/Scripts/python.exe");
                if executable != expected {
                    return Err(unavailable());
                }
                command
                    .args(["-X", "utf8", "-m", "school_mcp.desktop.legacy_worker"])
                    .current_dir(root);
            }
            #[cfg(not(debug_assertions))]
            return Err(unavailable());
        }
        command
            .env(
                "SCHOOL_NATIVE_CORE",
                std::env::current_exe().map_err(|_| unavailable())?,
            )
            .env("SCHOOL_MCP_LOCAL_DIR", self.root.join("profile"))
            .env("SCHOOL_MCP_BROWSER_HEADED", "0")
            .env("OPENBLAS_NUM_THREADS", "1")
            .env("OMP_NUM_THREADS", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command.spawn().map_err(|_| unavailable())?;
        let input = child.stdin.take().ok_or_else(unavailable)?;
        let mut output = child.stdout.take().ok_or_else(unavailable)?;
        let (tx, rx) = mpsc::sync_channel(8);
        std::thread::spawn(move || loop {
            let read = || -> Result<Value> {
                let mut head = [0; 4];
                output.read_exact(&mut head).map_err(|_| unavailable())?;
                let n = u32::from_be_bytes(head) as usize;
                if n == 0 || n > LIMIT {
                    return Err(unavailable());
                }
                let mut bytes = vec![0; n];
                output.read_exact(&mut bytes).map_err(|_| unavailable())?;
                strict_json::parse(&bytes).map_err(|_| unavailable())
            }();
            let failed = read.is_err();
            if tx.send(read).is_err() || failed {
                break;
            }
        });
        Ok(Worker {
            _child: child,
            input,
            output: rx,
        })
    }
}

impl Legacy for Bridge {
    fn call(
        &self,
        action: &str,
        input: Value,
        stopped: &AtomicBool,
        notify: &dyn Fn(&str, &str),
    ) -> Result<Value> {
        if !ACTIONS.contains(&action) || !input.is_object() {
            return Err(Error::invalid());
        }
        if action == "connect" {
            let service = input["service"].as_str().ok_or_else(Error::invalid)?;
            let checked = self.call("service_check", input.clone(), stopped, notify)?;
            if checked["connected"] == true
                || stopped.load(Ordering::SeqCst)
                || !matches!(
                    checked["error_code"].as_str(),
                    Some("authentication_required" | "identity_required")
                )
                || !matches!(
                    service,
                    "bb" | "jw" | "young" | "library" | "finance" | "nan7"
                )
            {
                return Ok(checked);
            }
            // Reuse the explicitly linked account provider before asking for
            // manual CAS interaction. The provider returns sessions only.
            let shared=self.call("identity_restore",json!({"service":service}),stopped,notify)?;
            if shared["linked"]==true {
                return if shared["connected"]==true {self.call("service_check",input,stopped,notify)} else {Ok(shared)};
            }
            let request =
                json!({"root":self.root,"service":service,"headed":false,"timeout_seconds":120});
            let result = std::thread::scope(|scope| {
                let (tx, rx) = mpsc::channel();
                scope.spawn(move || {
                    let result = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .map_err(|_| unavailable())
                        .and_then(|rt| {
                            rt.block_on(crate::login::run(&request, stopped, &|s, d| {
                                let _ = tx.send(Ok(json!({"stage":s,"detail":d})));
                            }))
                        });
                    let _ = tx.send(result.map(|result| json!({"result":result})));
                });
                while let Ok(value) = rx.recv() {
                    let value = value?;
                    if let Some(result) = value.get("result") {
                        return Ok(result.clone());
                    }
                    notify(
                        value["stage"].as_str().unwrap_or("authenticating"),
                        value["detail"].as_str().unwrap_or("正在恢复登录。"),
                    );
                }
                Err(unavailable())
            })?;
            if result["connected"] != true {
                return Ok(result);
            }
            return self.call("service_check", input, stopped, notify);
        }
        // Status/diagnostics remain available while a tool or model is waiting.
        let is_read = matches!(
            action,
            "metadata"
                | "diagnostics_report"
                | "diagnostics_export"
                | "diagnostics_record"
                | "diagnostic"
                | "background_status"
                | "campus_sites"
                | "campus_profile"
                | "campus_timetable"
                | "tasks_list"
        );
        let lane = if is_read { &self.read } else { &self.business };
        let mut guard = lane.lock().map_err(|_| unavailable())?;
        if guard.is_none() {
            *guard = Some(self.spawn()?);
        }
        let stop_name = format!(".adapter-stop-{}", uuid::Uuid::new_v4().simple());
        let stop_path = self.root.join(&stop_name);
        let bytes = serde_json::to_vec(
            &json!({"action":action,"input":input,"root":self.root,"stop_name":stop_name}),
        )
        .map_err(|_| Error::invalid())?;
        if bytes.len() > LIMIT {
            return Err(Error::new("context_capacity", "本机业务请求过大。"));
        }
        let mut sent = false;
        let mut received = false;
        let mut result = (|| {
            let worker = guard.as_mut().ok_or_else(unavailable)?;
            if stopped.load(Ordering::SeqCst) {
                std::fs::write(&stop_path, []).map_err(|_| unavailable())?;
            }
            sent = true;
            worker
                .input
                .write_all(&(bytes.len() as u32).to_be_bytes())
                .map_err(|_| unavailable())?;
            worker
                .input
                .write_all(&bytes)
                .and_then(|_| worker.input.flush())
                .map_err(|_| unavailable())?;
            let began = Instant::now();
            loop {
                if stopped.load(Ordering::SeqCst) || began.elapsed() > Duration::from_secs(1200) {
                    std::fs::write(&stop_path, []).map_err(|_| unavailable())?;
                }
                if began.elapsed() > Duration::from_secs(1230) {
                    return Err(unavailable());
                }
                match worker.output.recv_timeout(Duration::from_millis(100)) {
                    Ok(reply) => {
                        let reply = reply?;
                        if let Some(event) = reply.get("event") {
                            let stage = event["stage"].as_str().ok_or_else(unavailable)?;
                            let detail = event["detail"].as_str().ok_or_else(unavailable)?;
                            if stage.len() > 80 || detail.len() > 2000 {
                                return Err(unavailable());
                            }
                            notify(stage, detail);
                            continue;
                        }
                        if reply["ok"] == true {
                            received = reply.get("result").is_some();
                            return reply.get("result").cloned().ok_or_else(unavailable);
                        }
                        if reply["ok"] != false {
                            return Err(unavailable());
                        }
                        received = true;
                        let code = reply["error"]["code"].as_str().unwrap_or("runtime_failure");
                        let codes = [
                            "invalid_arguments",
                            "authentication_required",
                            "identity_required",
                            "manual_verification_required",
                            "login_password_required",
                            "login_email_required",
                            "login_captcha_required",
                            "login_phone_required",
                            "login_page_unrecognized",
                            "interrupted",
                            "task_access_denied",
                            "cooldown",
                            "timeout",
                            "login_failed",
                            "login_busy",
                            "storage_failure",
                            "runtime_failure",
                            "page_changed",
                            "permission_denied",
                            "missing_file",
                            "unsupported_format",
                            "file_too_large",
                            "unreadable_file",
                            "text_too_large",
                            "unavailable",
                        ];
                        let code = codes
                            .into_iter()
                            .find(|c| *c == code)
                            .unwrap_or("runtime_failure");
                        let detail=crate::recovery::failure(&json!({"code":code}))?;
                        return Err(Error::new(code,detail["next_step"].as_str().unwrap_or("本机兼容业务未完成，请查看诊断。")));
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(_) => return Err(unavailable()),
                }
            }
        })();
        if !received && sent && !is_read {
            self.uncertain.store(true, Ordering::SeqCst);
            let _ = crate::storage::private_write(
                &json!({"path":self.root.join("adapter-uncertain.dpapi"),
                "value":{"state":"unknown","action":action,"at":chrono::Utc::now().to_rfc3339(),
                    "note":"兼容业务通信中断；核实原记录之前保留本机账号和数据，不自动重发。"}}),
            );
        }
        if action == "background_status" {
            if let Ok(ref mut value) = result {
                value["adapter_uncertain"] = json!(
                    self.uncertain.load(Ordering::SeqCst)
                        || self.root.join("adapter-uncertain.dpapi").exists()
                );
            }
        }
        if !received {
            // EOF requests orderly exit after any in-flight write. Never kill or replay it.
            *guard = None;
        } else {
            let _ = std::fs::remove_file(&stop_path);
        }
        result
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    fn fixture(reply: Result<Value>) -> (PathBuf, Bridge) {
        fixture_many(vec![reply])
    }
    fn fixture_many(replies:Vec<Result<Value>>) -> (PathBuf, Bridge) {
        let root =
            std::env::temp_dir().join(format!("school-bridge-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir(&root).unwrap();
        let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .parent()
            .unwrap();
        // This synthetic process only waits for EOF. The test controls the
        // response channel to exercise a lost reply without any school call.
        let mut command = Command::new(repo.join(".venv/Scripts/python.exe"));
        command
            .args(["-c", "import sys; sys.stdin.buffer.read()"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
        let mut child = command.spawn().unwrap();
        let input = child.stdin.take().unwrap();
        let (tx, output) = mpsc::sync_channel(replies.len());
        for reply in replies {tx.send(reply).unwrap();}
        let bridge = Bridge::new(&root).unwrap();
        *bridge.business.lock().unwrap() = Some(Worker {
            _child: child,
            input,
            output,
        });
        (root, bridge)
    }
    #[test]
    fn chat_connection_uses_shared_provider_and_rechecks_session() {
        let (root,bridge)=fixture_many(vec![
            Ok(json!({"ok":true,"result":{"connected":false,"error_code":"authentication_required"}})),
            Ok(json!({"ok":true,"result":{"linked":true,"connected":true}})),
            Ok(json!({"ok":true,"result":{"connected":true,"rechecked":true}})),
        ]);
        let v=bridge.call("connect",json!({"service":"bb"}),&AtomicBool::new(false),&|_,_|{}).unwrap();
        assert_eq!(v["rechecked"],true);
        drop(bridge);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn shared_provider_verification_does_not_start_another_login() {
        let (root,bridge)=fixture_many(vec![
            Ok(json!({"ok":true,"result":{"connected":false,"error_code":"authentication_required"}})),
            Ok(json!({"ok":true,"result":{"linked":true,"connected":false,"error_code":"login_captcha_required"}})),
        ]);
        let v=bridge.call("connect",json!({"service":"bb"}),&AtomicBool::new(false),&|_,_|{}).unwrap();
        assert_eq!(v["error_code"],"login_captcha_required");
        drop(bridge);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn verification_errors_keep_actionable_fixed_guidance() {
        let (root,bridge)=fixture(Ok(json!({"ok":false,"error":{"code":"login_captcha_required","message":"synthetic secret"}})));
        let error=bridge.call("identity_restore",json!({"service":"bb"}),&AtomicBool::new(false),&|_,_|{}).unwrap_err();
        assert_eq!(error.code,"login_captcha_required");
        assert!(error.message.contains("打开验证窗口"));assert!(!error.message.contains("synthetic secret"));
        drop(bridge);std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn lost_business_reply_keeps_durable_unknown_state() {
        let (root, bridge) = fixture(Err(unavailable()));
        assert!(bridge
            .call(
                "tool_dispatch",
                json!({}),
                &AtomicBool::new(false),
                &|_, _| {}
            )
            .is_err());
        assert!(bridge.uncertain.load(Ordering::SeqCst));
        let marker =
            crate::storage::private_read(&json!({"path":root.join("adapter-uncertain.dpapi")}))
                .unwrap();
        assert_eq!(marker["state"], "unknown");
        assert_eq!(marker["action"], "tool_dispatch");
        drop(bridge);
        assert!(root.join("adapter-uncertain.dpapi").is_file());
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn received_business_failure_is_not_a_lost_transport() {
        let (root, bridge) = fixture(Ok(json!({"ok":false,"error":{"code":"runtime_failure"}})));
        assert!(bridge
            .call(
                "tool_dispatch",
                json!({}),
                &AtomicBool::new(false),
                &|_, _| {}
            )
            .is_err());
        assert!(!bridge.uncertain.load(Ordering::SeqCst));
        assert!(!root.join("adapter-uncertain.dpapi").exists());
        assert!(bridge.business.lock().unwrap().is_some());
        drop(bridge);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cached_views_do_not_wait_for_the_business_lane() {
        for action in ["campus_profile", "campus_timetable", "tasks_list"] {
            let (root, bridge) = fixture(Ok(json!({"ok":true,"result":{"cached":true}})));
            *bridge.read.lock().unwrap() = bridge.business.lock().unwrap().take();
            let bridge = std::sync::Arc::new(bridge);
            let business_guard = bridge.business.lock().unwrap();
            let reader = std::sync::Arc::clone(&bridge);
            let (done, result) = mpsc::channel();
            let worker = std::thread::spawn(move || {
                let reply = reader.call(action, json!({}), &AtomicBool::new(false), &|_, _| {});
                let _ = done.send(reply);
            });
            let immediate = result.recv_timeout(Duration::from_secs(2));
            drop(business_guard);
            worker.join().unwrap();
            assert_eq!(immediate.unwrap().unwrap()["cached"], true, "{action}");
            assert!(!root.join("adapter-uncertain.dpapi").exists());
            drop(bridge);
            std::fs::remove_dir_all(root).unwrap();
        }
    }
}
