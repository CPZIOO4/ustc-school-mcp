//! Native Blackboard assignment IO. Bytes never cross the public core pipe.
//! The existing encrypted SQLite format and one-shot target locks are retained.
use crate::{bb, cdp, network, storage::crypt, Error, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use chrono::Utc;
use reqwest::cookie::{CookieStore, Jar};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use unicode_casefold::UnicodeCaseFold;
use url::Url;

const LIMIT: usize = 50 * 1024 * 1024;
const HTML_LIMIT: usize = 5 * 1024 * 1024;
fn error(message: &str) -> Error {
    Error::new("unavailable", message)
}
fn storage_error() -> Error {
    Error::new(
        "storage_failure",
        "本机提交记录不可用，已停止；请勿重复提交。",
    )
}
fn require(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(error(message))
    }
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key].as_str().ok_or_else(Error::invalid)
}
fn native(action: &str, mut v: Value) -> Result<Value> {
    v["action"] = json!(action);
    bb::dispatch(&v)
}
fn now() -> String {
    Utc::now().to_rfc3339()
}
fn path(v: &Value, key: &str) -> Result<PathBuf> {
    let p = PathBuf::from(text(v, key)?);
    require(p.is_absolute(), "本机路径必须为绝对路径。")?;
    Ok(p)
}
// Match json.dumps(..., sort_keys=True, ensure_ascii=False), including spaces.
fn python_json(v: &Value) -> String {
    match v {
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(python_json).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(m) => {
            let mut keys: Vec<_> = m.keys().collect();
            keys.sort();
            format!(
                "{{{}}}",
                keys.iter()
                    .map(|k| format!("{}: {}", json!(k), python_json(&m[*k])))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        _ => v.to_string(),
    }
}
fn digest(v: &Value) -> String {
    format!("{:x}", Sha256::digest(python_json(v).as_bytes()))
}
fn encode(v: &Value) -> Result<Vec<u8>> {
    crypt(&serde_json::to_vec(v).map_err(|_| storage_error())?, false)
}
fn decode(v: &[u8]) -> Result<Value> {
    let plain = crypt(v, true)?;
    require(
        plain.len() <= 80 * 1024 * 1024,
        "本机作业记录超出大小限制。",
    )?;
    crate::strict_json::parse(&plain).map_err(|_| storage_error())
}
fn identifier(s: &str) -> Result<()> {
    require(
        s.len() == 32
            && s.bytes()
                .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()),
        "准备记录ID无效。",
    )
}

struct Store {
    database: PathBuf,
    account: String,
}
impl Store {
    fn new(v: &Value) -> Result<Self> {
        let account = text(v, "account")?;
        require(
            !account.is_empty() && account.len() <= 256,
            "当前账号标识无效。",
        )?;
        Ok(Self {
            database: path(v, "database")?,
            account: account.into(),
        })
    }
    fn transact<T>(&self, f: impl FnOnce(&Connection) -> Result<T>) -> Result<T> {
        fs::create_dir_all(self.database.parent().ok_or_else(Error::invalid)?)
            .map_err(|_| storage_error())?;
        let mut db = Connection::open(&self.database).map_err(|_| storage_error())?;
        db.busy_timeout(Duration::from_secs(5))
            .map_err(|_| storage_error())?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| storage_error())?;
        tx.execute_batch("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, account TEXT, target TEXT, digest TEXT, state TEXT, phase TEXT, payload BLOB, result BLOB);
            CREATE TABLE IF NOT EXISTS target_locks (target TEXT PRIMARY KEY, job TEXT);
            CREATE TABLE IF NOT EXISTS native_bb_write_lease (id INTEGER PRIMARY KEY CHECK(id=1));").map_err(|_| storage_error())?;
        // The lease exists only inside the IMMEDIATE transaction; old writers can
        // never observe it and their writes are rejected, including running workers.
        for table in ["jobs", "target_locks"] {
            for action in ["INSERT", "UPDATE", "DELETE"] {
                tx.execute_batch(&format!("CREATE TRIGGER IF NOT EXISTS native_bb_{table}_{action} BEFORE {action} ON {table} WHEN NOT EXISTS (SELECT 1 FROM native_bb_write_lease WHERE id=1) BEGIN SELECT RAISE(ABORT,'native BB runtime required'); END;")).map_err(|_| storage_error())?;
            }
        }
        tx.execute("INSERT INTO native_bb_write_lease VALUES (1)", [])
            .map_err(|_| storage_error())?;
        let out = f(&tx)?;
        tx.execute("DELETE FROM native_bb_write_lease WHERE id=1", [])
            .map_err(|_| storage_error())?;
        tx.commit().map_err(|_| storage_error())?;
        Ok(out)
    }
    fn create(&self, plan: &Value) -> Result<String> {
        let id = uuid::Uuid::new_v4().simple().to_string();
        let target = digest(&json!([
            plan["account"],
            plan["assignment"]["course_id"],
            plan["assignment"]["content_id"]
        ]));
        let payload = encode(plan)?;
        self.transact(|db| {
            let previous: Option<String> = db.query_row("SELECT id FROM jobs WHERE account=? AND digest=? AND state!='stopped_before_write'", params![self.account,text(plan,"sha256")?], |r|r.get(0)).optional().map_err(|_| storage_error())?;
            if let Some(previous) = previous { return Ok(previous); }
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)", params![id,self.account,target,text(plan,"sha256")?,"ready","prepared",payload]).map_err(|_| storage_error())?;
            Ok(id.clone())
        })
    }
    fn get(&self, id: &str) -> Result<Value> {
        identifier(id)?;
        self.transact(|db| {
            type Row = (String, String, String, Vec<u8>, Option<Vec<u8>>);
            let row: Option<Row> = db.query_row("SELECT account,state,phase,payload,result FROM jobs WHERE id=?",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional().map_err(|_| storage_error())?;
            let (account,state,phase,payload,result) = row.ok_or_else(|| error("准备记录不存在或不属于当前账号。"))?;
            require(account == self.account, "准备记录不存在或不属于当前账号。")?;
            Ok(json!({"state":state,"phase":phase,"plan":decode(&payload)?,"result":result.map(|v|decode(&v)).transpose()?}))
        })
    }
    fn claim(&self, id: &str, hash: &str) -> Result<bool> {
        identifier(id)?;
        self.transact(|db| {
            let row: Option<(String, String, String, String)> = db
                .query_row(
                    "SELECT account,target,digest,state FROM jobs WHERE id=?",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
                )
                .optional()
                .map_err(|_| storage_error())?;
            let (account, target, digest, state) =
                row.ok_or_else(|| error("账号或材料摘要不匹配。"))?;
            require(
                account == self.account && digest == hash,
                "账号或材料摘要不匹配。",
            )?;
            if state != "ready" {
                return Ok(false);
            }
            let locked: bool = db
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM target_locks WHERE target=?)",
                    [&target],
                    |r| r.get(0),
                )
                .map_err(|_| storage_error())?;
            require(
                !locked,
                "该作业已有执行中或结果待核实的提交；先查原记录，不创建另一份提交。",
            )?;
            db.execute("INSERT INTO target_locks VALUES (?,?)", params![target, id])
                .map_err(|_| storage_error())?;
            db.execute(
                "UPDATE jobs SET state='executing',phase='preflight' WHERE id=?",
                [id],
            )
            .map_err(|_| storage_error())?;
            Ok(true)
        })
    }
    fn advance(&self, id: &str, phase: &str) -> Result<()> {
        require(
            matches!(
                phase,
                "new_attempt_requested" | "materials_verified" | "submit_requested"
            ),
            "提交阶段无效。",
        )?;
        self.transact(|db| {
            let row: Option<(String, String, String)> = db
                .query_row(
                    "SELECT account,state,phase FROM jobs WHERE id=?",
                    [id],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .optional()
                .map_err(|_| storage_error())?;
            let (account, state, prior) = row.ok_or_else(storage_error)?;
            let valid = matches!(
                (prior.as_str(), phase),
                ("preflight", "new_attempt_requested" | "materials_verified")
                    | ("new_attempt_requested", "materials_verified")
                    | ("materials_verified", "submit_requested")
            );
            require(
                account == self.account && state == "executing" && valid,
                "提交阶段已经改变；不重复发送最终请求。",
            )?;
            db.execute("UPDATE jobs SET phase=? WHERE id=?", params![phase, id])
                .map_err(|_| storage_error())?;
            Ok(())
        })
    }
    fn finish(&self, id: &str, state: &str, result: &Value, expected: &str) -> Result<()> {
        require(
            matches!(state, "verified" | "uncertain" | "stopped_before_write")
                && matches!(expected, "executing" | "uncertain"),
            "提交状态无效。",
        )?;
        let encoded = encode(result)?;
        self.transact(|db| {
            let changed = db
                .execute(
                    "UPDATE jobs SET state=?,result=? WHERE id=? AND account=? AND state=?",
                    params![state, encoded, id, self.account, expected],
                )
                .map_err(|_| storage_error())?;
            if changed == 1 && matches!(state, "verified" | "stopped_before_write") {
                db.execute("DELETE FROM target_locks WHERE job=?", [id])
                    .map_err(|_| storage_error())?;
            }
            Ok(())
        })
    }
    fn status(&self, id: &str) -> Result<Value> {
        native(
            "status",
            json!({"identifier":id,"job":self.get(id)?,"now":now()}),
        )
    }
}

fn file_record(name: &str, bytes: &[u8]) -> Result<Value> {
    require(
        !name.is_empty()
            && name.chars().count() <= 125
            && !matches!(name, "." | "..")
            && !name.contains(['/', '\\', '\0', '\r', '\n']),
        "文件名不符合已验证上传要求。",
    )?;
    require(
        !bytes.is_empty() && bytes.len() <= LIMIT,
        "不提交空文件或超过50MiB的附件。",
    )?;
    Ok(
        json!({"filename":name,"size_bytes":bytes.len(),"sha256":format!("{:x}",Sha256::digest(bytes))}),
    )
}
fn canonical(plan: &Value) -> Result<Value> {
    let mut p = plan.as_object().ok_or_else(Error::invalid)?.clone();
    p.remove("sha256");
    p.remove("prepared_at");
    Ok(Value::Object(p))
}
fn frozen_bytes(plan: &Value) -> Result<Vec<Vec<u8>>> {
    let files = plan["files"].as_array().ok_or_else(Error::invalid)?;
    require((1..=10).contains(&files.len()), "附件数量无效。")?;
    let mut out = Vec::new();
    let mut total = 0;
    let mut names = HashSet::new();
    for f in files {
        let data = text(f, "data")?;
        require(data.len() <= LIMIT.div_ceil(3) * 4, "附件编码超过限制。")?;
        let b = STANDARD
            .decode(data)
            .map_err(|_| error("准备记录附件编码无效。"))?;
        total += b.len();
        require(total <= LIMIT, "附件总大小超过50MiB限制。")?;
        let r = file_record(text(f, "filename")?, &b)?;
        require(
            ["filename", "size_bytes", "sha256"]
                .iter()
                .all(|k| r[*k] == f[*k]),
            "准备记录附件摘要不匹配。",
        )?;
        require(
            names.insert(text(f, "filename")?.case_fold().collect::<String>()),
            "附件重名，已停止。",
        )?;
        out.push(b);
    }
    Ok(out)
}
fn paced(database: &Path) -> Result<()> {
    let until = network::now() + 10.;
    loop {
        let state = network::policy(
            &json!({"action":"acquire","database":database,"service":"bb","now":network::now(),"deadline":until}),
        )?;
        if state["state"] == "acquired" {
            return Ok(());
        }
        require(
            state["state"] == "wait" && network::now() < until,
            "BB 请求处于冷却中，请稍后核对，不自动重试。",
        )?;
        std::thread::sleep(Duration::from_secs_f64(
            state["retry_after_seconds"]
                .as_f64()
                .unwrap_or(1.)
                .clamp(0.01, 10.),
        ));
    }
}
fn record_response(
    database: &Path,
    status: u16,
    retry: Option<&str>,
    submission: bool,
) -> Result<()> {
    network::policy(
        &json!({"action":"response","database":database,"service":"bb","now":network::now(),"status":status,"retry_after":retry,"jitter":0.5}),
    )?;
    require(
        status == 200 || (submission && matches!(status, 302 | 303)),
        "BB 请求返回异常状态，已停止，不自动重试。",
    )
}
fn filtered_state(state: &Value) -> Result<Value> {
    crate::sessions::dispatch("sessions_filter", &json!({"profile":"bb","state":state}))
}
struct Http {
    client: reqwest::Client,
    state: Value,
    policy: PathBuf,
    user_agent: String,
}
impl Http {
    fn new(v: &Value) -> Result<Self> {
        let state = filtered_state(&v["state"])?;
        let agent = v["user_agent"].as_str().unwrap_or("Mozilla/5.0");
        require(
            agent.len() <= 2048 && !agent.contains(['\0', '\r', '\n']),
            "浏览器信息无效。",
        )?;
        Ok(Self {
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .retry(reqwest::retry::never())
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(30))
                .build()
                .map_err(|_| error("无法初始化BB连接。"))?,
            state,
            policy: path(v, "policy_database")?,
            user_agent: agent.into(),
        })
    }
    fn absorb_cookies(&mut self, response: &reqwest::Response, url: &Url) -> Result<()> {
        let mut rows = self.state["cookies"]
            .as_array()
            .ok_or_else(Error::invalid)?
            .clone();
        for c in response.cookies() {
            let domain = c
                .domain()
                .map(|s| format!(".{}", s.trim_start_matches('.').to_ascii_lowercase()))
                .unwrap_or_else(|| url.host_str().unwrap_or("").to_owned());
            let default_path = url
                .path()
                .rsplit_once('/')
                .map(|(p, _)| if p.is_empty() { "/" } else { p })
                .unwrap_or("/");
            let cookie_path = c
                .path()
                .filter(|p| p.starts_with('/'))
                .unwrap_or(default_path);
            let expires = if let Some(age) = c.max_age() {
                network::now() + age.as_secs_f64()
            } else if let Some(time) = c.expires() {
                time.duration_since(std::time::UNIX_EPOCH)
                    .map_or(0., |d| d.as_secs_f64())
            } else {
                -1.
            };
            let mut cookie = json!({"name":c.name(),"value":c.value(),"domain":domain,"path":cookie_path,"secure":c.secure(),"httpOnly":c.http_only(),"expires":expires});
            if c.same_site_lax() {
                cookie["sameSite"] = json!("Lax");
            }
            if c.same_site_strict() {
                cookie["sameSite"] = json!("Strict");
            }
            // Validate the response domain before replacing any existing cookie.
            // A rejected or expired Set-Cookie must not erase an unrelated scope.
            let domain_matches = domain.trim_start_matches('.') == url.host_str().unwrap_or("")
                || (domain.starts_with('.') && url.host_str().unwrap_or("").ends_with(&domain));
            if !domain_matches {
                continue;
            }
            rows.retain(|old| {
                !(old["name"] == c.name()
                    && old["domain"].as_str().is_some_and(|d| {
                        d.trim_start_matches('.')
                            .eq_ignore_ascii_case(domain.trim_start_matches('.'))
                    })
                    && old["path"] == cookie_path)
            });
            if expires < 0. || expires > network::now() {
                rows.push(cookie);
            }
        }
        self.state = filtered_state(&json!({"cookies":rows}))?;
        Ok(())
    }
    async fn get(&mut self, target: &str, binary: bool) -> Result<Vec<u8>> {
        let url = Url::parse(target).map_err(|_| Error::invalid())?;
        require(
            url.scheme() == "https"
                && url.host_str() == Some("www.bb.ustc.edu.cn")
                && url.port_or_known_default() == Some(443)
                && url.username().is_empty()
                && url.password().is_none(),
            "BB连接目标无效。",
        )?;
        let jar = Jar::default();
        for c in self.state["cookies"]
            .as_array()
            .ok_or_else(Error::invalid)?
        {
            let domain = c["domain"].as_str().unwrap_or("");
            let host = domain.trim_start_matches('.');
            let base = Url::parse(&format!("https://{host}/")).map_err(|_| Error::invalid())?;
            let name = text(c, "name")?;
            let value = text(c, "value")?;
            let cookie_path = c["path"].as_str().unwrap_or("/");
            require(
                !name.is_empty()
                    && name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b))
                    && !value
                        .bytes()
                        .any(|b| !(0x20..0x7f).contains(&b) || b";,".contains(&b))
                    && cookie_path.starts_with('/')
                    && !cookie_path.contains(['\r', '\n', ';']),
                "本机BB会话格式无效。",
            )?;
            if c["expires"]
                .as_f64()
                .is_some_and(|t| t >= 0. && t <= network::now())
            {
                continue;
            }
            let domain_attr = if domain.starts_with('.') {
                format!("; Domain={domain}")
            } else {
                String::new()
            };
            let age = c["expires"]
                .as_f64()
                .filter(|t| *t >= 0.)
                .map(|t| format!("; Max-Age={}", (t - network::now()).max(0.).floor() as u64))
                .unwrap_or_default();
            jar.add_cookie_str(
                &format!("{name}={value}; Path={cookie_path}{domain_attr}{age}; Secure"),
                &base,
            );
        }
        paced(&self.policy)?;
        let mut request = self
            .client
            .get(url.clone())
            .header("User-Agent", &self.user_agent);
        if let Some(cookie) = jar.cookies(&url) {
            request = request.header(reqwest::header::COOKIE, cookie);
        }
        let mut response = match request.send().await {
            Ok(r) => r,
            Err(_) => {
                network::policy(
                    &json!({"action":"failure","database":self.policy,"service":"bb","now":network::now(),"jitter":0.5}),
                )?;
                return Err(error("BB网络请求未完成，已停止；没有自动重试。"));
            }
        };
        record_response(
            &self.policy,
            response.status().as_u16(),
            response
                .headers()
                .get("retry-after")
                .and_then(|v| v.to_str().ok()),
            false,
        )?;
        self.absorb_cookies(&response, &url)?;
        let content_type = response
            .headers()
            .get("content-type")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_ascii_lowercase();
        require(
            if binary {
                !content_type.contains("text/html")
            } else {
                content_type.starts_with("text/html")
            },
            "BB响应类型与预期不一致。",
        )?;
        let limit = if binary { LIMIT } else { HTML_LIMIT };
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| error("BB响应未读取完整，已停止。"))?
        {
            require(
                bytes.len() + chunk.len() <= limit,
                "BB响应超过读取大小限制。",
            )?;
            bytes.extend_from_slice(&chunk);
        }
        require(!bytes.is_empty(), "BB响应为空。")?;
        network::policy(
            &json!({"action":"success","database":self.policy,"service":"bb","now":network::now()}),
        )?;
        Ok(bytes)
    }
    async fn read(&mut self, target: &str) -> Result<String> {
        let bytes = self.get(target, false).await?;
        let html = String::from_utf8(bytes).map_err(|_| error("BB页面编码不支持。"))?;
        if native("login_page", json!({"url":target,"html":html}))? == true {
            network::policy(
                &json!({"action":"failure","database":self.policy,"service":"bb","now":network::now(),"authentication":true}),
            )?;
            return Err(Error::new(
                "authentication_required",
                "BB登录已失效，请重新登录。",
            ));
        }
        Ok(html)
    }
    async fn view(&mut self, a: &Value) -> Result<(Value, String, String)> {
        let target = native("review_url", a.clone())?
            .as_str()
            .ok_or_else(Error::invalid)?
            .to_owned();
        let html = self.read(&target).await?;
        let snapshot = native("snapshot", json!({"html":html,"assignment":a}))?;
        Ok((snapshot, html, target))
    }
    async fn download(&mut self, url: &str, a: &Value, attempt: &Value) -> Result<Vec<u8>> {
        let checked = native(
            "download",
            json!({"url":url,"assignment":a,"attempt_id":attempt}),
        )?;
        self.get(checked[0].as_str().ok_or_else(Error::invalid)?, true)
            .await
    }
}

async fn materials(v: &Value, current: &Value, http: &mut Http) -> Result<Vec<Value>> {
    let reuse = v["reuse_previous_files"] == true;
    let entries = if reuse {
        &current["files"]
    } else {
        &v["files"]
    };
    let mut files = Vec::new();
    let mut total = 0;
    let mut names = HashSet::new();
    for entry in entries.as_array().ok_or_else(Error::invalid)? {
        let (name, bytes) = if reuse {
            (
                text(entry, "filename")?.to_owned(),
                http.download(
                    text(entry, "url")?,
                    &v["assignment"],
                    &current["attempt_id"],
                )
                .await?,
            )
        } else {
            let p = PathBuf::from(entry.as_str().ok_or_else(Error::invalid)?);
            require(p.is_absolute(), "附件必须是用户指定的现有绝对路径。")?;
            require(
                fs::metadata(&p)
                    .map_err(|_| error("无法核对作业附件。"))?
                    .is_file(),
                "作业附件必须是普通文件。",
            )?;
            let file = fs::File::open(&p).map_err(|_| error("无法读取用户指定的作业附件。"))?;
            require(
                file.metadata()
                    .map_err(|_| error("无法核对附件类型。"))?
                    .is_file(),
                "作业附件必须是普通文件。",
            )?;
            let mut bytes = Vec::new();
            file.take((LIMIT - total + 1) as u64)
                .read_to_end(&mut bytes)
                .map_err(|_| error("作业附件未读取完整。"))?;
            (
                p.file_name()
                    .and_then(|s| s.to_str())
                    .ok_or_else(Error::invalid)?
                    .to_owned(),
                bytes,
            )
        };
        total += bytes.len();
        require(total <= LIMIT, "附件总大小超过50MiB限制。")?;
        require(
            names.insert(name.case_fold().collect::<String>()),
            "附件重名，已停止。",
        )?;
        let mut item = file_record(&name, &bytes)?;
        item["data"] = json!(STANDARD.encode(bytes));
        files.push(item);
    }
    Ok(files)
}
async fn verify(http: &mut Http, plan: &Value, current: &Value) -> Result<Value> {
    require(
        current["kind"] == "history" && current["attempt_id"] != plan["baseline"]["attempt_id"],
        "尚未出现新的可核验尝试，结果待核实。",
    )?;
    let files = current["files"].as_array().ok_or_else(Error::invalid)?;
    require(
        files.len() == plan["files"].as_array().ok_or_else(Error::invalid)?.len(),
        "新记录附件数量不匹配，结果待核实。",
    )?;
    let mut actual = Vec::new();
    let mut total = 0;
    for f in files {
        let bytes = http
            .download(text(f, "url")?, &plan["assignment"], &current["attempt_id"])
            .await?;
        total += bytes.len();
        require(total <= LIMIT, "回执附件总量超过限制。")?;
        actual.push(file_record(text(f, "filename")?, &bytes)?);
    }
    native(
        "receipt",
        json!({"plan":plan,"current":current,"actual":actual}),
    )
}

fn multipart(body: &str, content_type: &str, plan: &Value) -> Result<()> {
    require(body.len() <= 2 * 1024 * 1024, "提交表单字段超过大小限制。")?;
    let boundary = content_type
        .split(';')
        .skip(1)
        .filter_map(|p| p.trim().split_once('='))
        .find(|(k, _)| k.eq_ignore_ascii_case("boundary"))
        .map(|(_, v)| v.trim().trim_matches('"'))
        .ok_or_else(|| error("附件表单没有边界标记。"))?;
    require(
        content_type
            .to_ascii_lowercase()
            .starts_with("multipart/form-data;")
            && !boundary.is_empty()
            && boundary.len() <= 200
            && !boundary.contains(['\r', '\n', '\0']),
        "提交请求不是预期的附件表单。",
    )?;
    let delimiter = format!("--{boundary}");
    require(
        body.starts_with(&delimiter)
            && body
                .trim_end_matches(['\r', '\n'])
                .ends_with(&format!("{delimiter}--")),
        "附件表单未完整读取。",
    )?;
    let mut fields = Map::new();
    let mut files = Vec::new();
    let parameter =
        regex::Regex::new(r#"(?:^|;\s*)(name|filename)="([^"]*)""#).expect("constant regex");
    for part in body.split(&delimiter).skip(1) {
        if part.starts_with("--") {
            break;
        }
        let part = part
            .strip_prefix("\r\n")
            .ok_or_else(|| error("附件表单分段无效。"))?;
        let (headers, data) = part
            .split_once("\r\n\r\n")
            .ok_or_else(|| error("附件表单头无效。"))?;
        require(headers.len() <= 8192, "附件表单头超过大小限制。")?;
        let dispositions: Vec<_> = headers
            .split("\r\n")
            .filter_map(|h| h.split_once(':'))
            .filter(|(k, _)| k.eq_ignore_ascii_case("content-disposition"))
            .collect();
        require(dispositions.len() == 1, "附件表单字段说明不唯一。")?;
        let value = dispositions[0].1.trim();
        require(value.starts_with("form-data;"), "附件表单字段类型无效。")?;
        let mut name = None;
        let mut filename = None;
        for pair in parameter.captures_iter(value) {
            let slot = if &pair[1] == "name" {
                &mut name
            } else {
                &mut filename
            };
            require(slot.is_none(), "附件表单字段参数重复。")?;
            *slot = Some(pair[2].to_owned());
        }
        let name = name.ok_or_else(|| error("附件表单字段名称缺失。"))?;
        if let Some(filename) = filename {
            files.push(json!(filename));
        } else {
            let data = data
                .strip_suffix("\r\n")
                .ok_or_else(|| error("附件表单字段不完整。"))?;
            fields
                .entry(name)
                .or_insert_with(|| json!([]))
                .as_array_mut()
                .ok_or_else(Error::invalid)?
                .push(json!(data));
        }
    }
    native(
        "multipart",
        json!({"fields":fields,"files":files,"plan":plan}),
    )?;
    Ok(())
}
struct TempFiles {
    directory: PathBuf,
    files: Vec<String>,
}
impl TempFiles {
    fn new(plan: &Value, bytes: &[Vec<u8>]) -> Result<Self> {
        let directory =
            std::env::temp_dir().join(format!("ustc-bb-upload-{}", uuid::Uuid::new_v4().simple()));
        fs::create_dir(&directory).map_err(|_| error("无法建立本机临时附件目录。"))?;
        let mut out = Self {
            directory,
            files: Vec::new(),
        };
        for (f, b) in plan["files"]
            .as_array()
            .ok_or_else(Error::invalid)?
            .iter()
            .zip(bytes)
        {
            let p = out.directory.join(text(f, "filename")?);
            // No user-controlled recursive cleanup target: only children of this
            // freshly created, owned random directory are ever written or removed.
            require(
                p.parent() == Some(out.directory.as_path()),
                "附件临时路径无效。",
            )?;
            fs::write(&p, b).map_err(|_| error("无法写入本机临时附件。"))?;
            out.files
                .push(p.to_str().ok_or_else(Error::invalid)?.into());
        }
        Ok(out)
    }
}
impl Drop for TempFiles {
    fn drop(&mut self) {
        // Do not follow a replaced owned directory. Chrome closes first on both
        // success and unwinding; retry briefly for Windows file-handle teardown.
        for _ in 0..20 {
            let safe = fs::symlink_metadata(&self.directory).is_ok_and(|m| {
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    if m.file_attributes() & 0x400 != 0 {
                        return false; // Any reparse point, including junctions.
                    }
                }
                m.is_dir() && !m.file_type().is_symlink()
            });
            if !safe || fs::remove_dir_all(&self.directory).is_ok() {
                break;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}
#[derive(Default)]
struct SendState {
    rendered: bool,
    armed: bool,
    posted: bool,
}

type TestTransport = fn(&Value) -> cdp::Decision;
// The optional fixture is a Rust function pointer, never deserialized from the
// pipe, environment, or webpage. Production always passes None.
#[allow(clippy::too_many_arguments)]
async fn send(
    http: &mut Http,
    store: &Store,
    id: &str,
    plan: &Value,
    current: &Value,
    mut html: String,
    mut target: String,
    bytes: &[Vec<u8>],
    test_transport: Option<TestTransport>,
) -> Result<()> {
    if current["kind"] == "history" {
        target = text(current, "new_url")?.into();
        store.advance(id, "new_attempt_requested")?;
        html = http.read(&target).await?;
    }
    let post_url = native("form", json!({"html":html,"assignment":plan["assignment"]}))?
        .as_str()
        .ok_or_else(Error::invalid)?
        .to_owned();
    let state = Arc::new(Mutex::new(SendState::default()));
    let shared = state.clone();
    let guard_target = target.clone();
    let guard_post = post_url.clone();
    let guard_plan = plan.clone();
    let guard_html = html.into_bytes();
    let guard_policy = http.policy.clone();
    let guard_store = Store {
        database: store.database.clone(),
        account: store.account.clone(),
    };
    let guard_id = id.to_owned();
    let guard = Box::new(move |event: &Value| -> Result<cdp::Decision> {
        let mut s = shared.lock().map_err(|_| error("本机提交状态不可用。"))?;
        let r = &event["request"];
        let url = text(r, "url")?;
        let method = text(r, "method")?;
        if !s.rendered
            && method == "GET"
            && url == guard_target
            && event["resourceType"] == "Document"
        {
            s.rendered = true;
            return Ok(cdp::Decision::Fulfill {
                status: 200,
                headers: vec![("Content-Type".into(), "text/html; charset=utf-8".into())],
                body: guard_html.clone(),
            });
        }
        if event["resourceType"] != "Document"
            && native("static_request", json!({"url":url,"method":method}))? == true
        {
            if let Some(fixture) = test_transport {
                return Ok(fixture(event));
            }
            return Ok(cdp::Decision::Continue);
        }
        if s.armed && !s.posted && method == "POST" && url == guard_post {
            let content_type = r["headers"]
                .as_object()
                .and_then(|h| {
                    h.iter()
                        .find(|(k, _)| k.eq_ignore_ascii_case("content-type"))
                })
                .and_then(|(_, v)| v.as_str())
                .ok_or_else(|| error("提交请求未提供附件表单类型。"))?;
            // CDP excludes file bytes; browser File objects were hashed below.
            // If Chrome stops exposing multipart metadata, fail closed.
            multipart(
                r["postData"]
                    .as_str()
                    .ok_or_else(|| error("浏览器未提供可核验的附件表单字段。"))?,
                content_type,
                &guard_plan,
            )?;
            paced(&guard_policy)?;
            guard_store.advance(&guard_id, "submit_requested")?;
            s.posted = true;
            if let Some(fixture) = test_transport {
                return Ok(fixture(event));
            }
            return Ok(cdp::Decision::Continue);
        }
        Ok(cdp::Decision::Abort)
    });
    let temp = TempFiles::new(plan, bytes)?;
    let mut page = cdp::Page::launch(&http.state, Some(&http.user_agent), true, guard).await?;
    page.navigate(&target)?;
    page.wait_expression(
        "Boolean(window.newFile_FilePickerObject && typeof window.checkDupeFile === 'function')",
        15000,
    )?;
    let document = page.command("DOM.getDocument", json!({"depth":0}))?;
    let node = page.command(
        "DOM.querySelector",
        json!({"nodeId":document["root"]["nodeId"],"selector":"#newFile_chooseLocalFile"}),
    )?;
    require(
        node["nodeId"].as_i64().is_some_and(|v| v > 0),
        "没有找到作业附件控件。",
    )?;
    page.command(
        "DOM.setFileInputFiles",
        json!({"nodeId":node["nodeId"],"files":temp.files}),
    )?;
    let comment = text(plan, "comment")?;
    let escaped = comment
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
        .replace('\n', "<br>");
    let rendered = if comment.is_empty() {
        String::new()
    } else {
        format!("<p>{escaped}</p>")
    };
    page.evaluate(&format!("(()=>{{const value={};const e=window.tinyMCE&&tinyMCE.get('student_commentstext');if(e)e.setContent(value);document.getElementById('student_commentstext').value=value;return true;}})()",json!(rendered)))?;
    let actual=page.evaluate("(async()=>{const rows=newFile_FilePickerObject.getPickedFiles(document.getElementById('uploadAssignmentFormId'));return await Promise.all(rows.map(async x=>{const b=await x.file.arrayBuffer();const h=await crypto.subtle.digest('SHA-256',b);return {filename:x.file.name,size_bytes:b.byteLength,sha256:Array.from(new Uint8Array(h)).map(v=>v.toString(16).padStart(2,'0')).join('')};}));})()")?;
    let expected: Vec<_> = plan["files"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .iter()
        .map(
            |f| json!({"filename":f["filename"],"size_bytes":f["size_bytes"],"sha256":f["sha256"]}),
        )
        .collect();
    require(
        actual == json!(expected) && page.take_events("Page.javascriptDialogOpening").is_empty(),
        "浏览器所选附件与冻结材料不一致，或出现未适配提示。",
    )?;
    store.advance(id, "materials_verified")?;
    state
        .lock()
        .map_err(|_| error("本机提交状态不可用。"))?
        .armed = true;
    page.clear_responses();
    page.evaluate("(()=>{const b=document.querySelector('input[name=\"bottom_提交\"][type=\"submit\"]');if(!b||b.disabled)throw new Error('submit control unavailable');b.click();return true;})()")?;
    let response = page.wait_response(&post_url, 30000)?;
    require(
        response["url"] == post_url && response["request"]["method"] == "POST",
        "提交响应目标不匹配，结果待核实。",
    )?;
    require(
        state
            .lock()
            .map_err(|_| error("本机提交状态不可用。"))?
            .posted,
        "没有记录最终提交请求，结果不能确认。",
    )?;
    record_response(
        &http.policy,
        response["status"]
            .as_u64()
            .and_then(|s| u16::try_from(s).ok())
            .ok_or_else(Error::invalid)?,
        None,
        true,
    )?;
    // Use cookies returned by this owned page only for immediate read-only receipt
    // verification; do not alter account bindings or the user's saved login.
    let cookies = page.command("Network.getAllCookies", json!({}))?;
    http.state = filtered_state(&json!({"cookies":cookies["cookies"]}))?;
    drop(page);
    drop(temp);
    Ok(())
}

async fn prepare(v: &Value, store: &Store) -> Result<Value> {
    let mut http = Http::new(v)?;
    let (current, _, _) = http.view(&v["assignment"]).await?;
    native(
        "prepare_check",
        json!({"current":current,"files":v["files"],"comment":v["comment"],"reuse_previous_files":v["reuse_previous_files"],"resubmit":v["resubmit"]}),
    )?;
    let files = materials(v, &current, &mut http).await?;
    let mut plan = json!({"account":store.account,"session_binding":text(v,"session_binding")?,"assignment":v["assignment"],"baseline":{"kind":current["kind"],"attempt_id":current["attempt_id"],"files":current["files"],"due_text":current["due_text"],"due_at":current["due_at"]},"files":files,"comment":text(v,"comment")?,"resubmit":v["resubmit"],"allow_late":v["allow_late"]});
    require(plan["allow_late"].is_boolean(), "迟交选项无效。")?;
    plan["sha256"] = json!(digest(&plan));
    plan["prepared_at"] = json!(now());
    let id = store.create(&plan)?;
    store.status(&id)
}
async fn submit(v: &Value, store: &Store) -> Result<Value> {
    let id = text(v, "identifier")?;
    let expected = text(v, "expected_sha256")?;
    let job = store.get(id)?;
    let plan = &job["plan"];
    require(
        plan["sha256"] == expected,
        "材料摘要不匹配，请使用准备结果中的摘要。",
    )?;
    if !store.claim(id, expected)? {
        return store.status(id);
    }
    let outcome=async {
        require(digest(&canonical(plan)?)==expected,"准备记录内容校验失败。")?;
        let bytes=frozen_bytes(plan)?;
        let mut http=Http::new(v)?;
        let (current,html,target)=http.view(&plan["assignment"]).await?;
        native("preflight",json!({"plan":plan,"current":current,"assignment":v["assignment"],"session_binding":v["session_binding"],"now":now()}))?;
        send(&mut http,store,id,plan,&current,html,target,&bytes,None).await?;
        require(store.get(id)?["phase"]=="submit_requested","未记录最终提交请求，结果待核实。")?;
        let (current,_,_)=http.view(&plan["assignment"]).await?;
        let result=verify(&mut http,plan,&current).await?;
        store.finish(id,"verified",&result,"executing")
    }.await;
    if let Err(exc) = outcome {
        let phase = store.get(id)?["phase"].clone();
        let state = if phase == "preflight" {
            "stopped_before_write"
        } else {
            "uncertain"
        };
        store.finish(
            id,
            state,
            &json!({"verified":false,"message":exc.message,"code":exc.code}),
            "executing",
        )?;
    }
    store.status(id)
}
async fn dispatch_inner(v: &Value) -> Result<Value> {
    let store = Store::new(v)?;
    match text(v, "action")? {
        "prepare" => prepare(v, &store).await,
        "submit" => submit(v, &store).await,
        "status" => {
            let id = text(v, "identifier")?;
            let job = store.get(id)?;
            if v["verify"] == true
                && job["state"] == "uncertain"
                && job["phase"] == "submit_requested"
            {
                // Verification failure never grants a repeat write or releases lock.
                let checked = async {
                    let mut http = Http::new(v)?;
                    let plan = &job["plan"];
                    let (current, _, _) = http.view(&plan["assignment"]).await?;
                    let result = verify(&mut http, plan, &current).await?;
                    store.finish(id, "verified", &result, "uncertain")
                }
                .await;
                if checked.is_err() {
                    return store.status(id);
                }
            }
            store.status(id)
        }
        _ => Err(Error::invalid()),
    }
}

pub async fn dispatch(v: &Value) -> Result<Value> {
    tokio::time::timeout(Duration::from_secs(480), dispatch_inner(v))
        .await
        .map_err(|_| {
            Error::new(
                "timeout",
                "本轮作业操作超时，原提交记录保留；请只读核验，不自动重试。",
            )
        })?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    struct Directory(PathBuf);
    impl Directory {
        fn new() -> Self {
            let p = std::env::temp_dir().join(format!(
                "school-bb-io-test-{}",
                uuid::Uuid::new_v4().simple()
            ));
            fs::create_dir(&p).unwrap();
            Self(p)
        }
        fn store(&self) -> Store {
            Store {
                database: self.0.join("bb.sqlite3"),
                account: "synthetic".into(),
            }
        }
    }
    impl Drop for Directory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn plan() -> Value {
        let mut file = file_record("original.pdf", b"synthetic bytes").unwrap();
        file["data"] = json!(STANDARD.encode(b"synthetic bytes"));
        let mut p = json!({"account":"synthetic","session_binding":"synthetic-session","assignment":{"course_id":"_1_1","content_id":"_2_1","title":"合成作业","entry_path":"/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view"},"baseline":{"kind":"new_form","attempt_id":null,"files":[],"due_at":"2099-01-01T23:59:00+08:00","due_text":"2099年1月1日 下午11:59"},"files":[file],"comment":"合成\n附言","resubmit":false,"allow_late":false});
        p["sha256"] = json!(digest(&p));
        p["prepared_at"] = json!(now());
        p
    }
    #[test]
    fn python_compatible_json_and_frozen_file_integrity() {
        assert_eq!(
            python_json(&json!({"汉":"字","a":[true,null,2]})),
            r#"{"a": [true, null, 2], "汉": "字"}"#
        );
        let mut p = plan();
        assert_eq!(digest(&canonical(&p).unwrap()), p["sha256"]);
        assert_eq!(frozen_bytes(&p).unwrap(), vec![b"synthetic bytes".to_vec()]);
        p["files"][0]["data"] = json!(STANDARD.encode(b"altered"));
        assert!(frozen_bytes(&p).is_err());
        assert!(file_record("../outside", b"a").is_err());
        assert!(file_record("empty", b"").is_err());
    }
    #[test]
    fn multipart_contract_keeps_target_comment_and_attachment_names() {
        let p = plan();
        let mut fields = String::new();
        for (key, value) in [
            ("course_id", "_1_1"),
            ("content_id", "_2_1"),
            ("dispatch", "submit"),
            ("attempt_id", ""),
            ("remove_file_id", ""),
            ("studentSubmission.text", ""),
            ("student_commentstext", "<p>合成<br>附言</p>"),
        ] {
            fields += &format!(
                "--fixture\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n"
            );
        }
        fields+="--fixture\r\nContent-Disposition: form-data; name=\"file\"; filename=\"original.pdf\"\r\nContent-Type: application/pdf\r\n\r\n\r\n--fixture--\r\n";
        assert!(multipart(&fields, "multipart/form-data; boundary=fixture", &p).is_ok());
        assert!(multipart(
            &fields.replace("_2_1", "_9_1"),
            "multipart/form-data; boundary=fixture",
            &p
        )
        .is_err());
        assert!(multipart(
            &fields.replace("original.pdf", "other.pdf"),
            "multipart/form-data; boundary=fixture",
            &p
        )
        .is_err());
        assert!(multipart(
            &fields.replace("合成", "changed"),
            "multipart/form-data; boundary=fixture",
            &p
        )
        .is_err());
    }
    #[test]
    #[cfg(windows)]
    fn compatible_store_one_shot_and_unknown_never_replay() {
        let dir = Directory::new();
        let store = dir.store();
        let p = plan();
        let id = store.create(&p).unwrap();
        assert_eq!(store.create(&p).unwrap(), id);
        assert!(store.claim(&id, text(&p, "sha256").unwrap()).unwrap());
        assert!(!store.claim(&id, text(&p, "sha256").unwrap()).unwrap());
        store.advance(&id, "materials_verified").unwrap();
        store.advance(&id, "submit_requested").unwrap();
        assert!(store.advance(&id, "submit_requested").is_err());
        store
            .finish(&id, "uncertain", &json!({"verified":false}), "executing")
            .unwrap();
        assert!(!store.claim(&id, text(&p, "sha256").unwrap()).unwrap());
        let mut another = p.clone();
        another["sha256"] = json!("different");
        let second = store.create(&another).unwrap();
        assert!(store.claim(&second, "different").is_err());
        let db = Connection::open(&store.database).unwrap();
        assert!(db
            .execute("UPDATE jobs SET state='ready' WHERE id=?", [&id])
            .is_err());
        assert!(db.execute("DELETE FROM target_locks", []).is_err());
        assert_eq!(
            db.query_row("SELECT COUNT(*) FROM native_bb_write_lease", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
        let payload: Vec<u8> = db
            .query_row("SELECT payload FROM jobs WHERE id=?", [&id], |r| r.get(0))
            .unwrap();
        assert_eq!(decode(&payload).unwrap(), p);
        drop(db);
        assert_eq!(store.status(&id).unwrap()["state"], "uncertain");
    }
    #[test]
    #[cfg(windows)]
    fn concurrent_store_claims_exactly_once_and_accounts_isolated() {
        let dir = Directory::new();
        let store = dir.store();
        let p = plan();
        let id = store.create(&p).unwrap();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let mut workers = Vec::new();
        for _ in 0..2 {
            let s = Store {
                database: store.database.clone(),
                account: store.account.clone(),
            };
            let id = id.clone();
            let hash = p["sha256"].as_str().unwrap().to_owned();
            let barrier = barrier.clone();
            workers.push(std::thread::spawn(move || {
                barrier.wait();
                s.claim(&id, &hash).unwrap()
            }));
        }
        assert_eq!(
            workers
                .into_iter()
                .filter_map(|w| w.join().ok())
                .filter(|v| *v)
                .count(),
            1
        );
        let other = Store {
            database: store.database.clone(),
            account: "different-account".into(),
        };
        assert!(other.get(&id).is_err());
    }
    #[test]
    #[cfg(windows)]
    fn native_store_reads_existing_legacy_encryption_and_target_lock() {
        let dir = Directory::new();
        let store = dir.store();
        let p = plan();
        let id = uuid::Uuid::new_v4().simple().to_string();
        let db = Connection::open(&store.database).unwrap();
        db.execute_batch("CREATE TABLE jobs (id TEXT PRIMARY KEY, account TEXT, target TEXT, digest TEXT, state TEXT, phase TEXT, payload BLOB, result BLOB); CREATE TABLE target_locks (target TEXT PRIMARY KEY, job TEXT);").unwrap();
        let target = digest(&json!([
            p["account"],
            p["assignment"]["course_id"],
            p["assignment"]["content_id"]
        ]));
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,NULL)",
            params![
                id,
                "synthetic",
                target,
                text(&p, "sha256").unwrap(),
                "uncertain",
                "submit_requested",
                encode(&p).unwrap()
            ],
        )
        .unwrap();
        db.execute("INSERT INTO target_locks VALUES (?,?)", params![target, id])
            .unwrap();
        drop(db);
        assert_eq!(store.get(&id).unwrap()["plan"], p);
        assert_eq!(
            store.status(&id).unwrap()["next_action"],
            "read_only_verify_or_manual_review"
        );
        assert!(!store.claim(&id, text(&p, "sha256").unwrap()).unwrap());
    }
    #[test]
    fn response_cookies_preserve_host_path_expiry_and_deletion_scope() {
        use std::io::{Read, Write};
        let server = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let address = server.local_addr().unwrap();
        let worker = std::thread::spawn(move || {
            let (mut stream, _) = server.accept().unwrap();
            let mut request = [0u8; 2048];
            let _ = stream.read(&mut request);
            stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nSet-Cookie: SID=new; Domain=www.bb.ustc.edu.cn; Path=/; HttpOnly\r\nSet-Cookie: REMOVE=gone; Max-Age=0; Path=/\r\nSet-Cookie: BAD=x; Domain=other.invalid; Path=/\r\nSet-Cookie: PATH=scoped; SameSite=Lax\r\nSet-Cookie: OLD=gone; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT\r\n\r\nok").unwrap();
        });
        let dir = Directory::new();
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async {
            let mut http=Http::new(&json!({"policy_database":dir.0.join("policy.sqlite3"),"state":{"cookies":[
                {"name":"SID","value":"old","domain":"www.bb.ustc.edu.cn","path":"/"},
                {"name":"REMOVE","value":"old","domain":"www.bb.ustc.edu.cn","path":"/"},
                {"name":"OLD","value":"old","domain":"www.bb.ustc.edu.cn","path":"/"},
                {"name":"KEEP","value":"kept","domain":".ustc.edu.cn","path":"/"}
            ]}})).unwrap();
            let response=http.client.get(format!("http://{address}/")).send().await.unwrap();
            http.absorb_cookies(&response,&Url::parse("https://www.bb.ustc.edu.cn/webapps/assignment/uploadAssignment?action=newAttempt").unwrap()).unwrap();
            let cookies=http.state["cookies"].as_array().unwrap();
            assert_eq!(cookies.len(),3);
            assert_eq!(cookies.iter().find(|c|c["name"]=="SID").unwrap()["value"],"new");
            assert_eq!(cookies.iter().find(|c|c["name"]=="KEEP").unwrap()["domain"],".ustc.edu.cn");
            assert_eq!(cookies.iter().find(|c|c["name"]=="PATH").unwrap()["path"],"/webapps/assignment");
            assert_eq!(cookies.iter().find(|c|c["name"]=="PATH").unwrap()["sameSite"],"Lax");
        });
        worker.join().unwrap();
    }
    static POST_COUNT: AtomicUsize = AtomicUsize::new(0);
    static POST_STATUS: AtomicUsize = AtomicUsize::new(200);
    #[test]
    fn only_submit_response_accepts_post_redirect_get_acknowledgment() {
        let dir = Directory::new();
        let database = dir.0.join("policy.sqlite3");
        for status in [200, 302, 303] {
            assert!(record_response(&database, status, None, true).is_ok());
        }
        for status in [302, 303] {
            assert!(record_response(&database, status, None, false).is_err());
        }
        for status in [301, 307, 308, 500] {
            assert!(record_response(&database, status, None, true).is_err());
        }
    }
    fn chrome_transport(event: &Value) -> cdp::Decision {
        if event["request"]["method"] == "POST" {
            POST_COUNT.fetch_add(1, Ordering::SeqCst);
            let status = POST_STATUS.load(Ordering::SeqCst) as u16;
            let mut headers = vec![("Content-Type".into(), "application/json".into())];
            if status != 200 {
                headers.push(("Location".into(), "/synthetic-receipt-redirect".into()));
            }
            cdp::Decision::Fulfill {
                status,
                headers,
                body: if status == 200 {
                    b"{\"ok\":true}".to_vec()
                } else {
                    Vec::new()
                },
            }
        } else {
            cdp::Decision::Abort
        }
    }
    #[test]
    #[cfg(windows)]
    #[ignore = "requires local Chrome; zero school network, synthetic intercepted page"]
    fn headless_chrome_upload_uses_rust_bytes_and_one_shot_gate() {
        for status in [200, 302, 303] {
            chrome_upload_case(status);
        }
    }
    fn chrome_upload_case(status: usize) {
        POST_COUNT.store(0, Ordering::SeqCst);
        POST_STATUS.store(status, Ordering::SeqCst);
        let dir = Directory::new();
        let store = dir.store();
        let p = plan();
        let id = store.create(&p).unwrap();
        store.claim(&id, text(&p, "sha256").unwrap()).unwrap();
        let form = r#"<html><div id="contentPanel">截止日期 2099年1月1日 下午11:59 满分 100<form id="uploadAssignmentFormId" action="/webapps/assignment/uploadAssignment?action=submit" method="post" enctype="multipart/form-data"><input name="course_id" value="_1_1" type="hidden"><input name="content_id" value="_2_1" type="hidden"><input name="attempt_id" value="" type="hidden"><input name="remove_file_id" value="" type="hidden"><input name="dispatch" value="" type="hidden"><textarea id="studentSubmission.text" name="studentSubmission.text"></textarea><textarea id="student_commentstext" name="student_commentstext"></textarea><input id="newFile_chooseLocalFile" type="file" multiple><input name="bottom_提交" value="提交" type="submit" onclick="checkDupeFile();return false;"></form></div><script>window.newFile_FilePickerObject={getPickedFiles:()=>Array.from(document.getElementById('newFile_chooseLocalFile').files).map(f=>({file:f}))};window.checkDupeFile=()=>{let f=document.getElementById('uploadAssignmentFormId');f.elements.dispatch.value='submit';let data=new FormData(f);Array.from(document.getElementById('newFile_chooseLocalFile').files).forEach((file,i)=>data.append('newFile_LocalFile'+i,file));fetch(f.action,{method:'POST',body:data});fetch(f.action,{method:'POST',body:data});};</script></html>"#;
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async {
            let mut http = Http::new(
                &json!({"state":{"cookies":[]},"policy_database":dir.0.join("policy.sqlite3")}),
            )
            .unwrap();
            let target = native("review_url", p["assignment"].clone())
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned();
            let current = native(
                "snapshot",
                json!({"html":form,"assignment":p["assignment"]}),
            )
            .unwrap();
            send(
                &mut http,
                &store,
                &id,
                &p,
                &current,
                form.into(),
                target,
                &frozen_bytes(&p).unwrap(),
                Some(chrome_transport),
            )
            .await
            .unwrap();
        });
        assert_eq!(POST_COUNT.load(Ordering::SeqCst), 1);
        assert_eq!(store.get(&id).unwrap()["phase"], "submit_requested");
        assert!(!store.claim(&id, text(&p, "sha256").unwrap()).unwrap());
    }
}
