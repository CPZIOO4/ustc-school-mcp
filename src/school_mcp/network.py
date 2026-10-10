"""Cross-process pacing and failure cooldowns; never store request or account data.

No request is replayed here. Callers may try again after the reported cooldown.
"""
from __future__ import annotations

import math
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .mail.config import local_dir
from .service_errors import policy_error

INTERVALS = {"mail": 0.5, "smtp": 2.0, "bb": 1.0, "jw": 1.0, "library": 1.0,
             "library-public": 1.0, "teach": 1.0, "nan7": 1.0, "icourse": 1.0,
             "young": 2.0, "finance": 1.0, "identity": 5.0}


class PolicyError(RuntimeError):
    pass


class CooldownError(PolicyError):
    def __init__(self, seconds: float):
        self.retry_after_seconds = max(1, math.ceil(seconds))
        super().__init__(f"该站点请求正在冷却，请至少 {self.retry_after_seconds} 秒后再试；不要连续重试或重新登录。")


def retry_after(value: str | None, now: float) -> float:
    if not value:
        return 0.0
    try:
        if value.strip().isdigit():
            seconds = float(value)
            # Saturate unrepresentable values far beyond a usable wait, never to a short retry.
            return seconds if math.isfinite(seconds) else 1e300
        date = parsedate_to_datetime(value)
        return max(0.0, date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return 0.0


class RequestLimiter:
    def __init__(self, path: Path | None = None, *, clock=time.time, sleep=time.sleep, jitter=random.random):
        self.path = path
        self.clock, self.sleep, self.jitter = clock, sleep, jitter

    @contextmanager
    def state(self, service: str):
        if service not in INTERVALS:
            raise ValueError("Unknown service")
        path = self.path or local_dir() / "request-policy.sqlite3"
        connection = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=2, isolation_level=None)
            connection.execute("CREATE TABLE IF NOT EXISTS pacing (service TEXT PRIMARY KEY, next_at REAL NOT NULL, failures INTEGER NOT NULL, blocked_until REAL NOT NULL)")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT next_at, failures, blocked_until FROM pacing WHERE service=?", (service,)).fetchone()
            value = list(row or (0.0, 0, 0.0))
            yield value
            connection.execute("INSERT OR REPLACE INTO pacing VALUES (?, ?, ?, ?)", (service, *value))
            connection.commit()
        except (OSError, sqlite3.Error):
            raise PolicyError("本机请求节流状态不可用，已停止网络请求；请检查私人目录权限后重试。") from None
        finally:
            if connection is not None:
                connection.close()

    def acquire(self, service: str) -> None:
        # Recheck after sleeping: another process may have received a 429 meanwhile.
        deadline = self.clock() + 10
        while True:
            with self.state(service) as state:
                now = self.clock()
                if state[2] > now:
                    raise CooldownError(state[2] - now)
                wait = max(0.0, state[0] - now)
                if not wait:
                    state[0] = now + INTERVALS[service]
                    return
                if now + wait > deadline:
                    raise CooldownError(wait)
            self.sleep(wait)

    def failure(self, service: str, *, server_wait: float = 0, authentication: bool = False) -> float:
        with self.state(service) as state:
            state[1] = min(state[1] + 1, 6)
            base = min(60.0, 2.0 ** state[1])
            delay = max(base + base * 0.2 * self.jitter(), server_wait, 60.0 if authentication else 0.0)
            state[2] = max(state[2], self.clock() + delay)
            return state[2] - self.clock()

    def success(self, service: str) -> None:
        with self.state(service) as state:
            # An older in-flight success must not erase a newer process's cooldown.
            if state[2] <= self.clock():
                state[1], state[2] = 0, 0.0

    def response(self, service: str, status: int, headers) -> None:
        if status in {401, 403}:
            self.failure(service, authentication=True, server_wait=retry_after(headers.get("retry-after"), self.clock()))
        elif status in {408, 429} or status >= 500:
            wait = max(30.0 if status == 429 else 0.0, retry_after(headers.get("retry-after"), self.clock()))
            delay = self.failure(service, server_wait=wait)
            raise CooldownError(delay)


limiter = RequestLimiter()


def bounded_request(client, method, url, *, maximum_bytes, error_type, **kwargs):
    """Read a bounded decoded body, without replaying or following redirects."""
    import httpx
    with client.stream(method, url, **kwargs) as response:
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            if len(body) + len(chunk) > maximum_bytes:
                raise error_type('响应超过读取上限；本次结果未确认，不能直接重发。')
            body.extend(chunk)
        # iter_bytes has already decompressed the response.
        headers={k:v for k,v in response.headers.items() if k.lower() not in {'content-encoding','content-length'}}
        return httpx.Response(response.status_code,headers=headers,content=bytes(body),request=response.request)


@contextmanager
def http_client(service: str, error_type, **kwargs):
    """Pace every explicit request/redirect, including streamed response bodies."""
    import httpx  # Mail setup remains usable with only Python's standard library.

    succeeded = False
    failure_recorded = False

    def on_request(request):
        limiter.acquire(service)

    def on_response(response):
        nonlocal succeeded, failure_recorded
        try:
            failure_recorded = response.status_code in {401, 403, 408, 429} or response.status_code >= 500
            limiter.response(service, response.status_code, response.headers)
            succeeded = 200 <= response.status_code < 300
        except PolicyError:
            response.close()
            raise

    try:
        with httpx.Client(**kwargs, event_hooks={"request": [on_request], "response": [on_response]}) as client:
            yield client
        if succeeded:
            limiter.success(service)
    except httpx.TransportError:
        try:
            delay = limiter.failure(service)
        except PolicyError as exc:
            raise policy_error(error_type, exc) from None
        raise policy_error(error_type, CooldownError(delay)) from None
    except PolicyError as exc:
        raise policy_error(error_type, exc) from None
    except error_type:
        # Includes a 200 login/denial page recognized by the adapter inside this context.
        if not failure_recorded:
            try:
                limiter.failure(service)
            except PolicyError as exc:
                raise policy_error(error_type, exc) from None
        raise
