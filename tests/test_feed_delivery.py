import base64
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from collector import parse
from publishing import PINNED_FEED, validate_outputs, write_compact_feed, write_pinned_feed
from serve_feed import handler_for


class FeedDeliveryTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.docs=Path(folder.name)
        self.now=int(time.time())
        self.urls=[
            'vless://fixture@edge.example:443?security=tls&type=ws&host=transport.example#current',
            'trojan://fixture@old.example:443?security=tls#expired',
            'trojan://fixture@private.example:443?security=tls#private-ip',
        ]
        candidates=[parse(url) for url in self.urls]
        self.rows=[dict(id=c.key,name=name,rank=rank,endpoint_ip=ip,last_success_epoch=self.now-120,
                        expires_epoch=expiry,video_bytes=131072)
                   for rank,(c,name,ip,expiry) in enumerate(zip(candidates,('current','expired','private-ip'),
                    ('2606:4700:4700::1111','1.1.1.1','127.0.0.1'),(self.now+1000,self.now,self.now+1000)),1)]
        self.outbounds=[dict(c.outbound,tag=row['name']) for c,row in zip(candidates,self.rows)]
        self.report=dict(nodes=self.rows,published=len(self.rows),generated_epoch=self.now-60,health='ok')
        data=('\n'.join(self.urls)+'\n').encode()
        (self.docs/'sub.txt').write_bytes(data)
        (self.docs/'sub.b64').write_bytes(base64.b64encode(data))
        (self.docs/'xray.json').write_text(json.dumps({'outbounds':self.outbounds}),encoding='utf-8')
        write_compact_feed(self.docs,self.urls,self.rows,self.report)
        write_pinned_feed(self.docs,self.outbounds,self.rows,self.report)
        self.save_report()

    def save_report(self):
        (self.docs/'status.json').write_text(json.dumps(self.report),encoding='utf-8')

    def test_pinned_export_changes_only_the_tested_address(self):
        self.assertEqual(validate_outputs(self.docs)['pinned_ids'],[row['id'] for row in self.rows[:2]])
        pinned_path=self.docs/PINNED_FEED
        pinned=json.loads(pinned_path.read_text())
        first=pinned['outbounds'][0]
        self.assertEqual(first['settings']['vnext'][0]['address'],'2606:4700:4700::1111')
        self.assertEqual(first['streamSettings']['tlsSettings']['serverName'],'edge.example')
        self.assertEqual(first['streamSettings']['wsSettings']['headers']['Host'],'transport.example')
        first['streamSettings']['tlsSettings']['serverName']='changed.example'
        pinned_path.write_text(json.dumps(pinned))
        with self.assertRaisesRegex(ValueError,'Pinned Xray export'):
            validate_outputs(self.docs)

    def test_http_filters_at_expiry_and_rejects_mixed_snapshots(self):
        with ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.docs)) as server:
            worker=threading.Thread(target=server.serve_forever,daemon=True)
            worker.start()
            try:
                def get(path, method='GET'):
                    connection=HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                    try:
                        connection.request(method,path)
                        response=connection.getresponse()
                        return response.status,response.getheader('Cache-Control'),response.read()
                    finally:
                        connection.close()

                status,cache,plain=get('/sub.txt')
                self.assertEqual((status,cache),(200,'no-store, max-age=0'))
                self.assertEqual(plain.decode().splitlines(),[self.urls[0],self.urls[2]])
                self.assertEqual(base64.b64decode(get('/sub.b64')[2]),plain)
                self.assertEqual(base64.b64decode(get('/sub-compact.b64')[2]),plain)
                self.assertEqual(len(json.loads(get('/xray.json')[2])['outbounds']),2)
                self.assertEqual(len(json.loads(get('/'+PINNED_FEED)[2])['outbounds']),1)
                self.assertEqual(json.loads(get('/status.json')[2])['pinned_ids'],[self.rows[0]['id']])
                self.assertEqual(get('/sub.txt','HEAD')[2],b'')
                self.assertEqual(get('/.local/history.json')[0],404)
                for row in self.rows:
                    row['expires_epoch']=self.now
                self.save_report()
                self.assertEqual(get('/sub.txt')[2],b'')
                self.assertEqual(json.loads(get('/xray.json')[2]),{'outbounds':[]})
                self.assertEqual(json.loads(get('/status.json')[2])['health'],'expired_or_empty')
                (self.docs/'sub.b64').write_bytes(b'invalid')
                self.assertEqual(get('/sub.txt')[0],503)
            finally:
                server.shutdown()
                worker.join()
