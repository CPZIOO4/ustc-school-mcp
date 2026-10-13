"""Narrow private-pipe facade for native desktop entry-point IO."""
import base64
import hashlib
import threading

from . import native_core


def call(data, action, **value):
    return native_core.call('app_io', {'action': action, 'root': str(data.root),
                                      'slot': getattr(data, 'model_slot', 'primary'), **value})


def test_connection(data, notify, stopped=None):
    identifier = call(data, 'test_begin')['id']
    done = threading.Event()
    cancellation_errors = []

    def cancel():
        while not done.wait(.05):
            if stopped is not None and stopped.is_set():
                try:
                    call(data, 'test_cancel', id=identifier)
                except Exception as error:
                    cancellation_errors.append(error)
                return

    notify('model_tool_check', '正在验证接口、工具调用与结果读取…')
    watcher = threading.Thread(target=cancel, daemon=True)
    try:
        if stopped is not None and stopped.is_set():
            call(data, 'test_cancel', id=identifier)
        watcher.start()
        result = call(data, 'test_connection', id=identifier)
    finally:
        done.set()
        if watcher.ident is not None:
            watcher.join(timeout=6)
    if cancellation_errors:
        raise cancellation_errors[0]
    if result.get('connected'):
        notify('model_ready', '模型已就绪，可以开始对话。')
    return result


def upload(data, name, content):
    # The pipe has a 16 MiB frame limit; bounded chunks preserve the existing
    # 50 MiB upload contract. Only Rust opens/writes/publishes the upload file.
    value = call(data, 'upload_begin', name=name, size=len(content),
                 sha256=hashlib.sha256(content).hexdigest())
    identifier = value['id']
    try:
        for offset in range(0, len(content), 1024 * 1024):
            chunk = content[offset:offset + 1024 * 1024]
            result = call(data, 'upload_chunk', id=identifier, offset=offset,
                          content=base64.b64encode(chunk).decode('ascii'))
            if result.get('offset') != offset + len(chunk):
                raise ValueError('附件未完整传输，请重新选择文件。')
        return call(data, 'upload_finish', id=identifier)
    except Exception:
        try:
            call(data, 'upload_abort', id=identifier)
        except Exception:
            pass  # Cleanup cannot replace the original error or erase a published file.
        raise
