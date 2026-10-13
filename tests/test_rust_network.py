"""Real native policy bridge acceptance with synthetic clocks and private temp DBs."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from school_mcp import network
from school_mcp.desktop import native_core

ROOT = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get('SCHOOL_TEST_CORE', ROOT / 'native/core/target/debug/school-core.exe'))


@unittest.skipUnless(CORE.is_file(), 'build native/core first')
class RustNetworkTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(native_core.shutdown)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'request-policy.sqlite3'
        self.now = 1000.0
        self.waits = []
        self.policy = network.RequestLimiter(self.path, clock=lambda: self.now,
            sleep=self.sleep, jitter=lambda: 0)

    def sleep(self, duration):
        self.waits.append(duration)
        self.now += duration

    def test_native_owns_database_and_backoff_without_python_sqlite(self):
        with patch.object(network.sqlite3, 'connect', side_effect=AssertionError('Python SQLite executed')):
            self.policy.acquire('teach')
            self.policy.acquire('teach')
            self.assertEqual(self.waits, [1])
            self.assertEqual(self.policy.failure('teach', server_wait=120), 120)
            self.policy.success('teach')
            with self.assertRaises(network.CooldownError) as caught:
                self.policy.acquire('teach')
            self.assertEqual(caught.exception.retry_after_seconds, 120)
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute('SELECT next_at, failures, blocked_until FROM pacing WHERE service=?', ('teach',)).fetchone()
        self.assertEqual(row, (1002.0, 1, 1121.0))

    def test_legacy_database_and_native_updates_are_bidirectionally_compatible(self):
        with patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': ''}):
            self.policy.acquire('bb')
            self.policy.failure('bb', server_wait=60)
        with self.assertRaises(network.CooldownError) as caught:
            self.policy.acquire('bb')
        self.assertEqual(caught.exception.retry_after_seconds, 60)
        self.now += 60
        self.policy.success('bb')
        self.policy.failure('bb', server_wait=3600)
        with patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': ''}):
            with self.assertRaises(network.CooldownError) as caught:
                self.policy.acquire('bb')
            self.assertEqual(caught.exception.retry_after_seconds, 3600)

    def test_sleep_rechecks_a_concurrent_cooldown(self):
        self.policy.acquire('teach')
        def slept(duration):
            self.sleep(duration)
            other = network.RequestLimiter(self.path, clock=lambda: self.now, jitter=lambda: 0)
            other.failure('teach', server_wait=90)
        self.policy.sleep = slept
        with self.assertRaises(network.CooldownError) as caught:
            self.policy.acquire('teach')
        self.assertEqual(caught.exception.retry_after_seconds, 90)
        self.assertEqual(self.waits, [1])

    def test_native_failure_never_falls_back_to_unpaced_python(self):
        with patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(self.path.parent/'missing-core.exe')}):
            with patch.object(network.sqlite3, 'connect', side_effect=AssertionError('Fallback attempted')):
                with self.assertRaises(network.PolicyError):
                    self.policy.acquire('teach')
        self.assertFalse(self.path.exists())

    def test_retry_after_date_and_huge_wait_are_not_shortened(self):
        self.assertEqual(network.retry_after('Thu, 01 Jan 1970 01:00:00 GMT', 1000), 2600)
        self.assertEqual(network.retry_after('9'*500, 1000), 1e300)
        with self.assertRaises(network.CooldownError) as caught:
            self.policy.response('icourse', 429, {'retry-after': '3600'})
        self.assertEqual(caught.exception.retry_after_seconds, 3600)


if __name__ == '__main__':
    unittest.main()
