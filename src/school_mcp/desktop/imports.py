"""Explicit local migration; never discover or import another profile automatically."""
import json
from pathlib import Path

from .storage import read_private
from ..bb.identity import load_credentials
from ..mail.config import MailConfig, local_dir, load_config
from ..mail.credentials import save_credentials
from ..mail.client import MailClient


def reuse_mail(data, source_directory):
    if local_dir() != data.profile.resolve() or (data.root / 'locked').exists():
        raise ValueError('请在已登录的目标应用档案中复用邮箱。')
    source = Path(source_directory).resolve()
    if source == data.profile.resolve():
        return {**MailClient(load_config()).check(), 'reused': True, 'created_client_password': False}
    try:
        source_identity = read_private(source / 'ustc-identity.credentials.dpapi', {})
        if source_identity.get('username') != load_credentials()['username']:
            raise ValueError('源配置与本机学校账号不一致。')
        config = MailConfig(json.loads((source / 'mail.json').read_text(encoding='utf-8'))['address'])
        credential = read_private(source / 'mail.credentials.dpapi', {})
        if credential.get('address') != config.address or not credential.get('password'):
            raise ValueError('原邮箱配置不完整。')
        if (data.profile / 'mail.json').exists():
            if load_config().address != config.address:
                raise ValueError('应用已绑定其他邮箱，请先核对并解绑后再复用。')
            if (data.profile / 'mail.credentials.dpapi').exists():
                return {**MailClient(load_config()).check(), 'reused': True, 'created_client_password': False}
        result = MailClient(config, credential['password']).check()
        if result.get('connected') is not True:
            raise ValueError('原邮箱未通过连接核实，未迁移。')
        save_credentials(config, credential['password'])
        return {**result, 'reused': True, 'created_client_password': False,
                'note': '已核对同一学校账号并复用原客户端凭据，未创建新密码。'}
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        raise ValueError('原邮箱配置无法读取；原文件与目标配置均保留。') from None
