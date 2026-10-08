import copy
import html
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from network_test_support import isolate_pacing as setUpModule
from school_mcp.workflow_store import PlanStore, WorkflowError
from school_mcp.jw.planner import Candidate, Constraints, Slot, generate
from school_mcp.jw.academic import schedule_slots, AcademicClient
from school_mcp.nan7.publishing import Publisher
from school_mcp.icourse.publishing import ReviewPublisher, RATINGS, form_snapshot


def candidate(identifier, *, day=1, weeks=None, start=1, end=2, credits=2, required=False, code=None, campus='east'):
    return Candidate(lesson_id=str(identifier),course_code=code or str(identifier),course_name='synthetic '+str(identifier),credits=credits,required=required,schedule_known=True,
                     slots=[Slot(weekday=day,start_period=start,end_period=end,weeks=weeks or [1,2,3],campus=campus)])


class PlannerTests(unittest.TestCase):
    def test_mixed_semesters_rejected(self):
        a,b=candidate(1),candidate(2,day=2);a.semester_id=1;b.semester_id=2
        self.assertEqual(generate([a,b],Constraints())['next_action'],'separate_semesters')

    def test_conflicts_and_alternatives(self):
        v=generate([candidate(1,required=True),candidate(2),candidate(3,day=2)],Constraints(min_credits=4,max_credits=4))
        self.assertEqual(v['plans'][0]['lesson_ids'],['1','3'])
        self.assertFalse(v['enrollment_performed'])

    def test_disjoint_weeks_not_conflict(self):
        v=generate([candidate(1,weeks=[1,3]),candidate(2,weeks=[2,4])],Constraints(min_credits=4,max_credits=4))
        self.assertEqual(len(v['plans']),1)

    def test_same_course_classes_mutually_exclusive(self):
        v=generate([candidate(1,code='A'),candidate(2,day=2,code='A')],Constraints(min_credits=4))
        self.assertEqual(v['state'],'no_feasible_plan')

    def test_unknown_required_not_silently_omitted(self):
        c=candidate(1,required=True);c.schedule_known=False
        self.assertEqual(generate([c],Constraints())['state'],'needs_input')

    def test_unavailable_and_free_day_ranking(self):
        c=Constraints(min_credits=2,max_credits=2,prefer_free_weekdays=[1])
        v=generate([candidate(1),candidate(2,day=2)],c)
        self.assertEqual(v['plans'][0]['lesson_ids'],['2'])
        c.unavailable=[Slot(weekday=2,start_period=1,end_period=3,weeks=[1])]
        self.assertEqual(generate([candidate(1),candidate(2,day=2)],c)['plans'][0]['lesson_ids'],['1'])

    def test_cross_campus_buffer(self):
        a,b=candidate(1),candidate(2,start=3,end=4,campus='west')
        self.assertEqual(generate([a,b],Constraints(min_credits=4))['state'],'no_feasible_plan')
        self.assertEqual(generate([a,b],Constraints(min_credits=4,cross_campus_gap_periods=0))['state'],'planned')

    def test_bounds_and_unknown_periods(self):
        with self.assertRaises(ValueError):Constraints(min_credits=4,max_credits=2)
        with self.assertRaises(ValueError):Slot(weekday=1,start_period=3,end_period=1,weeks=[1])
        self.assertEqual(generate([candidate(1),candidate(1)],Constraints())['state'],'needs_input')

    def test_schedule_parser_java_weekday_and_gaps(self):
        slots=schedule_slots('1~3,6~8周 A101 :2(1,3) 合成教师','east')
        self.assertEqual(slots[0]['weekday'],1)
        self.assertEqual(slots[0]['weeks'],[1,2,3,6,7,8])
        self.assertEqual([s['start_period'] for s in slots],[1,3])
        self.assertEqual(schedule_slots('待定'),[])
        self.assertEqual(schedule_slots('1~8单周 A101 :2(1,2)'),[])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name);self.store=PlanStore('nan7','synthetic-account',self.directory)

    def plan(self,content=None):return self.store.create('publish',content or {'title':'synthetic private'}, {},'synthetic-session',{'title':'synthetic'})

    def test_dedup_and_encryption(self):
        a=self.plan();b=self.plan()
        self.assertEqual(a['plan_id'],b['plan_id'])
        self.assertNotIn(b'synthetic private',self.store.path.read_bytes())

    def test_claim_concurrency_and_hash(self):
        p=self.plan()
        with self.assertRaises(WorkflowError):self.store.claim(p['plan_id'],'wrong')
        with ThreadPoolExecutor(max_workers=2) as pool:
            r=list(pool.map(lambda _:PlanStore('nan7','synthetic-account',self.directory).claim(p['plan_id'],p['content_sha256']),range(2)))
        self.assertEqual(sorted(r),[False,True])

    def test_account_isolation_target_lock_and_uncertain(self):
        p=self.plan();self.store.claim(p['plan_id'],p['content_sha256']);self.store.finish(p['plan_id'],'uncertain',{})
        other=self.plan({'title':'different'})
        with self.assertRaises(WorkflowError):self.store.claim(other['plan_id'],other['content_sha256'])
        with self.assertRaises(WorkflowError):PlanStore('nan7','other',self.directory).get(p['plan_id'])

    def test_expiry_and_digest_preview(self):
        from unittest.mock import patch
        from datetime import datetime,timezone,timedelta
        p=self.plan()
        with patch('school_mcp.workflow_store.datetime') as clock:
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(hours=3)
            clock.fromisoformat=datetime.fromisoformat
            self.assertEqual(self.store.view(p['plan_id'])['state'],'expired')
            replacement=self.plan()
            self.assertNotEqual(replacement['plan_id'],p['plan_id'])
            self.assertEqual(replacement['state'],'ready')
            self.assertFalse(self.store.claim(p['plan_id'],p['content_sha256']))


class Nan7Fake:
    def __init__(self):self.posts=[];self.offer=None;self.fail=None;self.media=[];self.owner='7'
    def handle(self,r):
        path=r.url.path
        if path.endswith('/profile/me'):return httpx.Response(200,json={'id':self.owner})
        if path.endswith('/offer/search'):
            assert type(json.loads(r.content)['owner']) is int
            return httpx.Response(200,json={'offers':[]})
        if path.endswith('/media/new'):
            self.media.append(r.content)
            if self.fail=='upload':raise httpx.ReadTimeout('synthetic',request=r)
            return httpx.Response(200,json={'id':'image'+str(len(self.media))})
        if path.endswith('/offer/new'):
            v=json.loads(r.content);self.posts.append(v)
            self.offer={**v,'id':'11','owner':{'id':self.owner},'editable':True,'valid':True,'images':[{'id':x} for x in v['images']]}
            if self.fail=='lost':raise httpx.ReadTimeout('synthetic',request=r)
            return httpx.Response(200,json={'id':'11'})
        if path.endswith('/offer/get'):
            if self.fail=='readback':raise httpx.ReadTimeout('synthetic',request=r)
            return httpx.Response(200,json=self.offer)
        raise AssertionError(path)


class Nan7PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name);self.fake=Nan7Fake()
        self.api=Publisher({'token':'synthetic-token','account':'synthetic-user'},httpx.MockTransport(self.fake.handle),self.directory)

    def prepare(self,images=None):return self.api.prepare('合成图书','合成测试商品描述','12.50','synthetic@example.com',1,images=images)

    def test_full_pipeline_once_and_bytes_frozen(self):
        image=self.directory/'test.png';image.write_bytes(b'\x89PNG\r\n\x1a\nsynthetic')
        p=self.prepare([str(image)]);image.write_bytes(b'changed')
        r=self.api.publish(p['plan_id'],p['content_sha256'])
        self.assertEqual(r['state'],'verified',r)
        self.api.publish(p['plan_id'],p['content_sha256']);self.assertEqual(len(self.fake.posts),1)
        self.assertIn(b'synthetic',self.fake.media[0]);self.assertNotIn(b'changed',self.fake.media[0])

    def test_missing_and_price(self):
        self.assertEqual(self.api.prepare()['state'],'needs_input')
        for price in ['nan','-1','1.234','Infinity']:
            with self.assertRaises(WorkflowError):self.api.prepare('a','b',price,'c',0)

    def test_preflight_identity_change_no_write(self):
        p=self.prepare();self.fake.owner='8'
        self.assertEqual(self.api.publish(p['plan_id'],p['content_sha256'])['state'],'stopped')
        self.assertFalse(self.fake.posts)

    def test_lost_response_not_replayed(self):
        p=self.prepare();self.fake.fail='lost'
        self.assertEqual(self.api.publish(p['plan_id'],p['content_sha256'])['state'],'uncertain')
        self.api.publish(p['plan_id'],p['content_sha256']);self.assertEqual(len(self.fake.posts),1)

    def test_readback_failure_later_verifies(self):
        p=self.prepare();self.fake.fail='readback'
        self.assertEqual(self.api.publish(p['plan_id'],p['content_sha256'])['state'],'uncertain')
        self.fake.fail=None
        self.assertEqual(self.api.status(p['plan_id'],True)['state'],'verified')
        self.assertEqual(len(self.fake.posts),1)

    def test_ambiguous_image_upload_stops_before_publication(self):
        image=self.directory/'test.jpg';image.write_bytes(b'\xff\xd8\xffsynthetic')
        p=self.prepare([str(image)]);self.fake.fail='upload'
        self.assertEqual(self.api.publish(p['plan_id'],p['content_sha256'])['state'],'uncertain')
        self.assertFalse(self.fake.posts)
        self.api.publish(p['plan_id'],p['content_sha256']);self.assertEqual(len(self.fake.media),1)


def review_html(payload=None,user='7',course=1):
    p=payload or {};existing=bool(payload)
    inputs=''.join(f'<input type="radio" name="{k}" value="{v}" '+('checked' if p.get(k)==str(v) else '')+'>' for k in RATINGS[:-1] for v in range(1,4))
    rate=float(p.get('rate','0'))/2
    return f'''<a href="/user/{user}">个人主页</a><div class="inline-h3">{'编辑' if existing else ''}点评 • 合成课程</div>
    <form id="review-form" method="post" action="/course/{course}/review/">
    <input name="csrf_token" value="synthetic-csrf"><select name="term"><option value="202601" selected>合成学期</option></select>
    {inputs}<input name="rate" value="{rate}"><textarea name="content">{html.escape(p.get('content',''))}</textarea>
    <input type="checkbox" name="is_anonymous" {'checked' if p.get('is_anonymous') else ''}>
    <input type="checkbox" name="only_visible_to_student" {'checked' if p.get('only_visible_to_student') else ''}></form>'''


class ReviewFake:
    def __init__(self):self.payload=None;self.posts=0;self.fail=None;self.user='7'
    def handle(self,r):
        if r.method=='GET':return httpx.Response(200,text=review_html(self.payload,self.user),headers={'content-type':'text/html'})
        self.posts+=1;self.payload={k:v[0] for k,v in parse_qs(r.content.decode()).items()}
        if self.fail=='lost':raise httpx.ReadTimeout('synthetic',request=r)
        return httpx.Response(200,json={'ok':True,'next_url':'https://icourse.club/course/1/#review-11'})


class ReviewPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.fake=ReviewFake();self.session={'user_id':'7','cookies':[{'domain':'icourse.club','name':'session','value':'synthetic-cookie'}]}
        self.api=ReviewPublisher(self.session,httpx.MockTransport(self.fake.handle),Path(self.temp.name))

    def prepare(self,ratings=None):return self.api.prepare(1,'202601','这是合成测试点评正文\n不代表任何真实修课经历。',ratings,True,True)

    def test_publish_and_readback_and_no_duplicate(self):
        p=self.prepare({k:2 for k in RATINGS});r=self.api.publish(p['plan_id'],p['content_sha256'])
        self.assertEqual(r['state'],'verified',r)
        self.api.publish(p['plan_id'],p['content_sha256']);self.assertEqual(self.fake.posts,1)
        self.assertEqual(self.prepare()['state'],'existing_review')

    def test_existing_review_appeared_before_publish(self):
        p=self.prepare();self.fake.payload={'content':'someone edited','term':'202601'}
        self.assertEqual(self.api.publish(p['plan_id'],p['content_sha256'])['state'],'stopped')
        self.assertEqual(self.fake.posts,0)

    def test_lost_response_verified_without_replay(self):
        p=self.prepare();self.fake.fail='lost'
        self.assertEqual(self.api.publish(p['plan_id'],p['content_sha256'])['state'],'uncertain')
        self.assertEqual(self.api.status(p['plan_id'],True)['state'],'verified')
        self.assertEqual(self.fake.posts,1)

    def test_partial_ratings_term_and_short_content_rejected(self):
        with self.assertRaises(WorkflowError):self.prepare({'rate':8})
        with self.assertRaises(WorkflowError):self.api.prepare(1,'202501','合成测试点评内容至少十字。')
        with self.assertRaises(WorkflowError):self.api.prepare(1,'202601','短')

    def test_new_account_cannot_publish_old_plan(self):
        p=self.prepare();self.fake.user='8'
        self.assertEqual(self.api.publish(p['plan_id'],p['content_sha256'])['state'],'stopped')
        self.assertEqual(self.fake.posts,0)



class AcademicTests(unittest.TestCase):
    def client(self, *, page_mismatch=False, bad_schedule=False):
        from school_mcp.jw.client import JWClient
        def handle(r):
            path=r.url.path
            if path=='/for-std/lesson-search':return httpx.Response(302,headers={'location':'/for-std/lesson-search/index/7'})
            if path=='/for-std/lesson-search/index/7':return httpx.Response(200,headers={'content-type':'text/html'},text='<select id="semester"><option value="1">合成学期</option></select><select id="weekIndexs"><option value="2">星期一</option></select>')
            if path=='/for-std/lesson-search/semester/1/search/7':
                assert r.url.params['queryPage__']=='2,3'
                assert 'pageNo' not in r.url.params
                return httpx.Response(200,json={'data':[{'id':1,'code':'A.01','course':{'nameZh':'synthetic','code':'A','credits':2},'scheduleText':{'dateTimePlacePersonText':{'textZh':'待定' if bad_schedule else '1~2周 A :2(1,2) test'}}}], '_page_':{'currentPage':1 if page_mismatch else 2,'rowsPerPage':3,'totalPages':2,'totalRows':4}})
            if path=='/for-std/course-select':return httpx.Response(200,headers={'content-type':'text/html'},text='<script>studentId:7,bizTypeId:2,fetchOpenTurns:"test"</script>')
            if path=='/ws/for-std/course-select/open-turns':
                assert r.method=='POST' and parse_qs(r.content.decode())=={'studentId':['7'],'bizTypeId':['2']}
                return httpx.Response(200,json=[])
            raise AssertionError(path)
        return AcademicClient(JWClient(cookies=[],user_agent='synthetic',transport=httpx.MockTransport(handle)))

    def test_offerings_pagination_and_schedule(self):
        r=self.client().offerings(1,'synthetic',2,3)
        self.assertEqual(r['offerings'][0]['planner_candidate']['slots'][0]['weekday'],1)
        self.assertIsNone(r['next_page'])

    def test_mismatched_page_and_unknown_schedule(self):
        with self.assertRaises(WorkflowError):self.client(page_mismatch=True).offerings(1,'synthetic',2,3)
        r=self.client(bad_schedule=True).offerings(1,'synthetic',2,3)
        self.assertFalse(r['offerings'][0]['schedule_known'])

    def test_window_and_arbitrary_write_blocked(self):
        c=self.client();self.assertEqual(c.window()['state'],'window_closed')
        with self.assertRaises(WorkflowError):c.request('/ws/for-std/course-select/add',method='POST')

    def test_drop_unselected_and_window_blockers(self):
        from unittest.mock import Mock
        c=self.client();c.jw.courses=Mock(return_value={'semester':{'id':1},'courses':[]})
        result=c.prepare_change('drop',1,2)
        self.assertFalse(result['can_execute'])
        self.assertIn('target_not_in_selected_courses',result['blockers'])
        self.assertIn('selection_window_closed',result['blockers'])


class ResponseBoundsTests(unittest.TestCase):
    def test_decoded_limit_and_no_replay(self):
        import gzip
        from school_mcp.network import bounded_request
        calls=[]
        def handle(r):
            calls.append(r)
            return httpx.Response(200,content=gzip.compress(b'x'*200),headers={'content-encoding':'gzip'})
        with httpx.Client(transport=httpx.MockTransport(handle)) as c:
            with self.assertRaises(WorkflowError):bounded_request(c,'GET','https://example.com',maximum_bytes=100,error_type=WorkflowError)
        self.assertEqual(len(calls),1)
        with httpx.Client(transport=httpx.MockTransport(handle)) as c:
            r=bounded_request(c,'GET','https://example.com',maximum_bytes=300,error_type=WorkflowError)
            self.assertEqual(r.content,b'x'*200)


class BusinessCLITests(unittest.TestCase):
    def test_login_command_dispatches_to_login_not_server(self):
        from unittest.mock import patch
        from school_mcp.__main__ import main
        with patch('sys.argv',['school-mcp','icourse-login']), patch('school_mcp.icourse.login.run') as login:
            main()
            login.assert_called_once_with()

    def test_unsupported_visible_login_is_explicit(self):
        from unittest.mock import patch
        from school_mcp.__main__ import main
        import io
        with patch('sys.argv',['school-mcp','icourse-login','--headed']), patch('sys.stderr',io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code,2)



if __name__=='__main__':unittest.main()
