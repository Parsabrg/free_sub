"""Resolve endpoints out of process, reject private addresses and pin Xray DNS."""
from __future__ import annotations
import copy
import ipaddress
import json
import os
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


# Adapters a VPN or proxy client brings up: never the one to test from.
VPN_ADAPTER_HINTS = ('nebula', 'wintun', 'wireguard', 'tap-windows', 'tap adapter', 'v2ray',
                     'xray', 'sing-box', 'singbox', 'clash', 'tun', 'openvpn', 'warp', 'cloudflare',
                     'hiddify', 'nekoray', 'outline', 'zerotier', 'tailscale', 'hyper-v', 'vethernet')

BIND_ENV = 'FREESUB_BIND_INTERFACE'


def pick_physical(rows):
    """The adapter to test from: a physical one holding a default route, lowest
    metric first. ``rows`` are (name, description, physical, metric)."""
    usable = [r for r in rows if r[2] and not any(
        h in f'{r[0]} {r[1]}'.lower() for h in VPN_ADAPTER_HINTS)]
    return min(usable, key=lambda r: r[3])[0] if usable else None


def physical_interface():
    """This PC's real network adapter (Wi-Fi, Ethernet, USB tethering), or None.

    Tests bind to it so they leave through the operator's network even while a
    VPN (Nebula, v2rayN's TUN, WARP) holds the default route."""
    if os.name != 'nt':
        return None
    script = ("$hw=(Get-NetAdapter -Physical -ErrorAction SilentlyContinue|?{$_.Status -eq 'Up'}).Name;"
              "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue|%{"
              "$a=Get-NetAdapter -InterfaceIndex $_.ifIndex -ErrorAction SilentlyContinue;"
              "$i=Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue;"
              "if($a){'{0}|{1}|{2}|{3}' -f $a.Name,$a.InterfaceDescription,($hw -contains $a.Name),($_.RouteMetric+$i.InterfaceMetric)}}")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    rows = []
    for line in out.splitlines():
        parts = line.split('|')
        if len(parts) == 4 and parts[3].strip().isdigit():
            rows.append((parts[0], parts[1], parts[2].strip() == 'True', int(parts[3])))
    return pick_physical(rows)


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
