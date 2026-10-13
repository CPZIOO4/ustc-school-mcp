mod backend;
mod policy;

use std::{path::PathBuf, sync::{atomic::{AtomicBool, Ordering}, Arc}, time::{SystemTime, UNIX_EPOCH}};
use tauri::{Emitter, Manager};
use tokio::sync::OnceCell;

struct Host {
    backend: OnceCell<Result<backend::Backend, String>>,
    resources: PathBuf,
    data_dir: PathBuf,
    closing: AtomicBool,
}

impl Host {
    async fn backend(&self) -> Result<&backend::Backend, String> {
        self.backend.get_or_init(|| async {
            let resources = self.resources.clone();
            let root = self.data_dir.clone();
            let result = tauri::async_runtime::spawn_blocking(move || backend::launch(resources, root))
                .await.map_err(|_| "本地引擎初始化中断。".to_string()).and_then(|r| r);
            if result.is_err() { self.log("engine_start_failed"); }
            result
        }).await.as_ref().map_err(Clone::clone)
    }

    fn log(&self, code: &'static str) {
        // Codes only: no arguments, paths, URLs, bodies, tokens or raw errors.
        use std::io::Write;
        let dir = self.data_dir.join("diagnostics");
        if std::fs::create_dir_all(&dir).is_err() { return; }
        let file = dir.join("native-events.jsonl");
        if std::fs::metadata(&file).is_ok_and(|m| m.len() > 256 * 1024) {
            let _ = std::fs::rename(&file, dir.join("native-events.previous.jsonl"));
        }
        if let Ok(mut output) = std::fs::OpenOptions::new().create(true).append(true).open(file) {
            let at = SystemTime::now().duration_since(UNIX_EPOCH).map(|t| t.as_secs()).unwrap_or(0);
            let _ = writeln!(output, "{{\"at\":{at},\"code\":\"{code}\",\"component\":\"native\"}}");
        }
    }
}

#[tauri::command]
async fn backend_request(request: tauri::ipc::Request<'_>, host: tauri::State<'_, Arc<Host>>, app: tauri::AppHandle) -> Result<tauri::ipc::Response, String> {
    let header = |key| request.headers().get(key).and_then(|v| v.to_str().ok()).map(str::to_owned);
    let route = header("x-school-route").ok_or("缺少本机操作地址。")?;
    let method = header("x-school-method").ok_or("缺少本机操作方法。")?;
    let body = match request.body() {
        tauri::ipc::InvokeBody::Raw(bytes) => bytes.clone(),
        _ => return Err("本机请求格式无效。".into()),
    };
    policy::validate_route(&method, &route, body.len())?;
    let result = host.backend().await?.request(&method, &route, body, header("x-school-file")).await;
    let (status, bytes) = match result { Ok(r) => r, Err(error) => {host.log("engine_request_failed"); return Err(error);} };
    if route == "/api/shutdown" && status == 200 {
        tauri::async_runtime::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(400)).await;
            app.exit(0);
        });
    }
    let mut packed = Vec::with_capacity(bytes.len() + 2);
    packed.extend_from_slice(&status.to_be_bytes());
    packed.extend_from_slice(&bytes);
    Ok(tauri::ipc::Response::new(packed))
}

#[tauri::command]
async fn open_external(url: String) -> Result<(), String> {
    let url = policy::external_url(&url)?;
    open::that_detached(url.as_str()).map_err(|_| "无法打开系统浏览器。".into())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let application = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![backend_request, open_external])
        .setup(|app| {
            let args: Vec<String> = std::env::args().collect();
            let override_dir = args.windows(2).find(|v| v[0] == "--data-dir").map(|v| PathBuf::from(&v[1]));
            let data_dir = override_dir.unwrap_or_else(|| std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from).unwrap_or_else(|| app.path().app_local_data_dir().unwrap()).join("USTCSchoolAgent"));
            let host = Arc::new(Host { backend: OnceCell::new(), resources: app.path().resource_dir()?,
                data_dir: data_dir.clone(), closing: AtomicBool::new(false) });
            // Keep WebView cache outside the business directory: the engine must
            // initialize/validate that directory before anything else writes it.
            use std::hash::{Hash, Hasher};
            let mut identity = std::collections::hash_map::DefaultHasher::new();
            data_dir.hash(&mut identity);
            let webview_dir = app.path().app_cache_dir()?.join(format!("webview-{:x}", identity.finish()));
            app.manage(host.clone());
            tauri::async_runtime::spawn(async move { let _ = host.backend().await; });
            tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
                .title("学校助手").inner_size(1220.0, 820.0).min_inner_size(390.0, 560.0)
                .visible(!args.iter().any(|v| v == "--hidden"))
                .data_directory(webview_dir)
                .on_navigation(policy::local_ui)
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let app = window.app_handle().clone();
                let host = app.state::<Arc<Host>>().inner().clone();
                if host.closing.swap(true, Ordering::SeqCst) { return; }
                tauri::async_runtime::spawn(async move {
                    let result = match host.backend().await { Ok(backend) => backend.close().await, Err(_) => Ok(()) };
                    if let Err(message) = result {
                        host.closing.store(false, Ordering::SeqCst);
                        host.log("close_deferred");
                        let _ = app.emit("native-notice", message);
                    } else { app.exit(0); }
                });
            }
        })
        .build(tauri::generate_context!());
    match application {
        Ok(app) => app.run(|_, _| {}),
        Err(_) => {
            // No raw error/URL/credential is printed on startup failure.
            eprintln!("学校助手窗口启动失败，请检查 WebView2 和安装包是否完整。");
            std::process::exit(1);
        }
    }
}
