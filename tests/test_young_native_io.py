"""Native boundary tests: no real login, browser, task registration, or writes."""
import unittest
from unittest.mock import patch

from school_mcp.young import client, native, registration, scheduler


class NativeYoungBridgeTests(unittest.TestCase):
    def setUp(self):
        enabled=patch.object(native,'enabled',return_value=True)
        enabled.start();self.addCleanup(enabled.stop)

    def test_read_and_run_use_fixed_native_actions_without_python_browser(self):
        with patch.object(native,'io',return_value={'synthetic':True}) as call,patch.object(registration,'RegistrationBrowser') as browser,patch.object(client,'sync_playwright') as playwright:
            self.assertEqual(client.YoungClient().home(40),{'synthetic':True})
            call.assert_called_with('home',max_chars=40)
            registration.find_projects('测试');call.assert_called_with('find',keyword='测试')
            registration.list_projects(2);call.assert_called_with('list',page=2)
            registration.read_project('event',2,'child');call.assert_called_with('detail',item_id='event',page=2,child_id='child')
            registration.run_job('a'*32);call.assert_called_with('run',job_id='a'*32)
        browser.assert_not_called();playwright.assert_not_called()

    def test_scheduler_has_no_python_process_or_powershell_on_native_path(self):
        with patch.object(native,'io',return_value={'native':True}) as call,patch.object(scheduler,'powershell') as powershell,patch.object(scheduler,'start_waiter') as waiter:
            scheduler.install_schedule('start','due',job_id='a'*32)
            call.assert_called_with('install_schedule',monitor_from='start',opens_at='due',job_id='a'*32,schedule_id=None)
            scheduler.inspect_schedule('b'*32);call.assert_called_with('inspect_schedule',schedule_id='b'*32)
            scheduler.cancel_schedule('b'*32);call.assert_called_with('cancel_schedule',schedule_id='b'*32)
        powershell.assert_not_called();waiter.assert_not_called()

    def test_native_error_does_not_fall_back_or_replay(self):
        from school_mcp.young.session import YoungError
        with patch.object(native,'io',side_effect=YoungError('synthetic failure')) as call,patch.object(registration,'RegistrationBrowser') as browser:
            with self.assertRaises(YoungError):registration.run_job('a'*32)
        call.assert_called_once();browser.assert_not_called()

    def test_invalid_query_is_rejected_before_native_io(self):
        with patch.object(native,'io') as call:
            for value in ['',None,True]:
                with self.assertRaises(registration.YoungError):registration.find_projects(value)
            with self.assertRaises(registration.YoungError):registration.list_projects(True)
            with self.assertRaises(registration.YoungError):registration.read_project('../event')
        call.assert_not_called()


if __name__=='__main__':unittest.main()
