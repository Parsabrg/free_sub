"""Gate a release by importing and exercising generated feeds in the two client apps."""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import re
import socket
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector import parse
from main import write_outputs
from network import outbound_endpoint
from publishing import validate_outputs
from state import observe, read_state
from xray_test import ProbeResult, VIDEO_BYTES, allocate_port, check_ready, socks_connect

DESKTOP_VERSION = '7.25.6'
ANDROID_VERSION = '2.3.15'
PACKAGE = 'com.v2ray.ang'
UID = '14a2c062-1234-5678-abcd-0123456789ab'


def xray_cases(xray, root):
    material = json.loads(subprocess.check_output([xray,'tls','cert','--ca','--domain=localhost','--json'],timeout=10))
    cert,key = root/'cert.pem',root/'key.pem'
    cert.write_text('\n'.join(material['certificate'])+'\n',encoding='ascii')
    key.write_text('\n'.join(material['key'])+'\n',encoding='ascii')
    certificates = {'certificateFile':str(cert),'keyFile':str(key)}
    # Xray's certificate pin lets the stock clients verify this ephemeral certificate.
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text(encoding='ascii'))).hexdigest()
    tls = {'security':'tls','tlsSettings':{'certificates':[certificates]}}
    suffix=f'?security=tls&sni=localhost&pcs={pin}'
    password=secrets.token_hex(12)
    hy2={'network':'hysteria','security':'tls','tlsSettings':dict(tls['tlsSettings'],alpn=['h3']),
         'hysteriaSettings':{'version':2}}
    vmess={'id':UID,'aid':'0','add':'127.0.0.1','net':'ws','path':'/fixture','tls':'','scy':'auto'}
    return [
        ('socks',{'auth':'password','accounts':[{'user':'fixture','pass':password}]},{},
         lambda port:f'socks://fixture:{password}@127.0.0.1:{port}'),
        ('shadowsocks',{'method':'aes-256-gcm','password':password,'network':'tcp'},{},
         lambda port:'ss://'+base64.urlsafe_b64encode(f'aes-256-gcm:{password}'.encode()).decode().rstrip('=')+f'@127.0.0.1:{port}'),
        ('vmess',{'clients':[{'id':UID}]},{'network':'ws','wsSettings':{'path':'/fixture'}},
         lambda port:'vmess://'+base64.b64encode(json.dumps(dict(vmess,port=str(port))).encode()).decode()),
        ('vless',{'clients':[{'id':UID}],'decryption':'none'},tls,
         lambda port:f'vless://{UID}@127.0.0.1:{port}{suffix}'),
        ('trojan',{'clients':[{'password':password}]},tls,
         lambda port:f'trojan://{password}@127.0.0.1:{port}{suffix}'),
        ('hysteria',{'version':2,'clients':[{'auth':password}]},hy2,
         lambda port:f'hy2://{password}@127.0.0.1:{port}?sni=localhost&pinSHA256={pin}'),
    ]


def new_origin(stack, folder, nonce=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path=folder/self.path.removeprefix('/')
            if nonce is not None:
                body=nonce.encode()
            elif self.path in ('/xray.json','/sub.b64'):
                body=path.read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self,*args):
            pass

    server=stack.enter_context(ThreadingHTTPServer(('127.0.0.1',0),Handler))
    worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.05},daemon=True)
    worker.start()
    stack.callback(server.shutdown)
    return server.server_port


def setup_fixtures(stack, xray, folder):
    config={'inbounds':[],'outbounds':[{'tag':'blocked','protocol':'blackhole'}],
            'routing':{'rules':[]}}
    profiles=[]
    target_port=allocate_port()
    now=time.time()
    state=read_state(folder/'history.json')
    for protocol,settings,stream,make_link in xray_cases(xray,folder):
        if protocol=='hysteria':
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
                sock.bind(('127.0.0.1',0))
                port=sock.getsockname()[1]
        else:
            port=allocate_port()
        tag='fixture-'+protocol
        nonce=secrets.token_hex(16)
        origin=new_origin(stack,folder,nonce)
        config['inbounds'].append({'listen':'127.0.0.1','port':port,'tag':tag,'protocol':protocol,
                                   'settings':settings,'streamSettings':stream})
        config['outbounds'].append({'tag':tag,'protocol':'freedom',
                                    'settings':{'redirect':f'127.0.0.1:{origin}'}})
        config['routing']['rules'].append({'type':'field','inboundTag':[tag],'outboundTag':tag})
        candidate=parse(make_link(port))
        if candidate is None:
            raise ValueError(f'Generated {protocol} fixture did not parse')
        result=ProbeResult(True,endpoint_ip='127.0.0.1',video_bytes=VIDEO_BYTES)
        observe(state,candidate,result,now)
        profiles.append((candidate,result,(target_port,nonce)))
    path=folder/'server.json'
    path.write_text(json.dumps(config),encoding='utf-8')
    log=stack.enter_context((folder/'xray.log').open('wb'))
    server=subprocess.Popen([xray,'run','-c',str(path)],stdout=log,stderr=subprocess.STDOUT)
    def stop():
        if server.poll() is None:
            server.terminate()
        server.wait(timeout=5)
    stack.callback(stop)
    for inbound in config['inbounds']:
        if inbound['protocol']!='hysteria':
            check_ready(inbound['port'],server)
    docs=folder/'docs'
    selected=[(candidate,result) for candidate,result,_ in profiles]
    report=write_outputs(docs,selected,state,{'mode':'full','health':'ok'},now,3600)
    validate_outputs(docs)
    return docs,[(candidate,row,target) for (candidate,_,target),row in zip(profiles,report['nodes'])]


def check_traffic(socks_port, target):
    # A distinct response for each inbound proves the imported credentials and route worked.
    port,nonce=target
    with socks_connect(socks_port,'example.com',port,8) as sock:
        sock.sendall(b'GET /probe HTTP/1.1\r\nHost: example.com\r\nConnection: close\r\n\r\n')
        response=http.client.HTTPResponse(sock)
        response.begin()
        return response.status==200 and response.read(128)==nonce.encode()


def check_core(xray, folder, profiles):
    from xray_test import make_config

    for candidate,_,nonce in profiles:
        port=allocate_port()
        config=folder/'core-client.json'
        config.write_text(json.dumps(make_config(candidate.outbound,port)),encoding='utf-8')
        with subprocess.Popen([xray,'run','-c',str(config)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL) as server:
            try:
                check_ready(port,server)
                if not check_traffic(port,nonce):
                    raise ValueError(f'Xray fixture failed for {candidate.protocol}')
            finally:
                if server.poll() is None:
                    server.terminate()
                server.wait(timeout=5)


def check_desktop_export(directory, docs):
    expected=json.loads((docs/'xray.json').read_text(encoding='utf-8'))['outbounds']
    imported=[]
    for path in directory.glob('*.json'):
        try:
            imported.append(json.loads(path.read_text(encoding='utf-8-sig')))
        except (OSError,ValueError):
            continue
    if any(outbound not in imported for outbound in expected):
        raise ValueError('Desktop profile settings differ from the generated Xray feed')


def check_android_export(candidate):
    shared=parse(input('Paste this profile’s exported share link, then press Enter. ').strip())
    if shared is None or android_settings(shared)!=android_settings(candidate):
        raise ValueError(f'Android exported settings differ for {candidate.protocol}')


def android_settings(candidate):
    outbound=candidate.outbound
    endpoint=outbound_endpoint(outbound)
    stream=outbound.get('streamSettings',{})
    tls=stream.get('tlsSettings',{})
    ws=stream.get('wsSettings',{})
    hysteria=stream.get('hysteriaSettings',{})
    account=(endpoint.get('users') or [endpoint])[0]
    return (candidate.protocol,candidate.host,candidate.port,stream.get('network','tcp'),
            stream.get('security','none'),tls.get('serverName',''),tls.get('pinnedPeerCertSha256',''),
            ws.get('path',''),(ws.get('headers') or {}).get('Host',''),
            account.get('id'),account.get('security'),endpoint.get('method'),endpoint.get('password'),
            endpoint.get('user'),endpoint.get('pass'),hysteria.get('auth'))


def android_fixture(docs, profiles, destination):
    """Use the emulator's host gateway so both TCP and QUIC can reach Xray."""
    from collector import b64decode

    lines=(docs/'sub.txt').read_text(encoding='utf-8').splitlines()
    transformed=[]
    for url in lines:
        if url.startswith('vmess://'):
            data=json.loads(b64decode(url.removeprefix('vmess://')))
            data['add']='10.0.2.2'
            url='vmess://'+base64.b64encode(json.dumps(data).encode()).decode()
        else:
            url=url.replace('@127.0.0.1:','@10.0.2.2:',1)
        transformed.append(url)
    destination.write_bytes(base64.b64encode(('\n'.join(transformed)+'\n').encode())+b'\n')
    return [(parse(url),row,target) for url,(_,row,target) in zip(transformed,profiles)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--xray',type=Path,default=ROOT/'tools'/('xray.exe' if os.name=='nt' else 'xray'))
    parser.add_argument('--desktop-pid',type=int,help='PID of the isolated v2rayN desktop app')
    parser.add_argument('--desktop-socks',type=int,default=10808)
    parser.add_argument('--android-serial',help='Serial of a disposable Android emulator')
    parser.add_argument('--android-socks',type=int,default=10808)
    parser.add_argument('--output',type=Path,help='Path to the release-gate JSON evidence')
    parser.add_argument('--core-only',action='store_true',help='Run the generated fixtures through Xray without either GUI client')
    args=parser.parse_args()
    if not args.core_only and (not args.desktop_pid or not args.output or not (args.android_serial or '').startswith('emulator-')):
        parser.error('Specify --desktop-pid, --android-serial for a disposable emulator, and --output')
    binary=str(args.xray.resolve(strict=True))
    version=subprocess.check_output([binary,'version'],text=True,timeout=10).splitlines()[0]
    with ExitStack() as stack:
        folder=Path(stack.enter_context(TemporaryDirectory(prefix='free-sub-client-release-')))
        docs,profiles=setup_fixtures(stack,binary,folder)
        check_core(binary,folder,profiles)
        print(f'{version} passed all {len(profiles)} generated fixture profiles.',flush=True)
        if args.core_only:
            return
        desktop=json.loads(subprocess.check_output(['powershell','-NoProfile','-Command',
            f'Get-Process -Id {args.desktop_pid} | Select-Object Path,MainWindowTitle | ConvertTo-Json -Compress'],
            text=True,timeout=15))
        if not re.search(rf'\bV{re.escape(DESKTOP_VERSION)}\b',desktop.get('MainWindowTitle','')):
            raise SystemExit(f'Start the pinned v2rayN desktop {DESKTOP_VERSION}')
        adb=['adb','-s',args.android_serial]
        info=subprocess.check_output(adb+['shell','dumpsys','package',PACKAGE],text=True,timeout=15)
        if not re.search(rf'^\s*versionName={re.escape(ANDROID_VERSION)}\s*$',info,re.MULTILINE):
            raise SystemExit(f'Install v2rayNG {ANDROID_VERSION} on the emulator')
        feed_port=new_origin(stack,docs)
        android_port=allocate_port()
        subprocess.run(adb+['forward',f'tcp:{android_port}',f'tcp:{args.android_socks}'],check=True,timeout=10)
        stack.callback(subprocess.run,adb+['forward','--remove',f'tcp:{android_port}'],timeout=10)
        android_file=folder/'android.b64'
        android_profiles=android_fixture(docs,profiles,android_file)
        remote='/sdcard/Download/free-sub-release-'+secrets.token_hex(4)+'.b64'
        subprocess.run(adb+['push',str(android_file),remote],check=True,timeout=15)
        stack.callback(subprocess.run,adb+['shell','rm',remote],timeout=10)
        print(f'Import http://127.0.0.1:{feed_port}/xray.json in v2rayN {DESKTOP_VERSION}.')
        print(f'Import {remote} in v2rayNG {ANDROID_VERSION}.')
        print('Set both test clients to Global proxy routing in their test profiles.')
        input('Press Enter after both imports finish. ')
        check_desktop_export(Path(desktop['Path']).parent/'guiConfigs',docs)
        evidence={'xray':version,'v2rayN':DESKTOP_VERSION,'v2rayNG':ANDROID_VERSION,'checks':[]}
        for client,port,rows in (('v2rayN',args.desktop_socks,profiles),('v2rayNG',android_port,android_profiles)):
            for candidate,row,target in rows:
                if client=='v2rayNG':
                    print(f'Share {row["name"]} as a link using v2rayNG.',flush=True)
                    check_android_export(candidate)
                for attempt in range(3):
                    input(f'Select {row["name"]} in {client}, start the proxy, and press Enter. ')
                    try:
                        if check_traffic(port,target):
                            break
                    except (OSError,http.client.HTTPException):
                        pass
                    print('Fixture response did not match. Check the active profile and proxy routing.',flush=True)
                else:
                    raise SystemExit(f'{client} {candidate.protocol} failed the fixture request')
                evidence['checks'].append({'client':client,'protocol':candidate.protocol,'result':'passed'})
        args.output.write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        print(f'Passed {len(evidence["checks"])} client traffic checks.',flush=True)


if __name__=='__main__':
    main()
