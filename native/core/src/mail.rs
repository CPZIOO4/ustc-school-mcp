//! Fixed mailbox onboarding and authentication-message policy. No arbitrary DOM input.
//! IMAP transport remains in the legacy adapter; captured secrets stay in DPAPI.
use crate::{
    cdp::{Decision, Page},
    storage, Error, Result,
};
use regex::Regex;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::{Duration, Instant},
};
use url::Url;

const JOURNAL: &str = "mail-browser-setup.dpapi";
const STATES: &[&str] = &[
    "not_started",
    "waiting_for_login",
    "ready_to_create",
    "creation_attempted",
    "captured",
    "connected",
    "manual_required",
    "existing_configuration",
];
fn failure() -> Error {
    Error::new(
        "mail_bind_unavailable",
        "邮箱绑定未完成；原配置和生成记录保留，请在本机核对，不能重复生成密码。",
    )
}
fn rx(pattern: &str) -> Regex {
    Regex::new(pattern).expect("fixed mailbox regex")
}
fn address(value: &str) -> bool {
    value.len() <= 254 && rx(r"(?i)^[A-Za-z0-9_.+\-]+@(?:mail\.)?ustc\.edu\.cn$").is_match(value)
}
fn trusted_url(value: &str) -> bool {
    Url::parse(value).is_ok_and(|u| {
        u.scheme() == "https"
            && u.host_str() == Some("mail.ustc.edu.cn")
            && u.port_or_known_default() == Some(443)
            && u.username().is_empty()
            && u.password().is_none()
    })
}
fn settings_url(value: &str) -> Option<String> {
    if !trusted_url(value) {
        return None;
    }
    let mut url = Url::parse(value).ok()?;
    if !matches!(
        url.path(),
        "/coremail/XT/index.jsp" | "/coremail/XT5/index.jsp"
    ) {
        return None;
    }
    url.set_fragment(Some("setting.security.altpwd"));
    Some(url.into())
}
fn generated(text: &str, expected: &str) -> Result<Option<(String, String)>> {
    if text.chars().count() > 12000
        || !rx(r"(?i)专用密码已生成|专用密码生成成功|生成专用密码成功|Specific password generated")
            .is_match(text)
        || !["IMAP", "SMTP", "993", "465"]
            .iter()
            .all(|s| text.contains(s))
    {
        return Ok(None);
    }
    let addresses: BTreeSet<_> = rx(r"(?i)[A-Za-z0-9_.+\-]+@(?:mail\.)?ustc\.edu\.cn\b")
        .find_iter(text)
        .map(|m| m.as_str().to_owned())
        .collect();
    if addresses.len() != 1 {
        return Ok(None);
    }
    let found = addresses.into_iter().next().unwrap();
    if !expected.is_empty() && !found.eq_ignore_ascii_case(expected) {
        return Err(Error::new(
            "identity_mismatch",
            "网页邮箱与指定地址不同；停止绑定，保留已有配置。",
        ));
    }
    let secret_pattern = rx(r"^(?:[A-Za-z0-9]{4}(?:\s+[A-Za-z0-9]{4}){3}|[A-Za-z0-9]{16})$");
    let copy = rx(r"(?i)\s+(?:复制|Copy)\s*$");
    let secrets: BTreeSet<String> = text
        .lines()
        .filter_map(|l| {
            let l = copy.replace(l.trim(), "");
            secret_pattern
                .is_match(&l)
                .then(|| l.chars().filter(|c| !c.is_whitespace()).collect())
        })
        .collect();
    Ok(if secrets.len() == 1 {
        Some((found, secrets.into_iter().next().unwrap()))
    } else {
        None
    })
}
fn matches_address(address: &str, displayed: &str) -> bool {
    if address.len() > 254 || displayed.len() > 1024 {
        return false;
    }
    let displayed = displayed.trim();
    let Some((name, domain)) = displayed.split_once('@') else {
        return false;
    };
    let Some((_, actual_domain)) = address.split_once('@') else {
        return false;
    };
    if !domain.eq_ignore_ascii_case(actual_domain)
        || !name.chars().any(|c| c.is_ascii_alphanumeric())
    {
        return false;
    }
    let mut pattern = String::from("(?i)^");
    for c in displayed.chars() {
        if matches!(c, '*' | '•' | '…') {
            pattern.push_str("[^@]*");
        } else {
            pattern.push_str(&regex::escape(&c.to_string()));
        }
    }
    pattern.push('$');
    rx(&pattern).is_match(address)
}
fn eligible(input: &Value) -> bool {
    let Some(target) = input["address"].as_str() else {
        return false;
    };
    let Some(senders) = input["senders"].as_array() else {
        return false;
    };
    let Some(recipients) = input["recipients"].as_array() else {
        return false;
    };
    senders.len() == 1
        && senders[0]
            .as_str()
            .and_then(|s| s.split_once('@'))
            .is_some_and(|(_, d)| {
                matches!(
                    d.to_ascii_lowercase().as_str(),
                    "ustc.edu.cn" | "mail.ustc.edu.cn"
                )
            })
        && recipients
            .iter()
            .any(|r| r.as_str().is_some_and(|r| r.eq_ignore_ascii_case(target)))
        && rx(r"(?i)验证|动态口令|动态密码|verification|one[- ]time|login code")
            .is_match(input["message"]["subject"].as_str().unwrap_or(""))
}
fn extract(input: &Value) -> Option<String> {
    let m = &input["message"];
    if m["body_truncated"].as_bool().unwrap_or(false) || !eligible(input) {
        return None;
    }
    let context = format!(
        "{}\n{}",
        m["subject"].as_str().unwrap_or(""),
        m["body"].as_str().unwrap_or("")
    );
    if context.len() > 100_000
        || rx(r"(?i)找回密码|重置密码|绑定邮箱|更换邮箱|password\s+reset|account\s+recovery")
            .is_match(&context)
        || !rx(r"(?i)统一身份|统一认证|身份认证|SourceID|USTC.*(?:login|auth)|(?:login|auth).*USTC")
            .is_match(&context)
    {
        return None;
    }
    // Consume a whole digit run and reject >8 digits; Rust regex has no look-ahead.
    let codes: BTreeSet<_> = rx(r"(?i)(?:验证码|校验码|动态口令|动态密码|verification\s+code|one[- ]time\s+(?:password|code))[\s:：,，为是\[\]【】()（）-]{0,30}([0-9]+)")
        .captures_iter(&context).filter(|c| (4..=8).contains(&c[1].len())).map(|c| c[1].to_owned()).collect();
    if codes.len() == 1 {
        codes.into_iter().next()
    } else {
        None
    }
}
pub fn parse(input: &Value) -> Result<Value> {
    match input["action"].as_str().ok_or_else(Error::invalid)? {
        "matches_address" => Ok(
            json!({"matches":matches_address(input["address"].as_str().ok_or_else(Error::invalid)?, input["displayed"].as_str().ok_or_else(Error::invalid)?)}),
        ),
        "eligible_header" => Ok(json!({"eligible":eligible(input)})),
        "extract_code" => Ok(json!({"code":extract(input)})),
        _ => Err(Error::invalid()),
    }
}
fn safe_path(path: &Path, directory: bool) -> Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err(Error::invalid());
    }
    for p in path.ancestors().filter(|p| p.exists()) {
        let m = fs::symlink_metadata(p).map_err(|_| failure())?;
        if m.file_type().is_symlink() || (p != path || directory) && !m.is_dir() {
            return Err(failure());
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if m.file_attributes() & 0x400 != 0 {
                return Err(failure());
            }
        }
    }
    Ok(())
}
fn load(root: &Path) -> Result<Value> {
    let path = root.join(JOURNAL);
    safe_path(&path, false)?;
    let value = storage::private_read(&json!({"path":path,"default":{"state":"not_started"}}))?;
    if !value["state"].as_str().is_some_and(|s| STATES.contains(&s)) {
        return Err(failure());
    }
    Ok(value)
}
fn save(root: &Path, mut value: Value) -> Result<()> {
    let path = root.join(JOURNAL);
    safe_path(&path, false)?;
    value["updated_at"] = json!(chrono::Utc::now().to_rfc3339());
    storage::private_write(&json!({"path":path,"value":value}))?;
    Ok(())
}
pub fn status(root: &Path) -> Result<Value> {
    let value = load(root)?;
    Ok(
        json!({"state":value["state"],"network_checked":false,"credential_captured":value["state"]=="captured","updated_at":value.get("updated_at"),"note":"历史进度不证明浏览器仍在运行或当前连接有效。"}),
    )
}
fn fingerprint(root: &Path) -> Result<String> {
    let mut hash = Sha256::new();
    for name in ["mail.json", "mail.credentials.dpapi"] {
        let path = root.join(name);
        safe_path(&path, false)?;
        hash.update(name);
        if path.try_exists().map_err(|_| failure())? {
            let mut data = Vec::new();
            fs::File::open(path)
                .map_err(|_| failure())?
                .take(1024 * 1024 + 1)
                .read_to_end(&mut data)
                .map_err(|_| failure())?;
            if data.len() > 1024 * 1024 {
                return Err(failure());
            }
            hash.update(data);
        } else {
            hash.update(b"absent");
        }
    }
    Ok(format!("{:x}", hash.finalize()))
}
struct BindLock {
    file: fs::File,
}
impl BindLock {
    fn acquire(root: &Path) -> Result<Self> {
        let path = root.join("mail-browser-setup.lock");
        safe_path(&path, false)?;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .open(path)
            .map_err(|_| failure())?;
        if file.metadata().map_err(|_| failure())?.len() == 0 {
            file.write_all(b"0")
                .and_then(|_| file.flush())
                .map_err(|_| failure())?;
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::{
                LockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
            };
            let mut overlapped = unsafe { std::mem::zeroed() };
            if unsafe {
                LockFileEx(
                    file.as_raw_handle(),
                    LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                    0,
                    1,
                    0,
                    &mut overlapped,
                )
            } == 0
            {
                return Err(Error::new(
                    "mail_bind_busy",
                    "另一个邮箱绑定正在运行；请处理已打开的窗口。",
                ));
            }
        }
        #[cfg(not(windows))]
        {
            file.try_lock().map_err(|_| failure())?;
        }
        Ok(Self { file })
    }
}
impl Drop for BindLock {
    fn drop(&mut self) {
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            let mut o = unsafe { std::mem::zeroed() };
            unsafe {
                windows_sys::Win32::Storage::FileSystem::UnlockFileEx(
                    self.file.as_raw_handle(),
                    0,
                    1,
                    0,
                    &mut o,
                );
            }
        }
        #[cfg(not(windows))]
        {
            let _ = self.file.unlock();
        }
    }
}
fn stopped(stop: &AtomicBool) -> Result<()> {
    if stop.load(Ordering::SeqCst) {
        Err(Error::new("cancelled", "已停止邮箱绑定；已有记录保留。"))
    } else {
        Ok(())
    }
}
fn dom(page: &mut Page, action: &str, label: &str) -> Result<Value> {
    if !trusted_url(&page.url()?) {
        return Err(failure());
    }
    page.evaluate(&format!(
        "{}({}, {})",
        include_str!("mail_dom.js"),
        json!(action),
        json!(label)
    ))
}
async fn browser_flow(
    root: &Path,
    expected: &str,
    timeout: u64,
    stop: &AtomicBool,
    notify: &dyn Fn(Value),
) -> Result<Value> {
    let label = format!(
        "SchoolMCP-{}-{}",
        chrono::Utc::now().format("%Y%m%d"),
        &uuid::Uuid::new_v4().simple().to_string()[..6]
    );
    let previous = fingerprint(root)?;
    save(root, json!({"state":"waiting_for_login","label":label}))?;
    notify(status(root)?);
    let mut page = Page::launch(
        &json!({"cookies":[],"origins":[]}),
        None,
        false,
        Box::new(|event| {
            let raw = event["request"]["url"].as_str().unwrap_or("");
            let allowed = Url::parse(raw).is_ok_and(|u| {
                u.scheme() == "https"
                    && matches!(
                        u.host_str(),
                        Some("mail.ustc.edu.cn" | "id.ustc.edu.cn" | "passport.ustc.edu.cn")
                    )
                    && u.port_or_known_default() == Some(443)
                    && u.username().is_empty()
                    && u.password().is_none()
            });
            Ok(if allowed {
                Decision::Continue
            } else {
                Decision::Abort
            })
        }),
    )
    .await?;
    page.enable_login()?;
    page_flow(
        root, expected, timeout, stop, notify, &mut page, &label, &previous,
    )
}
#[allow(clippy::too_many_arguments)]
fn page_flow(
    root: &Path,
    expected: &str,
    timeout: u64,
    stop: &AtomicBool,
    notify: &dyn Fn(Value),
    page: &mut Page,
    label: &str,
    previous: &str,
) -> Result<Value> {
    let outcome = (|| {
        // Webmail may replace its entry document before that loader emits
        // DOMContentLoaded. Initiate the fixed navigation once, then observe the
        // current authenticated document instead of waiting on an abandoned loader.
        let navigation =
            page.command("Page.navigate", json!({"url":"https://mail.ustc.edu.cn/"}))?;
        if navigation.get("errorText").is_some() {
            return Err(failure());
        }
        let end = Instant::now() + Duration::from_secs(timeout);
        let target = loop {
            stopped(stop)?;
            if let Some(url) = settings_url(&page.url()?) {
                if page.evaluate("document.readyState !== 'loading'")? == true {
                    break url;
                }
            }
            if Instant::now() >= end {
                return Err(failure());
            }
            page.pump(300)?;
        };
        page.navigate(&target)?;
        save(root, json!({"state":"ready_to_create","label":label}))?;
        notify(status(root)?);
        let mut opened = false;
        for _ in 0..20 {
            stopped(stop)?;
            if dom(page, "open", label)? == true {
                opened = true;
                break;
            }
            page.pump(500)?;
        }
        if !opened {
            return Err(failure());
        }
        let mut named = false;
        for _ in 0..10 {
            stopped(stop)?;
            if dom(page, "name", label)? == true {
                named = true;
                break;
            }
            page.pump(500)?;
        }
        if !named {
            return Err(failure());
        }
        stopped(stop)?;
        // Durable intent before the only final click. Never clear on DOM/network error.
        save(root, json!({"state":"creation_attempted","label":label}))?;
        notify(status(root)?);
        stopped(stop)?;
        dom(page, "generate", label)?;
        for _ in 0..30 {
            let values = dom(page, "capture", label)?;
            for text in values
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
            {
                if let Some((address, password)) = generated(text, expected)? {
                    save(
                        root,
                        json!({"state":"captured","address":address,"password":password,"label":label,"previous_config":previous}),
                    )?;
                    return status(root);
                }
            }
            if stop.load(Ordering::SeqCst) {
                break;
            }
            page.pump(500)?;
        }
        status(root)
    })();
    if load(root)?["state"] == "creation_attempted" {
        let mut result = status(root)?;
        result["requires_user_action"] = json!(true);
        result["manual_window_seconds"] = json!(180);
        notify(result.clone());
        let end = Instant::now() + Duration::from_secs(180);
        while Instant::now() < end && !stop.load(Ordering::SeqCst) {
            if page.pump(500).is_err() {
                break;
            }
        }
        return Ok(result);
    }
    outcome
}
pub async fn run(input: &Value, stop: &AtomicBool, notify: &dyn Fn(Value)) -> Result<Value> {
    let root = PathBuf::from(input["private_dir"].as_str().ok_or_else(Error::invalid)?);
    safe_path(&root, true)?;
    let expected = input["address"].as_str().unwrap_or("");
    if !expected.is_empty() && !address(expected) {
        return Err(Error::invalid());
    }
    let timeout = input["timeout_seconds"].as_u64().unwrap_or(600);
    if !(30..=900).contains(&timeout)
        || std::env::var("SCHOOL_MAIL_PASSWORD").is_ok_and(|s| !s.is_empty())
    {
        return Err(Error::invalid());
    }
    fs::create_dir_all(&root).map_err(|_| failure())?;
    let _lock = BindLock::acquire(&root)?;
    let old = load(&root)?;
    if old["state"] == "captured" {
        if !expected.is_empty()
            && !old["address"]
                .as_str()
                .is_some_and(|a| a.eq_ignore_ascii_case(expected))
        {
            return Err(Error::new(
                "identity_mismatch",
                "暂存密码与指定邮箱不匹配；停止处理。",
            ));
        }
        return status(&root);
    }
    if input["resume"] == true {
        return Err(Error::new(
            "mail_bind_unavailable",
            "没有待恢复的密码；先查看邮箱绑定状态。",
        ));
    }
    if old["state"] == "creation_attempted" {
        return status(&root);
    }
    // A partial config without credentials must still be bindable. Pin its
    // mailbox before generation so completing setup cannot silently switch it.
    let config_path = root.join("mail.json");
    safe_path(&config_path, false)?;
    let configured_address = if config_path.try_exists().map_err(|_| failure())? {
        let mut bytes = Vec::new();
        fs::File::open(&config_path)
            .map_err(|_| failure())?
            .take(1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| failure())?;
        if bytes.len() > 1024 * 1024 {
            return Err(failure());
        }
        let config = crate::strict_json::parse(&bytes).map_err(|_| failure())?;
        Some(
            config["address"]
                .as_str()
                .filter(|s| address(s))
                .ok_or_else(failure)?
                .to_owned(),
        )
    } else {
        None
    };
    let credential_path = root.join("mail.credentials.dpapi");
    safe_path(&credential_path, false)?;
    if configured_address.is_some()
        && credential_path.try_exists().map_err(|_| failure())?
        && input["replace_existing"] != true
    {
        return Ok(json!({"state":"existing_configuration","network_checked":false}));
    }
    let expected = if expected.is_empty() {
        configured_address.as_deref().unwrap_or("")
    } else {
        expected
    };
    if input["headed"] != true || input["create_client_password"] != true {
        return Err(Error::invalid());
    }
    stopped(stop)?;
    let result = browser_flow(&root, expected, timeout, stop, notify).await;
    if result.is_err()
        && !matches!(
            load(&root)?["state"].as_str(),
            Some("creation_attempted" | "captured" | "connected")
        )
    {
        save(&root, json!({"state":"manual_required"}))?;
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    fn mail(body: &str) -> Value {
        json!({"address":"synthetic@mail.ustc.edu.cn","senders":["id@ustc.edu.cn"],"recipients":["synthetic@mail.ustc.edu.cn"],"message":{"subject":"统一身份认证验证码","body":body}})
    }
    #[test]
    #[cfg(windows)]
    fn restart_never_recreates_after_unknown_or_capture() {
        let root =
            std::env::temp_dir().join(format!("school-mail-restart-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        for state in ["creation_attempted", "captured"] {
            save(&root, json!({"state":state,"address":"synthetic@mail.ustc.edu.cn","password":"synthetic-private"})).unwrap();
            // No flags permitting browser opening; terminal journal must return first.
            let result = rt
                .block_on(run(
                    &json!({"private_dir":root,"address":"synthetic@mail.ustc.edu.cn"}),
                    &AtomicBool::new(false),
                    &|_| {},
                ))
                .unwrap();
            assert_eq!(result["state"], state);
            assert!(!result.to_string().contains("synthetic-private"));
        }
        let mismatch = rt.block_on(run(
            &json!({"private_dir":root,"address":"other@mail.ustc.edu.cn"}),
            &AtomicBool::new(false),
            &|_| {},
        ));
        assert!(mismatch.is_err());
        assert_eq!(load(&root).unwrap()["state"], "captured");
        fs::remove_file(root.join(JOURNAL)).unwrap();
        fs::remove_file(root.join("mail-browser-setup.lock")).unwrap();
        fs::remove_dir(root).unwrap();
    }
    #[test]
    fn code_policy_rejects_ambiguity_recovery_and_long_numbers() {
        assert_eq!(extract(&mail("验证码：123456")), Some("123456".into()));
        for body in [
            "验证码：123456；验证码：654321",
            "重置密码验证码：123456",
            "验证码：123456789",
            "统一身份请联系123456",
        ] {
            assert_eq!(extract(&mail(body)), None);
        }
        let mut m = mail("验证码：123456");
        m["senders"] = json!(["id@ustc.edu.cn.evil.example"]);
        assert_eq!(extract(&m), None);
        m["senders"] = json!(["id@ustc.edu.cn"]);
        m["message"]["body_truncated"] = json!(true);
        assert_eq!(extract(&m), None);
    }
    #[test]
    fn exact_masked_mailbox_and_origin_only() {
        assert!(matches_address(
            "synthetic@mail.ustc.edu.cn",
            "sy**@mail.ustc.edu.cn"
        ));
        assert!(!matches_address(
            "synthetic@mail.ustc.edu.cn",
            "****@mail.ustc.edu.cn"
        ));
        assert!(!matches_address(
            "synthetic@mail.ustc.edu.cn",
            "te**@*.ustc.edu.cn"
        ));
        assert!(settings_url("https://mail.ustc.edu.cn/coremail/XT/index.jsp?sid=test").is_some());
        for url in [
            "http://mail.ustc.edu.cn/coremail/XT/index.jsp",
            "https://mail.ustc.edu.cn.evil.example/coremail/XT/index.jsp",
            "https://synthetic@mail.ustc.edu.cn/coremail/XT/index.jsp",
        ] {
            assert!(settings_url(url).is_none());
        }
    }
    #[test]
    fn generated_password_requires_unique_recognized_dialog() {
        let text = r"Specific password generated
test only Pass 1234
Email address
synthetic@mail.ustc.edu.cn
IMAP 993
SMTP 465";
        assert!(generated(text, "synthetic@mail.ustc.edu.cn")
            .unwrap()
            .is_some());
        assert!(generated(text, "other@mail.ustc.edu.cn").is_err());
        assert!(generated(&text.replace("465", "25"), "").unwrap().is_none());
        assert!(generated(
            &format!(
                r"{text}
other@mail.ustc.edu.cn"
            ),
            ""
        )
        .unwrap()
        .is_none());
    }
    #[test]
    #[cfg(windows)]
    fn journal_secret_and_cross_process_lock_compatible() {
        let root = std::env::temp_dir().join(format!("school-mail-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        let lock = BindLock::acquire(&root).unwrap();
        assert!(BindLock::acquire(&root).is_err());
        save(
            &root,
            json!({"state":"captured","password":"synthetic-password"}),
        )
        .unwrap();
        assert!(!status(&root)
            .unwrap()
            .to_string()
            .contains("synthetic-password"));
        assert!(!fs::read(root.join(JOURNAL))
            .unwrap()
            .windows(18)
            .any(|v| v == b"synthetic-password"));
        drop(lock);
        fs::remove_file(root.join(JOURNAL)).unwrap();
        fs::remove_file(root.join("mail-browser-setup.lock")).unwrap();
        fs::remove_dir(root).unwrap();
    }
    #[test]
    #[cfg(windows)]
    #[ignore = "requires installed Chrome; synthetic local interception only"]
    fn native_chrome_captures_once_after_durable_creation_marker() {
        use std::sync::{atomic::AtomicUsize, Arc};
        let root =
            std::env::temp_dir().join(format!("school-mail-browser-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let seen = count.clone();
        let journal = root.clone();
        let html = r#"<html><body><main><button onclick="document.querySelector('#form').hidden=false">Generate Specific Password</button><section id="form" hidden><label>Password name<input type="text"></label><button onclick="fetch('/synthetic-generate',{method:'POST'}).then(()=>{document.querySelector('#form').hidden=true;document.querySelector('#result').hidden=false})">Generate</button></section><section id="result" hidden><h2>Specific password generated</h2><p>The private password SchoolMCP</p><p>test only Pass 1234</p><button>Copy</button><p>Email address</p><p>synthetic@mail.ustc.edu.cn</p><p>IMAP 993</p><p>SMTP 465</p></section></main></body></html>"#;
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async {
            let mut page = Page::launch(
                &json!({"cookies":[],"origins":[]}),
                None,
                true,
                Box::new(move |request| {
                    let url = request["request"]["url"].as_str().unwrap_or("");
                    let body = if url == "https://mail.ustc.edu.cn/" {
                        "<script>location.replace('/coremail/XT/index.jsp?sid=synthetic')</script>"
                    } else if url.ends_with("/synthetic-generate") {
                        assert_eq!(load(&journal).unwrap()["state"], "creation_attempted");
                        assert_eq!(request["request"]["method"], "POST");
                        seen.fetch_add(1, Ordering::SeqCst);
                        "{}"
                    } else if url.starts_with("https://mail.ustc.edu.cn/coremail/XT/index.jsp") {
                        html
                    } else {
                        return Ok(Decision::Abort);
                    };
                    Ok(Decision::Fulfill {
                        status: 200,
                        headers: vec![("Content-Type".into(), "text/html; charset=utf-8".into())],
                        body: body.as_bytes().to_vec(),
                    })
                }),
            )
            .await
            .unwrap();
            page.enable_login().unwrap();
            let previous = fingerprint(&root).unwrap();
            let result = page_flow(
                &root,
                "synthetic@mail.ustc.edu.cn",
                30,
                &AtomicBool::new(false),
                &|_| {},
                &mut page,
                "SchoolMCP-synthetic",
                &previous,
            )
            .unwrap();
            assert_eq!(result["state"], "captured");
            assert_eq!(count.load(Ordering::SeqCst), 1);
            assert!(!result.to_string().contains("testonlyPass1234"));
            assert_eq!(load(&root).unwrap()["password"], "testonlyPass1234");
        });
        fs::remove_file(root.join(JOURNAL)).unwrap();
        fs::remove_dir(root).unwrap();
    }
}
