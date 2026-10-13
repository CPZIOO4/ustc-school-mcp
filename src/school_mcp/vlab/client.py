"""Own-VM discovery and encrypted SSH access via the official key-management page."""
from contextlib import contextmanager
import io
import re
from urllib.parse import urlsplit, unquote
from bs4 import BeautifulSoup
import paramiko
from ..campus.session import browser_session
from ..desktop.storage import read_private, write_private
from ..mail.config import local_dir
from ..bb.identity import load_credentials
from ..service_errors import ServiceError


def connected(s):
    page = s.goto()
    login = page.get_by_role('link', name='统一身份认证登录', exact=True)
    if login.count():
        login.first.click()
        page.wait_for_url('**/vm/', timeout=30000)
    s.require_site()
    page.locator('a[href="/vm/userinfo/"]').wait_for(state='attached', timeout=12000)
    account = page.locator('a[href="/vm/userinfo/"]').inner_text().strip()
    s.confirm(account)
    return page


def vm_records(page):
    soup = BeautifulSoup(page.content(), 'html.parser')
    found = {}
    for a in soup.select('a[href]'):
        match = re.fullmatch(r'/vm/ssh/listkey/(\d+)/?', urlsplit(a['href']).path)
        if match:
            found[int(match[1])] = {'vm_id': int(match[1]), 'ssh_available': True}
    return list(found.values())


def machines():
    with browser_session('vlab') as s:
        page = connected(s)
        return {'connected': True, 'machines': vm_records(page), 'source_url': s.base + '/vm/',
                'note': '仅列出当前账号可管理的虚拟机；不会新建、重启、删除或重置密码。'}


def key_for(vm_id):
    if type(vm_id) is not int or vm_id < 1:
        raise ServiceError('虚拟机编号无效。', code='invalid_arguments')
    account = load_credentials()['username']
    path = local_dir() / f'vlab-key-{vm_id}.dpapi'
    saved = read_private(path, {})
    if saved and saved.get('account') == account:
        return saved['private_key']
    with browser_session('vlab') as s:
        page = connected(s)
        if vm_id not in {x['vm_id'] for x in vm_records(page)}:
            raise ServiceError('虚拟机不在当前账号列表中。', code='invalid_arguments')
        page = s.goto('/vm/ssh/listkey/' + str(vm_id))
        # The site's existing-key download is a data URL; never expose it as a link
        # or artifact. No new key is generated and no account password is needed.
        values = page.locator('a[download][href^="data:"]').evaluate_all('(xs)=>xs.map(x=>x.href)')
        keys = []
        for value in values:
            if len(value) > 25000 or ',' not in value:
                continue
            raw = unquote(value.split(',', 1)[1])
            try:
                paramiko.RSAKey.from_private_key(io.StringIO(raw))
            except (paramiko.SSHException, ValueError):
                continue
            keys.append(raw)
        if len(keys) != 1:
            raise ServiceError('未找到唯一现有 SSH 密钥，请在 Vlab 官方页面核实虚拟机 SSH 配置。', code='manual_verification_required')
        write_private(path, {'account': account, 'vm_id': vm_id, 'private_key': keys[0]})
        return keys[0]


@contextmanager
def ssh(vm_id, username='vlab'):
    if username not in {'vlab', 'ubuntu', 'root'}:
        raise ServiceError('请选择虚拟机现有用户 vlab、ubuntu 或 root。', code='invalid_arguments')
    key = paramiko.RSAKey.from_private_key(io.StringIO(key_for(vm_id)))
    client = paramiko.SSHClient()
    hosts = local_dir() / 'vlab-known-hosts'
    if hosts.exists():
        client.load_host_keys(str(hosts))
    class FirstUse(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, c, host, remote_key):
            if host != 'vlab.ustc.edu.cn':
                raise ServiceError('不是 Vlab SSH 入口。', code='invalid_arguments')
            c.get_host_keys().add(host, remote_key.get_name(), remote_key)
            c.save_host_keys(str(hosts))
    client.set_missing_host_key_policy(FirstUse())
    try:
        client.connect('vlab.ustc.edu.cn', username=username, pkey=key, timeout=20, auth_timeout=20,
                       banner_timeout=20, look_for_keys=False, allow_agent=False)
        yield client
    except paramiko.BadHostKeyException:
        raise ServiceError('Vlab SSH 主机指纹与本机记录不同，已停止连接。', code='unavailable') from None
    except paramiko.AuthenticationException:
        raise ServiceError('当前虚拟机用户未接受现有密钥，请核实所选用户。', code='authentication_required') from None
    finally:
        client.close()


def environment(vm_id, username='vlab'):
    # A fixed read-only command, not a model-supplied shell expression.
    script = 'id -un; uname -srm; command -v python3; command -v git; command -v bash; df -Pk "$HOME" | tail -1'
    with ssh(vm_id, username) as client:
        _, stdout, stderr = client.exec_command(script, timeout=20)
        text = stdout.read(16000).decode('utf-8', 'replace')
        error = stderr.read(2000).decode('utf-8', 'replace')
        status = stdout.channel.recv_exit_status()
    return {'vm_id': vm_id, 'username': username, 'exit_code': status, 'summary': text, 'stderr': error,
            'read_only': True, 'untrusted_content': True}
