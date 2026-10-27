import unittest
from unittest.mock import patch
from school_mcp.compute import terminal as t
from school_mcp.service_errors import ServiceError

class TerminalTests(unittest.TestCase):
    def test_only_fixed_commands(self):
        self.assertIn('squeue',t.command())
        self.assertIn('sacct',t.command(123))
        for value in ['1; id',True,0,-1,1.5]:
            with self.subTest(value=value),self.assertRaises(ServiceError):t.command(value)
    def test_official_terminal_boundary(self):
        self.assertEqual(t.shell_url('https://107.ustc.edu.cn/shell/training/11.11.10.202'),'https://107.ustc.edu.cn/shell/training/11.11.10.202')
        for url in ['http://107.ustc.edu.cn/shell/training/1.2.3.4','https://example.org/shell/training/1.2.3.4','https://107.ustc.edu.cn/shell/training/1.2.3.4?token=x','https://107.ustc.edu.cn/shell/other/1.2.3.4']:
            with self.subTest(url=url),self.assertRaises(ServiceError):t.shell_url(url)
    def test_echo_is_not_completion(self):
        self.assertIsNone(t.completed('printf "\\n__DONE:%s\\n" "$?"','__DONE'))
        self.assertEqual(t.completed('output\n__DONE:0\nuser@node:~$ ','__DONE'),0)
        self.assertEqual(t.completed('__DONE:124\n','__DONE'),124)
    def test_reject_before_connection(self):
        with patch.object(t,'connected') as network:
            with self.assertRaises(ServiceError):t.inspect('$(pwd)')
            network.assert_not_called()
