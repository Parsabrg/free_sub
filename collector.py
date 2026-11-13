"""Fetch and safely parse public share-link subscriptions (Python standard library)."""
from __future__ import annotations
import base64
import concurrent.futures as cf
import hashlib
import html
import json
import re
import urllib.error
import urllib.parse as up
import urllib.request
from dataclasses import dataclass

SOURCES = {
    'Barry': 'https://raw.githubusercontent.com/barry-far/V2ray-Config/main/All_Configs_Sub.txt',
    'Shadowmere': 'https://shadowmere.xyz/api/b64sub/',
    # Additional public feeds. They are candidate sources, NOT trusted or pre-verified by us.
    # 2026-11-12: Epodonios/v2ray-configs (eight feeds here) answers 404 like EbraSha before it.
    # sources.py finds replacements by itself, and finds it again should it come back.
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
    # 2026-10-26: 16 active collectors were screened from Iran on the 6,625 configs they add;
    # only these four had any that worked (7, 6, 1 and 1). The other twelve had none.
    'RadikalAll': 'https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main/all/configs.txt',
    'Bahemmat': 'https://raw.githubusercontent.com/MohammadBahemmat/V2ray-Collector/main/all_servers.txt',
    'Hamedcode443': 'https://raw.githubusercontent.com/hamedcode/port-based-v2ray-configs/main/sub/port_443.txt',
    'MorpheusIran': 'https://raw.githubusercontent.com/morpheusadam/v2ray-config/main/subs/bundles/iran-base64.txt',
}
SCHEMES = ('vless://', 'vmess://', 'trojan://', 'ss://', 'hysteria2://', 'hy2://',
           'socks://', 'socks5://', 'socks5h://')
ACCEPTED_SS = {'aes-128-gcm','aes-256-gcm','chacha20-ietf-poly1305','xchacha20-ietf-poly1305',
               '2022-blake3-aes-128-gcm','2022-blake3-aes-256-gcm','2022-blake3-chacha20-poly1305'}
VMESS_CIPHERS = {'auto','aes-128-gcm','chacha20-poly1305','none','zero'}
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
    raw: bool = False

def b64decode(value: str) -> str:
    value = ''.join(value.split()).replace('-', '+').replace('_', '/')
    return base64.b64decode(value + '=' * (-len(value) % 4), validate=True).decode('utf-8')

# relay.fallback() sets this for the length of a discovery: get(url, timeout) through a verified node.
FALLBACK = None

def direct_get(url, timeout=20):
    request = urllib.request.Request(url, headers={'User-Agent': 'LocalXraySubscription/1.0 (+public feeds)'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_FETCH + 1)
        if len(data) > MAX_FETCH:
            raise ValueError('Source exceeded 8 MiB limit')
    return data.decode('utf-8-sig')

def get(url, timeout=20):
    try:
        return direct_get(url, timeout)
    except ValueError:
        raise  # too large or not text: the same through any node
    except Exception as exc:
        # An answer from the server is final, unless it refuses this address (403, 429).
        final = isinstance(exc, urllib.error.HTTPError) and exc.code not in (403, 429)
        if FALLBACK is None or final:
            raise
        try:
            return FALLBACK(url, timeout)
        except Exception:
            raise exc from None  # report what went wrong directly, not the relay's trouble

def links(data):
    # Accept both plain-text share links and Base64-encoded subscription files.
    stripped = data.strip()
    if not any(s in stripped.lower() for s in SCHEMES) and len(stripped) > 30:
        try:
            decoded = b64decode(stripped)
            if any(s in decoded.lower() for s in SCHEMES):
                data = decoded
        except (ValueError, UnicodeError):
            pass
    result=[]
    for line in data.splitlines():
        line = line.strip().strip('`\ufeff')
        if line.lower().startswith(SCHEMES):
            # Feeds scraped from web pages carry "&amp;" between parameters.
            result.append(html.unescape(line) if '&amp;' in line else line)
    return result

def nonempty(q, key, default=''):
    return q.get(key, [default])[0] or default

def safe_tls_flag(value):
    return value is None or str(value).lower() in ('','0','false','no')

def stream_settings(network, q, tls_mode, sni_default):
    n = (network or 'tcp').lower()
    if n in ('raw',): n='tcp'
    if n not in ('tcp','kcp','ws','grpc','httpupgrade','xhttp','splithttp'):
        raise ValueError('Unsupported transport '+n)
    transport_fields = {'serviceName': ('grpc',), 'authority': ('grpc',), 'mode': ('grpc','xhttp','splithttp'),
                        'extra': ('xhttp','splithttp'), 'mtu': ('kcp',), 'tti': ('kcp',), 'seed': ('kcp',)}
    if any(nonempty(q,key) and n not in allowed for key, allowed in transport_fields.items()):
        raise ValueError('Parameter used outside its transport')
    if n=='grpc' and nonempty(q,'mode','gun') not in ('gun','multi'):
        raise ValueError('Unknown gRPC mode')
    # REALITY derives ALPN from its ClientHello fingerprint; feeds also carry legacy TLS ALPN hints.
    security_fields = {'tls': ('ech','pcs','vcn'), 'reality': ('pbk','sid','spx','pqv')}
    if any(nonempty(q,key) for security, fields in security_fields.items() if tls_mode!=security for key in fields):
        raise ValueError('Parameter used outside its security mode')
    if tls_mode=='none' and any(nonempty(q,key) for key in ('alpn','fp')):
        raise ValueError('TLS parameter used with a clear transport')
    # RAW reads host/path only for an HTTP header, which parse() rejects: with none they
    # configure nothing, yet 610 otherwise valid links were rejected for one (measured 2026-11-12).
    cfg = {'network':n, 'security':tls_mode}
    if tls_mode == 'tls':
        tls={'serverName':nonempty(q,'sni',sni_default)}
        fp=nonempty(q,'fp'); alpn=nonempty(q,'alpn')
        if fp.lower() == 'unsafe': raise ValueError('Unsafe TLS fingerprint')
        if fp: tls['fingerprint'] = fp
        if alpn: tls['alpn'] = [a for a in alpn.split(',') if a]
        pins = nonempty(q,'pcs')
        if pins and any(not re.fullmatch(r'[0-9a-fA-F]{64}', pin.strip().replace(':','')) for pin in pins.split(',')):
            raise ValueError('Invalid SHA-256 certificate pin')
        for key, field in {'ech':'echConfigList','pcs':'pinnedPeerCertSha256','vcn':'verifyPeerCertByName'}.items():
            if nonempty(q,key): tls[field] = nonempty(q,key)
        cfg['tlsSettings']=tls
    elif tls_mode == 'reality':
        pbk=nonempty(q,'pbk'); sni=nonempty(q,'sni',sni_default)
        if not pbk or not sni: raise ValueError('Missing REALITY public key/SNI')
        reality={'show':False,'serverName':sni,'publicKey':pbk,
                 'shortId':nonempty(q,'sid'), 'fingerprint':nonempty(q,'fp','chrome'),
                 'spiderX':nonempty(q,'spx','/')}
        if nonempty(q,'pqv'): reality['mldsa65Verify'] = nonempty(q,'pqv')
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
        authority=nonempty(q,'authority',sni_default if tls_mode=='none' else '')
        if authority: cfg['grpcSettings']['authority']=authority
    if n=='kcp':
        cfg['kcpSettings']={key:int(nonempty(q,key)) for key in ('mtu','tti') if nonempty(q,key)}
        if nonempty(q,'seed'):
            cfg['finalmask']={'udp':[{'type':'mkcp-aes128gcm','settings':{'password':nonempty(q,'seed')}}]}
    if n=='httpupgrade':
        cfg['httpupgradeSettings']={'path':path,'host':host or sni_default}
    if n in ('xhttp','splithttp'):
        cfg['network']='xhttp'
        cfg['xhttpSettings']={'path':path,'host':host or sni_default}
        mode=nonempty(q,'mode')
        if mode: cfg['xhttpSettings']['mode']=mode
        if nonempty(q,'extra'):
            extra=json.loads(nonempty(q,'extra'))
            if not isinstance(extra,dict) or extra.keys()-XHTTP_EXTRA:
                raise ValueError('Unsupported XHTTP extra')
            if 'xmux' in extra and (not isinstance(extra['xmux'],dict) or extra['xmux'].keys()-XMUX_KEYS):
                raise ValueError('Unsupported XHTTP xmux')
            cfg['xhttpSettings']['extra']=extra
    return cfg

# Connection-affecting extensions must be implemented or rejected, never silently dropped.
# One with no value sets nothing: newer v2rayN writes "pcs":"" and "vcn":"" into every
# VMess link, which rejected 1,170 of them (measured 2026-11-12).
URL_KEYS = {'security','type','sni','fp','alpn','pbk','sid','spx','path','host',
            'serviceName','authority','mode','flow','encryption','allowInsecure','insecure','headerType',
            'ech','pcs','vcn','pqv','extra','mtu','tti','seed'}
VMESS_KEYS = {'v','ps','add','port','id','aid','scy','net','type','host','path',
              'tls','sni','servername','alpn','fp','mode','allowInsecure','insecure','authority','ech','pcs','vcn'}
XHTTP_EXTRA = {'headers','xPaddingBytes','xPaddingObfsMode','xPaddingKey','xPaddingHeader',
               'xPaddingPlacement','xPaddingMethod','uplinkHTTPMethod','sessionPlacement',
               'sessionKey','seqPlacement','seqKey','uplinkDataPlacement','uplinkDataKey',
               'uplinkChunkSize','noGRPCHeader','noSSEHeader','scMaxEachPostBytes',
               'scMinPostsIntervalMs','scMaxBufferedPosts','scStreamUpServerSecs','serverMaxHeaderBytes','xmux'}
XMUX_KEYS = {'maxConcurrency','maxConnections','cMaxReuseTimes','hMaxRequestTimes',
             'hMaxReusableSecs','hKeepAlivePeriod'}


def vmess_outbound(host, port, uid, cipher):
    if cipher not in VMESS_CIPHERS:
        raise ValueError('Unknown VMess encryption')
    return {'protocol':'vmess','settings':{'vnext':[{'address':host,'port':port,
            'users':[{'id':uid,'alterId':0,'security':cipher}]}]}}


def link_query(u, allowed):
    if u.path not in ('','/'):
        raise ValueError('Unexpected URI path')
    q=up.parse_qs(u.query,keep_blank_values=True)
    if any(len(v)!=1 for v in q.values()) or any(q[k][0] for k in set(q)-allowed):
        raise ValueError('Unsupported or repeated link parameter')
    if any(not safe_tls_flag(nonempty(q,key)) for key in ('allowInsecure','insecure')):
        raise ValueError('Insecure TLS setting')
    return q


def hysteria_port(u):
    authority=u.netloc.rsplit('@',1)[-1]
    port_list=authority[authority.rfind(']')+1:].partition(':')[2]
    if not port_list:
        return 443, ''
    if not re.fullmatch(r'[0-9]{1,5}(?:-[0-9]{1,5})?(?:,[0-9]{1,5}(?:-[0-9]{1,5})?)*',port_list):
        raise ValueError('Invalid Hysteria port list')
    for span in port_list.split(','):
        low, _, high = span.partition('-')
        if not 1<=int(low)<=int(high or low)<=65535:
            raise ValueError('Invalid Hysteria port range')
    first=int(re.split('[-,]',port_list,maxsplit=1)[0])
    return first, '' if port_list.isdigit() else port_list


def parse(link, source='unknown'):
    try:
        if not isinstance(link, str) or len(link)>16384: return None
        scheme=link.split('://',1)[0].lower()
        if scheme+'://' not in SCHEMES: return None
        payload=link.split('://',1)[1].split('#',1)[0]
        if scheme=='vmess' and '@' not in payload:
            obj=json.loads(b64decode(payload))
            if not isinstance(obj,dict) or any(obj[k] not in ('',None) for k in set(obj)-VMESS_KEYS): return None
            if any(not safe_tls_flag(obj.get(k)) for k in ('allowInsecure','insecure')): return None
            if obj.get('type','none') not in ('','none'): return None
            host=str(obj.get('add','')).strip(); port=int(obj.get('port',0))
            uid=str(obj.get('id','')).strip()
            if not uid or int(obj.get('aid') or 0)!=0: return None
            cipher=obj.get('scy') or 'auto'
            outbound=vmess_outbound(host,port,uid,cipher)
            q={'sni':[str(obj.get('sni') or obj.get('servername') or host)],
               'fp':[str(obj.get('fp') or '')], 'alpn':[str(obj.get('alpn') or '')],
               'path':[str(obj.get('path') or '/')], 'host':[str(obj.get('host') or '')],
               'serviceName':[str(obj.get('path') or '') if obj.get('net')=='grpc' else ''],
               'mode':[str(obj.get('mode') or '')]}
            for key in ('authority','ech','pcs','vcn'):
                if obj.get(key): q[key]=[str(obj[key])]
            sec=str(obj.get('tls') or 'none').lower()
            if sec=='1': sec='tls'
            if sec=='none' and cipher in ('none','zero'): return None
            if sec not in ('tls','none'): return None
            outbound['streamSettings']=stream_settings(obj.get('net','tcp'),q,sec,host)
        elif scheme=='ss':
            u=up.urlsplit(link)
            if u.query or u.path not in ('','/'): return None
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
            outbound={'protocol':'shadowsocks','settings':{'servers':[{'address':host,'port':port,'method':method.lower(),'password':password}]}}
        elif scheme in ('hysteria2','hy2'):
            u=up.urlsplit(link); host=u.hostname or ''; port, hopping=hysteria_port(u)
            q=link_query(u,{'sni','insecure','pinSHA256','obfs','obfs-password'})
            if nonempty(q,'pinSHA256'): q['pcs']=q['pinSHA256']
            stream=stream_settings('tcp',q,'tls',host)
            stream['tlsSettings']['alpn']=['h3']
            stream.update(network='hysteria',hysteriaSettings={'version':2,'auth':up.unquote(u.netloc.rpartition('@')[0])})
            obfs=nonempty(q,'obfs'); obfs_password=nonempty(q,'obfs-password')
            if obfs or obfs_password:
                if obfs!='salamander' or not obfs_password: return None
                stream['finalmask']={'udp':[{'type':'salamander','settings':{'password':obfs_password}}]}
            if hopping:
                # The pinned core's hop timer requires an explicit nonzero interval.
                stream.setdefault('finalmask',{})['quicParams']={'udpHop':{'ports':hopping,'interval':30}}
            outbound={'protocol':'hysteria','settings':{'version':2,'address':host,'port':port},'streamSettings':stream}
            scheme='hysteria2'
        elif scheme.startswith('socks'):
            u=up.urlsplit(link); host=u.hostname or ''; port=int(u.port or 0)
            q=link_query(u,{'security','sni','fp','alpn','ech','pcs','vcn','insecure','allowInsecure'})
            settings={'address':host,'port':port}
            if u.username is not None:
                if not u.username: return None
                settings.update({'user':up.unquote(u.username),'pass':up.unquote(u.password or '')})
            outbound={'protocol':'socks','settings':settings}
            sec=nonempty(q,'security','none')
            if sec not in ('tls','none'): return None
            if sec=='tls':
                outbound['streamSettings']=stream_settings('tcp',q,sec,host)
            elif any(nonempty(q,k) for k in ('sni','fp','alpn','ech','pcs','vcn')): return None
            scheme='socks'
        else:
            u=up.urlsplit(link); host=u.hostname or ''; port=int(u.port or 0)
            password=up.unquote(u.username or '')
            if not password: return None
            q=link_query(u,URL_KEYS)
            if nonempty(q,'headerType','none')!='none': return None
            if u.password is not None: return None
            sec=nonempty(q,'security','tls' if scheme=='trojan' else 'none').lower()
            network=nonempty(q,'type','tcp')
            if scheme=='vless':
                flow=nonempty(q,'flow')
                encryption=nonempty(q,'encryption','none')
                if encryption!='none' and not encryption.startswith('mlkem768x25519plus.'): return None
                if sec=='none' and encryption=='none': return None
                user={'id':password,'encryption':encryption}
                if flow: user['flow']=flow
                outbound={'protocol':'vless','settings':{'vnext':[{'address':host,'port':port,'users':[user]}]}}
            elif scheme=='vmess':
                if nonempty(q,'flow'): return None
                cipher=nonempty(q,'encryption','auto')
                if sec=='none' and cipher in ('none','zero'): return None
                outbound=vmess_outbound(host,port,password,cipher)
            else:
                if sec not in ('tls','reality') or nonempty(q,'flow') or nonempty(q,'encryption','none')!='none': return None
                outbound={'protocol':'trojan','settings':{'servers':[{'address':host,'port':port,'password':password}]}}
            outbound['streamSettings']=stream_settings(network,q,sec,host)
        if not host or not (1<=port<=65535): return None
        # Dedup based on effective Xray config, not node title/#fragment.
        key=hashlib.sha256(json.dumps(outbound,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return Candidate(link,outbound,key,scheme,host,port,source)
    except (ValueError,TypeError,AttributeError,KeyError,OverflowError,UnicodeError,json.JSONDecodeError):
        return None

def import_candidates(text, source='private'):
    """Read a private share-link list or strictly validated standalone Xray outbounds."""
    from xray_import import read_outbounds

    if not isinstance(text,str) or len(text)>MAX_FETCH:
        raise ValueError('Import exceeds the size limit')
    if text.lstrip('\ufeff \r\n\t').startswith(('{','[')):
        candidates = []
        for outbound in read_outbounds(text):
            settings, host, port = outbound
            protocol = {'hysteria':'hysteria2','shadowsocks':'ss'}.get(settings['protocol'],settings['protocol'])
            key=hashlib.sha256(json.dumps(settings,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            candidates.append(Candidate('',settings,key,protocol,host,port,source,raw=True))
    else:
        candidates = [candidate for line in links(text) if (candidate := parse(line,source))]
    return list({candidate.key:candidate for candidate in candidates}.values())


def fetch_source(name, urls, per_source=0):
    """One source's unique supported candidates; a source may publish several files."""
    original=[]; errors=[]
    for url in [urls] if isinstance(urls,str) else list(urls):
        for attempt in (1,2):
            try:
                original.extend(links(get(url)))
                break
            except Exception as e:
                # Resets are common on a filtered network; one more try is cheap.
                if attempt==2: errors.append(type(e).__name__)
    seen=set(); group=[]
    for entry in original:
        candidate=parse(entry,name)
        if candidate and candidate.key not in seen:
            seen.add(candidate.key)
            group.append(candidate)
        if per_source and len(group)>=per_source: break
    error=None if group else (errors[0] if errors else 'NoSupportedLinks')
    return group,{'raw':len(original),'supported':len(group),'error':error}


def collect(source_map=SOURCES, per_source=0, stats=None, workers=8):
    results={}
    if source_map: print(f'[SOURCES] fetching {len(source_map)} sources',flush=True)
    # Fetched side by side: one feed after another took 20 seconds for each dead one.
    with cf.ThreadPoolExecutor(max_workers=max(1,min(workers,len(source_map) or 1))) as executor:
        jobs={executor.submit(fetch_source,name,urls,per_source):name for name,urls in source_map.items()}
        # Reported as each one finishes: waiting in list order let one slow source hide the
        # lines of all that came after it, so the panel looked stuck.
        for job in cf.as_completed(jobs):
            name=jobs[job]
            try:
                group,row=job.result()
            except Exception as e:
                group,row=[],{'raw':0,'supported':0,'error':type(e).__name__}
            results[name]=(group,row)
            if row['error'] and not group: print(f'[SOURCE] {name}: FAILED ({row["error"]})',flush=True)
            else: print(f'[SOURCE] {name}: {row["raw"]} raw, {row["supported"]} supported unique',flush=True)
    groups={}
    for name in source_map:   # results keep the order the sources are listed in
        groups[name],row=results[name]
        if stats is not None: stats[name]=row
    return groups
