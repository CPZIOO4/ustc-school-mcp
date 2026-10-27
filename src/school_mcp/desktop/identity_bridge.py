"""Reuse an explicitly linked, same-owner MCP identity without copying credentials.

The link is encrypted local installation configuration, never model/UI arguments.
Only the existing authentication provider runs against that profile. School records,
mail configuration, passwords and API keys are never imported into the desktop.
"""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from .storage import read_private
from ..service_errors import ServiceError

FILES = {'bb': 'bb.session.dpapi', 'jw': 'jw.session.dpapi', 'library': 'library.session.dpapi',
         'young': 'young-session.dpapi', 'finance': 'finance-session.dpapi', 'nan7': 'nan7-session.dpapi'}

def _path(path):
    p=Path(path)
    if not p.is_absolute() or '..' in p.parts:
        raise ServiceError('关联身份目录无效。', code='identity_required')
    for part in (p, *p.parents):
        if part.exists() and (part.is_symlink() or getattr(part.stat(), 'st_file_attributes', 0)&0x400):
            raise ServiceError('关联身份目录发生变化，请重新关联。', code='identity_required')
    return p

def _binding(root):
    root=Path(root)
    link=read_private(root/'shared-identity-link.dpapi', None)
    if not link or link.get("allow_recovery") is not True:return None
    if (root/'locked').exists():raise ServiceError('应用已锁定。',code='identity_required')
    source=_path(link['source'])
    local=read_private(root/'profile/ustc-identity.credentials.dpapi', None) or {}
    remote=read_private(source/'ustc-identity.credentials.dpapi', None) or {}
    account=local.get('username')
    if not account or remote.get('username')!=account or link.get('owner')!=hashlib.sha256(account.encode()).hexdigest():
        raise ServiceError('关联身份与当前账号不一致，请重新关联本人账号。',code='identity_mismatch')
    return source,account,remote

def status(root):
    try:
        link=_binding(root)
        return {'linked':bool(link),'credential_recovery':bool(link and link[2].get('password')),
                'email_verification':bool(link and link[2].get('email_verification_enabled'))}
    except Exception:return {'linked':False,'needs_relink':True}

@contextmanager
def _scope(source):
    old=os.environ.get('SCHOOL_MCP_LOCAL_DIR');os.environ['SCHOOL_MCP_LOCAL_DIR']=str(source)
    try:yield
    finally:
        if old is None:os.environ.pop('SCHOOL_MCP_LOCAL_DIR',None)
        else:os.environ['SCHOOL_MCP_LOCAL_DIR']=old

def restore(root, service, stopped, notify):
    from .connections import connect
    from . import native_core
    binding=_binding(root)
    if not binding or service not in FILES:return {'linked':False}
    source,account,_=binding
    if stopped.is_set():raise ServiceError('已停止连接。',code='cancelled')
    notify('restoring_shared_identity','正在复用本机已关联的统一身份及邮件验证设置…')
    with _scope(source):
        result=connect(service,stopped,notify,restore=True)
    if result.get('connected') is not True:return {**result,'linked':True}
    if stopped.is_set():raise ServiceError('已停止连接。',code='cancelled')
    fresh=_binding(root)
    if fresh is None or fresh[:2]!=(source,account):raise ServiceError('关联账号已变化。',code='identity_mismatch')
    # Validate all records before writing; copy sessions only, using native scope checks.
    values=[]
    for filename,kind in [(FILES[service],service),('ustc-identity.device.dpapi','device')]:
        if not (source/filename).is_file():continue
        value=native_core.call('sessions_read',{'profile':kind,'path':str(source/filename),'account':account})
        values.append((filename,kind,value))
    for filename,kind,value in values:
        native_core.call('sessions_write',{'profile':kind,'path':str(Path(root)/'profile'/filename),'account':account,'value':value})
    return {**result,'linked':True,'shared_identity_reused':True,'credentials_copied':False}
