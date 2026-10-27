import unittest
from unittest.mock import patch
from contextlib import contextmanager
from school_mcp.compute import client
from school_mcp.service_errors import ServiceError

class ComputeTests(unittest.TestCase):
    def test_owner_boundary(self):
        client.check_owner('https://107.ustc.edu.cn/api/job/getRunningJobs?userId=pb123&cluster=training', 'PB123')
        for url in ['https://107.ustc.edu.cn/api/job/getRunningJobs?userId=pb456',
                    'https://example.org/api/job/getRunningJobs?userId=PB123',
                    'http://107.ustc.edu.cn/api/job/getRunningJobs?userId=PB123',
                    'https://107.ustc.edu.cn/api/job/getRunningJobs?userId=PB123&userId=PB456']:
            with self.subTest(url=url), self.assertRaises(ServiceError):
                client.check_owner(url, 'PB123')

    def test_latest_order_filter_and_no_private_path(self):
        class S:
            account = 'synthetic'
            running_jobs = []
            def get(self, path, params):
                self_test.assertEqual(path, '/api/job/getAllJobs')
                self_test.assertEqual(params['cluster'], 'training')
                class R:
                    def json(self):
                        return {'results': [
                            {'jobId': 2, 'name': 'second', 'submitTime': '2026-10-21', 'state': 'FAILED'},
                            {'jobId': 1, 'name': 'first', 'submitTime': '2026-10-20'},
                            {'jobId': 3, 'name': 'third', 'submitTime': '2026-10-22', 'state': 'COMPLETED', 'workingDirectory': '/private/research'}]}
                return R()
        self_test = self
        @contextmanager
        def connected(): yield S()
        with patch.object(client, 'connected', connected):
            value = client.jobs(limit=1)
            self.assertEqual(value['jobs'][0]['jobId'], 3)
            self.assertTrue(value['truncated'])
            self.assertNotIn('workingDirectory', value['jobs'][0])
            self.assertEqual(client.jobs(job_id=2)['jobs'][0]['state'], 'FAILED')
            self.assertEqual(client.jobs(job_id=99)['jobs'], [])
            self.assertEqual(client.jobs(running_only=True)['total'], 0)

    def test_invalid_arguments_stop_before_network(self):
        with patch.object(client, 'connected') as network:
            for args in [{'days':0},{'days':32},{'limit':0},{'limit':51},{'job_id':True},{'job_id':-1},{'running_only':'true'}]:
                with self.subTest(args=args), self.assertRaises(ServiceError): client.jobs(**args)
            network.assert_not_called()

    def test_date_range_uses_china_days(self):
        a,b=client.date_range(1)
        self.assertIn('T00:00:00+08:00',a)
        self.assertIn('T23:59:59.999000+08:00',b)

if __name__ == '__main__': unittest.main()
