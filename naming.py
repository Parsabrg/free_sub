"""Rename successful proxy *share links*, preserving their connection credentials.

VMess uses the `ps` field embedded in its Base64 JSON, not URL fragments.
VLESS, Trojan, and Shadowsocks use a URL-encoded #fragment label.
"""
from __future__ import annotations
import base64
import json
from urllib.parse import quote


def name_for_position(flag: str, position: int) -> str:
    if position < 1:
        raise ValueError('Positions start at 1')
    return f'server {flag} {position}'


def rename_share_link(url: str, name: str) -> str:
    scheme, sep, rest = url.partition('://')
    if not sep:
        raise ValueError('Not a share link')
    scheme = scheme.lower()
    payload = rest.split('#', 1)[0]
    if scheme == 'vmess':
        # The VMess label is JSON.ps. Appending #name breaks some clients.
        raw = payload.replace('-', '+').replace('_', '/')
        config = json.loads(base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True))
        if not isinstance(config, dict):
            raise ValueError('Bad VMess payload')
        config['ps'] = name
        encoded = base64.b64encode(json.dumps(config, ensure_ascii=False,
                                               separators=(',', ':')).encode('utf-8')).decode('ascii')
        return 'vmess://' + encoded
    if scheme in ('vless', 'trojan', 'ss'):
        return scheme + '://' + payload + '#' + quote(name, safe='')
    raise ValueError('Unsupported share link scheme')
