"""Validate and pin standalone Xray outbounds imported for private testing."""
from copy import deepcopy
import ipaddress
import json
import re
from urllib.parse import urlsplit


PROTOCOLS = {'http','hysteria','shadowsocks','socks','trojan','vless','vmess','wireguard'}
SECURITY_FIELDS = {
    'tls': ('tlsSettings', {'serverName','fingerprint','alpn','echConfigList','echForceQuery',
                           'pinnedPeerCertSha256','verifyPeerCertByName','minVersion','maxVersion',
                           'cipherSuites','curvePreferences','enableSessionResumption'}),
    'reality': ('realitySettings', {'serverName','fingerprint','shortId','password','publicKey',
                                    'mldsa65Verify','spiderX','show'}),
}
TRANSPORT_FIELDS = {
    'rawSettings': {'header'},
    'tcpSettings': {'header'},
    'wsSettings': {'host','path','headers','heartbeatPeriod'},
    'grpcSettings': {'authority','serviceName','multiMode','idle_timeout','health_check_timeout',
                     'permit_without_stream','initial_windows_size','user_agent'},
    'httpupgradeSettings': {'host','path','headers'},
    'kcpSettings': {'mtu','tti','uplinkCapacity','downlinkCapacity','congestion',
                    'readBufferSize','writeBufferSize'},
    'hysteriaSettings': {'version','auth','udpIdleTimeout'},
}
XHTTP_FIELDS = {'host','path','mode','headers','xPaddingBytes','xPaddingObfsMode','xPaddingKey',
                'xPaddingHeader','xPaddingPlacement','xPaddingMethod','uplinkHTTPMethod',
                'sessionPlacement','sessionKey','seqPlacement','seqKey','uplinkDataPlacement',
                'uplinkDataKey','uplinkChunkSize','noGRPCHeader','noSSEHeader','scMaxEachPostBytes',
                'scMinPostsIntervalMs','scMaxBufferedPosts','scStreamUpServerSecs',
                'serverMaxHeaderBytes','xmux','downloadSettings'}


def fields(value, allowed):
    if not isinstance(value,dict) or value.keys()-allowed:
        raise ValueError('Unsupported Xray fields')
    return value


def endpoint(address, port):
    if not isinstance(address,str) or not address or type(port) is not int or not 1<=port<=65535:
        raise ValueError('Invalid outbound endpoint')
    host = address.removeprefix('[').removesuffix(']')
    if '%' in host:
        raise ValueError('Invalid outbound endpoint')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        hostname = host.rstrip('.').encode('idna').decode('ascii')
        if len(hostname)>253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?',label)
                                        for label in hostname.split('.')):
            raise ValueError('Invalid outbound endpoint') from None
    return host, port


def primary_endpoint(outbound):
    settings = outbound['settings']
    if outbound['protocol']=='wireguard':
        peer = urlsplit('//'+settings['peers'][0]['endpoint'])
        if peer.username is not None or peer.path or peer.query or peer.fragment:
            raise ValueError('Invalid WireGuard endpoint')
        return endpoint(peer.hostname,peer.port)
    server = (settings.get('vnext') or settings.get('servers') or [settings])[0]
    return endpoint(server['address'],server['port'])


def download_settings(stream):
    for name in ('xhttpSettings','splithttpSettings'):
        if name in stream:
            settings = stream[name]
            return settings.get('extra',settings).get('downloadSettings')
    return None


def validate_fixture_endpoint(host):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError('FixturesMustBeLoopback')
    return [host]


def pin_import_endpoints(outbound, address, resolve_public):
    node = deepcopy(outbound)
    settings = node['settings']
    if node['protocol']=='wireguard':
        _, port = primary_endpoint(node)
        host = f'[{address}]' if ':' in address else address
        settings['peers'][0]['endpoint'] = f'{host}:{port}'
    else:
        (settings.get('vnext') or settings.get('servers') or [settings])[0]['address'] = address
    download = download_settings(node.get('streamSettings',{}))
    if download:
        host, _ = endpoint(download['address'],download['port'])
        download['address'] = resolve_public(host)[0]
    return node


def check_stream(stream, host, *, secondary=False):
    allowed = {'network','security','tlsSettings','realitySettings',*TRANSPORT_FIELDS,
               'xhttpSettings','splithttpSettings'}
    if secondary:
        allowed |= {'address','port'}
    fields(stream,allowed)
    network = stream.get('network','tcp')
    security = stream.get('security','none')
    transport_by_network = {'raw':{'rawSettings','tcpSettings'},'tcp':{'rawSettings','tcpSettings'},
                            'ws':{'wsSettings'},'grpc':{'grpcSettings'},'kcp':{'kcpSettings'},
                            'hysteria':{'hysteriaSettings'},'httpupgrade':{'httpupgradeSettings'},
                            'xhttp':{'xhttpSettings','splithttpSettings'},
                            'splithttp':{'xhttpSettings','splithttpSettings'}}
    if network not in transport_by_network or security not in ('none',*SECURITY_FIELDS):
        raise ValueError('Unsupported Xray transport or security')
    transports = stream.keys() & (TRANSPORT_FIELDS.keys() | {'xhttpSettings','splithttpSettings'})
    if len(transports)>1 or transports-transport_by_network[network]:
        raise ValueError('Conflicting transport settings')
    if secondary and (network not in ('xhttp','splithttp') or security=='none'):
        raise ValueError('Split download requires an explicit secured XHTTP transport')
    for kind,(name,allowed_fields) in SECURITY_FIELDS.items():
        if kind!=security:
            if name in stream:
                raise ValueError('Conflicting security settings')
            continue
        params=fields(stream.setdefault(name,{}),allowed_fields)
        params.setdefault('serverName',host)
        if str(params.get('fingerprint','')).lower()=='unsafe':
            raise ValueError('Unsafe TLS fingerprint')
    for name in transports & TRANSPORT_FIELDS.keys():
        params = fields(stream[name],TRANSPORT_FIELDS[name])
        if name in ('rawSettings','tcpSettings') and 'header' in params:
            if params['header'] != {'type':'none'}:
                raise ValueError('Unsupported RAW header')
    for name in transports & {'xhttpSettings','splithttpSettings'}:
        config = fields(stream[name],XHTTP_FIELDS | {'extra'})
        extra = fields(config.get('extra',{}), XHTTP_FIELDS-{'host','path','mode'})
        if extra and config.keys()-{'host','path','mode','extra'}:
            raise ValueError('Ambiguous XHTTP extra')
        download = download_settings(stream)
        if download is not None:
            if secondary or config.get('mode')=='stream-one' or not isinstance(download,dict):
                raise ValueError('Invalid XHTTP download settings')
            target, _ = endpoint(download.get('address'),download.get('port'))
            check_stream(download,target,secondary=True)
    return security


def check_settings(outbound):
    protocol = outbound['protocol']
    settings = outbound['settings']
    if protocol=='wireguard':
        fields(settings,{'secretKey','address','peers','mtu','workers','reserved','domainStrategy','noKernelTun'})
        peers = settings.get('peers')
        if not isinstance(peers,list) or len(peers)!=1:
            raise ValueError('WireGuard requires one peer')
        fields(peers[0],{'publicKey','preSharedKey','endpoint','keepAlive','allowedIPs'})
        if settings.get('noKernelTun',True) is not True or 'streamSettings' in outbound:
            raise ValueError('WireGuard import requires a userspace tunnel')
        settings['noKernelTun']=True
        for address in settings.get('address',[]):
            ipaddress.ip_interface(address)
        for network in peers[0].get('allowedIPs',[]):
            ipaddress.ip_network(network,strict=False)
        return

    server_list = 'vnext' if protocol in ('vless','vmess') else 'servers'
    protocol_fields = {
        'http': {'user','pass','headers'}, 'socks': {'user','pass'}, 'trojan': {'password'},
        'shadowsocks': {'method','password','uot','uotVersion'}, 'hysteria': {'version'},
        'vless': {'id','flow','encryption','testseed','reverse'}, 'vmess': {'id','security','experiments'}
    }[protocol]
    protocol_fields -= {'reverse'}
    if server_list in settings:
        fields(settings,{server_list,'headers'} if protocol=='http' else {server_list})
        servers=settings[server_list]
        if not isinstance(servers,list) or len(servers)!=1 or protocol=='hysteria':
            raise ValueError('An outbound requires one server')
        server = fields(servers[0],{'address','port','users'} if protocol in ('vless','vmess') else
                        {'address','port','level','email','users'} | protocol_fields)
        if 'users' in server:
            users=server['users']
            if protocol not in ('vless','vmess','http','socks') or not isinstance(users,list) or len(users)!=1:
                raise ValueError('An outbound requires one account')
            fields(users[0],protocol_fields | {'email','level','alterId'} if protocol=='vmess'
                   else protocol_fields | {'email','level'})
            if protocol=='vmess' and users[0].get('alterId',0)!=0:
                raise ValueError('Legacy VMess authentication is unsupported')
    else:
        fields(settings,{'address','port','level','email'} | protocol_fields)


def check_outbound(value):
    outbound = deepcopy(fields(value,{'protocol','settings','streamSettings','tag'}))
    outbound.pop('tag',None)
    protocol = outbound['protocol']
    if protocol not in PROTOCOLS:
        raise ValueError('Unsupported Xray outbound')
    check_settings(outbound)
    host, port = primary_endpoint(outbound)
    stream = outbound.get('streamSettings',{})
    security = check_stream(stream,host)
    if protocol in ('http','trojan','hysteria') and security=='none':
        raise ValueError('Secure transport required')
    if protocol=='http' and security!='tls':
        raise ValueError('HTTPS CONNECT requires TLS')
    return outbound, host, port


def unique_pairs(pairs):
    value = dict(pairs)
    if len(value)!=len(pairs):
        raise ValueError('Duplicate JSON fields')
    return value


def read_outbounds(text):
    try:
        data = json.loads(text.lstrip('\ufeff \r\n\t'),object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: None)
    except (ValueError,RecursionError):
        raise ValueError('Invalid Xray JSON') from None
    if isinstance(data,dict) and 'outbounds' in data:
        data = data['outbounds']
    elif isinstance(data,dict):
        data = [data]
    if not isinstance(data,list) or not data:
        raise ValueError('Expected an Xray outbound or a list of outbounds')
    for index, value in enumerate(data,1):
        try:
            yield check_outbound(value)
        except (AttributeError,IndexError,KeyError,TypeError,ValueError,UnicodeError,RecursionError):
            raise ValueError(f'Invalid or unsupported Xray outbound at position {index}') from None
