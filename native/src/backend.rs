use crate::policy;
use serde::Deserialize;
use std::{
    io::{BufRead, BufReader, Read},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};

#[derive(Deserialize)]
struct Handshake {
    protocol: u8,
    origin: String,
    token: String,
    owned: bool,
    core: Option<serde_json::Value>,
}

pub struct Backend {
    origin: String,
    token: String,
    pub owned: bool,
    pub stopped: AtomicBool,
    client: reqwest::Client,
    // Retain stdin for the host lifetime; dropping it releases only our own engine.
    _child: Mutex<Child>,
}

fn engine_path(resources: &Path) -> Result<PathBuf, String> {
    let bundled = resources.join("engine/USTC-School-Engine.exe");
    if bundled.is_file() {
        return Ok(bundled);
    }
    #[cfg(debug_assertions)]
    {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let python = root.join(".venv/Scripts/python.exe");
        if python.is_file() {
            return Ok(python);
        }
    }
    Err("缺少本地业务引擎，请重新解压完整安装包。".into())
}

pub fn launch(resources: PathBuf, data_dir: PathBuf) -> Result<Backend, String> {
    if !cfg!(target_os = "windows") {
        return Err("此版本的业务引擎暂仅支持 Windows；其他平台适配尚未完成。".into());
    }
    let engine = engine_path(&resources)?;
    let core = resources.join("school-core.exe");
    #[cfg(debug_assertions)]
    let core = if !core.is_file() {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("core/target/debug/school-core.exe")
    } else {
        core
    };
    if !core.is_file() {
        return Err("缺少 Rust 核心，请重新解压完整安装包。".into());
    }
    let mut command = Command::new(&core);
    command
        .env("SCHOOL_NATIVE_CORE", &core)
        .env("SCHOOL_LEGACY_ENGINE", engine);
    command
        .args(["--desktop", "--native-stdio", "--data-dir"])
        .arg(data_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }
    let mut child = command.spawn().map_err(|_| "本地业务引擎未能启动。")?;
    let stdout = child.stdout.take().ok_or("本地业务引擎通信失败。")?;
    let (send, receive) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let result = reader
            .by_ref()
            .take(4096)
            .read_line(&mut line)
            .map(|_| line)
            .map_err(|_| ());
        let _ = send.send(result);
        // No subprocess output is relayed to the WebView or diagnostic logs.
        let _ = std::io::copy(&mut reader, &mut std::io::sink());
    });
    let line = receive
        .recv_timeout(Duration::from_secs(45))
        .map_err(|_| "本地业务引擎启动超时，请重新打开应用。")?
        .map_err(|_| "本地业务引擎通信中断。")?;
    let hello: Handshake = serde_json::from_str(&line).map_err(|_| "本地业务引擎版本不兼容。")?;
    if hello.protocol != 1 {
        return Err("本地业务引擎版本不兼容。".into());
    }
    if hello.core.as_ref().is_none_or(|c| {
        c["implementation"] != "rust" || c["version"] != "0.9.0" || c["desktop_backend"] != "rust"
    }) {
        return Err(
            "原应用仍在使用旧引擎；请先在原应用中结束任务并退出，再打开新版。登录态会保留。".into(),
        );
    }
    policy::validate_origin(&hello.origin, &hello.token)?;
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(60))
        .pool_idle_timeout(Duration::from_secs(90))
        .build()
        .map_err(|_| "本机通信初始化失败。")?;
    Ok(Backend {
        origin: hello.origin,
        token: hello.token,
        owned: hello.owned,
        stopped: AtomicBool::new(false),
        client,
        _child: Mutex::new(child),
    })
}

impl Backend {
    pub async fn request(
        &self,
        method: &str,
        route: &str,
        body: Vec<u8>,
        filename: Option<String>,
    ) -> Result<(u16, Vec<u8>), String> {
        policy::validate_route(method, route, body.len())?;
        self.request_private(method, route, body, filename).await
    }
    pub async fn browser_session(&self, host: &str) -> Result<serde_json::Value, String> {
        if host.len()>253 || !host.bytes().all(|b|b.is_ascii_alphanumeric()||b".-".contains(&b)){return Err("网站地址无效。".into());}
        let (status,bytes)=self.request_private("GET", &format!("/api/native-browser-session?host={host}"),Vec::new(),None).await?;
        if status!=200{return Err("无法继承当前账号会话，请在账号页检查登录。".into());}
        serde_json::from_slice(&bytes).map_err(|_|"会话响应无效。".into())
    }
    async fn request_private(&self, method:&str, route:&str, body:Vec<u8>, filename:Option<String>) -> Result<(u16,Vec<u8>),String> {
        if self.stopped.load(Ordering::SeqCst) {
            return Err("应用已退出，请重新打开。".into());
        }
        let method =
            reqwest::Method::from_bytes(method.as_bytes()).map_err(|_| "请求方法无效。")?;
        let mut request = self
            .client
            .request(method.clone(), format!("{}{route}", self.origin))
            .header("X-School-Token", &self.token);
        if method == reqwest::Method::POST {
            request = request
                .header("Origin", &self.origin)
                .header("Sec-Fetch-Site", "same-origin")
                .header(
                    "Content-Type",
                    if route == "/api/upload" {
                        "application/octet-stream"
                    } else {
                        "application/json"
                    },
                )
                .body(body);
        }
        if let Some(filename) = filename {
            if route != "/api/upload"
                || filename.len() > 2048
                || !filename.is_ascii()
                || filename.bytes().any(|c| c < 32 || c == 127)
            {
                return Err("文件名格式无效。".into());
            }
            request = request.header("X-File-Name", filename);
        }
        // Never retry a request: timeouts on writes mean the outcome needs checking.
        let mut response = request
            .send()
            .await
            .map_err(|_| "本机服务请求未完成；提交类操作请先核实结果，勿直接重试。")?;
        let status = response.status().as_u16();
        if response
            .content_length()
            .is_some_and(|n| n > policy::MAX_RESPONSE as u64)
        {
            return Err("本机结果过大。".into());
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| "本机结果传输中断。")? {
            if bytes.len() + chunk.len() > policy::MAX_RESPONSE {
                return Err("本机结果过大。".into());
            }
            bytes.extend_from_slice(&chunk);
        }
        if route == "/api/shutdown" && status == 200 {
            self.stopped.store(true, Ordering::SeqCst);
        }
        Ok((status, bytes))
    }

    pub async fn close(&self) -> Result<(), String> {
        if self.stopped.load(Ordering::SeqCst) {
            return Ok(());
        }
        // Another browser/native launch may own this engine. Merely closing our
        // window must not terminate its work or its authorized digest worker.
        if !self.owned {
            return Ok(());
        }
        if self
            ._child
            .lock()
            .map_err(|_| "引擎状态暂不可用。")?
            .try_wait()
            .map_err(|_| "无法检查引擎状态。")?
            .is_some()
        {
            self.stopped.store(true, Ordering::SeqCst);
            return Ok(());
        }
        let (status, body) = self
            .request("POST", "/api/shutdown", b"{}".to_vec(), None)
            .await?;
        if status != 200 {
            let _ = body;
            return Err("当前任务仍在运行，请先等待或在应用中停止任务，再退出。".into());
        }
        Ok(())
    }
}
