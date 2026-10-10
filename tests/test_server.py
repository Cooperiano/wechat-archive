from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from test_core import fixtures
from wechat_archive.parser import parse_snapshots
from wechat_archive.server import ArchiveServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name)
        parse_snapshots(fixtures(root/'in'),root/'out')
        self.server=ArchiveServer(root/'out/archive.sqlite');self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def request(self,path,auth=True,extra=None):
        headers={'Host':self.server.host}
        if auth:headers['Authorization']='Bearer '+self.server.token
        headers.update(extra or {})
        with closing(http.client.HTTPConnection('127.0.0.1',self.server.server_port)) as conn:
            conn.request('GET',path,headers=headers);response=conn.getresponse();body=response.read()
            return response.status,body,response.getheader('Content-Security-Policy')
    def test_auth_host_origin_and_arbitrary_paths(self):
        self.assertEqual(self.request('/api/rooms',False)[0],401)
        self.assertEqual(self.request('/api/rooms',extra={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/rooms',extra={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/../../archive.sqlite')[0],404)
        status,body,csp=self.request('/',False);self.assertEqual(status,200);self.assertIn("frame-ancestors 'none'",csp)
        self.assertNotIn(b'synthetic-account',body)
    def test_pagination_and_last_page_clamping(self):
        rows=[]
        for page,expected in [(0,100),(1,100),(2,1),(9,1)]:
            status,body,_=self.request('/api/messages?room=demo&page='+str(page));data=json.loads(body)
            self.assertEqual(status,200);self.assertEqual(len(data['rows']),expected)
            self.assertEqual(data['page'],min(page,2))
            if page<3:rows.extend(r['message_key'] for r in data['rows'])
        self.assertEqual(len(set(rows)),201)
    def test_search_date_and_invalid_query(self):
        self.assertEqual(json.loads(self.request('/api/messages?room=demo&q=%25')[1])['total'],0)
        self.assertEqual(json.loads(self.request('/api/messages?room=demo&from=1700000000&before=1700000001')[1])['total'],1)
        self.assertEqual(self.request('/api/messages?room=demo&page=-1')[0],400)
        self.assertEqual(self.request('/api/messages?room=demo&type=bad')[0],400)


if __name__=='__main__':unittest.main()
