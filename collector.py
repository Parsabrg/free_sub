"""Fetch and safely parse public share-link subscriptions (Python standard library)."""
from __future__ import annotations
import base64
import hashlib
import json
import urllib.parse as up
import urllib.request
from dataclasses import dataclass

SOURCES = {
    'Barry': 'https://raw.githubusercontent.com/barry-far/V2ray-Config/main/All_Configs_Sub.txt',
    'Epodonios': 'https://raw.githubusercontent.com/Epodonios/v2ray-configs/main/All_Configs_Sub.txt',
    'Shadowmere': 'https://shadowmere.xyz/api/b64sub/',
    # Additional public feeds. They are candidate sources, NOT trusted or pre-verified by us.
    'Radikal': 'https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/verified/configs.txt',
    'Pawdroid': 'https://raw.githubusercontent.com/Pawdroid/Free-servers/main/sub',
    'MahdiblandPublic': 'https://raw.githubusercontent.com/mahdibland/V2RayAggregator/master/Eternity.txt',
    'Patterniha': 'https://raw.githubusercontent.com/patterniha/Free-Configs/main/configs.txt',
    'Whitedns': 'https://raw.githubusercontent.com/iampedii/whitedns-sub/refs/heads/main/base64.txt',
    'Kahrobas': 'https://bot.kahrobas.shop/sub.txt',
    # 2026-10-26: EbraSha's repository was deleted (404). These add 548, 228 and 56 configs
    # no other feed carries; the tests decide whether any of them works.
    'ALIILAPRO': 'https://raw.githubusercontent.com/ALIILAPRO/v2rayNG-Config/main/server.txt',
    'Ermaozi': 'https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/v2ray.txt',
    'MatinGhanbari': 'https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/v2ray/super-sub.txt',
}
SCHEMES = ('vless://', 'vmess://', 'trojan://', 'ss://')
ACCEPTED_SS = {'aes-128-gcm','aes-256-gcm','chacha20-ietf-poly1305','2022-blake3-aes-128-gcm','2022-blake3-aes-256-gcm'}
MAX_FETCH = 8 * 1024 * 1024

@dataclass(frozen=True)
class Candidate:
    url: str
    outbound: dict
    key: str
    protocol: str
    host: str
    port: int
    source: str

def b64decode(value: str) -> str:
    value = ''.join(value.split()).replace('-', '+').replace('_', '/')
    return base64.b64decode(value + '=' * (-len(value) % 4), validate=True).decode('utf-8')

def get(url, timeout=20):
    request = urllib.request.Request(url, headers={'User-Agent': 'LocalXraySubscription/1.0 (+public feeds)'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_FETCH + 1)
        if len(data) > MAX_FETCH:
            raise ValueError('Source exceeded 8 MiB limit')
    return data.decode('utf-8-sig')

def links(data):
    # Accept both plain-text share links and Base64-encoded subscription files.
    stripped = data.strip()
    if not any(s in stripped for s in SCHEMES) and len(stripped) > 30:
        try:
            decoded = b64decode(stripped)
            if any(s in decoded for s in SCHEMES):
                data = decoded
        except (ValueError, UnicodeError):
            pass
    result=[]
    for line in data.splitlines():
        line = line.strip().strip('`\ufeff')
        if line.startswith(SCHEMES):
            result.append(line)
    return result

def nonempty(q, key, default=''):
    return q.get(key, [default])[0] or default

def boolish(s):
    return str(s).lower() in ('1', 'true', 'yes')

def stream_settings(network, q, tls_mode, sni_default):
    n = (network or 'tcp').lower()
    if n in ('raw',): n='tcp'
    if n not in ('tcp','ws','grpc','httpupgrade','xhttp','splithttp'):
        raise ValueError('Unsupported transport '+n)
    if nonempty(q,'serviceName') and n!='grpc': raise ValueError('serviceName outside gRPC')
    if n=='tcp' and (nonempty(q,'host') or nonempty(q,'path','/') not in ('','/')):
        raise ValueError('Unsupported RAW header settings')
    cfg = {'network':n, 'security':tls_mode}
    if tls_mode == 'tls':
        tls={'serverName':nonempty(q,'sni',sni_default)}
        fp=nonempty(q,'fp'); alpn=nonempty(q,'alpn')
        if fp: tls['fingerprint'] = fp
        if alpn: tls['alpn'] = [a for a in alpn.split(',') if a]
        cfg['tlsSettings']=tls
    elif tls_mode == 'reality':
        pbk=nonempty(q,'pbk'); sni=nonempty(q,'sni',sni_default)
        if not pbk or not sni: raise ValueError('Missing REALITY public key/SNI')
        reality={'show':False,'serverName':sni,'publicKey':pbk,
                 'shortId':nonempty(q,'sid'), 'fingerprint':nonempty(q,'fp','chrome'),
                 'spiderX':nonempty(q,'spx','/')}
        cfg['realitySettings']=reality
    elif tls_mode != 'none':
        raise ValueError('Unknown transport security')
    path=nonempty(q,'path','/')
    host=nonempty(q,'host')
    if n=='ws':
        cfg['wsSettings']={'path':path}
        if host: cfg['wsSettings']['headers']={'Host':host}
    if n=='grpc':
        cfg['grpcSettings']={'serviceName':nonempty(q,'serviceName',path.lstrip('/')),
                             'multiMode':nonempty(q,'mode','gun') == 'multi'}
    if n=='httpupgrade':
        cfg['httpupgradeSettings']={'path':path,'host':host or sni_default}
    if n in ('xhttp','splithttp'):
        cfg['network']='xhttp'
        cfg['xhttpSettings']={'path':path,'host':host or sni_default}
        mode=nonempty(q,'mode')
        if mode: cfg['xhttpSettings']['mode']=mode
    if n=='h2':
        cfg['httpSettings']={'path':path,'host':[host] if host else []}
    return cfg

# Connection-affecting extensions must be implemented or rejected, never silently dropped.
URL_KEYS = {'security','type','sni','fp','alpn','pbk','sid','spx','path','host',
            'serviceName','mode','flow','encryption','allowInsecure','insecure','headerType'}
VMESS_KEYS = {'v','ps','add','port','id','aid','scy','net','type','host','path',
              'tls','sni','servername','alpn','fp','mode','allowInsecure','insecure'}


def parse(link, source='unknown'):
    try:
        if not isinstance(link, str) or len(link)>16384: return None
        scheme=link.split('://',1)[0].lower()
        if scheme not in ('vless','vmess','trojan','ss'): return None
        if scheme=='vmess':
            payload=link.split('://',1)[1].split('#',1)[0]
            obj=json.loads(b64decode(payload))
            if not isinstance(obj,dict) or set(obj)-VMESS_KEYS: return None
            if any(boolish(obj.get(k,False)) for k in ('allowInsecure','insecure')): return None
            if obj.get('type','none') not in ('','none'): return None
            host=str(obj.get('add','')).strip(); port=int(obj.get('port',0))
            uid=str(obj.get('id','')).strip()
            if not uid: return None
            # vmess has its own authenticated encryption, even without transport TLS.
            settings={'vnext':[{'address':host,'port':port,'users':[
                {'id':uid,'alterId':int(obj.get('aid') or 0), 'security':obj.get('scy') or 'auto'}]}]}
            outbound={'protocol':'vmess','settings':settings}
            q={'sni':[str(obj.get('sni') or obj.get('servername') or host)],
               'fp':[str(obj.get('fp') or '')], 'alpn':[str(obj.get('alpn') or '')],
               'path':[str(obj.get('path') or '/')], 'host':[str(obj.get('host') or '')],
               'serviceName':[str(obj.get('path') or '') if obj.get('net')=='grpc' else ''],
               'mode':[str(obj.get('mode') or '')]}
            sec='tls' if str(obj.get('tls','')).lower() in ('tls','1') else 'none'
            if sec=='none': return None   # require transport-layer authentication
            outbound['streamSettings']=stream_settings(obj.get('net','tcp'),q,sec,host)
        elif scheme=='ss':
            u=up.urlsplit(link)
            if u.query: return None  # Xray does not support external SS plugins here
            if '@' in u.netloc:
                credentials,server=u.netloc.rsplit('@',1)
                if ':' in up.unquote(credentials): method_password=up.unquote(credentials)
                else: method_password=b64decode(up.unquote(credentials))
            else:
                decoded=b64decode(u.netloc)
                method_password,server=decoded.rsplit('@',1)
            if ':' not in method_password: return None
            method,password=method_password.split(':',1)
            if method.lower() not in ACCEPTED_SS or not password: return None
            parsed=up.urlsplit('ss://'+server)
            host=parsed.hostname or ''; port=int(parsed.port or 0)
            outbound={'protocol':'shadowsocks','settings':{'servers':[{'address':host,'port':port,'method':method,'password':password}]}}
        else:
            u=up.urlsplit(link); host=u.hostname or ''; port=int(u.port or 0)
            password=up.unquote(u.username or '')
            if not password: return None
            q=up.parse_qs(u.query,keep_blank_values=True)
            if set(q)-URL_KEYS or any(len(v)!=1 for v in q.values()): return None
            if nonempty(q,'headerType','none')!='none': return None
            if nonempty(q,'encryption','none')!='none': return None
            if u.password is not None: return None
            n=nonempty(q,'type','tcp').lower()
            if nonempty(q,'mode') and n not in ('grpc','xhttp','splithttp'): return None
            if n=='grpc' and nonempty(q,'mode','gun') not in ('gun','multi'): return None
            sec=nonempty(q,'security','tls' if scheme=='trojan' else 'none').lower()
            # Never silently disable TLS/REALITY verification to make a result pass.
            if boolish(nonempty(q,'allowInsecure')) or boolish(nonempty(q,'insecure')): return None
            if sec not in ('tls','reality'): return None
            network=nonempty(q,'type','tcp')
            if scheme=='vless':
                flow=nonempty(q,'flow')
                user={'id':password,'encryption':'none'}
                if flow: user['flow']=flow
                outbound={'protocol':'vless','settings':{'vnext':[{'address':host,'port':port,'users':[user]}]}}
            else:
                outbound={'protocol':'trojan','settings':{'servers':[{'address':host,'port':port,'password':password}]}}
            outbound['streamSettings']=stream_settings(network,q,sec,host)
        if not host or not (1<=port<=65535): return None
        # Dedup based on effective Xray config, not node title/#fragment.
        key=hashlib.sha256(json.dumps(outbound,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return Candidate(link,outbound,key,scheme,host,port,source)
    except (ValueError,TypeError,AttributeError,KeyError,OverflowError,UnicodeError,json.JSONDecodeError):
        return None

def collect(source_map=SOURCES, per_source=0, stats=None):
    groups={}
    for name,url in source_map.items():
        try:
            raw=get(url)
            original=links(raw)
            seen=set(); group=[]
            for entry in original:
                candidate=parse(entry,name)
                if candidate and candidate.key not in seen:
                    seen.add(candidate.key)
                    group.append(candidate)
                if per_source and len(group)>=per_source: break
            groups[name]=group
            if stats is not None: stats[name]={'raw':len(original),'supported':len(group),'error':None if group else 'NoSupportedLinks'}
            print(f'[SOURCE] {name}: {len(original)} raw, {len(group)} supported unique',flush=True)
        except Exception as e:
            groups[name]=[]
            if stats is not None: stats[name]={'raw':0,'supported':0,'error':type(e).__name__}
            print(f'[SOURCE] {name}: FAILED ({type(e).__name__})',flush=True)
    return groups
