//! Deterministic project interpretation and durable, account-bound registration gate.
//! Browser DOM and OS task scheduling remain external IO; this module never submits HTTP.
use crate::{storage::crypt, Error, Result};
use chrono::{DateTime, Duration, FixedOffset, NaiveDateTime, SecondsFormat, TimeZone};
use regex::Regex;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use scraper::Html;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::{fs, path::PathBuf};
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;

fn err(message: &str) -> Error {
    Error::new("invalid_arguments", message)
}
fn require(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(err(message))
    }
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key].as_str().ok_or_else(Error::invalid)
}
fn code(v: &Value) -> String {
    v.as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| v.to_string())
}
fn normalized(value: &str) -> String {
    value
        .nfkc()
        .case_fold()
        .filter(|c| c.is_alphanumeric())
        .collect()
}
fn date(value: &Value, school: bool) -> Result<DateTime<FixedOffset>> {
    let value = value
        .as_str()
        .ok_or_else(|| err("时间缺失或格式变化，停止报名。"))?;
    if let Ok(d) = DateTime::parse_from_rfc3339(&value.replacen(' ', "T", 1)) {
        return Ok(d);
    }
    if school {
        for format in ["%Y-%m-%d %H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S%.f"] {
            if let Ok(d) = NaiveDateTime::parse_from_str(value, format) {
                return FixedOffset::east_opt(8 * 3600)
                    .unwrap()
                    .from_local_datetime(&d)
                    .single()
                    .ok_or_else(Error::invalid);
            }
        }
    }
    Err(err("时间格式无效；授权时间必须包含时区。"))
}
fn iso(d: DateTime<FixedOffset>) -> String {
    d.to_rfc3339_opts(
        if d.timestamp_subsec_nanos() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        false,
    )
}
fn identifier(v: &Value, max: usize) -> Result<&str> {
    v.as_str()
        .filter(|s| {
            !s.is_empty()
                && s.len() <= max
                && s.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
        })
        .ok_or_else(|| err("活动标识无效。"))
}
fn keywords(v: &Value) -> Result<Vec<String>> {
    let rows = v
        .as_array()
        .ok_or_else(|| err("模糊匹配需要2–8个明确关键词。"))?;
    require(
        (2..=8).contains(&rows.len()),
        "模糊匹配需要2–8个明确关键词。",
    )?;
    let mut found = Vec::new();
    for row in rows {
        let s = normalized(row.as_str().ok_or_else(Error::invalid)?);
        require(
            (2..=80).contains(&s.chars().count()),
            "关键词必须包含2–80个有效字符。",
        )?;
        require(!found.contains(&s), "关键词不能重复。")?;
        found.push(s);
    }
    Ok(found)
}
fn matches(detail: &Value, job: &Value) -> bool {
    let name = detail["itemName"].as_str().unwrap_or("");
    if job["resolved_name"].as_str().is_some_and(|s| s != name) {
        return false;
    }
    if let Some(exact) = job["exact_name"].as_str() {
        return name == exact;
    }
    let Ok(words) = keywords(&job["name_keywords"]) else {
        return false;
    };
    let title = normalized(name);
    words.iter().all(|word| title.contains(word))
}
fn summary(d: &Value) -> Value {
    let mut result = Map::new();
    for key in [
        "id",
        "itemName",
        "applySt",
        "applyEt",
        "st",
        "et",
        "itemCategory",
        "form",
        "formName",
        "serviceHour",
        "peopleNum",
        "applyNum",
        "booleanRegistration",
        "needSignInfo",
        "signInfo",
        "onlineStatus",
        "marathon",
    ] {
        result.insert(key.into(), d[key].clone());
    }
    Value::Object(result)
}
fn html_text(value: &Value, separator: &str) -> String {
    let raw = value.as_str().unwrap_or("");
    let doc = Html::parse_fragment(raw);
    doc.tree
        .nodes()
        .filter_map(|node| {
            let t = node.value().as_text()?;
            if node.ancestors().any(|p| {
                p.value()
                    .as_element()
                    .is_some_and(|e| ["script", "style"].contains(&e.name()))
            }) {
                return None;
            }
            let s = t.trim();
            if s.is_empty() {
                None
            } else {
                Some(s.to_owned())
            }
        })
        .collect::<Vec<_>>()
        .join(separator)
}
fn project(d: &Value) -> Value {
    let mut out = summary(d);
    for key in [
        "moduleName",
        "itemCategoryName",
        "businessDeptName",
        "applyRange",
        "rangeDeptName",
        "njName",
        "formName",
        "placeInfo",
        "applyWay",
        "teamSize",
        "applyStatus",
        "examineStatus",
        "canSubmitWork",
        "attaEndTime",
        "attaType",
    ] {
        out[key] = if d[key].is_object() || d[key].is_array() {
            Value::Null
        } else {
            d[key].clone()
        };
    }
    for key in ["baseContent", "conceive"] {
        let body = html_text(&d[key], "\n");
        out[key] = json!(body.chars().take(12000).collect::<String>());
        out[format!("{key}_truncated")] = json!(body.chars().count() > 12000);
    }
    out
}
fn identity(detail: &Value, job: &Value) -> Result<()> {
    require(matches(detail, job), "项目名称不符合授权筛选条件。")?;
    require(
        job["item_id"].is_null() || detail["id"] == job["item_id"],
        "项目标识不一致。",
    )?;
    require(
        date(&detail["applySt"], true)? == date(&job["opens_at"], false)?,
        "平台报名开始时间与授权时间不一致。",
    )
}
fn eligible(detail: &Value, job: &Value, now: DateTime<FixedOffset>) -> Result<&'static str> {
    identity(detail, job)?;
    require(
        date(&detail["applySt"], true)? <= now && now <= date(&detail["applyEt"], true)?,
        "不在平台报名窗口内。",
    )?;
    if code(&detail["booleanRegistration"]) == "1" {
        return Ok("already_registered");
    }
    require(
        code(&detail["booleanRegistration"]) == "0",
        "无法确定本人已有报名状态。",
    )?;
    for key in [
        "itemCategory",
        "form",
        "needSignInfo",
        "onlineStatus",
        "marathon",
    ] {
        require(
            code(&detail[key]) == "0",
            &format!("项目 {key} 需要额外流程；当前仅自动报名普通线下单次活动且不代填附加资料。"),
        )?;
    }
    let cancel = code(&detail["signInfo"]);
    require(
        cancel != "1" || job["allow_non_cancellable"] == true,
        "本项目报名后不可取消，现有授权没有涵盖该条件。",
    )?;
    require(["0", "1"].contains(&cancel.as_str()), "取消报名规则未知。")?;
    require(
        code(&detail["examineStatus"]) == "10" && code(&detail["applyStatus"]) == "26",
        "平台当前不允许报名。",
    )?;
    let body = format!(
        "{} {}",
        html_text(&detail["baseContent"], " "),
        html_text(&detail["conceive"], " ")
    );
    let restriction=Regex::new(r"缴费|付费|保证金|签署|必须.{0,8}(?:加群|另行报名)|仅限|限.{0,12}(?:年级|专业|学院|同学)|限额报名").unwrap();
    require(
        !restriction.is_match(&body),
        "活动说明包含需额外核实的资格、费用或承诺；请人工核对。",
    )?;
    Ok("eligible")
}
fn parse(input: &Value) -> Result<Value> {
    match text(input, "action")? {
        "summary" => Ok(summary(&input["detail"])),
        "project" => {
            require(input["detail"].is_object(), "活动详情格式变化。")?;
            Ok(project(&input["detail"]))
        }
        "detail" => {
            identifier(&input["item_id"], 100)?;
            let data = &input["data"];
            require(
                data["success"] == true && data["result"].is_object(),
                "活动详情无有效数据。",
            )?;
            require(
                data["result"]["id"] == input["item_id"],
                "活动详情标识不一致。",
            )?;
            Ok(data["result"].clone())
        }
        "children" => {
            let data = &input["data"];
            require(data["success"] == true, "系列场次读取失败。")?;
            let rows = data["result"]
                .as_array()
                .ok_or_else(|| err("系列场次格式变化。"))?;
            require(
                rows.len() <= 1000
                    && rows
                        .iter()
                        .all(|r| r.is_object() && identifier(&r["id"], 100).is_ok()),
                "系列场次过多或标识缺失，停止读取。",
            )?;
            Ok(json!(rows))
        }
        "list" => {
            let data = &input["data"];
            require(
                data["success"] == true,
                "活动列表读取失败，不能视为空结果。",
            )?;
            let rows = data["result"]["records"]
                .as_array()
                .ok_or_else(|| err("活动列表格式变化。"))?;
            require(
                rows.len() <= 1000
                    && rows.iter().all(|r| {
                        r.is_object()
                            && identifier(&r["id"], 100).is_ok()
                            && r["itemName"].is_string()
                    }),
                "活动列表标识或名称缺失，停止查询。",
            )?;
            Ok(json!(rows))
        }
        "home" => {
            let max = input["max_chars"]
                .as_u64()
                .filter(|x| (1..=50000).contains(x))
                .ok_or_else(Error::invalid)? as usize;
            let body = text(input, "text")?;
            Ok(
                json!({"connected":true,"title":input["title"],"source":"https://young.ustc.edu.cn/login/sc-wisdom-group-learning/dataAnalysis/visual","text":body.chars().take(max).collect::<String>(),"text_truncated":body.chars().count()>max,"headless":true,"content_is_untrusted":true,"note":"数据大屏包含全校汇总，不能当作本人学时或本人活动记录。"}),
            )
        }
        _ => Err(Error::invalid()),
    }
}
fn policy(input: &Value) -> Result<Value> {
    let d = &input["detail"];
    let job = &input["job"];
    match text(input, "action")? {
        "normalize" => Ok(json!(normalized(text(input, "value")?))),
        "keywords" => {
            keywords(&input["values"])?;
            Ok(input["values"].clone())
        }
        "matches" => Ok(json!(matches(d, job))),
        "eligible" => Ok(json!(eligible(d, job, date(&input["now"], false)?)?)),
        "monitor" => {
            let now = date(&input["now"], false)?;
            let result = if !job["expires_at"].is_null() && now > date(&job["expires_at"], false)? {
                "expired"
            } else if d.is_null() {
                "waiting_for_project"
            } else {
                identity(d, job)?;
                if code(&d["booleanRegistration"]) == "1" {
                    "already_registered"
                } else if now > date(&d["applyEt"], true)? {
                    "expired"
                } else if now < date(&job["opens_at"], false)? || code(&d["applyStatus"]) != "26" {
                    "waiting_for_open"
                } else {
                    eligible(d, job, now)?
                }
            };
            Ok(json!(result))
        }
        "candidates" | "locate" => {
            let rows = input["records"].as_array().ok_or_else(Error::invalid)?;
            require(
                rows.len() <= 30000,
                "活动列表超过读取上限，无法保证匹配唯一。",
            )?;
            let mut found = Vec::new();
            for row in rows {
                if matches(row, job)
                    && (job["item_id"].is_null() || row["id"] == job["item_id"])
                    && (input["action"] == "candidates"
                        || !job["exact_name"].is_null()
                        || date(&row["applySt"], true)? == date(&job["opens_at"], false)?)
                {
                    found.push(row.clone());
                }
            }
            Ok(
                json!({"state":match found.len(){0=>"missing",1=>"unique",_=>"ambiguous"},"candidates":found}),
            )
        }
        "receipt" => {
            require(matches(d, job), "核验名称不一致；保留未知结果，禁止重发。")?;
            require(
                input["item_id"] == d["id"],
                "核验活动标识不一致；禁止重发。",
            )?;
            let verified = code(&d["booleanRegistration"]) == "1";
            Ok(
                json!({"state":if verified{"verified"}else{"uncertain"},"result":{"item":summary(d),"response_success":input["response"]["success"]==true,"registration_verified":verified,"credit_hours_awarded":false}}),
            )
        }
        "before_write" => {
            let now = date(&input["now"], false)?;
            require(
                job["expires_at"].is_null() || now <= date(&job["expires_at"], false)?,
                "定时执行窗口已过，停止提交。",
            )?;
            require(
                eligible(d, job, now)? == "eligible",
                "项目已经报名，停止重复提交。",
            )?;
            Ok(json!({"allowed":true}))
        }
        "schedule" => {
            let start = date(&input["monitor_from"], false)?;
            let due = date(&input["opens_at"], false)?;
            require(
                start <= due && start > date(&input["now"], false)?,
                "监控启动时间须在未来且不晚于报名开放时间。",
            )?;
            Ok(json!({"monitor_from":iso(start),"opens_at":iso(due)}))
        }
        _ => Err(Error::invalid()),
    }
}

fn storage_error() -> Error {
    Error::new(
        "storage_failure",
        "本地报名记录无法读写；保留记录，不能重发。",
    )
}
fn encode(v: &Value) -> Result<Vec<u8>> {
    let bytes = serde_json::to_vec(v).map_err(|_| storage_error())?;
    require(bytes.len() <= 2 * 1024 * 1024, "报名记录过大。")?;
    crypt(&bytes, false)
}
fn decode(raw: &[u8]) -> Result<Value> {
    let bytes = crypt(raw, true)?;
    require(bytes.len() <= 2 * 1024 * 1024, "报名记录过大。")?;
    crate::strict_json::parse(&bytes).map_err(|_| storage_error())
}
fn job_id(input: &Value) -> Result<&str> {
    input["job_id"]
        .as_str()
        .filter(|s| {
            s.len() == 32
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .ok_or_else(|| err("job_id 无效。"))
}
fn account(input: &Value) -> Result<&str> {
    input["account"]
        .as_str()
        .filter(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
        .ok_or_else(Error::invalid)
}
fn view(db: &Connection, input: &Value) -> Result<Value> {
    type StoredRow = (String, Vec<u8>, Option<Vec<u8>>);
    let row: Option<StoredRow> = db
        .query_row(
            "SELECT state,payload,result FROM jobs WHERE id=? AND account=?",
            params![job_id(input)?, account(input)?],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(|_| storage_error())?;
    let (state, payload, result) = row.ok_or_else(|| err("任务不存在或不属于当前账号。"))?;
    let mut job = decode(&payload)?;
    require(job.is_object(), "本地报名记录格式变化。")?;
    job["job_id"] = input["job_id"].clone();
    job["state"] = json!(state);
    job["result"] = if let Some(bytes) = result {
        decode(&bytes)?
    } else {
        Value::Null
    };
    job["scheduler_installed"] = json!(false);
    job["retry_write_allowed"] = json!(false);
    job["note"] = json!("监控任务由系统计划任务启动脚本；状态及结果由本地程序记录。");
    Ok(job)
}
fn python_array(v: &Value) -> String {
    if let Some(a) = v.as_array() {
        format!(
            "[{}]",
            a.iter().map(python_array).collect::<Vec<_>>().join(", ")
        )
    } else {
        v.to_string()
    }
}
fn receipt_item(value: &Value) -> Option<&Value> {
    if !value["item_id"].is_null() {
        Some(&value["item_id"])
    } else if !value["item"]["id"].is_null() {
        Some(&value["item"]["id"])
    } else {
        None
    }
}
fn submit_gate(db: &Connection, input: &Value) -> Result<()> {
    let item = identifier(
        receipt_item(&input["result"]).ok_or_else(|| err("报名提交缺少活动标识。"))?,
        100,
    )?;
    // Older versions only locked each job. Recover evidence of previous writes
    // from their encrypted receipts before introducing the account/item latch.
    let mut statement=db.prepare("SELECT id,payload,result FROM jobs WHERE account=? AND state IN ('submitting','uncertain','verified')").map_err(|_|storage_error())?;
    type Evidence = (String, Vec<u8>, Option<Vec<u8>>);
    let records: Vec<Evidence> = statement
        .query_map([account(input)?], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .map_err(|_| storage_error())?
        .collect::<std::result::Result<_, _>>()
        .map_err(|_| storage_error())?;
    for (owner, payload, result) in records {
        let payload = decode(&payload)?;
        let result = if let Some(bytes) = result {
            decode(&bytes)?
        } else {
            Value::Null
        };
        if let Some(known) = receipt_item(&result).or_else(|| receipt_item(&payload)) {
            let known = identifier(known, 100)?;
            let key = format!("{:x}", Sha256::digest(known.as_bytes()));
            db.execute("INSERT OR IGNORE INTO young_submission_gates(account,item_key,job_id) VALUES (?,?,?)",params![account(input)?,key,owner]).map_err(|_|storage_error())?;
        }
    }
    let key = format!("{:x}", Sha256::digest(item.as_bytes()));
    let inserted = db
        .execute(
            "INSERT OR IGNORE INTO young_submission_gates(account,item_key,job_id) VALUES (?,?,?)",
            params![account(input)?, key, job_id(input)?],
        )
        .map_err(|_| storage_error())?;
    require(
        inserted == 1,
        "当前账号对该活动已有提交或报名记录；禁止跨任务重复提交，请核实原回执。",
    )
}
fn create(db: &Connection, input: &Value) -> Result<Value> {
    require(
        input["authorized"] == true,
        "只有用户明确授权该项目的定时报名后才能创建执行任务。",
    )?;
    let name = if input["exact_name"].is_null() {
        None
    } else {
        let name = text(input, "exact_name")?.trim();
        require(
            (4..=200).contains(&name.chars().count())
                && !name.chars().any(|c| c <= '\u{1f}')
                && !name.contains("...")
                && !name.contains('…'),
            "需要完整的项目名称，不能包含截断省略号或控制字符。",
        )?;
        require(
            input["name_keywords"].is_null(),
            "完整名称与模糊关键词只能选择一种。",
        )?;
        Some(name)
    };
    let mut words = if name.is_none() {
        keywords(&input["name_keywords"])?
    } else {
        Vec::new()
    };
    words.sort();
    let now = date(&input["now"], false)?;
    let due = date(&input["opens_at"], false)?;
    require(
        now < due && due < now + Duration::days(30),
        "开始时间必须在未来 30 天内。",
    )?;
    if !input["item_id"].is_null() {
        identifier(&input["item_id"], 80)?;
    }
    let poll = input["poll_seconds"]
        .as_u64()
        .filter(|n| (10..=300).contains(n))
        .ok_or_else(|| err("轮询间隔必须为10–300秒。"))?;
    let start = if input["monitor_from"].is_null() {
        None
    } else {
        Some(date(&input["monitor_from"], false)?)
    };
    require(
        start.is_none_or(|s| due - Duration::days(1) <= s && s <= due),
        "监控开始时间必须在开放时间前24小时内。",
    )?;
    let end = if input["stop_at"].is_null() {
        None
    } else {
        Some(date(&input["stop_at"], false)?)
    };
    require(
        end.is_none_or(|s| s > due),
        "监控结束时间必须晚于开放时间。",
    )?;
    let payload = json!({"created_at":iso(now),"exact_name":name,"name_keywords":input["name_keywords"],"opens_at":iso(due),"expires_at":end.map(iso).or_else(||if start.is_none(){Some(iso(due+Duration::minutes(15)))}else{None}),"monitor_from":start.map(iso),"poll_seconds":poll,"item_id":input["item_id"],"allow_non_cancellable":input["allow_non_cancellable"]==true});
    let key = json!([
        account(input)?,
        if let Some(n) = name {
            json!(n)
        } else {
            json!(words)
        },
        iso(due)
    ]);
    let target = format!("{:x}", Sha256::digest(python_array(&key).as_bytes()));
    let old: Option<String> = db
        .query_row(
            "SELECT id FROM jobs WHERE target=? AND account=? AND state!=?",
            params![target, account(input)?, "cancelled"],
            |r| r.get(0),
        )
        .optional()
        .map_err(|_| storage_error())?;
    let id = old.unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
    let exists: bool = db
        .query_row("SELECT EXISTS(SELECT 1 FROM jobs WHERE id=?)", [&id], |r| {
            r.get(0)
        })
        .map_err(|_| storage_error())?;
    if !exists {
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,NULL)",
            params![id, account(input)?, target, "scheduled", encode(&payload)?],
        )
        .map_err(|_| storage_error())?;
    }
    let mut read = input.clone();
    read["job_id"] = json!(id);
    view(db, &read)
}
fn jobs(input: &Value) -> Result<Value> {
    let path = PathBuf::from(text(input, "database")?);
    require(
        path.is_absolute()
            && path
                .file_name()
                .is_some_and(|s| s == "young-registrations.sqlite3"),
        "报名数据库路径无效。",
    )?;
    account(input)?;
    fs::create_dir_all(path.parent().ok_or_else(Error::invalid)?).map_err(|_| storage_error())?;
    let mut db = Connection::open(path).map_err(|_| storage_error())?;
    db.busy_timeout(std::time::Duration::from_secs(10))
        .map_err(|_| storage_error())?;
    db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, account TEXT, target TEXT, state TEXT, payload BLOB, result BLOB)",[]).map_err(|_|storage_error())?;
    db.execute("CREATE TABLE IF NOT EXISTS young_submission_gates (account TEXT NOT NULL, item_key TEXT NOT NULL, job_id TEXT NOT NULL, PRIMARY KEY(account,item_key))",[]).map_err(|_|storage_error())?;
    // Old Python workers use the same table. SQLite itself prevents those
    // already-running processes from bypassing the new cross-job gate.
    db.execute_batch("CREATE TRIGGER IF NOT EXISTS young_require_submission_gate BEFORE UPDATE OF state ON jobs WHEN NEW.state='submitting' AND NOT EXISTS (SELECT 1 FROM young_submission_gates WHERE account=NEW.account AND job_id=NEW.id) BEGIN SELECT RAISE(ABORT,'native submission gate required'); END;
        CREATE TRIGGER IF NOT EXISTS young_no_write_replay BEFORE UPDATE OF state ON jobs WHEN (OLD.state='submitting' AND NEW.state NOT IN ('submitting','uncertain','verified')) OR (OLD.state='uncertain' AND NEW.state NOT IN ('uncertain','verified')) OR (OLD.state='verified' AND NEW.state!='verified') BEGIN SELECT RAISE(ABORT,'registration outcome cannot be reset'); END;") .map_err(|_|storage_error())?;
    let tx = db
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| storage_error())?;
    let action = text(input, "action")?;
    let out = if action == "create" {
        create(&tx, input)?
    } else {
        let job = view(&tx, input)?;
        let state = text(&job, "state")?;
        match action {
            "view" => job,
            "claim" | "claim_monitor" => {
                let now = date(&input["now"], false)?;
                let start = if action == "claim_monitor" && !job["monitor_from"].is_null() {
                    &job["monitor_from"]
                } else {
                    &job["opens_at"]
                };
                if state != "scheduled" || now < date(start, false)? {
                    json!(false)
                } else if !job["expires_at"].is_null() && now > date(&job["expires_at"], false)? {
                    tx.execute(
                        "UPDATE jobs SET state='expired' WHERE id=?",
                        [job_id(input)?],
                    )
                    .map_err(|_| storage_error())?;
                    json!(false)
                } else {
                    tx.execute(
                        "UPDATE jobs SET state=? WHERE id=?",
                        params![
                            if action == "claim_monitor" {
                                "monitoring"
                            } else {
                                "checking"
                            },
                            job_id(input)?
                        ],
                    )
                    .map_err(|_| storage_error())?;
                    json!(true)
                }
            }
            "transition" | "cancel" => {
                let expected = if action == "cancel" {
                    state
                } else {
                    text(input, "expected")?
                };
                let next = if action == "cancel" {
                    "cancelled"
                } else {
                    text(input, "state")?
                };
                require(state == expected, "任务状态已变化；不能重复执行。")?;
                let valid = match state {
                    "scheduled" => next == "cancelled",
                    "monitoring" | "checking" => {
                        ["cancelled", "submitting", "verified", "expired", "stopped"]
                            .contains(&next)
                    }
                    "submitting" => ["verified", "uncertain"].contains(&next),
                    "uncertain" => next == "verified",
                    _ => false,
                };
                require(valid, "已提交或结果不明的报名不能撤回、重置或重复执行。")?;
                if next == "submitting" {
                    submit_gate(&tx, input)?;
                }
                let result = if input["result"].is_null() {
                    None
                } else {
                    require(input["result"].is_object(), "报名回执格式无效。")?;
                    Some(encode(&input["result"])?)
                };
                tx.execute(
                    "UPDATE jobs SET state=?, result=? WHERE id=? AND account=? AND state=?",
                    params![next, result, job_id(input)?, account(input)?, expected],
                )
                .map_err(|_| storage_error())?;
                if action == "cancel" {
                    view(&tx, input)?
                } else {
                    Value::Null
                }
            }
            "progress" => {
                require(input["result"].is_object(), "监控进度格式无效。")?;
                tx.execute(
                    "UPDATE jobs SET result=? WHERE id=? AND account=? AND state='monitoring'",
                    params![encode(&input["result"])?, job_id(input)?, account(input)?],
                )
                .map_err(|_| storage_error())?;
                Value::Null
            }
            _ => return Err(Error::invalid()),
        }
    };
    tx.commit().map_err(|_| storage_error())?;
    Ok(out)
}
pub fn dispatch(operation: &str, input: &Value) -> Result<Value> {
    match operation {
        "young_parse" => parse(input),
        "young_policy" => policy(input),
        "young_job" => jobs(input),
        _ => Err(Error::invalid()),
    }
}
