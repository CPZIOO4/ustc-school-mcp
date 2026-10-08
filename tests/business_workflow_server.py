"""Test-only stdio server. All external traffic is intercepted by MockTransport."""
import sys
from pathlib import Path
import httpx
from school_mcp.mail.config import local_dir
from school_mcp import network
from unittest.mock import Mock
from test_business_workflows import Nan7Fake, ReviewFake

network.limiter=Mock()
mode=sys.argv[1]
if mode=='nan7':
    from school_mcp.nan7 import server
    from school_mcp.nan7.publishing import Publisher
    fake=Nan7Fake()
    if len(sys.argv)>2:fake.fail=sys.argv[2]
    server.Publisher=lambda:Publisher({'token':'synthetic-token','account':'synthetic-user'},httpx.MockTransport(fake.handle),local_dir())
elif mode=='icourse':
    from school_mcp.icourse import server
    from school_mcp.icourse.publishing import ReviewPublisher
    fake=ReviewFake()
    if len(sys.argv)>2:fake.fail=sys.argv[2]
    server.ReviewPublisher=lambda:ReviewPublisher({'user_id':'7','cookies':[{'domain':'icourse.club','name':'session','value':'synthetic-cookie'}]},httpx.MockTransport(fake.handle),local_dir())
elif mode=='jw':
    from school_mcp.jw import server
else:raise SystemExit('unknown synthetic server')
server.run()
