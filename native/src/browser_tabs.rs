//! Remote content lives in child WebViews, outside the local UI's IPC capability.
use crate::{policy, Host};
use serde::Serialize;
use serde_json::{json, Value};
use std::{path::PathBuf, sync::Mutex};
use tauri::{Emitter, LogicalPosition, LogicalSize, Manager, WebviewBuilder, WebviewUrl};
const TOP: f64 = 84.;
#[derive(Clone, Serialize)]
pub struct Tab {
    id: String,
    title: String,
    url: String,
    loading: bool,
}
#[derive(Default)]
pub struct Tabs {
    pub state: Mutex<State>,
    pub gate: tokio::sync::Mutex<()>,
}
#[derive(Default)]
pub struct State {
    tabs: Vec<Tab>,
    active: Option<String>,
    owner: String,
    serial: u64,
}
fn failure() -> String {
    "网页标签操作未完成，请重试。".into()
}
pub fn web_url(raw: &str) -> Result<url::Url, String> {
    let u = policy::external_url(raw)?;
    let h = u.host_str().unwrap_or("");
    if u.port_or_known_default() != Some(443)
        || !h.contains('.')
        || h == "tauri.localhost"
        || h.ends_with(".localhost")
        || h.ends_with(".local")
        || h.parse::<std::net::IpAddr>().is_ok()
    {
        return Err("此标签仅支持公共 HTTPS 网站。".into());
    }
    Ok(u)
}
pub fn emit(app: &tauri::AppHandle) {
    let h = app.state::<std::sync::Arc<Host>>();
    if let Ok(s) = h.tabs.state.lock() {
        let _ = app.emit_to(
            "main",
            "browser-tabs",
            json!({"tabs":s.tabs,"active":s.active}),
        );
    };
}
fn update(
    app: &tauri::AppHandle,
    id: &str,
    title: Option<String>,
    url: Option<String>,
    loading: Option<bool>,
) {
    let h = app.state::<std::sync::Arc<Host>>();
    if let Ok(mut s) = h.tabs.state.lock() {
        if let Some(t) = s.tabs.iter_mut().find(|t| t.id == id) {
            if let Some(v) = title {
                t.title = v.chars().take(100).collect();
            }
            if let Some(v) = url {
                t.url = v;
            }
            if let Some(v) = loading {
                t.loading = v;
            }
        }
    }
    emit(app);
}
pub fn clear(app: &tauri::AppHandle, erase: bool) {
    let h = app.state::<std::sync::Arc<Host>>();
    let ids = if let Ok(mut s) = h.tabs.state.lock() {
        s.active = None;
        s.owner.clear();
        s.tabs.drain(..).map(|t| t.id).collect::<Vec<_>>()
    } else {
        vec![]
    };
    for id in ids {
        if let Some(w) = app.get_webview(&id) {
            if erase {
                let _ = w.clear_all_browsing_data();
            }
            let _ = w.close();
        }
    }
    if erase {
        if let Ok(cache) = app.path().app_cache_dir() {
            let root = cache.join("school-browser").join(scope(&h.data_dir));
            let paths = std::fs::read_dir(&root)
                .into_iter()
                .flatten()
                .filter_map(Result::ok)
                .filter(|e| {
                    e.file_name()
                        .to_str()
                        .is_some_and(|n| n.len() == 64 && n.bytes().all(|b| b.is_ascii_hexdigit()))
                })
                .map(|e| e.path())
                .collect::<Vec<_>>();
            let host = h.inner().clone();
            tauri::async_runtime::spawn_blocking(move || {
                for path in paths {
                    if path
                        .canonicalize()
                        .ok()
                        .and_then(|p| p.parent().map(std::path::Path::to_owned))
                        != root.canonicalize().ok()
                    {
                        host.log("browser_cache_clear_failed");
                        continue;
                    }
                    let mut ok = false;
                    for _ in 0..10 {
                        if !path.exists() || std::fs::remove_dir_all(&path).is_ok() {
                            ok = true;
                            break;
                        }
                        std::thread::sleep(std::time::Duration::from_secs(1));
                    }
                    if !ok {
                        host.log("browser_cache_clear_failed");
                    }
                }
            });
        }
    }
    emit(app);
}
pub fn resize(app: &tauri::AppHandle) {
    let Some(win) = app.get_window("main") else {
        return;
    };
    let Ok(size) = win.inner_size() else { return };
    let Ok(scale) = win.scale_factor() else {
        return;
    };
    for w in win.webviews() {
        if w.label().starts_with("site-") {
            let _ = w.set_position(LogicalPosition::new(0., TOP));
            let _ = w.set_size(LogicalSize::new(
                size.width as f64 / scale,
                (size.height as f64 / scale - TOP).max(1.),
            ));
        }
    }
}
fn activate(app: &tauri::AppHandle, id: Option<String>) -> Result<(), String> {
    let h = app.state::<std::sync::Arc<Host>>();
    let ids = {
        let mut s = h.tabs.state.lock().map_err(|_| failure())?;
        if id
            .as_ref()
            .is_some_and(|id| !s.tabs.iter().any(|t| &t.id == id))
        {
            return Err(failure());
        }
        s.active = id.clone();
        s.tabs.iter().map(|t| t.id.clone()).collect::<Vec<_>>()
    };
    for key in ids {
        if let Some(w) = app.get_webview(&key) {
            if Some(&key) == id.as_ref() {
                w.show().map_err(|_| failure())?;
                let _ = w.set_focus();
            } else {
                w.hide().map_err(|_| failure())?;
            }
        }
    }
    resize(app);
    emit(app);
    Ok(())
}
fn cookie_value(c: &Value) -> Option<cookie::Cookie<'static>> {
    let mut v = cookie::Cookie::new(
        c["name"].as_str()?.to_owned(),
        c["value"].as_str()?.to_owned(),
    );
    v.set_domain(c["domain"].as_str()?.to_owned());
    v.set_path(c["path"].as_str().unwrap_or("/").to_owned());
    v.set_secure(c["secure"].as_bool().unwrap_or(true));
    v.set_http_only(c["httpOnly"].as_bool().unwrap_or(false));
    v.set_same_site(match c["sameSite"].as_str() {
        Some("Strict") => cookie::SameSite::Strict,
        Some("None") => cookie::SameSite::None,
        _ => cookie::SameSite::Lax,
    });
    if let Some(exp) = c["expires"].as_f64().filter(|n| *n > 0.) {
        let when = cookie::time::OffsetDateTime::from_unix_timestamp(exp as i64).ok()?;
        if when <= cookie::time::OffsetDateTime::now_utc() {
            return None;
        }
        v.set_expires(when);
    }
    Some(v)
}
// The import receipt must have the same lifetime as WebView2 session cookies.
// A disk receipt survives the last WebView closing, while the school login
// cookies do not. Use an HttpOnly session marker on a separate, offline origin.
// Within the live browser session, opening another tab preserves website state
// (including logout) instead of replaying the saved school cookies.
const IMPORT_DOMAIN: &str = "session.schoolagent.invalid";
fn import_marker_name(host: &str) -> String {
    format!("school_import_{}", host.replace('.', "_"))
}
fn needs_session_import(cookies: &[cookie::Cookie<'_>], host: &str, revision: &str) -> bool {
    let name = import_marker_name(host);
    !cookies.iter().any(|c| {
        c.domain().unwrap_or("").trim_start_matches('.') == IMPORT_DOMAIN
            && c.name() == name && c.value() == revision
    })
}
fn session_marker(host: &str, revision: &str) -> cookie::Cookie<'static> {
    let mut c = cookie::Cookie::new(import_marker_name(host), revision.to_owned());
    c.set_domain(IMPORT_DOMAIN);
    c.set_path("/");
    c.set_secure(true);
    c.set_http_only(true);
    c.set_same_site(cookie::SameSite::Strict);
    c
}
pub fn open(
    app: tauri::AppHandle,
    raw: String,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), String>> + Send>> {
    open_with_activation(app, raw, true)
}
pub fn open_with_activation(
    app: tauri::AppHandle,
    raw: String,
    foreground: bool,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), String>> + Send>> {
    Box::pin(async move {
        let url = web_url(&raw)?;
        let h = app.state::<std::sync::Arc<Host>>().inner().clone();
        let _gate = h.tabs.gate.lock().await;
        let session = h
            .backend()
            .await?
            .browser_session(url.host_str().unwrap())
            .await?;
        let owner = session["owner"]
            .as_str()
            .filter(|v| v.len() == 64 && v.bytes().all(|b| b.is_ascii_hexdigit()))
            .ok_or_else(failure)?
            .to_owned();
        let changed = h.tabs.state.lock().map_err(|_| failure())?.owner != owner;
        if changed {
            clear(&app, false);
        }
        let id = {
            let mut s = h.tabs.state.lock().map_err(|_| failure())?;
            if s.tabs.len() >= 10 {
                return Err("最多保留 10 个网页标签，请先关闭不再使用的网页。".into());
            }
            s.owner = owner.clone();
            s.serial += 1;
            format!("site-{}", s.serial)
        };
        let dir = app
            .path()
            .app_cache_dir()
            .map_err(|_| failure())?
            .join("school-browser")
            .join(scope(&h.data_dir))
            .join(&owner);
        std::fs::create_dir_all(&dir).map_err(|_| failure())?;
        let revision = session["revision"].as_str().ok_or_else(failure)?;
        // Only origin-owned localStorage is injected; no main-UI credential crosses here.
        // Preserve localStorage changes made by the website after the initial import.
        let origins = session["state"]["origins"].clone();
        let script=format!("(()=>{{if(window.top!==window)return;const all={origins};for(const o of all){{if(o.origin!==location.origin)continue;const k='__school_import_{revision}';if(localStorage.getItem(k))continue;for(const v of o.localStorage||[]){{if(typeof v.name==='string'&&typeof v.value==='string')localStorage.setItem(v.name,v.value);}}localStorage.setItem(k,'1');}}}})();");
        let nav_app = app.clone();
        let nav_id = id.clone();
        let title_app = app.clone();
        let title_id = id.clone();
        let load_app = app.clone();
        let load_id = id.clone();
        let popup_app = app.clone();
        let builder =
            WebviewBuilder::new(&id, WebviewUrl::External("about:blank".parse().unwrap()))
                .data_directory(dir)
                .initialization_script(script)
                .on_navigation(move |u| {
                    let allowed = u.as_str() == "about:blank" || web_url(u.as_str()).is_ok();
                    if allowed && u.as_str() != "about:blank" {
                        update(&nav_app, &nav_id, None, Some(u.to_string()), Some(true));
                    }
                    allowed
                })
                .on_document_title_changed(move |_, title| {
                    if title != "about:blank" {
                        update(&title_app, &title_id, Some(title), None, None);
                    }
                })
                .on_page_load(move |_, event| {
                    if matches!(event.event(), tauri::webview::PageLoadEvent::Finished) {
                        update(&load_app, &load_id, None, None, Some(false));
                    }
                })
                .on_new_window(move |u, _| {
                    let a = popup_app.clone();
                    if web_url(u.as_str()).is_ok() {
                        tauri::async_runtime::spawn(async move {
                            if let Err(e) = open(a.clone(), u.to_string()).await {
                                let _ = a.emit_to("main", "native-notice", e);
                            }
                        });
                    }
                    tauri::webview::NewWindowResponse::Deny
                });
        #[cfg(debug_assertions)]
        let builder = if let Ok(port) = std::env::var("SCHOOL_BROWSER_TEST_CDP")
            .and_then(|v| v.parse::<u16>().map_err(|_| std::env::VarError::NotPresent))
        {
            builder.additional_browser_args(&format!("--remote-debugging-port={port}"))
        } else {
            builder
        };
        let win = app.get_window("main").ok_or_else(failure)?;
        let size = win.inner_size().map_err(|_| failure())?;
        let scale = win.scale_factor().map_err(|_| failure())?;
        let child = win
            .add_child(
                builder,
                LogicalPosition::new(0., TOP),
                LogicalSize::new(
                    size.width as f64 / scale,
                    (size.height as f64 / scale - TOP).max(1.),
                ),
            )
            .map_err(|_| failure())?;
        let _ = child.hide();
        let prepare = (|| -> Result<(), String> {
            let cookies = child.cookies().map_err(|_| failure())?;
            if needs_session_import(&cookies, url.host_str().unwrap(), revision) {
                for c in session["state"]["cookies"].as_array().into_iter().flatten() {
                    if let Some(v) = cookie_value(c) {
                        child.set_cookie(v).map_err(|_| failure())?;
                    }
                }
                child.set_cookie(session_marker(url.host_str().unwrap(), revision)).map_err(|_| failure())?;
            }
            Ok(())
        })();
        if let Err(e) = prepare {
            let _ = child.close();
            return Err(e);
        }
        // Recheck account after asynchronous construction, before exposing a cached page.
        let still = h
            .backend()
            .await?
            .browser_session(url.host_str().unwrap())
            .await;
        if !still.as_ref().is_ok_and(|s| s["owner"] == owner) {
            let _ = child.close();
            return Err("账号已变化，请重新打开链接。".into());
        }
        h.tabs.state.lock().map_err(|_| failure())?.tabs.push(Tab {
            id: id.clone(),
            title: url.host_str().unwrap().into(),
            url: url.to_string(),
            loading: true,
        });
        child.navigate(url).map_err(|_| failure())?;
        if foreground {
            activate(&app, Some(id))
        } else {
            emit(&app);
            Ok(())
        }
    })
}
fn scope(path: &PathBuf) -> String {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    path.hash(&mut h);
    format!("{:x}", h.finish())
}
pub async fn action(
    app: tauri::AppHandle,
    action: String,
    id: Option<String>,
) -> Result<(), String> {
    let h = app.state::<std::sync::Arc<Host>>().inner().clone();
    let _gate = h.tabs.gate.lock().await;
    if action == "chat" {
        return activate(&app, None);
    }
    if action == "reset" {
        clear(&app, false);
        return Ok(());
    }
    if action == "state" {
        emit(&app);
        return Ok(());
    }
    let id = id.ok_or_else(failure)?;
    if !h
        .tabs
        .state
        .lock()
        .map_err(|_| failure())?
        .tabs
        .iter()
        .any(|t| t.id == id)
    {
        return Err(failure());
    }
    if h.backend()
        .await?
        .browser_session("www.teach.ustc.edu.cn")
        .await
        .is_err()
    {
        clear(&app, false);
        return Err("请先解锁应用。".into());
    }
    match action.as_str() {
        "select" => activate(&app, Some(id)),
        "close" => {
            if let Some(w) = app.get_webview(&id) {
                w.close().map_err(|_| failure())?;
            }
            let next = {
                let mut s = h.tabs.state.lock().map_err(|_| failure())?;
                s.tabs.retain(|t| t.id != id);
                if s.active.as_ref() == Some(&id) {
                    None
                } else {
                    s.active.clone()
                }
            };
            activate(&app, next)
        }
        "reload" | "back" | "forward" => {
            let w = app.get_webview(&id).ok_or_else(failure)?;
            w.eval(match action.as_str() {
                "reload" => "location.reload()",
                "back" => "history.back()",
                _ => "history.forward()",
            })
            .map_err(|_| failure())
        }
        _ => Err(failure()),
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn import_follows_browser_session_and_saved_revision() {
        let marker = session_marker("www.bb.ustc.edu.cn", "rev1");
        assert!(marker.expires().is_none());
        assert_eq!(marker.http_only(), Some(true));
        assert_eq!(marker.secure(), Some(true));
        let live = vec![marker];
        // A website logout may remove its auth cookies; it does not remove
        // our separate-origin marker, so opening another tab keeps it logged out.
        assert!(!needs_session_import(&live, "www.bb.ustc.edu.cn", "rev1"));
        assert!(needs_session_import(&[], "www.bb.ustc.edu.cn", "rev1"));
        assert!(needs_session_import(&live, "www.bb.ustc.edu.cn", "rev2"));
        assert!(needs_session_import(&live, "jw.ustc.edu.cn", "rev1"));
        let forged = cookie::Cookie::build((import_marker_name("www.bb.ustc.edu.cn"), "rev1"))
            .domain("www.bb.ustc.edu.cn").build();
        assert!(needs_session_import(&[forged], "www.bb.ustc.edu.cn", "rev1"));
    }
    #[test]
    fn remote_navigation_cannot_reach_local_ui() {
        for u in [
            "http://tauri.localhost",
            "https://localhost/a",
            "https://127.0.0.1",
            "file:///C:/test",
            "javascript:alert(1)",
            "https://tauri.localhost",
            "https://www.ustc.edu.cn:8080",
        ] {
            assert!(web_url(u).is_err());
        }
        assert!(web_url("https://jw.ustc.edu.cn/home").is_ok());
    }
}
