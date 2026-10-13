"""Observed public sources only; no credentials, arbitrary URLs or guessed APIs."""
from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import urljoin, urlsplit, parse_qs
from bs4 import BeautifulSoup
from ..network import http_client, bounded_request
from ..service_errors import ServiceError

JOB = 'https://www.job.ustc.edu.cn'
SOURCES = {
    'jobs': ('就业岗位', JOB + '/Jobinformation/index.aspx', 'job'),
    'internships': ('实习机会', JOB + '/Internship/index.aspx', 'job'),
    'research_jobs': ('科研助理', JOB + '/research/list.aspx', 'research'),
    'job_events': ('就业宣讲', JOB + '/Specialrecruitment/list.aspx', 'event'),
    'math_notices': ('数学学院通知', 'https://math.ustc.edu.cn/tzgg/list.htm', 'college'),
    'math_lectures': ('数学学院学术报告', 'https://math.ustc.edu.cn/xsbg_18822/list.htm', 'college'),
    'cs_notices': ('计算机学院通知', 'https://cs.ustc.edu.cn/3054/list.htm', 'college'),
    'cs_lectures': ('计算机学院学术活动', 'https://cs.ustc.edu.cn/xsbg/list.htm', 'college'),
    'physics_notices': ('物理学院通知', 'https://physics.ustc.edu.cn/3584/list.htm', 'college'),
    'physics_lectures': ('物理学院学术报告', 'https://physics.ustc.edu.cn/xsbg/list.htm', 'college'),
}

def stamp():
    return datetime.now(timezone.utc).isoformat()

def text(value, limit=16000):
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)[:limit]

def item(source, key, title, url, **fields):
    return dict(id=hashlib.sha256((source + ':' + str(key)).encode()).hexdigest()[:32], source=source,
        title=text(title, 300), source_url=url, published_at=None, deadline=None, start=None, end=None,
        location='', summary='', time_evidence={}, collected_at=stamp(), content_is_untrusted=True,
        ) | fields

def fetch(url, *, data=None, transport=None, allow_redirect=False):
    # Callers construct URLs from the closed registry and validated identifiers.
    with http_client('web', ServiceError, timeout=25, trust_env=False, follow_redirects=False,
                     transport=transport, headers={'User-Agent':'SchoolMCP/0.1'}) as c:
        r = bounded_request(c, 'POST' if data is not None else 'GET', url,
            maximum_bytes=3*1024*1024, error_type=ServiceError, **({'data':data} if data is not None else {}))
    if r.status_code != 200 and not (allow_redirect and r.status_code in {301,302,303,307,308}):
        raise ServiceError('公开信息源未成功返回，请稍后重试。', code='unavailable')
    r.encoding = 'utf-8'
    return r

def _dated(value):
    m = re.search(r'(?<!\d)(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?', value or '')
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).date().isoformat()
    except ValueError:
        return None

def _fields(body):
    """Extract only explicitly labelled complete dates. Ambiguities stay unknown."""
    result = {'time_evidence': {}};warnings=[]
    for key, labels in [('deadline', r'截止时间|截止日期|报名截止'), ('start', r'举办时间|报告时间|讲座时间|活动时间|(?:^|\n)时间')]:
        candidates = re.findall(r'(?:' + labels + r')\s*[：:]\s*([^\n]{1,100})', body)
        if any(re.search(r'\d{1,2}月\d{1,2}',s) and not _dated(s) for s in candidates):
            warnings.append('时间缺少年份或日期无效，未根据发布日期推算。')
        candidates = [s.strip() for s in candidates if _dated(s)]
        if len(set(candidates)) != 1:
            if len(set(candidates))>1:warnings.append('同类时间有多个不同值，请查看原文。')
            continue
        raw = candidates[0]
        if len(re.findall(r'20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2}',raw))!=1:
            warnings.append('时间行包含多个日期，未自动选择。')
            continue
        day = _dated(raw)
        weekday=re.search(r'(?:星期|周)([一二三四五六日天])',raw)
        if weekday and datetime.fromisoformat(day).weekday()!='一二三四五六日'.index(weekday[1].replace('天','日')):
            warnings.append('原文日期与星期不一致，请核实后加入日历。');continue
        if re.search(r'(?<!\d)(?:2[4-9]|[3-9]\d)[:：]\d{2}|\d{1,2}[:：][6-9]\d',raw):
            warnings.append('原文时刻格式无效，请核对。');continue
        times = re.findall(r'(?<!\d)([0-2]?\d)[:：]([0-5]\d)', raw)
        if times and int(times[0][0]) < 24:
            h, m = map(int, times[0]); value = f'{day}T{h:02}:{m:02}:00+08:00'
        else:
            value = day
        result[key] = value
        result['time_evidence'][key] = raw
        if key == 'start' and len(times) == 2 and int(times[1][0]) < 24:
            h, m = map(int, times[1]); end = f'{day}T{h:02}:{m:02}:00+08:00'
            if end > value:
                result['end'] = end
    loc = re.search(r'(?:举办地点|报告地点|讲座地点|活动地点|(?:^|\n)地点)\s*[：:]\s*([^\n]{1,180})', body)
    if loc:
        result['location'] = loc[1].strip()
    if warnings:result['time_warning']=' '.join(dict.fromkeys(warnings))
    return result

def college_url(url,host):
    p=urlsplit(url)
    if (p.scheme not in {'https','http'} or p.hostname!=host or p.port not in (None,443,80)
        or p.username or p.password or p.fragment):raise ValueError('学院链接超出读取范围。')
    article=bool(re.fullmatch(r'/\d{4}/\d{4}/c\d+a\d+/page\.(?:htm|psp)',p.path)) and not p.query
    redirect=p.path=='/_redirect' and bool(re.fullmatch(r'siteId=\d+&columnId=\d+&articleId=\d+',p.query))
    if not article and not redirect:raise ValueError('学院链接尚未适配。')
    return 'https://'+host+p.path+('?' + p.query if p.query else '')

def college_response(url,host,transport=None):
    visited=set()
    for _ in range(4):
        url=college_url(url,host)
        if url in visited:raise ValueError('学院链接发生循环跳转。')
        visited.add(url);r=fetch(url,transport=transport,allow_redirect=True)
        if r.status_code==200:return r,url
        url=urljoin(url,r.headers.get('location',''))
    raise ValueError('学院跳转次数过多。')

def parse_college(source, html, url, page):
    soup = BeautifulSoup(html, 'html.parser')
    # WebPlus list containers and next-page controls were observed on each registered school.
    rows = soup.select('#wp_news_w07 .hero-unify') if source.startswith('physics_') else soup.select('.wp_article_list .list_item, .news_list .news, .wp_listcolumn .list_item')
    records = {};unsupported=set()
    for row in rows:
        for a in row.select('a[href]'):
            u = urljoin(url, a['href'])
            try:u=college_url(u,urlsplit(url).hostname)
            except ValueError:unsupported.add(u);continue
            title = a.get('title') or a.get_text(' ', strip=True)
            if not title:
                continue
            p=urlsplit(u);record = item(source, p.path+('?' + p.query if p.query else ''), title, u)
            date_node = row.select_one('.news_meta, .Article_PublishDate, .date')
            record['published_at'] = _dated(date_node.get_text() if date_node else '')
            records.setdefault(record['id'],record)
    if not records:
        raise ServiceError('未识别学院列表，不能据此判定没有消息。', code='unavailable')
    next_a = soup.select_one('a.next[href]')
    has_next = bool(next_a and next_a.get('href') not in ('#','javascript:void(0);','javascript:void(0)'))
    return {'items':list(records.values()), 'next_page': page+1 if has_next else None,
            'coverage':'requested_page', 'source_url':url,'unsupported_links':len(unsupported)}

def listing(source, page=1, transport=None):
    if source not in SOURCES or type(page) is not int or not 1 <= page <= 100:
        raise ValueError('请选择信息源；页码须为 1–100。')
    label, url, kind = SOURCES[source]
    if kind == 'job':
        r = fetch(JOB+'/Ajax/jywapi.ashx', data={'action':'joblist','pageindex':page,'pagesize':10,
            'type':1 if source=='jobs' else 2}, transport=transport).json()
        if r.get('r') != 0 or not isinstance(r.get('data'),list) or type(r.get('PageCount')) is not int:
            raise ServiceError('就业列表结构已变化。', code='unavailable')
        records = {}
        for row in r['data']:
            if type(row.get('ID')) is not int or not row.get('JobName'):
                raise ServiceError('就业列表缺少必要字段。', code='unavailable')
            detail_url = url.replace('index.aspx', 'list.aspx') + '?itemid=' + str(row['ID'])
            x = item(source, row['ID'], row['JobName'], detail_url)
            x.update(company=text(row.get('CompanyName'),200), published_at=_dated(row.get('UpdateDate')),
                     location_code=str(row.get('JobCity') or ''), salary=text(row.get('SalaryName'),80))
            records[x['id']] = x
        return {'items':list(records.values()),'next_page':page+1 if page<r['PageCount'] else None,
                'total':r.get('RecorderCount'), 'coverage':'requested_page', 'source_url':url}
    if kind == 'event':
        # This exact public GET is used by the official recruitment page.
        r = fetch('https://ustc.ahbys.com/API/Web/index10358.ashx?pagesize=10&pageindex='+str(page)+'&action=bookinglist&kind=13&keyword=', transport=transport).json()
        if not isinstance(r.get('data'),list) or type(r.get('RecorderCount')) is not int:
            raise ServiceError('宣讲列表结构已变化。', code='unavailable')
        records=[]
        for row in r['data']:
            if not re.fullmatch(r'\d+',str(row.get('ID',''))) or not row.get('Theme'):
                raise ServiceError('宣讲字段变化。',code='unavailable')
            x=item(source,row['ID'],row['Theme'],JOB+'/Specialrecruitment/info.aspx?itemid='+str(row['ID']))
            x.update(_fields('举办时间：'+text(row.get('HoldDateTxt'))))
            x.update(location=text(row.get('VenuesName'),180), status=text(row.get('StatusName'),50))
            records.append(x)
        return {'items':records,'next_page':page+1 if page*10<r['RecorderCount'] else None,'coverage':'requested_page','source_url':url}
    if kind == 'college':
        if page>1:
            url=url.replace('/list.htm','/list'+str(page)+'.htm')
        return parse_college(source,fetch(url,transport=transport).text,url,page)
    if page>1:
        url+='?page='+str(page)
    soup=BeautifulSoup(fetch(url,transport=transport).text,'html.parser')
    records={}
    for a in soup.select('a[href]'):
        u=urljoin(url,a['href'])
        if re.fullmatch(re.escape(JOB)+r'/research/info.aspx\?itemid=\d+',u):
            x=item(source,u,a.get_text(' ',strip=True),u);records[x['id']]=x
    if not records:
        raise ServiceError('科研助理列表未识别，不能判定为空。',code='unavailable')
    match=re.search(r'共有\s*(\d+)\s*页',soup.get_text(' ',strip=True))
    return {'items':list(records.values()),'next_page':page+1 if match and page<int(match[1]) else None,'source_url':url,'coverage':'requested_page'}

def detail(source, url, transport=None):
    if source not in SOURCES:
        raise ValueError('未知信息源。')
    p=urlsplit(url);base=urlsplit(SOURCES[source][1])
    valid=(p.scheme=='https' and p.hostname==base.hostname and p.port in (None,443)
           and not p.username and not p.password and not p.fragment)
    kind=SOURCES[source][2]
    if kind=='college':
        valid=valid and college_url(url,base.hostname)==url
    else:
        folder={'jobs':'Jobinformation','internships':'Internship','research_jobs':'research','job_events':'Specialrecruitment'}[source]
        valid=valid and p.path==f'/{folder}/'+('list.aspx' if kind=='job' else 'info.aspx') and bool(re.fullmatch(r'itemid=\d{1,12}',p.query))
    if not valid:
        raise ValueError('请使用该信息源列表返回的正文链接。')
    response,resolved_url=college_response(url,base.hostname,transport) if kind=='college' else (fetch(url,transport=transport),url)
    html=response.text
    soup=BeautifulSoup(html,'html.parser')
    if kind=='event':
        identifier=parse_qs(p.query)['itemid'][0]
        if not re.search(r"infoapi\(\s*['\"]bookinginfo['\"],\s*['\"]info1['\"],\s*['\"]info2['\"],\s*"+identifier+r'\s*\)',html):
            raise ServiceError('宣讲详情读取合同变化。',code='unavailable')
        row=fetch(JOB+'/Ajax/jywapi.ashx',data={'action':'bookinginfo','rid':identifier},transport=transport).json()
        if row.get('r')!=0 or not row.get('Theme'):raise ServiceError('宣讲详情不可用。',code='unavailable')
        x=item(source,identifier,row['Theme'],url)
        x.update(summary=text(row.get('Description')),location=text(row.get('VenuesText'),200))
        x.update(_fields('举办时间：'+text(row.get('HoldDate'))+' '+text(row.get('TimeSlot'))))
        return x
    if kind=='job':
        # Parse only the exact observed detail-page AJAX contract, never eval website JS.
        call=re.search(r"jobinfo2\(\s*['\"](\w+)['\"].*?['\"]?(\d+)['\"]?\s*\)",html)
        if not call or call[1]!='jobinfo' or call[2]!=parse_qs(p.query)['itemid'][0]:
            raise ServiceError('职位详情读取合同尚未识别。',code='unavailable')
        row=fetch(JOB+'/Ajax/jywapi.ashx',data={'action':'jobinfo','jid':call[2]},transport=transport).json()
        if row.get('r')!=0 or str(row.get('ID'))!=call[2] or not row.get('JobName'):
            raise ServiceError('职位详情不匹配。',code='unavailable')
        x=item(source,call[2],row['JobName'],url)
        x.update(company=text(row.get('CompanyName'),200),location=text(row.get('Workplace'),200),
                 published_at=_dated(row.get('UpdateTime')), salary=text(row.get('Salary'),100),
                 education=text(row.get('EducationRequired'),200),major=text(row.get('DisciplineRequired'),500),
                 summary='\n'.join(text(row.get(k),3500) for k in ['Describe','Require','Benefits','Future']))
        # Days is a duration, not an absolute deadline. Never derive expiry from it.
        raw=text(row.get('ValidityPeriod'),100)
        if re.fullmatch(r'20\d{2}[-/]\d{1,2}[-/]\d{1,2}',raw):
            x.update(deadline=_dated(raw),time_evidence={'deadline':raw})
        return x
    body=soup.select_one('.wp_articlecontent, .article, .info_cont, .info_content, .InfoContent, .content_detail, .edit, .edit-info')
    title=soup.select_one('.arti_title, .Article_Title, h1, .info_title')
    if not body or not title:
        raise ServiceError('正文结构未识别，请查看原文。',code='unavailable')
    for node in body.select('script,style,form,iframe'):
        node.decompose()
    paragraphs=body.select('p')
    content='\n'.join(p.get_text(' ',strip=True) for p in paragraphs) if paragraphs else body.get_text('\n',strip=True)
    x=item(source,p.path+('?' + p.query if p.query else '') if kind=='college' else url,title.get_text(' ',strip=True),url)
    x.update(summary=content[:16000],truncated=len(content)>16000)
    x['image_content_present']=bool(body.select('img'))
    if kind=='college' and x['image_content_present'] and len(content)<500:
        from .posters import read_images
        x['poster_ocr']=read_images(body,resolved_url,lambda u:fetch(u,transport=transport))
        ocr_text='\n'.join(i['text'] for i in x['poster_ocr']['images'])
        if ocr_text:
            x['summary']=(content+'\n【海报 OCR，需与原图核对】\n'+ocr_text)[:16000]
            x['time_candidates']=_fields(ocr_text)
        x['time_warning']=x['poster_ocr']['note']
    if len(x['title'])<4 or len(x['summary'])<10:
        x['quality']='insufficient_information'
    x.update(_fields(content))
    date_node=soup.select_one('.arti_update, .arti_metas, .Article_PublishDate, .info_time')
    x['published_at']=_dated(date_node.get_text() if date_node else '')
    return x
