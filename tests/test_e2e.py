"""Actual Xray/Trojan/TLS/HTTPS tests; pre-push verification makes these mandatory."""
import base64
from contextlib import contextmanager
from functools import partial
import http.server
import json
import os
from pathlib import Path
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from collector import collect, import_candidates, parse
from geolocation import CountryInfo
from main import arguments, run_cycle
from naming import rename_share_link
from publishing import validate_outputs
from state import read_state
from xray_test import VIDEO_BYTES, allocate_port, check_ready, run_probe

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
                if self.path in ('/video','/bad-video','/bad-range','/short-video','/html-video'):
                    if self.headers.get('Range') != f'bytes=0-{VIDEO_BYTES-1}':
                        self.send_error(416)
                        return
                    data = struct.pack('>I4s4sI4s',12,b'ftyp',b'isom',VIDEO_BYTES,b'mdat')
                    data += bytes(VIDEO_BYTES-len(data))
                    if self.path=='/bad-video': data=b'x'*VIDEO_BYTES
                    if self.path=='/short-video': data=data[:-1]
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes 0-{VIDEO_BYTES-(2 if self.path=="/bad-range" else 1)}/{VIDEO_BYTES+1}')
                    self.send_header('Content-Type','text/html' if self.path=='/html-video' else 'video/mp4')
                    self.send_header('Content-Length',str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
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
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.cert),str(cls.key))
        context.set_alpn_protocols(['h2','http/1.1'])
        class Origin(http.server.ThreadingHTTPServer):
            request_queue_size=64

            def process_request_thread(self, request, client_address):
                # REALITY probes its TLS target concurrently. Handshakes belong in the request threads.
                try:
                    request.settimeout(5)
                    request=context.wrap_socket(request,server_side=True)
                except OSError:
                    request.close()
                    return
                super().process_request_thread(request,client_address)
        cls.origin=Origin(('127.0.0.1',0),Handler)
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
        cls.origin_url=f'https://localhost:{cls.origin.server_port}'
        cls.fixture={'ca_file':str(cls.cert),'probes':[(cls.origin_url+'/payload-a',8192)],
                     'video_probes':[cls.origin_url+'/video']}
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
        self.assertEqual((good.video_bytes,good.video_host),(VIDEO_BYTES,'localhost'))
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
        fixture=dict(self.fixture,probes=[(self.origin_url+'/generate_204',0)],video_probes=[])
        result=run_probe(parse(self.url),BINARY,8,quick=True,screen_timeout=2,local_fixture=fixture)
        self.assertTrue(result.passed,result.error)
        self.assertEqual((result.successes,result.body_bytes),(1,0))
        # A 200 captive response must not impersonate the expected 204 endpoint.
        wrong=dict(fixture,probes=[(f'https://localhost:{self.origin.server_port}/payload-a',0)])
        self.assertFalse(run_probe(parse(self.url),BINARY,8,quick=True,local_fixture=wrong).passed)
        # Screening success alone cannot pass the two-transfer publication gate.
        self.assertFalse(run_probe(parse(self.url),BINARY,8,quick=False,local_fixture=fixture).passed)

    def test_026_video_gate_rejects_invalid_responses_and_uses_fallback(self):
        for path in ('bad-video','bad-range','short-video','html-video'):
            with self.subTest(path=path):
                fixture=dict(self.fixture,video_probes=[f'{self.origin_url}/{path}'])
                result=run_probe(parse(self.url),BINARY,8,local_fixture=fixture)
                self.assertFalse(result.passed)
                self.assertEqual((result.error,result.successes),('VideoVerificationFailed',1))
        fixture=dict(self.fixture,video_probes=[self.origin_url+'/bad-video',self.origin_url+'/video'])
        result=run_probe(parse(self.url),BINARY,8,local_fixture=fixture)
        self.assertTrue(result.passed,result.error)
        self.assertEqual(result.attempts,3)

    def test_protocols_and_transports_carry_https_and_video(self):
        uid = '14a2c062-1234-5678-abcd-0123456789ab'
        legacy = {'add':'127.0.0.1','id':uid,'net':'tcp','tls':'none','scy':'auto'}
        ss_key=base64.b64encode(bytes(range(16))).decode()
        encryption_keys=subprocess.run([BINARY,'vlessenc'],capture_output=True,text=True,check=True,timeout=10).stdout
        decryption=re.search(r'"decryption": "([^"]+)"',encryption_keys)[1]
        encryption=re.search(r'"encryption": "([^"]+)"',encryption_keys)[1]
        reality_keys=subprocess.run([BINARY,'x25519'],capture_output=True,text=True,check=True,timeout=10).stdout.splitlines()
        private_key=reality_keys[0].split(': ',1)[1]
        public_key=reality_keys[1].split(': ',1)[1]
        cases = [
            ('vless', {'clients':[{'id':uid}],'decryption':'none'}, f'vless://{uid}@{{addr}}', 'tcp', 'tls', '', {}),
            ('vmess', {'clients':[{'id':uid}]}, f'vmess://{uid}@{{addr}}', 'ws', 'tls', '&path=%2Fchat', {'wsSettings':{'path':'/chat'}}),
            ('vmess', {'clients':[{'id':uid}]}, None, 'tcp', 'none', '', {}),
            ('shadowsocks', {'method':'aes-256-gcm','password':'fixture-secret','network':'tcp'}, 'ss://aes-256-gcm:fixture-secret@{addr}', 'tcp', 'none', '', {}),
            ('shadowsocks', {'method':'2022-blake3-aes-128-gcm','password':ss_key,'network':'tcp'}, 'ss://'+base64.urlsafe_b64encode(f'2022-blake3-aes-128-gcm:{ss_key}'.encode()).decode()+'@{addr}', 'tcp', 'none', '', {}),
            ('socks', {'auth':'password','accounts':[{'user':'fixture','pass':'secret'}]}, 'socks5://fixture:secret@{addr}', 'tcp', 'none', '', {}),
            ('trojan', {'clients':[{'password':'fixture-secret'}]}, 'trojan://fixture-secret@{addr}', 'grpc', 'tls', '&serviceName=fixture&authority=localhost', {'grpcSettings':{'serviceName':'fixture'}}),
            ('trojan', {'clients':[{'password':'fixture-secret'}]}, 'trojan://fixture-secret@{addr}', 'httpupgrade', 'tls', '&path=%2Fchat', {'httpupgradeSettings':{'path':'/chat'}}),
            ('trojan', {'clients':[{'password':'fixture-secret'}]}, 'trojan://fixture-secret@{addr}', 'xhttp', 'tls', '&path=%2Fchat&mode=packet-up&extra=%7B%22xPaddingBytes%22%3A%22100-200%22%7D', {'xhttpSettings':{'path':'/chat','mode':'packet-up'}}),
            ('vmess', {'clients':[{'id':uid}]}, f'vmess://{uid}@{{addr}}', 'kcp', 'none', '?type=kcp&mtu=1200&seed=fixture-seed', {'kcpSettings':{'mtu':1200},'finalmask':{'udp':[{'type':'mkcp-aes128gcm','settings':{'password':'fixture-seed'}}]}}),
            ('hysteria', {'version':2,'clients':[{'auth':'fixture-secret'}]}, 'hy2://fixture-secret@{addr}-{port}?sni=localhost&obfs=salamander&obfs-password=fixture-mask', 'hysteria', 'tls', '', {'hysteriaSettings':{'version':2},'finalmask':{'udp':[{'type':'salamander','settings':{'password':'fixture-mask'}}]}}),
            ('vless', {'clients':[{'id':uid}],'decryption':decryption}, f'vless://{uid}@{{addr}}', 'tcp', 'none', f'?encryption={encryption}', {}),
            ('vless', {'clients':[{'id':uid,'flow':'xtls-rprx-vision'}],'decryption':'none'}, f'vless://{uid}@{{addr}}', 'tcp', 'reality', f'?security=reality&sni=localhost&fp=chrome&sid=0708&pbk={public_key}&flow=xtls-rprx-vision', {'realitySettings':{'target':f'127.0.0.1:{self.origin.server_port}','privateKey':private_key,'serverNames':['localhost'],'shortIds':['0708']}}),
        ]
        for protocol, settings, link, transport, security, query, transport_settings in cases:
            with self.subTest(protocol=protocol,transport=transport,security=security):
                port=allocate_port()
                if link is None:
                    encoded=base64.b64encode(json.dumps(dict(legacy,port=port)).encode()).decode()
                    url='vmess://'+encoded
                else:
                    url=link.format(addr=f'127.0.0.1:{port}',port=port)
                    if security=='tls' and protocol!='hysteria':
                        url+=f'?type={transport}&security=tls&sni=localhost'
                    url+=query
                stream=dict(transport_settings,network=transport,security=security)
                if security=='tls':
                    stream['tlsSettings']={'certificates':[{'certificateFile':str(self.cert),'keyFile':str(self.key)}]}
                if transport=='hysteria':
                    stream['tlsSettings']['alpn']=['h3']
                inbound={'listen':'127.0.0.1','port':port,'protocol':protocol,'settings':settings,'streamSettings':stream}
                config={'inbounds':[inbound],'outbounds':[{'protocol':'freedom'}]}
                cfg=self.root/'matrix.json'
                cfg.write_text(json.dumps(config))
                with subprocess.Popen([BINARY,'run','-c',str(cfg)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as server:
                    try:
                        if transport in ('kcp','hysteria'):
                            # Wait for the UDP bind without sending a protocol packet.
                            deadline=time.monotonic()+4
                            while time.monotonic()<deadline:
                                self.assertIsNone(server.poll(),'Xray fixture exited')
                                with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as udp:
                                    if os.name=='nt':
                                        udp.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                                    try: udp.bind(('127.0.0.1',port))
                                    except OSError: break
                                time.sleep(0.05)
                            else: self.fail('Xray fixture did not bind UDP')
                        else:
                            check_ready(port,server)
                        candidate=parse(url)
                        self.assertIsNotNone(candidate)
                        self.assertEqual(parse(rename_share_link(url,'Verified fixture')).key,candidate.key)
                        result=run_probe(candidate,BINARY,12,local_fixture=self.fixture)
                        self.assertTrue(result.passed,result.error)
                        self.assertGreaterEqual(result.body_bytes,VIDEO_BYTES)
                    finally:
                        if server.poll() is None: server.terminate()
                        server.wait(timeout=5)

    def test_private_https_connect_and_xhttp_split_download(self):
        certificates={'certificates':[{'certificateFile':str(self.cert),'keyFile':str(self.key)}]}
        tls={'network':'tcp','security':'tls','tlsSettings':dict(certificates,alpn=['http/1.1'])}
        port=allocate_port()
        download=allocate_port()
        auth={'user':'fixture','pass':'secret'}
        xhttp=dict(tls,network='xhttp',tlsSettings=certificates,xhttpSettings={'path':'/split','mode':'packet-up'})
        trojan={'clients':[{'password':'secret'}]}
        cases=[
            ([{'port':port,'protocol':'http','settings':{'accounts':[auth]},'streamSettings':tls}],
             {'protocol':'http','settings':{'address':'127.0.0.1','port':port,**auth},
              'streamSettings':{'network':'tcp','security':'tls','tlsSettings':{'serverName':'localhost'}}}),
            ([{'port':port,'protocol':'trojan','settings':trojan,'streamSettings':xhttp},
              {'port':download,'protocol':'tunnel','settings':{'address':'127.0.0.1','port':port,'network':'tcp'}}],
             {'protocol':'trojan','settings':{'address':'127.0.0.1','port':port,'password':'secret'},
              'streamSettings':{'network':'xhttp','security':'tls','tlsSettings':{'serverName':'localhost'},
                  'xhttpSettings':{'path':'/split','mode':'packet-up','downloadSettings':{
                      'address':'127.0.0.1','port':download,'network':'xhttp','security':'tls',
                      'tlsSettings':{'serverName':'localhost'},'xhttpSettings':{'path':'/split','mode':'packet-up'}}}}}),
        ]
        for inbounds,outbound in cases:
            with self.subTest(protocol=outbound['protocol']):
                for inbound in inbounds:
                    inbound['listen']='127.0.0.1'
                config={'inbounds':inbounds,'outbounds':[{'protocol':'freedom'}]}
                cfg=self.root/'private.json'
                cfg.write_text(json.dumps(config))
                with subprocess.Popen([BINARY,'run','-c',str(cfg)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as server:
                    try:
                        for inbound in inbounds:
                            check_ready(inbound['port'],server)
                        candidate,=import_candidates(json.dumps({'outbounds':[outbound]}))
                        result=run_probe(candidate,BINARY,12,local_fixture=self.fixture)
                        self.assertTrue(result.passed,f'{result.stage}/{result.error}')
                    finally:
                        if server.poll() is None: server.terminate()
                        server.wait(timeout=5)

    def test_private_wireguard(self):
        def keypair():
            result=subprocess.run([BINARY,'x25519'],capture_output=True,text=True,check=True,timeout=10)
            return [line.split(': ',1)[1] for line in result.stdout.splitlines()[:2]]
        server_key,server_public=keypair()
        client_key,client_public=keypair()
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        inbound={'listen':'127.0.0.1','port':port,'protocol':'wireguard','settings':{
            'secretKey':server_key,'address':['10.11.12.1'],'noKernelTun':True,
            'peers':[{'publicKey':client_public,'allowedIPs':['10.11.12.2/32']}]}}
        cfg=self.root/'wireguard.json'
        cfg.write_text(json.dumps({'inbounds':[inbound],'outbounds':[{'protocol':'freedom','settings':{
            'redirect':f'127.0.0.1:{self.origin.server_port}'}}]}))
        outbound={'protocol':'wireguard','settings':{'secretKey':client_key,'address':['10.11.12.2'],
                  'peers':[{'publicKey':server_public,'endpoint':f'127.0.0.1:{port}'}]}}
        candidate,=import_candidates(json.dumps(outbound))
        # Route to the fixture tunnel address and preserve localhost as the HTTPS name.
        fixture=dict(self.fixture,connect_host='10.11.12.1')
        with subprocess.Popen([BINARY,'run','-c',str(cfg)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as server:
            try:
                self.assertIsNone(server.poll())
                result=run_probe(candidate,BINARY,12,local_fixture=fixture)
                self.assertTrue(result.passed,f'{result.stage}/{result.error}')
            finally:
                if server.poll() is None: server.terminate()
                server.wait(timeout=5)

    def test_027_a_failed_direct_fetch_goes_through_a_verified_node(self):
        import collector
        from relay import fallback
        self.__class__.feed=self.url+'#through-relay\n'
        origin=f'https://localhost:{self.origin.server_port}'
        fixture={'ca_file':str(self.cert),'check':(origin+'/generate_204',204)}
        def reset(url,timeout=20):
            raise ConnectionResetError()
        dead=parse(self.url.replace('fixture-secret@','bad-secret@'))
        self.requests.clear()
        with patch('collector.direct_get',reset):
            # No relay: the reset is the result. With one: the dead node is passed over.
            self.assertEqual(collect({'Fixture':origin+'/feed'})['Fixture'],[])
            with fallback(BINARY,[dead,parse(self.url)],fixture) as relay:
                self.assertIsNone(relay.proc)
                groups=collect({'Fixture':origin+'/feed'})
                self.assertIsNotNone(relay.proc)
            self.assertEqual([c.key for c in groups['Fixture']],[parse(self.url).key])
            self.assertEqual(self.requests,['/generate_204','/feed'])
            self.assertIsNone(relay.proc)
            self.assertIsNone(collector.FALLBACK)
            with fallback(BINARY,[dead],fixture):
                self.assertEqual(collect({'Fixture':origin+'/feed'})['Fixture'],[])

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
            self.assertEqual(report['nodes'][0]['video_bytes'],VIDEO_BYTES)
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
                workflow=Path(folder)/publishing.SNAPSHOT_WORKFLOW
                workflow.parent.mkdir(parents=True)
                workflow.write_bytes((Path(__file__).resolve().parents[1]/publishing.SNAPSHOT_WORKFLOW).read_bytes())
                code_head=git('rev-parse','HEAD').stdout.strip()
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
                self.assertEqual(code_head,git('rev-parse','HEAD').stdout.strip())
                self.assertEqual(code_head,git('ls-remote','origin','refs/heads/main').stdout.split()[0])
                git('fetch','origin',publishing.FEED_BRANCH)
                committed=git('ls-tree','-r','--name-only','FETCH_HEAD').stdout.splitlines()
                self.assertEqual(set(committed),{publishing.SNAPSHOT_WORKFLOW,*(f'docs/{name}' for name in publishing.OUTPUTS)})
                self.assertEqual(len(git('rev-list','--parents','-1','FETCH_HEAD').stdout.split()),1)
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
