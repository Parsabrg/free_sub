"""Real Xray-core end-to-end HTTPS probes over an isolated localhost SOCKS inbound."""
from __future__ import annotations
import http.client
import json
import copy
import statistics
import sys
import ipaddress
import os
import re
import socket
import ssl
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.parse import urlsplit
from network import BIND_ENV, outbound_endpoint, public_address, resolve_public, pin_endpoint
from process_guard import ProbeDeadline, communicate_bounded

PROBES = [
    ('https://en.wikipedia.org/robots.txt', 128),
    ('https://www.apple.com/robots.txt', 128),
]
SCREEN_PROBES = [
    ('https://www.gstatic.com/generate_204', 0),
    ('https://www.apple.com/robots.txt', 128),
]
# The primary works with Cloudflare Worker proxies that cannot dial Cloudflare IPs.
VIDEO_PROBES = [
    'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4',
    'https://media.w3.org/2010/05/sintel/trailer.mp4',
]
VIDEO_BYTES = 128 * 1024
YOUTUBE_ID = re.compile(r'[A-Za-z0-9_-]{11}')

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
    video_host: str = ''
    video_bytes: int = 0
    video_ms: float = 0
    video_probe_index: int = -1
    stage: str = ''
    stream_segments: int = 0
    stream_kbps: float = 0
    stream_start_ms: float = 0
    stream_stalls: int = 0
    stream_error: str = ''
    udp: bool | None = None
    ipv6: bool | None = None
    youtube_played: bool | None = None
    youtube_error: str = ''


def validate_video_mirrors(urls):
    """Override the media origins with a bounded list of HTTPS URLs."""
    mirrors = list(dict.fromkeys(urls or []))
    if len(mirrors) > 4:
        raise ValueError('Use at most four video mirrors')
    for url in mirrors:
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            port = parsed.port
            if (len(url) > 2048 or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url)
                    or parsed.scheme != 'https' or not host or parsed.username is not None
                    or parsed.fragment or port == 0 or host.rstrip('.').endswith(('.localhost','.local'))):
                raise ValueError()
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if '.' not in host.rstrip('.') or '\\' in host:
                    raise ValueError() from None
            else:
                if not public_address(host):
                    raise ValueError()
        except (TypeError, ValueError):
            raise ValueError('Video mirrors must be public HTTPS URLs without credentials or fragments') from None
    return mirrors


class SocksError(OSError):
    pass

def recv_exact(sock, num):
    buf=b''
    while len(buf)<num:
        chunk=sock.recv(num-len(buf))
        if not chunk: raise OSError('SOCKS server closed connection')
        buf+=chunk
    return buf

def socks_address(host, port):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        data = host.encode('idna')
        if not 0 < len(data) <= 255:
            raise ValueError('Invalid SOCKS5 hostname length') from None
        addr = b'\x03' + bytes([len(data)]) + data
    else:
        addr = (b'\x01' if ip.version == 4 else b'\x04') + ip.packed
    return addr + port.to_bytes(2,'big')


def socks_command(port, command, host, dest_port, timeout):
    """Use literal address types for IPs and let the proxy resolve domain targets."""
    try:
        s = socket.create_connection(('127.0.0.1',port),timeout=timeout)
    except OSError as exc:
        raise SocksError('SOCKS5 listener unavailable') from exc
    try:
        s.settimeout(timeout)
        s.sendall(b'\x05\x01\x00')
        if recv_exact(s,2)!=b'\x05\x00': raise SocksError('SOCKS5 handshake refused')
        s.sendall(bytes((5,command,0))+socks_address(host,dest_port))
        h=recv_exact(s,4)
        if h[:3]!=b'\x05\x00\x00': raise SocksError(f'SOCKS5 command rejected (code {h[1]})')
        typ=h[3]
        if typ==1: bound=socket.inet_ntop(socket.AF_INET,recv_exact(s,4))
        elif typ==4: bound=socket.inet_ntop(socket.AF_INET6,recv_exact(s,16))
        elif typ==3: bound=recv_exact(s,recv_exact(s,1)[0]).decode('ascii')
        else: raise SocksError('Unsupported SOCKS5 address type')
        return s, (bound,int.from_bytes(recv_exact(s,2),'big'))
    except OSError as exc:
        s.close()
        if isinstance(exc,SocksError): raise
        raise SocksError('SOCKS5 handshake or command failed') from exc
    except Exception:
        s.close()
        raise


def socks_connect(port, host, dest_port, timeout):
    return socks_command(port,1,host,dest_port,timeout)[0]

def mp4_contains_media(body):
    if body[4:8] != b'ftyp':
        return False
    offset = 0
    while offset+8 <= len(body):
        size, kind = struct.unpack_from('>I4s',body,offset)
        if kind == b'mdat':
            return min(size or len(body),len(body)-offset) >= 8+4096
        if size < 8:
            return False
        offset += size
    return False


def https_get_via_socks(local_port, target, timeout, ssl_context, max_bytes=65536, *, expected_status=200,
                       video_range=False, range_start=0, accept=None, connect_host=None):
    u=urlsplit(target)
    if u.scheme!='https': raise ValueError('HTTPS only')
    started=time.monotonic()
    def remaining():
        seconds=timeout-(time.monotonic()-started)
        if seconds<=0: raise TimeoutError('HTTPS probe deadline')
        return seconds

    raw=socks_connect(local_port,connect_host or u.hostname,u.port or 443,timeout)
    try:
        raw.settimeout(remaining())
        with ssl_context.wrap_socket(raw,server_hostname=u.hostname) as secured:
            secured.settimeout(remaining())
            conn=http.client.HTTPSConnection(u.hostname, timeout=remaining())
            conn.sock=secured
            headers = {
                'Host':u.netloc,'User-Agent':'LocalXrayTester/1.0',
                'Accept-Encoding':'identity','Connection':'close'}
            if accept: headers['Accept']=accept
            if video_range:
                headers['Range'] = f'bytes={range_start}-{range_start+max_bytes-1}'
                expected_status = 206
            conn.request('GET',(u.path or '/')+('?' + u.query if u.query else ''), headers=headers)
            secured.settimeout(remaining())
            resp=conn.getresponse()
            if resp.status!=expected_status: raise OSError(f'HTTP status {resp.status}')
            if video_range:
                content_range = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', resp.getheader('Content-Range', ''))
                if (resp.headers.get_content_type() != 'video/mp4' or not content_range
                        or int(content_range[1]) != range_start or int(content_range[2]) != range_start+max_bytes-1
                        or int(content_range[3]) < range_start+max_bytes):
                    raise ValueError('InvalidVideoRange')
            body=bytearray()
            while len(body)<=max_bytes:
                if resp.isclosed(): break
                secured.settimeout(remaining())
                chunk=resp.read1(min(64*1024,max_bytes+1-len(body)))
                if not chunk: break
                body.extend(chunk)
            if len(body)>max_bytes: raise OSError('OversizeBody')
            if video_range and (len(body)!=max_bytes or (range_start==0 and not mp4_contains_media(body))):
                raise ValueError('InvalidVideoBody')
            elapsed=max(time.monotonic()-started, 0.001)
            return bytes(body),elapsed
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
    if node['protocol']=='wireguard':
        from xray_import import primary_endpoint
        host=primary_endpoint(node)[0]
    else:
        host=str(outbound_endpoint(node).get('address') or '').strip('[]').lower()
    if host in ('localhost',''):
        return host=='localhost'
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_config(outbound, local_port, *, udp=False):
    node=copy.deepcopy(outbound)
    node['tag']='candidate'
    # Leave through the real adapter even while a VPN holds the default route.
    interface=os.environ.get(BIND_ENV)
    if interface and not _is_local_node(node):
        node.setdefault('streamSettings',{}).setdefault('sockopt',{})['interface']=interface
    return {
        'log':{'loglevel':'warning'},
        'inbounds':[{'listen':'127.0.0.1','port':local_port,'protocol':'socks',
                     'settings':{'auth':'noauth','udp':udp},'tag':'local-test'}],
        'outbounds':[node,{'protocol':'blackhole','tag':'blocked'}],
        'routing':{'domainStrategy':'AsIs','rules':[
            {'type':'field','inboundTag':['local-test'],'outboundTag':'candidate'}]},
    }

def run_probe(candidate, xray_bin, timeout=30.0, *, quick=False, screen_timeout=None, local_fixture=None,
              video_mirrors=None, stream_segments=1, check_udp=False, check_ipv6=False, youtube_id=None,
              interface=None):
    """An outer deadline covers DNS, startup, TLS, body reads and fallback probes.

    screen_timeout optionally starts a shorter network deadline after Xray is ready.

    local_fixture is an internal integration-test hook: all addresses MUST be loopback.
    The production CLI never exposes this hook or accepts a custom trust anchor.
    """
    mirrors = validate_video_mirrors(video_mirrors)
    if not isinstance(stream_segments,int) or not 1 <= stream_segments <= 8:
        raise ValueError('Stream segments must be between 1 and 8')
    if youtube_id is not None and not YOUTUBE_ID.fullmatch(youtube_id):
        raise ValueError('Use an 11-character YouTube video ID')
    # Parent ownership cleans probe files even when the whole worker is killed.
    # ignore_cleanup_errors: closing the probe's job object kills Xray asynchronously, so
    # Windows can still hold a file for a moment. The PermissionError that raised then
    # replaced the probe's result (a passing one too) and left 10,800 empty folders in
    # %TEMP% (2026-10-26); state.sweep_stale_probe_dirs removes any that remain.
    with tempfile.TemporaryDirectory(prefix='xray-probe-',ignore_cleanup_errors=True) as work_dir:
        payload = {'candidate': asdict(candidate), 'xray_bin':str(xray_bin), 'timeout':timeout,
                   'quick':quick, 'screen_timeout':screen_timeout, 'fixture':local_fixture,
                   'work_dir':work_dir, 'video_mirrors':mirrors, 'interface':interface}
        command = [sys.executable, str(Path(__file__).resolve()), '--worker']
        try:
            output = communicate_bounded(command,json.dumps(payload),timeout,ready_timeout=screen_timeout)
            result = ProbeResult(**json.loads(output))
            result.completed_epoch = time.time()
        except ProbeDeadline as exc:
            error = 'ScreenStartupDeadline' if exc.phase == 'startup' else 'ScreenNetworkDeadline'
            return ProbeResult(False,error=error,stage=exc.phase,completed_epoch=time.time())
        except subprocess.TimeoutExpired:
            return ProbeResult(False,error='CandidateDeadline',stage='timeout',completed_epoch=time.time())
        except (OSError, ValueError, RuntimeError):
            return ProbeResult(False,error='ProbeWorkerFailed',stage='startup')
        if quick or not result.passed or not (stream_segments>1 or check_udp or check_ipv6 or youtube_id):
            return result
        # An optional diagnostic can time out without erasing the completed publication check.
        payload.update(endpoint=result.endpoint_ip, video_probe_index=result.video_probe_index, video_ms=result.video_ms,
                       stream_segments=stream_segments,check_udp=check_udp,check_ipv6=check_ipv6,
                       youtube_id=youtube_id,timeout=max(30,timeout))
        try:
            output = communicate_bounded(command,json.dumps(payload),payload['timeout'])
            diagnostics = json.loads(output)
            for key in ('stream_segments','stream_kbps','stream_start_ms','stream_stalls','stream_error',
                        'udp','ipv6','youtube_played','youtube_error'):
                if key in diagnostics:
                    setattr(result,key,diagnostics[key])
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
            if stream_segments>1: result.stream_error='DiagnosticWorkerFailed'
            if youtube_id: result.youtube_error='DiagnosticWorkerFailed'
        return result


def fixture_address(host):
    if host=='localhost':
        return ['127.0.0.1']
    if ipaddress.ip_address(host).is_loopback:
        return [host]
    raise ValueError('FixturesMustBeLoopback')


def probe_context(candidate, fixture, quick, video_mirrors):
    if fixture:
        fixture_address(candidate.host)
        if fixture.get('connect_host') and fixture['connect_host']!='10.11.12.1':
            raise ValueError('Invalid fixture tunnel target')
        probes = fixture['probes']
        videos = fixture.get('video_probes', [])
        for url in [url for url, _ in probes]+videos:
            fixture_address(urlsplit(url).hostname)
        return probes,videos,ssl.create_default_context(cafile=fixture['ca_file'])
    probes = SCREEN_PROBES if quick else PROBES
    # Rotate destinations across runs so a single provider cannot dominate screening.
    if int(time.time()//1800) % 2:
        probes = probes[::-1]
    return probes,video_mirrors or VIDEO_PROBES,ssl.create_default_context()


def probe_outbound(candidate, address, fixture):
    if getattr(candidate,'raw',False):
        from xray_import import pin_import_endpoints
        outbound = pin_import_endpoints(candidate.outbound,address,fixture_address if fixture else resolve_public)
    else:
        outbound = pin_endpoint(candidate.outbound,[address])
    if fixture:
        from xray_import import download_settings
        stream=outbound.get('streamSettings',{})
        # Windows Xray uses custom CA certificates only with its isolated root pool.
        for settings in (stream,download_settings(stream)):
            if settings and settings.get('security')=='tls':
                tls=settings['tlsSettings']
                tls['disableSystemRoot']=True
                tls['certificates']=[{'usage':'verify','certificateFile':fixture['ca_file']}]
    return outbound


def _probe_worker(payload):
    from collector import Candidate
    candidate = Candidate(**payload['candidate'])
    fixture = payload.get('fixture')
    if payload.get('interface'):
        os.environ[BIND_ENV] = payload['interface']
    quick = payload.get('quick')
    full_deadline=time.monotonic()+payload['timeout']-0.5
    try:
        probes,videos,context = probe_context(candidate,fixture,quick,payload.get('video_mirrors'))
    except (OSError,ValueError) as exc:
        return ProbeResult(False,error=type(exc).__name__,stage='tls')
    if 'endpoint' in payload:
        from probe_diagnostics import run_diagnostics
        endpoint=payload['endpoint']
        if fixture: fixture_address(endpoint)
        elif not public_address(endpoint): raise ValueError('NonPublicEndpoint')
        outbound = probe_outbound(candidate,endpoint,fixture)
        return run_diagnostics(payload,outbound,videos,context)
    try:
        addresses = fixture_address(candidate.host) if fixture else resolve_public(candidate.host)
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        return ProbeResult(False,error=type(exc).__name__,stage='dns')
    if len(addresses)>1:
        # Try an alternate address family before more addresses of the preferred family.
        primary_version = ipaddress.ip_address(addresses[0]).version
        alternate = next((i for i,ip in enumerate(addresses) if ipaddress.ip_address(ip).version!=primary_version),None)
        if alternate is not None:
            addresses.insert(1,addresses.pop(alternate))
    addresses = addresses[:2] if quick else addresses[:3]
    failures=[]
    network_deadline=None
    for index,address in enumerate(addresses):
        try:
            outbound = probe_outbound(candidate,address,fixture)
        except (OSError,ValueError,subprocess.SubprocessError) as exc:
            return ProbeResult(False,error=type(exc).__name__,stage='dns')
        result,network_deadline = _probe_endpoint(payload,outbound,address,probes,videos,context,
                                                   network_deadline,full_deadline,len(addresses)-index)
        if result.passed:
            result.attempts += sum(row.attempts for row in failures)
            return result
        failures.append(result)
        if min(network_deadline or full_deadline,full_deadline)-time.monotonic()<0.25:
            break
    best=max(failures,key=lambda row:row.successes)
    best.attempts=sum(row.attempts for row in failures)
    return best


def _probe_endpoint(payload,outbound,address,probes,videos,context,network_deadline,full_deadline,remaining_addresses):
    successes = []; attempts = 0; last_error = ''
    quick = payload.get('quick')
    connect_host=(payload.get('fixture') or {}).get('connect_host')
    stage = 'connect'
    video_probe_index=-1
    p = allocate_port()
    with tempfile.TemporaryDirectory(prefix='xray-check-',dir=payload.get('work_dir'),ignore_cleanup_errors=True) as tmp:
        cfg = Path(tmp)/'config.json'
        cfg.write_text(json.dumps(make_config(outbound,p)), encoding='utf-8')
        proc = None
        try:
            proc = subprocess.Popen([payload['xray_bin'],'run','-c',str(cfg)], cwd=tmp,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            try:
                check_ready(p,proc,timeout=max(0.1,min(4,(network_deadline or full_deadline)-time.monotonic())))
            except (OSError,RuntimeError) as exc:
                return ProbeResult(False,error=type(exc).__name__,stage='startup',endpoint_ip=address),network_deadline
            network_timeout = payload.get('screen_timeout')
            if network_timeout is not None and network_deadline is None:
                network_deadline=time.monotonic()+network_timeout
                print('READY',flush=True)
            budget = max(0.1,min(network_deadline or full_deadline,full_deadline)-time.monotonic())
            per_probe_timeout = min(6,budget/(remaining_addresses*len(probes))) if probes else 1
            for url, minimum in probes:
                if not quick and not payload.get('fixture') and not any(
                        urlsplit(video).hostname!=urlsplit(url).hostname for video in videos):
                    continue
                attempts += 1
                try:
                    body, elapsed = https_get_via_socks(p,url,min(per_probe_timeout,payload['timeout']),context,
                                                      expected_status=204 if minimum==0 else 200,connect_host=connect_host)
                    if len(body)<minimum:
                        raise OSError('ShortBody')
                    successes.append((url, len(body), elapsed))
                    break
                except (OSError, ValueError, http.client.HTTPException) as exc:
                    last_error = type(exc).__name__
                    stage = 'tls' if isinstance(exc,ssl.SSLError) else 'connect' if isinstance(exc,SocksError) else 'https'
            if successes and not quick:
                last_error = 'VideoVerificationFailed'
                stage = 'video'
                video_timeout=min(8,budget/(remaining_addresses*len(videos))) if videos else 1
                for index,url in enumerate(videos):
                    if not payload.get('fixture') and urlsplit(url).hostname==urlsplit(successes[0][0]).hostname:
                        continue
                    attempts += 1
                    try:
                        body, elapsed = https_get_via_socks(p,url,video_timeout,context,
                                                           max_bytes=VIDEO_BYTES,video_range=True,connect_host=connect_host)
                        successes.append((url, len(body), elapsed))
                        video_probe_index=index
                        break
                    except (OSError, ValueError, http.client.HTTPException):
                        continue
            if len(successes)<(1 if quick else 2):
                return (ProbeResult(False,error=last_error or 'NoSuccessfulHTTPSProbe',stage=stage,
                                    attempts=attempts,successes=len(successes),endpoint_ip=address),network_deadline)
            times = [t*1000 for _,_,t in successes]
            transferred = sum(n for _,n,_ in successes)
            elapsed = sum(t for _,_,t in successes)
            hosts = tuple(urlsplit(u).hostname for u,_,_ in successes)
            return (ProbeResult(True, round(statistics.median(times),1), transferred,
                               round(transferred*8/1000/max(elapsed,0.001),1),
                               not quick, endpoint_ip=address,stage='passed',
                               successes=len(successes), attempts=attempts,
                               jitter_ms=round(max(times)-min(times),1),
                               probe_hosts=hosts,video_host='' if quick else hosts[-1],
                               video_bytes=0 if quick else successes[-1][1],
                               video_ms=0 if quick else round(times[-1],1),video_probe_index=video_probe_index),network_deadline)
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
        result = _probe_worker(json.loads(sys.stdin.read()))
        print(json.dumps(asdict(result) if isinstance(result,ProbeResult) else result))
    except Exception as exc:
        # Never print a source URL, credential, or untrusted Xray log.
        print(json.dumps(asdict(ProbeResult(False,error=type(exc).__name__))))
