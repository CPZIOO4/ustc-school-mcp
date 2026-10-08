"""Test-only stdio server: synthetic IMAP, real MCP and local DPAPI, no SMTP."""
from unittest.mock import Mock, patch
from mail_workflow_support import MemoryIMAP
from school_mcp.server import run


if __name__ == '__main__':
    with patch('school_mcp.mail.client.imaplib.IMAP4_SSL', return_value=MemoryIMAP()), \
         patch('school_mcp.mail.client.load_password', return_value='synthetic-password'), \
         patch('school_mcp.mail.outbox.smtplib.SMTP_SSL', side_effect=AssertionError('SMTP forbidden in this test')), \
         patch('school_mcp.network.limiter', Mock()):
        run()
