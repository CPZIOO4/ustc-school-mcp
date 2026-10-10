"""Offline preparation checks over supplied facts; no invoice authentication or school writes."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field
from .workflows import TYPES, workflow_guide

Business = Literal['auto', 'daily', 'travel', 'loan', 'remuneration', 'internal_transfer']
Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')]
Money = Annotated[str, Field(pattern=r'^(0|[1-9][0-9]{0,11})(\.[0-9]{1,2})?$')]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$')]
FactName = Literal['purpose', 'funding_source', 'contact', 'settlement', 'travel_route', 'travel_dates',
                   'allowance_basis', 'loan_category', 'borrower', 'remuneration_category', 'income_period',
                   'personnel_list', 'transfer_target', 'transfer_type', 'acceptor']
DocumentKind = Literal['invoice', 'itinerary', 'contract', 'personnel_list', 'payment_proof', 'other']


class InputModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class Fact(InputModel):
    field: FactName
    value: str = Field(default='', max_length=1000)
    confirmed: bool = False


class Document(InputModel):
    document_id: Identifier
    kind: DocumentKind
    available: bool = False
    confirmed: bool = False
    status: Literal['normal', 'void', 'red', 'unknown'] = 'unknown'
    dedup_key: str = Field(default='', max_length=120, description='核对后的完整发票代码与号码组合（无代码时用完整号码）；未知留空并报告查重缺项。只用于本次材料查重，不回显。')


class AmountLine(InputModel):
    line_id: Identifier
    kind: Literal['invoice', 'allowance', 'loan_detail', 'payroll', 'transfer_detail', 'other']
    amount: Money | None = None
    document_amount: Money | None = None
    currency: Currency = 'CNY'
    document_ids: list[Identifier] = Field(default_factory=list, max_length=10)
    confirmed: bool = False


class PreflightRequest(InputModel):
    business_type: Business = 'auto'
    facts: list[Fact] = Field(default_factory=list, max_length=30)
    claim_amount: Money | None = None
    currency: Currency = 'CNY'
    lines: list[AmountLine] = Field(default_factory=list, max_length=100)
    documents: list[Document] = Field(default_factory=list, max_length=100)
    contract_required: bool | None = None


LABELS = {'purpose':'真实事项', 'funding_source':'经费项目及使用依据', 'contact':'联系方式',
          'settlement':'结算方式与收付款信息', 'travel_route':'出发到达城市', 'travel_dates':'行程日期',
          'allowance_basis':'补贴依据（不申请补贴也需注明）', 'loan_category':'借款性质与明细类别',
          'borrower':'借款人', 'remuneration_category':'发放类别及酬金性质', 'income_period':'所得期间',
          'personnel_list':'人员清单与发放事由', 'transfer_target':'转入项目', 'transfer_type':'转账类型',
          'acceptor':'验收人信息（实际表单要求时）'}
REQUIRED = {'daily': [], 'travel':['travel_route','travel_dates','allowance_basis'],
            'loan':['loan_category','borrower'], 'remuneration':['remuneration_category','income_period','personnel_list'],
            'internal_transfer':['transfer_target','transfer_type']}
CUES = {'daily': ('日常报销','办公用品','打印','复印'), 'travel': ('差旅','出差'),
        'loan': ('借款',), 'remuneration': ('薪酬','酬金','劳务费'), 'internal_transfer': ('校内转账',)}
LINE_KINDS = {'daily': {'invoice','other'}, 'travel': {'invoice','allowance','other'},
              'loan': {'loan_detail'}, 'remuneration': {'payroll'}, 'internal_transfer': {'transfer_detail'}}


def preflight(request: PreflightRequest) -> dict:
    issues = []
    def issue(code, field, message):
        issues.append({'code':code, 'field':field, 'message':message})

    facts = {}
    for fact in request.facts:
        if fact.field in facts:
            issue('duplicate_fact', fact.field, '同一信息出现多次，请合并并确认唯一值。')
        else:
            facts[fact.field] = fact
    purpose = facts.get('purpose')
    text = purpose.value if purpose else ''
    candidates = [key for key, terms in CUES.items() if any(term in text for term in terms)]
    # Keywords propose candidates only: even a single candidate needs explicit selection.
    selected = None if request.business_type == 'auto' else request.business_type
    if selected is None:
        issue('choose_business_type', 'business_type', '按实际事项选择业务类型；关键词候选不是校方业务认定。')

    for name in ['purpose', 'funding_source', 'contact', 'settlement'] + REQUIRED.get(selected, []):
        fact = facts.get(name)
        if fact is None or not fact.value:
            issue('missing_information', name, '请补充' + LABELS[name] + '。')
        elif not fact.confirmed:
            issue('unconfirmed_information', name, '请核对' + LABELS[name] + '；不能将猜测或未核对提取结果当作事实。')

    documents = {}
    seen_invoice_keys = {}
    for doc in request.documents:
        if doc.document_id in documents:
            issue('duplicate_document_id', doc.document_id, '材料标识重复，请使用唯一标识。')
        else:
            documents[doc.document_id] = doc
        if doc.kind == 'invoice' and not doc.dedup_key:
            issue('missing_invoice_identity', doc.document_id, '缺少已核对的完整票据查重标识；不能确认本次材料是否重复。')
        if doc.kind == 'invoice' and doc.dedup_key:
            key = ''.join(unicodedata.normalize('NFKC', doc.dedup_key).split()).casefold()
            if key in seen_invoice_keys:
                issue('possible_duplicate_invoice', doc.document_id, '该票据与另一材料的查重标识相同；核对后保留正确材料，不自动扣减金额。')
            seen_invoice_keys[key] = doc.document_id

    def usable(doc):
        return doc.available and doc.confirmed and (doc.kind != 'invoice' or doc.status == 'normal')

    def require_material(kind, label):
        if not any(doc.kind == kind and usable(doc) for doc in request.documents):
            issue('missing_material', kind, '请准备并核对' + label + '；这只是准备清单，不代表完整校方要求。')

    if selected == 'travel':
        require_material('itinerary', '行程依据')
    if selected == 'remuneration':
        require_material('personnel_list', '人员与金额清单')
    if request.contract_required is True:
        require_material('contract', '涉及的合同依据')
    elif request.contract_required is None and selected in {'daily', 'loan'}:
        issue('confirm_contract_requirement', 'contract_required', '请确认本事项是否涉及合同。')

    totals = {}
    seen_lines, invoice_uses = set(), {}
    amounts_complete = bool(request.lines)
    if not request.lines:
        issue('missing_amount_lines', 'lines', '请提供拟报销、借款、发放或转账的金额明细。')
    for line in request.lines:
        if line.line_id in seen_lines:
            issue('duplicate_line_id', line.line_id, '金额明细标识重复；总额尚不能作为可用金额。')
        seen_lines.add(line.line_id)
        if selected and line.kind not in LINE_KINDS[selected]:
            issue('line_kind_mismatch', line.line_id, '金额明细类型与所选业务不匹配。')
        if line.amount is None:
            amounts_complete = False
            issue('missing_amount', line.line_id, '请补充此项金额。')
        else:
            amount = Decimal(line.amount)
            totals[line.currency] = totals.get(line.currency, Decimal('0')) + amount
            if amount <= 0:
                issue('nonpositive_amount', line.line_id, '本预检仅支持正数支出；零值、退票或冲销需单独核实。')
            if line.document_amount is not None and amount > Decimal(line.document_amount):
                issue('exceeds_document_amount', line.line_id, '拟报金额超过所填凭据金额，请核对。')
        if not line.confirmed:
            issue('unconfirmed_amount', line.line_id, '请核对金额提取结果和拟申请金额。')
        if line.currency != request.currency or line.currency != 'CNY':
            issue('unsupported_currency', line.line_id, '本版只核对人民币金额，不自行换算或合并外币。')
        if line.kind == 'invoice' and line.document_amount is None:
            issue('missing_document_amount', line.line_id, '请补充对应发票的票面金额。')
        if not line.document_ids:
            issue('missing_amount_evidence', line.line_id, '请关联该金额的票据、明细表或其他依据。')
        linked = []
        for doc_id in line.document_ids:
            doc = documents.get(doc_id)
            if doc is None:
                issue('unknown_document', line.line_id, '关联材料不存在于本次材料列表。')
                continue
            linked.append(doc)
            if not usable(doc):
                issue('unusable_document', doc_id, '材料缺失、未核对，或发票状态未知/红字/作废，需先确认。')
            if doc.kind == 'invoice':
                invoice_uses.setdefault(doc_id, []).append(line.line_id)
        if line.kind == 'invoice' and (len(linked) != 1 or linked[0].kind != 'invoice'):
            issue('invoice_reference_mismatch', line.line_id, '一项发票金额必须关联且仅关联一份发票材料。')
        if line.kind == 'other':
            issue('manual_amount_basis_review', line.line_id, '非标准金额依据需人工核对，不能自动认定可报销。')
    for doc_id, uses in invoice_uses.items():
        if len(uses) > 1:
            issue('invoice_reused', doc_id, '同一发票被多项金额引用；核对是否重复或分摊，程序不自动合并。')
    if request.claim_amount is None:
        issue('missing_claim_amount', 'claim_amount', '请确认本次拟申请总额。')
    elif Decimal(request.claim_amount) <= 0:
        issue('nonpositive_claim_amount', 'claim_amount', '拟申请总额需为正数。')
    if request.currency != 'CNY':
        issue('unsupported_currency', 'currency', '本版不核定外币折算金额。')
    difference = None
    if amounts_complete and set(totals) == {request.currency} and request.claim_amount is not None:
        difference = Decimal(request.claim_amount) - totals[request.currency]
        if difference:
            issue('amount_mismatch', 'claim_amount', '拟申请总额与已列金额明细之和不一致。')
    guide = workflow_guide(selected or 'overview')
    return {'state':'needs_input' if issues else 'ready_for_manual_review', 'business_type':selected,
            'business_candidates':[{'business_type':key,'name':TYPES[key]['name']} for key in candidates],
            'duplicate_check':{'scope':'provided_documents_only',
                               'identities_complete':all(doc.dedup_key for doc in request.documents if doc.kind == 'invoice'),
                               'history_checked':False},
            'issues':issues, 'next_action':'resolve_issues_and_rerun' if issues else 'verify_current_form_and_requirements',
            'amounts':{'currency':request.currency, 'claim_amount':request.claim_amount,
                       'observed_line_totals':{key:format(value,'.2f') for key,value in totals.items()},
                       'amounts_complete':amounts_complete, 'claim_minus_lines':format(difference,'.2f') if difference is not None else None,
                       'totals_are_approved':False},
            'materials':[{'document_id':doc.document_id,'kind':doc.kind,'available':doc.available,
                          'confirmed':doc.confirmed} for doc in request.documents],
            'guide':{'source':guide['source'],'source_title':guide['source_title'], 'pdf_pages':guide.get('pdf_pages',[])},
            'network_checked':False, 'files_read':False, 'writes_supported':False, 'safe_to_submit':False,
            'checklist_is_exhaustive':False, 'invoice_authenticity_verified':False,
            'note':'仅按输入的已核对摘要做材料准备和算术检查，不读取原文件、不鉴别发票真伪、不核定报销资格、标准或额度。金额总和可能包含待查重项目；通过后仍须核对实时表单与校方要求。'}
