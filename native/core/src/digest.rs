//! Native digest state, subscriptions and app inbox. Collection IO remains a
//! temporary adapter; this module never sends mail or performs school writes.
use crate::{storage, Error, Result};
use chrono::Utc;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs,
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};
use unicode_casefold::UnicodeCaseFold;

fn bad(message: &str) -> Error {
    Error::new("invalid_arguments", message)
}
fn failure() -> Error {
    Error::new("storage_failure", "信息中心记录无法读写，原记录保留。")
}
fn wire_capacity(value: &Value) -> Result<()> {
    // Leave room for the envelope/source response below the fixed 16 MiB pipe.
    // Never truncate an old encrypted state to make a migration succeed.
    if serde_json::to_vec(value).map_err(|_| failure())?.len() > 14 * 1024 * 1024 {
        return Err(Error::new(
            "context_capacity",
            "信息中心记录超过本机管道容量，原记录保留；请先缩小订阅数据范围。",
        ));
    }
    Ok(())
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key].as_str().ok_or_else(Error::invalid)
}
fn now() -> String {
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}

pub fn defaults() -> Value {
    json!({"enabled":false,"sources":["teach"],"keywords":[],"exclude":[],"locations":[],
        "max_pages":3,"detail_limit":10,"interval_minutes":60,"daily_time":"08:00","urgent_hours":24,
        "email":"","email_enabled":false,"bb_courses":[]})
}

pub fn catalog() -> Value {
    let public = [
        (
            "jobs",
            "就业岗位",
            "https://www.job.ustc.edu.cn/Jobinformation/index.aspx",
        ),
        (
            "internships",
            "实习机会",
            "https://www.job.ustc.edu.cn/Internship/index.aspx",
        ),
        (
            "research_jobs",
            "科研助理",
            "https://www.job.ustc.edu.cn/research/list.aspx",
        ),
        (
            "job_events",
            "就业宣讲",
            "https://www.job.ustc.edu.cn/Specialrecruitment/list.aspx",
        ),
        (
            "math_notices",
            "数学学院通知",
            "https://math.ustc.edu.cn/tzgg/list.htm",
        ),
        (
            "math_lectures",
            "数学学院学术报告",
            "https://math.ustc.edu.cn/xsbg_18822/list.htm",
        ),
        (
            "cs_notices",
            "计算机学院通知",
            "https://cs.ustc.edu.cn/3054/list.htm",
        ),
        (
            "cs_lectures",
            "计算机学院学术活动",
            "https://cs.ustc.edu.cn/xsbg/list.htm",
        ),
        (
            "physics_notices",
            "物理学院通知",
            "https://physics.ustc.edu.cn/3584/list.htm",
        ),
        (
            "physics_lectures",
            "物理学院学术报告",
            "https://physics.ustc.edu.cn/xsbg/list.htm",
        ),
    ];
    let mut rows: Vec<Value> = public
        .into_iter()
        .map(|(id, label, url)| json!({"id":id,"label":label,"url":url,"public":true}))
        .collect();
    rows.extend(
        [
            ("teach", "教务处通知"),
            ("portal", "个人待办"),
            ("scholarships", "奖学金申请通知"),
            ("library", "借阅到期"),
            ("young", "第二课堂活动"),
            ("bb", "所选 BB 课程作业"),
        ]
        .into_iter()
        .map(|(id, label)| json!({"id":id,"label":label,"public":id=="teach"})),
    );
    json!(rows)
}

pub fn settings(value: &Value) -> Result<Value> {
    let input = value.as_object().ok_or_else(|| bad("订阅设置字段无效。"))?;
    let mut base = defaults();
    for (k, v) in input {
        if base.get(k).is_none() {
            return Err(bad("订阅设置字段无效。"));
        }
        base[k] = v.clone();
    }
    for k in ["enabled", "email_enabled"] {
        if !base[k].is_boolean() {
            return Err(bad("订阅开关须为布尔值。"));
        }
    }
    let source_catalog = catalog();
    let valid: HashSet<_> = source_catalog
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|x| x["id"].as_str())
        .collect();
    let sources = base["sources"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 12)
        .ok_or_else(|| bad("选择 1–12 个已适配的信息源。"))?;
    let mut unique = Vec::new();
    for s in sources {
        if !s.as_str().is_some_and(|s| valid.contains(s)) {
            return Err(bad("选择 1–12 个已适配的信息源。"));
        }
        if !unique.contains(s) {
            unique.push(s.clone());
        }
    }
    base["sources"] = json!(unique);
    for key in ["keywords", "exclude", "locations", "bb_courses"] {
        let rows = base[key]
            .as_array()
            .filter(|a| a.len() <= 20)
            .ok_or_else(|| bad("筛选项最多 20 个，每项 1–100 字。"))?;
        let mut unique = Vec::new();
        for row in rows {
            let s = row
                .as_str()
                .map(str::trim)
                .filter(|s| !s.is_empty() && s.chars().count() <= 100)
                .ok_or_else(|| bad("筛选项最多 20 个，每项 1–100 字。"))?;
            let v = json!(s);
            if !unique.contains(&v) {
                unique.push(v);
            }
        }
        base[key] = json!(unique);
    }
    let course = regex::Regex::new(r"^_?[0-9]+(?:_[0-9]+)?$").unwrap();
    if base["bb_courses"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| !course.is_match(x.as_str().unwrap()))
    {
        return Err(bad("请使用 BB 课程查询返回的编号。"));
    }
    if base["sources"].as_array().unwrap().contains(&json!("bb"))
        && base["bb_courses"].as_array().unwrap().is_empty()
    {
        return Err(bad("订阅 BB 作业前请选择课程。"));
    }
    for (key, lo, hi) in [
        ("max_pages", 1, 10),
        ("detail_limit", 0, 30),
        ("interval_minutes", 15, 1440),
        ("urgent_hours", 0, 168),
    ] {
        if !base[key].as_i64().is_some_and(|n| n >= lo && n <= hi) {
            return Err(bad("采集范围或提醒间隔超出支持范围。"));
        }
    }
    if !base["daily_time"].as_str().is_some_and(|s| {
        regex::Regex::new(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
            .unwrap()
            .is_match(s)
    }) {
        return Err(bad("日报时间使用北京时间 HH:MM。"));
    }
    let email = text(&base, "email")?;
    if !email.is_empty() && !valid_address(email) {
        return Err(bad("请输入有效的通知收件邮箱。"));
    }
    if base["email_enabled"] == true && email.is_empty() {
        return Err(bad("邮件推送需要收件邮箱。"));
    }
    Ok(base)
}

fn valid_address(value: &str) -> bool {
    // Same simple addr-spec contract as mail.outbox; display names are not accepted.
    if value.len()>254 || !regex::Regex::new(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}$").unwrap().is_match(value){return false;}
    let Some((local, domain)) = value.rsplit_once('@') else {
        return false;
    };
    local.len() <= 64
        && !local.starts_with('.')
        && !local.ends_with('.')
        && !value.contains("..")
        && domain
            .split('.')
            .all(|x| x.len() <= 63 && !x.starts_with('-') && !x.ends_with('-'))
}

// Preserve Python's canonical JSON separators: old fingerprints must not all
// look changed merely because the implementation language changed.
fn python_json(v: &Value) -> String {
    crate::agent::canonical(v)
}
pub fn fingerprint(item: &Value) -> Result<Value> {
    let mut stable = item.as_object().ok_or_else(Error::invalid)?.clone();
    for k in [
        "collected_at",
        "revision",
        "change",
        "matched_by",
        "last_seen",
        "fingerprint",
        "urgent_key",
        "detail_unavailable",
    ] {
        stable.remove(k);
    }
    if let Some(ocr) = stable.get_mut("poster_ocr").and_then(Value::as_object_mut) {
        ocr.remove("errors");
    }
    Ok(json!(format!(
        "{:x}",
        Sha256::digest(python_json(&Value::Object(stable)).as_bytes())
    )))
}
fn folded(s: &str) -> String {
    s.case_fold().collect()
}
pub fn matches(item: &Value, cfg: &Value) -> Result<Value> {
    let hay = folded(
        &[
            "title",
            "summary",
            "company",
            "location",
            "major",
            "education",
        ]
        .iter()
        .map(|k| item[k].as_str().unwrap_or(""))
        .collect::<Vec<_>>()
        .join(" "),
    );
    let strings = |k: &str| -> Result<Vec<String>> {
        cfg[k]
            .as_array()
            .ok_or_else(Error::invalid)?
            .iter()
            .map(|v| v.as_str().map(str::to_owned).ok_or_else(Error::invalid))
            .collect()
    };
    if strings("exclude")?.iter().any(|s| hay.contains(&folded(s))) {
        return Ok(json!([]));
    }
    let locations = strings("locations")?;
    if !locations.is_empty()
        && !locations
            .iter()
            .any(|s| folded(item["location"].as_str().unwrap_or("")).contains(&folded(s)))
    {
        return Ok(json!([]));
    }
    let keywords = strings("keywords")?;
    let hits: Vec<_> = keywords
        .iter()
        .filter(|s| hay.contains(&folded(s)))
        .cloned()
        .collect();
    Ok(if keywords.is_empty() {
        json!(["所选来源"])
    } else {
        json!(hits)
    })
}
pub fn deduplicate(items: &Value) -> Result<Value> {
    let mut rows: Vec<Value> = Vec::new();
    let mut seen = std::collections::HashMap::new();
    for item in items.as_array().ok_or_else(Error::invalid)? {
        let title = text(item, "title")?
            .chars()
            .filter(|c| !c.is_whitespace())
            .collect::<String>();
        let key = json!([
            title,
            item["published_at"],
            item["start"],
            item["deadline"],
            item["summary"]
        ])
        .to_string();
        if let Some(&n) = seen.get(&key) {
            if item["summary"].as_str().is_some_and(|s| !s.is_empty())
                && item["published_at"].as_str().is_some_and(|s| !s.is_empty())
            {
                let prior: &mut Value = &mut rows[n];
                if prior.get("also_from").is_none() {
                    prior["also_from"] = json!([]);
                }
                prior["also_from"]
                    .as_array_mut()
                    .ok_or_else(Error::invalid)?
                    .push(item["source_url"].clone());
                continue;
            }
        }
        seen.insert(key, rows.len());
        rows.push(item.clone());
    }
    Ok(json!(rows))
}
pub fn report_text(events: &Value, health: &Value) -> Result<Value> {
    let events = deduplicate(events)?;
    let events = events.as_array().unwrap();
    let mut lines = vec!["校园信息简报（北京时间）".to_owned(), String::new()];
    let mut shown = 0;
    for (index, x) in events.iter().take(50).enumerate() {
        if lines.iter().map(|s| s.chars().count()).sum::<usize>() > 14000 {
            break;
        }
        shown = index + 1;
        let kind = match x["change"].as_str() {
            Some("new") => "新增",
            Some("changed") => "信息变更",
            Some("deadline") => "即将截止",
            Some("source_failed") => "来源异常",
            _ => "事项",
        };
        lines.push(format!("【{kind}】{}", text(x, "title")?));
        for (key, label, limit) in [
            ("deadline", "截止：", usize::MAX),
            ("start", "时间：", usize::MAX),
            ("location", "地点：", usize::MAX),
            ("summary", "", 350),
        ] {
            if let Some(s) = x[key].as_str().filter(|s| !s.is_empty()) {
                lines.push(format!(
                    "{label}{}",
                    s.chars().take(limit).collect::<String>()
                ));
            }
        }
        if let Some(a) = x["matched_by"].as_array().filter(|a| !a.is_empty()) {
            lines.push(format!(
                "匹配：{}",
                a.iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join("、")
            ));
        }
        lines.push(format!("来源：{}", text(x, "source_url")?));
        lines.push(String::new());
    }
    if shown < events.len() {
        lines.push(format!(
            "本简报共 {} 项，此处显示 {shown} 项；其余请在信息中心缩小来源与关键词查看。",
            events.len()
        ));
    }
    let incomplete: Vec<_> = health
        .as_object()
        .ok_or_else(Error::invalid)?
        .iter()
        .filter(|(_, v)| v["state"] != "ok" || v["complete"] != true)
        .map(|(k, _)| k.as_str())
        .collect();
    if !incomplete.is_empty() {
        lines.push(format!(
            "未完整覆盖：{}。请在信息中心查看最后成功时间与范围。",
            incomplete.join("、")
        ));
    }
    Ok(json!(lines.join("\n")))
}

struct Store {
    root: PathBuf,
    owner: String,
}
fn checked_path(path: &Path, directory: bool) -> Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|p| matches!(p, std::path::Component::ParentDir))
    {
        return Err(Error::invalid());
    }
    // symlink_metadata also detects dangling links; exists() would skip them.
    // Only the supplied path and its ancestors are checked, never directory contents.
    for current in path.ancestors() {
        let metadata = match fs::symlink_metadata(current) {
            Ok(value) => value,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err(failure()),
        };
        if metadata.file_type().is_symlink()
            || if current != path || directory {
                !metadata.is_dir()
            } else {
                !metadata.is_file()
            }
        {
            return Err(failure());
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if metadata.file_attributes() & 0x400 != 0 {
                return Err(failure());
            }
        }
    }
    Ok(())
}
fn checked_files(root: &Path) -> Result<()> {
    checked_path(root, true)?;
    for name in [
        "digest.sqlite3",
        "digest.sqlite3-wal",
        "digest.sqlite3-shm",
        "digest.sqlite3-journal",
        "digest-cycle.lock",
        "digest-worker.lock",
    ] {
        checked_path(&root.join(name), false)?;
    }
    Ok(())
}
fn checked_root(input: &Value) -> Result<PathBuf> {
    let root = PathBuf::from(text(input, "root")?);
    checked_path(&root, true)?;
    fs::create_dir_all(&root).map_err(|_| failure())?;
    checked_files(&root)?;
    Ok(root)
}
impl Store {
    fn open(input: &Value) -> Result<Self> {
        let root = checked_root(input)?;
        let owner = text(input, "owner")?.to_owned();
        if owner.is_empty() || owner.len() > 128 {
            return Err(Error::invalid());
        }
        Ok(Self { root, owner })
    }
    fn db(&self) -> Result<Connection> {
        checked_files(&self.root)?;
        let db = Connection::open(self.root.join("digest.sqlite3")).map_err(|_| failure())?;
        db.busy_timeout(Duration::from_secs(10))
            .map_err(|_| failure())?;
        db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)",[]).map_err(|_|failure())?;
        Ok(db)
    }
    fn read(&self) -> Result<Value> {
        let db = self.db()?;
        let row: Option<Vec<u8>> = db
            .query_row("SELECT payload FROM state WHERE id=1", [], |r| r.get(0))
            .optional()
            .map_err(|_| failure())?;
        let Some(raw) = row else {
            return Ok(
                json!({"owner":self.owner,"settings":defaults(),"items":{},"sources":{},"messages":[],"pending":{},"last_daily":null,"last_cycle":null}),
            );
        };
        if raw.len() > 24 * 1024 * 1024 {
            return Err(failure());
        }
        let plain = storage::crypt(&raw, true)?;
        if plain.len() > 20 * 1024 * 1024 {
            return Err(failure());
        }
        let state = crate::strict_json::parse(&plain).map_err(|_| failure())?;
        self.validate(&state)?;
        Ok(state)
    }
    fn validate(&self, state: &Value) -> Result<()> {
        if state["owner"] != self.owner {
            return Err(bad("订阅属于其他账号，请恢复原账号后管理。"));
        }
        valid_state(state).map_err(|_| failure())
    }
    fn save(&self, state: &Value) -> Result<()> {
        self.validate(state)?;
        let raw = serde_json::to_vec(state).map_err(|_| failure())?;
        if raw.len() > 20 * 1024 * 1024 {
            return Err(bad("信息中心记录超过上限，请缩小来源范围。"));
        }
        let bytes = storage::crypt(&raw, false)?;
        let mut db = self.db()?;
        let tx = db
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| failure())?;
        // Do not overwrite a different owner even if a stale caller retained an old snapshot.
        let prior: Option<Vec<u8>> = tx
            .query_row("SELECT payload FROM state WHERE id=1", [], |r| r.get(0))
            .optional()
            .map_err(|_| failure())?;
        if let Some(prior) = prior {
            let state =
                crate::strict_json::parse(&storage::crypt(&prior, true)?).map_err(|_| failure())?;
            self.validate(&state)?;
        }
        tx.execute("INSERT OR REPLACE INTO state VALUES(1,?)", [bytes])
            .map_err(|_| failure())?;
        tx.commit().map_err(|_| failure())
    }
}

// Byte-zero lock interoperates with Python msvcrt.locking; a whole-file lock
// would prevent the legacy a+b append and break coexistence.
struct CycleLock {
    file: fs::File,
    #[cfg(windows)]
    overlap: Box<windows_sys::Win32::System::IO::OVERLAPPED>,
}
impl CycleLock {
    fn acquire(root: &Path) -> Result<Self> {
        checked_path(root, true)?;
        checked_path(&root.join("digest-cycle.lock"), false)?;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .read(true)
            .open(root.join("digest-cycle.lock"))
            .map_err(|_| failure())?;
        if file.metadata().map_err(|_| failure())?.len() == 0 {
            file.write_all(b"0").map_err(|_| failure())?;
            file.flush().map_err(|_| failure())?;
        }
        let busy = || bad("信息中心正在采集或发送，请稍后再试。");
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::{
                LockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
            };
            let mut overlap = Box::new(unsafe { std::mem::zeroed() });
            if unsafe {
                LockFileEx(
                    file.as_raw_handle(),
                    LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                    0,
                    1,
                    0,
                    &mut *overlap,
                )
            } == 0
            {
                return Err(busy());
            }
            Ok(Self { file, overlap })
        }
        #[cfg(not(windows))]
        {
            file.try_lock().map_err(|_| busy())?;
            Ok(Self { file })
        }
    }
}
impl Drop for CycleLock {
    fn drop(&mut self) {
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            unsafe {
                windows_sys::Win32::Storage::FileSystem::UnlockFileEx(
                    self.file.as_raw_handle(),
                    0,
                    1,
                    0,
                    &mut *self.overlap,
                );
            }
        }
        #[cfg(not(windows))]
        {
            let _ = self.file.unlock();
        }
    }
}

pub fn overview(state: &Value, limit: usize) -> Result<Value> {
    valid_state(state)?;
    let cfg = &state["settings"];
    let source_ids = cfg["sources"].as_array().ok_or_else(Error::invalid)?;
    let mut items = Vec::new();
    for x in state["items"]
        .as_object()
        .ok_or_else(Error::invalid)?
        .values()
    {
        if !source_ids.contains(&x["source"]) {
            continue;
        }
        let mut x = x.clone();
        x["matched_by"] = matches(&x, cfg)?;
        if x["matched_by"].as_array().is_some_and(|a| !a.is_empty()) {
            items.push(x);
        }
    }
    items.sort_by(|a, b| {
        let key = |x: &Value| {
            [
                x["deadline"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("9999"),
                x["start"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .unwrap_or("9999"),
                x["title"].as_str().unwrap_or(""),
            ]
            .map(str::to_owned)
        };
        key(a).cmp(&key(b))
    });
    let total = items.len();
    let mut dedup = deduplicate(&json!(items))?;
    dedup.as_array_mut().unwrap().truncate(limit);
    let messages: Vec<_> = state["messages"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .iter()
        .rev()
        .map(|x| {
            let mut x = x.clone();
            if let Some(m) = x.as_object_mut() {
                m.remove("items");
            }
            x
        })
        .collect();
    Ok(
        json!({"settings":cfg,"sources":catalog(),"source_health":state["sources"],"items":dedup,"total_matched":total,"truncated":total>limit || state["storage_truncated"]==true,
        "pending_overflow":state.get("pending_overflow").cloned().unwrap_or(json!(0)),"messages":messages,"last_cycle":state["last_cycle"],"preview_only":cfg["enabled"]!=true,
        "note":"未提供截止时间的条目不会猜测日期。列表未出现不代表取消或已完成。"}),
    )
}

fn date(value: &str) -> Result<chrono::DateTime<chrono::FixedOffset>> {
    chrono::DateTime::parse_from_rfc3339(value).map_err(|_| bad("信息中心时间格式无效。"))
}
fn blank(v: &Value) -> bool {
    v.is_null() || v.as_str() == Some("") || v.as_object().is_some_and(|m| m.is_empty())
}
fn valid_state(state: &Value) -> Result<()> {
    settings(&state["settings"])?;
    for k in ["items", "sources", "pending"] {
        if !state[k]
            .as_object()
            .is_some_and(|m| m.values().all(Value::is_object))
        {
            return Err(Error::invalid());
        }
    }
    if !state["messages"]
        .as_array()
        .is_some_and(|a| a.iter().all(Value::is_object))
    {
        return Err(Error::invalid());
    }
    Ok(())
}

/// Reduce one bounded collector response. The adapter has no authority to choose
/// delivery recipients, enable subscriptions, or replace pending/unknown receipts.
pub fn apply_source(input: &Value) -> Result<Value> {
    let mut state = input["state"].clone();
    valid_state(&state)?;
    let source = text(input, "source")?;
    let stamp = text(input, "now")?;
    date(stamp)?;
    let cfg = state["settings"].clone();
    if !cfg["sources"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .contains(&json!(source))
    {
        return Err(Error::invalid());
    }
    let response = &input["response"];
    let rows = response["items"]
        .as_array()
        .filter(|a| a.len() <= 10000)
        .ok_or_else(|| bad("信息源返回格式不正确。"))?;
    let previous = state["sources"][source].clone();
    let baseline = previous["initialized"] != true;
    for item in rows {
        let mut x = item.clone();
        let id = text(&x, "id")?.to_owned();
        text(&x, "title")?;
        text(&x, "source_url")?;
        if x["source"] != source {
            return Err(bad("信息源条目归属不一致。"));
        }
        if let Some(images) = x
            .get_mut("poster_ocr")
            .and_then(|v| v.get_mut("images"))
            .and_then(Value::as_array_mut)
        {
            for image in images {
                if let Some(image) = image.as_object_mut() {
                    image.remove("text");
                }
            }
        }
        if x["summary"]
            .as_str()
            .is_some_and(|s| s.chars().count() > 1200)
        {
            x["summary"] = json!(x["summary"]
                .as_str()
                .unwrap()
                .chars()
                .take(1200)
                .collect::<String>());
            x["summary_truncated"] = json!(true);
        }
        let old = state["items"].get(&id).cloned();
        if let Some(old) = &old {
            if x["poster_ocr"]["errors"]
                .as_array()
                .is_some_and(|a| !a.is_empty())
                && old["poster_ocr"]["images"]
                    .as_array()
                    .is_some_and(|a| !a.is_empty())
            {
                x["summary"] = old.get("summary").cloned().unwrap_or(json!(""));
                x["poster_ocr"] = old["poster_ocr"].clone();
                x["time_candidates"] = old.get("time_candidates").cloned().unwrap_or(json!({}));
                x["detail_unavailable"] = json!(true);
                x["summary_truncated"] = old
                    .get("summary_truncated")
                    .cloned()
                    .unwrap_or(json!(false));
            }
            if blank(&x["summary"]) && old["summary"].as_str().is_some_and(|s| !s.is_empty()) {
                let fresh = x.as_object().ok_or_else(Error::invalid)?;
                let mut merged = old.as_object().ok_or_else(Error::invalid)?.clone();
                for (k, v) in fresh {
                    if !blank(v) {
                        merged.insert(k.clone(), v.clone());
                    }
                }
                x = Value::Object(merged);
            }
        }
        let fp = fingerprint(&x)?;
        let changed = old.as_ref().is_some_and(|old| old["fingerprint"] != fp);
        x["fingerprint"] = fp;
        x["revision"] = json!(old
            .as_ref()
            .and_then(|o| o["revision"].as_u64())
            .unwrap_or(0)
            .checked_add(u64::from(changed))
            .ok_or_else(failure)?);
        x["last_seen"] = json!(stamp);
        x["matched_by"] = matches(&x, &cfg)?;
        x["urgent_key"] = old
            .as_ref()
            .map(|o| o["urgent_key"].clone())
            .unwrap_or(Value::Null);
        state["items"][&id] = x.clone();
        if cfg["enabled"] == true
            && !baseline
            && (old.is_none() || changed)
            && x["matched_by"].as_array().is_some_and(|a| !a.is_empty())
        {
            x["summary"] = json!(x["summary"]
                .as_str()
                .unwrap_or("")
                .chars()
                .take(450)
                .collect::<String>());
            x["change"] = json!(if changed { "changed" } else { "new" });
            state["pending"][&id] = x;
        }
    }
    let health = json!({"state":"ok","initialized":true,"last_success":stamp,"complete":response.get("complete").cloned().unwrap_or(json!(false)),"scope":response["scope"],"next_page":response["next_page"],
        "detail_failures":response.get("detail_failures").cloned().unwrap_or(json!(0)),"failures":0,
        "excluded_low_quality":response.get("excluded_low_quality").cloned().unwrap_or(json!(0)),"unsupported_links":response.get("unsupported_links").cloned().unwrap_or(json!(0))});
    state["sources"][source] = health;
    if previous["state"] == "failed" {
        state["sources"][source]["recovered_at"] = json!(stamp);
    }
    wire_capacity(&state)?;
    Ok(json!({"state":state,"baseline":baseline}))
}

pub fn finish_cycle(input: &Value) -> Result<Value> {
    let mut state = input["state"].clone();
    valid_state(&state)?;
    let cfg = state["settings"].clone();
    let stamp = text(input, "now")?;
    let now = date(stamp)?;
    let notify = input["notify"] == true && cfg["enabled"] == true;
    let mut keep: Vec<Value> = state["items"]
        .as_object()
        .ok_or_else(Error::invalid)?
        .values()
        .cloned()
        .collect();
    keep.sort_by(|a, b| b["last_seen"].as_str().cmp(&a["last_seen"].as_str()));
    state["storage_truncated"] = json!(keep.len() > 1500);
    keep.truncate(1500);
    state["items"] = json!({});
    for item in keep {
        if now - date(text(&item, "last_seen")?)? < chrono::Duration::days(90) {
            let id = text(&item, "id")?.to_owned();
            state["items"][&id] = item;
        }
    }
    let pending = state["pending"]
        .as_object_mut()
        .ok_or_else(Error::invalid)?;
    let overflow = pending.len().saturating_sub(1500);
    if overflow > 0 {
        let keys: Vec<_> = pending.keys().take(overflow).cloned().collect();
        for k in keys {
            pending.shift_remove(&k);
        }
        state["pending_overflow"] = json!(state["pending_overflow"]
            .as_u64()
            .unwrap_or(0)
            .checked_add(overflow as u64)
            .ok_or_else(failure)?);
    }
    let mut urgent = Vec::new();
    let hours = cfg["urgent_hours"].as_i64().ok_or_else(Error::invalid)?;
    if notify && hours > 0 {
        let health = state["sources"].clone();
        for x in state["items"].as_object_mut().unwrap().values_mut() {
            let source = text(x, "source")?;
            if !cfg["sources"]
                .as_array()
                .ok_or_else(Error::invalid)?
                .contains(&json!(source))
                || x["last_seen"] != stamp
                || !x["matched_by"].as_array().is_some_and(|a| !a.is_empty())
                || health[source]["state"] != "ok"
            {
                continue;
            }
            let Some(deadline) = x["deadline"].as_str().map(str::to_owned) else {
                continue;
            };
            let end = if deadline.len() == 10 {
                chrono::NaiveDate::parse_from_str(&deadline, "%Y-%m-%d")
                    .ok()
                    .and_then(|d| d.succ_opt())
                    .and_then(|d| d.and_hms_opt(0, 0, 0))
                    .and_then(|d| {
                        d.and_local_timezone(chrono::FixedOffset::east_opt(8 * 3600).unwrap())
                            .single()
                    })
            } else {
                date(&deadline).ok()
            };
            if let Some(end) = end {
                let delta = end - now;
                if delta >= chrono::Duration::zero()
                    && delta <= chrono::Duration::hours(hours)
                    && x["urgent_key"] != deadline
                {
                    let mut event = x.clone();
                    event["change"] = json!("deadline");
                    urgent.push(event);
                    x["urgent_key"] = json!(deadline);
                }
            }
        }
    }
    let local = now.with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).unwrap());
    let today = local.format("%Y-%m-%d").to_string();
    let due = local.format("%H:%M").to_string().as_str() >= text(&cfg, "daily_time")?
        && state["last_daily"] != today;
    let mut unique = serde_json::Map::new();
    if notify && due {
        for x in state["pending"].as_object().unwrap().values() {
            unique.insert(text(x, "id")?.to_owned(), x.clone());
        }
    }
    for x in &urgent {
        unique.insert(text(x, "id")?.to_owned(), x.clone());
    }
    let mut message_id = Value::Null;
    if !unique.is_empty() {
        let events: Vec<_> = unique.values().cloned().collect();
        let mut report = report_text(&json!(events), &state["sources"])?
            .as_str()
            .unwrap()
            .to_owned();
        if state["pending_overflow"].as_u64().unwrap_or(0) > 0 {
            report.push_str(&format!(
                "\n待汇总变化曾超过 1500 项，另有 {} 项未保留，请缩小订阅范围并核对原站。",
                state["pending_overflow"]
            ));
        }
        message_id = json!(uuid::Uuid::new_v4().simple().to_string());
        let message = json!({"id":message_id,"created_at":stamp,"read":false,"items":events.iter().map(|x|json!({"id":x["id"],"change":x["change"]})).collect::<Vec<_>>(),
            "text":report,"delivery":{"status":if cfg["email_enabled"]==true{"pending"}else{"app_only"}},"subscription_version":state["subscription_version"]});
        let messages = state["messages"]
            .as_array_mut()
            .ok_or_else(Error::invalid)?;
        messages.push(message);
        if messages.len() > 60 {
            messages.drain(..messages.len() - 60);
        }
        if due {
            state["pending"] = json!({});
            state["last_daily"] = json!(today);
            state["pending_overflow"] = json!(0);
        } else {
            for x in urgent {
                state["pending"]
                    .as_object_mut()
                    .unwrap()
                    .shift_remove(text(&x, "id")?);
            }
        }
    } else if notify && due {
        state["last_daily"] = json!(today);
    }
    state["last_cycle"] = json!(stamp);
    wire_capacity(&state)?;
    Ok(json!({"state":state,"message_id":message_id}))
}

pub fn dispatch(input: &Value) -> Result<Value> {
    let action = text(input, "action")?;
    match action {
        "check_paths" => {
            checked_root(input)?;
            return Ok(json!({"valid":true}));
        }
        "settings" => return settings(&input["value"]),
        "defaults" => return Ok(defaults()),
        "catalog" => return Ok(catalog()),
        "fingerprint" => return fingerprint(&input["item"]),
        "matches" => return matches(&input["item"], &input["settings"]),
        "deduplicate" => return deduplicate(&input["items"]),
        "report_text" => return report_text(&input["items"], &input["health"]),
        _ => {}
    }
    if action == "apply_source" {
        return apply_source(input);
    }
    if action == "finish_cycle" {
        return finish_cycle(input);
    }
    let store = Store::open(input)?;
    match action {
        "read" => {
            let state = store.read()?;
            wire_capacity(&state)?;
            Ok(state)
        }
        "save" => {
            store.save(&input["state"])?;
            Ok(json!({"saved":true}))
        }
        "overview" => {
            let limit = input.get("limit").and_then(Value::as_u64).unwrap_or(100);
            if limit > 1500 {
                return Err(Error::invalid());
            }
            overview(&store.read()?, limit as usize)
        }
        "configure" => {
            let cfg = settings(&input["settings"])?;
            if cfg["enabled"] == true && input["authorized"] != true {
                return Err(bad(
                    "开启定时采集与推送前，需要用户确认来源、时间和收件人。",
                ));
            }
            let _guard = CycleLock::acquire(&store.root)?;
            let mut state = store.read()?;
            if cfg["enabled"] == true && cfg["email_enabled"] == true {
                let sender = text(input, "sender")?;
                if !valid_address(sender) {
                    return Err(bad("请先连接通知发送邮箱。"));
                }
                state["sender"] = json!(sender);
            } else if state.get("sender").is_none() {
                state["sender"] = Value::Null;
            }
            if ["sources", "keywords", "exclude", "locations", "bb_courses"]
                .iter()
                .any(|k| state["settings"][k] != cfg[k])
                || cfg["enabled"] == true && state["settings"]["enabled"] != true
            {
                state["sources"] = json!({});
                state["pending"] = json!({});
                state["last_cycle"] = Value::Null;
            }
            state["settings"] = cfg.clone();
            state["configured_at"] = json!(now());
            state["subscription_version"] = json!(uuid::Uuid::new_v4().simple().to_string());
            store.save(&state)?;
            Ok(
                json!({"settings":cfg,"state":if cfg["enabled"]==true{"enabled"}else{"paused"},"baseline":"首次成功采集建立基线，不推送全部历史记录。"}),
            )
        }
        "mark_read" => {
            let id = text(input, "message_id")?;
            let _guard = CycleLock::acquire(&store.root)?;
            let mut state = store.read()?;
            let item = state["messages"]
                .as_array_mut()
                .unwrap()
                .iter_mut()
                .find(|x| x["id"] == id)
                .ok_or_else(|| bad("简报不存在。"))?;
            item["read"] = json!(true);
            store.save(&state)?;
            Ok(json!({"read":true}))
        }
        "preview_report" => {
            let _guard = CycleLock::acquire(&store.root)?;
            let mut state = store.read()?;
            let view = overview(&state, 100)?;
            let mut text = format!(
                "【试阅简报】当前采集快照，不表示这些消息刚刚发布。\n\n{}",
                report_text(&view["items"], &state["sources"])?
                    .as_str()
                    .unwrap()
            );
            if view["items"].as_array().unwrap().is_empty() {
                text.push_str("\n尚无匹配内容，请先采集或检查来源状态。");
            }
            let key = format!("{:x}", Sha256::digest(text.as_bytes()));
            let messages = state["messages"].as_array_mut().unwrap();
            if let Some(old) = messages.iter().find(|x| x["preview_key"] == key) {
                return Ok(
                    json!({"state":"existing","message_id":old["id"],"text":old["text"],"delivery":old["delivery"]}),
                );
            }
            let message = json!({"id":uuid::Uuid::new_v4().simple().to_string(),"created_at":now(),"read":false,"items":[],"text":text,"preview_key":key,"delivery":{"status":"app_only"},"kind":"preview"});
            messages.push(message.clone());
            if messages.len() > 60 {
                messages.drain(..messages.len() - 60);
            }
            store.save(&state)?;
            Ok(
                json!({"state":"created","message_id":message["id"],"text":text,"delivery":message["delivery"]}),
            )
        }
        _ => Err(Error::invalid()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn state() -> Value {
        json!({"owner":"synthetic-owner","settings":defaults(),"items":{},"sources":{},"messages":[],"pending":{},"last_daily":null,"last_cycle":null})
    }
    fn item(id: &str) -> Value {
        json!({"id":id,"source":"teach","title":"合成 AI 讲座","source_url":"https://www.teach.ustc.edu.cn/synthetic","summary":"可核对的正文","published_at":"2026-10-01","deadline":null,"start":null,"location":"合肥","collected_at":"2026-10-01T00:00:00+00:00"})
    }
    fn source(state: Value, rows: Vec<Value>, stamp: &str) -> Value {
        apply_source(&json!({"state":state,"source":"teach","response":{"items":rows,"complete":true},"now":stamp})).unwrap()["state"].clone()
    }
    fn finish(state: Value, stamp: &str, notify: bool) -> Value {
        finish_cycle(&json!({"state":state,"now":stamp,"notify":notify})).unwrap()["state"].clone()
    }

    #[test]
    fn settings_preserve_app_only_and_reject_implicit_permissions() {
        assert_eq!(settings(&json!({})).unwrap(), defaults());
        for v in [
            json!({"enabled":1}),
            json!({"email_enabled":true}),
            json!({"sources":["bb"]}),
            json!({"daily_time":"24:00"}),
            json!({"interval_minutes":true}),
            json!({"unknown":true}),
        ] {
            assert!(settings(&v).is_err());
        }
        for email in [
            ".a@example.com",
            "a..b@example.com",
            "a@-example.com",
            "a@example.c",
            "a@example.com\n",
        ] {
            assert!(!valid_address(email));
        }
        assert!(valid_address("synthetic+digest@example.com"));
        let cfg = settings(&json!({"sources":["teach","teach"],"keywords":[" AI ","AI"]})).unwrap();
        assert_eq!(cfg["sources"], json!(["teach"]));
        assert_eq!(cfg["keywords"], json!(["AI"]));
        assert_eq!(cfg["email_enabled"], false);
    }
    #[test]
    fn fingerprint_ignores_transient_fields_but_not_real_changes() {
        let original = item("x");
        let fp = fingerprint(&original).unwrap();
        let mut changed = original.clone();
        changed["collected_at"] = json!("new");
        changed["detail_unavailable"] = json!(true);
        changed["revision"] = json!(7);
        assert_eq!(fp, fingerprint(&changed).unwrap());
        changed["deadline"] = json!("2026-10-02");
        assert_ne!(fp, fingerprint(&changed).unwrap());
        assert_eq!(
            python_json(&json!({"b":[1,"科大"],"a":null})),
            "{\"a\": null, \"b\": [1, \"科大\"]}"
        );
    }
    #[test]
    fn unicode_filters_and_exact_cross_posts() {
        let mut cfg = defaults();
        cfg["keywords"] = json!(["STRASSE"]);
        let mut row = item("x");
        row["summary"] = json!("Straße");
        assert_eq!(matches(&row, &cfg).unwrap(), json!(["STRASSE"]));
        cfg["locations"] = json!(["北京"]);
        assert_eq!(matches(&row, &cfg).unwrap(), json!([]));
        let row = item("x");
        let mut same = row.clone();
        same["id"] = json!("other");
        same["title"] = json!("合成AI讲座");
        same["source_url"] = json!("https://example.invalid/also");
        let rows = deduplicate(&json!([row, same])).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(
            rows[0]["also_from"],
            json!(["https://example.invalid/also"])
        );
    }
    #[test]
    fn baseline_daily_inbox_and_restart_dedup() {
        let mut s = state();
        s["settings"]["enabled"] = json!(true);
        s["settings"]["urgent_hours"] = json!(0);
        s = finish(
            source(s, vec![item("first")], "2026-10-01T02:00:00+00:00"),
            "2026-10-01T02:00:00+00:00",
            true,
        );
        assert_eq!(s["messages"], json!([]));
        s = finish(
            source(
                s,
                vec![item("first"), item("second")],
                "2026-10-02T02:00:00+00:00",
            ),
            "2026-10-02T02:00:00+00:00",
            true,
        );
        assert_eq!(s["messages"].as_array().unwrap().len(), 1);
        assert_eq!(s["messages"][0]["delivery"]["status"], "app_only");
        assert_eq!(
            s["messages"][0]["items"],
            json!([{"id":"second","change":"new"}])
        );
        s = finish(
            source(
                s,
                vec![item("first"), item("second")],
                "2026-10-03T02:00:00+00:00",
            ),
            "2026-10-03T02:00:00+00:00",
            true,
        );
        assert_eq!(s["messages"].as_array().unwrap().len(), 1);
    }
    #[test]
    fn previews_preserve_pending_and_unknown_receipts() {
        let mut s = state();
        s["settings"]["enabled"] = json!(true);
        s["settings"]["urgent_hours"] = json!(0);
        s["messages"] =
            json!([{"id":"old","delivery":{"status":"unknown","draft_id":"must-keep"}}]);
        s = source(s, vec![item("first")], "2026-10-01T02:00:00+00:00");
        s = finish(
            source(s, vec![item("second")], "2026-10-02T02:00:00+00:00"),
            "2026-10-02T02:00:00+00:00",
            false,
        );
        assert!(s["pending"].get("second").is_some());
        assert_eq!(s["messages"].as_array().unwrap().len(), 1);
        assert_eq!(
            s["messages"][0]["delivery"],
            json!({"status":"unknown","draft_id":"must-keep"})
        );
    }
    #[test]
    fn urgent_date_only_and_explicit_timezone_once() {
        let mut s = state();
        s["settings"]["enabled"] = json!(true);
        s["settings"]["daily_time"] = json!("23:00");
        let mut x = item("first");
        x["deadline"] = json!("2026-10-01");
        s = finish(
            source(s, vec![x.clone()], "2026-10-01T02:00:00+00:00"),
            "2026-10-01T02:00:00+00:00",
            true,
        );
        assert_eq!(s["messages"].as_array().unwrap().len(), 1);
        assert_eq!(s["items"]["first"]["urgent_key"], "2026-10-01");
        s = finish(
            source(s, vec![x], "2026-10-01T03:00:00+00:00"),
            "2026-10-01T03:00:00+00:00",
            true,
        );
        assert_eq!(s["messages"].as_array().unwrap().len(), 1);
    }
    #[test]
    fn transient_details_do_not_erase_or_signal_changes() {
        let s = source(state(), vec![item("first")], "2026-10-01T02:00:00+00:00");
        let before = s["items"]["first"].clone();
        let mut empty = item("first");
        empty["summary"] = json!("");
        empty["detail_unavailable"] = json!(true);
        let after = source(s, vec![empty], "2026-10-02T02:00:00+00:00");
        assert_eq!(
            before["fingerprint"],
            after["items"]["first"]["fingerprint"]
        );
        assert_eq!(after["items"]["first"]["revision"], 0);
        assert_eq!(after["items"]["first"]["summary"], before["summary"]);
    }
    #[test]
    fn invalid_collector_cannot_select_other_source_or_change_state() {
        let s = state();
        let mut row = item("x");
        row["source"] = json!("portal");
        assert!(apply_source(&json!({"state":s,"source":"teach","response":{"items":[row]},"now":"2026-10-01T00:00:00+00:00"})).is_err());
        assert!(finish_cycle(&json!({"state":{},"now":"2026-10-01T00:00:00+00:00"})).is_err());
    }
    #[test]
    fn malformed_history_and_counter_overflow_fail_closed() {
        let mut s = state();
        s["items"]["bad"] = json!(42);
        assert!(overview(&s, 100).is_err());
        let mut s = source(state(), vec![item("x")], "2026-10-01T02:00:00+00:00");
        s["items"]["x"]["revision"] = json!(u64::MAX);
        let mut changed = item("x");
        changed["summary"] = json!("changed");
        assert!(apply_source(&json!({"state":s,"source":"teach","response":{"items":[changed]},"now":"2026-10-02T02:00:00+00:00"})).is_err());
        assert_eq!(
            python_json(&json!([1e-7, 1e-5, 1e16, -0.0])),
            "[1e-07, 1e-05, 1e+16, -0.0]"
        );
    }
    #[test]
    fn storage_rejects_traversal_and_nonregular_targets_before_opening() {
        let root =
            std::env::temp_dir().join(format!("school-digest-paths-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        assert!(
            Store::open(&json!({"root":root.join("..").join("escape"),"owner":"synthetic"}))
                .is_err()
        );
        for name in [
            "digest.sqlite3",
            "digest.sqlite3-wal",
            "digest.sqlite3-shm",
            "digest.sqlite3-journal",
            "digest-cycle.lock",
            "digest-worker.lock",
        ] {
            let path = root.join(name);
            fs::create_dir(&path).unwrap();
            assert!(dispatch(&json!({"action":"check_paths","root":root})).is_err());
            fs::remove_dir(path).unwrap();
        }
        fs::write(root.join("not-directory"), b"keep").unwrap();
        assert!(Store::open(
            &json!({"root":root.join("not-directory").join("profile"),"owner":"synthetic"})
        )
        .is_err());
        assert_eq!(fs::read(root.join("not-directory")).unwrap(), b"keep");
        fs::remove_dir_all(root).unwrap();
    }
    #[cfg(unix)]
    #[test]
    fn storage_rejects_symlink_root_files_and_dangling_sidecars() {
        let root =
            std::env::temp_dir().join(format!("school-digest-links-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        let target = root.join("target");
        fs::create_dir(&target).unwrap();
        std::os::unix::fs::symlink(&target, root.join("alias")).unwrap();
        assert!(Store::open(
            &json!({"root":root.join("alias").join("new-profile"),"owner":"synthetic"})
        )
        .is_err());
        assert!(!target.join("new-profile").exists());
        for name in [
            "digest.sqlite3",
            "digest.sqlite3-wal",
            "digest.sqlite3-shm",
            "digest.sqlite3-journal",
            "digest-cycle.lock",
        ] {
            std::os::unix::fs::symlink(root.join("absent"), target.join(name)).unwrap();
            assert!(checked_files(&target).is_err());
            fs::remove_file(target.join(name)).unwrap();
        }
        fs::remove_dir_all(root).unwrap();
    }
    #[cfg(windows)]
    #[test]
    fn large_legacy_state_supports_local_views_without_full_pipe_transfer() {
        let root =
            std::env::temp_dir().join(format!("school-digest-large-{}", uuid::Uuid::new_v4()));
        let args = json!({"root":root,"owner":"synthetic-owner"});
        let store = Store::open(&args).unwrap();
        let mut state = store.read().unwrap();
        state["historical_extension"] = json!("x".repeat(15 * 1024 * 1024));
        state["messages"] = json!([{"id":"old","read":false,"text":"synthetic","delivery":{"status":"unknown","draft_id":"keep"}}]);
        store.save(&state).unwrap();
        let mut request = args.clone();
        request["action"] = json!("overview");
        assert_eq!(dispatch(&request).unwrap()["messages"][0]["id"], "old");
        request["action"] = json!("mark_read");
        request["message_id"] = json!("old");
        assert_eq!(dispatch(&request).unwrap()["read"], true);
        request["action"] = json!("preview_report");
        assert_eq!(
            dispatch(&request).unwrap()["delivery"]["status"],
            "app_only"
        );
        request["action"] = json!("read");
        assert_eq!(dispatch(&request).unwrap_err().code, "context_capacity");
        let retained = store.read().unwrap();
        assert_eq!(
            retained["historical_extension"].as_str().unwrap().len(),
            15 * 1024 * 1024
        );
        assert_eq!(retained["messages"][0]["delivery"]["status"], "unknown");
        assert_eq!(retained["messages"][0]["read"], true);
        fs::remove_dir_all(root).unwrap();
    }
    #[cfg(windows)]
    #[test]
    fn encrypted_inbox_is_atomic_owner_bound_and_preview_idempotent() {
        let root =
            std::env::temp_dir().join(format!("school-digest-test-{}", uuid::Uuid::new_v4()));
        let args = json!({"root":root,"owner":"synthetic-owner"});
        let store = Store::open(&args).unwrap();
        let mut s = store.read().unwrap();
        s["messages"] = json!([{"id":"old","read":false,"delivery":{"status":"unknown","draft_id":"preserve"},"text":"PRIVATE-CANARY"}]);
        store.save(&s).unwrap();
        assert!(!fs::read(root.join("digest.sqlite3"))
            .unwrap()
            .windows(14)
            .any(|s| s == b"PRIVATE-CANARY"));
        let mut request = args.clone();
        request["action"] = json!("preview_report");
        let a = dispatch(&request).unwrap();
        let b = dispatch(&request).unwrap();
        assert_eq!(a["message_id"], b["message_id"]);
        assert_eq!(b["state"], "existing");
        request["action"] = json!("mark_read");
        request["message_id"] = json!("old");
        dispatch(&request).unwrap();
        assert_eq!(
            store.read().unwrap()["messages"][0]["delivery"]["status"],
            "unknown"
        );
        let foreign = Store::open(&json!({"root":root,"owner":"other"})).unwrap();
        assert!(foreign.read().is_err());
        let mut changed = s.clone();
        changed["owner"] = json!("other");
        assert!(foreign.save(&changed).is_err());
        let lock = CycleLock::acquire(&root).unwrap();
        assert!(dispatch(&request).is_err());
        drop(lock);
        assert_eq!(store.read().unwrap()["messages"][0]["read"], true);
        fs::remove_dir_all(root).unwrap();
    }
}
