//! Fixed Young browser workflows and standalone, durable registration worker.
//! No arbitrary JS, target URL, shell command, or executable enters the public pipe.
use crate::{
    cdp::{Decision, Page},
    network, sessions, young, Error, Result,
};
use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use url::Url;

const HOST: &str = "young.ustc.edu.cn";
const HOME: &str = "https://young.ustc.edu.cn/login/sc-wisdom-group-learning/dataAnalysis/visual";
const BASE: &str = "/login/wisdom-group-learning-bg";
const LIST: &str = "/login/wisdom-group-learning-bg/mobile/item/enrolmentList";
const DETAIL: &str = "/login/wisdom-group-learning-bg/item/scItem/queryById";
const CHILDREN: &str = "/login/wisdom-group-learning-bg/item/scItem/selectSignChirdItem";
const WRITE: &str = "/login/wisdom-group-learning-bg/item/scItemRegistration/enter/";

fn failure(code: &'static str, message: &str) -> Error {
    Error::new(code, message)
}
fn require(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(failure("invalid_arguments", message))
    }
}
fn text<'a>(v: &'a Value, name: &str) -> Result<&'a str> {
    v[name].as_str().ok_or_else(Error::invalid)
}
fn now() -> String {
    Utc::now().to_rfc3339()
}
fn date(v: &Value) -> Result<DateTime<chrono::FixedOffset>> {
    DateTime::parse_from_rfc3339(v.as_str().ok_or_else(Error::invalid)?)
        .map_err(|_| Error::invalid())
}
fn identifier(value: &str) -> Result<&str> {
    require(
        value.len() == 32
            && value
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)),
        "任务编号无效。",
    )?;
    Ok(value)
}
fn item_id(value: &Value) -> Result<&str> {
    let s = value.as_str().ok_or_else(Error::invalid)?;
    require(
        !s.is_empty()
            && s.len() <= 100
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-'),
        "项目编号无效。",
    )?;
    Ok(s)
}
fn private_dir(input: &Value) -> Result<PathBuf> {
    let p = PathBuf::from(text(input, "directory")?);
    require(
        p.is_absolute()
            && !p
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir)),
        "私人目录必须为绝对路径。",
    )?;
    Ok(p)
}
fn parse(action: &str, mut args: Value) -> Result<Value> {
    args["action"] = json!(action);
    young::dispatch("young_parse", &args)
}
fn policy(action: &str, mut args: Value) -> Result<Value> {
    args["action"] = json!(action);
    young::dispatch("young_policy", &args)
}

#[derive(Clone)]
struct Store {
    directory: PathBuf,
    account: String,
}
impl Store {
    fn load(directory: &Path) -> Result<(Self, Value)> {
        let identity = sessions::dispatch(
            "sessions_read",
            &json!({"profile":"identity","path":directory.join("ustc-identity.credentials.dpapi")}),
        )?;
        let account = text(&identity, "username")?;
        let session = sessions::dispatch(
            "sessions_read",
            &json!({"profile":"young","path":directory.join("young-session.dpapi"),"account":account}),
        )?;
        Ok((
            Self {
                directory: directory.to_owned(),
                account: format!("{:x}", Sha256::digest(account.as_bytes())),
            },
            session,
        ))
    }
    fn call(&self, action: &str, id: &str, mut args: Value) -> Result<Value> {
        identifier(id)?;
        args["action"] = json!(action);
        args["job_id"] = json!(id);
        args["database"] = json!(self.directory.join("young-registrations.sqlite3"));
        args["account"] = json!(self.account);
        young::dispatch("young_job", &args)
    }
    fn view(&self, id: &str) -> Result<Value> {
        self.call("view", id, json!({}))
    }
    fn transition(&self, id: &str, expected: &str, state: &str, result: Value) -> Result<()> {
        self.call(
            "transition",
            id,
            json!({"expected":expected,"state":state,"result":result}),
        )?;
        Ok(())
    }
    fn progress(&self, id: &str, result: Value) -> Result<()> {
        self.call("progress", id, json!({"result":result}))?;
        Ok(())
    }
}

fn pace(directory: &Path, action: &str, extra: Value) -> Result<Value> {
    let mut args = json!({"action":action,"database":directory.join("request-policy.sqlite3"),"service":"young","now":network::now()});
    for (k, v) in extra.as_object().ok_or_else(Error::invalid)? {
        args[k] = v.clone();
    }
    network::policy(&args)
}
fn acquire(directory: &Path) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let state = pace(directory, "acquire", json!({}))?;
        match state["state"].as_str() {
            Some("acquired") => return Ok(()),
            Some("wait") if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_secs_f64(
                    state["retry_after_seconds"]
                        .as_f64()
                        .unwrap_or(1.0)
                        .clamp(0.01, 2.0),
                ))
            }
            _ => {
                return Err(failure(
                    "rate_limited",
                    "青春科大请求正在冷却，停止当前读取或提交。",
                ))
            }
        }
    }
}

#[derive(Clone)]
struct Armed {
    store: Store,
    id: String,
    expected: String,
    detail: Value,
    job: Value,
}
struct Gate {
    directory: PathBuf,
    armed: Option<Armed>,
    sent: bool,
    deadline: Option<Instant>,
}
fn allowed_read(request: &Value) -> bool {
    let Some(raw) = request["url"].as_str() else {
        return false;
    };
    let Ok(url) = Url::parse(raw) else {
        return false;
    };
    if url.scheme() != "https"
        || url.host_str() != Some(HOST)
        || url.port_or_known_default() != Some(443)
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return false;
    }
    let path = url.path().to_ascii_lowercase();
    if path.contains('%')
        || url.query_pairs().any(|(k, v)| {
            matches!(
                k.to_ascii_lowercase().as_str(),
                "action" | "method" | "operation"
            ) && [
                "delete", "save", "submit", "logout", "approve", "upload", "cancel", "create",
                "enter", "add",
            ]
            .iter()
            .any(|bad| v.to_ascii_lowercase().contains(bad))
        })
    {
        return false;
    }
    if [
        "delete", "save", "submit", "logout", "approve", "upload", "cancel", "create", "/enter/",
        "/add/",
    ]
    .iter()
    .any(|s| path.contains(s))
        || path.ends_with("/add")
    {
        return false;
    }
    let method = request["method"].as_str().unwrap_or("");
    matches!(method, "GET" | "HEAD")
        || method == "POST"
            && [
                "/lib/scItemProgramme/selectProgramme",
                "/item/scItemPlace/list",
            ]
            .iter()
            .any(|p| url.path() == format!("{BASE}{p}"))
}
impl Gate {
    fn request(&mut self, event: &Value) -> Result<Decision> {
        self.check_deadline()?;
        let r = &event["request"];
        let raw = r["url"].as_str().unwrap_or("");
        let parsed = Url::parse(raw).ok();
        if event["resourceType"] == "Document"
            && parsed
                .as_ref()
                .and_then(Url::host_str)
                .is_some_and(|h| matches!(h, "id.ustc.edu.cn" | "passport.ustc.edu.cn"))
        {
            return Err(failure(
                "authentication_required",
                "青春科大会话需要统一身份认证，请在账号页重新登录。",
            ));
        }
        if let (Some(a), Some(url)) = (&self.armed, &parsed) {
            if r["method"] == "POST"
                && url.scheme() == "https"
                && url.host_str() == Some(HOST)
                && url.port_or_known_default() == Some(443)
                && url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.path() == format!("{WRITE}{}", item_id(&a.detail["id"])?)
            {
                if self.sent {
                    return Ok(Decision::Abort);
                }
                // No additional fields are silently supplied by a changed page.
                require(
                    r["postData"]
                        .as_str()
                        .is_none_or(|s| matches!(s.trim(), "" | "{}" | "null")),
                    "报名请求出现额外字段；未提交。",
                )?;
                acquire(&self.directory)?;
                self.check_deadline()?;
                let (current, _) = Store::load(&self.directory)?;
                require(
                    current.account == a.store.account,
                    "报名执行期间账号已变化，未提交。",
                )?;
                policy(
                    "before_write",
                    json!({"detail":a.detail,"job":a.job,"now":now()}),
                )?;
                a.store.transition(
                    &a.id,
                    &a.expected,
                    "submitting",
                    json!({"item_id":a.detail["id"]}),
                )?;
                self.sent = true;
                return Ok(Decision::Continue);
            }
        }
        if !allowed_read(r) {
            return Ok(Decision::Abort);
        }
        if parsed.is_some_and(|u| u.path().starts_with(BASE)) {
            acquire(&self.directory)?;
        }
        Ok(Decision::Continue)
    }
    fn check_deadline(&self) -> Result<()> {
        if self.deadline.is_some_and(|d| Instant::now() >= d) {
            Err(failure(
                "timeout",
                "本次青春科大操作超过时限，保留任务状态，不重复提交。",
            ))
        } else {
            Ok(())
        }
    }
}

struct Browser {
    page: Page,
    gate: Arc<Mutex<Gate>>,
    directory: PathBuf,
    token: Option<String>,
}
impl Browser {
    async fn open(directory: &Path, session: &Value, deadline: Option<Instant>) -> Result<Self> {
        let gate = Arc::new(Mutex::new(Gate {
            directory: directory.to_owned(),
            armed: None,
            sent: false,
            deadline,
        }));
        let capture = gate.clone();
        let mut page = Page::launch(
            &session["state"],
            session["user_agent"].as_str(),
            true,
            Box::new(move |event| capture.lock().map_err(|_| Error::invalid())?.request(event)),
        )
        .await?;
        page.navigate(HOME)?;
        page.wait_expression(
            "document.body && document.body.innerText.includes('欢迎您')",
            20000,
        )?;
        require(
            Url::parse(&page.url()?)
                .ok()
                .is_some_and(|u| u.scheme() == "https" && u.host_str() == Some(HOST)),
            "青春科大会话已失效；请重新连接。",
        )?;
        Ok(Self {
            page,
            gate,
            directory: directory.to_owned(),
            token: None,
        })
    }
    fn check(&self) -> Result<()> {
        self.gate
            .lock()
            .map_err(|_| Error::invalid())?
            .check_deadline()
    }
    fn responses(&mut self) -> Result<()> {
        self.check()?;
        for event in self.page.take_events("Network.responseReceived") {
            let r = &event["response"];
            if Url::parse(r["url"].as_str().unwrap_or(""))
                .ok()
                .is_none_or(|u| u.host_str() != Some(HOST) || !u.path().starts_with(BASE))
            {
                continue;
            }
            self.response(r["status"].as_u64().unwrap_or(0), &r["headers"])?;
        }
        Ok(())
    }
    fn response(&self, status: u64, headers: &Value) -> Result<()> {
        let retry = headers
            .as_object()
            .and_then(|o| {
                o.iter()
                    .find(|(k, _)| k.eq_ignore_ascii_case("retry-after"))
            })
            .and_then(|(_, v)| v.as_str());
        let result = pace(
            &self.directory,
            "response",
            json!({"status":status,"retry_after":retry}),
        )?;
        if status == 401 {
            return Err(failure(
                "authentication_required",
                "青春科大会话失效，请重新登录。",
            ));
        }
        if status == 403 {
            return Err(failure("access_denied", "青春科大拒绝访问。"));
        }
        if result["state"] == "cooldown" {
            return Err(failure(
                "rate_limited",
                "青春科大响应要求冷却，停止后续请求。",
            ));
        }
        require(status == 200, "青春科大响应异常；不重复请求。")
    }
    fn click_text(&mut self, title: &str) -> Result<()> {
        self.check()?;
        let title = serde_json::to_string(title).map_err(|_| Error::invalid())?;
        let expression=format!("(()=>{{const t={title};const visible=e=>!!(e.getClientRects().length);const a=[...document.querySelectorAll('body *')].filter(e=>visible(e)&&e.textContent.trim()===t&&!([...e.children].some(c=>visible(c)&&c.textContent.trim()===t)));if(a.length!==1)throw Error('selector');a[0].click();return true}})()");
        self.page.wait_expression(&format!("(()=>{{const t={title};return [...document.querySelectorAll('body *')].some(e=>e.getClientRects().length&&e.textContent.trim()===t)}})()"),10000)?;
        self.page.evaluate(&expression)?;
        Ok(())
    }
    fn response_json(&mut self, path: &str) -> Result<Value> {
        self.check()?;
        let response = self.page.wait_response(path, 25000)?;
        self.response(
            response["status"].as_u64().unwrap_or(0),
            &response["headers"],
        )?;
        if path == LIST {
            self.token = response["request"]["headers"]
                .as_object()
                .and_then(|o| {
                    o.iter()
                        .find(|(k, _)| k.eq_ignore_ascii_case("x-access-token"))
                })
                .and_then(|(_, v)| v.as_str())
                .map(str::to_owned);
        }
        let body = response["body"]
            .as_str()
            .ok_or_else(|| failure("invalid_response", "青春科大正文格式异常。"))?;
        require(body.len() <= 5 * 1024 * 1024, "青春科大响应过大。")?;
        crate::strict_json::parse(body.as_bytes())
            .map_err(|_| failure("invalid_response", "青春科大正文不是有效 JSON。"))
    }
    fn listing_start(&mut self) -> Result<Value> {
        self.check()?;
        self.page.clear_responses();
        self.page.navigate(HOME)?;
        self.page.wait_expression(
            "document.body && document.body.innerText.includes('欢迎您')",
            15000,
        )?;
        self.click_text("项目管理")?;
        self.click_text("我要报名")?;
        self.page.clear_responses();
        self.click_text("报名中")?;
        self.response_json(LIST)
    }
    fn next_ready(&mut self) -> Result<bool> {
        self.page.wait_expression("[...document.querySelectorAll('.ant-pagination-next')].some(e=>e.getClientRects().length)",5000)?;
        let result=self.page.evaluate("(()=>{const a=[...document.querySelectorAll('.ant-pagination-next')].filter(e=>e.getClientRects().length);if(a.length!==1)throw Error('selector');return !a[0].classList.contains('ant-pagination-disabled')})()")?;
        result.as_bool().ok_or_else(Error::invalid)
    }
    fn next(&mut self) -> Result<Value> {
        require(self.next_ready()?, "请求页码超过当前列表范围。")?;
        self.page.clear_responses();
        self.page.evaluate("(()=>{const a=[...document.querySelectorAll('.ant-pagination-next')].filter(e=>e.getClientRects().length&&!e.classList.contains('ant-pagination-disabled'));if(a.length!==1)throw Error('selector');a[0].click();return true})()")?;
        self.response_json(LIST)
    }
    fn listing(&mut self, page_number: Option<usize>) -> Result<Value> {
        let mut data = self.listing_start()?;
        let mut all = Vec::new();
        let mut seen = HashSet::new();
        for current in 1..=30 {
            self.responses()?;
            let batch = parse("list", json!({"data":data}))?;
            let rows = batch.as_array().ok_or_else(Error::invalid)?;
            let ids = rows.iter().map(|r| r["id"].to_string()).collect::<Vec<_>>();
            require(
                rows.is_empty() || seen.insert(ids),
                "分页没有前进，停止查询。",
            )?;
            let next = self.next_ready()?;
            if page_number == Some(current) {
                let projects = rows
                    .iter()
                    .map(|r| parse("summary", json!({"detail":r})))
                    .collect::<Result<Vec<_>>>()?;
                return Ok(
                    json!({"projects":projects,"page":current,"count":rows.len(),"has_next":next,"next_page":if next&&current<30{Some(current+1)}else{None},"scope":"本人可见报名中项目的本页列表；不代表个人报名资格或全站总数","content_is_untrusted":true}),
                );
            }
            for row in rows {
                let mut row = row.clone();
                row["source_page"] = json!(current);
                all.push(row);
            }
            if !next {
                require(page_number.is_none(), "请求页码超过当前列表范围。")?;
                return Ok(json!(all));
            }
            data = self.next()?;
        }
        Err(failure(
            "invalid_response",
            "活动列表超过读取上限，无法保证匹配唯一。",
        ))
    }
    fn read_api(&mut self, path: &str, id: &str) -> Result<Value> {
        self.check()?;
        require(matches!(path, DETAIL | CHILDREN), "读取路径无效。")?;
        item_id(&json!(id))?;
        let token = self
            .token
            .as_deref()
            .ok_or_else(|| failure("authentication_required", "缺少当前会话请求凭据。"))?;
        let url = format!("https://{HOST}{path}?id={id}");
        let script=format!("(async()=>{{const r=await fetch({},{{method:'GET',headers:{{'X-Access-Token':{}}},credentials:'same-origin',redirect:'error',cache:'no-store',signal:AbortSignal.timeout(12000)}});const t=await r.text();if(t.length>5242880)throw Error('size');return {{status:r.status,headers:Object.fromEntries(r.headers),body:t}}}})()",json!(url),json!(token));
        let response = self.page.evaluate(&script)?;
        self.response(
            response["status"].as_u64().unwrap_or(0),
            &response["headers"],
        )?;
        let data = crate::strict_json::parse(text(&response, "body")?.as_bytes())
            .map_err(|_| failure("invalid_response", "青春科大正文格式异常。"))?;
        parse(
            if path == DETAIL { "detail" } else { "children" },
            json!({"data":data,"item_id":id}),
        )
    }
    fn locate(&mut self, job: &Value, missing: bool) -> Result<Value> {
        let rows = self.listing(None)?;
        let candidates = policy("candidates", json!({"records":rows,"job":job}))?;
        let mut found = candidates["candidates"]
            .as_array()
            .ok_or_else(Error::invalid)?
            .clone();
        if job["exact_name"].is_null() {
            for row in &mut found {
                *row = self.read_api(DETAIL, item_id(&row["id"])?)?;
            }
        }
        let selected = policy("locate", json!({"records":found,"job":job}))?;
        let found = selected["candidates"]
            .as_array()
            .ok_or_else(Error::invalid)?;
        if found.is_empty() && missing {
            return Ok(Value::Null);
        }
        require(
            found.len() == 1,
            "没有唯一的授权活动；可能匹配多个项目，未提交报名。",
        )?;
        let selected = &found[0];
        let id = item_id(&selected["id"])?;
        let title = text(selected, "itemName")?;
        let first=self.page.evaluate("(()=>{const e=[...document.querySelectorAll('.ant-pagination-item-1')].find(e=>e.getClientRects().length);return !!e&&!e.classList.contains('ant-pagination-item-active')})()")?;
        if first == true {
            self.page.clear_responses();
            self.page.evaluate("(()=>{const e=[...document.querySelectorAll('.ant-pagination-item-1')].find(e=>e.getClientRects().length);e.click();return true})()")?;
            self.response_json(LIST)?;
        }
        for _ in 0..30 {
            let expression=format!("[...document.querySelectorAll('body *')].filter(e=>e.getClientRects().length&&e.textContent.trim()==={}&&!([...e.children].some(c=>c.getClientRects().length&&c.textContent.trim()==={}))).length",json!(title),json!(title));
            if self.page.evaluate(&expression)?.as_u64() == Some(1) {
                self.page.clear_responses();
                self.click_text(title)?;
                let data = self.response_json(DETAIL)?;
                let detail = parse("detail", json!({"data":data,"item_id":id}))?;
                self.page.wait_expression("[...document.querySelectorAll('.ant-modal')].some(e=>e.getClientRects().length)",10000)?;
                return Ok(detail);
            }
            self.next()?;
        }
        Err(failure("invalid_response", "无法定位项目页面。"))
    }
    fn ready(&mut self) -> Result<bool> {
        Ok(self.page.evaluate("(()=>{const a=[...document.querySelectorAll('.ant-modal button')].filter(e=>e.getClientRects().length&&e.textContent.replace(/\\s/g,'')==='报名');return a.length===1&&!a[0].disabled})()")?==true)
    }
    fn submit(&mut self, armed: Armed) -> Result<Value> {
        require(self.ready()?, "没有唯一可用的报名按钮。")?;
        let id = item_id(&armed.detail["id"])?.to_owned();
        self.gate.lock().map_err(|_| Error::invalid())?.armed = Some(armed);
        self.page.clear_responses();
        self.page.evaluate("(()=>{const a=[...document.querySelectorAll('.ant-modal button')].filter(e=>e.getClientRects().length&&e.textContent.replace(/\\s/g,'')==='报名'&&!e.disabled);if(a.length!==1)throw Error('selector');a[0].click();return true})()")?;
        self.page.wait_expression("[...document.querySelectorAll('.ant-popover')].some(e=>e.getClientRects().length&&e.innerText.includes('确定报名吗'))",5000)?;
        self.page.evaluate("(()=>{const p=[...document.querySelectorAll('.ant-popover')].filter(e=>e.getClientRects().length&&e.innerText.includes('确定报名吗'));if(p.length!==1)throw Error('confirmation');const a=[...p[0].querySelectorAll('button')].filter(e=>e.getClientRects().length&&e.textContent.replace(/\\s/g,'')==='确定'&&!e.disabled);if(a.length!==1)throw Error('selector');a[0].click();return true})()")?;
        let result = self.response_json(&format!("{WRITE}{id}"))?;
        require(
            self.gate.lock().map_err(|_| Error::invalid())?.sent && result.is_object(),
            "报名响应未确认，禁止重新提交。",
        )?;
        Ok(result)
    }
}

fn stopped(store: &Store, id: &str, error: Error) -> Result<Value> {
    let current = store.view(id)?;
    let state = text(&current, "state")?;
    if matches!(state, "monitoring" | "checking" | "submitting") {
        let mut result = if current["result"].is_object() {
            current["result"].clone()
        } else {
            json!({})
        };
        result["reason"] = json!(error.message);
        result["error_code"] = json!(error.code);
        result["retry_write_allowed"] = json!(false);
        store.transition(
            id,
            state,
            if state == "submitting" {
                "uncertain"
            } else {
                "stopped"
            },
            result,
        )?;
    }
    store.view(id)
}
async fn execute(directory: &Path, id: &str, monitor: bool) -> Result<Value> {
    identifier(id)?;
    let (store, session) = Store::load(directory)?;
    if store.call(
        if monitor { "claim_monitor" } else { "claim" },
        id,
        json!({"now":now()}),
    )? != true
    {
        return store.view(id);
    }
    let outcome=async {
        let mut job=store.view(id)?;let expected=if monitor{"monitoring"}else{"checking"};
        let mut browser=Browser::open(directory,&session,if monitor{None}else{Some(Instant::now()+Duration::from_secs(105))}).await?;let mut failures=0;
        loop {
            if store.view(id)?["state"]!=expected{return store.view(id);}
            if !job["expires_at"].is_null()&&Utc::now()>date(&job["expires_at"])? {store.transition(id,expected,"expired",Value::Null)?;return store.view(id);}
            let detail=match browser.locate(&job,monitor){
                Ok(d)=>{failures=0;d},
                Err(e) if monitor && matches!(e.code,"timeout"|"network_error"|"rate_limited")=>{
                    failures+=1;if failures>3{return Err(e);}
                    let retry=pace(directory,"failure",json!({}))?["retry_after_seconds"].as_f64().unwrap_or(30.0).max(30.0*2f64.powi(failures-1));
                    store.progress(id,json!({"phase":"read_backoff","retry_after_seconds":retry,"checked_at":now()}))?;
                    cancellable(&store,id,retry)?;
                    if store.view(id)?["state"]!="monitoring"{return store.view(id);}
                    let(current,session)=Store::load(directory)?;
                    require(current.account==store.account,"监控期间账号已变化，停止执行。")?;
                    // A CDP policy failure deliberately poisons that page. Reopen only
                    // this pre-write read session after the shared cooldown; never
                    // reuse/reopen a page after entering the submitting state.
                    drop(browser);
                    browser=Browser::open(directory,&session,None).await?;
                    continue;
                }
                Err(e)=>return Err(e),
            };
            if !detail.is_null(){job["item_id"]=detail["id"].clone();job["resolved_name"]=detail["itemName"].clone();}
            let mut decision=policy(if monitor{"monitor"}else{"eligible"},json!({"detail":detail,"job":job,"now":now()}))?;
            if decision=="eligible"&&!browser.ready()?{decision=json!("waiting_for_button");}
            if monitor {store.progress(id,json!({"phase":decision,"checked_at":now(),"item_id":detail["id"]}))?;}
            match decision.as_str(){
                Some("expired")=>{store.transition(id,expected,"expired",Value::Null)?;return store.view(id);}
                Some("already_registered")=>{store.transition(id,expected,"verified",json!({"already_registered":true,"item":parse("summary",json!({"detail":detail}))?}))?;return store.view(id);}
                Some("eligible")=>{
                    let response=browser.submit(Armed{store:store.clone(),id:id.to_owned(),expected:expected.into(),detail:detail.clone(),job:job.clone()})?;
                    let after=browser.read_api(DETAIL,item_id(&detail["id"])?)?;
                    let receipt=policy("receipt",json!({"detail":after,"job":job,"item_id":detail["id"],"response":response}))?;
                    store.transition(id,"submitting",text(&receipt,"state")?,receipt["result"].clone())?;
                    return store.view(id);
                }
                _ if !monitor=>return Err(failure("invalid_arguments","报名按钮尚不可用，本次未提交。")),
                _=>{}
            }
            let mut delay=job["poll_seconds"].as_f64().unwrap_or(10.0);let until=(date(&job["opens_at"])?.with_timezone(&Utc)-Utc::now()).num_milliseconds() as f64/1000.0;
            if until>0.0{delay=delay.min(until);}
            cancellable(&store,id,delay)?;
        }
    }.await;
    match outcome {
        Ok(v) => Ok(v),
        Err(e) => stopped(&store, id, e),
    }
}
fn cancellable(store: &Store, id: &str, seconds: f64) -> Result<()> {
    let end = Instant::now() + Duration::from_secs_f64(seconds.clamp(0.0, 3600.0));
    while Instant::now() < end && store.view(id)?["state"] == "monitoring" {
        std::thread::sleep(
            end.saturating_duration_since(Instant::now())
                .min(Duration::from_secs(2)),
        );
    }
    Ok(())
}

pub async fn dispatch(input: &Value) -> Result<Value> {
    let directory = private_dir(input)?;
    let action = text(input, "action")?;
    if matches!(
        action,
        "install_schedule"
            | "inspect_schedule"
            | "cancel_schedule"
            | "schedule_xml"
            | "runtime_check"
    ) {
        return schedule_dispatch(&directory, input);
    }
    if action == "run" {
        return execute(&directory, text(input, "job_id")?, false).await;
    }
    let (store, session) = Store::load(&directory)?;
    let mut browser = Browser::open(
        &directory,
        &session,
        Some(Instant::now() + Duration::from_secs(105)),
    )
    .await?;
    match action {
        "home" => {
            browser.page.wait_expression(
                "document.body && /数据更新截至时间\\s*\\d{4}年/.test(document.body.innerText)",
                15000,
            )?;
            let body = browser.page.body_text()?;
            let title = browser.page.evaluate("document.title")?;
            parse(
                "home",
                json!({"text":body,"title":title,"max_chars":input["max_chars"]}),
            )
        }
        "list" => {
            let page = input["page"]
                .as_u64()
                .filter(|n| (1..=30).contains(n))
                .ok_or_else(Error::invalid)?;
            browser.listing(Some(page as usize))
        }
        "find" => {
            let keyword = text(input, "keyword")?;
            require(
                (1..=200).contains(&keyword.chars().count()),
                "查询词须为 1–200 字。",
            )?;
            let normalized = policy("normalize", json!({"value":keyword}))?;
            let needle = text(&json!({"s":normalized}), "s")?.to_owned();
            let rows = browser.listing(None)?;
            let mut projects = Vec::new();
            for row in rows.as_array().ok_or_else(Error::invalid)? {
                let name = policy("normalize", json!({"value":row["itemName"]}))?;
                if name.as_str().unwrap_or("").contains(&needle) {
                    let mut result = parse("summary", json!({"detail":row}))?;
                    result["source_page"] = row["source_page"].clone();
                    projects.push(result);
                }
            }
            Ok(json!({"projects":projects,"scope":"当前报名中项目","content_is_untrusted":true}))
        }
        "detail" => {
            let id = item_id(&input["item_id"])?;
            let page = input["page"]
                .as_u64()
                .filter(|n| (1..=30).contains(n))
                .ok_or_else(Error::invalid)?;
            let list = browser.listing(Some(page as usize))?;
            require(
                list["projects"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|r| r["id"] == id)),
                "该页未找到此项目，请重新查询列表和页码。",
            )?;
            let mut detail = browser.read_api(DETAIL, id)?;
            let children = if detail["itemCategory"].to_string().trim_matches('"') == "1" {
                browser.read_api(CHILDREN, id)?
            } else {
                json!([])
            };
            if !input["child_id"].is_null() {
                let child = item_id(&input["child_id"])?;
                require(
                    children
                        .as_array()
                        .is_some_and(|a| a.iter().any(|r| r["id"] == child)),
                    "场次不属于所选系列。",
                )?;
                detail = browser.read_api(DETAIL, child)?;
            }
            let rows = children.as_array().ok_or_else(Error::invalid)?;
            let summaries = rows
                .iter()
                .take(100)
                .map(|r| parse("summary", json!({"detail":r})))
                .collect::<Result<Vec<_>>>()?;
            Ok(
                json!({"project":parse("project",json!({"detail":detail}))?,"parent_id":if input["child_id"].is_null(){Value::Null}else{json!(id)},"children":summaries,"child_count":rows.len(),"children_truncated":rows.len()>100,"source_page":page,"content_is_untrusted":true,"read_only":true,"registration_executed":false,"note":"详情用于核对时间、资格和作品要求；系列、作品及额外资料报名不能套用普通单次报名执行器。"}),
            )
        }
        "verify" => {
            let id = text(input, "job_id")?;
            let job = store.view(id)?;
            if !matches!(job["state"].as_str(), Some("submitting" | "uncertain")) {
                return Ok(job);
            }
            let item = job["result"]["item_id"]
                .as_str()
                .or(job["result"]["item"]["id"].as_str())
                .ok_or_else(|| {
                    failure(
                        "invalid_arguments",
                        "没有可独立核验的活动标识；不可重新提交。",
                    )
                })?;
            browser.listing_start()?;
            let detail = browser.read_api(DETAIL, item)?;
            let receipt = policy(
                "receipt",
                json!({"detail":detail,"job":job,"item_id":item,"response":{}}),
            )?;
            if receipt["state"] == "verified" {
                store.transition(
                    id,
                    text(&job, "state")?,
                    "verified",
                    receipt["result"].clone(),
                )?;
            }
            store.view(id)
        }
        _ => Err(Error::invalid()),
    }
}

fn spec_path(directory: &Path, id: &str) -> Result<PathBuf> {
    Ok(directory
        .join("young-schedules")
        .join(format!("{}.json", identifier(id)?)))
}
fn read_spec(directory: &Path, id: &str) -> Result<Value> {
    let bytes = fs::read(spec_path(directory, id)?)
        .map_err(|_| failure("storage_failure", "本地报名计划不存在或不可读。"))?;
    require(bytes.len() <= 1024 * 1024, "本地报名计划过大。")?;
    let spec = crate::strict_json::parse(&bytes)
        .map_err(|_| failure("storage_failure", "本地计划格式损坏，停止执行。"))?;
    require(
        spec.is_object() && spec["schedule_id"] == id,
        "本地报名计划标识不一致。",
    )?;
    Ok(spec)
}
fn write_spec(directory: &Path, id: &str, spec: &Value) -> Result<()> {
    require(spec["schedule_id"] == id, "计划标识不一致。")?;
    let path = spec_path(directory, id)?;
    let parent = path.parent().ok_or_else(Error::invalid)?;
    fs::create_dir_all(parent).map_err(|_| failure("storage_failure", "本地计划目录无法创建。"))?;
    let temporary = parent.join(format!("{id}.{}.tmp", uuid::Uuid::new_v4().simple()));
    let bytes = serde_json::to_vec_pretty(spec).map_err(|_| Error::invalid())?;
    fs::write(&temporary, bytes)
        .and_then(|_| fs::rename(&temporary, &path))
        .map_err(|_| {
            let _ = fs::remove_file(&temporary);
            failure("storage_failure", "本地计划无法原子保存。")
        })
}
fn lock_spec(directory: &Path, id: &str) -> Result<fs::File> {
    let path = spec_path(directory, id)?.with_extension("lock");
    fs::create_dir_all(path.parent().ok_or_else(Error::invalid)?)
        .map_err(|_| failure("storage_failure", "计划目录无法创建。"))?;
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .map_err(|_| failure("storage_failure", "计划锁无法打开。"))?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        match file.try_lock() {
            Ok(()) => return Ok(file),
            Err(fs::TryLockError::WouldBlock) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(100))
            }
            Err(_) => {
                return Err(failure(
                    "storage_failure",
                    "计划正在更新或锁未释放；保留记录并停止。",
                ))
            }
        }
    }
}
fn update_spec(directory: &Path, id: &str, values: Value) -> Result<Value> {
    let _lock = lock_spec(directory, id)?;
    let mut spec = read_spec(directory, id)?;
    for (key, value) in values.as_object().ok_or_else(Error::invalid)? {
        spec[key] = value.clone();
    }
    write_spec(directory, id, &spec)?;
    Ok(spec)
}
fn executable_hash(path: &Path) -> Result<String> {
    use std::io::Read;
    let mut file =
        fs::File::open(path).map_err(|_| failure("runtime_failure", "报名执行核心不可读。"))?;
    let mut hash = Sha256::new();
    let mut bytes = [0u8; 65536];
    loop {
        let count = file
            .read(&mut bytes)
            .map_err(|_| failure("runtime_failure", "报名执行核心核验失败。"))?;
        if count == 0 {
            break;
        }
        hash.update(&bytes[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn runtime() -> Result<Value> {
    let exe = std::env::current_exe()
        .map_err(|_| failure("runtime_failure", "无法确定报名执行核心。"))?;
    Ok(
        json!({"required":true,"path":exe,"version":env!("CARGO_PKG_VERSION"),"protocol":1,"sha256":executable_hash(&exe)?}),
    )
}
fn check_runtime(spec: &Value) -> Result<()> {
    let pinned = &spec["native_runtime"];
    require(
        pinned["required"] == true
            && pinned["protocol"] == 1
            && pinned["version"] == env!("CARGO_PKG_VERSION"),
        "报名计划核心版本已变更，请在当前应用重新核对计划。",
    )?;
    let path = Path::new(text(pinned, "path")?);
    let exe = std::env::current_exe().map_err(|_| Error::invalid())?;
    require(
        path.is_absolute()
            && fs::canonicalize(path).ok() == fs::canonicalize(&exe).ok()
            && pinned["sha256"] == executable_hash(&exe)?,
        "报名计划执行文件缺失或已变更；停止，不回退其他程序。",
    )
}
fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}
fn windows_quote(value: &str) -> String {
    // Microsoft C argv quoting, including trailing backslashes before the quote.
    let mut out = String::from("\"");
    let mut slashes = 0;
    for c in value.chars() {
        if c == '\\' {
            slashes += 1;
            continue;
        }
        if c == '"' {
            out.push_str(&"\\".repeat(slashes * 2 + 1));
        } else {
            out.push_str(&"\\".repeat(slashes));
        }
        slashes = 0;
        out.push(c);
    }
    out.push_str(&"\\".repeat(slashes * 2));
    out.push('"');
    out
}
fn task_xml(directory: &Path, id: &str, spec: &Value) -> Result<String> {
    identifier(id)?;
    check_runtime(spec)?;
    let exe = Path::new(text(&spec["native_runtime"], "path")?);
    let args = format!(
        "--young-worker --schedule-id {id} --private-dir {}",
        windows_quote(&directory.to_string_lossy())
    );
    let start = date(&spec["monitor_from"])?.to_rfc3339();
    Ok(format!(
        r#"<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>School MCP native Rust registration worker; no AI runtime.</Description></RegistrationInfo>
<Triggers><TimeTrigger><StartBoundary>{}</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
<Principals><Principal id="Author"><UserId>__CURRENT_USER_SID__</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><Hidden>true</Hidden><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>
<Actions Context="Author"><Exec><Command>{}</Command><Arguments>{}</Arguments><WorkingDirectory>{}</WorkingDirectory></Exec></Actions></Task>"#,
        xml_escape(&start),
        xml_escape(&exe.to_string_lossy()),
        xml_escape(&args),
        xml_escape(&exe.parent().ok_or_else(Error::invalid)?.to_string_lossy())
    ))
}
#[cfg(windows)]
fn powershell(script: &str) -> Result<Value> {
    use base64::Engine;
    use std::{
        io::Read,
        os::windows::process::CommandExt,
        process::{Command, Stdio},
    };
    let encoded = base64::engine::general_purpose::STANDARD.encode(
        script
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    let mut child = Command::new("powershell.exe")
        .args(["-NoProfile", "-NonInteractive", "-EncodedCommand", &encoded])
        .creation_flags(0x08000000)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| failure("runtime_failure", "Windows 计划任务程序无法启动。"))?;
    let deadline = Instant::now() + Duration::from_secs(30);
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(100)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(failure("timeout", "Windows 计划任务操作超时；请核对状态。"));
            }
        }
    };
    require(status.success(), "Windows 计划任务操作失败；未提升权限。")?;
    let mut bytes = Vec::new();
    child
        .stdout
        .take()
        .ok_or_else(Error::invalid)?
        .take(65537)
        .read_to_end(&mut bytes)
        .map_err(|_| Error::invalid())?;
    require(bytes.len() <= 65536, "Windows 计划任务响应过大。")?;
    let bytes = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
    crate::strict_json::parse(bytes)
        .map_err(|_| failure("runtime_failure", "无法核验 Windows 计划任务结果。"))
}
#[cfg(not(windows))]
fn powershell(_script: &str) -> Result<Value> {
    Err(failure(
        "unsupported_platform",
        "本机原生自动触发暂仅支持 Windows 计划任务。",
    ))
}
fn task_name(id: &str) -> Result<String> {
    Ok(format!("SchoolMCP-Young-{}", identifier(id)?))
}
fn inspect(directory: &Path, id: &str) -> Result<Value> {
    let spec = read_spec(directory, id)?;
    let mut result = if spec["scheduler"] == "background-rust"
        || spec["scheduler"] == "background-python"
    {
        let pid = spec["pid"]
            .as_u64()
            .filter(|p| *p > 0 && *p <= u32::MAX as u64)
            .ok_or_else(Error::invalid)?;
        let process=powershell(&format!("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; if($p){{@{{running=$true;created=$p.StartTime.ToUniversalTime().ToString('o');started=([DateTimeOffset]$p.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()/1000.0}}|ConvertTo-Json -Compress}}else{{@{{running=$false}}|ConvertTo-Json -Compress}}"))?;
        let same_process = if spec["scheduler"] == "background-python" {
            process["started"]
                .as_f64()
                .zip(spec["process_started_at"].as_f64())
                .is_some_and(|(a, b)| (a - b).abs() < 0.1)
        } else {
            process["created"] == spec["process_created"]
        };
        let alive = process["running"] == true && same_process;
        json!({"scheduler":spec["scheduler"],"enabled":alive&&spec["cancelled"]!=true,"process_running":alive,"survives_reboot":false})
    } else {
        let name = task_name(id)?;
        let mut result=powershell(&format!("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();$t=Get-ScheduledTask -TaskName '{name}';$i=$t|Get-ScheduledTaskInfo;@{{task_name=$t.TaskName;state=[string]$t.State;enabled=[bool]$t.Settings.Enabled;start_boundary=$t.Triggers[0].StartBoundary;command=$t.Actions[0].Execute;arguments=$t.Actions[0].Arguments;last_result=$i.LastTaskResult}}|ConvertTo-Json -Compress"))?;
        result["scheduler"] = json!("windows-task-scheduler");
        result
    };
    for key in ["job_id", "worker_state", "monitor_from", "opens_at"] {
        result[key] = spec[key].clone();
    }
    result["schedule_id"] = json!(id);
    result["uses_ai"] = json!(false);
    result["executor"] = spec["executor"].clone();
    Ok(result)
}
fn disable(id: &str) -> Result<()> {
    let name = task_name(id)?;
    powershell(&format!("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();Disable-ScheduledTask -TaskName '{name}'|Out-Null;@{{disabled=$true}}|ConvertTo-Json -Compress"))?;
    Ok(())
}
#[cfg(windows)]
fn background(directory: &Path, id: &str) -> Result<Value> {
    use std::{
        os::windows::process::CommandExt,
        process::{Command, Stdio},
    };
    let spec = read_spec(directory, id)?;
    check_runtime(&spec)?;
    let exe = std::env::current_exe().map_err(|_| Error::invalid())?;
    update_spec(
        directory,
        id,
        json!({"scheduler":"background-rust","scheduler_warning":"系统计划任务未成功核验，使用驻留 Rust 等待；不跨电脑重启。"}),
    )?;
    let mut child = Command::new(exe)
        .args(["--young-worker", "--schedule-id", id, "--private-dir"])
        .arg(directory)
        .creation_flags(0x08000000 | 0x00000008)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| failure("runtime_failure", "系统不允许独立后台进程；未安排成功。"))?;
    let pid = child.id();
    let process=powershell(&format!("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();$p=Get-Process -Id {pid};@{{created=$p.StartTime.ToUniversalTime().ToString('o')}}|ConvertTo-Json -Compress"));
    let process = match process {
        Ok(v) => v,
        Err(e) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(e);
        }
    };
    update_spec(
        directory,
        id,
        json!({"pid":pid,"process_created":process["created"]}),
    )?;
    let inspected = inspect(directory, id)?;
    if inspected["process_running"] != true {
        let _ = child.kill();
        let _ = child.wait();
        return Err(failure(
            "runtime_failure",
            "后台原生进程未运行，不能视为已安排。",
        ));
    }
    Ok(inspected)
}
#[cfg(not(windows))]
fn background(_directory: &Path, _id: &str) -> Result<Value> {
    Err(failure(
        "unsupported_platform",
        "原生计划调度尚未在此平台实现。",
    ))
}
fn schedule_dispatch(directory: &Path, input: &Value) -> Result<Value> {
    let action = text(input, "action")?;
    if action == "runtime_check" {
        check_runtime(&input["spec"])?;
        return Ok(json!({"valid":true,"uses_python":false}));
    }
    if action == "schedule_xml" {
        let id = text(input, "schedule_id")?;
        let spec = json!({"native_runtime":runtime()?,"monitor_from":input["monitor_from"]});
        return Ok(json!({"xml":task_xml(directory,id,&spec)?}));
    }
    if action == "inspect_schedule" {
        return inspect(directory, text(input, "schedule_id")?);
    }
    if action == "cancel_schedule" {
        let id = text(input, "schedule_id")?;
        let spec = read_spec(directory, id)?;
        if let Some(job) = spec["job_id"].as_str() {
            let (store, _) = Store::load(directory)?;
            let view = store.view(job)?;
            if matches!(
                view["state"].as_str(),
                Some("scheduled" | "monitoring" | "checking")
            ) {
                store.call("cancel", job, json!({}))?;
            } else {
                require(
                    !matches!(view["state"].as_str(), Some("submitting" | "uncertain")),
                    "报名已提交或结果未知，不能用取消计划撤回。",
                )?;
            }
        }
        update_spec(directory, id, json!({"cancelled":true}))?;
        if spec["scheduler"] != "background-rust" && spec["scheduler"] != "background-python" {
            disable(id)?;
        }
        return inspect(directory, id);
    }
    require(action == "install_schedule", "计划操作无效。")?;
    policy(
        "schedule",
        json!({"monitor_from":input["monitor_from"],"opens_at":input["opens_at"],"now":now()}),
    )?;
    let id = input["schedule_id"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
    identifier(&id)?;
    let (store, _) = Store::load(directory)?;
    if let Some(job) = input["job_id"].as_str() {
        let view = store.view(job)?;
        require(
            view["state"] == "scheduled",
            "仅可安排尚未执行的报名任务；保留未知结果。",
        )?;
        require(
            date(&view["opens_at"])? == date(&input["opens_at"])?
                && date(&view["monitor_from"])? == date(&input["monitor_from"])?,
            "计划时间与已授权报名任务不一致。",
        )?;
    }
    {
        let _lock = lock_spec(directory, &id)?;
        if spec_path(directory, &id)?.exists() {
            let old = read_spec(directory, &id)?;
            require(
                old["job_id"].is_null() || old["job_id"] == input["job_id"],
                "不能将已有计划改绑到另一个报名任务。",
            )?;
            require(
                old["cancelled"] == true,
                "现有计划须先取消再重建，避免并行等待进程。",
            )?;
        }
        let spec = json!({"schedule_id":id,"monitor_from":date(&input["monitor_from"])?.to_rfc3339(),"opens_at":date(&input["opens_at"])?.to_rfc3339(),"job_id":input["job_id"],"worker_state":if input["job_id"].is_null(){"needs_exact_name"}else{"scheduled"},"cancelled":false,"native_runtime":runtime()?,"executor":"rust","scheduler":"windows-task-scheduler","account_sha256":store.account});
        write_spec(directory, &id, &spec)?;
    }
    let spec = read_spec(directory, &id)?;
    let xml = task_xml(directory, &id, &spec)?;
    use base64::Engine;
    let data = base64::engine::general_purpose::STANDARD.encode(xml.as_bytes());
    let name = task_name(&id)?;
    let installed=powershell(&format!("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();$xml=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{data}'));$xml=$xml.Replace('__CURRENT_USER_SID__',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value);Register-ScheduledTask -TaskName '{name}' -Xml $xml -Force|Out-Null;@{{installed=$true}}|ConvertTo-Json -Compress"));
    if installed.is_ok() {
        match inspect(directory, &id) {
            Ok(result)
                if result["enabled"] == true
                    && date(&result["start_boundary"])? == date(&spec["monitor_from"])? =>
            {
                return Ok(result)
            }
            _ => disable(&id)?,
        }
    } else {
        // Registration may have completed before a transport/inspection error. Disable
        // any task under our exact id before starting the alternative worker.
        let name = task_name(&id)?;
        powershell(&format!("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.UTF8Encoding]::new();$t=Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue;if($t){{$t|Disable-ScheduledTask|Out-Null}};@{{disabled=$true}}|ConvertTo-Json -Compress"))?;
    }
    background(directory, &id)
}

/// Executed only by the fixed --young-worker CLI. Never interprets model code.
pub async fn run_schedule(directory: &Path, id: &str) -> Result<()> {
    identifier(id)?;
    private_dir(&json!({"directory":directory}))?;
    let result: Result<()> = async {
        let mut spec = read_spec(directory, id)?;
        check_runtime(&spec)?;
        require(
            spec["executor"] == "rust",
            "旧计划须重新核对执行核心，不能自动切换执行器。",
        )?;
        let (store, _) = Store::load(directory)?;
        require(
            spec["account_sha256"] == store.account,
            "计划账号与当前统一身份不一致，停止执行。",
        )?;
        loop {
            if spec["cancelled"] == true {
                return Ok(());
            }
            if Utc::now() >= date(&spec["monitor_from"])? {
                break;
            }
            std::thread::sleep(Duration::from_secs(2));
            spec = read_spec(directory, id)?;
        }
        update_spec(
            directory,
            id,
            json!({"worker_started_at":now(),"worker_state":"starting"}),
        )?;
        loop {
            spec = read_spec(directory, id)?;
            if spec["cancelled"] == true {
                return Ok(());
            }
            check_runtime(&spec)?;
            let (current, _) = Store::load(directory)?;
            require(
                current.account == store.account && spec["account_sha256"] == store.account,
                "计划账号已变化，停止执行。",
            )?;
            if let Some(job) = spec["job_id"].as_str() {
                let result = execute(directory, job, true).await?;
                update_spec(
                    directory,
                    id,
                    json!({"worker_state":result["state"],"finished_at":now()}),
                )?;
                return Ok(());
            }
            update_spec(directory, id, json!({"worker_state":"needs_exact_name"}))?;
            std::thread::sleep(Duration::from_secs(10));
        }
    }
    .await;
    if let Err(error) = result {
        let _ = update_spec(
            directory,
            id,
            json!({"worker_state":"stopped","reason":error.message,"error_code":error.code,"finished_at":now()}),
        );
        return Err(error);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Temp(PathBuf);
    impl Temp {
        fn new() -> Self {
            let p = std::env::temp_dir()
                .join(format!("school-young-io-{}", uuid::Uuid::new_v4().simple()));
            fs::create_dir_all(&p).unwrap();
            Self(p)
        }
    }
    impl Drop for Temp {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn request(method: &str, path: &str) -> Value {
        json!({"url":format!("https://{HOST}{path}"),"method":method})
    }
    fn synthetic_store(directory: &Path) -> Store {
        crate::storage::private_write(&json!({"path":directory.join("ustc-identity.credentials.dpapi"),"value":{"username":"synthetic-account","password":"","session_only":true,"saved_at":now()}})).unwrap();
        crate::storage::private_write(&json!({"path":directory.join("young-session.dpapi"),"value":{"account":"synthetic-account","state":{"cookies":[],"origins":[]},"saved_at":now()}})).unwrap();
        Store {
            directory: directory.to_owned(),
            account: format!("{:x}", Sha256::digest(b"synthetic-account")),
        }
    }
    fn detail(due: DateTime<Utc>) -> Value {
        json!({"id":"synthetic-event","itemName":"合成测试活动","applySt":due.to_rfc3339_opts(chrono::SecondsFormat::Micros,false),"applyEt":(due+chrono::Duration::hours(1)).to_rfc3339_opts(chrono::SecondsFormat::Micros,false),"booleanRegistration":0,"itemCategory":"0","form":"0","needSignInfo":"0","onlineStatus":0,"marathon":"0","signInfo":0,"examineStatus":10,"applyStatus":26,"baseContent":"线下报告","conceive":""})
    }
    fn create(store: &Store, due: DateTime<Utc>, name: &str) -> Value {
        young::dispatch("young_job",&json!({"action":"create","database":store.directory.join("young-registrations.sqlite3"),"account":store.account,"exact_name":name,"opens_at":due.to_rfc3339(),"authorized":true,"now":(due-chrono::Duration::minutes(10)).to_rfc3339(),"monitor_from":(due-chrono::Duration::minutes(2)).to_rfc3339(),"poll_seconds":10})).unwrap()
    }
    #[test]
    fn request_allowlist_rejects_writes_aliases_and_cross_origin() {
        assert!(allowed_read(&request("GET", LIST)));
        assert!(allowed_read(&request(
            "POST",
            &format!("{BASE}/lib/scItemProgramme/selectProgramme")
        )));
        for (method, path) in [
            ("POST", LIST),
            ("PUT", DETAIL),
            ("GET", "/logout"),
            ("GET", "/item/%65nter/x"),
            ("GET", "/item?method=delete"),
            ("POST", "/different/lib/scItemProgramme/selectProgramme"),
        ] {
            assert!(!allowed_read(&request(method, path)), "{method} {path}");
        }
        for url in [
            "http://young.ustc.edu.cn/",
            "https://young.ustc.edu.cn.evil.invalid/",
            "https://young.ustc.edu.cn:8443/",
            "https://x@young.ustc.edu.cn/",
        ] {
            assert!(!allowed_read(&json!({"method":"GET","url":url})));
        }
    }
    #[test]
    fn schedule_paths_and_windows_quoting_are_bounded() {
        let t = Temp::new();
        assert!(spec_path(&t.0, "../bad").is_err());
        assert!(private_dir(&json!({"directory":"relative"})).is_err());
        assert_eq!(windows_quote("C:\\x y\\"), "\"C:\\x y\\\\\"");
        assert_eq!(windows_quote("x\"y"), "\"x\\\"y\"");
        let id = "a".repeat(32);
        write_spec(&t.0, &id, &json!({"schedule_id":id,"cancelled":false})).unwrap();
        update_spec(&t.0, &id, json!({"cancelled":true})).unwrap();
        update_spec(&t.0, &id, json!({"worker_state":"stopped"})).unwrap();
        assert_eq!(read_spec(&t.0, &id).unwrap()["cancelled"], true);
    }
    #[test]
    fn native_task_xml_uses_current_core_only() {
        let t = Temp::new();
        let spec =
            json!({"monitor_from":Utc::now().to_rfc3339(),"native_runtime":runtime().unwrap()});
        let xml = task_xml(&t.0, &"a".repeat(32), &spec).unwrap();
        assert!(xml.contains("--young-worker --schedule-id"));
        assert!(!xml.contains("python"));
        assert!(xml.contains("LeastPrivilege"));
        assert!(xml.contains("IgnoreNew"));
        let mut wrong = spec.clone();
        wrong["native_runtime"]["sha256"] = json!("0".repeat(64));
        assert!(check_runtime(&wrong).is_err());
        wrong = spec;
        wrong["native_runtime"]["version"] = json!("0.6.0");
        assert!(check_runtime(&wrong).is_err());
    }
    #[test]
    #[cfg(windows)]
    fn request_gate_claims_once_and_unexpected_body_stops_before_claim() {
        let t = Temp::new();
        let store = synthetic_store(&t.0);
        let due = Utc::now() - chrono::Duration::seconds(1);
        let job = create(&store, due, "合成测试活动");
        let id = text(&job, "job_id").unwrap();
        assert_eq!(store.call("claim", id, json!({"now":now()})).unwrap(), true);
        let armed = Armed {
            store: store.clone(),
            id: id.into(),
            expected: "checking".into(),
            detail: detail(due),
            job,
        };
        let mut gate = Gate {
            directory: t.0.clone(),
            armed: Some(armed),
            sent: false,
            deadline: None,
        };
        let mut request = request("POST", &format!("{WRITE}synthetic-event"));
        request["postData"] = json!("{\"extra\":true}");
        assert!(gate.request(&json!({"request":request})).is_err());
        assert_eq!(
            store.view(&gate.armed.as_ref().unwrap().id).unwrap()["state"],
            "checking"
        );
        request["postData"] = json!("{}");
        assert!(matches!(
            gate.request(&json!({"request":request})).unwrap(),
            Decision::Continue
        ));
        assert!(matches!(
            gate.request(&json!({"request":request})).unwrap(),
            Decision::Abort
        ));
        let id = gate.armed.as_ref().unwrap().id.clone();
        store
            .transition(
                &id,
                "submitting",
                "uncertain",
                json!({"item_id":"synthetic-event"}),
            )
            .unwrap();
        gate.sent = false;
        assert!(gate.request(&json!({"request":request})).is_err());
        assert_eq!(store.view(&id).unwrap()["state"], "uncertain");
    }
    #[test]
    #[cfg(windows)]
    fn deadline_and_account_switch_stop_before_write() {
        let t = Temp::new();
        let store = synthetic_store(&t.0);
        let due = Utc::now() - chrono::Duration::seconds(1);
        let job = create(&store, due, "合成测试活动");
        let id = text(&job, "job_id").unwrap().to_owned();
        store.call("claim", &id, json!({"now":now()})).unwrap();
        let armed = Armed {
            store: store.clone(),
            id: id.clone(),
            expected: "checking".into(),
            detail: detail(due),
            job,
        };
        let mut gate = Gate {
            directory: t.0.clone(),
            armed: Some(armed),
            sent: false,
            deadline: Some(Instant::now() - Duration::from_secs(1)),
        };
        let request = json!({"request":request("POST",&format!("{WRITE}synthetic-event"))});
        assert_eq!(gate.request(&request).err().unwrap().code, "timeout");
        assert!(!t.0.join("request-policy.sqlite3").exists());
        gate.deadline = None;
        crate::storage::private_write(&json!({"path":t.0.join("ustc-identity.credentials.dpapi"),"value":{"username":"different-account","password":"","session_only":true,"saved_at":now()}})).unwrap();
        assert!(gate.request(&request).is_err());
        assert_eq!(store.view(&id).unwrap()["state"], "checking");
        assert!(!gate.sent);
    }
    #[test]
    #[cfg(windows)]
    fn cancelled_monitor_wait_returns_without_sleep() {
        let t = Temp::new();
        let store = synthetic_store(&t.0);
        let due = Utc::now() + chrono::Duration::seconds(30);
        let job = create(&store, due, "合成测试活动");
        let id = text(&job, "job_id").unwrap();
        assert_eq!(
            store
                .call("claim_monitor", id, json!({"now":now()}))
                .unwrap(),
            true
        );
        store.call("cancel", id, json!({})).unwrap();
        let start = Instant::now();
        cancellable(&store, id, 600.0).unwrap();
        assert!(start.elapsed() < Duration::from_secs(1));
    }
    #[test]
    #[cfg(windows)]
    fn changed_runtime_worker_stops_before_account_read() {
        let t = Temp::new();
        let id = "b".repeat(32);
        write_spec(&t.0,&id,&json!({"schedule_id":id,"executor":"rust","cancelled":false,"native_runtime":{"required":true,"protocol":1,"version":"old","path":"missing"}})).unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        assert!(runtime.block_on(run_schedule(&t.0, &id)).is_err());
        let spec = read_spec(&t.0, &id).unwrap();
        assert_eq!(spec["worker_state"], "stopped");
        assert!(!t.0.join("young-registrations.sqlite3").exists());
    }
    #[test]
    #[ignore = "explicit headless Chrome synthetic smoke; no school network"]
    #[cfg(windows)]
    fn headless_young_submission_and_receipt_are_native() {
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
        let t = Temp::new();
        let store = synthetic_store(&t.0);
        let due = Utc::now() - chrono::Duration::seconds(1);
        let job = create(&store, due, "合成测试活动");
        let id = text(&job, "job_id").unwrap().to_owned();
        assert_eq!(
            store.call("claim", &id, json!({"now":now()})).unwrap(),
            true
        );
        let fixture = detail(due);
        let registered = Arc::new(AtomicBool::new(false));
        let writes = Arc::new(AtomicUsize::new(0));
        let gate = Arc::new(Mutex::new(Gate {
            directory: t.0.clone(),
            armed: None,
            sent: false,
            deadline: None,
        }));
        let capture = gate.clone();
        let registered_capture = registered.clone();
        let writes_capture = writes.clone();
        let fixture_capture = fixture.clone();
        let html = format!(
            r#"<!doctype html><meta charset="utf-8"><title>Synthetic Young</title><div>欢迎您</div><button id="projects">项目管理</button><button id="apply">我要报名</button><button id="listing">报名中</button><div id="rows"></div><ul><li class="ant-pagination-item-1 ant-pagination-item-active">1</li><li class="ant-pagination-next ant-pagination-disabled">下一页</li></ul><div class="ant-modal" style="display:none"><button id="register">报 名</button></div><div class="ant-popover" style="display:none">确定报名吗<button id="confirm">确 定</button></div><script>
document.querySelector('#listing').onclick=async()=>{{const r=await fetch('{LIST}',{{headers:{{'X-Access-Token':'synthetic-token'}}}});await r.json();document.querySelector('#rows').innerHTML='<button id="event">合成测试活动</button>';document.querySelector('#event').onclick=async()=>{{await fetch('{DETAIL}?id=synthetic-event');document.querySelector('.ant-modal').style.display='block'}}}};
document.querySelector('#register').onclick=()=>document.querySelector('.ant-popover').style.display='block';
document.querySelector('#confirm').onclick=async()=>{{await fetch('{WRITE}synthetic-event',{{method:'POST',body:'{{}}'}})}};
</script>"#
        );
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let page=Page::launch(&json!({"cookies":[],"origins":[]}),None,true,Box::new(move |event|{
                let decision=capture.lock().unwrap().request(event)?;
                if !matches!(decision,Decision::Continue){return Ok(decision);}
                let url=Url::parse(event["request"]["url"].as_str().unwrap()).unwrap();
                let (content_type,body)=if url.path()==Url::parse(HOME).unwrap().path(){("text/html",html.clone().into_bytes())}
                else if url.path()==LIST{("application/json",serde_json::to_vec(&json!({"success":true,"result":{"records":[fixture_capture.clone()]}})).unwrap())}
                else if url.path()==DETAIL{let mut after=fixture_capture.clone();after["booleanRegistration"]=json!(if registered_capture.load(Ordering::SeqCst){1}else{0});("application/json",serde_json::to_vec(&json!({"success":true,"result":after})).unwrap())}
                else if url.path()==format!("{WRITE}synthetic-event"){writes_capture.fetch_add(1,Ordering::SeqCst);registered_capture.store(true,Ordering::SeqCst);("application/json",br#"{"success":true}"#.to_vec())}
                else{return Ok(Decision::Abort);};
                Ok(Decision::Fulfill{status:200,headers:vec![("Content-Type".into(),content_type.into())],body})
            })).await.unwrap();
            let mut browser=Browser{page,gate,directory:t.0.clone(),token:None};
            let selected=browser.locate(&job,false).unwrap();assert_eq!(selected["id"],"synthetic-event");
            let response=browser.submit(Armed{store:store.clone(),id:id.clone(),expected:"checking".into(),detail:selected,job:job.clone()}).unwrap();
            let after=browser.read_api(DETAIL,"synthetic-event").unwrap();
            let receipt=policy("receipt",json!({"detail":after,"job":job,"item_id":"synthetic-event","response":response})).unwrap();
            assert_eq!(receipt["state"],"verified");store.transition(&id,"submitting","verified",receipt["result"].clone()).unwrap();
            assert_eq!(writes.load(Ordering::SeqCst),1);assert_eq!(store.view(&id).unwrap()["state"],"verified");
            assert_eq!(store.call("claim",&id,json!({"now":now()})).unwrap(),false);
        });
    }
}
