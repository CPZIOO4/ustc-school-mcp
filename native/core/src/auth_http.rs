//! Authenticated, bounded school reads. A cookie jar lives for one request chain only.
use crate::{network, sessions, Error, Result};
use reqwest::cookie::{CookieStore, Jar};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::{collections::BTreeMap, path::Path, time::Duration};
use url::Url;

const LIMIT: usize = 5 * 1024 * 1024;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    service: String,
    path: String,
    method: String,
    #[serde(default)]
    params: Map<String, Value>,
    cookies: Vec<Value>,
    user_agent: String,
    database: String,
}

fn base(service: &str) -> Result<&'static str> {
    match service {
        "jw" => Ok("https://jw.ustc.edu.cn"),
        "bb" => Ok("https://www.bb.ustc.edu.cn"),
        _ => Err(Error::invalid()),
    }
}
fn school_host(service: &str, host: &str) -> bool {
    match service {
        "jw" => host == "jw.ustc.edu.cn",
        "bb" => matches!(host, "bb.ustc.edu.cn" | "www.bb.ustc.edu.cn"),
        _ => false,
    }
}
fn plain(value: &str, maximum: usize) -> bool {
    value.len() <= maximum && !value.chars().any(char::is_control)
}
fn matches(pattern: &str, value: &str) -> bool {
    regex::Regex::new(pattern)
        .expect("fixed regex")
        .is_match(value)
}
fn query(url: &Url) -> Result<BTreeMap<String, String>> {
    let mut result = BTreeMap::new();
    for (key, value) in url.query_pairs() {
        if !plain(&key, 80)
            || !plain(&value, 1000)
            || result
                .insert(key.into_owned(), value.into_owned())
                .is_some()
        {
            return Err(Error::invalid());
        }
    }
    Ok(result)
}
fn validate(service: &str, method: &str, url: &Url, form: &BTreeMap<String, String>) -> Result<()> {
    if url.scheme() != "https"
        || !school_host(service, url.host_str().unwrap_or(""))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port_or_known_default() != Some(443)
        || url.fragment().is_some()
    {
        return Err(Error::invalid());
    }
    let path = url.path();
    let q = query(url)?;
    let valid = if service == "jw" {
        if method == "POST" {
            path == "/ws/for-std/course-select/open-turns"
                && q.is_empty()
                && form.len() == 2
                && ["studentId", "bizTypeId"]
                    .iter()
                    .all(|k| form.get(*k).is_some_and(|v| matches(r"^[0-9]{1,16}$", v)))
        } else if method == "GET" && form.is_empty() {
            let read = matches!(
                path,
                "/" | "/home"
                    | "/home/menu"
                    | "/for-std/course-table"
                    | "/for-std/course-table/get-data"
                    | "/for-std/grade/sheet"
                    | "/for-std/grade/sheet/getSemesters"
                    | "/for-std/grade/sheet/getGradeSheetTypes"
                    | "/for-std/grade/sheet/getGradeList"
            ) || matches(
                r"^/for-std/course-table/(?:info/[0-9]+|semester/[0-9]+/print-data/[0-9]+)$",
                path,
            );
            let entry = matches(
                r"^/for-std/(lesson-search|course-select)(?:/(?:index|turns)/[0-9]+)?$",
                path,
            );
            let search = matches(
                r"^/for-std/lesson-search/semester/[0-9]+/search/[0-9]+$",
                path,
            );
            (read || entry || search)
                && q.iter().all(|(key, value)| {
                    if search {
                        match key.as_str() {
                            "queryPage__" => value.split_once(',').is_some_and(|(page, limit)| {
                                matches(r"^[0-9]{1,3}$", page)
                                    && matches(r"^[0-9]{1,2}$", limit)
                                    && page.parse::<u32>().is_ok_and(|n| (1..=100).contains(&n))
                                    && limit.parse::<u32>().is_ok_and(|n| (1..=50).contains(&n))
                            }),
                            "courseNameZhLike" | "courseCodeLike" | "teacherNameLike" => {
                                value.chars().count() <= 200
                            }
                            _ => false,
                        }
                    } else {
                        let allowed = match path {
                            "/for-std/course-table/get-data" => {
                                matches!(key.as_str(), "bizTypeId" | "semesterId" | "dataId")
                            }
                            "/for-std/grade/sheet/getGradeList" => {
                                matches!(key.as_str(), "trainTypeId" | "semesterIds")
                            }
                            _ => path.contains("/print-data/") && key == "weekIndex",
                        };
                        allowed
                            && (matches(r"^[0-9]+(?:,[0-9]+)*$", value)
                                || (matches!(key.as_str(), "semesterIds" | "weekIndex")
                                    && value.is_empty()))
                    }
                })
        } else {
            false
        }
    } else if service == "bb" && method == "GET" && form.is_empty() {
        let read = matches!(
            path,
            "/webapps/portal/execute/tabs/tabAction"
                | "/webapps/blackboard/execute/launcher"
                | "/webapps/blackboard/content/listContent.jsp"
                | "/webapps/blackboard/content/launchLink.jsp"
                | "/webapps/blackboard/execute/announcement"
                | "/webapps/blackboard/execute/courseMain"
                | "/webapps/blackboard/execute/modulepage/view"
                | "/webapps/blackboard/execute/displayLearningUnit"
                | "/webapps/blackboard/execute/content/blankPage"
        );
        read && q.iter().all(|(key, value)| match key.as_str() {
            "mode" | "method" | "action" | "editMode" | "displayMode" => {
                matches!(
                    value.as_str(),
                    "view"
                        | "list"
                        | "listAnnouncements"
                        | "display"
                        | "false"
                        | "false_"
                        | "read"
                        | "Full"
                        | ""
                ) || (key == "method"
                    && path == "/webapps/blackboard/execute/announcement"
                    && value == "search")
            }
            "type" => value == "Course",
            "url" => value.is_empty(),
            "tool_type" => value == "TOOL",
            "course_id" | "id" | "content_id" | "toc_id" | "tool_id" | "tab_tab_group_id"
            | "tab_id" | "tabId" | "courseTocLabel" | "cmp_tab_id" | "context" | "navItem"
            | "viewChoice" | "handle" => true,
            _ => false,
        })
    } else {
        false
    };
    if valid {
        Ok(())
    } else {
        Err(Error::invalid())
    }
}

fn planned(input: &Input) -> Result<(Url, BTreeMap<String, String>)> {
    let origin = base(&input.service)?;
    if !Path::new(&input.database).is_absolute()
        || !plain(&input.path, 8192)
        || input.path.contains('\\')
        || input.path.starts_with("//")
        || !plain(&input.user_agent, 512)
        || input.cookies.len() > 5000
        || !(input.path.starts_with('/') || input.path.starts_with("https://"))
    {
        return Err(Error::invalid());
    }
    let mut url = Url::parse(origin)
        .unwrap()
        .join(&input.path)
        .map_err(|_| Error::invalid())?;
    let mut seen = query(&url)?;
    let mut form = BTreeMap::new();
    for (key, value) in &input.params {
        let value = if let Some(s) = value.as_str() {
            s.to_owned()
        } else if value.is_i64() || value.is_u64() {
            value.to_string()
        } else {
            return Err(Error::invalid());
        };
        if !plain(key, 80)
            || !plain(&value, 1000)
            || seen.insert(key.clone(), value.clone()).is_some()
        {
            return Err(Error::invalid());
        }
        if input.method == "GET" {
            url.query_pairs_mut().append_pair(key, &value);
        } else {
            form.insert(key.clone(), value);
        }
    }
    validate(&input.service, &input.method, &url, &form)?;
    Ok((url, form))
}

fn cookie_jar(input: &Input, url: &Url) -> Result<Jar> {
    let filtered = sessions::dispatch(
        "sessions_filter",
        &json!({"profile":input.service,
        "state":{"cookies":input.cookies,"origins":[]}}),
    )?;
    let jar = Jar::default();
    for c in filtered["cookies"].as_array().ok_or_else(Error::invalid)? {
        let name = c["name"].as_str().ok_or_else(Error::invalid)?;
        let value = c["value"].as_str().ok_or_else(Error::invalid)?;
        let domain = c["domain"].as_str().ok_or_else(Error::invalid)?;
        let path = c["path"].as_str().unwrap_or("/");
        if !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
            || value
                .bytes()
                .any(|b| !(0x21..0x7f).contains(&b) || b"\";,\\".contains(&b))
            || path.contains(';')
        {
            return Err(Error::invalid());
        }
        let cookie_url = if school_host(&input.service, domain.trim_start_matches('.')) {
            Url::parse(&format!("https://{}/", domain.trim_start_matches('.')))
                .map_err(|_| Error::invalid())?
        } else {
            url.clone()
        };
        let host = cookie_url.host_str().unwrap_or("");
        let d = domain.trim_start_matches('.');
        if host != d && !(domain.starts_with('.') && host.ends_with(&format!(".{d}"))) {
            continue;
        }
        // Host-only cookies omit Domain; a leading dot marks domain cookies in Playwright.
        let mut cookie = format!("{name}={value}; Path={path}");
        if domain.starts_with('.') {
            cookie.push_str(&format!("; Domain={d}"));
        }
        if c["secure"].as_bool().unwrap_or(false) {
            cookie.push_str("; Secure");
        }
        if let Some(expires) = c["expires"].as_f64().filter(|v| *v > 0.0) {
            // Preserve expiry during a redirect chain as well as at initial filtering.
            let seconds = expires.min(253_402_300_799.0).floor() as u64;
            let when = std::time::UNIX_EPOCH + Duration::from_secs(seconds);
            cookie.push_str(&format!("; Expires={}", httpdate::fmt_http_date(when)));
        }
        jar.add_cookie_str(&cookie, &cookie_url);
    }
    Ok(jar)
}

fn needs_login(url: &Url) -> bool {
    matches!(
        url.host_str(),
        Some("id.ustc.edu.cn" | "passport.ustc.edu.cn")
    ) || (url.host_str() == Some("jw.ustc.edu.cn")
        && matches!(url.path(), "/login" | "/ucas-sso/login"))
        || (school_host("bb", url.host_str().unwrap_or(""))
            && url.path().starts_with("/webapps/login"))
        || (school_host("bb", url.host_str().unwrap_or(""))
            && url.path().starts_with("/nginx_auth"))
}
fn login_html(service: &str, text: &str) -> bool {
    let html = scraper::Html::parse_document(text);
    let selector =
        scraper::Selector::parse("input[type=password], a[href='/ucas-sso/login']").unwrap();
    html.select(&selector).next().is_some()
        || (service == "bb"
            && html
                .select(
                    &scraper::Selector::parse("form#loginForm, form[action*='/webapps/login']")
                        .unwrap(),
                )
                .next()
                .is_some())
}
fn error(code: &str, message: &str) -> Value {
    json!({"state":"error","code":code,"message":message})
}
fn record(input: &Input, action: &str) -> Result<Value> {
    network::policy(
        &json!({"action":action,"database":input.database,"service":input.service,"now":network::now(),"jitter":0.5}),
    )
}
fn authentication(input: &Input) -> Result<Value> {
    network::policy(
        &json!({"action":"failure","database":input.database,"service":input.service,"now":network::now(),"authentication":true}),
    )?;
    Ok(error(
        "authentication_required",
        "学校登录会话已失效，请先重新认证。",
    ))
}

pub struct Client {
    client: reqwest::Client,
}
impl Client {
    pub fn new() -> Result<Self> {
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .connect_timeout(Duration::from_secs(10))
            .pool_idle_timeout(Duration::from_secs(60))
            .pool_max_idle_per_host(2)
            .build()
            .map_err(|_| Error::new("network_unavailable", "无法初始化学校连接。"))?;
        Ok(Self { client })
    }
    pub async fn request(&self, value: &Value) -> Result<Value> {
        let input: Input = serde_json::from_value(value.clone()).map_err(|_| Error::invalid())?;
        let (url, form) = planned(&input)?;
        let jar = cookie_jar(&input, &url)?;
        let reservation = record(&input, "acquire")?;
        if reservation["state"] != "acquired" {
            return Ok(reservation);
        }
        match tokio::time::timeout(
            Duration::from_secs(100),
            self.fetch(&input, url, &form, &jar),
        )
        .await
        {
            Ok(result) => result,
            Err(_) => {
                record(&input, "failure")?;
                Ok(error(
                    "timeout",
                    "学校查询超时，本轮已停止，没有自动重试。",
                ))
            }
        }
    }
    async fn fetch(
        &self,
        input: &Input,
        mut url: Url,
        form: &BTreeMap<String, String>,
        jar: &Jar,
    ) -> Result<Value> {
        let hops = if input.service == "bb" { 8 } else { 5 };
        for hop in 0..hops {
            // Subsequent reservations are held inside this chain: a returned wait must
            // never cause the bridge to replay a request that has already been sent.
            if hop > 0 {
                loop {
                    let state = record(input, "acquire")?;
                    if state["state"] == "acquired" {
                        break;
                    }
                    if state["state"] != "wait" {
                        return Ok(state);
                    }
                    let delay = state["retry_after_seconds"]
                        .as_f64()
                        .ok_or_else(Error::invalid)?;
                    tokio::time::sleep(Duration::from_secs_f64(delay.min(10.0))).await;
                }
            }
            let mut request = if input.method == "POST" {
                self.client.post(url.clone()).form(form)
            } else {
                self.client.get(url.clone())
            };
            request = request
                .header("User-Agent", &input.user_agent)
                .header("Accept", "text/html, application/json")
                .header("Accept-Language", "zh-CN")
                .timeout(Duration::from_secs(20));
            if let Some(cookie) = jar.cookies(&url) {
                request = request.header(reqwest::header::COOKIE, cookie);
            }
            let mut response = match request.send().await {
                Ok(r) => r,
                Err(e) => {
                    record(input, "failure")?;
                    return Ok(error(
                        if e.is_timeout() {
                            "timeout"
                        } else {
                            "network_unavailable"
                        },
                        "学校网络请求失败，本轮已停止，没有自动重试。",
                    ));
                }
            };
            let status = response.status().as_u16();
            let state = network::policy(
                &json!({"action":"response","database":input.database,"service":input.service,
                "now":network::now(),"status":status,"retry_after":response.headers().get("retry-after").and_then(|v|v.to_str().ok()),"jitter":0.5}),
            )?;
            if state["state"] == "cooldown" {
                return Ok(state);
            }
            if status == 401 {
                return Ok(error(
                    "authentication_required",
                    "学校登录会话已失效，请先重新认证。",
                ));
            }
            if status == 403 {
                return Ok(error("access_denied", "学校拒绝访问当前内容。"));
            }
            jar.set_cookies(
                &mut response
                    .headers()
                    .get_all(reqwest::header::SET_COOKIE)
                    .iter(),
                &url,
            );
            if (300..400).contains(&status) {
                let target = response
                    .headers()
                    .get("location")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|s| {
                        if plain(s, 8192) && !s.contains('\\') {
                            url.join(s).ok()
                        } else {
                            None
                        }
                    });
                if let Some(target) = target {
                    if needs_login(&target) {
                        return authentication(input);
                    }
                    if input.method == "GET"
                        && validate(&input.service, "GET", &target, &BTreeMap::new()).is_ok()
                    {
                        url = target;
                        continue;
                    }
                }
                record(input, "failure")?;
                return Ok(error(
                    "unavailable",
                    "学校页面跳转超出已适配的查询范围，已停止。",
                ));
            }
            if status != 200 {
                record(input, "failure")?;
                return Ok(error(
                    "service_unavailable",
                    "学校查询返回异常状态，已停止。",
                ));
            }
            let content_type = response
                .headers()
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .to_owned();
            let media = content_type
                .split(';')
                .next()
                .unwrap_or("")
                .trim()
                .to_ascii_lowercase();
            if media != "text/html" && !(input.service == "jw" && media == "application/json") {
                record(input, "failure")?;
                return Ok(error("unavailable", "学校响应格式不支持。"));
            }
            let mut bytes = Vec::new();
            loop {
                match response.chunk().await {
                    Ok(Some(chunk)) if bytes.len() + chunk.len() <= LIMIT => {
                        bytes.extend_from_slice(&chunk)
                    }
                    Ok(Some(_)) => {
                        record(input, "failure")?;
                        return Ok(error("unavailable", "学校响应超过读取大小限制。"));
                    }
                    Ok(None) => break,
                    Err(_) => {
                        record(input, "failure")?;
                        return Ok(error("network_unavailable", "学校响应未读取完整，已停止。"));
                    }
                }
            }
            let encoding = content_type
                .split(';')
                .skip(1)
                .filter_map(|p| p.trim().split_once('='))
                .find(|(k, _)| k.trim().eq_ignore_ascii_case("charset"))
                .and_then(|(_, v)| {
                    encoding_rs::Encoding::for_label(v.trim().trim_matches(['\'', '"']).as_bytes())
                })
                .unwrap_or(encoding_rs::UTF_8);
            let text = encoding.decode(&bytes).0.into_owned();
            if text.len() > LIMIT {
                record(input, "failure")?;
                return Ok(error("unavailable", "学校响应解码后超过大小限制。"));
            }
            if media == "text/html" && login_html(&input.service, &text) {
                return authentication(input);
            }
            let result = json!({"state":"response","status":status,"url":url.as_str(),
                "headers":{"content-type": format!("{media}; charset=utf-8")},"text":text});
            if serde_json::to_vec(&result)
                .map_err(|_| Error::invalid())?
                .len()
                > 15 * 1024 * 1024
            {
                record(input, "failure")?;
                return Ok(error("unavailable", "学校响应超过本机传输大小限制。"));
            }
            record(input, "success")?;
            return Ok(result);
        }
        record(input, "failure")?;
        Ok(error("unavailable", "学校页面跳转次数超过限制。"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn input() -> Input {
        serde_json::from_value(json!({"service":"jw","path":"/home","method":"GET",
            "params":{},"cookies":[],"user_agent":"synthetic-reader",
            "database":std::env::temp_dir().join(format!("school-auth-{}.sqlite3", uuid::Uuid::new_v4()))})).unwrap()
    }
    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }
    fn local_response(input: &Input, response: String) -> Value {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = Url::parse(&format!("http://{}/home", listener.local_addr().unwrap())).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let mut request = Vec::new();
            let mut bytes = [0; 1024];
            while !request.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = stream.read(&mut bytes).unwrap();
                if n == 0 {
                    break;
                }
                request.extend_from_slice(&bytes[..n]);
            }
            let _ = stream.write_all(response.as_bytes());
            String::from_utf8(request).unwrap()
        });
        // Only private unit tests can substitute loopback. Public dispatch always validates.
        let result = runtime()
            .block_on(
                Client::new()
                    .unwrap()
                    .fetch(input, url, &BTreeMap::new(), &Jar::default()),
            )
            .unwrap();
        let request = server.join().unwrap();
        assert!(request.starts_with("GET /home HTTP/1.1"));
        assert!(!request.to_lowercase().contains("authorization:"));
        let _ = std::fs::remove_file(&input.database);
        result
    }
    #[test]
    fn rejects_mutations_foreign_destinations_and_duplicate_parameters() {
        let mut p = input();
        for path in [
            "https://evil.example/home",
            "https://jw.ustc.edu.cn@evil.example/home",
            "//evil.example/home",
            "/for-std/course-select/drop",
            "/home?token=synthetic",
            "/home?x=1&x=2",
            "/home#fragment",
            "/home?x=%0a",
        ] {
            p.path = path.to_owned();
            assert!(planned(&p).is_err(), "{path}");
        }
        p.path = "/home".into();
        p.method = "POST".into();
        assert!(planned(&p).is_err());
        p.path = "/ws/for-std/course-select/open-turns".into();
        p.params = serde_json::from_value(json!({"studentId":"1","bizTypeId":2})).unwrap();
        assert!(planned(&p).is_ok());
        p.params.insert("action".into(), json!("delete"));
        assert!(planned(&p).is_err());
        p.method = "GET".into();
        p.path = "/for-std/lesson-search/semester/1/search/2".into();
        p.params =
            serde_json::from_value(json!({"queryPage__":"2,3", "courseNameZhLike":"合成课程"}))
                .unwrap();
        assert!(planned(&p).is_ok());
        for page in ["0,3", "101,3", "2,51", "1", "2,3,4"] {
            p.params.insert("queryPage__".into(), json!(page));
            assert!(planned(&p).is_err());
        }
    }
    #[test]
    fn accepts_only_observed_bb_reads() {
        let mut p = input();
        p.service = "bb".into();
        for path in ["/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1",
            "https://bb.ustc.edu.cn/webapps/blackboard/execute/announcement?method=search&course_id=_1_1"] {
            p.path=path.into(); assert!(planned(&p).is_ok());
        }
        for path in [
            "/webapps/assignment/uploadAssignment",
            "/webapps/blackboard/execute/announcement?method=delete",
            "/webapps/blackboard/execute/launcher?type=Course&url=https%3A%2F%2Fevil.example",
        ] {
            p.path = path.into();
            assert!(planned(&p).is_err());
        }
    }
    #[test]
    fn cookies_obey_domain_path_expiry_and_do_not_cross_accounts() {
        let mut p = input();
        p.service = "bb".into();
        p.path = "/webapps/portal/execute/tabs/tabAction".into();
        p.cookies = vec![
            json!({"name":"own","value":"synthetic-one","domain":"www.bb.ustc.edu.cn","path":"/"}),
            json!({"name":"alias","value":"synthetic-two","domain":"bb.ustc.edu.cn","path":"/"}),
            json!({"name":"wrong_path","value":"synthetic-three","domain":"www.bb.ustc.edu.cn","path":"/private"}),
            json!({"name":"identity","value":"synthetic-four","domain":"id.ustc.edu.cn","path":"/"}),
            json!({"name":"expired","value":"synthetic-five","domain":"www.bb.ustc.edu.cn","expires":1.0,"path":"/"}),
        ];
        let (url, _) = planned(&p).unwrap();
        let jar = cookie_jar(&p, &url).unwrap();
        assert_eq!(
            jar.cookies(&url).unwrap().to_str().unwrap(),
            "own=synthetic-one"
        );
        assert_eq!(
            jar.cookies(&Url::parse("https://bb.ustc.edu.cn/").unwrap())
                .unwrap()
                .to_str()
                .unwrap(),
            "alias=synthetic-two"
        );
        assert!(jar
            .cookies(&Url::parse("https://id.ustc.edu.cn/").unwrap())
            .is_none());
        p.cookies.clear();
        assert!(cookie_jar(&p, &url).unwrap().cookies(&url).is_none());
        p.cookies = vec![
            json!({"name":"own","value":"synthetic; injected=value","domain":"www.bb.ustc.edu.cn"}),
        ];
        assert!(cookie_jar(&p, &url).is_err());
    }
    #[test]
    fn preserves_json_and_strips_response_cookie_headers() {
        let body = "{\"content\":\"合成课表\"}";
        let response=format!("HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nSet-Cookie: synthetic=sample\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
        let result = local_response(&input(), response);
        assert_eq!(result["text"], body);
        assert_eq!(result["state"], "response");
        assert!(result["headers"].get("set-cookie").is_none());
    }
    #[test]
    fn login_and_foreign_redirects_stop_before_following() {
        for (target, code) in [
            (
                "https://id.ustc.edu.cn/cas/login",
                "authentication_required",
            ),
            ("https://evil.example/home", "unavailable"),
        ] {
            let result=local_response(&input(),format!("HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"));
            assert_eq!(result["code"], code);
        }
    }
    #[test]
    fn rate_limit_and_authentication_are_not_retried() {
        let result=local_response(&input(),"HTTP/1.1 429 Too Many Requests\r\nRetry-After: 120\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".into());
        assert_eq!(result["state"], "cooldown");
        assert!(result["retry_after_seconds"].as_f64().unwrap() >= 120.0);
        for (status, code) in [(401, "authentication_required"), (403, "access_denied")] {
            let result = local_response(
                &input(),
                format!(
                    "HTTP/1.1 {status} Denied\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                ),
            );
            assert_eq!(result["code"], code);
        }
    }
    #[test]
    fn bounds_decoded_body_and_detects_login_html() {
        assert!(login_html("bb", "<form id='loginForm'></form>"));
        for (body, code) in [
            ("x".repeat(LIMIT + 1), "unavailable"),
            (
                "<form><input type='password'></form>".into(),
                "authentication_required",
            ),
        ] {
            let result=local_response(&input(),format!("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()));
            assert_eq!(result["code"], code);
        }
    }
}
