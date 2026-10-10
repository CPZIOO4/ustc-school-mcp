"""Bounded homepage metadata reads; no generic SQL, procedure or form API."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit
from bs4 import BeautifulSoup

from .session import HOST


class CatalogBootstrap:
    """One observed home navigation, one session-context sync, one catalog query.

    The site's page.openExternal synchronizes navigation context before loading
    the homepage definition. Opaque beforeLoadProcs and button-event procedures
    remain blocked. Only the homepage searchBar's read query is executed.
    """
    def __init__(self):
        self.default_window = None
        self.context_sent = False
        self.context_ready = False
        self.definition_sent = False
        self.definition_ready = False
        self.query_armed = False
        self.query_sent = False
        self.policy_error = None

    def observe(self, path, status, body):
        if status != 200:
            return
        if path == '/YBX/getDefaultWinno.action' and len(body) < 160:
            match = re.fullmatch(r'WF_YBX:([0-9]{1,6}):SY-个人首页', body.strip())
            if match:
                self.default_window = int(match[1])
        elif path == '/YBX/common_updateUserContext.action' and self.context_sent:
            self.context_ready = body.strip() == 'ok'
        elif path == '/YBX/loadDefinition.action' and self.definition_sent:
            self.definition_ready = True

    def allow(self, url, method, body):
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.hostname != HOST or parsed.port not in (None, 443)
                    or parsed.username or parsed.password or method != 'POST'
                    or not body or len(body) > 1024 * 1024):
                return False
        except ValueError:
            return False
        # The framework encrypts its transport fields. No body/SQL/window
        # parameters are accepted from the MCP caller; the browser builds them.
        if parsed.path == '/YBX/common_updateUserContext.action' and not parsed.query:
            if self.default_window is not None and not self.context_sent:
                self.context_sent = True
                return True
        elif parsed.path == '/YBX/loadDefinition.action' and parse_qs(parsed.query, keep_blank_values=True) in ({}, {'type': ['W']}):
            if self.context_ready and not self.definition_sent:
                self.definition_sent = True
                return True
        elif parsed.path == '/YBX/common_bindSQLDataBackend.action' and not parsed.query:
            if self.definition_ready and self.query_armed and not self.query_sent:
                self.query_sent = True
                return True
        return False


HOME_READY = """() => window.jQuery && $.page && Object.values($.page.list || {}).some(
 d => (d.wins || []).some(w => w.init_title === '报销大类' && w.func_type === 'S'))"""

CATALOG_QUERY = r"""expectedWindow => new Promise(resolve => {
 const defs = Object.values($.page.list || {}).filter(d => d.projID === 'WF_YBX'
   && Number(d.mainWinno) === expectedWindow
   && (d.wins || []).some(w => w.init_title === '报销大类' && w.func_type === 'S' && w.wf_enabled === false));
 const funcs = defs.flatMap(d => d.funcs || []).filter(f => f.caption === '业务大类');
 if (defs.length !== 1 || funcs.length !== 1) { resolve({error:'homepage_changed'}); return; }
 const f = funcs[0];
 // Accept only the site's account/role-filtered homepage search expression.
 if (typeof f.searchSql !== 'string' || !/^\$[a-f0-9]{32}\|#0-userinfo\.userid#,#0-SEARCHVAL#,#0-USERINFO\.ALL_USERROLE#$/.test(f.searchSql)) {
   resolve({error:'catalog_query_changed'}); return;
 }
 $.UC.setData('0-SEARCHVAL', '');
 $.UC.setData('0-SEARCHALL_FLAG', false);
 const timer = setTimeout(() => resolve({error:'catalog_timeout'}), 12000);
 $.customWin.getDataFromDBBackend(f.searchSql, rows => {
   clearTimeout(timer);
   if (!Array.isArray(rows)) { resolve({error:'catalog_shape_changed'}); return; }
   const entries = rows.slice(0,200).map(row => {
     const r = Object.fromEntries(Object.entries(row).map(([k,v]) => [k.toLowerCase(),v]));
     return {code:r.code, title:r.title, group:r.groupname};
   });
   resolve({entries,total:rows.length,truncated:rows.length>200});
 }, () => { clearTimeout(timer); resolve({error:'catalog_query_failed'}); }, {needUp:false});
})"""


def catalog_result(data):
    """Minimize the response: never return procedures, URLs, user fields or amounts."""
    if not isinstance(data, dict) or data.get('error') or not isinstance(data.get('entries'), list):
        known = {'homepage_changed', 'catalog_query_changed', 'catalog_timeout',
                 'catalog_shape_changed', 'catalog_query_failed'}
        reason = data.get('error') if isinstance(data, dict) else None
        return {'catalog_complete': False, 'business_entries': [],
                'catalog_state': reason if reason in known else 'catalog_shape_changed'}
    entries, omitted = [], 0
    guide_types = {'日常报销': 'daily', '国内差旅': 'travel', '公务借款': 'loan',
                   '薪酬发放': 'remuneration', '校内转账': 'internal_transfer'}
    seen = set()
    for row in data['entries'][:200]:
        if not isinstance(row, dict):
            omitted += 1
            continue
        code, title, group = (row.get(k) for k in ('code', 'title', 'group'))
        if (not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', code)
                or not isinstance(title, str) or not 0 < len(title) <= 160
                or not isinstance(group, str) or not 0 < len(group) <= 80
                or '#' in title + group):
            omitted += 1
            continue
        title = BeautifulSoup(title, 'html.parser').get_text(' ', strip=True)
        group = BeautifulSoup(group, 'html.parser').get_text(' ', strip=True)
        if not title or not group or code in seen:
            omitted += 1
            continue
        seen.add(code)
        entry = {'business_id': code, 'name': title, 'category': group,
                 'business_operations_verified': False}
        if title in guide_types:
            entry['guide_business_type'] = guide_types[title]
        entries.append(entry)
    complete = (not data.get('truncated') and not omitted and len(data['entries']) <= 200
                and data.get('total') == len(entries))
    return {'catalog_complete': complete, 'catalog_state': 'completed' if complete else 'partial',
            'catalog_source': 'authenticated_home_business_query', 'business_entries': entries,
            'business_entry_count': len(entries), 'omitted_entry_count': omitted,
            'categories': list(dict.fromkeys(e['category'] for e in entries)),
            'truncated': bool(data.get('truncated')) or len(data['entries']) > 200}
