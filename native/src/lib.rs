mod backend;
mod policy;
mod browser_tabs;

use std::{path::PathBuf, sync::{atomic::{AtomicBool, Ordering}, Arc}, time::{SystemTime, UNIX_EPOCH}};
use tauri::{Emitter, Manager};
use tokio::sync::OnceCell;

struct Host {
    backend: OnceCell<Result<backend::Backend, String>>,
    resources: PathBuf,
    data_dir: PathBuf,
    closing: AtomicBool,
    tabs: browser_tabs::Tabs,
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
async fn backend_request(request: tauri::ipc::Request<'_>, host: tauri::State<'_, Arc<Host>>, app: tauri::AppHandle, webview:tauri::Webview) -> Result<tauri::ipc::Response, String> {
    if webview.label()!="main"{return Err("网页不能调用本地业务接口。".into());}
    let header = |key| request.headers().get(key).and_then(|v| v.to_str().ok()).map(str::to_owned);
    let route = header("x-school-route").ok_or("缺少本机操作地址。")?;
    let method = header("x-school-method").ok_or("缺少本机操作方法。")?;
    let body = match request.body() {
        tauri::ipc::InvokeBody::Raw(bytes) => bytes.clone(),
        _ => return Err("本机请求格式无效。".into()),
    };
    let clear_scope=if route=="/api/clear"{serde_json::from_slice::<serde_json::Value>(&body).ok().and_then(|v|v["scope"].as_str().map(str::to_owned))}else{None};
    policy::validate_route(&method, &route, body.len())?;
    let result = host.backend().await?.request(&method, &route, body, header("x-school-file")).await;
    let (status, bytes) = match result { Ok(r) => r, Err(error) => {host.log("engine_request_failed"); return Err(error);} };
    if status==200 && ((route=="/api/clear" && clear_scope.as_deref()!=Some("mail")) || (route=="/api/status" && serde_json::from_slice::<serde_json::Value>(&bytes).is_ok_and(|v|v["locked"]==true||v["identity"]["saved"]!=true))){
        let _gate=host.tabs.gate.lock().await;browser_tabs::clear(&app,route=="/api/clear" && serde_json::from_slice::<serde_json::Value>(&bytes).is_ok_and(|v|v["locked"]!=true));
    }
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
#[tauri::command]
async fn browser_open(url:String,background:Option<bool>,app:tauri::AppHandle,webview:tauri::Webview)->Result<(),String>{
    if webview.label()!="main"{return Err("网页不能调用本地浏览器管理接口。".into());}browser_tabs::open_with_activation(app,url,!background.unwrap_or(false)).await
}
#[tauri::command]
async fn browser_action(action:String,id:Option<String>,app:tauri::AppHandle,webview:tauri::Webview)->Result<(),String>{
    if webview.label()!="main"{return Err("网页不能调用本地浏览器管理接口。".into());}browser_tabs::action(app,action,id).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let application = tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![backend_request, open_external, browser_open, browser_action])
        .setup(|app| {
            let args: Vec<String> = std::env::args().collect();
            let override_dir = args.windows(2).find(|v| v[0] == "--data-dir").map(|v| PathBuf::from(&v[1]));
            let data_dir = override_dir.unwrap_or_else(|| std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from).unwrap_or_else(|| app.path().app_local_data_dir().unwrap()).join("USTCSchoolAgent"));
            let host = Arc::new(Host { backend: OnceCell::new(), resources: app.path().resource_dir()?,
                data_dir: data_dir.clone(), closing: AtomicBool::new(false), tabs:browser_tabs::Tabs::default() });
            // Keep WebView cache outside the business directory: the engine must
            // initialize/validate that directory before anything else writes it.
            use std::hash::{Hash, Hasher};
            let mut identity = std::collections::hash_map::DefaultHasher::new();
            data_dir.hash(&mut identity);
            let webview_dir = app.path().app_cache_dir()?.join(format!("webview-{:x}", identity.finish()));
            app.manage(host.clone());
            tauri::async_runtime::spawn(async move { let _ = host.backend().await; });
            let main_builder=tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()));
            #[cfg(debug_assertions)]
            let main_builder=if let Ok(port)=std::env::var("SCHOOL_MAIN_TEST_CDP").and_then(|v|v.parse::<u16>().map_err(|_|std::env::VarError::NotPresent)){main_builder.additional_browser_args(&format!("--remote-debugging-port={port}"))}else{main_builder};
            main_builder
                .title("学校助手").inner_size(1220.0, 820.0).min_inner_size(390.0, 560.0)
                .visible(!args.iter().any(|v| v == "--hidden"))
                .data_directory(webview_dir)
                .on_navigation(policy::local_ui)
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .build()?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event,tauri::WindowEvent::Resized(_)|tauri::WindowEvent::ScaleFactorChanged{..}){browser_tabs::resize(window.app_handle());}
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
