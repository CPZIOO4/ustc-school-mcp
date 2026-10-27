//! Shared cross-process pacing and bounded anonymous public HTTP. Never replay requests.
use crate::{Error, Result};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

fn policy_error() -> Error {
    Error::new(
        "local_policy_unavailable",
        "本机请求节流状态不可用，已停止网络请求；请检查私人目录权限后重试。",
    )
}

fn interval(service: &str) -> Result<f64> {
    match service {
        "mail" => Ok(0.5),
        "smtp" | "young" => Ok(2.0),
        "identity" => Ok(5.0),
        "bb" | "jw" | "library" | "library-public" | "teach" | "nan7" | "icourse" | "finance"
        | "portal" | "student" | "spaces" | "rooms" | "hightech" | "drive" | "compute" | "vlab" | "llm"
        | "web" => Ok(1.0),
        _ => Err(Error::invalid()),
    }
}

/// Wall clock is shared with the legacy SQLite policy, including across processes.
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

pub fn retry_after(value: Option<&str>, current: f64) -> f64 {
    let value = value.unwrap_or("").trim();
    if value.is_empty() {
        return 0.0;
    }
    if value.bytes().all(|b| b.is_ascii_digit()) {
        return value
            .parse::<f64>()
            .ok()
            .filter(|v| v.is_finite())
            .unwrap_or(1e300);
    }
    httpdate::parse_http_date(value)
        .ok()
        .and_then(|v| v.duration_since(UNIX_EPOCH).ok())
        .map(|v| (v.as_secs_f64() - current).max(0.0))
        .unwrap_or(0.0)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PolicyInput {
    action: String,
    database: Option<String>,
    service: Option<String>,
    now: f64,
    deadline: Option<f64>,
    jitter: Option<f64>,
    server_wait: Option<f64>,
    authentication: Option<bool>,
    status: Option<u16>,
    retry_after: Option<String>,
}

/// Acquire returns wait without sleeping or reserving a future slot. The caller must
/// recheck after waiting, so a concurrent 429 always wins over an older wait.
pub fn policy(value: &Value) -> Result<Value> {
    let p: PolicyInput = serde_json::from_value(value.clone()).map_err(|_| Error::invalid())?;
    if !p.now.is_finite()
        || p.now < 0.0
        || p.now > 1e100
        || p.deadline.is_some_and(|v| !v.is_finite())
        || p.jitter
            .is_some_and(|v| !v.is_finite() || !(0.0..=1.0).contains(&v))
        || p.server_wait.is_some_and(|v| !v.is_finite())
    {
        return Err(Error::invalid());
    }
    if p.action == "retry_after" {
        return Ok(json!({"seconds": retry_after(p.retry_after.as_deref(), p.now)}));
    }
    if !matches!(
        p.action.as_str(),
        "acquire" | "failure" | "success" | "response"
    ) {
        return Err(Error::invalid());
    }
    let service = p.service.as_deref().ok_or_else(Error::invalid)?;
    let spacing = interval(service)?;
    let database = Path::new(p.database.as_deref().ok_or_else(Error::invalid)?);
    if !database.is_absolute() {
        return Err(Error::invalid());
    }
    std::fs::create_dir_all(database.parent().ok_or_else(Error::invalid)?)
        .map_err(|_| policy_error())?;
    let mut connection = Connection::open(database).map_err(|_| policy_error())?;
    connection
        .busy_timeout(Duration::from_secs(2))
        .map_err(|_| policy_error())?;
    connection.execute("CREATE TABLE IF NOT EXISTS pacing (service TEXT PRIMARY KEY, next_at REAL NOT NULL, failures INTEGER NOT NULL, blocked_until REAL NOT NULL)", []).map_err(|_| policy_error())?;
    let tx = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| policy_error())?;
    let (mut next_at, mut failures, mut blocked_until): (f64, i64, f64) = tx
        .query_row(
            "SELECT next_at, failures, blocked_until FROM pacing WHERE service=?",
            [service],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(|_| policy_error())?
        .unwrap_or((0.0, 0, 0.0));
    if !next_at.is_finite() || !blocked_until.is_finite() || !(0..=6).contains(&failures) {
        return Err(policy_error());
    }
    let mut response_status = None;
    if p.action == "response" {
        response_status = Some(p.status.ok_or_else(Error::invalid)?);
    }
    let is_failure = p.action == "failure"
        || response_status.is_some_and(|s| matches!(s, 401 | 403 | 408 | 429) || s >= 500);
    let result = if p.action == "acquire" {
        if blocked_until > p.now {
            json!({"state":"cooldown", "retry_after_seconds": blocked_until - p.now})
        } else {
            let wait = (next_at - p.now).max(0.0);
            if wait == 0.0 {
                next_at = p.now + spacing;
                json!({"state":"acquired"})
            } else if p.now + wait > p.deadline.unwrap_or(p.now + 10.0) {
                json!({"state":"cooldown", "retry_after_seconds":wait})
            } else {
                json!({"state":"wait", "retry_after_seconds":wait})
            }
        }
    } else if is_failure {
        failures = (failures + 1).min(6);
        let base = (2_f64.powi(failures as i32)).min(60.0);
        let authentication = p.authentication.unwrap_or(false)
            || response_status.is_some_and(|s| s == 401 || s == 403);
        let server_wait = p
            .server_wait
            .unwrap_or(0.0)
            .max(retry_after(p.retry_after.as_deref(), p.now))
            .max(if response_status == Some(429) {
                30.0
            } else {
                0.0
            });
        let delay = (base + base * 0.2 * p.jitter.unwrap_or(0.0))
            .max(server_wait)
            .max(if authentication { 60.0 } else { 0.0 });
        blocked_until = blocked_until.max(p.now + delay);
        // 401/403 records cooldown but leaves original status handling to its adapter.
        json!({"state":if response_status.is_some_and(|s| s == 401 || s == 403) { "ok" } else { "cooldown" }, "retry_after_seconds":blocked_until - p.now})
    } else {
        if p.action == "success" && blocked_until <= p.now {
            failures = 0;
            blocked_until = 0.0;
        }
        json!({"state":"ok"})
    };
    tx.execute(
        "INSERT OR REPLACE INTO pacing VALUES (?, ?, ?, ?)",
        params![service, next_at, failures, blocked_until],
    )
    .map_err(|_| policy_error())?;
    tx.commit().map_err(|_| policy_error())?;
    Ok(result)
}

fn jitter() -> f64 {
    let value = uuid::Uuid::new_v4();
    u32::from_be_bytes(value.as_bytes()[0..4].try_into().unwrap()) as f64 / u32::MAX as f64
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PublicInput {
    database: String,
    service: String,
    url: String,
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
            .user_agent("SchoolMCP/0.1 (public read-only)")
            .build()
            .map_err(|_| Error::new("network_unavailable", "无法初始化公开站点连接。"))?;
        Ok(Self { client })
    }

    pub async fn get_public(&self, value: &Value) -> Result<Value> {
        let input: PublicInput =
            serde_json::from_value(value.clone()).map_err(|_| Error::invalid())?;
        let (url, seconds, limit) = Self::validate(&input)?;
        self.fetch(&input, url, seconds, limit).await
    }

    fn validate(input: &PublicInput) -> Result<(url::Url, u64, usize)> {
        let (host, seconds, limit) = match input.service.as_str() {
            "teach" => ("www.teach.ustc.edu.cn", 25, 5 * 1024 * 1024),
            "icourse" => ("icourse.club", 30, 8 * 1024 * 1024),
            _ => return Err(Error::invalid()),
        };
        if !Path::new(&input.database).is_absolute()
            || input.url.len() > 8192
            || input.url.contains('\\')
            || input.url.chars().any(char::is_control)
        {
            return Err(Error::invalid());
        }
        let url = url::Url::parse(&input.url).map_err(|_| Error::invalid())?;
        if url.scheme() != "https"
            || url.host_str() != Some(host)
            || url.port_or_known_default() != Some(443)
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
        {
            return Err(Error::invalid());
        }
        Ok((url, seconds, limit))
    }

    async fn fetch(
        &self,
        input: &PublicInput,
        url: url::Url,
        seconds: u64,
        limit: usize,
    ) -> Result<Value> {
        let state = policy(
            &json!({"action":"acquire", "database":input.database,"service":input.service,"now":now()}),
        )?;
        if state["state"] != "acquired" {
            return Ok(state);
        }
        let response = self
            .client
            .get(url.clone())
            .header("Accept", "text/html")
            .header("Accept-Language", "zh-CN")
            .timeout(Duration::from_secs(seconds))
            .send()
            .await;
        let mut response = match response {
            Ok(v) => v,
            Err(_) => return Self::failed(input),
        };
        let status = response.status().as_u16();
        let retry = response
            .headers()
            .get("retry-after")
            .and_then(|v| v.to_str().ok());
        let state = policy(
            &json!({"action":"response","database":input.database,"service":input.service,
            "now":now(),"status":status,"retry_after":retry,"jitter":jitter()}),
        )?;
        if state["state"] == "cooldown" {
            return Ok(state);
        }
        if matches!(status, 401 | 403) {
            return Ok(Self::error(
                "access_denied",
                "此内容不允许匿名访问；公开读取已停止。",
            ));
        }
        if (300..400).contains(&status) {
            Self::failed(input)?;
            return Ok(Self::error(
                "unavailable",
                "站点要求跳转，公开读取未跟随；请核对入口或站点状态。",
            ));
        }
        if status == 404 {
            Self::failed(input)?;
            return Ok(Self::error("unavailable", "没有找到该公开页面。"));
        }
        if !(200..300).contains(&status) {
            Self::failed(input)?;
            return Ok(Self::error(
                "service_unavailable",
                "公开站点返回异常状态，本次读取已停止。",
            ));
        }
        let content_type = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_owned();
        if !content_type
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .eq_ignore_ascii_case("text/html")
        {
            Self::failed(input)?;
            return Ok(Self::error("unavailable", "站点返回的内容不是 HTML 页面。"));
        }
        let mut bytes = Vec::new();
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) => {
                    if bytes.len() + chunk.len() > limit {
                        Self::failed(input)?;
                        return Ok(Self::error(
                            "unavailable",
                            "页面超过读取大小限制，本次读取已停止。",
                        ));
                    }
                    bytes.extend_from_slice(&chunk);
                }
                Ok(None) => break,
                Err(_) => return Self::failed(input),
            }
        }
        let encoding = content_type
            .split(';')
            .skip(1)
            .filter_map(|part| part.trim().split_once('='))
            .find(|(key, _)| key.trim().eq_ignore_ascii_case("charset"))
            .and_then(|(_, label)| {
                encoding_rs::Encoding::for_label(label.trim().trim_matches(['\'', '"']).as_bytes())
            })
            .unwrap_or(encoding_rs::UTF_8);
        let html = encoding.decode(&bytes).0.into_owned();
        if html.len() > limit {
            Self::failed(input)?;
            return Ok(Self::error(
                "unavailable",
                "页面解码后超过读取大小限制，本次读取已停止。",
            ));
        }
        // Adapter parsing must succeed before calling policy success. A 200 verification
        // page or schema drift is still a failure, not evidence to erase backoff.
        Ok(
            json!({"state":"response","html":html,"source_url":input.url,"status":status,
            "headers":{"content-type":content_type}}),
        )
    }

    fn failed(input: &PublicInput) -> Result<Value> {
        policy(
            &json!({"action":"failure","database":input.database,"service":input.service,"now":now(),"jitter":jitter()}),
        )
    }
    fn error(code: &str, message: &str) -> Value {
        json!({"state":"error","code":code,"message":message})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::{Read, Write},
        net::{TcpListener, TcpStream},
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
    };
    fn database() -> String {
        std::env::temp_dir()
            .join(format!("school-network-{}.sqlite3", uuid::Uuid::new_v4()))
            .to_string_lossy()
            .into_owned()
    }
    #[test]
    fn policy_persists_spacing_and_does_not_erase_cooldown() {
        let database = database();
        let call = |action: &str, now: f64| {
            policy(&json!({"action":action,"database":database,"service":"teach","now":now,"jitter":0})).unwrap()
        };
        assert_eq!(call("acquire", 1000.0)["state"], "acquired");
        assert_eq!(call("acquire", 1000.0)["retry_after_seconds"], 1.0);
        assert_eq!(call("failure", 1000.0)["retry_after_seconds"], 2.0);
        call("success", 1001.0);
        assert_eq!(call("acquire", 1001.0)["state"], "cooldown");
        call("success", 1002.0);
        assert_eq!(call("failure", 1002.0)["retry_after_seconds"], 2.0);
        std::fs::remove_file(database).unwrap();
    }
    #[test]
    fn retry_after_and_domains_fail_closed() {
        assert_eq!(retry_after(Some(&"9".repeat(20000)), 0.0), 1e300);
        assert_eq!(
            retry_after(Some(&format!("1{}", "0".repeat(301))), 0.0),
            1e301
        );
        assert_eq!(
            retry_after(Some("999999999999999999999999999999999999999"), 0.0),
            1e39
        );
        assert_eq!(
            retry_after(Some("Thu, 01 Jan 1970 01:00:00 GMT"), 1000.0),
            2600.0
        );
        assert_eq!(retry_after(Some("-1"), 0.0), 0.0);
        for url in [
            "http://icourse.club/course/",
            "https://icourse.club.evil.invalid/",
            "https://user:secret@icourse.club/",
            "https://icourse.club:444/",
            "https://icourse.club/#secret",
        ] {
            assert!(Client::validate(&PublicInput {
                database: database(),
                service: "icourse".into(),
                url: url.into()
            })
            .is_err());
        }
    }

    // Loopback is reachable only from this private test harness. Production always
    // validates the fixed public HTTPS hosts before entering fetch.
    fn server(
        status: &str,
        headers: &str,
        body: Vec<u8>,
        delay: Duration,
    ) -> (url::Url, Arc<AtomicUsize>, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url =
            url::Url::parse(&format!("http://{}/test", listener.local_addr().unwrap())).unwrap();
        let hits = Arc::new(AtomicUsize::new(0));
        let count = Arc::clone(&hits);
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n{headers}\r\n",
            body.len()
        );
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            let request = read_request(&mut stream);
            assert!(request.starts_with("GET /test HTTP/1.1\r\n"));
            assert!(!request.to_ascii_lowercase().contains("cookie:"));
            assert!(!request.to_ascii_lowercase().contains("authorization:"));
            count.fetch_add(1, Ordering::SeqCst);
            std::thread::sleep(delay);
            let _ = stream.write_all(response.as_bytes());
            let _ = stream.write_all(&body);
        });
        (url, hits, handle)
    }

    fn read_request(stream: &mut TcpStream) -> String {
        let mut bytes = Vec::new();
        while !bytes.ends_with(b"\r\n\r\n") {
            let mut byte = [0];
            stream.read_exact(&mut byte).unwrap();
            bytes.push(byte[0]);
            assert!(bytes.len() < 16384);
        }
        String::from_utf8(bytes).unwrap()
    }

    #[test]
    fn http_cooldown_redirect_and_auth_are_never_replayed() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        for (status, headers, expected) in [
            ("429 Too Many Requests", "Retry-After: 600\r\n", "cooldown"),
            (
                "302 Found",
                "Location: http://127.0.0.1:1/secret\r\n",
                "error",
            ),
            ("403 Forbidden", "Content-Type: text/html\r\n", "error"),
        ] {
            let (url, hits, handle) = server(
                status,
                headers,
                b"private response".to_vec(),
                Duration::ZERO,
            );
            let input = PublicInput {
                database: database(),
                service: "teach".into(),
                url: url.to_string(),
            };
            let client = Client::new().unwrap();
            let result = runtime
                .block_on(client.fetch(&input, url.clone(), 2, 1024))
                .unwrap();
            assert_eq!(result["state"], expected);
            if status.starts_with("403") {
                assert_eq!(result["code"], "access_denied");
            }
            assert!(!result.to_string().contains("private response"));
            assert_eq!(
                runtime
                    .block_on(client.fetch(&input, url, 2, 1024))
                    .unwrap()["state"],
                "cooldown"
            );
            handle.join().unwrap();
            assert_eq!(hits.load(Ordering::SeqCst), 1);
            std::fs::remove_file(input.database).unwrap();
        }
    }

    #[test]
    fn http_decoding_is_bounded_and_timeout_records_cooldown() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let compressed = vec![
            31, 139, 8, 0, 0, 0, 0, 0, 2, 10, 171, 168, 24, 5, 163, 96, 20, 12, 119, 0, 0, 230,
            201, 65, 59, 232, 3, 0, 0,
        ];
        let (url, _, handle) = server(
            "200 OK",
            "Content-Type: text/html\r\nContent-Encoding: gzip\r\n",
            compressed,
            Duration::ZERO,
        );
        let input = PublicInput {
            database: database(),
            service: "teach".into(),
            url: url.to_string(),
        };
        let client = Client::new().unwrap();
        let result = runtime.block_on(client.fetch(&input, url, 2, 100)).unwrap();
        assert_eq!(result["state"], "error");
        assert!(result["message"].as_str().unwrap().contains("大小限制"));
        handle.join().unwrap();
        std::fs::remove_file(input.database).unwrap();
        let (url, hits, handle) = server(
            "200 OK",
            "Content-Type: text/html\r\n",
            b"slow".to_vec(),
            Duration::from_millis(1200),
        );
        let input = PublicInput {
            database: database(),
            service: "icourse".into(),
            url: url.to_string(),
        };
        let result = runtime
            .block_on(client.fetch(&input, url.clone(), 1, 100))
            .unwrap();
        assert_eq!(result["state"], "cooldown");
        assert_eq!(
            runtime.block_on(client.fetch(&input, url, 1, 100)).unwrap()["state"],
            "cooldown"
        );
        handle.join().unwrap();
        assert_eq!(hits.load(Ordering::SeqCst), 1);
        std::fs::remove_file(input.database).unwrap();
    }

    #[test]
    fn client_reuses_connection_but_never_saves_cookies() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url =
            url::Url::parse(&format!("http://{}/test", listener.local_addr().unwrap())).unwrap();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            for _ in 0..2 {
                let request = read_request(&mut stream).to_ascii_lowercase();
                assert!(!request.contains("cookie:"));
                stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=windows-1252\r\nSet-Cookie: private_cookie=secret\r\nContent-Length: 1\r\n\r\n\xe9").unwrap();
                stream.flush().unwrap();
            }
        });
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let client = Client::new().unwrap();
        let input = PublicInput {
            database: database(),
            service: "teach".into(),
            url: url.to_string(),
        };
        for index in 0..2 {
            if index == 1 {
                std::thread::sleep(Duration::from_millis(1050));
            }
            let result = runtime
                .block_on(client.fetch(&input, url.clone(), 2, 100))
                .unwrap();
            assert_eq!(result["state"], "response");
            assert_eq!(result["html"], "é");
            assert!(!result.to_string().contains("private_cookie"));
        }
        handle.join().unwrap();
        std::fs::remove_file(input.database).unwrap();
    }
}
