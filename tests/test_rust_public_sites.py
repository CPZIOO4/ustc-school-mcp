"""Compare real Rust HTML extraction with established public adapter contracts."""
import os
from pathlib import Path
import unittest
import tempfile
from urllib.parse import quote
from unittest.mock import patch

import httpx
from school_mcp.desktop import native_core
from school_mcp.desktop.model import ModelError
from school_mcp.icourse.client import ICourseClient
from school_mcp.teach.client import parse_article, parse_listing, BASE_URL
from school_mcp.teach.client import TeachClient, TeachError
from school_mcp import public_native
from test_teach import LIST, ARTICLE
from test_icourse import CARD, html
from network_test_support import isolate_pacing as setUpModule

CORE = Path(os.environ.get('SCHOOL_TEST_CORE', Path(__file__).resolve().parents[1] / 'native/core/target/debug/school-core.exe'))


@unittest.skipUnless(CORE.is_file(), 'build native/core first')
class RustPublicSitesTests(unittest.TestCase):
    def setUp(self):
        native_core.shutdown()
        self.env = patch.dict(os.environ, {'SCHOOL_NATIVE_CORE': str(CORE)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(native_core.shutdown)

    def parse(self, action, params, body):
        return native_core.call('public_parse', {'action': action, 'params': params, 'html': body})

    def parity(self, action, params, body, method, *args, **kwargs):
        with patch.object(native_core, 'enabled', return_value=False):
            client = ICourseClient(httpx.MockTransport(lambda r: httpx.Response(200, text=body, headers={'content-type': 'text/html'})))
            expected = getattr(client, method)(*args, **kwargs)
        actual = self.parse(action, params, body)
        expected.pop('retrieved_at', None)
        actual.pop('retrieved_at', None)
        def canonical(value):
            if isinstance(value, dict):
                return {k: canonical(v) for k,v in value.items()}
            if isinstance(value, list):
                return [canonical(v) for v in value]
            if isinstance(value, str) and value.startswith(('https://','http://')):
                return quote(value, safe=":/?#[]@!$&'()*+,;=%")
            return value
        self.assertEqual(canonical(actual), canonical(expected))

    def test_teach_list_article_restricted_and_empty(self):
        self.assertEqual(self.parse('teach_list', {}, LIST), parse_listing(LIST, BASE_URL+'/category/notice', 1))
        self.assertEqual(self.parse('teach_read', {'url':'/notice/123.html'}, ARTICLE), parse_article(ARTICLE, BASE_URL+'/notice/123.html'))
        for body in ['<h1>受限资源</h1>', '<h1>搜索</h1><main><div class="list">未找到相关文章</div></main>']:
            self.assertEqual(self.parse('teach_list', {}, body), parse_listing(body, BASE_URL+'/category/notice', 1))
        with self.assertRaisesRegex(ModelError, '页码'):
            self.parse('teach_list', {'page':2}, LIST)

    def test_course_cards_paging_search_and_teacher(self):
        body=html(CARD+'共 13 门课（当前第 2 页）<ul class="pagination"><li><a aria-label="Next" href="/search/?q=数学&amp;page=3">后</a></li></ul>')
        self.parity('icourse_search', {'query':'数学 & page=9','page':2}, body, 'search_courses', '数学 & page=9',2)
        self.parity('icourse_list', {}, html(CARD), 'list_courses')
        self.parity('icourse_teacher', {'teacher_id':4}, html('<h3 class="blue">教师</h3>'+CARD), 'teacher',4)
        self.parity('icourse_check', {}, html(CARD), 'check')

    def test_course_detail_excludes_review_metadata(self):
        body=html('<span id="review-anchor"></span><span class="h4">8.1</span>(2人评价)'
                  '<h3><a href="/teacher/4/">教师</a></h3><span class="align-bottom desktop">2026秋 课程号：AB1</span>'
                  '<table><tr><td><strong>学分：</strong>3.0</td></tr></table><div id="course-intro">简介</div>'
                  '<div class="review"><ul><li>课程难度：不可混入</li></ul><table><tr><td><strong>私有：</strong>bad</td></tr></table></div>')
        self.parity('icourse_course', {'course_id':12}, body, 'get_course',12)

    def test_review_unicode_truncation_and_links(self):
        body=html('''<div><span id="review-anchor">点评</span>2 条点评</div>
        <div class="review" id="review-8"><div class="blue"><span class="px16">用户</span>
        <span class="glyphicon-star"></span><span class="left-pd-md">2026秋</span></div>
        <div id="review-content-8">好😀好测试文字<script>不执行</script><a href="javascript:alert(1)">bad</a><a href="#" data-target="#signin">附件</a><a href="/course/1/">引用</a></div>
        <div class="grey" id="review-8"><span class="localtime">原始时间</span></div>
        <div class="review-comments"><div class="solid"><a>回复者</a><span>公开回复</span><span class="localtime">日期</span></div></div></div>''')
        self.parity('icourse_reviews', {'course_id':12,'page_size':1,'max_chars':5},body,'course_reviews',12,page_size=1,max_chars=5)
        self.parity('icourse_reviews', {'course_id':12,'page':9},body,'course_reviews',12,page=9)

    def test_review_search_and_explicit_empty(self):
        body=html('<div><a href="/course/12/#review-8">课程</a><span class="blue">作者</span><span class="localtime">日期</span><p class="review-content">节选 &gt;&gt;更多</p></div>共 1 个点评')
        self.parity('icourse_search_reviews',{'query':'课'},body,'search_reviews','课')
        self.parity('icourse_search',{'query':'无'},html('共 0 门课'),'search_courses','无')
        empty='<title>您的搜索「无匹配词」没有匹配到任何点评 - USTC评课社区</title>'
        self.parity('icourse_search_reviews',{'query':'无'},empty,'search_reviews','无')
        with self.assertRaisesRegex(ModelError,'回退'):
            self.parse('icourse_search',{'query':'无'},empty)

    def test_invalid_inputs_and_changed_layout_stop(self):
        for action,params in [('teach_read',{'url':'https://evil.test/notice/1.html'}),('teach_read',{'url':'/notice/%2e%2e/notice/1.html'}),('teach_list',{'page':True}),('teach_search',{'keyword':'\n'}),('icourse_list',{'sort_by':'mutate'}),('icourse_reviews',{'course_id':1,'max_chars':0}),('icourse_course',{'course_id':-1})]:
            with self.subTest(action=action,params=params),self.assertRaises(ModelError):
                self.parse(action,params,'')
        for action,params in [('teach_list',{}),('icourse_list',{}),('icourse_course',{'course_id':1})]:
            with self.subTest(action=action),self.assertRaises(ModelError):
                self.parse(action,params,html('<div>changed</div>'))

    def test_native_adapter_rejects_bad_destination_without_python_http(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR':tmp}), patch('httpx.Client',side_effect=AssertionError('Python HTTP forbidden')):
            with self.assertRaises(TeachError) as caught:
                TeachClient().read_article('https://foreign.invalid/notice/1.html')
            self.assertEqual(caught.exception.code,'invalid_arguments')

    def test_pacing_wait_rechecks_but_server_failure_never_replays(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'SCHOOL_MCP_LOCAL_DIR':tmp}), patch('school_mcp.public_native.time.sleep') as sleep:
            with patch.object(native_core,'call',side_effect=[{'state':'wait','retry_after_seconds':0.01},{'state':'parsed','result':{'count':1}}]) as call:
                self.assertEqual(public_native.read('teach_list',{},TeachError),{'count':1})
                self.assertEqual(call.call_count,2)
                sleep.assert_called_once_with(0.01)
            for response in [{'state':'cooldown','retry_after_seconds':30},{'state':'error','code':'unavailable','message':'failed'}]:
                with patch.object(native_core,'call',return_value=response) as call,self.assertRaises(TeachError):
                    public_native.read('teach_list',{},TeachError)
                self.assertEqual(call.call_count,1)

    def test_directory_failure_has_safe_fixed_recovery(self):
        with patch('school_mcp.public_native.local_dir',side_effect=PermissionError('PRIVATE-PATH-CANARY')):
            with self.assertRaises(TeachError) as caught:
                public_native.read('teach_list',{},TeachError)
        self.assertEqual(caught.exception.code,'local_policy_unavailable')
        self.assertNotIn('PRIVATE-PATH-CANARY',str(caught.exception))
