//! Internal Chrome page driver. The pipe never accepts JavaScript or CDP commands.
//! Each page owns its Chrome process, context, request policy, and bounded event queue.
use crate::{browser, Error, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    net::{SocketAddr, TcpStream},
    time::{Duration, Instant},
};
use tungstenite::{Message, WebSocket};

const MAX_BYTES: usize = 16 * 1024 * 1024;
const MAX_EVENTS: usize = 1024;
// These adapters only use guarded fetch/XHR. Chrome's Fetch and URL-blocking
// domains do not intercept WebSockets. Disable unused streaming constructors
// in the main world before site scripts, including every document/frame.
const NO_STREAMING: &str = "(()=>{for(const n of ['WebSocket','WebSocketStream','WebTransport'])if(typeof globalThis[n]==='function')Object.defineProperty(globalThis,n,{value:class{constructor(){throw new DOMException('Unavailable in this adapter','SecurityError')}},writable:false,configurable:false});})()";
fn target_filter() -> Value {
    json!([
        {"type":"page"},{"type":"worker"},{"type":"service_worker"},
        {"type":"shared_worker"},{"type":"iframe"},{"exclude":true}
    ])
}
pub enum Decision {
    Continue,
    Abort,
    Fulfill {
        status: u16,
        headers: Vec<(String, String)>,
        body: Vec<u8>,
    },
}
pub type Guard = Box<dyn FnMut(&Value) -> Result<Decision> + Send>;
fn failed() -> Error {
    Error::new(
        "browser_unavailable",
        "后台页面操作未完成；没有自动重放请求，请核对原任务状态。",
    )
}
fn timeout() -> Error {
    Error::new(
        "timeout",
        "后台页面等待超时；原任务状态保留，没有自动重新提交。",
    )
}

pub struct Page {
    _owner: browser::Manager,
    socket: WebSocket<TcpStream>,
    session: String,
    target: String,
    context: String,
    next_id: u64,
    fire_ids: HashSet<u64>,
    child_sessions: HashSet<String>,
    child_resumes: HashMap<u64, String>,
    replies: HashMap<u64, Value>,
    events: VecDeque<Value>,
    requests: HashMap<String, Value>,
    observed_bytes: usize,
    guard: Guard,
    policy_error: Option<Error>,
    bootstrap: Option<String>,
    login_mode: bool,
    login_contexts: HashMap<i64, String>,
    login_accounts: Vec<String>,
}
impl Page {
    pub async fn launch(
        state: &Value,
        user_agent: Option<&str>,
        headless: bool,
        guard: Guard,
    ) -> Result<Self> {
        let owner = browser::Manager::new();
        let started = owner.dispatch("browser_start", &json!({"headless":headless}))?;
        let endpoint = started["endpoint"].as_str().ok_or_else(failed)?;
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(5))
            .build()
            .map_err(|_| failed())?;
        let meta: Value = client
            .get(format!("{endpoint}/json/version"))
            .send()
            .await
            .map_err(|_| failed())?
            .json()
            .await
            .map_err(|_| failed())?;
        let ws = meta["webSocketDebuggerUrl"].as_str().ok_or_else(failed)?;
        let parsed = url::Url::parse(ws).map_err(|_| failed())?;
        let port = url::Url::parse(endpoint)
            .map_err(|_| failed())?
            .port()
            .ok_or_else(failed)?;
        if parsed.scheme() != "ws"
            || parsed.host_str() != Some("127.0.0.1")
            || parsed.port() != Some(port)
            || !parsed.path().starts_with("/devtools/browser/")
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || parsed.query().is_some()
            || parsed.fragment().is_some()
        {
            return Err(failed());
        }
        let stream = TcpStream::connect_timeout(
            &SocketAddr::from(([127, 0, 0, 1], port)),
            Duration::from_secs(5),
        )
        .map_err(|_| failed())?;
        stream
            .set_read_timeout(Some(Duration::from_millis(200)))
            .map_err(|_| failed())?;
        stream
            .set_write_timeout(Some(Duration::from_secs(5)))
            .map_err(|_| failed())?;
        let config = tungstenite::protocol::WebSocketConfig::default()
            .max_message_size(Some(MAX_BYTES))
            .max_frame_size(Some(MAX_BYTES));
        let (socket, _) = tungstenite::client::client_with_config(ws, stream, Some(config))
            .map_err(|_| failed())?;
        let mut page = Self {
            _owner: owner,
            socket,
            session: String::new(),
            target: String::new(),
            context: String::new(),
            next_id: 0,
            fire_ids: HashSet::new(),
            child_sessions: HashSet::new(),
            child_resumes: HashMap::new(),
            replies: HashMap::new(),
            events: VecDeque::new(),
            requests: HashMap::new(),
            observed_bytes: 0,
            guard,
            policy_error: None,
            bootstrap: None,
            login_mode: false,
            login_contexts: HashMap::new(),
            login_accounts: Vec::new(),
        };
        page.context = page.command(
            "Target.createBrowserContext",
            json!({"disposeOnDetach":true}),
        )?["browserContextId"]
            .as_str()
            .ok_or_else(failed)?
            .into();
        let target = page.command(
            "Target.createTarget",
            json!({"url":"about:blank","browserContextId":page.context}),
        )?;
        page.target = target["targetId"].as_str().ok_or_else(failed)?.into();
        page.session = page.command(
            "Target.attachToTarget",
            json!({"targetId":target["targetId"],"flatten":true}),
        )?["sessionId"]
            .as_str()
            .ok_or_else(failed)?
            .into();
        // Chrome's own UI target is not web content and cannot be paused. Keep
        // it (and the browser/tab containers) out of the child-content policy.
        let auto_attach = json!({"autoAttach":true,"waitForDebuggerOnStart":true,"flatten":true,
            "filter":target_filter()});
        page.command("Target.setAutoAttach", auto_attach.clone())?;
        page.command_scoped("Target.setAutoAttach", auto_attach, true)?;
        page.command("Page.enable", json!({}))?;
        page.command(
            "Page.addScriptToEvaluateOnNewDocument",
            json!({"source":NO_STREAMING}),
        )?;
        page.command("Page.setLifecycleEventsEnabled", json!({"enabled":true}))?;
        page.command("Runtime.enable", json!({}))?;
        page.command("Network.enable", json!({"maxTotalBufferSize":MAX_BYTES,"maxResourceBufferSize":8*1024*1024,"maxPostDataSize":8*1024*1024}))?;
        page.command("Network.setBypassServiceWorker", json!({"bypass":true}))?;
        page.command("Network.setCacheDisabled", json!({"cacheDisabled":true}))?;
        page.command(
            "Network.setBlockedURLs",
            json!({"urls":["ws://*","wss://*"]}),
        )?;
        page.command(
            "Browser.setDownloadBehavior",
            json!({"behavior":"deny","browserContextId":page.context}),
        )?;
        page.command("Page.setDownloadBehavior", json!({"behavior":"deny"}))?;
        page.command(
            "Emulation.setTimezoneOverride",
            json!({"timezoneId":"Asia/Shanghai"}),
        )?;
        if let Some(agent) = user_agent.filter(|v| !v.is_empty()) {
            if agent.len() > 2048 || agent.contains(['\r', '\n']) {
                return Err(Error::invalid());
            }
            page.command(
                "Network.setUserAgentOverride",
                json!({"userAgent":agent,"acceptLanguage":"zh-CN,zh;q=0.9"}),
            )?;
        }
        page.command(
            "Emulation.setDeviceMetricsOverride",
            json!({"width":1280,"height":900,"deviceScaleFactor":1,"mobile":false}),
        )?;
        page.command("Fetch.enable", json!({"patterns":[{"urlPattern":"*","requestStage":"Request"}],"handleAuthRequests":true}))?;
        let cookies = state
            .get("cookies")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if cookies.len() > 5000 {
            return Err(Error::invalid());
        }
        let now = chrono::Utc::now().timestamp() as f64;
        let cookies: Vec<Value> = cookies
            .into_iter()
            .filter_map(|cookie| {
                if cookie["expires"]
                    .as_f64()
                    .is_some_and(|n| n > 0.0 && n <= now)
                {
                    return None;
                }
                let mut filtered = serde_json::Map::new();
                for key in [
                    "name", "value", "domain", "path", "secure", "httpOnly", "sameSite",
                ] {
                    if let Some(value) = cookie.get(key) {
                        filtered.insert(key.into(), value.clone());
                    }
                }
                if cookie["expires"].as_f64().is_some_and(|n| n > 0.0) {
                    filtered.insert("expires".into(), cookie["expires"].clone());
                }
                Some(Value::Object(filtered))
            })
            .collect();
        page.command("Network.setCookies", json!({"cookies":cookies}))?;
        let origins = state
            .get("origins")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if origins.len() > 100 {
            return Err(Error::invalid());
        }
        for origin in origins {
            page.restore_origin(&origin)?;
        }
        page.clear_responses();
        Ok(page)
    }
    fn send(&mut self, method: &str, params: Value) -> Result<u64> {
        self.send_scoped(method, params, false)
    }
    fn send_scoped(&mut self, method: &str, params: Value, page_scope: bool) -> Result<u64> {
        self.next_id += 1;
        let mut request = json!({"id":self.next_id,"method":method,"params":params});
        if !self.session.is_empty()
            && (page_scope
                || (!method.starts_with("Target.")
                    && !method.starts_with("Browser.")
                    && method != "Storage.getCookies"))
        {
            request["sessionId"] = json!(self.session);
        }
        let text = serde_json::to_string(&request).map_err(|_| failed())?;
        if text.len() > MAX_BYTES {
            return Err(Error::invalid());
        }
        self.socket
            .send(Message::Text(text.into()))
            .map_err(|_| failed())?;
        Ok(self.next_id)
    }
    fn fire(&mut self, method: &str, params: Value) -> Result<()> {
        if self.fire_ids.len() >= MAX_EVENTS {
            return Err(failed());
        }
        let id = self.send(method, params)?;
        self.fire_ids.insert(id);
        Ok(())
    }
    fn fire_child(&mut self, session: &str, method: &str, params: Value) -> Result<()> {
        if self.fire_ids.len() >= MAX_EVENTS {
            return Err(failed());
        }
        self.next_id += 1;
        let request =
            json!({"id":self.next_id,"sessionId":session,"method":method,"params":params});
        self.socket
            .send(Message::Text(request.to_string().into()))
            .map_err(|_| failed())?;
        self.fire_ids.insert(self.next_id);
        Ok(())
    }
    pub fn command(&mut self, method: &str, params: Value) -> Result<Value> {
        self.command_scoped(method, params, false)
    }
    fn command_scoped(&mut self, method: &str, params: Value, page_scope: bool) -> Result<Value> {
        if let Some(error) = &self.policy_error {
            return Err(Error::new(error.code, error.message.clone()));
        }
        let id = self.send_scoped(method, params, page_scope)?;
        let end = Instant::now() + Duration::from_secs(30);
        while Instant::now() < end {
            if let Some(error) = &self.policy_error {
                return Err(Error::new(error.code, error.message.clone()));
            }
            if let Some(value) = self.replies.remove(&id) {
                if value.get("error").is_some() {
                    return Err(failed());
                }
                return Ok(value["result"].clone());
            }
            self.receive()?;
        }
        let error = timeout();
        self.policy_error = Some(timeout());
        Err(error)
    }
    fn receive(&mut self) -> Result<()> {
        if let Some(error) = &self.policy_error {
            return Err(Error::new(error.code, error.message.clone()));
        }
        let message = match self.socket.read() {
            Ok(value) => value,
            Err(tungstenite::Error::Io(e))
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                return Ok(())
            }
            Err(_) => return Err(failed()),
        };
        let Message::Text(text) = message else {
            return if matches!(message, Message::Close(_)) {
                Err(failed())
            } else {
                Ok(())
            };
        };
        // Count all frames, including large command replies and separately retained
        // request records. An explicit boundary clears caches and resets this budget.
        self.observed_bytes = self
            .observed_bytes
            .saturating_add(text.len().saturating_mul(2));
        if self.observed_bytes > 32 * 1024 * 1024 {
            self.policy_error = Some(Error::new(
                "response_too_large",
                "页面事件超过本机容量上限，已停止；没有重放请求。",
            ));
            return Err(Error::new(
                "response_too_large",
                "页面事件超过本机容量上限，已停止；没有重放请求。",
            ));
        }
        let value: Value = serde_json::from_str(&text).map_err(|_| failed())?;
        if let Some(id) = value["id"].as_u64() {
            if self.fire_ids.remove(&id) {
                if value.get("error").is_some() {
                    return Err(failed());
                }
                if let Some(session) = self.child_resumes.remove(&id) {
                    self.fire_child(&session, "Runtime.runIfWaitingForDebugger", json!({}))?;
                }
            } else {
                if self.replies.len() >= MAX_EVENTS {
                    return Err(failed());
                }
                self.replies.insert(id, value);
            }
            return Ok(());
        }
        let method = value["method"].as_str().unwrap_or("");
        let params = &value["params"];
        if self.login_mode && method == "Network.responseReceived" {
            let status = params["response"]["status"].as_u64().unwrap_or(0);
            if status == 429
                || (status >= 500
                    && matches!(params["type"].as_str(), Some("Document" | "XHR" | "Fetch")))
            {
                let code = if status == 429 {
                    "rate_limited"
                } else {
                    "network_unavailable"
                };
                self.policy_error = Some(Error::new(
                    code,
                    "认证站点暂时限制访问或服务异常，已停止重试；已有会话保留。",
                ));
                return Err(Error::new(
                    code,
                    "认证站点暂时限制访问或服务异常，已停止重试；已有会话保留。",
                ));
            }
        }
        if self.login_mode
            && method == "Runtime.executionContextCreated"
            && params["context"]["auxData"]["isDefault"] == true
        {
            if let (Some(id), Some(origin)) = (
                params["context"]["id"].as_i64(),
                params["context"]["origin"].as_str(),
            ) {
                self.login_contexts.insert(id, origin.to_owned());
            }
        }
        if self.login_mode && method == "Runtime.executionContextDestroyed" {
            if let Some(id) = params["executionContextId"].as_i64() {
                self.login_contexts.remove(&id);
            }
        }
        if self.login_mode && method == "Runtime.executionContextsCleared" {
            self.login_contexts.clear();
        }
        if self.login_mode
            && method == "Runtime.bindingCalled"
            && params["name"] == "schoolLoginAccount"
        {
            let origin = params["executionContextId"]
                .as_i64()
                .and_then(|id| self.login_contexts.get(&id));
            if origin.is_some_and(|s| {
                matches!(
                    s.as_str(),
                    "https://id.ustc.edu.cn" | "https://passport.ustc.edu.cn"
                )
            }) {
                if let Some(account) = params["payload"].as_str().filter(|s| {
                    !s.is_empty()
                        && s.len() <= 100
                        && s.bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_.@-".contains(&b))
                }) {
                    if self.login_accounts.last().is_none_or(|v| v != account) {
                        if self.login_accounts.len() >= 1024 {
                            return Err(failed());
                        }
                        self.login_accounts.push(account.to_owned());
                    }
                }
            }
        }
        if let Some(session) = value["sessionId"]
            .as_str()
            .filter(|id| self.child_sessions.contains(*id) && method != "Target.attachedToTarget")
        {
            if method == "Fetch.requestPaused" {
                self.fire_child(
                    session,
                    "Fetch.failRequest",
                    json!({"requestId":params["requestId"],"errorReason":"BlockedByClient"}),
                )?;
            } else if method == "Fetch.authRequired" {
                self.fire_child(session,"Fetch.continueWithAuth",json!({"requestId":params["requestId"],"authChallengeResponse":{"response":"CancelAuth"}}))?;
            }
            return Ok(());
        }
        match method {
            "Fetch.requestPaused" => {
                let decision = if self.policy_error.is_some() { Ok(Decision::Abort) }
                else if let Some(origin) = &self.bootstrap {
                    if params["resourceType"] == "Document" && params["request"]["url"] == *origin {
                        Ok(Decision::Fulfill {status:200,headers:vec![("Content-Type".into(),"text/html; charset=utf-8".into())],body:b"<!doctype html><html><body></body></html>".to_vec()})
                    } else { Ok(Decision::Abort) }
                } else { (self.guard)(params) };
                let decision = match decision { Ok(value) => value, Err(e) => { self.policy_error=Some(e); Decision::Abort } };
                match decision {
                    Decision::Continue => self.fire("Fetch.continueRequest",json!({"requestId":params["requestId"]}))?,
                    Decision::Abort => self.fire("Fetch.failRequest",json!({"requestId":params["requestId"],"errorReason":"BlockedByClient"}))?,
                    Decision::Fulfill{status,headers,body} => {
                        if body.len() > 8*1024*1024 || !(100..600).contains(&status) { return Err(Error::invalid()); }
                        self.fire("Fetch.fulfillRequest",json!({"requestId":params["requestId"],"responseCode":status,
                            "responseHeaders":headers.into_iter().map(|(name,value)|json!({"name":name,"value":value})).collect::<Vec<_>>(),"body":STANDARD.encode(body)}))?;
                    }
                }
            }
            "Fetch.authRequired" => self.fire("Fetch.continueWithAuth",json!({"requestId":params["requestId"],"authChallengeResponse":{"response":"CancelAuth"}}))?,
            "Page.javascriptDialogOpening" => self.fire("Page.handleJavaScriptDialog",json!({"accept":false}))?,
            "Target.attachedToTarget" if params["targetInfo"]["targetId"] != self.target => {
                    // Browser-wide auto-attach also reports Chrome startup
                    // pages outside our isolated context. They have no account
                    // state and are not part of this page's content tree.
                    if params["targetInfo"]["browserContextId"].as_str().is_some_and(|id| id != self.context) {
                        return Ok(());
                    }
                    if params["waitingForDebugger"] != true {
                        // Only the primary page has a request guard. Never allow
                        // an already-running child to continue outside that policy.
                        self.policy_error=Some(failed());
                        return Err(failed());
                    }
                    if params["targetInfo"]["type"] == "page" {
                        let session=params["sessionId"].as_str().ok_or_else(failed)?;
                        if self.child_sessions.len()>=MAX_EVENTS {return Err(failed());}
                        self.child_sessions.insert(session.to_owned());
                        // Closing a popup while window.open waits for its startup
                        // deadlocks the parent renderer on Chrome. Install a deny-
                        // all guard before resuming, then retain it until disposal.
                        self.fire_child(session,"Network.enable",json!({}))?;
                        self.fire_child(session,"Network.setBlockedURLs",json!({"urls":["ws://*","wss://*"]}))?;
                        self.fire_child(session,"Page.addScriptToEvaluateOnNewDocument",json!({"source":NO_STREAMING}))?;
                        self.fire_child(session,"Target.setAutoAttach",json!({"autoAttach":true,"waitForDebuggerOnStart":true,"flatten":true,"filter":target_filter()}))?;
                        self.fire_child(session,"Fetch.enable",json!({"patterns":[{"urlPattern":"*","requestStage":"Request"}],"handleAuthRequests":true}))?;
                        // Do not resume until Chrome has acknowledged the guard.
                        self.child_resumes.insert(self.next_id,session.to_owned());
                    }
                    // Chrome cannot close a dedicated worker with closeTarget.
                    // Keep its debugger attached and paused for this Page's whole
                    // lifetime; workers are never sent runIfWaitingForDebugger.
                    // Dropping the owning browser disposes all paused children.
            }
            _ => {}
        }
        if method == "Network.requestWillBeSent" && !self.login_mode {
            if self.requests.len() >= MAX_EVENTS {
                return Err(failed());
            }
            if params["redirectResponse"].is_object() {
                if self.events.len() >= MAX_EVENTS {
                    return Err(failed());
                }
                self.events.push_back(json!({"method":"Network.redirectResponse","params":{
                    "response":params["redirectResponse"],"request":self.requests.get(params["requestId"].as_str().unwrap_or(""))}}));
            }
            if let Some(id) = params["requestId"].as_str() {
                self.requests.insert(id.into(), params["request"].clone());
            }
        }
        // Login request bodies can contain the password the user types in Chrome.
        // Never retain them, request headers, response headers, or network traces.
        if (!self.login_mode
            && (method.starts_with("Network.")
                || method.starts_with("Page.")
                || method == "Target.attachedToTarget"))
            || method == "Page.lifecycleEvent"
        {
            if self.events.len() >= MAX_EVENTS {
                return Err(failed());
            }
            self.events.push_back(value);
        }
        Ok(())
    }
    pub fn evaluate(&mut self, expression: &str) -> Result<Value> {
        let result=self.command("Runtime.evaluate",json!({"expression":expression,"awaitPromise":true,"returnByValue":true,"userGesture":true,"timeout":25000}))?;
        if result.get("exceptionDetails").is_some() {
            return Err(Error::new(
                "page_changed",
                "网页结构或运行状态变化，已停止本次页面操作。",
            ));
        }
        Ok(result["result"]
            .get("value")
            .cloned()
            .unwrap_or(Value::Null))
    }
    pub fn navigate(&mut self, url: &str) -> Result<()> {
        let parsed = url::Url::parse(url).map_err(|_| Error::invalid())?;
        if !matches!(parsed.scheme(), "http" | "https")
            || !parsed.username().is_empty()
            || parsed.password().is_some()
        {
            return Err(Error::invalid());
        }
        let result = self.command("Page.navigate", json!({"url":url}))?;
        if result.get("errorText").is_some() {
            return Err(failed());
        }
        if let Some(loader) = result["loaderId"].as_str() {
            let end = Instant::now() + Duration::from_secs(30);
            while !self.events.iter().any(|e| {
                e["method"] == "Page.lifecycleEvent"
                    && e["params"]["loaderId"] == loader
                    && matches!(
                        e["params"]["name"].as_str(),
                        Some("DOMContentLoaded" | "load")
                    )
            }) {
                if Instant::now() >= end {
                    return Err(timeout());
                }
                self.receive()?;
            }
        }
        Ok(())
    }
    pub fn wait_expression(&mut self, expression: &str, timeout_ms: u64) -> Result<Value> {
        let end = Instant::now() + Duration::from_millis(timeout_ms.min(60000));
        while Instant::now() < end {
            match self.evaluate(expression) {
                Ok(value)
                    if value != Value::Null && value != false && value != "" && value != 0 =>
                {
                    return Ok(value)
                }
                Ok(_) => {}
                Err(e) if e.code == "page_changed" => {}
                Err(e) => return Err(e),
            }
            self.pump(100)?;
        }
        Err(timeout())
    }
    pub fn pump(&mut self, milliseconds: u64) -> Result<()> {
        let end = Instant::now() + Duration::from_millis(milliseconds.min(60000));
        while Instant::now() < end {
            if let Some(error) = &self.policy_error {
                return Err(Error::new(error.code, error.message.clone()));
            }
            self.receive()?;
        }
        Ok(())
    }
    pub fn url(&mut self) -> Result<String> {
        self.evaluate("location.href")?
            .as_str()
            .map(str::to_owned)
            .ok_or_else(failed)
    }
    pub fn html(&mut self) -> Result<String> {
        self.evaluate("document.documentElement.outerHTML")?
            .as_str()
            .map(str::to_owned)
            .ok_or_else(failed)
    }
    pub fn body_text(&mut self) -> Result<String> {
        self.evaluate("document.body?.innerText || ''")?
            .as_str()
            .map(str::to_owned)
            .ok_or_else(failed)
    }
    pub fn cookies(&mut self) -> Result<Value> {
        Ok(self.command("Network.getCookies", json!({}))?["cookies"].clone())
    }
    /// Internal official-login profile: no request/response capture, only a
    /// narrowly scoped account binding. The default business profile is unchanged.
    pub fn enable_login(&mut self) -> Result<()> {
        self.login_mode = true;
        self.clear_responses();
        self.command("Runtime.addBinding", json!({"name":"schoolLoginAccount"}))?;
        self.command(
            "Page.addScriptToEvaluateOnNewDocument",
            json!({"source":include_str!("login_capture.js")}),
        )?;
        Ok(())
    }
    pub fn take_login_accounts(&mut self) -> Vec<String> {
        std::mem::take(&mut self.login_accounts)
    }
    /// Snapshot only adapter-selected origins using locally fulfilled documents.
    /// No origin capture navigates to its network or invokes website JavaScript.
    pub fn storage_state(&mut self, origins: &[String]) -> Result<Value> {
        if origins.len() > 100 {
            return Err(Error::invalid());
        }
        let cookies = self.command(
            "Storage.getCookies",
            json!({"browserContextId":self.context}),
        )?["cookies"]
            .clone();
        let mut saved = Vec::new();
        for origin in origins {
            let parsed = url::Url::parse(origin).map_err(|_| Error::invalid())?;
            if !matches!(parsed.scheme(), "http" | "https")
                || parsed.path() != "/"
                || parsed.query().is_some()
                || parsed.fragment().is_some()
                || !parsed.username().is_empty()
                || parsed.password().is_some()
            {
                return Err(Error::invalid());
            }
            self.bootstrap = Some(parsed.to_string());
            self.navigate(parsed.as_str())?;
            let value = self.evaluate(include_str!("cdp_capture.js"))?;
            if !value["localStorage"]
                .as_array()
                .is_some_and(|v| v.is_empty())
                || !value["indexedDB"].as_array().is_some_and(|v| v.is_empty())
            {
                saved.push(value);
            }
            self.bootstrap = None;
            self.clear_responses();
        }
        Ok(json!({"cookies":cookies,"origins":saved}))
    }
    pub fn clear_responses(&mut self) {
        self.events.clear();
        self.requests.clear();
        self.replies.clear();
        self.observed_bytes = 0;
    }
    pub fn take_events(&mut self, method: &str) -> Vec<Value> {
        let mut found = Vec::new();
        self.events.retain(|event| {
            if event["method"] == method {
                found.push(event["params"].clone());
                false
            } else {
                true
            }
        });
        found
    }
    pub fn wait_response(&mut self, path: &str, timeout_ms: u64) -> Result<Value> {
        let end = Instant::now() + Duration::from_millis(timeout_ms.min(60000));
        let mut selected = None;
        while Instant::now() < end {
            // Redirects have no responseReceived/loadingFinished pair, and the
            // next request reuses the ID. Keep the original request and response.
            if let Some(index) = self.events.iter().position(|event| {
                event["method"] == "Network.redirectResponse"
                    && response_matches(path, &event["params"]["response"]["url"])
            }) {
                let event = self.events.remove(index).ok_or_else(failed)?;
                let params = &event["params"];
                return Ok(
                    json!({"url":params["response"]["url"],"status":params["response"]["status"],
                    "headers":params["response"]["headers"],"request":params["request"],"body":"","redirected":true}),
                );
            }
            if selected.is_none() {
                if let Some(index) = self.events.iter().position(|event| {
                    event["method"] == "Network.responseReceived"
                        && response_matches(path, &event["params"]["response"]["url"])
                }) {
                    selected = self
                        .events
                        .remove(index)
                        .map(|event| event["params"].clone());
                }
            }
            if let Some(response) = &selected {
                let id = &response["requestId"];
                if self.events.iter().any(|event| {
                    event["method"] == "Network.loadingFailed"
                        && event["params"]["requestId"] == *id
                }) {
                    return Err(failed());
                }
                if let Some(index) = self.events.iter().position(|event| {
                    event["method"] == "Network.loadingFinished"
                        && event["params"]["requestId"] == *id
                }) {
                    self.events.remove(index);
                    let data = self.command("Network.getResponseBody", json!({"requestId":id}))?;
                    let raw = data["body"].as_str().ok_or_else(failed)?;
                    let body = if data["base64Encoded"] == true {
                        String::from_utf8(STANDARD.decode(raw).map_err(|_| failed())?)
                            .map_err(|_| failed())?
                    } else {
                        raw.into()
                    };
                    if body.len() > 8 * 1024 * 1024 {
                        return Err(failed());
                    }
                    return Ok(
                        json!({"url":response["response"]["url"],"status":response["response"]["status"],"headers":response["response"]["headers"],
                        "request":self.requests.remove(id.as_str().unwrap_or("")).unwrap_or(Value::Null),"body":body}),
                    );
                }
            }
            self.receive()?;
        }
        Err(timeout())
    }
    pub fn fill(&mut self, selector: &str, value: &str) -> Result<()> {
        let script=format!("(()=>{{const all=[...document.querySelectorAll({})].filter(e=>e.getClientRects().length);if(all.length!==1)throw Error();const e=all[0];if(e.disabled||e.readOnly)throw Error();const p=e instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,{});e.dispatchEvent(new Event('input',{{bubbles:true}}));e.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()",json!(selector),json!(value));
        self.evaluate(&script)?;
        Ok(())
    }
    pub fn click(&mut self, selector: &str) -> Result<()> {
        let script=format!("(()=>{{const a=[...document.querySelectorAll({})].filter(e=>e.getClientRects().length);if(a.length!==1||a[0].disabled)throw Error();a[0].scrollIntoView({{block:'center'}});const r=a[0].getBoundingClientRect();return {{x:r.x+r.width/2,y:r.y+r.height/2}};}})()",json!(selector));
        let position = self.evaluate(&script)?;
        for kind in ["mousePressed", "mouseReleased"] {
            self.command("Input.dispatchMouseEvent",json!({"type":kind,"x":position["x"],"y":position["y"],"button":"left","clickCount":1}))?;
        }
        Ok(())
    }
    fn restore_origin(&mut self, state: &Value) -> Result<()> {
        let origin = state["origin"].as_str().ok_or_else(Error::invalid)?;
        let parsed = url::Url::parse(origin).map_err(|_| Error::invalid())?;
        if !matches!(parsed.scheme(), "http" | "https")
            || parsed.path() != "/"
            || parsed.query().is_some()
            || parsed.fragment().is_some()
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || state["opfs"].as_array().is_some_and(|a| !a.is_empty())
        {
            return Err(Error::invalid());
        }
        self.bootstrap = Some(parsed.to_string());
        self.navigate(parsed.as_str())?;
        let script = format!(
            "(async()=>{{const s={};\n{}\nreturn true;}})()",
            state,
            include_str!("cdp_restore.js")
        );
        self.evaluate(&script)?;
        self.bootstrap = None;
        Ok(())
    }
}

fn response_matches(path: &str, value: &Value) -> bool {
    value.as_str().is_some_and(|raw| {
        if path.starts_with("https://") || path.starts_with("http://") {
            raw == path
        } else {
            url::Url::parse(raw).is_ok_and(|u| u.path() == path || u.path().ends_with(path))
        }
    })
}

impl Drop for Page {
    fn drop(&mut self) {
        let _ = self.socket.close(None);
    }
}
