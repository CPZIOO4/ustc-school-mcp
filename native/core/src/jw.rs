//! Deterministic read-only teaching-administration parsing and timetable search.
//! This module accepts response data, never runs page scripts or enrolment writes.
use crate::{Error, Result};
use regex::Regex;
use scraper::{ElementRef, Html, Selector};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashSet};
use unicode_casefold::UnicodeCaseFold;
use url::Url;

const BASE: &str = "https://jw.ustc.edu.cn";
fn error(message: &str) -> Error {
    Error::new("unavailable", message)
}
fn need(condition: bool, message: &str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(error(message))
    }
}
fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("constant pattern")
}
fn sel(pattern: &str) -> Selector {
    Selector::parse(pattern).expect("constant selector")
}
fn list<'a>(input: &'a Value, key: &str) -> Result<&'a Vec<Value>> {
    input[key].as_array().ok_or_else(Error::invalid)
}
fn str_arg<'a>(input: &'a Value, key: &str, default: &'a str) -> Result<&'a str> {
    input
        .get(key)
        .map_or(Ok(default), |v| v.as_str().ok_or_else(Error::invalid))
}
fn uint(input: &Value, key: &str, default: u64, maximum: u64) -> Result<u64> {
    let value = input
        .get(key)
        .map_or(Ok(default), |v| v.as_u64().ok_or_else(Error::invalid))?;
    if value > maximum {
        Err(Error::invalid())
    } else {
        Ok(value)
    }
}
fn boolean(input: &Value, key: &str, default: bool) -> Result<bool> {
    input
        .get(key)
        .map_or(Ok(default), |v| v.as_bool().ok_or_else(Error::invalid))
}
fn text(node: ElementRef<'_>, separator: &str) -> String {
    node.descendants()
        .filter_map(|n| {
            let value = n.value().as_text()?;
            if n.ancestors().filter_map(ElementRef::wrap).any(|e| {
                matches!(
                    e.value().name(),
                    "script" | "style" | "input" | "textarea" | "select"
                )
            }) {
                return None;
            }
            let value = value.trim();
            if value.is_empty() {
                None
            } else {
                Some(value.to_owned())
            }
        })
        .collect::<Vec<_>>()
        .join(separator)
}
fn all_text(node: ElementRef<'_>) -> String {
    node.text()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join("")
}
fn json_body(input: &Value) -> Result<Value> {
    serde_json::from_str(str_arg(input, "text", "")?)
        .map_err(|_| error("教务接口未返回有效 JSON，可能需要重新登录。"))
}
fn keyword(input: &Value) -> Result<String> {
    let word = str_arg(input, "keyword", "")?;
    if word.chars().count() > 200 {
        Err(Error::invalid())
    } else {
        Ok(fold(word))
    }
}
fn fold(text: &str) -> String {
    text.case_fold().collect()
}
fn value_string(value: &Value) -> String {
    value.as_str().map(str::to_owned).unwrap_or_else(|| {
        if value.is_null() {
            "None".to_owned()
        } else {
            value.to_string()
        }
    })
}
fn lesson_row(lesson: &Value) -> Value {
    let course = &lesson["course"];
    let mut teachers = Vec::new();
    if let Some(assignments) = lesson["teacherAssignmentList"].as_array() {
        for assignment in assignments {
            let person = if assignment["person"].is_object() {
                &assignment["person"]
            } else {
                &assignment["teacher"]["person"]
            };
            let name = person["nameZh"]
                .as_str()
                .filter(|s| !s.is_empty())
                .or_else(|| person["nameEn"].as_str().filter(|s| !s.is_empty()));
            if let Some(name) = name {
                if !teachers.contains(&name) {
                    teachers.push(name);
                }
            }
        }
    }
    let name = course["nameZh"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map_or_else(|| course["nameEn"].clone(), |v| json!(v));
    json!({"lesson_id":lesson["id"],"course_code":course["code"],"course_name":name,"class_code":lesson["code"],
        "credits":lesson.get("credits").unwrap_or(&course["credits"]),"teachers":teachers,"campus":lesson["campus"]["nameZh"],
        "teaching_class":lesson["nameZh"],"remark":lesson["remark"]})
}
fn context(input: &Value) -> Result<Value> {
    let html = str_arg(input, "text", "")?;
    let url = Url::parse(str_arg(input, "url", "")?).map_err(|_| Error::invalid())?;
    let captures = re(r"^/for-std/course-table/info/(\d+)$");
    let student = captures
        .captures(url.path())
        .and_then(|c| c[1].parse::<u64>().ok())
        .ok_or_else(|| error("教务课表页面结构已变化，无法识别当前学生与学期。"))?;
    let doc = Html::parse_document(html);
    let select = doc
        .select(&sel("select#allSemesters"))
        .next()
        .ok_or_else(|| error("教务课表页面结构已变化，无法识别当前学生与学期。"))?;
    let semesters: Vec<Value> = select
        .select(&sel("option[value]"))
        .filter_map(|o| {
            let id = o.value().attr("value")?.parse::<u64>().ok()?;
            Some(json!({"id":id,"name":all_text(o),"current":o.value().attr("selected").is_some()}))
        })
        .collect();
    let current = semesters
        .iter()
        .find(|s| s["current"] == true)
        .map(|s| s["id"].clone());
    let biz = re(r"bizTypeId\s*:\s*(\d+)")
        .captures(html)
        .and_then(|v| v[1].parse::<u64>().ok());
    need(
        current.is_some() && biz.is_some(),
        "课表页面未提供有效的当前学期或学生业务类型。",
    )?;
    Ok(
        json!({"student_id":student,"biz_type_id":biz,"current_semester_id":current,"semesters":semesters}),
    )
}
fn schedule_slots(text: &str, campus: &str) -> Vec<Value> {
    let pattern = re(r"^\s*([\d,~\-]+)周\s+.*?[:：]\s*([1-7])\(([\d,]+)\)(?:\s+.*)?$");
    let mut slots = Vec::new();
    for line in text.lines() {
        let Some(m) = pattern.captures(line) else {
            return vec![];
        };
        let mut weeks = BTreeSet::new();
        for item in m[1].split(',') {
            let ends = item
                .split(['~', '-'])
                .map(str::parse::<u64>)
                .collect::<std::result::Result<Vec<_>, _>>();
            let Ok(ends) = ends else {
                return vec![];
            };
            if ends.is_empty() || ends.len() > 2 {
                return vec![];
            }
            let low = ends[0];
            let high = *ends.last().unwrap();
            if low < 1 || low > high || high > 60 {
                return vec![];
            }
            weeks.extend(low..=high);
        }
        let periods = m[3]
            .split(',')
            .map(str::parse::<u64>)
            .collect::<std::result::Result<BTreeSet<_>, _>>();
        let Ok(periods) = periods else {
            return vec![];
        };
        if periods.is_empty() || *periods.first().unwrap() < 1 || *periods.last().unwrap() > 20 {
            return vec![];
        }
        let day = m[2].parse::<u64>().unwrap();
        for period in periods {
            slots.push(json!({"weekday":(day+5)%7+1,"start_period":period,"end_period":period,"weeks":weeks,"campus":campus}));
        }
    }
    slots
}
fn timetable(input: &Value) -> Result<Value> {
    let week = uint(input, "week", 0, 10_000_000)?;
    let weekday = uint(input, "weekday", 0, 7)?;
    let data = &input["data"];
    need(
        week == 0
            || data["weekIndices"]
                .as_array()
                .is_some_and(|a| a.contains(&json!(week))),
        "周次不在该学期课表提供的范围内。",
    )?;
    let result = &input["result"];
    let activities = result["studentTableVm"]["activities"]
        .as_array()
        .ok_or_else(|| error("课表结构已变化。"))?;
    let dates = if week != 0 {
        data["oddWeekIndex2dayOfWeek2Date"]
            .get(week.to_string())
            .cloned()
            .unwrap_or(json!({}))
    } else {
        json!({})
    };
    let fields = [
        ("lessonId", "lesson_id"),
        ("lessonCode", "class_code"),
        ("courseCode", "course_code"),
        ("courseName", "course_name"),
        ("weeksArray", "weeks"),
        ("weeksStr", "weeks_text"),
        ("weekday", "weekday"),
        ("startUnit", "start_period"),
        ("endUnit", "end_period"),
        ("startDate", "start"),
        ("endDate", "end"),
        ("room", "room"),
        ("building", "building"),
        ("campus", "campus"),
        ("teachers", "teachers"),
        ("credits", "credits"),
        ("lessonRemark", "remark"),
        ("groupNo", "group"),
    ];
    let rows: Vec<Value> = activities
        .iter()
        .filter(|a| {
            (week == 0
                || a["weeksArray"]
                    .as_array()
                    .is_some_and(|w| w.contains(&json!(week))))
                && (weekday == 0 || a["weekday"] == weekday)
        })
        .map(|a| {
            let mut row = serde_json::Map::new();
            for (old, new) in fields {
                row.insert(new.to_owned(), a[old].clone());
            }
            row.insert(
                "self_defined_dates".to_owned(),
                json!(truthy(&a["selfDefineDate"])),
            );
            if dates.as_object().is_some_and(|d| !d.is_empty()) {
                row.insert(
                    "date".to_owned(),
                    dates[value_string(&a["weekday"])].clone(),
                );
            }
            Value::Object(row)
        })
        .collect();
    Ok(
        json!({"semester":input["context"]["semester"],"week":week,"weekday":weekday,"current_week":data["currentWeek"],"count":rows.len(),"activities":rows,"week_dates":dates,"content_is_untrusted":true,"note":"按教务平台的周次和星期编号返回安排；可能包含不同教学分组。"}),
    )
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::String(v) => !v.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
fn grades(input: &Value) -> Result<Value> {
    let data = &input["data"];
    let semesters = data["semesters"]
        .as_array()
        .ok_or_else(|| error("成绩接口结构已变化。"))?;
    let word = keyword(input)?;
    let summary = boolean(input, "summary_only", false)?;
    let mut rows = Vec::new();
    for semester in semesters {
        let Some(scores) = semester["scores"].as_array() else {
            continue;
        };
        for score in scores {
            let name = score["courseNameCh"]
                .as_str()
                .filter(|s| !s.is_empty())
                .or_else(|| score["courseNameEn"].as_str())
                .unwrap_or("");
            if !fold(name).contains(&word) {
                continue;
            }
            let visible = score["canSee"] != false;
            rows.push(json!({"semester_id":semester["id"],"semester_name":score["semesterCh"],"course_name":name,"course_code":score["courseCode"],"class_code":score["lessonCode"],"hours":score["total"],"credits":score["credits"],"visible":visible,"score":if visible {score["scoreCh"].clone()}else{Value::Null},"grade_point":if visible {score["gp"].clone()}else{Value::Null},"passed":if visible {score["passed"].clone()}else{Value::Null}}));
        }
    }
    let mut overview = serde_json::Map::new();
    for key in [
        "passedCredits",
        "gpa",
        "weightedScore",
        "notPassedCredits",
        "arithmeticScore",
    ] {
        overview.insert(key.to_owned(), data["overview"][key].clone());
    }
    let identifiers = list(input, "identifiers")?;
    let returned: Vec<Value> = list(input, "semesters")?
        .iter()
        .filter(|s| identifiers.contains(&s["id"]))
        .map(|s| json!({"id":s["id"],"name":s["nameZh"]}))
        .collect();
    Ok(
        json!({"train_type_id":uint(input,"train_type_id",1,10_000_000)?,"semesters":returned,"grades":if summary {vec![]} else {rows.clone()},"details_omitted":summary,"count":rows.len(),"overview":overview,"overview_scope":"所请求学期的完整成绩，关键词仅筛选课程行。","content_is_untrusted":true}),
    )
}
fn offerings_context(input: &Value) -> Result<Value> {
    let semester = uint(input, "semester_id", 0, 10_000_000)?;
    let page = uint(input, "page", 1, 100)?;
    let limit = uint(input, "limit", 20, 50)?;
    let word = str_arg(input, "keyword", "")?.trim();
    need(
        semester > 0 && !word.is_empty() && word.chars().count() <= 100,
        "提供实时学期ID及1–100字课程关键词。",
    )?;
    need(page > 0 && limit > 0, "页码1–100，每页1–50。")?;
    let doc = Html::parse_document(str_arg(input, "text", "")?);
    need(
        doc.select(&sel("#semester option"))
            .any(|o| o.value().attr("value") == Some(semester.to_string().as_str())),
        "学期不在全校开课查询范围。",
    )?;
    need(
        doc.select(&sel("#weekIndexs option[value='2']"))
            .next()
            .is_some_and(|o| all_text(o).contains("星期一")),
        "星期编码已变化，不能可靠排课。",
    )?;
    let url = Url::parse(str_arg(input, "url", "")?).map_err(|_| Error::invalid())?;
    let student = re(r"^/for-std/lesson-search/index/(\d+)$")
        .captures(url.path())
        .map(|c| c[1].to_owned())
        .ok_or_else(|| error("全校开课查询结构已变化。"))?;
    Ok(
        json!({"path":format!("/for-std/lesson-search/semester/{semester}/search/{student}"),"params":{"courseNameZhLike":word,"queryPage__":format!("{page},{limit}")}}),
    )
}
fn offerings(input: &Value) -> Result<Value> {
    let semester = uint(input, "semester_id", 0, 10_000_000)?;
    let page = uint(input, "page", 1, 100)?;
    let limit = uint(input, "limit", 20, 50)? as usize;
    need(semester > 0 && page > 0 && limit > 0, "页码或学期无效。")?;
    let raw = &input["data"];
    let items = raw["data"]
        .as_array()
        .ok_or_else(|| error("开课查询格式已变化。"))?;
    let paging = &raw["_page_"];
    for key in ["currentPage", "rowsPerPage", "totalRows", "totalPages"] {
        need(paging[key].as_u64().is_some(), "开课分页字段异常。")?;
    }
    need(
        paging["currentPage"] == page,
        "学校返回页码不符，不能把第一页当后续页。",
    )?;
    let mut rows = Vec::new();
    for item in items.iter().take(limit) {
        let mut row = lesson_row(item);
        let text = item["scheduleText"]["dateTimePlacePersonText"]["textZh"]
            .as_str()
            .unwrap_or("");
        let slots = schedule_slots(text, row["campus"].as_str().unwrap_or(""));
        let complete = truthy(&row["course_code"])
            && truthy(&row["course_name"])
            && !row["lesson_id"].is_null()
            && row["credits"].is_number();
        row["planner_candidate"] = if complete {
            json!({"semester_id":semester,"lesson_id":value_string(&row["lesson_id"]),"course_code":row["course_code"],"course_name":row["course_name"],"credits":row["credits"],"slots":slots,"schedule_known":!slots.is_empty(),"source":"official"})
        } else {
            Value::Null
        };
        row["missing_planning_metadata"] = json!(!complete);
        row["schedule_text"] = json!(text);
        row["schedule_known"] = json!(!slots.is_empty());
        row["slots"] = json!(slots);
        row["capacity"] = item["limitCount"].clone();
        row["selected_count"] = item["selectedStdCount"].clone();
        row["eligibility_verified"] = json!(false);
        rows.push(row);
    }
    Ok(
        json!({"semester_id":semester,"offerings":rows,"page":page,"server_page_size":paging["rowsPerPage"],"total_rows":paging["totalRows"],"next_page":if page<paging["totalPages"].as_u64().unwrap() {json!(page+1)}else{Value::Null},"truncated":items.len()>limit,"source":format!("{BASE}/for-std/lesson-search"),"content_is_untrusted":true,"next_action":"use_planner_candidate_or_refine_search","note":"开课不等于可选；截断时缩小关键词，勿丢弃同页剩余候选。"}),
    )
}
fn planning_context(input: &Value) -> Result<Value> {
    let context = &input["context"];
    let lessons = input["data"]["lessons"]
        .as_array()
        .ok_or_else(|| error("课程列表结构已变化。"))?;
    let mut candidates = Vec::new();
    for item in lessons {
        let row = lesson_row(item);
        if !row["credits"].is_number() {
            return Ok(
                json!({"state":"needs_input","missing_fields":["official_course_credits"],"next_action":"verify_official_course_data","candidates":[]}),
            );
        }
        let text = item["scheduleText"]["dateTimePlacePersonText"]["textZh"]
            .as_str()
            .unwrap_or("");
        let slots = schedule_slots(text, row["campus"].as_str().unwrap_or(""));
        candidates.push(json!({"semester_id":context["semester"]["id"],"lesson_id":value_string(&row["lesson_id"]),"course_code":row["course_code"],"course_name":row["course_name"],"credits":row["credits"],"slots":slots,"schedule_known":!slots.is_empty(),"required":true,"source":"official"}));
    }
    let missing: Vec<Value> = candidates
        .iter()
        .filter(|c| c["schedule_known"] == false)
        .map(|c| c["lesson_id"].clone())
        .collect();
    Ok(
        json!({"state":"context_ready","semester":context["semester"],"candidates":candidates,"missing_schedule_ids":missing,"next_action":"combine_with_offering_candidates_then_plan","note":"已选课程默认必须保留；只在用户明确要求调整的情景方案中修改required，不执行真实退课。","content_is_untrusted":true}),
    )
}

pub fn dispatch(input: &Value) -> Result<Value> {
    match str_arg(input, "action", "")? {
        "json" => json_body(input),
        "context" => context(input),
        "schedule_slots" => Ok(json!(schedule_slots(
            str_arg(input, "text", "")?,
            str_arg(input, "campus", "")?
        ))),
        "validate_response" => {
            let doc = Html::parse_document(str_arg(input, "text", "")?);
            if doc
                .select(&sel("a[href='/ucas-sso/login'], input[type='password']"))
                .next()
                .is_some()
            {
                return Err(Error::new("authentication_required", "教务系统尚未认证。"));
            }
            Ok(json!({"valid":true}))
        }
        "check" | "home" => {
            let doc = Html::parse_document(str_arg(input, "text", "")?);
            let title = doc
                .select(&sel("title"))
                .next()
                .map(all_text)
                .unwrap_or_else(|| "教务系统".to_owned());
            if input["action"] == "check" {
                need(
                    doc.select(&sel("#e-top-menu")).next().is_some(),
                    "未取得完整的个人教务门户，请重新登录或稍后重试。",
                )?;
                return Ok(json!({"connected":true,"title":title,"read_only":true}));
            }
            let max_chars = uint(input, "max_chars", 6000, 100000)? as usize;
            if max_chars == 0 {
                return Err(Error::invalid());
            }
            let mut seen = HashSet::new();
            let mut links = Vec::new();
            let base = Url::parse(BASE).unwrap();
            for anchor in doc.select(&sel("a[href]")) {
                let label = text(anchor, " ");
                let Ok(url) = base.join(anchor.value().attr("href").unwrap()) else {
                    continue;
                };
                if label.is_empty()
                    || url.scheme() != "https"
                    || url.host_str() != Some("jw.ustc.edu.cn")
                    || !url.username().is_empty()
                    || url.password().is_some()
                    || url.query().is_some()
                    || url.path().contains("logout")
                    || url.path().starts_with("/login")
                    || url.path().starts_with("/ucas-sso")
                {
                    continue;
                }
                if seen.insert((label.clone(), url.path().to_owned())) {
                    links.push(json!({"text":label,"path":url.path()}));
                }
            }
            let visible = text(doc.root_element(), "\n");
            Ok(
                json!({"title":title,"text":visible.chars().take(max_chars).collect::<String>(),"text_truncated":visible.chars().count()>max_chars,"links":links,"source":format!("{BASE}/home"),"content_is_untrusted":true}),
            )
        }
        "modules" => {
            let data = &input["data"];
            let items = data
                .as_array()
                .ok_or_else(|| error("教务菜单结构已变化。"))?;
            let read = [
                "/",
                "/home",
                "/home/menu",
                "/for-std/course-table",
                "/for-std/course-table/get-data",
                "/for-std/grade/sheet",
                "/for-std/grade/sheet/getSemesters",
                "/for-std/grade/sheet/getGradeSheetTypes",
                "/for-std/grade/sheet/getGradeList",
            ];
            let modules:Vec<Value>=items.iter().map(|item| {let path=item["href"].as_str().filter(|p|p.is_empty() || re(r"^/[A-Za-z0-9_/-]+$").is_match(p)); json!({"id":item["id"],"parent_id":item["parentId"],"name":item["title"],"path":path,"supported_reading":path.is_some_and(|p|read.contains(&p))})}).collect();
            Ok(json!({"count":modules.len(),"modules":modules,"content_is_untrusted":true}))
        }
        "course_selection" => {
            let context = &input["context"];
            let semester = uint(input, "semester_id", 0, 10_000_000)?;
            let selected = if semester == 0 {
                context["current_semester_id"].clone()
            } else {
                json!(semester)
            };
            let found = context["semesters"]
                .as_array()
                .and_then(|a| a.iter().find(|s| s["id"] == selected))
                .ok_or_else(|| {
                    error("学期不在教务系统提供的列表中，请先调用 school_jw_list_semesters。")
                })?;
            Ok(
                json!({"semester":found,"params":{"bizTypeId":context["biz_type_id"],"semesterId":selected,"dataId":context["student_id"]}}),
            )
        }
        "course_data" => {
            need(input["data"]["lessons"].is_array(), "课程列表结构已变化。")?;
            Ok(input["data"].clone())
        }
        "courses" => {
            let word = keyword(input)?;
            let lessons = input["data"]["lessons"]
                .as_array()
                .ok_or_else(|| error("课程列表结构已变化。"))?;
            let rows: Vec<Value> = lessons
                .iter()
                .map(lesson_row)
                .filter(|r| fold(r["course_name"].as_str().unwrap_or("")).contains(&word))
                .collect();
            Ok(
                json!({"semester":input["context"]["semester"],"count":rows.len(),"courses":rows,"total_in_semester":lessons.len(),"content_is_untrusted":true}),
            )
        }
        "timetable" => timetable(input),
        "grade_selection" => {
            let semester = uint(input, "semester_id", 0, 10_000_000)?;
            let train = uint(input, "train_type_id", 1, 10_000_000)?;
            let semesters = list(input, "semesters")?;
            let types = list(input, "types")?;
            need(
                types.iter().any(|t| t["id"] == train),
                "修读类型不在当前用户可查询的成绩单类型中。",
            )?;
            let identifiers: Vec<Value> = semesters.iter().map(|s| s["id"].clone()).collect();
            if semester != 0 && !identifiers.contains(&json!(semester)) {
                if input.get("course_semesters").is_none() {
                    return Ok(json!({"needs_course_semesters":true}));
                }
                need(
                    list(input, "course_semesters")?
                        .iter()
                        .any(|s| s["id"] == semester),
                    "成绩学期不在教务系统提供的列表中。",
                )?;
            }
            let selected = if semester == 0 {
                identifiers
            } else {
                vec![json!(semester)]
            };
            let ids = selected
                .iter()
                .map(value_string)
                .collect::<Vec<_>>()
                .join(",");
            Ok(json!({"identifiers":selected,"params":{"trainTypeId":train,"semesterIds":ids}}))
        }
        "grades" => grades(input),
        "offerings_context" => offerings_context(input),
        "offerings" => offerings(input),
        "window_context" => {
            let html = str_arg(input, "text", "")?;
            let student = re(r"studentId:\s*(\d+)")
                .captures(html)
                .map(|c| c[1].to_owned());
            let biz = re(r"bizTypeId:\s*(\d+)")
                .captures(html)
                .map(|c| c[1].to_owned());
            need(
                student.is_some() && biz.is_some() && html.contains("fetchOpenTurns"),
                "选课入口结构已变化。",
            )?;
            Ok(json!({"studentId":student,"bizTypeId":biz}))
        }
        "window" => {
            let turns = input["data"]
                .as_array()
                .ok_or_else(|| error("选课批次结构已变化。"))?;
            let open: Vec<Value> = turns
                .iter()
                .filter_map(|t| {
                    t.as_object().map(|t| {
                        t.iter()
                            .filter(|(k, _)| {
                                ["id", "name", "nameZh", "allowEnter", "beginTime", "endTime"]
                                    .contains(&k.as_str())
                            })
                            .map(|(k, v)| (k.clone(), v.clone()))
                            .collect::<serde_json::Map<_, _>>()
                            .into()
                    })
                })
                .collect();
            Ok(
                json!({"state":if turns.is_empty(){"window_closed"}else{"requires_live_contract"},"open_turns":open,"can_execute":false,"next_action":if turns.is_empty(){"wait_for_open_window"}else{"inspect_current_turn_before_implementation"},"mutation_performed":false,"note":"常规选退课与个性化选课/放弃修读是不同流程；不互相替代。当前无经过验证的选退课执行合同。"}),
            )
        }
        "planning_context" => planning_context(input),
        "prepare_change" => {
            let action = str_arg(input, "change_action", "")?;
            need(
                ["select", "drop"].contains(&action),
                "action 只能 select 或 drop；放弃成绩/修读不属于本工具。",
            )?;
            let lesson = uint(input, "lesson_id", 0, 10_000_000)?;
            need(lesson > 0, "需实时结果中的学期和课堂ID。")?;
            let courses = &input["courses"];
            let window = &input["window"];
            let target = courses["courses"]
                .as_array()
                .and_then(|r| r.iter().find(|r| r["lesson_id"] == lesson))
                .cloned()
                .unwrap_or(Value::Null);
            let mut missing = Vec::new();
            if action == "drop" && target.is_null() {
                missing.push("target_not_in_selected_courses");
            }
            if action == "select" && !target.is_null() {
                missing.push("target_already_selected");
            }
            if window["state"] == "window_closed" {
                missing.push("selection_window_closed");
            }
            missing.push("live_selection_contract_not_verified");
            let credit = if action == "drop" {
                target["credits"].as_f64().map(|v| -v)
            } else {
                None
            };
            Ok(
                json!({"state":"blocked","preview":{"action":action,"semester":courses["semester"],"lesson_id":lesson,"target":target,"credit_change":credit,"consequences":if action=="drop"{vec!["退课后原名额可能无法恢复。"]}else{vec!["开课、无冲突不代表具备选课资格。"]}},"blockers":missing,"can_execute":false,"mutation_performed":false,"next_action":window["next_action"],"window":window,"workflow":["check_live_conditions","present_exact_change","execute_only_with_user_authorization_and_verified_contract","read_back_selected_courses"]}),
            )
        }
        "plan" => plan(input),
        _ => Err(Error::invalid()),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Slot {
    weekday: u64,
    start_period: u64,
    end_period: u64,
    weeks: Vec<u64>,
    #[serde(default)]
    campus: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Candidate {
    lesson_id: String,
    course_code: String,
    course_name: String,
    #[serde(default)]
    semester_id: Option<u64>,
    credits: f64,
    #[serde(default)]
    slots: Vec<Slot>,
    #[serde(default)]
    schedule_known: bool,
    #[serde(default)]
    required: bool,
    #[serde(default)]
    preference: i64,
    #[serde(default = "user_source")]
    source: String,
}
fn user_source() -> String {
    "user_supplied".to_owned()
}
fn max_credit() -> f64 {
    30.0
}
fn max_courses() -> usize {
    12
}
fn default_gap() -> u64 {
    1
}
fn default_limit() -> usize {
    3
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Constraints {
    #[serde(default)]
    min_credits: f64,
    #[serde(default = "max_credit")]
    max_credits: f64,
    #[serde(default)]
    target_credits: Option<f64>,
    #[serde(default)]
    unavailable: Vec<Slot>,
    #[serde(default)]
    prefer_free_weekdays: Vec<u64>,
    #[serde(default = "max_courses")]
    max_courses: usize,
    #[serde(default = "default_gap")]
    cross_campus_gap_periods: u64,
    #[serde(default = "default_limit")]
    limit: usize,
}
fn valid_slot(s: &Slot) -> bool {
    (1..=7).contains(&s.weekday)
        && (1..=20).contains(&s.start_period)
        && (s.start_period..=20).contains(&s.end_period)
        && !s.weeks.is_empty()
        && s.weeks.len() <= 60
        && s.weeks.iter().all(|w| (1..=60).contains(w))
        && s.campus.chars().count() <= 100
}
fn overlap(a: &Slot, b: &Slot, gap: u64) -> bool {
    let transfer = if !a.campus.is_empty() && !b.campus.is_empty() && a.campus != b.campus {
        gap
    } else {
        0
    };
    a.weekday == b.weekday
        && a.weeks.iter().any(|w| b.weeks.contains(w))
        && a.start_period <= b.end_period + transfer
        && b.start_period <= a.end_period + transfer
}
#[derive(Clone)]
struct Plan {
    picked: Vec<usize>,
    credits: f64,
    days: BTreeSet<u64>,
    preference: i64,
    distance: f64,
    occupied_preferred: usize,
}
struct Search<'a> {
    eligible: Vec<usize>,
    candidates: &'a [Candidate],
    constraints: &'a Constraints,
    conflicts: HashSet<(usize, usize)>,
    results: Vec<Plan>,
    nodes: usize,
    truncated: bool,
}
impl Search<'_> {
    fn walk(&mut self, index: usize, picked: &mut Vec<usize>, credits: f64) {
        self.nodes += 1;
        if self.nodes > 100000 {
            self.truncated = true;
            return;
        }
        if credits > self.constraints.max_credits + 1e-8
            || picked.len() > self.constraints.max_courses
        {
            return;
        }
        if index == self.eligible.len() {
            if picked.is_empty() || credits + 1e-8 < self.constraints.min_credits {
                return;
            }
            let days: BTreeSet<u64> = picked
                .iter()
                .flat_map(|i| self.candidates[*i].slots.iter().map(|s| s.weekday))
                .collect();
            let plan = Plan {
                picked: picked.clone(),
                credits,
                preference: picked.iter().map(|i| self.candidates[*i].preference).sum(),
                distance: (credits
                    - self
                        .constraints
                        .target_credits
                        .unwrap_or(self.constraints.max_credits))
                .abs(),
                occupied_preferred: days
                    .iter()
                    .filter(|d| self.constraints.prefer_free_weekdays.contains(d))
                    .count(),
                days,
            };
            self.results.push(plan);
            let candidates = self.candidates;
            self.results.sort_by(|a, b| {
                a.distance
                    .total_cmp(&b.distance)
                    .then(a.occupied_preferred.cmp(&b.occupied_preferred))
                    .then(b.preference.cmp(&a.preference))
                    .then(a.days.len().cmp(&b.days.len()))
                    .then_with(|| {
                        b.picked
                            .iter()
                            .map(|i| &candidates[*i].lesson_id)
                            .cmp(a.picked.iter().map(|i| &candidates[*i].lesson_id))
                    })
            });
            self.results.truncate(self.constraints.limit);
            return;
        }
        let current = self.eligible[index];
        if !picked
            .iter()
            .any(|p| self.conflicts.contains(&(current.min(*p), current.max(*p))))
        {
            picked.push(current);
            self.walk(
                index + 1,
                picked,
                credits + self.candidates[current].credits,
            );
            picked.pop();
        }
        if !self.candidates[current].required {
            self.walk(index + 1, picked, credits);
        }
    }
}
fn round6(value: f64) -> f64 {
    (value * 1_000_000.0).round_ties_even() / 1_000_000.0
}
fn plan(input: &Value) -> Result<Value> {
    let values = list(input, "candidates")?;
    let candidates: Vec<Candidate> =
        serde_json::from_value(input["candidates"].clone()).map_err(|_| Error::invalid())?;
    let c: Constraints =
        serde_json::from_value(input["constraints"].clone()).map_err(|_| Error::invalid())?;
    let valid_num = |v: f64| v.is_finite() && (0.0..=100.0).contains(&v);
    if !valid_num(c.min_credits)
        || !valid_num(c.max_credits)
        || c.min_credits > c.max_credits
        || c.target_credits
            .is_some_and(|v| !valid_num(v) || v < c.min_credits || v > c.max_credits)
        || c.unavailable.len() > 100
        || !c.unavailable.iter().all(valid_slot)
        || c.prefer_free_weekdays.len() > 7
        || !c.prefer_free_weekdays.iter().all(|d| (1..=7).contains(d))
        || !(1..=25).contains(&c.max_courses)
        || c.cross_campus_gap_periods > 10
        || !(1..=10).contains(&c.limit)
    {
        return Err(Error::invalid());
    }
    for v in &candidates {
        if v.lesson_id.is_empty()
            || v.lesson_id.chars().count() > 80
            || v.course_code.is_empty()
            || v.course_code.chars().count() > 80
            || v.course_name.is_empty()
            || v.course_name.chars().count() > 200
            || v.semester_id == Some(0)
            || !valid_num(v.credits)
            || v.slots.len() > 100
            || !v.slots.iter().all(valid_slot)
            || !(-100..=100).contains(&v.preference)
            || !["official", "user_supplied"].contains(&v.source.as_str())
        {
            return Err(Error::invalid());
        }
    }
    if candidates.len() > 25
        || candidates
            .iter()
            .map(|v| &v.lesson_id)
            .collect::<HashSet<_>>()
            .len()
            != candidates.len()
    {
        return Ok(
            json!({"state":"needs_input","missing_fields":["unique_candidates_max_25"],"next_action":"narrow_candidates","plans":[]}),
        );
    }
    if candidates
        .iter()
        .filter_map(|v| v.semester_id)
        .collect::<HashSet<_>>()
        .len()
        > 1
    {
        return Ok(
            json!({"state":"needs_input","missing_fields":["single_semester_candidates"],"next_action":"separate_semesters","plans":[]}),
        );
    }
    let mut blocked = Vec::new();
    let mut eligible = Vec::new();
    for (i, v) in candidates.iter().enumerate() {
        let mut reasons = Vec::new();
        if !v.schedule_known || v.slots.is_empty() {
            reasons.push("schedule_unknown");
        }
        if v.slots
            .iter()
            .any(|s| c.unavailable.iter().any(|u| overlap(s, u, 0)))
        {
            reasons.push("unavailable_time");
        }
        if reasons.is_empty() {
            eligible.push(i);
        } else {
            blocked.push(json!({"lesson_id":v.lesson_id,"reasons":reasons,"required":v.required}));
        }
    }
    if blocked.iter().any(|b| b["required"] == true) {
        return Ok(
            json!({"state":"needs_input","next_action":"resolve_required_course_constraints","plans":[],"excluded":blocked}),
        );
    }
    let mut conflicts = HashSet::new();
    let mut public_conflicts = Vec::new();
    for (p, a) in eligible.iter().enumerate() {
        for b in eligible.iter().skip(p + 1) {
            let x = &candidates[*a];
            let y = &candidates[*b];
            let reason = if x.course_code == y.course_code {
                Some("same_course")
            } else if x.slots.iter().any(|s| {
                y.slots
                    .iter()
                    .any(|t| overlap(s, t, c.cross_campus_gap_periods))
            }) {
                Some("time_or_transfer_conflict")
            } else {
                None
            };
            if let Some(reason) = reason {
                conflicts.insert((*a, *b));
                let mut ids = vec![&x.lesson_id, &y.lesson_id];
                ids.sort();
                public_conflicts.push(json!({"lesson_ids":ids,"reason":reason}));
            }
        }
    }
    let mut search = Search {
        eligible,
        candidates: &candidates,
        constraints: &c,
        conflicts,
        results: vec![],
        nodes: 0,
        truncated: false,
    };
    search.walk(0, &mut vec![], 0.0);
    let plans:Vec<Value>=search.results.iter().map(|p|{let ids:Vec<&String>=p.picked.iter().map(|i|&candidates[*i].lesson_id).collect();let courses:Vec<Value>=p.picked.iter().map(|i|values[*i].clone()).collect();let free:Vec<u64>=(1..=7).filter(|d|!p.days.contains(d)).collect();json!({"lesson_ids":ids,"courses":courses,"total_credits":round6(p.credits),"occupied_weekdays":p.days,"free_weekdays":free,"preference_total":p.preference,"target_credit_distance":round6(p.distance),"tradeoffs":["先满足学分目标，再尽量保留指定空闲日，随后按偏好分和到校天数排序。"]})}).collect();
    Ok(
        json!({"state":if !plans.is_empty(){"planned"}else if search.truncated{"search_incomplete"}else{"no_feasible_plan"},"next_action":if !plans.is_empty(){"present_candidates"}else{"relax_constraints_or_add_candidates"},"plans":plans,"excluded":blocked,"conflicts":public_conflicts,"search_truncated":search.truncated,"search_nodes":search.nodes.min(100000),"optimality_proven":!search.truncated,"enrollment_performed":false,"eligibility_verified":false,"limitations":["只依据输入周次与节次；资格、容量、培养方案和考试冲突需另核对。","空闲日表示该日全学期均无所选课程；跨校区间隔按输入节数计算，未核实交通。"]}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grade_rows_never_expose_unpublished_values_or_private_metadata() {
        let result = dispatch(&json!({"action":"grades","semesters":[{"id":1,"nameZh":"合成"}],"identifiers":[1],"data":{"overview":{"gpa":3.5,"private":"hidden"},"semesters":[{"id":1,"scores":[{"courseNameCh":"合成","canSee":false,"scoreCh":"hidden","gp":4.3,"passed":true}]}]}})).unwrap();
        assert_eq!(result["grades"][0]["score"], Value::Null);
        assert_eq!(result["grades"][0]["grade_point"], Value::Null);
        assert_eq!(result["grades"][0]["passed"], Value::Null);
        assert_eq!(result["overview"]["gpa"], 3.5);
        assert!(!result.to_string().contains("hidden"));
    }

    #[test]
    fn schedule_fails_closed_and_preserves_disjoint_periods() {
        let slots = schedule_slots("1~3周 A :2(1,3)", "east");
        assert_eq!(slots.len(), 2);
        assert_eq!(slots[0]["weekday"], 1);
        assert_eq!(slots[1]["start_period"], 3);
        for raw in [
            "待定",
            "3~1周 A :2(1)",
            "1~61周 A :2(1)",
            "1周 A :2(21)",
            "1周 A :2(1)\n未知时间",
        ] {
            assert!(schedule_slots(raw, "east").is_empty());
        }
    }

    #[test]
    fn selection_windows_cannot_enable_mutation() {
        for data in [json!([]), json!([{"id":1,"allowEnter":true}])] {
            let window = dispatch(&json!({"action":"window","data":data})).unwrap();
            assert_eq!(window["can_execute"], false);
            let result=dispatch(&json!({"action":"prepare_change","change_action":"select","lesson_id":3,"window":window,"courses":{"semester":{"id":1},"courses":[]}})).unwrap();
            assert_eq!(result["state"], "blocked");
            assert_eq!(result["can_execute"], false);
            assert_eq!(result["mutation_performed"], false);
        }
    }

    #[test]
    fn planner_rejects_unknown_fields_and_boolean_integer_values() {
        for constraints in [
            json!({"max_courses":true}),
            json!({"min_credits":10,"max_credits":2}),
            json!({"ignore_required":true}),
        ] {
            assert!(
                dispatch(&json!({"action":"plan","candidates":[],"constraints":constraints}))
                    .is_err()
            );
        }
    }
}
