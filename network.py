"""Resolve endpoints out of process, reject private addresses and pin Xray DNS."""
from __future__ import annotations
import copy
import ipaddress
import json
import socket
import subprocess
import sys
from pathlib import Path


def public_address(value):
    try:
        ip = ipaddress.ip_address(value)
        return ip.is_global and not ip.is_multicast and not ip.is_unspecified
    except ValueError:
        return False


def resolve_public(host, timeout=4):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not public_address(str(literal)):
            raise ValueError('NonPublicEndpoint')
        return [str(literal)]
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), host],
                            capture_output=True, text=True, timeout=timeout, check=True)
    addresses = json.loads(result.stdout)
    # Reject mixed public/private answers too. Never pass an unvalidated alternative to Xray.
    if not addresses or any(not public_address(ip) for ip in addresses):
        raise ValueError('NonPublicEndpoint')
    return sorted(addresses,key=lambda ip:ipaddress.ip_address(ip).version)


def pin_endpoint(outbound, addresses):
    node = copy.deepcopy(outbound)
    protocol = node['protocol']
    collection = 'vnext' if protocol in ('vless', 'vmess') else 'servers'
    node['settings'][collection][0]['address'] = addresses[0]
    return node


if __name__ == '__main__':
    try:
        addresses = list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(
            sys.argv[1], None, type=socket.SOCK_STREAM)))
        print(json.dumps(addresses))
    except (OSError, UnicodeError):
        sys.exit(1)
