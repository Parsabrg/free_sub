"""Actual Xray/Trojan/TLS/HTTPS tests; pre-push verification makes these mandatory."""
import base64
from contextlib import contextmanager
from functools import partial
import http.server
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from collector import collect, parse
from geolocation import CountryInfo
from main import arguments, run_cycle
from publishing import validate_outputs
from state import read_state
from xray_test import allocate_port, check_ready, run_probe

BINARY=os.environ.get('XRAY_E2E_BINARY')


@unittest.skipUnless(BINARY,'Set XRAY_E2E_BINARY or run verify.py for real Xray tests')
class RealXrayEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='xray-e2e-')
        cls.root=Path(cls.temp.name)
        # Xray itself generates ephemeral CA/server material. No OpenSSL or pip dependency on Windows.
        generated=subprocess.run([BINARY,'tls','cert','--ca','--domain=localhost','--json'],
                                 capture_output=True,text=True,check=True,timeout=10)
        material=json.loads(generated.stdout)
        cls.cert=cls.root/'cert.pem';cls.key=cls.root/'key.pem'
        cls.cert.write_text('\n'.join(material['certificate'])+'\n')
        key_lines=material['key']
        # Xray emits PKCS#8 DER with a legacy PEM label; Python/OpenSSL needs the PKCS#8 label.
        key_lines[0]='-----BEGIN PRIVATE KEY-----';key_lines[-1]='-----END PRIVATE KEY-----'
        cls.key.write_text('\n'.join(key_lines)+'\n')
        cls.requests=[];cls.feed=''
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path)
                if self.path=='/generate_204':
                    self.send_response(204)
                    self.end_headers()
                    return
                if self.path=='/feed':
                    data=cls.feed.encode()
                else:
                    data=b'Xray E2E verified payload\n'*1024
                self.send_response(200)
                self.send_header('Content-Length',str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self,*args): pass
        cls.origin=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.cert),str(cls.key))
        cls.origin.socket=context.wrap_socket(cls.origin.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.origin.serve_forever,daemon=True);cls.thread.start()
        cls.proxy_port=allocate_port()
        config={'log':{'loglevel':'none'},'inbounds':[{'listen':'127.0.0.1','port':cls.proxy_port,
                  'protocol':'trojan','settings':{'clients':[{'password':'fixture-secret'}]},
                  'streamSettings':{'network':'tcp','security':'tls','tlsSettings':{
                    'certificates':[{'certificateFile':str(cls.cert),'keyFile':str(cls.key)}]}}}],
                'outbounds':[{'protocol':'freedom'}]}
        cfg=cls.root/'server.json';cfg.write_text(json.dumps(config))
        cls.proxy=subprocess.Popen([BINARY,'run','-c',str(cfg)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        check_ready(cls.proxy_port,cls.proxy)
        cls.url=f'trojan://fixture-secret@127.0.0.1:{cls.proxy_port}?security=tls&sni=localhost'
        cls.fixture={'ca_file':str(cls.cert),'probes':[
            (f'https://localhost:{cls.origin.server_port}/payload-a',8192),
            (f'https://localhost:{cls.origin.server_port}/payload-b',8192)]}
    @classmethod
    def tearDownClass(cls):
        cls.proxy.terminate();cls.proxy.wait(timeout=5)
        cls.origin.shutdown();cls.origin.server_close();cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def test_01_actual_tls_data_and_wrong_password(self):
        good=run_probe(parse(self.url),BINARY,15,local_fixture=self.fixture)
        self.assertTrue(good.passed,good.error)
        self.assertEqual(good.successes,2)
        self.assertGreater(good.body_bytes,32768)
        self.assertEqual(good.endpoint_ip,'127.0.0.1')
        bad=run_probe(parse(self.url.replace('fixture-secret@','incorrect-password@')),BINARY,8,local_fixture=self.fixture)
        self.assertFalse(bad.passed)

    def test_02_production_rejects_local_and_deadline(self):
        blocked=run_probe(parse(self.url),BINARY,8)
        self.assertFalse(blocked.passed)
        # Worker must not be able to make an external request using the local-test override.
        invalid=dict(self.fixture,probes=[('https://example.com/',128)])
        self.assertFalse(run_probe(parse(self.url),BINARY,5,local_fixture=invalid).passed)
        class Silent(socketserver.ThreadingTCPServer):
            allow_reuse_address=True
            daemon_threads=True
        class Sink(socketserver.BaseRequestHandler):
            def handle(self): time.sleep(7)
        with Silent(('127.0.0.1',0),Sink) as sink:
            t=threading.Thread(target=sink.serve_forever,daemon=True);t.start()
            url=f'trojan://fixture-secret@127.0.0.1:{sink.server_address[1]}?security=tls&sni=localhost'
            start=time.monotonic()
            result=run_probe(parse(url),BINARY,1.2,local_fixture=self.fixture)
            self.assertFalse(result.passed)
            self.assertLess(time.monotonic()-start,3)
            sink.shutdown();t.join(timeout=2)

    def test_025_wrong_tls_hostname_is_rejected(self):
        wrong=parse(self.url.replace('sni=localhost','sni=wrong.invalid'))
        self.assertFalse(run_probe(wrong,BINARY,8,local_fixture=self.fixture).passed)

    def test_026_real_delay_is_authenticated_https_not_a_body_download(self):
        fixture=dict(self.fixture,probes=[(f'https://localhost:{self.origin.server_port}/generate_204',0)])
        result=run_probe(parse(self.url),BINARY,8,quick=True,screen_timeout=2,local_fixture=fixture)
        self.assertTrue(result.passed,result.error)
        self.assertEqual((result.successes,result.body_bytes),(1,0))
        # A 200 captive response must not impersonate the expected 204 endpoint.
        wrong=dict(fixture,probes=[(f'https://localhost:{self.origin.server_port}/payload-a',0)])
        self.assertFalse(run_probe(parse(self.url),BINARY,8,quick=True,local_fixture=wrong).passed)
        # Screening success alone cannot pass the two-transfer publication gate.
        self.assertFalse(run_probe(parse(self.url),BINARY,8,quick=False,local_fixture=fixture).passed)

    def test_03_full_pipeline_then_health_and_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            args=arguments(['--xray',BINARY,'--limit','3','--recheck','3','--max-tests','3','--mode','full','--timeout','8'])
            # Collect from the actual HTTPS fixture using its trust anchor; no fake probe results.
            self.__class__.feed=self.url+'#old-label\n'+self.url.replace('fixture-secret@','bad-secret@')+'\n'
            feed_url=f'https://localhost:{self.origin.server_port}/feed'
            def collector(**kwargs):
                import urllib.request
                opener=urllib.request.build_opener(urllib.request.HTTPSHandler(
                    context=ssl.create_default_context(cafile=str(self.cert))))
                with patch('collector.urllib.request.urlopen',opener.open):
                    return collect({'Fixture':feed_url},**kwargs)
            probe=partial(run_probe,local_fixture=self.fixture)
            geo=lambda ips,path,workers: {ip:CountryInfo(ip,'DE','🇩🇪') for ip in ips}
            now=time.time()
            report=run_cycle(args,folder,probe,collector,geo,now)
            self.assertEqual(report['published'],1)
            self.assertTrue(report['nodes'][0]['verified_this_run'])
            first_url=(Path(folder)/'docs/sub.txt').read_text()
            validate_outputs(Path(folder)/'docs')
            self.assertEqual(parse(first_url.strip()).key,parse(self.url).key)
            self.assertIn('/feed',self.requests)
            # Exercise real commit/push against an isolated local bare remote.
            # Only the nested verification invocation is intercepted to avoid recursively running this suite.
            import sys
            import publishing
            remote=Path(folder).parent/(Path(folder).name+'-remote.git')
            try:
                def git(*a):
                    return subprocess.run(['git',*a],cwd=folder,check=True,capture_output=True,text=True)
                subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
                git('init','-b','main')
                git('config','user.name','Local Integration Test')
                git('config','user.email','integration@example.invalid')
                (Path(folder)/'.gitignore').write_text('.local/\n')
                git('add','.gitignore');git('commit','-m','Initialize fixture')
                git('remote','add','origin',str(remote));git('push','-u','origin','main')
                actual_run=subprocess.run
                gates=[]
                def dispatch(command,**kwargs):
                    if command[:2]==[sys.executable,str(Path(folder).resolve()/'verify.py')]:
                        gates.append(command)
                        return subprocess.CompletedProcess(command,0)
                    return actual_run(command,**kwargs)
                with patch('publishing.subprocess.run',side_effect=dispatch):
                    publishing.publish(folder,BINARY,Path(folder)/'docs')
                self.assertEqual(len(gates),1)
                local_sha=git('rev-parse','HEAD').stdout.strip()
                remote_sha=git('ls-remote','origin','refs/heads/main').stdout.split()[0]
                self.assertEqual(local_sha,remote_sha)
                committed=git('diff-tree','--no-commit-id','--name-only','-r','HEAD').stdout.splitlines()
                self.assertEqual(set(committed),{'docs/sub.txt','docs/sub.b64','docs/xray.json','docs/status.json','docs/index.html'})
                self.assertFalse(any('.local' in p for p in committed))
            finally:
                import shutil
                shutil.rmtree(remote,ignore_errors=True)
            args.mode='health'
            healthy=run_cycle(args,folder,probe,collector,geo,now+1800)
            self.assertEqual(healthy['published'],1)
            self.assertEqual((Path(folder)/'docs/sub.txt').read_text(),first_url)
            # Kill proxy access with wrong CA while retaining prior test timestamp honestly.
            wrong=dict(self.fixture,ca_file=str(Path(folder)/'missing-ca.pem'))
            failed=partial(run_probe,local_fixture=wrong)
            retained=run_cycle(args,folder,failed,collector,geo,now+3600)
            self.assertEqual(retained['health'],'retained_previous')
            self.assertFalse(retained['nodes'][0]['verified_this_run'])
            self.assertEqual(retained['nodes'][0]['last_success_epoch'],healthy['nodes'][0]['last_success_epoch'])
            expired=run_cycle(args,folder,failed,collector,geo,now+8*3600)
            self.assertEqual(expired['published'],0)
            self.assertEqual(expired['health'],'expired_or_empty')
            validate_outputs(Path(folder)/'docs')


import socketserver
