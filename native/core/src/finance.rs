//! Offline material checks, exact integer cents, no network or school mutations.
use crate::{Error, Result};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Fact {
    field: String,
    #[serde(default)]
    value: String,
    #[serde(default)]
    confirmed: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Document {
    document_id: String,
    kind: String,
    #[serde(default)]
    available: bool,
    #[serde(default)]
    confirmed: bool,
    #[serde(default = "unknown")]
    status: String,
    #[serde(default)]
    dedup_key: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Line {
    line_id: String,
    kind: String,
    #[serde(default)]
    amount: Option<String>,
    #[serde(default)]
    document_amount: Option<String>,
    #[serde(default = "cny")]
    currency: String,
    #[serde(default)]
    document_ids: Vec<String>,
    #[serde(default)]
    confirmed: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    #[serde(default = "auto")]
    business_type: String,
    #[serde(default)]
    facts: Vec<Fact>,
    #[serde(default)]
    claim_amount: Option<String>,
    #[serde(default = "cny")]
    currency: String,
    #[serde(default)]
    lines: Vec<Line>,
    #[serde(default)]
    documents: Vec<Document>,
    #[serde(default)]
    contract_required: Option<bool>,
}
fn unknown() -> String {
    "unknown".into()
}
fn cny() -> String {
    "CNY".into()
}
fn auto() -> String {
    "auto".into()
}
const LABELS: &[(&str, &str)] = &[
    ("purpose", "真实事项"),
    ("funding_source", "经费项目及使用依据"),
    ("contact", "联系方式"),
    ("settlement", "结算方式与收付款信息"),
    ("travel_route", "出发到达城市"),
    ("travel_dates", "行程日期"),
    ("allowance_basis", "补贴依据（不申请补贴也需注明）"),
    ("loan_category", "借款性质与明细类别"),
    ("borrower", "借款人"),
    ("remuneration_category", "发放类别及酬金性质"),
    ("income_period", "所得期间"),
    ("personnel_list", "人员清单与发放事由"),
    ("transfer_target", "转入项目"),
    ("transfer_type", "转账类型"),
    ("acceptor", "验收人信息（实际表单要求时）"),
];
const CUES: &[(&str, &[&str])] = &[
    ("daily", &["日常报销", "办公用品", "打印", "复印"]),
    ("travel", &["差旅", "出差"]),
    ("loan", &["借款"]),
    ("remuneration", &["薪酬", "酬金", "劳务费"]),
    ("internal_transfer", &["校内转账"]),
];
fn required(kind: &str) -> &[&str] {
    match kind {
        "travel" => &["travel_route", "travel_dates", "allowance_basis"],
        "loan" => &["loan_category", "borrower"],
        "remuneration" => &["remuneration_category", "income_period", "personnel_list"],
        "internal_transfer" => &["transfer_target", "transfer_type"],
        _ => &[],
    }
}
fn line_kinds(kind: &str) -> &[&str] {
    match kind {
        "daily" => &["invoice", "other"],
        "travel" => &["invoice", "allowance", "other"],
        "loan" => &["loan_detail"],
        "remuneration" => &["payroll"],
        "internal_transfer" => &["transfer_detail"],
        _ => &[],
    }
}
fn identifier(value: &mut String) -> Result<()> {
    *value = value.trim().into();
    if !(1..=64).contains(&value.len())
        || !value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    {
        return Err(Error::invalid());
    }
    Ok(())
}
fn currency(value: &mut String) -> Result<()> {
    *value = value.trim().into();
    if value.len() != 3 || !value.bytes().all(|c| c.is_ascii_uppercase()) {
        return Err(Error::invalid());
    }
    Ok(())
}
fn cents(value: &str) -> Result<i64> {
    let (whole, fraction) = value.split_once('.').unwrap_or((value, ""));
    if whole.is_empty()
        || whole.len() > 12
        || (whole.len() > 1 && whole.starts_with('0'))
        || !whole.bytes().all(|c| c.is_ascii_digit())
        || fraction.len() > 2
        || (value.contains('.') && fraction.is_empty())
        || !fraction.bytes().all(|c| c.is_ascii_digit())
    {
        return Err(Error::invalid());
    }
    Ok(whole.parse::<i64>().map_err(|_| Error::invalid())? * 100
        + if fraction.is_empty() {
            0
        } else {
            fraction.parse::<i64>().map_err(|_| Error::invalid())?
                * if fraction.len() == 1 { 10 } else { 1 }
        })
}
fn money(value: &mut Option<String>) -> Result<()> {
    if let Some(v) = value {
        *v = v.trim().into();
        cents(v)?;
    }
    Ok(())
}
fn fixed(cents: i64) -> String {
    format!(
        "{}{}.{:02}",
        if cents < 0 { "-" } else { "" },
        cents.abs() / 100,
        cents.abs() % 100
    )
}
fn text(value: &mut String, max: usize) -> Result<()> {
    *value = value.trim().into();
    if value.chars().count() > max {
        return Err(Error::invalid());
    }
    Ok(())
}
impl Request {
    fn parse(input: &Value) -> Result<Self> {
        let mut r: Self = serde_json::from_value(input.clone()).map_err(|_| Error::invalid())?;
        if ![
            "auto",
            "daily",
            "travel",
            "loan",
            "remuneration",
            "internal_transfer",
        ]
        .contains(&r.business_type.as_str())
            || r.facts.len() > 30
            || r.lines.len() > 100
            || r.documents.len() > 100
        {
            return Err(Error::invalid());
        }
        currency(&mut r.currency)?;
        money(&mut r.claim_amount)?;
        for f in &mut r.facts {
            if !LABELS.iter().any(|(name, _)| *name == f.field) {
                return Err(Error::invalid());
            }
            text(&mut f.value, 1000)?;
        }
        for d in &mut r.documents {
            identifier(&mut d.document_id)?;
            text(&mut d.dedup_key, 120)?;
            if ![
                "invoice",
                "itinerary",
                "contract",
                "personnel_list",
                "payment_proof",
                "other",
            ]
            .contains(&d.kind.as_str())
                || !["normal", "void", "red", "unknown"].contains(&d.status.as_str())
            {
                return Err(Error::invalid());
            }
        }
        for l in &mut r.lines {
            identifier(&mut l.line_id)?;
            money(&mut l.amount)?;
            money(&mut l.document_amount)?;
            currency(&mut l.currency)?;
            if ![
                "invoice",
                "allowance",
                "loan_detail",
                "payroll",
                "transfer_detail",
                "other",
            ]
            .contains(&l.kind.as_str())
                || l.document_ids.len() > 10
            {
                return Err(Error::invalid());
            }
            for id in &mut l.document_ids {
                identifier(id)?;
            }
        }
        Ok(r)
    }
}
fn issue(issues: &mut Vec<Value>, code: &str, field: &str, message: &str) {
    issues.push(json!({"code":code,"field":field,"message":message}));
}
fn usable(d: &Document) -> bool {
    d.available && d.confirmed && (d.kind != "invoice" || d.status == "normal")
}
pub fn guide(kind: &str) -> Result<Value> {
    let guides: Value =
        serde_json::from_str(include_str!("finance_guides.json")).map_err(|_| Error::invalid())?;
    guides.get(kind).cloned().ok_or_else(Error::invalid)
}
pub fn preflight(input: &Value) -> Result<Value> {
    let r = Request::parse(input)?;
    let mut issues = Vec::new();
    let mut facts = HashMap::new();
    for f in &r.facts {
        if facts.contains_key(&f.field) {
            issue(
                &mut issues,
                "duplicate_fact",
                &f.field,
                "同一信息出现多次，请合并并确认唯一值。",
            );
        } else {
            facts.insert(&f.field, f);
        }
    }
    let purpose = facts
        .get(&"purpose".to_string())
        .map(|f| f.value.as_str())
        .unwrap_or("");
    let selected = (r.business_type != "auto").then_some(r.business_type.as_str());
    if selected.is_none() {
        issue(
            &mut issues,
            "choose_business_type",
            "business_type",
            "按实际事项选择业务类型；关键词候选不是校方业务认定。",
        );
    }
    for name in ["purpose", "funding_source", "contact", "settlement"]
        .iter()
        .chain(required(selected.unwrap_or("")))
    {
        let label = LABELS
            .iter()
            .find(|(key, _)| key == name)
            .map(|(_, v)| *v)
            .ok_or_else(Error::invalid)?;
        match facts.get(&name.to_string()) {
            None => issue(
                &mut issues,
                "missing_information",
                name,
                &format!("请补充{label}。"),
            ),
            Some(f) if f.value.is_empty() => issue(
                &mut issues,
                "missing_information",
                name,
                &format!("请补充{label}。"),
            ),
            Some(f) if !f.confirmed => issue(
                &mut issues,
                "unconfirmed_information",
                name,
                &format!("请核对{label}；不能将猜测或未核对提取结果当作事实。"),
            ),
            _ => (),
        }
    }
    let mut documents = HashMap::new();
    let mut invoice_keys = HashSet::new();
    for d in &r.documents {
        if documents.contains_key(&d.document_id) {
            issue(
                &mut issues,
                "duplicate_document_id",
                &d.document_id,
                "材料标识重复，请使用唯一标识。",
            );
        } else {
            documents.insert(&d.document_id, d);
        }
        if d.kind == "invoice" {
            if d.dedup_key.is_empty() {
                issue(
                    &mut issues,
                    "missing_invoice_identity",
                    &d.document_id,
                    "缺少已核对的完整票据查重标识；不能确认本次材料是否重复。",
                );
            } else {
                let key: String = d
                    .dedup_key
                    .nfkc()
                    .filter(|c| !c.is_whitespace())
                    .case_fold()
                    .collect();
                if !invoice_keys.insert(key) {
                    issue(
                        &mut issues,
                        "possible_duplicate_invoice",
                        &d.document_id,
                        "该票据与另一材料的查重标识相同；核对后保留正确材料，不自动扣减金额。",
                    );
                }
            }
        }
    }
    let mut require = |kind: &str, label: &str| {
        if !r.documents.iter().any(|d| d.kind == kind && usable(d)) {
            issue(
                &mut issues,
                "missing_material",
                kind,
                &format!("请准备并核对{label}；这只是准备清单，不代表完整校方要求。"),
            );
        }
    };
    if selected == Some("travel") {
        require("itinerary", "行程依据");
    }
    if selected == Some("remuneration") {
        require("personnel_list", "人员与金额清单");
    }
    if r.contract_required == Some(true) {
        require("contract", "涉及的合同依据");
    } else if r.contract_required.is_none() && matches!(selected, Some("daily" | "loan")) {
        issue(
            &mut issues,
            "confirm_contract_requirement",
            "contract_required",
            "请确认本事项是否涉及合同。",
        );
    }
    let mut totals: serde_json::Map<String, Value> = serde_json::Map::new();
    let mut numeric_totals: HashMap<&str, i64> = HashMap::new();
    let mut lines_seen = HashSet::new();
    let mut invoice_uses: Vec<(&str, usize)> = Vec::new();
    let mut complete = !r.lines.is_empty();
    if !complete {
        issue(
            &mut issues,
            "missing_amount_lines",
            "lines",
            "请提供拟报销、借款、发放或转账的金额明细。",
        );
    }
    for l in &r.lines {
        let id = &l.line_id;
        if !lines_seen.insert(id) {
            issue(
                &mut issues,
                "duplicate_line_id",
                id,
                "金额明细标识重复；总额尚不能作为可用金额。",
            );
        }
        if selected.is_some_and(|s| !line_kinds(s).contains(&l.kind.as_str())) {
            issue(
                &mut issues,
                "line_kind_mismatch",
                id,
                "金额明细类型与所选业务不匹配。",
            );
        }
        if let Some(amount) = &l.amount {
            let amount = cents(amount)?;
            *numeric_totals.entry(&l.currency).or_default() += amount;
            totals.insert(l.currency.clone(), Value::Null);
            if amount <= 0 {
                issue(
                    &mut issues,
                    "nonpositive_amount",
                    id,
                    "本预检仅支持正数支出；零值、退票或冲销需单独核实。",
                );
            }
            if let Some(document) = &l.document_amount {
                if amount > cents(document)? {
                    issue(
                        &mut issues,
                        "exceeds_document_amount",
                        id,
                        "拟报金额超过所填凭据金额，请核对。",
                    );
                }
            }
        } else {
            complete = false;
            issue(&mut issues, "missing_amount", id, "请补充此项金额。");
        }
        if !l.confirmed {
            issue(
                &mut issues,
                "unconfirmed_amount",
                id,
                "请核对金额提取结果和拟申请金额。",
            );
        }
        if l.currency != r.currency || l.currency != "CNY" {
            issue(
                &mut issues,
                "unsupported_currency",
                id,
                "本版只核对人民币金额，不自行换算或合并外币。",
            );
            if let Some(last) = issues.last_mut() {
                last["scope"] = json!("line");
            }
        }
        if l.kind == "invoice" && l.document_amount.is_none() {
            issue(
                &mut issues,
                "missing_document_amount",
                id,
                "请补充对应发票的票面金额。",
            );
        }
        if l.document_ids.is_empty() {
            issue(
                &mut issues,
                "missing_amount_evidence",
                id,
                "请关联该金额的票据、明细表或其他依据。",
            );
        }
        let mut linked = Vec::new();
        for doc_id in &l.document_ids {
            let Some(d) = documents.get(doc_id) else {
                issue(
                    &mut issues,
                    "unknown_document",
                    id,
                    "关联材料不存在于本次材料列表。",
                );
                continue;
            };
            linked.push(*d);
            if !usable(d) {
                issue(
                    &mut issues,
                    "unusable_document",
                    doc_id,
                    "材料缺失、未核对，或发票状态未知/红字/作废，需先确认。",
                );
            }
            if d.kind == "invoice" {
                if let Some((_, count)) = invoice_uses.iter_mut().find(|(key, _)| *key == doc_id) {
                    *count += 1;
                } else {
                    invoice_uses.push((doc_id, 1));
                }
            }
        }
        if l.kind == "invoice" && (linked.len() != 1 || linked[0].kind != "invoice") {
            issue(
                &mut issues,
                "invoice_reference_mismatch",
                id,
                "一项发票金额必须关联且仅关联一份发票材料。",
            );
        }
        if l.kind == "other" {
            issue(
                &mut issues,
                "manual_amount_basis_review",
                id,
                "非标准金额依据需人工核对，不能自动认定可报销。",
            );
        }
    }
    for (doc, count) in invoice_uses {
        if count > 1 {
            issue(
                &mut issues,
                "invoice_reused",
                doc,
                "同一发票被多项金额引用；核对是否重复或分摊，程序不自动合并。",
            );
        }
    }
    if r.claim_amount.is_none() {
        issue(
            &mut issues,
            "missing_claim_amount",
            "claim_amount",
            "请确认本次拟申请总额。",
        );
    } else if cents(r.claim_amount.as_deref().unwrap_or(""))? <= 0 {
        issue(
            &mut issues,
            "nonpositive_claim_amount",
            "claim_amount",
            "拟申请总额需为正数。",
        );
    }
    if r.currency != "CNY" {
        issue(
            &mut issues,
            "unsupported_currency",
            "currency",
            "本版不核定外币折算金额。",
        );
    }
    let mut difference = None;
    if complete && numeric_totals.len() == 1 {
        if let (Some(total), Some(claim)) =
            (numeric_totals.get(r.currency.as_str()), &r.claim_amount)
        {
            let value = cents(claim)? - total;
            difference = Some(fixed(value));
            if value != 0 {
                issue(
                    &mut issues,
                    "amount_mismatch",
                    "claim_amount",
                    "拟申请总额与已列金额明细之和不一致。",
                );
            }
        }
    }
    for (key, value) in &mut totals {
        *value = json!(fixed(numeric_totals[key.as_str()]));
    }
    let guide = guide(selected.unwrap_or("overview"))?;
    let guides: Value =
        serde_json::from_str(include_str!("finance_guides.json")).map_err(|_| Error::invalid())?;
    let candidates: Vec<_> = CUES
        .iter()
        .filter(|(_, terms)| terms.iter().any(|t| purpose.contains(t)))
        .map(|(key, _)| json!({"business_type":key,"name":guides[key]["name"]}))
        .collect();
    Ok(
        json!({"state":if issues.is_empty(){"ready_for_manual_review"}else{"needs_input"}, "business_type":selected,
        "business_candidates":candidates,"duplicate_check":{"scope":"provided_documents_only",
            "identities_complete":r.documents.iter().filter(|d|d.kind=="invoice").all(|d|!d.dedup_key.is_empty()),"history_checked":false},
        "input_guidance":if issues.is_empty(){Value::Null}else{input_guide(&issues)},"issues":issues,
        "next_action":if issues.is_empty(){"verify_current_form_and_requirements"}else{"resolve_issues_and_rerun"},
        "amounts":{"currency":r.currency,"claim_amount":r.claim_amount,"observed_line_totals":totals,
            "amounts_complete":complete,"claim_minus_lines":difference,"totals_are_approved":false},
        "materials":r.documents.iter().map(|d|json!({"document_id":d.document_id,"kind":d.kind,"available":d.available,"confirmed":d.confirmed})).collect::<Vec<_>>(),
        "guide":{"source":guide["source"],"source_title":guide["source_title"],"pdf_pages":guide.get("pdf_pages").cloned().unwrap_or(json!([]))},
        "network_checked":false,"files_read":false,"writes_supported":false,"safe_to_submit":false,
        "checklist_is_exhaustive":false,"invoice_authenticity_verified":false,
        "note":"仅按输入的已核对摘要做材料准备和算术检查，不读取原文件、不鉴别发票真伪、不核定报销资格、标准或额度。金额总和可能包含待查重项目；通过后仍须核对实时表单与校方要求。"}),
    )
}

fn input_guide(issues: &[Value]) -> Value {
    let fields: Vec<_> = issues.iter().map(|i| {
        let name = i["field"].as_str().unwrap_or("");
        let code = i["code"].as_str().unwrap_or("");
        let mut selector = None;
        let mut source = "user";
        let (path,expected) = match code {
            "missing_information"|"unconfirmed_information"|"duplicate_fact" => {
                selector = Some(json!({"field":name}));
                ("/request/facts","Fact对象：field为此字段名，value为已核对说明，confirmed仅在实际核对后为true")
            },
            "choose_business_type" => ("/request/business_type","daily | travel | loan | remuneration | internal_transfer"),
            "confirm_contract_requirement" => ("/request/contract_required","boolean；确认是否涉及合同，未知保留null"),
            "missing_claim_amount"|"nonpositive_claim_amount"|"amount_mismatch" => ("/request/claim_amount","人民币正数十进制字符串，最多两位小数"),
            "unsupported_currency" if name == "currency" && i["scope"] != "line" => {
                source = "manual_review"; ("/request/currency","CNY；非人民币事项交人工核实，不自行换算")
            },
            "missing_amount_lines"|"duplicate_line_id"|"missing_amount"|"nonpositive_amount"|"exceeds_document_amount"|
            "unconfirmed_amount"|"unsupported_currency"|"missing_document_amount"|"missing_amount_evidence"|
            "unknown_document"|"invoice_reference_mismatch"|"line_kind_mismatch"|"manual_amount_basis_review" => {
                source = "verified_document";
                if name != "lines" { selector = Some(json!({"line_id":name})); }
                ("/request/lines","AmountLine对象：金额为十进制字符串；document_amount为票面金额，document_ids引用本次材料ID")
            },
            _ => {
                source = "verified_document";
                selector = Some(if code == "missing_material" {json!({"kind":name})} else {json!({"document_id":name})});
                ("/request/documents","Document对象：实际材料的类型、可用性、核对状态；发票含完整查重标识及真实状态")
            },
        };
        if matches!(code,"manual_amount_basis_review"|"possible_duplicate_invoice"|"invoice_reused"|"duplicate_line_id"|"duplicate_document_id") { source="manual_review"; }
        let mut value = json!({"path":path,"question":i["message"],"expected_format":expected,"source":source,"code":code});
        if let Some(s) = selector { value["selector"] = s; } value
    }).collect();
    json!({"version":1,"fields":fields,"ask_together":true,"mutation_authorized":false,
        "resume":{"tool":"school_finance_preflight","arguments":{},"arguments_complete":false,
            "preserve_arguments":["request"],"requires_complete_arguments":true,"ready_to_call":false},
        "note":"按字段来源补齐或核实，保留原参数后重新调用完整请求；本提示不保存输入、不自动填写或执行。"})
}
