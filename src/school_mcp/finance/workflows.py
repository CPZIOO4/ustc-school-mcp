"""Document-backed preparation routes; never represents live permissions."""
from copy import deepcopy
from .session import FinanceError, GUIDE

TYPES = {
    'daily': {'name': '日常报销', 'pdf_pages': [7, 15], 'entry': ['智能报销', '待报销票据或日常报销'],
              'specific_information': ['真实支出事由', '票据及拟报销金额', '验收人信息（页面要求时）'],
              'steps': ['选择票据', '基本信息', '经费项目与报销项', '按需分摊和冲借款', '结算信息', '补充说明与附件', '校验与提交']},
    'travel': {'name': '国内差旅', 'pdf_pages': [15, 24], 'entry': ['智能报销', '选择票据', '国内差旅'],
               'specific_information': ['出差事由', '出发到达城市及日期', '交通住宿票据', '补贴所依据的行程与天数'],
               'steps': ['选择票据与类别', '基本信息和经费项目', '核对行程及补贴', '报销项与分摊', '冲借款与结算', '补充材料', '校验与提交']},
    'loan': {'name': '借款业务', 'pdf_pages': [24, 31], 'entry': ['智能报销', '借款业务'],
             'specific_information': ['借款性质及明细类别', '借款人', '拟借金额与真实用途', '合同依据（涉及合同时）'],
             'steps': ['借款类别、借款人和金额', '基本信息', '经费项目', '结算', '说明与辅助证据', '校验与提交']},
    'remuneration': {'name': '薪酬发放', 'pdf_pages': [31, 38], 'entry': ['智能报销', '薪酬发放'],
                     'specific_information': ['发放类别及酬金性质', '所得期间', '人员清单、发放事由和金额'],
                     'steps': ['类别和所得期间', '录入或导入人员清单', '基本信息和经费', '报销项及冲借款', '结算与说明', '校验与提交']},
    'internal_transfer': {'name': '校内转账', 'pdf_pages': [38, 43], 'entry': ['智能报销', '校内转账'],
                          'specific_information': ['转账事由与类型', '转入项目号', '转账金额'],
                          'steps': ['转账基本信息', '摘要与联系方式', '转出经费项目', '按需分摊', '说明与附件', '校验与提交']},
}


def workflow_guide(business_type='overview'):
    if business_type != 'overview' and business_type not in TYPES:
        raise FinanceError('业务类型应为overview、daily、travel、loan、remuneration或internal_transfer。')
    result = {'source': GUIDE, 'source_title': '智能报销系统操作指南（2025年4月）',
              'source_checked_on': '2026-10-08', 'source_kind': 'official_document',
              'network_checked': False, 'live_form_verified': False, 'business_permissions_verified': False,
              'writes_supported': False, 'checklist_is_exhaustive': False}
    if business_type == 'overview':
        result['business_types'] = [{'business_type': key, 'name': value['name']} for key, value in TYPES.items()]
        result['next_action'] = 'choose_business_type_from_actual_user_matter'
    else:
        result.update(business_type=business_type, **deepcopy(TYPES[business_type]))
        result['common_information'] = ['用户确认的真实事项及联系方式', '经费项目/项目代码及使用依据',
                                        '金额依据与收付款信息', '该业务需要的原始票据、合同或辅助材料']
        result['next_action'] = 'collect_missing_information_then_verify_live_form'
    result['state_rules'] = ['指南中的保存草稿会占用相关票据，属于写操作。',
                             '已提交、审批完成和已支付是不同状态；后续可能还需打印投递。',
                             '财务字段缺失时询问用户，不用示例金额、项目号或收款人填报。']
    result['note'] = '这是文档中的填单路线与准备清单，不是当前账号业务权限或最新报销限额；实时表单和材料要求仍须核实。'
    return result
