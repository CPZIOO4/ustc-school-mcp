"""Loopback-only HTTP UI. Every private route requires a per-launch capability."""
import argparse
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs, unquote

from .app import Application
from .chat import ui_view
from .storage import read_private, write_private
from .. import safety_log


def assets():
    return Path(__file__).with_name('web')


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, application, port=0):
        self.app = application
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    server_version = 'SchoolLocal/1'
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass  # No URL, attachment name, API key or request body access logs.

    def reply(self, value, status=200, content_type='application/json; charset=utf-8'):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        if status >= 400:
            # Rejected requests may still have unread bodies. Do not reuse them.
            self.close_connection = True
            self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def authorized(self):
        return self.valid_host() and hmac.compare_digest(self.headers.get('X-School-Token', ''), self.server.token)

    def do_GET(self):
        if not self.valid_host():
            return self.reply({'error': '访问来源不正确。', **safety_log.record(code='access_denied', stage='api', outcome='not_started')}, 403)
        target = urlsplit(self.path)
        public = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/transport.js': ('transport.js', 'text/javascript; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if target.path in public:
            filename, content_type = public[target.path]
            return self.reply((assets() / filename).read_bytes(), content_type=content_type)
        if not self.authorized():
            return self.reply({'error': '请从本机应用入口打开页面。', **safety_log.record(code='access_denied', stage='api', outcome='not_started')}, 403)
        try:
            app = self.server.app
            if target.path == '/api/diagnostics':
                return self.reply(safety_log.report())
            if target.path == '/api/diagnostics/export':
                return self.reply(safety_log.export(), content_type='application/json; charset=utf-8')
            if target.path == '/api/status':
                return self.reply(app.status())
            if target.path == '/api/campus/sites':
                from .campus import sites
                return self.reply(sites())
            app.require_login()
            if target.path in {'/api/campus/profile', '/api/campus/timetable'}:
                from . import campus
                with app.mutex:
                    app.require_login()
                    return self.reply(campus.profile(app.data) if target.path.endswith('/profile') else campus.timetable(app.data))
            if target.path == '/api/digest':
                from ..digest.server import school_digest_overview
                return self.reply(school_digest_overview())
            if target.path == '/api/artifact':
                from .artifacts import download
                query = parse_qs(target.query)
                return self.reply(download(app.data, query.get('chat', [''])[0], query.get('id', [''])[0]),
                                  content_type='application/octet-stream')
            if target.path == '/api/chats':
                return self.reply({'chats': app.data.chats()})
            if target.path == '/api/chat':
                identifier = parse_qs(target.query).get('id', [''])[0]
                return self.reply(ui_view(app.data.chat(identifier)))
            if target.path == '/api/tasks':
                from ..agent.server import school_agent_tasks
                query = parse_qs(target.query)
                offset = int(query.get('offset', ['0'])[0])
                if not 0 <= offset <= 10000:
                    raise ValueError('页码无效。')
                return self.reply(school_agent_tasks(offset=offset, limit=20))
            return self.reply({'error': '入口不存在。'}, 404)
        except ValueError as exc:
            incident = safety_log.record(code='invalid_arguments', stage='api', exc=exc, outcome='not_started')
            return self.reply({'error': str(exc)[:300], **incident}, 400)
        except Exception as exc:
            incident = safety_log.record(stage='api', exc=exc, outcome='read_failed')
            return self.reply({'error': '读取未完成，请检查本机状态。', **incident}, 500)

    def do_POST(self):
        if (not self.authorized() or self.headers.get('Origin') != self.server.origin
                or self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin')):
            # Drain only small bodies, otherwise Windows may reset the connection
            # before a browser receives the rejection. No content is parsed or logged.
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if 0 < length <= 200_000:
                    self.connection.settimeout(2)
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            return self.reply({'error': '请求来源或访问凭证不正确。', **safety_log.record(code='access_denied', stage='api', outcome='not_started')}, 403)
        try:
            length = int(self.headers.get('Content-Length', '-1'))
            upload = urlsplit(self.path).path == '/api/upload'
            maximum = 50 * 1024 * 1024 if upload else 200_000
            if not 0 <= length <= maximum:
                # A bounded discard lets Windows deliver 413 before closing the socket.
                # Never parse/store a rejected body, and never drain an unbounded upload.
                if 0 < length <= 1_000_000:
                    try:
                        self.connection.settimeout(1)
                        self.rfile.read(length)
                    except OSError:
                        pass
                return self.reply({'error': '请求大小超过限制。'}, 413)
            self.connection.settimeout(30)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('请求未完整传输。')
            if upload:
                name = unquote(self.headers.get('X-File-Name', ''))
                with self.server.app.mutex:
                    return self.reply(self.server.app.upload(name, raw))
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.reply({'error': '请求格式不正确。'}, 415)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('请求须为对象。')
            if self.path == '/api/shutdown':
                self.reply(self.server.app.shutdown())
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            with self.server.app.mutex:
                return self.reply(self.server.app.api(self.path, value))
        except ValueError as exc:
            from pydantic import ValidationError
            message = '请检查填写格式；地址、模型和密钥均为必填。' if isinstance(exc, ValidationError) else str(exc)[:300]
            self.reply({'error': message, **safety_log.record(code='invalid_arguments', stage='api', exc=exc, outcome='not_started')}, 400)
        except Exception as exc:
            self.reply({'error': '操作未完成，请检查输入与当前任务状态。',
                        **safety_log.record(stage='api', exc=exc, outcome='unknown')}, 500)


def main(argv=None):
    parser = argparse.ArgumentParser(description='USTC School Agent local application')
    parser.add_argument('--data-dir')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--native-stdio', action='store_true', help='Private handshake with the native desktop host')
    parser.add_argument('--self-check', action='store_true', help='Offline bundled tools and headless Chrome check')
    parser.add_argument('--export-diagnostics', help='Export sanitized diagnostics without opening the app or decrypting accounts')
    args = parser.parse_args(argv)
    root = Path(args.data_dir or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'USTCSchoolAgent').resolve()
    safety_log.configure_desktop(root)
    if args.export_diagnostics:
        target = Path(args.export_diagnostics).resolve()
        # No overwrite and no writes inside the private application data.
        if target.is_relative_to(root):
            raise ValueError('诊断报告请保存到应用数据目录以外。')
        with target.open('xb') as output:
            output.write(safety_log.export())
        return
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    app = Application(args.data_dir)
    # OS file lock is held for the whole process; another launch opens the existing UI.
    handle = (app.data.root / 'desktop.lock').open('a+b')
    handle.write(b'0'); handle.flush(); handle.seek(0)
    import msvcrt
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        previous = read_private(app.data.root / 'instance.dpapi', {})
        if args.native_stdio:
            native_handshake(previous.get('url', ''), owned=False, core=previous.get('core'))
        elif previous.get('url', '').startswith('http://127.0.0.1:') and not args.no_open:
            webbrowser.open(previous['url'])
        handle.close()
        return
    app.data.recover()
    if args.self_check:
        from .diagnostics import check
        report = check()
        (app.data.root / 'runtime-check.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()
        raise SystemExit(0 if report['ready'] else 1)
    with Server(app, args.port) as server:
        url = server.origin + '/#' + server.token
        from . import native_core
        core = native_core.call('status', {}) if native_core.enabled() else None
        write_private(app.data.root / 'instance.dpapi', {'url': url, 'pid': os.getpid(), 'core': core})
        if args.native_stdio:
            native_handshake(url, owned=True, core=core)
            # The Rust host holds stdin open. On a host crash, stop future steps,
            # allow in-flight operations to settle, then release this owned server.
            def parent_lifetime():
                sys.stdin.buffer.read()
                app.stop_event.set()
                while app.busy():
                    time.sleep(.25)
                app.shutdown()
                server.shutdown()
            threading.Thread(target=parent_lifetime, daemon=True).start()
        elif not args.no_open:
            webbrowser.open(url)
        # Resume only a previously authorized subscription, without blocking UI startup.
        def resume_digest():
            try:
                from ..digest.worker import ensure
                ensure(allow_in_process=True)
            except Exception as exc:
                safety_log.record(service='digest', stage='background', exc=exc, outcome='not_started')
        threading.Thread(target=resume_digest, daemon=True).start()
        try:
            server.serve_forever(poll_interval=.3)
        finally:
            (app.data.root / 'instance.dpapi').unlink(missing_ok=True)
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()


def native_handshake(url, *, owned, core=None):
    """Only to the parent's anonymous pipe; never a file, URL argument or log."""
    target = urlsplit(url)
    if (target.scheme != 'http' or target.hostname != '127.0.0.1' or not target.port
            or target.username or target.password or target.path not in ('', '/')
            or target.query or not 32 <= len(target.fragment) <= 128):
        raise ValueError('本机服务入口无效，请重新打开应用。')
    print(json.dumps({'protocol': 1, 'origin': f'http://127.0.0.1:{target.port}',
                      'token': target.fragment, 'owned': owned, 'core': core}), flush=True)


if __name__ == '__main__':
    main()
