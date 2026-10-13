//! Blackboard Original read adapters and one-shot submission business contracts.
//! No network, upload, or script execution. HTML remains untrusted data.
use crate::{Error, Result};
use chrono::{DateTime, FixedOffset, NaiveDate, TimeZone};
use regex::Regex;
use scraper::{ElementRef, Html, Selector};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};
use unicode_casefold::UnicodeCaseFold;
use url::Url;

const BASE: &str = "https://www.bb.ustc.edu.cn";
const ASSIGNMENT: &str = "/webapps/assignment/uploadAssignment";
const CONTENT: &str = "/webapps/blackboard/content/listContent.jsp";
const LAUNCH: &str = "/webapps/blackboard/content/launchLink.jsp";
fn error(message: &str) -> Error {
    Error::new("unavailable", message)
}
fn require(ok: bool, message: &str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(error(message))
    }
}
fn contract(ok: bool) -> Result<()> {
    require(ok, "作业页面与已验证模板不一致，已停止。")
}
fn sel(s: &str) -> Selector {
    Selector::parse(s).expect("constant selector")
}
fn one<'a>(root: ElementRef<'a>, s: &str) -> Option<ElementRef<'a>> {
    root.select(&sel(s)).next()
}
fn nodes<'a>(root: ElementRef<'a>, s: &str) -> Vec<ElementRef<'a>> {
    root.select(&sel(s)).collect()
}
fn re(s: &str) -> Regex {
    Regex::new(s).expect("constant regex")
}
fn string<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key].as_str().ok_or_else(Error::invalid)
}
fn boolean(v: &Value, key: &str) -> Result<bool> {
    v[key].as_bool().ok_or_else(Error::invalid)
}
fn number(v: &Value, key: &str, min: u64, max: u64) -> Result<u64> {
    let n = v[key].as_u64().ok_or_else(Error::invalid)?;
    require(n >= min && n <= max, "BB 参数超出允许范围。")?;
    Ok(n)
}
fn short(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}
fn id(s: &str) -> bool {
    re(r"^_?\d+(?:_\d+)?$").is_match(s)
}
fn host(u: &Url) -> bool {
    matches!(u.host_str(), Some("www.bb.ustc.edu.cn" | "bb.ustc.edu.cn"))
}
fn safe(value: &str) -> Result<Url> {
    require(
        !value.is_empty() && !value.contains(['\r', '\n', '\0']),
        "BB 页面地址无效。",
    )?;
    let u = Url::parse(BASE)
        .unwrap()
        .join(value)
        .map_err(|_| Error::invalid())?;
    require(
        u.scheme() == "https"
            && host(&u)
            && u.port_or_known_default() == Some(443)
            && u.username().is_empty()
            && u.password().is_none(),
        "只能读取中科大 BB 平台的 HTTPS 页面。",
    )?;
    Ok(u)
}
fn normalized(mut u: Url) -> Url {
    let pairs: Vec<_> = u
        .query_pairs()
        .filter(|(k, v)| {
            !["nonce", "csrf", "session", "password", "ticket", "token"]
                .iter()
                .any(|x| k.to_lowercase().contains(x))
                && !(k == "mode" && v == "reset")
                && !(k == "url" && v.is_empty())
        })
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();
    u.set_query(None);
    if !pairs.is_empty() {
        u.query_pairs_mut().extend_pairs(pairs);
    }
    u.set_fragment(None);
    if host(&u) {
        let _ = u.set_scheme("https");
    }
    u
}
fn path(u: &Url) -> String {
    format!(
        "{}{}",
        u.path(),
        u.query().map(|q| format!("?{q}")).unwrap_or_default()
    )
}
fn query(u: &Url, blanks: bool) -> BTreeMap<String, Vec<String>> {
    let mut q: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (k, v) in u.query_pairs() {
        if blanks || !v.is_empty() {
            q.entry(k.into_owned()).or_default().push(v.into_owned());
        }
    }
    q
}
fn exact(q: &BTreeMap<String, Vec<String>>, key: &str, expected: &str) -> bool {
    q.get(key).is_some_and(|v| v.len() == 1 && v[0] == expected)
}
fn in_course(u: &Url, course: &str) -> bool {
    exact(&query(u, false), "course_id", course)
}
fn blocked(e: ElementRef<'_>) -> bool {
    matches!(
        e.value().name(),
        "script" | "style" | "noscript" | "input" | "textarea" | "select"
    )
}
fn text(node: Option<ElementRef<'_>>, separator: &str, clean: bool) -> String {
    node.map(|e| {
        e.descendants()
            .filter_map(|n| {
                let t = n.value().as_text()?;
                if clean && n.ancestors().filter_map(ElementRef::wrap).any(blocked) {
                    return None;
                }
                let t = t.trim();
                if t.is_empty() {
                    None
                } else {
                    Some(t.to_owned())
                }
            })
            .collect::<Vec<_>>()
            .join(separator)
    })
    .unwrap_or_default()
}
fn courses(html: &str) -> Vec<Value> {
    let s = Html::parse_document(html);
    let mut result = vec![];
    let mut seen = HashSet::new();
    for a in nodes(s.root_element(), "a[href]") {
        let href = a.value().attr("href").unwrap_or_default();
        if href.starts_with("javascript:") || href.starts_with('#') {
            continue;
        }
        let Ok(u) = Url::parse(BASE).unwrap().join(href) else {
            continue;
        };
        let u = normalized(u);
        if !host(&u) || !u.username().is_empty() || u.password().is_some() {
            continue;
        }
        let q = query(&u, false);
        let identifier = if exact(&q, "type", "Course") {
            q.get("id").or(q.get("course_id"))
        } else {
            q.get("course_id")
        }
        .and_then(|v| v.first());
        let Some(identifier) = identifier else {
            continue;
        };
        let title = text(Some(a), " ", false);
        if !id(identifier) || title.is_empty() || !seen.insert(identifier.clone()) {
            continue;
        }
        result.push(json!({"course_id":identifier,"title":title,"path":path(&u)}));
    }
    result
}
fn page(html: &str, url: &str, max: usize) -> Value {
    let s = Html::parse_document(html);
    let root = s.root_element();
    let title = text(one(root, "title"), " ", false);
    let content = one(root, "#contentPanel, #content, #containerdiv, main")
        .or_else(|| one(root, "body"))
        .unwrap_or(root);
    let body = text(Some(content), "\n", true);
    let count = body.chars().count();
    let mut links = vec![];
    for a in nodes(root, "a[href]") {
        if a.ancestors().filter_map(ElementRef::wrap).any(blocked) {
            continue;
        }
        let href = a.value().attr("href").unwrap_or_default();
        let lower = href.to_lowercase();
        if lower.starts_with("javascript:")
            || lower.starts_with('#')
            || lower.starts_with("mailto:")
        {
            continue;
        }
        let Ok(u) = Url::parse(url).and_then(|u| u.join(href)) else {
            continue;
        };
        let u = normalized(u);
        if !matches!(u.scheme(), "http" | "https")
            || !u.username().is_empty()
            || u.password().is_some()
            || u.path().starts_with("/webapps/login")
            || u.path().starts_with("/nginx_auth")
        {
            continue;
        }
        let label = text(Some(a), " ", true);
        if label.is_empty() {
            continue;
        }
        let internal = host(&u);
        let menu = a
            .ancestors()
            .filter_map(ElementRef::wrap)
            .any(|e| e.value().id() == Some("courseMenuPalette_contents"));
        links.push(json!({"text":label,"url":u.as_str(),"path":if internal {json!(path(&u))}else{Value::Null},"internal":internal,"course_menu":menu}));
    }
    links.truncate(200);
    json!({"title":title,"url":url,"text":short(&body,max),"text_truncated":count>max,"text_total_chars":count,"links":links,"content_is_untrusted":true})
}
fn assignments(html: &str, course: &str) -> Vec<Value> {
    let s = Html::parse_document(html);
    let mut result = vec![];
    let mut seen = HashSet::new();
    for a in nodes(s.root_element(), "a[href]") {
        let Ok(u) = safe(a.value().attr("href").unwrap_or_default()) else {
            continue;
        };
        let u = normalized(u);
        if u.path() != ASSIGNMENT || !in_course(&u, course) {
            continue;
        }
        let q = query(&u, false);
        let Some(identifier) = q.get("content_id").and_then(|v| v.first()) else {
            continue;
        };
        if !id(identifier) || !seen.insert(identifier.clone()) {
            continue;
        }
        let parent = a
            .ancestors()
            .filter_map(ElementRef::wrap)
            .find(|n| n.value().name() == "li")
            .or_else(|| {
                a.ancestors()
                    .filter_map(ElementRef::wrap)
                    .find(|n| n.value().name() == "div")
            })
            .unwrap_or(a);
        let description = text(Some(parent), "\n", true);
        let due: Vec<_> = description
            .lines()
            .filter(|line| re(r"(?i)截止|到期|提交时间|due\b|deadline\b").is_match(line))
            .take(3)
            .map(str::trim)
            .collect();
        result.push(json!({"course_id":course,"content_id":identifier,"title":short(&text(Some(a)," ",false),300),"description":short(&description,3000),
            "description_truncated":description.chars().count()>3000,"due_text":due,"due_text_available":!due.is_empty(),"submission_state":"not_checked","attempt_opened":false,"entry_path":path(&u)}));
    }
    result
}
fn catalog_step(v: &Value) -> Result<Value> {
    let course = string(v, "course_id")?;
    require(id(course), "请使用有效课程ID。")?;
    let max = number(v, "max_pages", 1, 15)? as usize;
    let limit = number(v, "limit", 1, 50)? as usize;
    let mut state = if v["state"].is_null() {
        let links = v["entry"]["links"].as_array().ok_or_else(Error::invalid)?;
        let mut queue = vec![];
        for link in links {
            if link["course_menu"] != true || link["internal"] != true {
                continue;
            }
            let Some(p) = link["path"].as_str() else {
                continue;
            };
            let Ok(u) = safe(p) else {
                continue;
            };
            if matches!(u.path(), CONTENT | LAUNCH)
                && in_course(&u, course)
                && (u.path() == CONTENT || query(&u, false).contains_key("toc_id"))
            {
                queue.push(json!(p));
            }
        }
        json!({"queue":queue,"visited":[],"assignments":[]})
    } else {
        v["state"].clone()
    };
    let mut queue = state["queue"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .clone();
    let mut visited = state["visited"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .clone();
    let mut found = state["assignments"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .clone();
    contract(queue.len() <= 5000 && visited.len() <= 15 && found.len() <= 5000)?;
    if let Some(html) = v["html"].as_str() {
        let expected = string(&state, "pending")?;
        contract(v["path"] == expected && !visited.contains(&json!(expected)))?;
        visited.push(json!(expected));
        for a in assignments(html, course) {
            if !found.iter().any(|old| old["content_id"] == a["content_id"]) {
                found.push(a);
            }
        }
        let s = Html::parse_document(html);
        for a in nodes(s.root_element(), "a[href]") {
            let Ok(u) = safe(a.value().attr("href").unwrap_or_default()) else {
                continue;
            };
            let u = normalized(u);
            if u.path() == CONTENT && in_course(&u, course) {
                let p = json!(path(&u));
                if !visited.contains(&p) && !queue.contains(&p) {
                    queue.push(p);
                }
            }
        }
    }
    while !queue.is_empty() && visited.len() < max && found.len() < limit {
        let next = queue.remove(0);
        if visited.contains(&next) {
            continue;
        }
        state = json!({"queue":queue,"visited":visited,"assignments":found,"pending":next});
        return Ok(json!({"state":state,"next_path":next,"done":false}));
    }
    let truncated = !queue.is_empty() || found.len() > limit;
    found.truncate(limit);
    let status = if found.is_empty() {
        "not_found_in_scanned_pages"
    } else {
        "entries_found"
    };
    Ok(
        json!({"done":true,"result":{"course_id":course,"assignments":found,"pages_checked":visited.len(),"truncated":truncated,"attempts_opened":false,
        "scope":"reachable_course_content_pages","content_is_untrusted":true,"status":status,"note":"没有找到仅表示本次可达内容页未列出标准BB作业；不覆盖隐藏条目、外部教学工具或其他学期。"}}),
    )
}
fn review_url(v: &Value) -> Result<Value> {
    let course = string(v, "course_id")?;
    let content = string(v, "content_id")?;
    let u = safe(string(v, "entry_path")?)?;
    let q = query(&u, true);
    require(
        id(course)
            && id(content)
            && u.path() == ASSIGNMENT
            && u.fragment().is_none()
            && q.keys()
                .all(|k| ["course_id", "content_id", "mode", "group_id"].contains(&k.as_str()))
            && exact(&q, "course_id", course)
            && exact(&q, "content_id", content)
            && exact(&q, "mode", "view")
            && (!q.contains_key("group_id") || exact(&q, "group_id", "")),
        "此作业入口不是已验证的个人作业查看模式，已停止；不进入新尝试或小组操作。",
    )?;
    Ok(json!(u.as_str()))
}
fn review(html: &str, names: bool) -> Value {
    let s = Html::parse_document(html);
    let root = s.root_element();
    let title = text(one(root, "#pageTitleHeader"), " ", false);
    let history =
        title.starts_with("复查提交历史记录") || title.starts_with("Review Submission History");
    let mut result = json!({"page_kind":if history{"submission_history"}else{"unverified_page"},"submission_state":"unknown","catalog_complete":false,"new_attempt_requested":false,
        "uploaded_by_this_call":false,"submitted_by_this_call":false,"upload_supported":false,"content_is_untrusted":true});
    if !history {
        result["next_action"] = json!("verify_page_template_before_proceeding");
        result["note"] =
            json!("页面不是已验证的历史记录模板；没有运行脚本或操作表单，不能推断未提交。");
        return result;
    }
    let mut due = vec![];
    if let Some(info) = one(root, "#assignmentInfo") {
        for label in nodes(info, "h3") {
            if ["截止日期", "Due Date"].contains(&text(Some(label), " ", false).as_str()) {
                if let Some(p) = label
                    .next_siblings()
                    .filter_map(ElementRef::wrap)
                    .find(|e| e.value().name() == "p")
                {
                    due.push(short(&text(Some(p), " ", false), 300));
                }
            }
        }
    }
    let attempt = one(root, "#currentAttempt_label");
    let date = attempt.and_then(|e| one(e, ".dateStamp"));
    let label = attempt.and_then(|e| one(e, ".mainLabel"));
    let attachments = nodes(root, "#currentAttempt_submissionList a.attachment");
    let mut new = false;
    let mut continuing = false;
    for control in nodes(
        root,
        "#bottom_submitButtonRow input, #bottom_submitButtonRow button, #bottom_submitButtonRow a",
    ) {
        let fallback = text(Some(control), " ", false);
        let value = control
            .value()
            .attr("value")
            .filter(|v| !v.is_empty())
            .unwrap_or(&fallback)
            .trim();
        new |= ["开始新的", "Start New"].contains(&value);
        continuing |= ["继续", "Continue"].contains(&value);
    }
    let extra = json!({"submission_state":if attempt.is_some(){"historical_attempt_present"}else{"unknown"},"due_text":due,"due_text_available":!due.is_empty(),
        "attempt_scope":"currently_displayed_attempt","attempt_time_text":date.map(|e|short(&text(Some(e)," ",false),200)),"late_label_present":re(r"(?i)逾期|迟交|\blate\b").is_match(&text(label," ",false)),
        "attachment_count":attachments.len(),"attachment_names_omitted":!names,"new_attempt_control_present":new,"continue_control_present":continuing});
    result
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    if names {
        result["attachment_names"] = json!(attachments
            .iter()
            .take(50)
            .map(|e| short(&text(Some(*e), " ", false), 300))
            .collect::<Vec<_>>());
        result["attachment_names_truncated"] = json!(attachments.len() > 50);
    }
    result["next_action"] = json!("stop_at_existing_history");
    result["note"]=json!("仅核对当前显示的历史尝试；不是全部历史、不是本次提交回执。存在开始新的按钮不代表目前可重交；未标迟交也不代表按时。");
    result
}
fn deadline(text: &str) -> Option<String> {
    let r = re(
        r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日(?:\s*星期\S)?\s*(上午|下午)?\s*(\d{1,2})[:：](\d{2})",
    );
    let c = r.captures(text)?;
    let year = c[1].parse().ok()?;
    let month = c[2].parse().ok()?;
    let day = c[3].parse().ok()?;
    let mut hour: u32 = c[5].parse().ok()?;
    let minute = c[6].parse().ok()?;
    if let Some(a) = c.get(4) {
        if !(1..=12).contains(&hour) {
            return None;
        }
        hour = hour % 12 + if a.as_str() == "下午" { 12 } else { 0 };
    }
    let d = NaiveDate::from_ymd_opt(year, month, day)?.and_hms_opt(hour, minute, 0)?;
    Some(
        FixedOffset::east_opt(28800)?
            .from_local_datetime(&d)
            .single()?
            .to_rfc3339(),
    )
}
fn download(v: &Value) -> Result<Value> {
    let a = &v["assignment"];
    let u = safe(string(v, "url")?)?;
    let q = query(&u, true);
    contract(
        u.path() == "/webapps/assignment/download"
            && u.fragment().is_none()
            && q.keys()
                .all(|k| ["course_id", "attempt_id", "file_id", "fileName"].contains(&k.as_str()))
            && exact(&q, "course_id", string(a, "course_id")?),
    )?;
    for key in ["attempt_id", "file_id"] {
        contract(
            q.get(key)
                .is_some_and(|x| x.len() == 1 && re(r"^_\d+_\d+$").is_match(&x[0])),
        )?;
    }
    if let Some(attempt) = v["attempt_id"].as_str() {
        contract(exact(&q, "attempt_id", attempt))?;
    }
    Ok(json!([u.as_str(), q["attempt_id"][0]]))
}
fn form(html: &str, a: &Value) -> Result<Value> {
    let s = Html::parse_document(html);
    let f = one(s.root_element(), "#uploadAssignmentFormId")
        .ok_or_else(|| error("尚未识别支持的个人作业上传表单。"))?;
    let target = safe(f.value().attr("action").unwrap_or_default())?;
    let q = query(&target, false);
    contract(
        target.path() == ASSIGNMENT
            && q.len() == 1
            && exact(&q, "action", "submit")
            && f.value()
                .attr("method")
                .unwrap_or_default()
                .eq_ignore_ascii_case("post")
            && f.value().attr("enctype") == Some("multipart/form-data"),
    )?;
    let inputs = nodes(f, "input[name]");
    let values = |name: &str| {
        inputs
            .iter()
            .filter(|n| n.value().attr("name") == Some(name))
            .map(|n| n.value().attr("value").unwrap_or_default())
            .collect::<Vec<_>>()
    };
    for key in ["course_id", "content_id"] {
        require(
            values(key) == [string(a, key)?],
            "提交表单目标与准备记录不一致。",
        )?;
    }
    require(
        values("attempt_id") == [""] && values("remove_file_id") == [""],
        "发现已有草稿/附件操作，不自动续写或替换。",
    )?;
    for n in inputs {
        require(
            !(n.value()
                .attr("name")
                .unwrap_or_default()
                .to_lowercase()
                .contains("group")
                && !n.value().attr("value").unwrap_or_default().is_empty()),
            "小组作业尚未适配。",
        )?;
    }
    contract(
        one(f, "#newFile_chooseLocalFile").is_some()
            && one(f, "input[name=\"bottom_提交\"][type=\"submit\"]").is_some(),
    )?;
    for area in nodes(f, "textarea") {
        require(
            text(Some(area), "", false).is_empty(),
            "表单已有文本或附言，不能覆盖；请先核实既有草稿。",
        )?;
    }
    require(
        nodes(f, "#newFile_table a.attachment").is_empty(),
        "发现已有附件，不能自动覆盖。",
    )?;
    Ok(json!(target.as_str()))
}
fn snapshot(html: &str, a: &Value) -> Result<Value> {
    let s = Html::parse_document(html);
    let root = s.root_element();
    let r = review(html, true);
    if r["page_kind"] == "submission_history" {
        require(
            r["continue_control_present"] != true,
            "发现继续作答/草稿入口，当前工具不自动续写。",
        )?;
        let mut files = vec![];
        let mut attempts = HashSet::new();
        for row in nodes(root, "#currentAttempt_submissionList li") {
            let name = one(row, "a.attachment").ok_or_else(Error::invalid)?;
            let link = one(row, "a.dwnldBtn[href]").ok_or_else(Error::invalid)?;
            let d = download(&json!({"assignment":a,"url":link.value().attr("href")}))?;
            attempts.insert(string_index(&d, 1)?.to_owned());
            files.push(json!({"filename":text(Some(name)," ",false),"url":d[0]}));
        }
        require(
            !files.is_empty() && attempts.len() == 1,
            "历史尝试缺少唯一可核验的附件记录，已停止。",
        )?;
        let mut new_url = Value::Null;
        if let Some(button) = one(
            root,
            "input[value=\"开始新的\"], input[value=\"Start New\"]",
        ) {
            let r = re(r"^document\.location='([^']+)';$");
            let c = r
                .captures(button.value().attr("onclick").unwrap_or_default())
                .ok_or_else(Error::invalid)?;
            let u = safe(&c[1])?;
            let q = query(&u, false);
            contract(
                u.path() == ASSIGNMENT
                    && q.len() == 3
                    && exact(&q, "action", "newAttempt")
                    && exact(&q, "course_id", string(a, "course_id")?)
                    && exact(&q, "content_id", string(a, "content_id")?),
            )?;
            new_url = json!(u.as_str());
        }
        let due = r["due_text"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join(" ");
        Ok(
            json!({"kind":"history","attempt_id":attempts.into_iter().next(),"files":files,"new_url":new_url,"due_text":due,"due_at":deadline(&due),"attempt_time_text":r["attempt_time_text"],"late":r["late_label_present"]}),
        )
    } else {
        form(html, a)?;
        let body = text(Some(one(root, "#contentPanel").unwrap_or(root)), " ", true);
        let due = re(r"截止日期\s*(.{0,90})")
            .captures(&body)
            .map(|c| {
                c[1].split("满分")
                    .next()
                    .unwrap_or_default()
                    .trim()
                    .to_owned()
            })
            .unwrap_or_default();
        Ok(
            json!({"kind":"new_form","attempt_id":null,"files":[],"new_url":null,"due_text":due,"due_at":deadline(&due),"attempt_time_text":null,"late":false}),
        )
    }
}
fn string_index(v: &Value, index: usize) -> Result<&str> {
    v[index].as_str().ok_or_else(Error::invalid)
}
fn baseline(current: &Value) -> Value {
    json!({"kind":current["kind"],"attempt_id":current["attempt_id"],"files":current["files"],"due_text":current["due_text"],"due_at":current["due_at"]})
}
fn when(value: &Value) -> Result<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(value.as_str().ok_or_else(Error::invalid)?)
        .map_err(|_| Error::invalid())
}
fn preflight(v: &Value) -> Result<Value> {
    let p = &v["plan"];
    let now = when(&v["now"])?;
    let prepared = when(&p["prepared_at"])?;
    require(
        now >= prepared,
        "准备时间晚于当前时间，请先核对本机时间后重新准备。",
    )?;
    require(
        now.signed_duration_since(prepared).num_seconds() < 24 * 3600,
        "准备记录已超过24小时，请重新准备。",
    )?;
    require(
        p["session_binding"] == v["session_binding"],
        "登录会话已变化，请重新准备以核实账号和目标。",
    )?;
    require(
        p["assignment"] == v["assignment"],
        "作业要求或入口已改变，请重新准备。",
    )?;
    let current = &v["current"];
    require(
        baseline(current) == p["baseline"],
        "已有提交或截止条件发生变化，请重新准备。",
    )?;
    require(
        current["due_at"].is_string(),
        "无法确定截止时间，暂不自动提交。",
    )?;
    require(
        boolean(p, "allow_late")? || now <= when(&current["due_at"])?,
        "作业已逾期，需要在用户知情下重新准备并允许迟交。",
    )?;
    Ok(json!({"allowed":true}))
}
fn prepare_check(v: &Value) -> Result<Value> {
    let current = &v["current"];
    let resubmit = boolean(v, "resubmit")?;
    let reuse = boolean(v, "reuse_previous_files")?;
    let files = v["files"].as_array().ok_or_else(Error::invalid)?;
    require(
        string(v, "comment")?.chars().count() <= 10000,
        "附言最多10000字符。",
    )?;
    require(
        (current["kind"] == "history") == resubmit,
        "已有提交请明确选择resubmit；首次提交不能使用重交模式。",
    )?;
    require(
        !resubmit || current["new_url"].as_str().is_some_and(|x| !x.is_empty()),
        "作业没有提供重新提交入口。",
    )?;
    require(!reuse || resubmit, "复用原附件需要已有提交。")?;
    require(
        !files.is_empty() != reuse,
        "指定本地文件与复用原附件必须二选一。",
    )?;
    let count = if reuse {
        current["files"]
            .as_array()
            .ok_or_else(Error::invalid)?
            .len()
    } else {
        files.len()
    };
    require((1..=10).contains(&count), "仅支持1至10个附件。")?;
    Ok(json!({"allowed":true}))
}
fn receipt(v: &Value) -> Result<Value> {
    let current = &v["current"];
    let p = &v["plan"];
    require(
        current["kind"] == "history" && current["attempt_id"] != p["baseline"]["attempt_id"],
        "未出现可区分于准备时的新尝试，结果待核实。",
    )?;
    let expected = p["files"].as_array().ok_or_else(Error::invalid)?;
    let actual = v["actual"].as_array().ok_or_else(Error::invalid)?;
    require(
        current["files"]
            .as_array()
            .ok_or_else(Error::invalid)?
            .len()
            == expected.len(),
        "新记录附件数量不匹配，结果待核实。",
    )?;
    let normalize = |files: &[Value]| -> Result<Vec<Value>> {
        let mut files:Vec<_>=files.iter().map(|f|json!({"filename":f["filename"],"size_bytes":f["size_bytes"],"sha256":f["sha256"]})).collect();
        for f in &files {
            string(f, "filename")?;
            number(f, "size_bytes", 1, 50 * 1024 * 1024)?;
            contract(re(r"^[a-f0-9]{64}$").is_match(string(f, "sha256")?))?;
        }
        files.sort_by(|a, b| a["filename"].as_str().cmp(&b["filename"].as_str()));
        Ok(files)
    };
    require(
        normalize(actual)? == normalize(expected)?,
        "新记录附件与准备材料不一致，结果待核实。",
    )?;
    Ok(
        json!({"verified":true,"new_attempt_id":current["attempt_id"],"submitted_at_text":current["attempt_time_text"],"late_label_present":current["late"],"files_match":true,"attachment_count":actual.len()}),
    )
}
fn status(v: &Value) -> Result<Value> {
    let job = &v["job"];
    let p = &job["plan"];
    let due = &p["baseline"]["due_at"];
    let late = if due.is_null() {
        Value::Null
    } else {
        json!(when(&v["now"])? > when(due)?)
    };
    let files = p["files"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .iter()
        .map(
            |f| json!({"filename":f["filename"],"size_bytes":f["size_bytes"],"sha256":f["sha256"]}),
        )
        .collect::<Vec<_>>();
    let state = string(job, "state")?;
    Ok(
        json!({"preparation_id":v["identifier"],"content_sha256":p["sha256"],"state":state,"phase":job["phase"],"assignment":{"course_id":p["assignment"]["course_id"],"content_id":p["assignment"]["content_id"],"title":p["assignment"]["title"]},"files":files,"comment":p["comment"],"resubmit":p["resubmit"],"due_text":p["baseline"]["due_text"],"late_now":late,"allow_late":p["allow_late"],"deadline_known":!due.is_null(),"result":job["result"],"can_execute":state=="ready"&&!due.is_null()&&(late!=true||boolean(p,"allow_late")?),"next_action":if state=="ready"{"review_materials_and_authorization"}else if ["uncertain","executing"].contains(&state){"read_only_verify_or_manual_review"}else{"done"},"note":"ready只表示材料已冻结；最终操作需用户授权。重复执行同一ID不重交；结果不明时查询，不能另建记录重试。"}),
    )
}
fn multipart(v: &Value) -> Result<Value> {
    let fields = v["fields"].as_object().ok_or_else(Error::invalid)?;
    let p = &v["plan"];
    let a = &p["assignment"];
    for k in ["course_id", "content_id"] {
        contract(fields.get(k) == Some(&json!([string(a, k)?])))?;
    }
    for (k, value) in [
        ("dispatch", "submit"),
        ("attempt_id", ""),
        ("remove_file_id", ""),
    ] {
        contract(fields.get(k) == Some(&json!([value])))?;
    }
    let names = p["files"]
        .as_array()
        .ok_or_else(Error::invalid)?
        .iter()
        .map(|f| f["filename"].clone())
        .collect::<Vec<_>>();
    require(v["files"] == json!(names), "待传附件与准备记录不一致。")?;
    for (k, values) in fields {
        let values = values.as_array().ok_or_else(Error::invalid)?;
        require(
            !(k.to_lowercase().contains("group")
                && values
                    .iter()
                    .any(|x| x.as_str().is_none_or(|s| !s.is_empty()))),
            "小组作业不支持。",
        )?;
    }
    let field_text = |key: &str, separator: &str| -> Result<String> {
        let values = fields
            .get(key)
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let values = values
            .iter()
            .map(|v| v.as_str().ok_or_else(Error::invalid))
            .collect::<Result<Vec<_>>>()?
            .join(" ");
        let s = Html::parse_fragment(&values);
        Ok(text(Some(s.root_element()), separator, false))
    };
    require(
        field_text("studentSubmission.text", "")?.is_empty(),
        "出现未授权的正文内容。",
    )?;
    require(
        field_text("student_commentstext", "\n")?.trim() == string(p, "comment")?.trim(),
        "实际附言与准备记录不一致。",
    )?;
    Ok(json!({"allowed":true}))
}

pub fn dispatch(v: &Value) -> Result<Value> {
    let action = string(v, "action")?;
    if let Some(html) = v["html"].as_str() {
        require(
            html.len() <= 5 * 1024 * 1024,
            "此 BB 页面超过读取大小限制。",
        )?;
    }
    match action {
        "safe_url" => Ok(json!(safe(string(v, "path")?)?.as_str())),
        "course_path" | "announcement_path" => {
            let course = string(v, "course_id")?;
            require(id(course), "请使用课程列表中的有效 course_id。")?;
            Ok(json!(if action == "course_path" {
                format!("/webapps/blackboard/execute/launcher?type=Course&id={course}")
            } else {
                format!("/webapps/blackboard/execute/announcement?method=search&context=course_entry&course_id={course}&handle=announcements_entry&mode=view")
            }))
        }
        "login_page" => {
            let u = Url::parse(string(v, "url")?).map_err(|_| Error::invalid())?;
            let s = Html::parse_document(string(v, "html")?);
            Ok(json!(
                !host(&u)
                    || u.path().starts_with("/nginx_auth/")
                    || u.path().starts_with("/webapps/login")
                    || one(s.root_element(), "input[type=\"password\"], form#loginForm").is_some()
            ))
        }
        "course_links" => Ok(json!(courses(string(v, "html")?))),
        "check" => {
            let html = string(v, "html")?;
            Ok(
                json!({"connected":true,"title":page(html,string(v,"url")?,1000)["title"],"course_links_in_portal":courses(html).len(),"read_only":true}),
            )
        }
        "courses" => {
            let keyword = string(v, "keyword")?;
            let term = string(v, "term")?;
            require(
                keyword.chars().count() <= 200
                    && (term.is_empty() || re(r"^20\d{2}(?:FA|SP|SU|WI)$").is_match(term)),
                "课程关键字过长，或学期格式无效；学期示例为 2026FA。",
            )?;
            let all = courses(string(v, "html")?);
            let found = all
                .iter()
                .filter(|c| {
                    c["title"]
                        .as_str()
                        .unwrap()
                        .case_fold()
                        .collect::<String>()
                        .contains(&keyword.case_fold().collect::<String>())
                        && (term.is_empty() || c["title"].as_str().unwrap().contains(term))
                })
                .collect::<Vec<_>>();
            Ok(
                json!({"courses":found,"count":found.len(),"total_in_portal":all.len(),"source":v["url"],"content_is_untrusted":true,"note":if all.is_empty(){"仅返回门户 HTML 中提供的课程；若门户含动态课程模块，需要单独适配。"}else{""}}),
            )
        }
        "page" => Ok(page(
            string(v, "html")?,
            string(v, "url")?,
            number(v, "max_chars", 1, 100000)? as usize,
        )),
        "assignments" => {
            let course = string(v, "course_id")?;
            require(id(course), "请使用有效课程ID。")?;
            Ok(json!(assignments(string(v, "html")?, course)))
        }
        "catalog_step" => catalog_step(v),
        "review_url" => review_url(v),
        "review" => Ok(review(
            string(v, "html")?,
            boolean(v, "include_attachment_names")?,
        )),
        "deadline" => Ok(json!(deadline(string(v, "text")?))),
        "download" => download(v),
        "form" => form(string(v, "html")?, &v["assignment"]),
        "snapshot" => snapshot(string(v, "html")?, &v["assignment"]),
        "prepare_check" => prepare_check(v),
        "preflight" => preflight(v),
        "receipt" => receipt(v),
        "status" => status(v),
        "multipart" => multipart(v),
        "static_request" => {
            let u = Url::parse(string(v, "url")?).map_err(|_| Error::invalid())?;
            Ok(json!(
                v["method"] == "GET"
                    && u.scheme() == "https"
                    && u.host_str() == Some("www.bb.ustc.edu.cn")
                    && u.port_or_known_default() == Some(443)
                    && u.username().is_empty()
                    && u.password().is_none()
                    && [
                        "/javascript/",
                        "/groupjs/",
                        "/webapps/",
                        "/common/",
                        "/themes/",
                        "/images/",
                        "/branding/"
                    ]
                    .iter()
                    .any(|x| u.path().starts_with(x))
                    && [".js", ".css", ".png", ".gif", ".jpg", ".svg", ".woff", ".woff2", ".ico"]
                        .iter()
                        .any(|x| u.path().ends_with(x))
            ))
        }
        _ => Err(Error::invalid()),
    }
}
