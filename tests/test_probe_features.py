"""Bounded diagnostics and address fallback through the pinned Xray core."""
from __future__ import annotations

import http.server
import os
import socket
import ssl
import struct
import threading
import unittest
from unittest.mock import patch

from collector import parse
from xray_test import VIDEO_BYTES, ProbeResult, _probe_worker, run_probe, validate_video_mirrors
from dataclasses import asdict


class ProbeFeatures(unittest.TestCase):
    def test_mirror_validation(self):
        mirror='https://media.example.org/movie.mp4'
        self.assertEqual(validate_video_mirrors([mirror,mirror]),[mirror])
        for invalid in ('http://media.example.org', 'https://localhost/movie.mp4',
                        'https://127.0.0.1/movie.mp4', 'https://user@media.example.org/movie.mp4',
                        'https://media.example.org/movie.mp4#bad'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_video_mirrors([invalid])

    def test_retry_prefers_alternate_address_family(self):
        candidate=parse('trojan://test@proxy.example.org:443?security=tls')
        addresses=['1.1.1.1','8.8.8.8','2606:4700:4700::1111']
        def verify(payload,outbound,address,*args):
            self.assertEqual(outbound['settings']['servers'][0]['address'],address)
            self.assertEqual(outbound['streamSettings']['tlsSettings']['serverName'],candidate.host)
            return ProbeResult(address==addresses[2],endpoint_ip=address,attempts=1),None
        with patch('xray_test.resolve_public',return_value=addresses.copy()), patch('xray_test._probe_endpoint',side_effect=verify) as check:
            result=_probe_worker({'candidate':asdict(candidate),'timeout':10,'quick':True})
        self.assertTrue(result.passed)
        self.assertEqual(result.attempts,2)
        self.assertEqual([call.args[2] for call in check.call_args_list],[addresses[0],addresses[2]])

    def test_optional_failure_does_not_change_baseline_pass(self):
        candidate=parse('trojan://test@proxy.example.org:443?security=tls')
        with patch('xray_test.communicate_bounded',side_effect=['{"passed":true}',RuntimeError()]):
            result=run_probe(candidate,'xray',stream_segments=3,check_udp=True,youtube_id='abcdefghijk')
        self.assertTrue(result.passed)
        self.assertIsNone(result.udp)
        self.assertEqual(result.stream_error,'DiagnosticWorkerFailed')
        self.assertEqual(result.youtube_error,'DiagnosticWorkerFailed')


BINARY=os.environ.get('XRAY_E2E_BINARY')


@unittest.skipUnless(BINARY,'Run verify.py for actual Xray diagnostics')
class RealProbeDiagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_e2e import RealXrayEndToEnd
        cls.proxy=RealXrayEndToEnd
        cls.proxy.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.proxy.tearDownClass()

    def test_udp_reply_through_trojan(self):
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as target:
            target.settimeout(8)
            target.bind(('127.0.0.1',0))
            def reply():
                try:
                    query,client=target.recvfrom(512)
                    target.sendto(query[:2]+struct.pack('>5H',0x8180,1,1,0,0)+query[12:]
                                  + b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\xc0\x00\x02\x01',client)
                except OSError:
                    return
            server=threading.Thread(target=reply,daemon=True)
            server.start()
            fixture=dict(self.proxy.fixture,udp_target=target.getsockname())
            result=run_probe(parse(self.proxy.url),BINARY,12,local_fixture=fixture,check_udp=True)
        server.join(timeout=2)
        self.assertTrue(result.passed,result.error)
        self.assertIs(result.udp,True)

    def test_media_segments_and_literal_ipv6_through_trojan(self):
        requests=[]
        video=struct.pack('>I4s4sI4s',12,b'ftyp',b'isom',3*VIDEO_BYTES-12,b'mdat')
        video+=bytes(3*VIDEO_BYTES-len(video))

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/ipv6':
                    self.send_response(200)
                    self.send_header('Content-Length','2')
                    self.end_headers()
                    self.wfile.write(b'OK')
                    return
                start,end=map(int,self.headers['Range'].removeprefix('bytes=').split('-'))
                requests.append((start,end))
                self.send_response(206)
                self.send_header('Content-Range',f'bytes {start}-{end}/{len(video)}')
                self.send_header('Content-Type','video/mp4')
                self.send_header('Content-Length',str(end-start+1))
                self.end_headers()
                self.wfile.write(video[start:end+1])

            def log_message(self, *args):
                pass

        class DualLoopbackServer(http.server.ThreadingHTTPServer):
            address_family=socket.AF_INET6

            def server_bind(self):
                self.socket.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,0)
                super().server_bind()

            def verify_request(self, request, client_address):
                from ipaddress import ip_address
                address=ip_address(client_address[0])
                return (address.ipv4_mapped or address).is_loopback

        try:
            server=DualLoopbackServer(('::',0),Handler)
        except OSError:
            self.skipTest('The test host has no dual-stack loopback')
        with server:
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(self.proxy.cert),str(self.proxy.key))
            server.socket=context.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                origin=f'https://localhost:{server.server_port}'
                fixture=dict(self.proxy.fixture,video_probes=[origin+'/video'],ipv6_target=origin+'/ipv6')
                result=run_probe(parse(self.proxy.url),BINARY,12,local_fixture=fixture,check_ipv6=True,stream_segments=3)
            finally:
                server.shutdown()
                thread.join(timeout=2)
        self.assertTrue(result.passed,result.error)
        self.assertIs(result.ipv6,True)
        self.assertEqual(result.stream_segments,3,result.stream_error)
        self.assertEqual(result.stream_stalls,0)
        self.assertGreater(result.stream_kbps,0)
        self.assertEqual(requests,[(i*VIDEO_BYTES,(i+1)*VIDEO_BYTES-1) for i in range(3)])
