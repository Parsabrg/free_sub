"""Rename successful proxy *share links*, preserving their connection credentials.

Legacy VMess uses the `ps` field embedded in its Base64 JSON. URI formats
use a URL-encoded fragment label.
"""
from __future__ import annotations
import base64
import json
from urllib.parse import quote, unquote, urlsplit, urlunsplit


TRANSPORTS = {'kcp': 'MKCP', 'ws': 'WS', 'grpc': 'GRPC', 'httpupgrade': 'HTTPUPGRADE', 'xhttp': 'XHTTP'}
PROTOCOLS = {'shadowsocks': 'SS', 'hysteria': 'HYSTERIA2', 'socks': 'SOCKS5'}


def describe(outbound: dict) -> str:
    """VLESS-WS-TLS, VLESS-REALITY, TROJAN-TLS, SS: what the tested outbound actually is."""
    stream = outbound.get('streamSettings') or {}
    parts = [PROTOCOLS.get(outbound['protocol'], outbound['protocol'].upper())]
    if stream.get('network') in TRANSPORTS:
        parts.append(TRANSPORTS[stream['network']])
    if stream.get('security') in ('tls', 'reality'):
        parts.append(stream['security'].upper())
    return '-'.join(parts)


def node_name(flag: str, country_code: str | None, number: int, outbound: dict) -> str:
    """The usual subscription form, e.g. "🇩🇪 DE-07 | VLESS-WS-TLS"; XX is an unknown country."""
    if number < 1:
        raise ValueError('Numbers start at 1')
    return f'{flag} {country_code or "XX"}-{number:02d} | {describe(outbound)}'


def assign_labels(selected, nodes, now, hold_seconds=86400):
    """Give each listed node a small number within its country and keep it between runs.

    The old names carried one counter for every config ever tested (server 🇳🇱 50186).
    A number stays reserved for a day after its node last worked, so a node that
    misses one health check comes back under the same name."""
    taken = {}
    listed = {candidate.key for candidate, _ in selected}
    for key, row in nodes.items():
        label = row.get('label')
        if not label:
            continue
        code = (row.get('country_code') or 'XX').upper()
        current = key in listed or now - row.get('last_success', 0) < hold_seconds
        if label[0] != code or not current or label[1] in taken.setdefault(code, set()):
            row['label'] = None
        else:
            taken[code].add(label[1])
    for candidate, _ in selected:
        row = nodes[candidate.key]
        if row.get('label'):
            continue
        code = (row.get('country_code') or 'XX').upper()
        used = taken.setdefault(code, set())
        number = next(n for n in range(1, len(used) + 2) if n not in used)
        used.add(number)
        row['label'] = [code, number]


def rename_share_link(url: str, name: str) -> str:
    scheme, sep, rest = url.partition('://')
    if not sep:
        raise ValueError('Not a share link')
    scheme = scheme.lower()
    payload = rest.split('#', 1)[0]
    if scheme == 'vmess' and '@' not in payload:
        # The VMess label is JSON.ps. Appending #name breaks some clients.
        raw = payload.replace('-', '+').replace('_', '/')
        config = json.loads(base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True))
        if not isinstance(config, dict):
            raise ValueError('Bad VMess payload')
        config['ps'] = name
        encoded = base64.b64encode(json.dumps(config, ensure_ascii=False,
                                               separators=(',', ':')).encode('utf-8')).decode('ascii')
        return 'vmess://' + encoded
    if scheme in ('vless', 'vmess', 'trojan'):
        uri = urlsplit(scheme + '://' + payload)
        if uri.username is None or uri.password is not None:
            raise ValueError('Invalid share-link credentials')
        # Raw @ and + in passwords break .NET URI import or URL decoding.
        authority = quote(unquote(uri.username), safe='') + '@' + uri.netloc.rsplit('@', 1)[1]
        return urlunsplit((scheme, authority, uri.path, uri.query, quote(name, safe='')))
    if scheme in ('ss','hysteria2','hy2','socks','socks5','socks5h'):
        return scheme + '://' + payload + '#' + quote(name, safe='')
    raise ValueError('Unsupported share link scheme')
