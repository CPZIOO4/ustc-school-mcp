"""Bounded local OCR of article images from the same official origin."""
import hashlib
import json
import re
from urllib.parse import urljoin,urlsplit
from ..mail.config import local_dir

def read_images(body,article_url,fetch):
    from ..desktop.document_io import image_text
    records=[];skipped=0;errors=[]
    nodes=body.select('img[src]')
    for node in nodes[:2]:
        url=urljoin(article_url,node['src']);p=urlsplit(url)
        if (p.scheme!='https' or p.hostname!=urlsplit(article_url).hostname or p.port not in (None,443)
            or p.username or p.password or p.query or p.fragment
            or not re.fullmatch(r'/_upload/article/(?:images|files)/[A-Za-z0-9_./-]+\.(?:png|jpe?g|webp)',p.path,re.I)
            or '..' in p.path):
            skipped+=1;continue
        try:
            response=fetch(url)
            if not response.headers.get('content-type','').lower().startswith('image/'):raise ValueError('not image')
            raw=response.content;key=hashlib.sha256(raw).hexdigest()
            folder=local_dir()/'digest-ocr-cache';folder.mkdir(parents=True,exist_ok=True)
            cache=folder/(key+'.json')
            if cache.exists() and cache.stat().st_size<100000:
                result=json.loads(cache.read_text(encoding='utf-8'))
            else:
                result=image_text(raw);result['content']=result['content'][:12000]
                cache.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
                for old in sorted(folder.glob('*.json'),key=lambda f:f.stat().st_mtime,reverse=True)[100:]:old.unlink(missing_ok=True)
            records.append({'source_url':url,'text':result['content'],'minimum_confidence':result.get('minimum_confidence'),
                            'requires_review':True})
        except Exception as exc:
            # Preserve article text and disclose missing image coverage.
            skipped+=1
            from ..safety_log import record
            errors.append(record(service='digest',stage='background',exc=exc,outcome='read_failed'))
    return {'images':records,'errors':errors,'skipped':skipped+max(0,len(nodes)-2),'note':'本机 OCR；数字和时间需与原图核对，不自动加入日历或截止提醒。'}
