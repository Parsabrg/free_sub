"""Real Xray-core end-to-end HTTPS probes over an isolated localhost SOCKS inbound."""
from __future__ import annotations
import http.client
import json
import copy
import statistics
import sys
import ipaddress
import os
import socket
import ssl
import subprocess
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlsplit
from network import BIND_ENV, resolve_public, pin_endpoint
from process_guard import communicate_bounded

# First URL transfers ~13 KiB of real HTTPS response-body data.
# The others are independent smaller fallbacks; their throughput ranks lower.
# None is hosted on Cloudflare: a Cloudflare Worker relay (edgetunnel, BPB and
# their kind, a large share of public Iranian configs) cannot open sockets to
# Cloudflare's own addresses, so a Cloudflare probe failed every such config
# however well it carried everything else (2026-10-22: 356 of 3466 passed the
# Google-only screen, 4 survived two Cloudflare probes of three).
PROBES = [
    ('https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png', 8192),
    ('https://en.wikipedia.org/robots.txt', 128),
    ('https://www.google.com/robots.txt', 128),
]
# Real-delay screening must not spend its short budget on a speed download.
SCREEN_PROBES = [
    ('https://www.gstatic.com/generate_204', 0),
    ('https://www.google.com/generate_204', 0),
]

@dataclass
class ProbeResult:
    passed: bool
    latency_ms: float = 0
    body_bytes: int = 0
    kbps: float = 0
    full_size: bool = False
    error: str = ''
    endpoint_ip: str = ''
    successes: int = 0
    attempts: int = 0
    jitter_ms: float = 0
    probe_hosts: tuple = ()
    completed_epoch: float = 0

def recv_exact(sock, num):
    buf=b''
    while len(buf)<num:
        chunk=sock.recv(num-len(buf))
        if not chunk: raise OSError('SOCKS server closed connection')
        buf+=chunk
    return buf

def socks_connect(port, host, dest_port, timeout):
    """Open SOCKS5 with DOMAIN address; target DNS is delegated to Xray/proxy."""
    host_bytes=host.encode('idna')
    if len(host_bytes)>255: raise ValueError('Host too long')
    s=socket.create_connection(('127.0.0.1',port),timeout=timeout)
    try:
        s.settimeout(timeout)
        s.sendall(b'\x05\x01\x00')
        if recv_exact(s,2)!=b'\x05\x00': raise OSError('SOCKS5 handshake refused')
        request=b'\x05\x01\x00\x03'+bytes([len(host_bytes)])+host_bytes+dest_port.to_bytes(2,'big')
        s.sendall(request)
        h=recv_exact(s,4)
        if h[0]!=5 or h[1]!=0: raise OSError(f'SOCKS5 connect rejected (code {h[1]})')
        typ=h[3]
        if typ==1: recv_exact(s,4)
        elif typ==4: recv_exact(s,16)
        elif typ==3: recv_exact(s,recv_exact(s,1)[0])
        else: raise OSError('Unsupported SOCKS5 address type')
        recv_exact(s,2)
        return s
    except Exception:
        s.close()
        raise

def https_get_via_socks(local_port, target, timeout, ssl_context, max_bytes=65536, *, expected_status=200):
    u=urlsplit(target)
    if u.scheme!='https': raise ValueError('HTTPS only')
    started=time.monotonic()
    raw=socks_connect(local_port,u.hostname,u.port or 443,timeout)
    try:
        with ssl_context.wrap_socket(raw,server_hostname=u.hostname) as secured:
            secured.settimeout(timeout)
            conn=http.client.HTTPSConnection(u.hostname, timeout=timeout)
            conn.sock=secured
            conn.request('GET',(u.path or '/')+('?' + u.query if u.query else ''), headers={
                'Host':u.netloc,'User-Agent':'LocalXrayTester/1.0',
                'Accept-Encoding':'identity','Connection':'close'})
            resp=conn.getresponse()
            body=resp.read(max_bytes+1)
            if len(body)>max_bytes: raise OSError('OversizeBody')
            elapsed=max(time.monotonic()-started, 0.001)
            if resp.status!=expected_status: raise OSError(f'HTTP status {resp.status}')
            return body,elapsed
    finally:
        raw.close()

def allocate_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]

def check_ready(port, proc, timeout=4.0):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if proc.poll() is not None: raise RuntimeError(f'Xray exited with code {proc.returncode}')
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=0.15): return
        except OSError:
            time.sleep(0.08)
    raise TimeoutError('Xray SOCKS listener did not become ready')

def _is_local_node(node):
    """A node on this PC (the self-test's fixture server) cannot be reached
    through the Wi-Fi adapter: binding it there reset every connection, the
    self-test failed, and publishing stopped (2026-10-23 to 2026-10-26)."""
    s=node.get('settings') or {}
    entry=((s.get('vnext') or s.get('servers') or [{}])[0]) or {}
    host=str(entry.get('address') or '').strip('[]').lower()
    if host in ('localhost',''):
        return host=='localhost'
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_config(outbound, local_port):
    node=copy.deepcopy(outbound)
    node['tag']='candidate'
    # Leave through the real adapter even while a VPN holds the default route.
    interface=os.environ.get(BIND_ENV)
    if interface and not _is_local_node(node):
        node.setdefault('streamSettings',{}).setdefault('sockopt',{})['interface']=interface
    return {
        'log':{'loglevel':'warning'},
        'inbounds':[{'listen':'127.0.0.1','port':local_port,'protocol':'socks',
                     'settings':{'auth':'noauth','udp':False},'tag':'local-test'}],
        'outbounds':[node,{'protocol':'blackhole','tag':'blocked'}],
        'routing':{'domainStrategy':'AsIs','rules':[
            {'type':'field','inboundTag':['local-test'],'outboundTag':'candidate'}]},
    }

def run_probe(candidate, xray_bin, timeout=30.0, *, quick=False, screen_timeout=None, local_fixture=None):
    """An outer deadline covers DNS, startup, TLS, body reads and fallback probes.

    screen_timeout optionally starts a shorter network deadline after Xray is ready.

    local_fixture is an internal integration-test hook: all addresses MUST be loopback.
    The production CLI never exposes this hook or accepts a custom trust anchor.
    """
    # Parent ownership cleans probe files even when the whole worker is killed.
    with tempfile.TemporaryDirectory(prefix='xray-probe-') as work_dir:
        payload = {'candidate': asdict(candidate), 'xray_bin':str(xray_bin), 'timeout':timeout,
                   'quick':quick, 'screen_timeout':screen_timeout, 'fixture':local_fixture, 'work_dir':work_dir}
        try:
            output = communicate_bounded([sys.executable, str(Path(__file__).resolve()), '--worker'],
                                         json.dumps(payload), timeout, ready_timeout=screen_timeout)
            result = ProbeResult(**json.loads(output))
            result.completed_epoch = time.time()
            return result
        except subprocess.TimeoutExpired:
            return ProbeResult(False, error='CandidateDeadline')
        except (OSError, ValueError, RuntimeError):
            return ProbeResult(False, error='ProbeWorkerFailed')


def _probe_worker(payload):
    from collector import Candidate
    candidate = Candidate(**payload['candidate'])
    fixture = payload.get('fixture')
    if fixture:
        if not ipaddress.ip_address(candidate.host).is_loopback:
            raise ValueError('FixturesMustBeLoopback')
        probes = fixture['probes']
        if any(urlsplit(u).hostname!='localhost' and not ipaddress.ip_address(urlsplit(u).hostname).is_loopback for u, _ in probes):
            raise ValueError('FixturesMustBeLoopback')
        addresses = [candidate.host]
        context = ssl.create_default_context(cafile=fixture['ca_file'])
    else:
        addresses = resolve_public(candidate.host)
        probes = list(SCREEN_PROBES if payload.get('quick') else PROBES)
        # Rotate destinations across runs; failure of one provider need not kill the node.
        if int(time.time()//1800) % 2:
            probes = probes[-1:] + probes[:-1]
        context = ssl.create_default_context()
    outbound = pin_endpoint(candidate.outbound, addresses)
    if fixture:
        # Windows Xray uses custom CA certificates only with its isolated root pool.
        outbound['streamSettings']['tlsSettings']['disableSystemRoot'] = True
        outbound['streamSettings']['tlsSettings']['certificates'] = [
            {'usage':'verify', 'certificateFile':fixture['ca_file']}]
    successes = []; attempts = 0; last_error = ''
    target_successes = 1 if payload.get('quick') else 2
    p = allocate_port()
    with tempfile.TemporaryDirectory(prefix='xray-check-',dir=payload.get('work_dir')) as tmp:
        cfg = Path(tmp)/'config.json'
        cfg.write_text(json.dumps(make_config(outbound,p)), encoding='utf-8')
        proc = None
        try:
            proc = subprocess.Popen([payload['xray_bin'],'run','-c',str(cfg)], cwd=tmp,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            check_ready(p,proc)
            if payload.get('screen_timeout') is not None:
                print('READY',flush=True)
            for url, minimum in probes:
                attempts += 1
                try:
                    body, elapsed = https_get_via_socks(p,url,min(6,payload['timeout']),context,
                                                      expected_status=204 if minimum==0 else 200)
                    if len(body)<minimum:
                        raise OSError('ShortBody')
                    successes.append((url, len(body), elapsed, minimum>=8192))
                    if len(successes)>=target_successes:
                        break
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    last_error = type(exc).__name__
            if len(successes)<target_successes:
                return ProbeResult(False, error=last_error or 'TooFewIndependentProbes',
                                   attempts=attempts, successes=len(successes), endpoint_ip=addresses[0])
            times = [t*1000 for _,_,t,_ in successes]
            transferred = sum(n for _,n,_,_ in successes)
            elapsed = sum(t for _,_,t,_ in successes)
            return ProbeResult(True, round(statistics.median(times),1), transferred,
                               round(transferred*8/1000/max(elapsed,0.001),1),
                               any(full for *_,full in successes), endpoint_ip=addresses[0],
                               successes=len(successes), attempts=attempts,
                               jitter_ms=round(max(times)-min(times),1),
                               probe_hosts=tuple(urlsplit(u).hostname for u,_,_,_ in successes))
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)


if __name__ == '__main__':
    try:
        print(json.dumps(asdict(_probe_worker(json.loads(sys.stdin.read())))))
    except Exception as exc:
        # Never print a source URL, credential, or untrusted Xray log.
        print(json.dumps(asdict(ProbeResult(False,error=type(exc).__name__))))
