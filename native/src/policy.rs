use url::Url;

pub const MAX_UPLOAD: usize = 50 * 1024 * 1024;
pub const MAX_JSON: usize = 200_000;
pub const MAX_RESPONSE: usize = 64 * 1024 * 1024;

pub fn validate_route(method: &str, route: &str, size: usize) -> Result<(), String> {
    if route.len() > 2048 || route.contains(['\\', '#', '\r', '\n']) {
        return Err("请求地址无效。".into());
    }
    let (path, query) = route.split_once('?').unwrap_or((route, ""));
    let allowed = match method {
        "GET" => matches!(path, "/api/status" | "/api/diagnostics" | "/api/diagnostics/export"
            | "/api/campus/sites" | "/api/campus/profile" | "/api/campus/timetable"
            | "/api/digest" | "/api/artifact" | "/api/chats" | "/api/chat" | "/api/tasks"),
        "POST" => matches!(path, "/api/upload" | "/api/shutdown" | "/api/campus/profile"
            | "/api/campus/timetable/refresh" | "/api/digest/plan" | "/api/digest/preview-report"
            | "/api/digest/configure" | "/api/digest/refresh" | "/api/digest/read"
            | "/api/digest/courses" | "/api/digest/calendar" | "/api/digest/summarize"
            | "/api/diagnostics/clear" | "/api/diagnostics/ui-error" | "/api/login"
            | "/api/model/save" | "/api/model/test" | "/api/model/connect" | "/api/model/models"
            | "/api/model/school-keys" | "/api/model/school-connect" | "/api/connect"
            | "/api/email-verification" | "/api/chat/send" | "/api/chat/approve"
            | "/api/stop" | "/api/clear"),
        _ => false,
    };
    if !allowed || (method == "POST" && route.contains('?')) || (method == "GET" && size != 0) {
        return Err("不支持此本机操作。".into());
    }
    let keys: &[&str] = match path {
        "/api/artifact" => &["chat", "id"], "/api/chat" => &["id"],
        "/api/tasks" => &["offset"], _ => &[],
    };
    let mut seen = std::collections::HashSet::new();
    for (key, _) in url::form_urlencoded::parse(query.as_bytes()) {
        if !keys.contains(&key.as_ref()) || !seen.insert(key.into_owned()) {
            return Err("请求参数无效。".into());
        }
    }
    if size > if path == "/api/upload" { MAX_UPLOAD } else { MAX_JSON } {
        return Err("请求大小超过限制。".into());
    }
    Ok(())
}

pub fn validate_origin(origin: &str, token: &str) -> Result<(), String> {
    let url = Url::parse(origin).map_err(|_| "本机服务启动失败。")?;
    if url.scheme() != "http" || url.host_str() != Some("127.0.0.1")
        || url.port().is_none() || url.port() == Some(0) || !url.username().is_empty()
        || url.password().is_some() || url.path() != "/" || url.query().is_some()
        || url.fragment().is_some() || !(32..=128).contains(&token.len())
        || !token.bytes().all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c)) {
        return Err("本机服务返回了无效入口。".into());
    }
    Ok(())
}

pub fn external_url(value: &str) -> Result<Url, String> {
    let url = Url::parse(value).map_err(|_| "链接无效。")?;
    if value.len() > 8192 || url.scheme() != "https" || url.host_str().is_none()
        || !url.username().is_empty() || url.password().is_some() {
        return Err("仅支持无账号信息的 HTTPS 网站链接。".into());
    }
    Ok(url)
}

pub fn local_ui(url: &Url) -> bool {
    (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost") && url.port().is_none())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn routes_do_not_become_a_generic_http_or_file_proxy() {
        for path in ["https://example.com/api/status", "//example.com", "/api/../status", "/api/%2e%2e/status", "/api/status#x", "/api/status?url=https://example.com", "/api/status\\x"] {
            assert!(validate_route("GET", path, 0).is_err(), "{path}");
        }
        assert!(validate_route("GET", "/api/status", 1).is_err());
        assert!(validate_route("POST", "/api/login?x=1", 2).is_err());
        assert!(validate_route("DELETE", "/api/status", 0).is_err());
        assert!(validate_route("GET", "/api/chat?id=a&id=b", 0).is_err());
        assert!(validate_route("GET", "/api/artifact?chat=a&id=b", 0).is_ok());
        assert!(validate_route("POST", "/api/upload", MAX_UPLOAD).is_ok());
        assert!(validate_route("POST", "/api/upload", MAX_UPLOAD + 1).is_err());
        assert!(validate_route("POST", "/api/chat/send", MAX_JSON + 1).is_err());
    }
    #[test]
    fn handshake_and_navigation_stay_local() {
        let token = "a".repeat(43);
        assert!(validate_origin("http://127.0.0.1:3718", &token).is_ok());
        for origin in ["http://localhost:3718", "https://127.0.0.1:3718", "http://127.0.0.1:3718/a", "http://user@127.0.0.1:3718", "http://127.0.0.1:3718?x"] {
            assert!(validate_origin(origin, &token).is_err());
        }
        assert!(validate_origin("http://127.0.0.1:3718", "short").is_err());
        assert!(local_ui(&Url::parse("http://tauri.localhost/").unwrap()));
        assert!(!local_ui(&Url::parse("https://tauri.localhost/").unwrap()));
        for value in ["file:///C:/Windows", "javascript:alert(1)", "http://example.com", "https://u:p@example.com"] {
            assert!(external_url(value).is_err());
        }
        assert!(external_url("https://jw.ustc.edu.cn/home").is_ok());
    }
}
