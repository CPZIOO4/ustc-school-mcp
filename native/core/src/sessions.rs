//! Fixed-site, byte-compatible encrypted session records. No credentials leave this pipe.
use crate::{storage, Error, Result};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::{
    fs::{File, OpenOptions, TryLockError},
    path::Path,
    time::{Duration, Instant},
};

fn invalid() -> Error {
    Error::new(
        "authentication_required",
        "本地登录会话无效或已经过期，请重新登录。",
    )
}
fn mismatch() -> Error {
    Error::new(
        "identity_mismatch",
        "本地会话与当前账号不一致，请重新登录。",
    )
}
fn lock_record(input: &Value) -> Result<File> {
    let failed = || {
        Error::new(
            "local_policy_unavailable",
            "登录状态正由其他进程更新或无法加锁；本轮未保存，请稍后重试。",
        )
    };
    let path = Path::new(input["path"].as_str().ok_or_else(Error::invalid)?);
    if !path.is_absolute() {
        return Err(Error::invalid());
    }
    let parent = path.parent().ok_or_else(Error::invalid)?;
    let filename = path
        .file_name()
        .and_then(|v| v.to_str())
        .ok_or_else(Error::invalid)?;
    std::fs::create_dir_all(parent).map_err(|_| failed())?;
    // Keep a stable inode beside the replaceable encrypted file. Never unlink this
    // empty lock file: another process may already hold or be waiting on its handle.
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(parent.join(format!("{filename}.lock")))
        .map_err(|_| failed())?;
    let started = Instant::now();
    loop {
        match file.try_lock() {
            Ok(()) => return Ok(file),
            Err(TryLockError::WouldBlock) if started.elapsed() < Duration::from_secs(2) => {
                std::thread::sleep(Duration::from_millis(25))
            }
            Err(_) => return Err(failed()),
        }
    }
}
fn host(profile: &str) -> Result<&'static str> {
    Ok(match profile {
        "bb" | "device" => "www.bb.ustc.edu.cn",
        "identity" => "id.ustc.edu.cn",
        "jw" => "jw.ustc.edu.cn",
        "library" => "opac.lib.ustc.edu.cn",
        "finance" => "cwzh.ustc.edu.cn",
        "young" => "young.ustc.edu.cn",
        "icourse" => "icourse.club",
        "nan7" => "nan7market.com",
        "portal" => "i.ustc.edu.cn",
        "student" => "xgyth.ustc.edu.cn",
        "spaces" => "ic.lib.ustc.edu.cn",
        "rooms" => "roombooking.cmet.ustc.edu.cn",
        "hightech" => "hs.lib.ustc.edu.cn",
        "drive" => "pan.ustc.edu.cn",
        "vlab" => "vlab.ustc.edu.cn",
        "llm" => "llm.ustc.edu.cn",
        _ => return Err(Error::invalid()),
    })
}
fn campus(profile: &str) -> bool {
    matches!(
        profile,
        "portal" | "student" | "spaces" | "rooms" | "hightech" | "drive" | "vlab" | "llm"
    )
}
fn identity_host(value: &str) -> bool {
    matches!(value, "id.ustc.edu.cn" | "passport.ustc.edu.cn")
}
fn cookie_scope(profile: &str, domain: &str) -> bool {
    let domain = domain
        .strip_prefix('.')
        .unwrap_or(domain)
        .to_ascii_lowercase();
    let Ok(own) = host(profile) else {
        return false;
    };
    if domain == own {
        return true;
    }
    match profile {
        "device" => {
            identity_host(&domain) || matches!(domain.as_str(), "bb.ustc.edu.cn" | "ustc.edu.cn")
        }
        "bb" => matches!(domain.as_str(), "bb.ustc.edu.cn" | "ustc.edu.cn"),
        "jw" => domain == "ustc.edu.cn",
        "library" => domain == "lib.ustc.edu.cn",
        _ if campus(profile) => identity_host(&domain) || domain == "ustc.edu.cn",
        _ => false,
    }
}
fn origin_scope(profile: &str, raw: &str) -> bool {
    let Ok(url) = url::Url::parse(raw) else {
        return false;
    };
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port_or_known_default() != Some(443)
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return false;
    }
    let Some(domain) = url.host_str() else {
        return false;
    };
    domain == host(profile).unwrap_or("")
        || (profile == "device" && (identity_host(domain) || domain == "bb.ustc.edu.cn"))
        || (campus(profile) && identity_host(domain))
}
fn now() -> f64 {
    chrono::Utc::now().timestamp_millis() as f64 / 1000.0
}
fn live(cookie: &Value) -> bool {
    match cookie.get("expires") {
        None => true,
        Some(value) => value
            .as_f64()
            .is_some_and(|expires| expires == -1.0 || expires > now()),
    }
}
fn string_field(value: &Value, key: &str, max: usize) -> bool {
    value.get(key).is_none_or(|v| {
        v.as_str()
            .is_some_and(|s| s.len() <= max && !s.chars().any(char::is_control))
    })
}
fn selected_cookie(profile: &str, cookie: &Value) -> bool {
    cookie.is_object()
        && cookie["domain"]
            .as_str()
            .is_some_and(|d| cookie_scope(profile, d))
        && cookie["name"].as_str().is_some_and(|s| !s.is_empty())
        && cookie["value"].is_string()
        && string_field(cookie, "name", 4096)
        && string_field(cookie, "value", 32768)
        && cookie.get("path").is_none_or(|v| {
            v.as_str().is_some_and(|p| {
                p.starts_with('/') && p.len() <= 4096 && !p.chars().any(char::is_control)
            })
        })
        && ["secure", "httpOnly"]
            .iter()
            .all(|k| cookie.get(k).is_none_or(Value::is_boolean))
        && cookie
            .get("sameSite")
            .is_none_or(|v| matches!(v.as_str(), Some("Strict" | "Lax" | "None")))
}
fn filtered(profile: &str, state: &Value, include_expired: bool) -> Result<Value> {
    host(profile)?;
    if !state.is_object() {
        return Err(invalid());
    }
    let empty = Vec::new();
    let cookies = match state.get("cookies") {
        None => &empty,
        Some(v) => v.as_array().ok_or_else(invalid)?,
    };
    let origins = match state.get("origins") {
        None => &empty,
        Some(v) => v.as_array().ok_or_else(invalid)?,
    };
    if cookies.len() > 5000 || origins.len() > 100 {
        return Err(invalid());
    }
    let cookies: Vec<Value> = cookies
        .iter()
        .filter(|c| selected_cookie(profile, c) && (include_expired || live(c)))
        .map(|c| {
            if profile == "bb" {
                let keys = [
                    "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite",
                ];
                Value::Object(
                    c.as_object()
                        .unwrap()
                        .iter()
                        .filter(|(k, _)| keys.contains(&k.as_str()))
                        .map(|(k, v)| (k.clone(), v.clone()))
                        .collect(),
                )
            } else {
                c.clone()
            }
        })
        .collect();
    let origins: Vec<Value> = origins
        .iter()
        .filter(|o| {
            o["origin"]
                .as_str()
                .is_some_and(|s| origin_scope(profile, s))
        })
        .cloned()
        .collect();
    Ok(json!({"cookies":cookies,"origins":origins}))
}
fn key(cookie: &Value) -> Option<(String, String, String)> {
    Some((
        cookie["name"].as_str()?.to_owned(),
        cookie["domain"].as_str()?.to_ascii_lowercase(),
        cookie["path"].as_str().unwrap_or("/").to_owned(),
    ))
}
fn merge(profile: &str, previous: &Value, state: &Value) -> Result<Value> {
    let old = filtered(profile, previous, false)?;
    // Expired fresh cookies are deletion tombstones, not a reason to revive an old cookie.
    let fresh = filtered(profile, state, true)?;
    let mut cookies = BTreeMap::new();
    for c in old["cookies"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fresh["cookies"].as_array().unwrap())
    {
        if let Some(key) = key(c) {
            if live(c) {
                cookies.insert(key, c.clone());
            } else {
                cookies.remove(&key);
            }
        }
    }
    let mut origins = BTreeMap::new();
    for o in old["origins"]
        .as_array()
        .unwrap()
        .iter()
        .chain(fresh["origins"].as_array().unwrap())
    {
        origins.insert(o["origin"].as_str().unwrap().to_owned(), o.clone());
    }
    Ok(
        json!({"cookies":cookies.into_values().collect::<Vec<_>>(),"origins":origins.into_values().collect::<Vec<_>>()}),
    )
}
fn valid_username(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 100
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.@-".contains(&b))
    })
}
fn valid_token(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 16384
            && !matches!(s, "null" | "undefined")
            && s.bytes().all(|b| (33..=126).contains(&b))
    })
}
fn validated(profile: &str, mut value: Value, account: Option<&Value>) -> Result<Value> {
    host(profile)?;
    if !value.is_object() {
        return Err(invalid());
    }
    if value
        .get("account")
        .is_some_and(|v| !v.is_null() && !valid_username(v))
        || value.get("user_agent").is_some_and(|v| {
            !v.is_null()
                && !v
                    .as_str()
                    .is_some_and(|s| s.len() <= 2048 && !s.chars().any(char::is_control))
        })
    {
        return Err(invalid());
    }
    if let Some(expected) = account.filter(|v| !v.is_null()) {
        // Old BB/JW records did not store an account. Keep them readable, but all new
        // saves bind them when an identity is available. Other profiles require equality.
        if profile != "identity"
            && profile != "icourse"
            && ((profile != "bb" && profile != "jw") || value.get("account").is_some())
            && value.get("account") != Some(expected)
        {
            return Err(mismatch());
        }
    }
    match profile {
        "identity" => {
            if !valid_username(&value["username"])
                || !value["password"].is_string()
                || value["password"].as_str().is_some_and(|p| {
                    p.len() > 4096
                        || p.contains('\0')
                        || (p.is_empty() && value["session_only"] != true)
                })
            {
                return Err(invalid());
            }
        }
        "nan7" => {
            if !valid_token(&value["token"]) || !value["saved_at"].is_string() {
                return Err(invalid());
            }
            // Explicit projection: this independent bearer token never gains CAS cookies.
            let keys = ["token", "account", "saved_at"];
            value
                .as_object_mut()
                .unwrap()
                .retain(|k, _| keys.contains(&k.as_str()));
        }
        "bb" | "jw" | "library" | "icourse" => {
            if !value["cookies"].is_array() {
                return Err(invalid());
            }
            let original = value["cookies"].as_array().unwrap();
            if profile == "icourse"
                && original.iter().any(|c| {
                    !c["domain"]
                        .as_str()
                        .is_some_and(|s| cookie_scope(profile, s))
                })
            {
                return Err(invalid());
            }
            value["cookies"] =
                filtered(profile, &json!({"cookies":value["cookies"]}), false)?["cookies"].clone();
            if value["cookies"].as_array().unwrap().is_empty() {
                return Err(invalid());
            }
            if profile == "icourse" {
                let id = value["user_id"].as_str().ok_or_else(invalid)?;
                if id.is_empty() || !id.bytes().all(|b| b.is_ascii_digit()) {
                    return Err(invalid());
                }
            } else {
                let base = value["base_url"].as_str().ok_or_else(invalid)?;
                if base != format!("https://{}", host(profile)?)
                    && !(profile == "library" && base == "http://opac.lib.ustc.edu.cn")
                {
                    return Err(invalid());
                }
                if profile != "bb" && !value["user_agent"].is_string() {
                    return Err(invalid());
                }
            }
        }
        _ => {
            if value["state"]["cookies"].as_array().is_some_and(|cookies| {
                cookies.iter().any(|c| {
                    c["domain"]
                        .as_str()
                        .is_some_and(|d| cookie_scope(profile, d))
                        && !selected_cookie(profile, c)
                })
            }) {
                return Err(invalid());
            }
            value["state"] = filtered(profile, &value["state"], false)?;
        }
    }
    Ok(value)
}
fn read(input: &Value, profile: &str) -> Result<Value> {
    let raw = storage::private_read(&json!({"path":input["path"],"default":null}))?;
    validated(profile, raw, input.get("account"))
}
fn write(input: &Value, profile: &str, value: Value) -> Result<Value> {
    let value = validated(profile, value, input.get("account"))?;
    storage::private_write(&json!({"path":input["path"],"value":value}))?;
    Ok(value)
}
pub fn dispatch(operation: &str, input: &Value) -> Result<Value> {
    let profile = input["profile"].as_str().ok_or_else(Error::invalid)?;
    host(profile)?;
    // Acquire once around the entire read/validate/merge/atomic-write sequence;
    // generic writes participate in the same protocol and cannot bypass it.
    let _lock = if matches!(
        operation,
        "sessions_write" | "sessions_device_save" | "sessions_identity_save"
    ) {
        Some(lock_record(input)?)
    } else {
        None
    };
    match operation {
        "sessions_filter" => filtered(profile, &input["state"], false),
        "sessions_merge" => merge(profile, &input["previous"], &input["state"]),
        "sessions_read" => read(input, profile),
        "sessions_validate" => validated(profile, input["value"].clone(), input.get("account")),
        "sessions_write" => write(input, profile, input["value"].clone()),
        "sessions_device_save" if profile == "device" => {
            let previous = storage::private_read(&json!({"path":input["path"],"default":null}))?;
            let previous = if previous.is_null() {
                previous
            } else {
                validated(profile, previous, None)?
            };
            // Never merge device trust belonging to a different identity, including a null identity.
            let state = if previous.is_object()
                && valid_username(&input["account"])
                && previous["account"] == input["account"]
            {
                let previous = validated(profile, previous, input.get("account"))?;
                merge(profile, &previous["state"], &input["state"])?
            } else {
                filtered(profile, &input["state"], false)?
            };
            write(
                input,
                profile,
                json!({"account":input["account"],"state":state,"channel":input["channel"],"context_options":input["context_options"],"saved_at":chrono::Utc::now().to_rfc3339()}),
            )
        }
        "sessions_identity_save" if profile == "identity" => {
            if !valid_username(&input["username"]) {
                return Err(invalid());
            }
            let previous = storage::private_read(&json!({"path":input["path"],"default":null}))?;
            let previous = if previous.is_null() {
                previous
            } else {
                validated(profile, previous, None)?
            };
            if previous.is_object() && previous["username"] != input["username"] {
                return Err(mismatch());
            }
            let mut value = json!({"username":input["username"],"password":"","session_only":true,"saved_at":chrono::Utc::now().to_rfc3339(),"email_verification_enabled":false});
            for key in ["email_verification_enabled", "email_verification_address"] {
                if let Some(v) = previous.get(key) {
                    value[key] = v.clone();
                }
            }
            write(input, profile, value)
        }
        _ => Err(Error::invalid()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn cookie(name: &str, domain: &str) -> Value {
        json!({"name":name,"value":"synthetic","domain":domain,"path":"/","expires":-1,"secure":true})
    }
    #[test]
    fn scope_rejects_parent_suffix_and_insecure_origin() {
        let s = json!({"cookies":[cookie("own","www.bb.ustc.edu.cn"),cookie("broad","cn"),cookie("foreign","www.bb.ustc.edu.cn.evil.test")],"origins":[]});
        assert_eq!(
            filtered("bb", &s, false).unwrap()["cookies"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert!(!origin_scope("drive", "http://id.ustc.edu.cn"));
        assert!(!origin_scope(
            "drive",
            "https://user:password@id.ustc.edu.cn"
        ));
        assert!(!origin_scope("drive", "https://pan.ustc.edu.cn:444"));
        assert!(origin_scope("drive", "https://id.ustc.edu.cn"));
    }
    #[test]
    fn merge_preserves_trust_and_honors_cookie_deletion() {
        let old = json!({"cookies":[cookie("trust","id.ustc.edu.cn"),cookie("gone","www.bb.ustc.edu.cn")],"origins":[]});
        let mut gone = cookie("gone", "www.bb.ustc.edu.cn");
        gone["expires"] = json!(0);
        let fresh = json!({"cookies":[gone,cookie("session","www.bb.ustc.edu.cn")],"origins":[]});
        let result = merge("device", &old, &fresh).unwrap();
        let names: Vec<_> = result["cookies"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, vec!["session", "trust"]);
    }
    #[test]
    fn independent_sessions_reject_school_state_and_bad_accounts() {
        assert!(!cookie_scope("icourse", ".ustc.edu.cn"));
        assert!(!cookie_scope("nan7", "id.ustc.edu.cn"));
        assert_eq!(
            validated(
                "young",
                json!({"account":"other","state":{"cookies":[],"origins":[]}}),
                Some(&json!("current"))
            )
            .unwrap_err()
            .code,
            "identity_mismatch"
        );
        assert!(!valid_token(&json!("token\r\nAuthorization: forged")));
    }
}
