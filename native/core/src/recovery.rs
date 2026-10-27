//! Fixed failure contracts. Exception text, URLs and website prose are not inputs.
use crate::{Error, Result};
use serde_json::{json, Value};

const REASONS: &[(&str, &str, &str)] = &[
    ("login_password_required", "manual_verification", "学校已返回密码登录页。点击“账号与连接 → 对应平台 → 打开验证窗口”，在官方页面输入统一身份账号和密码；出现设备信任选项时勾选。验证成功后应用自动保存会话。"),
    ("login_email_required", "manual_verification", "学校正在要求邮件验证码。已关联并授权的邮箱验证会先自动尝试；若仍停在此处，点击“打开验证窗口”，选择邮箱验证并填写最新验证码。"),
    ("login_captcha_required", "manual_verification", "学校正在要求图形或滑块验证。点击“打开验证窗口”，在官方页面完成图形或滑块；通过后继续登录，应用会自动检查连接。"),
    ("login_phone_required", "manual_verification", "学校正在要求手机扫码或动态口令。点击“打开验证窗口”，按官方页面提示扫码或填写手机动态口令。"),
    ("login_page_unrecognized", "manual_verification", "暂未识别到登录成功或明确的验证控件。点击“查看登录页面”检查是否停在跳转、网络错误或授权页；不要反复输入密码。"),

    (
        "authorization_required",
        "confirm_operation",
        "操作尚未获准执行；核对具体预览和已有授权后继续。",
    ),
    (
        "authentication_required",
        "restore_session",
        "登录已失效；在账号页恢复当前平台会话，仍失败时由本人重新验证。",
    ),
    (
        "identity_required",
        "manual_login",
        "尚无可用身份；请在账号页由本人登录。",
    ),
    (
        "manual_verification_required",
        "manual_verification",
        "自动恢复已停在学校验证页，尚未识别具体验证类型。打开 SchoolAgent 的“账号与连接 → 对应平台 → 打开验证窗口”，查看官方页面要求并完成验证；应用随后检查连接。",
    ),
    (
        "access_denied",
        "check_permission",
        "平台拒绝访问；请核实账号权限，不要反复重新登录。",
    ),
    (
        "cooldown",
        "wait_then_check",
        "请求正在冷却；按返回的等待时间检查连接，不要重连或换工具绕过冷却。",
    ),
    (
        "timeout",
        "check_connection",
        "请求超时；检查网络和平台连接后再继续。",
    ),
    (
        "network_unavailable",
        "check_connection",
        "网络连接未完成；检查网络和平台连接，不自动重新登录。",
    ),
    (
        "service_unavailable",
        "check_connection",
        "平台暂时不可用；稍后检查连接，不自动重新登录。",
    ),
    (
        "local_policy_unavailable",
        "check_local_storage",
        "本机请求记录不可用；检查私人目录权限后继续。",
    ),
    (
        "invalid_arguments",
        "correct_arguments",
        "工具参数不符合要求；重新读取工具定义并修正参数。",
    ),
    (
        "task_access_denied",
        "open_original_chat",
        "任务不属于当前对话；请打开原任务对应的对话。",
    ),
    (
        "file_not_authorized",
        "select_file",
        "文件未经当前对话选择；请在输入框旁添加文件。",
    ),
    (
        "runtime_failure",
        "check_runtime",
        "后台运行环境异常；检查本机 Chrome 和程序状态。",
    ),
    (
        "browser_unavailable",
        "check_runtime",
        "后台浏览器未能完成连接；请检查 Chrome 和登录状态后继续原查询。",
    ),
    (
        "study_week_unknown",
        "check_calendar",
        "教务未提供当前教学周；请核实校历后查询指定周次。",
    ),
    (
        "login_failed",
        "check_login_status",
        "本次恢复失败或中断；检查登录进度，不自动重复启动登录。",
    ),
    (
        "login_busy",
        "check_login_status",
        "已有登录流程进行中；等待原流程完成，不并行启动登录。",
    ),
    (
        "login_running",
        "check_login_status",
        "后台登录尚未完成；稍后检查原登录进度。",
    ),
    (
        "outcome_unknown",
        "verify_original_receipt",
        "结果尚未核实；查询原业务回执，不重复提交。",
    ),
    (
        "unavailable",
        "inspect_task",
        "未取得可靠结果；检查平台及原任务状态，不把失败当作空结果。",
    ),
];
const SERVICES: &[&str] = &[
    "app",
    "agent",
    "model",
    "mail",
    "smtp",
    "bb",
    "jw",
    "young",
    "finance",
    "library",
    "library-public",
    "teach",
    "icourse",
    "nan7",
    "portal",
    "student",
    "spaces",
    "rooms",
    "hightech",
    "drive",
    "compute", "vlab",
    "web",
    "digest",
    "identity",
    "llm",
];
const DIAGNOSTIC_UNAVAILABLE: &str =
    "诊断日志无法写入，请检查磁盘空间和目录权限；本次错误编号仅供当前界面使用。";

fn reason(code: &str) -> Option<(&'static str, &'static str, &'static str)> {
    REASONS.iter().copied().find(|item| item.0 == code)
}

/// Carry a normalized read failure through a bounded compound operation.
pub fn read_error(value: &Value) -> Error {
    let (code, _, message) = reason(value["error_code"].as_str().unwrap_or("unavailable"))
        .unwrap_or_else(|| reason("unavailable").unwrap());
    Error::new(code, message)
}

fn service(value: Option<&str>) -> Option<&str> {
    value.filter(|name| SERVICES.contains(name))
}

fn wait_seconds(value: &Value) -> Option<(Value, String)> {
    // Preserve integer waits without a lossy float conversion. Booleans, strings,
    // zero, negatives and non-finite values cannot request a retry interval.
    if let Some(seconds) = value.as_u64().filter(|n| *n > 0) {
        return Some((json!(seconds), seconds.to_string()));
    }
    let seconds = value.as_f64().filter(|n| n.is_finite() && *n > 0.0)?.ceil();
    let encoded = if seconds < u64::MAX as f64 {
        json!(seconds as u64)
    } else {
        // Do not saturate a very long server cooldown to a shorter wait.
        json!(seconds)
    };
    Some((encoded, format!("{seconds:.0}")))
}

fn build(code: &str, platform: Option<&str>, write: bool, retry: &Value) -> Value {
    let (code, action, note) = reason(code).unwrap_or_else(|| reason("unavailable").unwrap());
    let mut note = note.to_owned();
    let mut recovery = json!({"action": action, "automatic_retry": false,
        "retry_write_allowed": false, "retry_read_after_check": matches!(code,
            "timeout" | "network_unavailable" | "service_unavailable" | "cooldown")});
    if write {
        recovery["action"] = json!("verify_original_receipt");
        recovery["after_verification"] = json!(action);
        recovery["retry_read_after_check"] = json!(false);
        note = format!("操作可能已产生效果；先查询原任务回执，不重复提交。{note}");
    }
    let mut result = json!({"state": if write { "needs_verification" } else { code },
        "error_code": code, "operation_failed": true, "completed": false,
        "next_step": note, "recovery": recovery});
    if let Some(platform) = service(platform) {
        result["service"] = json!(platform);
    }
    if let Some((seconds, label)) = wait_seconds(retry) {
        result["retry_after_seconds"] = seconds;
        result["next_step"] = json!(format!("{note} 至少等待 {label} 秒。"));
    }
    result
}

/// Receives only a fixed reason code, a service identifier, a write flag and wait.
pub fn failure(input: &Value) -> Result<Value> {
    if !input.is_object()
        || input
            .get("write_may_have_run")
            .is_some_and(|v| !v.is_boolean())
    {
        return Err(Error::invalid());
    }
    Ok(build(
        input["code"].as_str().unwrap_or("unavailable"),
        input["service"].as_str(),
        input["write_may_have_run"] == true,
        &input["retry_after_seconds"],
    ))
}

fn preserve_diagnostic(result: &mut Value, value: &Value) {
    if let (Some(identifier), Some(saved)) = (
        value["error_id"].as_str(),
        value["diagnostic_saved"].as_bool(),
    ) {
        if identifier.len() == 20
            && identifier.starts_with("ERR-")
            && identifier.as_bytes()[4..]
                .iter()
                .all(|b| b.is_ascii_digit() || (b'A'..=b'F').contains(b))
        {
            result["error_id"] = json!(identifier);
            result["diagnostic_saved"] = json!(saved);
            if !saved && value["diagnostic_note"] == DIAGNOSTIC_UNAVAILABLE {
                result["diagnostic_note"] = json!(DIAGNOSTIC_UNAVAILABLE);
            }
        }
    }
}

/// Classifies explicit result fields only; success and unknown states return None.
/// Previously normalized contracts are rebuilt so an adapter cannot enable retry.
pub fn returned_failure(value: &Value, platform: Option<&str>) -> Option<Value> {
    if !value.is_object() {
        return Some(build("unavailable", platform, false, &Value::Null));
    }
    let state = match value["state"].as_str().unwrap_or("") {
        "waiting_for_verification" => "manual_verification_required",
        "authentication_failed" => "authentication_required",
        state => state,
    };
    let unknown_write = matches!(
        state,
        "needs_verification" | "uncertain" | "already_attempted"
    );
    let normalized = value["operation_failed"] == true && value["recovery"].is_object();
    let mut result = if normalized {
        let code = value["error_code"]
            .as_str()
            .filter(|code| reason(code).is_some())
            .unwrap_or(if unknown_write {
                "outcome_unknown"
            } else {
                state
            });
        build(
            code,
            platform.or_else(|| value["service"].as_str()),
            unknown_write
                || value["recovery"]["action"] == "verify_original_receipt"
                    && code != "outcome_unknown",
            &value["retry_after_seconds"],
        )
    } else if unknown_write {
        build("outcome_unknown", platform, true, &Value::Null)
    } else if reason(state).is_some() {
        build(state, platform, false, &value["retry_after_seconds"])
    } else if value["connected"] == false {
        build("unavailable", platform, false, &Value::Null)
    } else {
        return None;
    };
    preserve_diagnostic(&mut result, value);
    Some(result)
}

/// Pipe entry point; JSON null represents a result with no explicit failure.
pub fn returned(input: &Value) -> Result<Value> {
    let value = input.get("value").ok_or_else(Error::invalid)?;
    Ok(returned_failure(value, input["service"].as_str()).unwrap_or(Value::Null))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reasons_are_fixed_and_unknown_writes_never_retry() {
        for (code, action, _) in REASONS {
            let normal = failure(&json!({"code": code})).unwrap();
            assert_eq!(normal["recovery"]["action"], *action);
            let write = failure(&json!({"code": code, "write_may_have_run": true})).unwrap();
            assert_eq!(write["state"], "needs_verification");
            assert_eq!(write["recovery"]["action"], "verify_original_receipt");
            assert_eq!(write["recovery"]["automatic_retry"], false);
            assert_eq!(write["recovery"]["retry_write_allowed"], false);
            assert_eq!(write["recovery"]["retry_read_after_check"], false);
            assert_eq!(returned_failure(&write, None), Some(write));
        }
    }

    #[test]
    fn only_explicit_states_are_classified_and_diagnostics_are_validated() {
        assert!(returned_failure(&json!({"note": "authentication_required"}), None).is_none());
        let value = json!({"state":"access_denied", "error_id":"ERR-0123456789ABCDEF", "diagnostic_saved":false,
            "body":"synthetic-private-content"});
        let result = returned_failure(&value, Some("bb")).unwrap();
        assert_eq!(result["error_id"], "ERR-0123456789ABCDEF");
        assert_eq!(result["recovery"]["action"], "check_permission");
        assert!(!result.to_string().contains("synthetic-private-content"));
        for identifier in [
            "ERR-0123456789abcdef",
            "ERR-0123456789ABCDEF\n",
            "https://private.invalid/",
        ] {
            let result = returned_failure(
                &json!({"state":"timeout", "error_id":identifier,
                "diagnostic_saved":true}),
                None,
            )
            .unwrap();
            assert!(result.get("error_id").is_none());
        }
    }

    #[test]
    fn untrusted_recovery_cannot_enable_retry_or_replace_fixed_text() {
        let result = returned_failure(
            &json!({"state":"uncertain", "error_code":"timeout",
            "operation_failed":true, "next_step":"synthetic-private-content",
            "recovery":{"action":"retry", "automatic_retry":true, "retry_write_allowed":true}}),
            None,
        )
        .unwrap();
        assert_eq!(result["recovery"]["action"], "verify_original_receipt");
        assert_eq!(result["recovery"]["automatic_retry"], false);
        assert!(!result.to_string().contains("synthetic-private-content"));
    }

    #[test]
    fn waits_are_numeric_positive_finite_and_rounded_up() {
        for value in [
            Value::Null,
            json!(false),
            json!(true),
            json!("12"),
            json!(-1),
            json!(0),
        ] {
            assert!(
                failure(&json!({"code":"cooldown","retry_after_seconds":value}))
                    .unwrap()
                    .get("retry_after_seconds")
                    .is_none()
            );
        }
        let result = failure(&json!({"code":"cooldown","retry_after_seconds":0.25})).unwrap();
        assert_eq!(result["retry_after_seconds"], 1);
        assert!(result["next_step"]
            .as_str()
            .unwrap()
            .ends_with("至少等待 1 秒。"));
        assert_eq!(wait_seconds(&json!(1e300)).unwrap().0, json!(1e300));
    }
}
