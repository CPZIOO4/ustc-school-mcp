"""Prepare, publish once, and independently verify Nan7 offers."""
from __future__ import annotations

import base64
import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from ..network import http_client, bounded_request
from ..workflow_store import PlanStore, digest, require, WorkflowError
from .client import Nan7Client, valid_offer_id, CATEGORIES
from .session import load_session, API_URL, SITE_URL, Nan7Error


def image_record(path):
    p = Path(path)
    require(p.is_absolute() and p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}, '图片须为用户指定的本地PNG/JPEG/WebP绝对路径。')
    require(0 < p.stat().st_size <= 10*1024*1024, '单张图片须在10MiB以内。')
    data = p.read_bytes()
    require(0 < len(data) <= 10*1024*1024, '读取图片时大小变化。')
    kind = 'image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if data.startswith(b'\xff\xd8\xff') else 'image/webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None
    require(kind is not None, '无法识别图片格式。')
    return dict(name=p.name, mime=kind, size=len(data), sha256=hashlib.sha256(data).hexdigest(), data=base64.b64encode(data).decode())


class Publisher:
    def __init__(self, session=None, transport=None, directory=None):
        self.session = session or load_session()
        self.token = self.session['token']
        self.binding = digest(self.token)
        self.account = digest(self.session.get('account') or self.token)
        self.store = PlanStore('nan7', self.account, directory)
        self.reader = Nan7Client(self.token, transport)
        self.transport = transport

    def request(self, path, *, payload=None, file=None):
        require(path in {'/v1/profile/me','/v1/media/new','/v1/offer/new'}, '未支持的集市请求。')
        method = 'GET' if path == '/v1/profile/me' else 'POST'
        options = {'files': {'file': (file['name'],base64.b64decode(file['data']),file['mime'])}} if file else {'json': payload} if method == 'POST' else {}
        try:
            with http_client('nan7', Nan7Error, transport=self.transport, timeout=30, follow_redirects=False, headers={'Authorization':'Bearer '+self.token, 'Origin':SITE_URL,'Referer':SITE_URL+'/'}) as c:
                r = bounded_request(c,method,API_URL+path,maximum_bytes=5*1024*1024,error_type=Nan7Error,**options)
                if r.status_code != 200 or 'application/json' not in r.headers.get('content-type',''):
                    raise Nan7Error('集市未返回可确认的结果；不能直接重发。')
                try:
                    data = r.json()
                except ValueError:
                    raise Nan7Error('集市返回结构已变化。') from None
                if not isinstance(data,dict):
                    raise Nan7Error('集市返回结构已变化。')
                return data
        except (httpx.HTTPError, ValueError):
            raise Nan7Error('集市网络或响应异常；先查记录，不能重复发布。') from None

    def profile(self):
        data = self.request('/v1/profile/me')
        require(valid_offer_id(data.get('id')), '无法确认当前集市账号。')
        return str(data['id'])

    def duplicates(self, owner, title):
        found, page = [], None
        for _ in range(3):
            require(str(owner).isdigit(), '集市账号ID结构已变化。')
            payload = {'owner':int(owner), 'query':title}
            if page:
                payload['page'] = page
            # Site requires a type; inspect both categories of intended offer separately.
            payload['type'] = self.offer_type
            data = self.reader._post('/v1/offer/search', payload)
            require(isinstance(data.get('offers'),list), '无法检查已有商品，停止发布。')
            for o in data['offers']:
                require(isinstance(o,dict), '已有商品结构未知。')
                if o.get('title') == title and o.get('valid') is not False:
                    found.append(str(o.get('id')))
            page = data.get('nextPage')
            if not page:
                return found
        raise WorkflowError('已有商品查询超过3页；缩小范围并人工核对，不能默认没有重复。')

    def prepare(self, title='', description='', price=None, contact='', category=None, offer_type='sell', images=None):
        require(offer_type in {'sell','buy'}, '类型为sell或buy。')
        require(all(isinstance(x,str) for x in [title,description,contact]), '标题、描述和联系方式需为文本。')
        missing = [name for name,value in [('title',title),('description',description),('contact',contact)] if not value.strip()]
        if offer_type == 'sell' and category is None:
            missing.append('category')
        if missing:
            return dict(state='needs_input', missing_fields=missing, can_execute=False, next_action='ask_user_for_missing_fields', mutation_performed=False)
        require(len(title.strip()) <= 100 and len(description) <= 20000 and len(contact) <= 500, '标题最多100字，描述最多20000字，联系方式最多500字。')
        require(category is None or type(category) is int and 0 <= category <= 6, '分类为0–6。')
        price_text = None
        if price is not None:
            try:
                value = Decimal(str(price))
                require(value.is_finite() and value >= 0 and value.as_tuple().exponent >= -2, '价格应为非负金额，最多两位小数。')
                price_text = format(value, 'f')
                require(len(price_text) <= 10, '价格最多10位字符。')
            except InvalidOperation:
                raise WorkflowError('价格格式无效。') from None
        require(isinstance(images or [],list) and len(images or []) <= 9, '最多9张图片。')
        records = [image_record(p) for p in (images or [])]
        require(sum(r['size'] for r in records) <= 30*1024*1024, '图片总大小最多30MiB。')
        self.offer_type = offer_type
        owner = self.profile()
        duplicates = self.duplicates(owner,title.strip())
        if duplicates:
            return dict(state='existing_offer', can_execute=False, existing_offer_ids=duplicates, next_action='review_existing_offer', mutation_performed=False)
        payload = dict(type=offer_type,title=title.strip(),category=category if offer_type=='sell' else 0,price=price_text,detail=description.strip()+'\n\n联系方式：'+contact.strip())
        preview = dict(**payload, category_name=CATEGORIES[payload['category']], images=[{k:v for k,v in r.items() if k!='data'} for r in records], publication_scope='公开商品详情，含上述联系方式', price_unspecified=price is None)
        return self.store.create('publish',dict(payload=payload,images=records), dict(owner=owner),self.binding,preview)

    def verify(self, plan, offer_id, image_ids):
        raw = self.reader._post('/v1/offer/get', {'id':offer_id})
        payload = plan['content']['payload']
        owner = raw.get('owner') or {}
        equal = str(raw.get('id')) == str(offer_id) and raw.get('editable') is True and raw.get('valid') is True and str(owner.get('id')) == plan['snapshot']['owner']
        equal = equal and all(raw.get(k) == payload[k] for k in ['type','title','category','detail'])
        equal = equal and ((raw.get('price') is None and payload['price'] is None) or str(raw.get('price')) == str(payload['price']))
        actual_images = raw.get('images')
        equal = equal and isinstance(actual_images,list) and [str(x.get('id')) for x in actual_images if isinstance(x,dict)] == [str(x) for x in image_ids]
        return dict(matched=bool(equal), offer_id=str(offer_id), url=SITE_URL+'/offer/'+str(offer_id), image_ids=image_ids, verified_fields=['owner','title','type','category','price','detail','image_ids'] if equal else [])

    def publish(self, plan_id, content_sha256):
        if not self.store.claim(plan_id,content_sha256):
            return self.store.view(plan_id)
        wrote, offer_id, image_ids = False, None, []
        try:
            plan = self.store.get(plan_id)['plan']
            require(plan['session'] == self.binding and self.profile() == plan['snapshot']['owner'], '当前账号或会话已变化，重新准备。')
            payload = plan['content']['payload']
            self.offer_type = payload['type']
            require(not self.duplicates(plan['snapshot']['owner'],payload['title']), '发现同标题现有商品，停止重复发布。')
            for i, record in enumerate(plan['content']['images']):
                self.store.phase(plan_id,f'upload_{i}_requested'); wrote=True
                media = self.request('/v1/media/new',file=record)
                require(valid_offer_id(media.get('id')), '图片上传未返回有效ID。')
                image_ids.append(media['id'])
            self.store.phase(plan_id,'publish_requested'); wrote=True
            result = self.request('/v1/offer/new',payload={**payload,'images':image_ids})
            require(valid_offer_id(result.get('id')), '发布未返回有效商品ID，不能重发。')
            offer_id = result['id']
            self.store.checkpoint(plan_id,dict(offer_id=offer_id,image_ids=image_ids))
            self.store.phase(plan_id,'readback')
            result = self.verify(plan,offer_id,image_ids)
            self.store.finish(plan_id,'verified' if result['matched'] else 'uncertain',result)
        except Exception as exc:
            self.store.finish(plan_id,'uncertain' if wrote else 'stopped', dict(reason='write_result_needs_verification' if wrote else 'preflight_failed', detail=str(exc) if isinstance(exc,(WorkflowError,Nan7Error)) else type(exc).__name__,offer_id=offer_id,image_ids=image_ids, next_action='check_status_do_not_retry' if wrote else 'check_connection_and_existing_offers'))
        return self.store.view(plan_id)

    def status(self, plan_id, verify=False):
        record = self.store.get(plan_id)
        result = record['result'] or {}
        if verify and record['state'] in {'uncertain','executing'} and valid_offer_id(result.get('offer_id')):
            plan=record['plan']
            require(self.profile() == plan['snapshot']['owner'], '账号已变化。')
            result = self.verify(plan,result['offer_id'],result.get('image_ids',[]))
            self.store.finish(plan_id,'verified' if result['matched'] else 'uncertain',result)
        return self.store.view(plan_id)
