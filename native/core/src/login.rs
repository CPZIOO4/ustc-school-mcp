//! Official, user-operated sign-in and cookie-only background recovery.
//! No password, OTP, URL, selector, or JavaScript parameter enters this API.
use crate::{
    cdp::{Decision, Page},
    sessions, Error, Result,
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashSet},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};
use url::Url;

const IDENTITY: [&str; 2] = ["id.ustc.edu.cn", "passport.ustc.edu.cn"];
const BB: &str =
    "https://www.bb.ustc.edu.cn/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1";
fn manual() -> Error {
    Error::new(
        "manual_verification_required",
        "登录需要本人完成验证，请在账号页打开官方登录窗口；已有会话保留。",
    )
}
fn cancelled() -> Error {
    Error::new("cancelled", "已停止登录；已有会话保留。")
}
fn mismatch() -> Error {
    Error::new(
        "identity_mismatch",
        "本次登录账号与应用账号不同，请先切换账号；没有保存混用的会话。",
    )
}
fn check_stop(stopped: &AtomicBool) -> Result<()> {
    if stopped.load(Ordering::SeqCst) {
        Err(cancelled())
    } else {
        Ok(())
    }
}
fn entry(service: &str) -> Result<&'static str> {
    Ok(match service {
        "identity" | "bb" => BB,
        "jw" => "https://jw.ustc.edu.cn/ucas-sso/login",
        "young" => "https://young.ustc.edu.cn/login/sc-wisdom-group-learning/dataAnalysis/visual",
        "finance" => "https://cwzh.ustc.edu.cn/WFManager/home2.jsp",
        "library" => "https://opac.lib.ustc.edu.cn/reader/login-cas.php",
        "nan7" => "https://sso-proxy.lug.ustc.edu.cn/auth/default/?service=https%3A%2F%2Fnan7market.com%2Fcas",
        "icourse" => "https://icourse.club/signin/",
        "portal" => "https://i.ustc.edu.cn/page/site/index",
        "student" => "https://xgyth.ustc.edu.cn/",
        "spaces" => "https://ic.lib.ustc.edu.cn/",
        "drive" => "https://pan.ustc.edu.cn/profile/index",
        "vlab" => "https://vlab.ustc.edu.cn/vm/",
        _ => return Err(Error::invalid()),
    })
}
fn host(service: &str) -> Result<&'static str> {
    Ok(match service {
        "identity" | "bb" => "www.bb.ustc.edu.cn",
        "jw" => "jw.ustc.edu.cn",
        "young" => "young.ustc.edu.cn",
        "finance" => "cwzh.ustc.edu.cn",
        "library" => "opac.lib.ustc.edu.cn",
        "nan7" => "nan7market.com",
        "icourse" => "icourse.club",
        "portal" => "i.ustc.edu.cn",
        "student" => "xgyth.ustc.edu.cn",
        "spaces" => "ic.lib.ustc.edu.cn",
        "drive" => "pan.ustc.edu.cn",
        "vlab" => "vlab.ustc.edu.cn",
        _ => return Err(Error::invalid()),
    })
}
fn profile(service: &str) -> &str {
    if service == "identity" {
        "bb"
    } else {
        service
    }
}
fn filename(service: &str) -> Result<String> {
    entry(service)?;
    Ok(match profile(service) {
        "bb" | "jw" | "library" => format!("{}.session.dpapi", profile(service)),
        "young" | "finance" | "nan7" | "icourse" => format!("{}-session.dpapi", profile(service)),
        other => format!("{other}-campus-session.dpapi"),
    })
}
fn valid_account(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 100
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.@-".contains(&b))
}
fn private_dir(input: &Value) -> Result<PathBuf> {
    let path = if let Some(value) = input.get("private_dir") {
        PathBuf::from(value.as_str().ok_or_else(Error::invalid)?)
    } else {
        PathBuf::from(input["root"].as_str().ok_or_else(Error::invalid)?).join("profile")
    };
    if !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err(Error::invalid());
    }
    for part in path.ancestors().filter(|p| p.exists()) {
        let m = std::fs::symlink_metadata(part).map_err(|_| Error::invalid())?;
        if !m.is_dir() || m.file_type().is_symlink() {
            return Err(Error::invalid());
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if m.file_attributes() & 0x400 != 0 {
                return Err(Error::invalid());
            }
        }
    }
    std::fs::create_dir_all(&path)
        .map_err(|_| Error::new("storage_failure", "无法打开应用私人目录。"))?;
    Ok(path)
}
fn read_if_present(
    root: &Path,
    name: &str,
    kind: &str,
    account: Option<&str>,
) -> Result<Option<Value>> {
    let path = root.join(name);
    if !path.exists() {
        return Ok(None);
    }
    let m = std::fs::symlink_metadata(&path).map_err(|_| Error::invalid())?;
    if !m.is_file() || m.file_type().is_symlink() {
        return Err(Error::invalid());
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if m.file_attributes() & 0x400 != 0 {
            return Err(Error::invalid());
        }
    }
    sessions::dispatch(
        "sessions_read",
        &json!({"profile":kind,"path":path,"account":account}),
    )
    .map(Some)
}
fn identity(root: &Path) -> Result<Option<String>> {
    let value = read_if_present(root, "ustc-identity.credentials.dpapi", "identity", None)?;
    Ok(value.and_then(|v| v["username"].as_str().map(str::to_owned)))
}
fn state_of(value: &Value) -> Value {
    value.get("state").cloned().unwrap_or_else(
        || json!({"cookies":value.get("cookies").cloned().unwrap_or(json!([])),"origins":[]}),
    )
}
fn combine(own: &Value, shared: &Value) -> Value {
    let mut cookies = BTreeMap::new();
    let mut origins = BTreeMap::new();
    for state in [own, shared] {
        for c in state["cookies"].as_array().into_iter().flatten() {
            cookies.insert(
                format!("{}|{}|{}", c["domain"], c["path"], c["name"]),
                c.clone(),
            );
        }
        for o in state["origins"].as_array().into_iter().flatten() {
            origins.insert(o["origin"].to_string(), o.clone());
        }
    }
    json!({"cookies":cookies.into_values().collect::<Vec<_>>(),"origins":origins.into_values().collect::<Vec<_>>()})
}
fn allowed_host(service: &str, domain: &str) -> bool {
    domain == host(service).unwrap_or("")
        || (service != "icourse" && IDENTITY.contains(&domain))
        || (matches!(service, "bb" | "identity") && domain == "bb.ustc.edu.cn")
        || (service == "nan7"
            && matches!(
                domain,
                "sso-proxy.lug.ustc.edu.cn" | "nan7market-api.0x01.work"
            ))
}
fn static_resource(path: &str) -> bool {
    [
        ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf",
        ".eot", ".map",
    ]
    .iter()
    .any(|s| path.to_ascii_lowercase().ends_with(s))
}
fn request_decision(service: &str, params: &Value) -> Result<Decision> {
    let r = &params["request"];
    let Ok(mut url) = Url::parse(r["url"].as_str().unwrap_or("")) else {
        return Ok(Decision::Abort);
    };
    let method = r["method"].as_str().unwrap_or("");
    let domain = url.host_str().unwrap_or("").to_owned();
    if !allowed_host(service, &domain) || !url.username().is_empty() || url.password().is_some() {
        return Ok(Decision::Abort);
    }
    // Legacy published callbacks are upgraded before a cookie or ticket is sent.
    // OPAC alone keeps its established HTTP transport; no password POST is allowed.
    if url.scheme() == "http"
        && url.port_or_known_default() == Some(80)
        && method == "GET"
        && params["resourceType"] == "Document"
        && service != "library"
    {
        url.set_scheme("https").map_err(|_| Error::invalid())?;
        url.set_port(None).map_err(|_| Error::invalid())?;
        return Ok(Decision::Fulfill {
            status: 307,
            headers: vec![("Location".into(), url.to_string())],
            body: vec![],
        });
    }
    if !(url.scheme() == "https" && url.port_or_known_default() == Some(443)
        || service == "library"
            && domain == host(service)?
            && url.scheme() == "http"
            && url.port_or_known_default() == Some(80)
            && matches!(method, "GET" | "HEAD"))
    {
        return Ok(Decision::Abort);
    }
    if IDENTITY.contains(&domain.as_str()) {
        // OPAC advertises gateway=true, which deliberately skips authentication
        // when CAS has expired. Follow the observed service, removing only this
        // optional flag locally; never invent a CAS service URL.
        if service == "library"
            && method == "GET"
            && params["resourceType"] == "Document"
            && url.query_pairs().any(|(k, _)| k == "gateway")
        {
            let pairs: Vec<_> = url
                .query_pairs()
                .filter(|(k, _)| k != "gateway")
                .map(|(k, v)| (k.into_owned(), v.into_owned()))
                .collect();
            url.set_query(None);
            url.query_pairs_mut().extend_pairs(pairs);
            return Ok(Decision::Fulfill {
                status: 307,
                headers: vec![("Location".into(), url.to_string())],
                body: vec![],
            });
        }
        return Ok(if matches!(method, "GET" | "POST" | "HEAD" | "OPTIONS") {
            Decision::Continue
        } else {
            Decision::Abort
        });
    }
    let path = url.path().split(';').next().unwrap_or("");
    if matches!(method, "GET" | "HEAD") {
        // Navigation is restricted to login and profile pages, while same-site
        // non-document reads support the official SPA's own loading sequence.
        let document = params["resourceType"] == "Document";
        let allowed = !document
            || static_resource(path)
            || match service {
                "identity" | "bb" => {
                    path.starts_with("/webapps/login")
                        || matches!(
                            path,
                            "/nginx_auth/"
                                | "/nginx_auth/login.php"
                                | "/webapps/portal/execute/tabs/tabAction"
                        )
                }
                "jw" => matches!(path, "/" | "/home" | "/ucas-sso/login" | "/login"),
                "young" => path.starts_with("/login/sc-wisdom-group-learning/") || path == "/login",
                "finance" => matches!(
                    path,
                    "/WFManager/home2.jsp" | "/YBX/main2.jsp" | "/YBX/main.jsp"
                ),
                "library" => matches!(
                    path,
                    "/reader/login-cas.php"
                        | "/reader/login.php"
                        | "/reader/redr_info.php"
                        | "/reader/book_lst.php"
                        | "/reader/book_hist.php"
                ),
                "icourse" => {
                    matches!(path, "/" | "/signin/" | "/signin") || path.starts_with("/user/")
                }
                "nan7" => {
                    domain == "sso-proxy.lug.ustc.edu.cn" && path.starts_with("/auth/")
                        || domain == "nan7market.com" && matches!(path, "/" | "/cas" | "/cas/")
                }
                "portal" => matches!(path, "/" | "/page/site/index" | "/cas/login" | "/login"),
                "student" => {
                    matches!(
                        path,
                        "/" | "/default.aspx"
                            | "/Default.aspx"
                            | "/index.aspx"
                            | "/login.aspx"
                            | "/Login.aspx"
                    ) || path.starts_with("/cas/")
                }
                "spaces" => matches!(path, "/" | "/index" | "/login") || path.starts_with("/auth/"),
                "drive" => {
                    matches!(path, "/" | "/profile/index" | "/login" | "/sso")
                        || path.starts_with("/cas/")
                }
                "vlab" => {
                    matches!(
                        path,
                        "/" | "/vm/"
                            | "/vm/userinfo/"
                            | "/accounts/login/"
                            | "/accounts/cas/login/"
                    ) || path.starts_with("/cas/")
                }
                _ => false,
            };
        return Ok(if allowed {
            Decision::Continue
        } else {
            Decision::Abort
        });
    }
    let post = method == "POST"
        && match service {
            "icourse" => matches!(path, "/signin/" | "/signin"),
            "nan7" => {
                domain == "nan7market-api.0x01.work"
                    && matches!(
                        path,
                        "/api/v1/offer/search" | "/api/v1/user/login" | "/api/v1/user/cas"
                    )
                    || domain == "sso-proxy.lug.ustc.edu.cn" && path.starts_with("/auth/")
            }
            "young" => {
                path.starts_with("/login/wisdom-group-learning-bg/sys/")
                    && !["save", "delete", "update", "edit", "add"]
                        .iter()
                        .any(|v| path.to_lowercase().contains(v))
            }
            "finance" => matches!(
                path,
                "/YBX/getWinTypes.action"
                    | "/YBX/loadRolesMenu.action"
                    | "/YBX/loadInitFunction.action"
                    | "/YBX/loadRolesName.action"
                    | "/YBX/getDefaultWinno.action"
            ),
            "portal" => path == "/site/user_info",
            "spaces" => path == "/ic-web/auth/userInfo",
            "drive" => path == "/api/v1/user/get_user_info",
            _ => false,
        };
    Ok(
        if post || method == "OPTIONS" && service == "nan7" && domain == "nan7market-api.0x01.work"
        {
            Decision::Continue
        } else {
            Decision::Abort
        },
    )
}
fn check_account(expected: Option<&str>, observed: Option<&str>) -> Result<()> {
    if observed.is_some_and(|v| !valid_account(v)) {
        return Err(mismatch());
    }
    if let (Some(a), Some(b)) = (expected, observed) {
        if !a.eq_ignore_ascii_case(b) {
            return Err(mismatch());
        }
    }
    Ok(())
}
fn fetch_json(page: &mut Page, path: &str) -> Result<Value> {
    page.evaluate(&format!("(async()=>{{const r=await fetch({},{{credentials:'same-origin',redirect:'error',signal:AbortSignal.timeout(12000)}});if(r.status!==200)return null;const t=await r.text();if(t.length>1048576)throw Error();return JSON.parse(t);}})()",json!(path)))
}
fn verified(page: &mut Page, service: &str, url: &Url) -> Result<Option<Value>> {
    if url.host_str() != Some(host(service)?)
        && !(matches!(service, "identity" | "bb") && url.host_str() == Some("bb.ustc.edu.cn"))
    {
        return Ok(None);
    }
    let path = url.path().split(';').next().unwrap_or("");
    let mut metadata = json!({});
    let ready=match service {
        "identity"|"bb"=>path=="/webapps/portal/execute/tabs/tabAction" && page.evaluate("!!document.querySelector('a[href*=\"logout\"],#globalNavPageNavArea,#globalNavLink') && !document.querySelector('input[type=password]')")?==true,
        "jw"=>path=="/home" && page.evaluate("!!document.querySelector('#e-top-menu') && !document.querySelector('input[type=password],a[href=\"/ucas-sso/login\"]')")?==true,
        "library"=>matches!(path,"/reader/redr_info.php"|"/reader/book_lst.php"|"/reader/book_hist.php") && page.evaluate("!!document.querySelector('a[href*=\"logout.php\"]') && !document.querySelector('input[type=password],form[action*=login]')")?==true,
        "young"=>page.evaluate("(document.body?.innerText||'').includes('欢迎您')")?==true,
        "finance"=>path=="/WFManager/home2.jsp" && page.evaluate("(()=>{const x=document.querySelector('#YBX[url]');if(!x)return false;const u=new URL(x.getAttribute('url').replace(/^(WINOPEN|ERPURL)/,''),location.href);return u.origin===location.origin && u.pathname==='/YBX/main2.jsp';})()")?==true,
        "icourse"=>{
            let id=page.evaluate("(()=>{const ids=[...new Set([...document.querySelectorAll('a[href]')].filter(a=>a.textContent.trim()==='个人主页').map(a=>/^\\/user\\/(\\d+)$/.exec(a.getAttribute('href'))?.[1]).filter(Boolean))];return ids.length===1?ids[0]:null;})()")?;
            metadata["user_id"]=id.clone(); id.is_string()
        },
        "nan7"=>{
            let result=page.evaluate("(async()=>{const t=localStorage.getItem('token');if(!t || !/^[!-~]{1,16384}$/.test(t) || ['null','undefined'].includes(t))return null;const r=await fetch('https://nan7market-api.0x01.work/api/v1/offer/search',{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+t},body:JSON.stringify({type:'sell'}),redirect:'error',signal:AbortSignal.timeout(12000)});if(r.status!==200)return null;const v=await r.json();return Array.isArray(v.offers)?t:null;})()")?;
            metadata["token"]=result.clone();result.is_string()
        },
        "portal"=>{if path!="/page/site/index" {false} else {let v=fetch_json(page,"/site/user_info?clenttype=pc")?;metadata["account"]=v["d"].get("user_number").or(v["d"].get("account")).cloned().unwrap_or(Value::Null);v["e"]==0 && metadata["account"].is_string()}},
        "student"=>page.evaluate("[...document.querySelectorAll('a,span,div,td')].some(e=>e.textContent.trim()==='奖学金申报')")?==true,
        "spaces"=>{let v=fetch_json(page,"/ic-web/auth/userInfo")?;metadata["account"]=v["data"]["logonName"].clone();v["code"]==0 && metadata["account"].is_string()},
        "drive"=>{let v=fetch_json(page,"/api/v1/user/get_user_info")?;v["code"]==1},
        "vlab"=>{let account=page.evaluate("document.querySelector('a[href=\"/vm/userinfo/\"]')?.textContent.trim() || null")?;metadata["account"]=account.clone();account.is_string()},
        _=>false,
    };
    Ok(if ready { Some(metadata) } else { None })
}
fn next_link(page: &mut Page, service: &str, current: &Url) -> Result<Option<String>> {
    if !matches!(service, "identity" | "bb" | "library") {
        return Ok(None);
    }
    if !allowed_host(service, current.host_str().unwrap_or(""))
        || IDENTITY.contains(&current.host_str().unwrap_or(""))
    {
        return Ok(None);
    }
    let links=page.evaluate("[...document.querySelectorAll('a[href]')].map(a=>({url:a.href,text:a.textContent.trim()}))")?;
    for a in links.as_array().into_iter().flatten() {
        let Some(raw) = a["url"].as_str() else {
            continue;
        };
        let Ok(url) = Url::parse(raw) else { continue };
        if url.scheme() != "https"
            || url.port_or_known_default() != Some(443)
            || !url.username().is_empty()
            || url.password().is_some()
        {
            continue;
        }
        if IDENTITY.contains(&url.host_str().unwrap_or(""))
            && (a["text"].as_str().unwrap_or("").contains("统一身份")
                || matches!(url.path(), "/cas/login" | "/login"))
        {
            return Ok(Some(raw.to_owned()));
        }
        if current.path() == "/nginx_auth/"
            && url.host_str() == current.host_str()
            && url.path() == "/nginx_auth/login.php"
            && a["text"] == "登录"
        {
            return Ok(Some(raw.to_owned()));
        }
    }
    Ok(None)
}
fn origins(service: &str) -> Result<Vec<String>> {
    let mut values = vec![format!("https://{}", host(service)?)];
    if service != "icourse" {
        values.extend(IDENTITY.map(|v| format!("https://{v}")));
    }
    if matches!(service, "identity" | "bb") {
        values.push("https://bb.ustc.edu.cn".into());
    }
    Ok(values)
}
fn save(
    root: &Path,
    service: &str,
    account: Option<&str>,
    state: &Value,
    metadata: &Value,
    user_agent: &str,
    base: &str,
) -> Result<()> {
    let at = chrono::Utc::now().to_rfc3339();
    let kind = profile(service);
    let value = match kind {
        "bb" | "jw" | "library" => {
            json!({"base_url":if kind=="bb" {"https://www.bb.ustc.edu.cn"} else {base},"cookies":state["cookies"],"user_agent":user_agent,"account":account,"saved_at":at})
        }
        "icourse" => {
            let own = sessions::dispatch(
                "sessions_filter",
                &json!({"profile":"icourse","state":state}),
            )?;
            json!({"cookies":own["cookies"],"user_id":metadata["user_id"],"user_agent":user_agent,"saved_at":at})
        }
        "nan7" => json!({"token":metadata["token"],"account":account,"saved_at":at}),
        _ => json!({"state":state,"account":account,"user_agent":user_agent,"saved_at":at}),
    };
    // Validate every projected record before the first write. Unsupported storage
    // and incomplete site cookies cannot partially bind a new identity.
    sessions::dispatch(
        "sessions_validate",
        &json!({"profile":kind,"value":value,"account":account}),
    )?;
    if service != "icourse" {
        let account = account.ok_or_else(manual)?;
        if matches!(service, "identity" | "bb") {
            sessions::dispatch(
                "sessions_identity_save",
                &json!({"profile":"identity","path":root.join("ustc-identity.credentials.dpapi"),"username":account}),
            )?;
        }
        sessions::dispatch(
            "sessions_device_save",
            &json!({"profile":"device","path":root.join("ustc-identity.device.dpapi"),"account":account,"state":state,"channel":"chrome","context_options":{"user_agent":user_agent,"locale":"zh-CN","timezone_id":"Asia/Shanghai"}}),
        )?;
    }
    sessions::dispatch(
        "sessions_write",
        &json!({"profile":kind,"path":root.join(filename(service)?),"account":account,"value":value}),
    )?;
    Ok(())
}

pub async fn run(
    input: &Value,
    stopped: &AtomicBool,
    notify: &dyn Fn(&str, &str),
) -> Result<Value> {
    run_inner(input, stopped, notify, None).await
}
async fn run_inner(
    input: &Value,
    stopped: &AtomicBool,
    notify: &dyn Fn(&str, &str),
    guard: Option<crate::cdp::Guard>,
) -> Result<Value> {
    if !input.is_object()
        || input.as_object().unwrap().keys().any(|k| {
            !matches!(
                k.as_str(),
                "root" | "private_dir" | "service" | "headed" | "timeout_seconds"
            )
        })
    {
        return Err(Error::invalid());
    }
    let service = input["service"].as_str().ok_or_else(Error::invalid)?;
    let target = entry(service)?;
    let headed = input
        .get("headed")
        .map(|v| v.as_bool().ok_or_else(Error::invalid))
        .transpose()?
        .unwrap_or(false);
    let timeout = input
        .get("timeout_seconds")
        .map(|v| v.as_u64().ok_or_else(Error::invalid))
        .transpose()?
        .unwrap_or(if headed { 600 } else { 60 });
    if !(1..=600).contains(&timeout) {
        return Err(Error::invalid());
    }
    check_stop(stopped)?;
    let root = private_dir(input)?;
    let previous = if service == "icourse" {
        None
    } else {
        identity(&root)?
    };
    if previous.is_none() && !matches!(service, "identity" | "bb" | "icourse") {
        return Err(Error::new(
            "identity_required",
            "请先在账号页登录学校统一身份。",
        ));
    }
    let own = read_if_present(
        &root,
        &filename(service)?,
        profile(service),
        previous.as_deref(),
    )?;
    let device = if service != "icourse" && previous.is_some() {
        read_if_present(
            &root,
            "ustc-identity.device.dpapi",
            "device",
            previous.as_deref(),
        )?
    } else {
        None
    };
    let empty = json!({"cookies":[],"origins":[]});
    let mut own_state = own.as_ref().map(state_of).unwrap_or_else(|| empty.clone());
    if service == "nan7" {
        if let Some(v) = &own {
            own_state["origins"] = json!([{"origin":"https://nan7market.com","localStorage":[{"name":"token","value":v["token"]}]}]);
        }
    }
    let state = combine(
        &own_state,
        &device
            .as_ref()
            .map(state_of)
            .unwrap_or_else(|| empty.clone()),
    );
    let ua = device
        .as_ref()
        .and_then(|v| v["context_options"]["user_agent"].as_str())
        .or_else(|| own.as_ref().and_then(|v| v["user_agent"].as_str()));
    notify(
        if headed {
            "waiting_for_user"
        } else {
            "authenticating"
        },
        if headed {
            "请在官方 Chrome 窗口完成登录和验证；仅保存会话，不保存登录密码。"
        } else {
            "正在后台核验已保存的登录与设备信任；需要验证码时会停止并提示。"
        },
    );
    let site = service.to_owned();
    let mut page = Page::launch(
        &state,
        ua,
        !headed,
        guard.unwrap_or_else(|| Box::new(move |p| request_decision(&site, p))),
    )
    .await?;
    page.enable_login()?;
    // A saved independent bearer token can be checked at the market origin
    // without forcing another CAS redirect.
    page.navigate(if service == "nan7" && own.is_some() {
        "https://nan7market.com/"
    } else {
        target
    })?;
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let mut next = Instant::now();
    let mut followed = HashSet::new();
    let mut captured = previous.clone();
    while Instant::now() < deadline {
        check_stop(stopped)?;
        for account in page.take_login_accounts() {
            check_account(previous.as_deref(), Some(&account))?;
            captured = Some(previous.clone().unwrap_or(account));
        }
        let raw = page.url()?;
        let url = Url::parse(&raw).map_err(|_| Error::invalid())?;
        if IDENTITY.contains(&url.host_str().unwrap_or("")) {
            // Only inspect visibility, never read any password/code input value.
            let waiting=page.evaluate("[...document.querySelectorAll('input[type=password],input[autocomplete=one-time-code]')].some(e=>e.getClientRects().length) || /验证码|动态口令|二次验证|Verification code/i.test(document.body?.innerText||'')")?==true;
            if waiting && !headed {
                return Err(manual());
            }
            if headed {
                page.evaluate("(()=>{const c=[...document.querySelectorAll('input[type=checkbox]')].filter(e=>/信任.*设备|记住.*设备|Trust.*device|Remember.*device/i.test(e.closest('label')?.textContent||document.querySelector('label[for=\"'+CSS.escape(e.id)+'\"]')?.textContent||''));if(c.length===1&&!c[0].checked&&c[0].getClientRects().length)c[0].click();return true;})()")?;
            }
        }
        if let Some(link) = next_link(&mut page, service, &url)? {
            if followed.insert(link.clone()) {
                page.navigate(&link)?;
                continue;
            }
        }
        if Instant::now() >= next {
            next = Instant::now() + Duration::from_secs(4);
            let result = verified(&mut page, service, &url);
            let metadata = match result {
                Ok(v) => v,
                Err(e) if e.code == "page_changed" => None,
                Err(e) => return Err(e),
            };
            if let Some(metadata) = metadata {
                for account in page.take_login_accounts() {
                    check_account(previous.as_deref(), Some(&account))?;
                    captured = Some(previous.clone().unwrap_or(account));
                }
                check_account(captured.as_deref(), metadata["account"].as_str())?;
                if service != "icourse" && captured.is_none() {
                    return Err(manual());
                }
                let ua = page
                    .evaluate("navigator.userAgent")?
                    .as_str()
                    .ok_or_else(Error::invalid)?
                    .to_owned();
                let state = page.storage_state(&origins(service)?)?;
                check_stop(stopped)?;
                if service != "icourse" && identity(&root)? != previous {
                    return Err(mismatch());
                }
                save(
                    &root,
                    service,
                    captured.as_deref(),
                    &state,
                    &metadata,
                    &ua,
                    &url.origin().ascii_serialization(),
                )?;
                notify(
                    "connected",
                    "官方登录已核实，会话及同账号设备信任已在本机加密保存。",
                );
                return Ok(
                    json!({"connected":true,"service":service,"account":if service=="identity" {captured} else {None},"password_saved":false,"implementation":"rust","verification":"site_response"}),
                );
            }
        }
        page.clear_responses();
        page.pump(250)?;
    }
    Err(manual())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn req(url: &str, method: &str, resource: &str) -> Value {
        json!({"request":{"url":url,"method":method},"resourceType":resource})
    }
    #[test]
    fn fixed_services_and_guard_do_not_widen_credentials() {
        assert!(entry("other").is_err());
        assert!(matches!(
            request_decision(
                "identity",
                &req("https://id.ustc.edu.cn/cas/login", "POST", "Document")
            )
            .unwrap(),
            Decision::Continue
        ));
        for raw in [
            "http://id.ustc.edu.cn/cas/login",
            "https://id.ustc.edu.cn.evil.test/cas/login",
            "https://id.ustc.edu.cn:444/cas/login",
            "https://user@id.ustc.edu.cn/cas/login",
        ] {
            assert!(matches!(
                request_decision("identity", &req(raw, "POST", "Document")).unwrap(),
                Decision::Abort
            ));
        }
        assert!(matches!(
            request_decision(
                "icourse",
                &req("https://id.ustc.edu.cn/cas/login", "POST", "Document")
            )
            .unwrap(),
            Decision::Abort
        ));
        assert!(matches!(
            request_decision(
                "finance",
                &req("https://cwzh.ustc.edu.cn/YBX/save.action", "POST", "XHR")
            )
            .unwrap(),
            Decision::Abort
        ));
        assert!(matches!(
            request_decision(
                "library",
                &req(
                    "http://opac.lib.ustc.edu.cn/reader/login.php",
                    "POST",
                    "Document"
                )
            )
            .unwrap(),
            Decision::Abort
        ));
    }
    #[test]
    fn callback_upgrade_preserves_ticket_without_network() {
        let Decision::Fulfill {
            status, headers, ..
        } = request_decision(
            "finance",
            &req(
                "http://cwzh.ustc.edu.cn/WFManager/home2.jsp?ticket=synthetic",
                "GET",
                "Document",
            ),
        )
        .unwrap()
        else {
            panic!()
        };
        assert_eq!(status, 307);
        assert_eq!(
            headers[0].1,
            "https://cwzh.ustc.edu.cn/WFManager/home2.jsp?ticket=synthetic"
        );
    }
    #[test]
    fn account_conflict_and_case_comparison() {
        assert!(check_account(Some("test1"), Some("TEST1")).is_ok());
        assert_eq!(
            check_account(Some("test1"), Some("test2"))
                .unwrap_err()
                .code,
            "identity_mismatch"
        );
        assert!(check_account(None, Some("a\n")).is_err());
    }
    #[test]
    fn shared_identity_wins_without_dropping_service_storage() {
        let old = json!({"cookies":[{"name":"a","domain":"id.ustc.edu.cn","path":"/","value":"old"}],"origins":[{"origin":"https://jw.ustc.edu.cn","localStorage":[{"name":"local","value":"yes"}]}]});
        let fresh = json!({"cookies":[{"name":"a","domain":"id.ustc.edu.cn","path":"/","value":"new"}],"origins":[]});
        let state = combine(&old, &fresh);
        assert_eq!(state["cookies"][0]["value"], "new");
        assert_eq!(state["origins"].as_array().unwrap().len(), 1);
    }
    #[test]
    fn cancellation_and_unknown_arguments_happen_before_browser() {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(async {
                let stopped = AtomicBool::new(true);
                assert_eq!(
                    run(&json!({"service":"identity"}), &stopped, &|_, _| {})
                        .await
                        .unwrap_err()
                        .code,
                    "cancelled"
                );
                assert_eq!(
                    run(
                        &json!({"service":"identity","url":"https://example.test"}),
                        &AtomicBool::new(false),
                        &|_, _| {}
                    )
                    .await
                    .unwrap_err()
                    .code,
                    "invalid_arguments"
                );
            });
    }
    #[test]
    #[ignore = "launches real headless Chrome using only locally fulfilled synthetic pages"]
    fn chrome_account_binding_storage_and_session_write() {
        tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async {
        let source=r#"<!doctype html><input id="username"><input type="password" id="password"><script>
            localStorage.setItem('trust','synthetic-local');
            const q=indexedDB.open('device',2);q.onupgradeneeded=()=>q.result.createObjectStore('trust');q.onsuccess=()=>{const tx=q.result.transaction('trust','readwrite');tx.objectStore('trust').put({date:new Date('2026-01-01'),nested:[1,'two'],big:123n},'device');tx.oncomplete=()=>{q.result.close();window.ready=true;}};
            </script>"#;
        let guard:crate::cdp::Guard=Box::new(move |_|Ok(Decision::Fulfill{status:200,headers:vec![("Content-Type".into(),"text/html".into())],body:source.as_bytes().to_vec()}));
        let mut page=Page::launch(&json!({"cookies":[],"origins":[]}),None,true,guard).await.unwrap();
        page.enable_login().unwrap();
        page.navigate("https://id.ustc.edu.cn/cas/login").unwrap();
        page.wait_expression("window.ready===true",5000).unwrap();
        page.fill("#username","TEST_ACCOUNT").unwrap();
        page.fill("#password","DO_NOT_CAPTURE_PASSWORD").unwrap();
        page.pump(100).unwrap();
        assert_eq!(page.take_login_accounts(),vec!["TEST_ACCOUNT"]);
        assert!(page.take_events("Network.requestWillBeSent").is_empty());
        let state=page.storage_state(&["https://id.ustc.edu.cn".into()]).unwrap();
        assert!(!state.to_string().contains("DO_NOT_CAPTURE_PASSWORD"));
        assert_eq!(state["origins"][0]["indexedDB"][0]["version"],2);
        drop(page);
        let mut restored=Page::launch(&state,None,true,Box::new(|_|Ok(Decision::Abort))).await.unwrap();
        assert_eq!(restored.evaluate("localStorage.getItem('trust')").unwrap(),"synthetic-local");
        let value=restored.evaluate("(async()=>{const db=await new Promise(r=>{const q=indexedDB.open('device');q.onsuccess=()=>r(q.result);});const v=await new Promise(r=>{const q=db.transaction('trust').objectStore('trust').get('device');q.onsuccess=()=>r(q.result);});db.close();return {date:v.date.toISOString(),big:String(v.big),nested:v.nested};})()").unwrap();
        assert_eq!(value["big"],"123");assert_eq!(value["nested"],json!([1,"two"]));
        drop(restored);
        let root=std::env::temp_dir().join(format!("school-login-test-{}",uuid::Uuid::new_v4().simple()));std::fs::create_dir_all(&root).unwrap();
        sessions::dispatch("sessions_identity_save",&json!({"profile":"identity","path":root.join("ustc-identity.credentials.dpapi"),"username":"TEST_ACCOUNT"})).unwrap();
        sessions::dispatch("sessions_device_save",&json!({"profile":"device","path":root.join("ustc-identity.device.dpapi"),"account":"TEST_ACCOUNT","state":state,"channel":"chrome","context_options":{}})).unwrap();
        let guard:crate::cdp::Guard=Box::new(|p|{
            if p["request"]["url"].as_str().unwrap_or("").starts_with("https://www.bb.ustc.edu.cn/") {
                Ok(Decision::Fulfill{status:200,headers:vec![("Content-Type".into(),"text/html".into()),("Set-Cookie".into(),"session=synthetic; Path=/; Secure; HttpOnly".into())],body:b"<!doctype html><a href='/webapps/logout'>Logout</a>".to_vec()})
            }else{Ok(Decision::Abort)}
        });
        let result=run_inner(&json!({"private_dir":root,"service":"identity","headed":false,"timeout_seconds":10}),&AtomicBool::new(false),&|_,_|{},Some(guard)).await.unwrap();
        assert_eq!(result["connected"],true);assert!(!result.to_string().contains("synthetic-local"));
        let saved=read_if_present(&root,"ustc-identity.device.dpapi","device",Some("TEST_ACCOUNT")).unwrap().unwrap();
        assert!(saved["state"]["origins"].as_array().unwrap().iter().any(|v|v["origin"]=="https://id.ustc.edu.cn" && v["indexedDB"].as_array().is_some_and(|a|!a.is_empty())));
        assert_eq!(read_if_present(&root,"bb.session.dpapi","bb",Some("TEST_ACCOUNT")).unwrap().unwrap()["cookies"][0]["value"],"synthetic");
        std::fs::remove_dir_all(root).unwrap();
    });
    }
    #[test]
    #[ignore = "launches real headless Chrome using only locally fulfilled synthetic pages"]
    fn chrome_site_verifiers_reject_login_pages_and_accept_fixtures() {
        tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async {
        let cases=[
            ("jw","https://jw.ustc.edu.cn/home","<nav id='e-top-menu'></nav>",""),
            ("young","https://young.ustc.edu.cn/login/sc-wisdom-group-learning/dataAnalysis/visual","<p>欢迎您</p>",""),
            ("library","https://opac.lib.ustc.edu.cn/reader/redr_info.php","<a href='logout.php'>logout</a>",""),
            ("finance","https://cwzh.ustc.edu.cn/WFManager/home2.jsp","<div id='YBX' url='WINOPEN/YBX/main2.jsp'></div>",""),
            ("icourse","https://icourse.club/","<a href='/user/123'>个人主页</a>",""),
            ("student","https://xgyth.ustc.edu.cn/","<a>奖学金申报</a>",""),
            ("portal","https://i.ustc.edu.cn/page/site/index","<p>profile</p>",r#"{"e":0,"d":{"user_number":"TEST_ACCOUNT"}}"#),
            ("spaces","https://ic.lib.ustc.edu.cn/","<p>个人中心</p>",r#"{"code":0,"data":{"logonName":"TEST_ACCOUNT"}}"#),
            ("drive","https://pan.ustc.edu.cn/profile/index","<p>profile</p>",r#"{"code":1}"#),
            ("vlab","https://vlab.ustc.edu.cn/vm/","<a href='/vm/userinfo/'>TEST_ACCOUNT</a>",""),
            ("nan7","https://nan7market.com/","<script>localStorage.setItem('token','synthetic-token')</script>",r#"{"offers":[]}"#),
        ];
        for (service,url,html,api) in cases {
            let endpoint=url.to_owned();let mut page=Page::launch(&json!({"cookies":[],"origins":[]}),None,true,Box::new(move|p|{
                let document=p["request"]["url"]==endpoint;
                Ok(Decision::Fulfill{status:200,headers:vec![("Content-Type".into(),if document {"text/html; charset=utf-8"}else{"application/json"}.into()),("Access-Control-Allow-Origin".into(),"https://nan7market.com".into()),("Access-Control-Allow-Headers".into(),"authorization,content-type".into()),("Access-Control-Allow-Methods".into(),"GET,POST,OPTIONS".into())],body:if document {html.as_bytes()}else{api.as_bytes()}.to_vec()})
            })).await.unwrap();
            page.enable_login().unwrap();page.navigate(url).unwrap();
            assert!(verified(&mut page,service,&Url::parse(url).unwrap()).unwrap().is_some(),"{service}");
            assert!(verified(&mut page,service,&Url::parse("https://id.ustc.edu.cn/cas/login").unwrap()).unwrap().is_none());
        }
        for (status,code) in [(429,"rate_limited"),(503,"network_unavailable")] {
            let mut page=Page::launch(&json!({"cookies":[],"origins":[]}),None,true,Box::new(move |_|Ok(Decision::Fulfill{status,headers:vec![("Content-Type".into(),"text/html".into())],body:b"temporarily unavailable".to_vec()}))).await.unwrap();
            page.enable_login().unwrap();
            assert_eq!(page.navigate("https://id.ustc.edu.cn/cas/login").unwrap_err().code,code);
            assert_eq!(page.pump(10).unwrap_err().code,code);
        }
    });
    }
}
