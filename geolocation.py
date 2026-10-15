"""Country flags based on the public IP of each candidate's *connection endpoint*.

Endpoints behind CDNs may resolve to CDN ingress IPs rather than the proxy exit.
GeoIP lookups happen only for nodes that passed the two real Xray probes.
Uses a persistent local cache and the no-key HTTPS endpoint at ipwho.is.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GEO_URL = 'https://ipwho.is/'
CACHE_SECONDS = 14 * 24 * 60 * 60
MAX_CACHE_ITEMS = 20000
MAX_FREE_LOOKUPS_PER_UTC_DAY = 900  # ipwho.is permits 1,000/day; keep a buffer.


@dataclass(frozen=True)
class CountryInfo:
    ip: str | None
    country_code: str | None
    flag: str


def country_flag(code: str | None) -> str:
    """ISO 3166-1 alpha-2 -> emoji, otherwise a neutral globe."""
    code = (code or '').strip().upper()
    if len(code) == 2 and code.isascii() and code.isalpha():
        return ''.join(chr(0x1F1E6 + ord(letter) - ord('A')) for letter in code)
    return '🌐'


def public_ip(ip_text: str) -> str | None:
    try:
        addr = ipaddress.ip_address(ip_text)
        return str(addr) if addr.is_global else None
    except ValueError:
        return None


def resolve_endpoint(host: str) -> str | None:
    """Resolve on the testing device, never infer a country from a node's title."""
    host = (host or '').strip().strip('[]')
    if not host:
        return None
    try:
        direct_ip = ipaddress.ip_address(host)
    except ValueError:
        direct_ip = None
    if direct_ip is not None:
        return str(direct_ip) if direct_ip.is_global else None
    try:
        # Preserve the OS resolver's order. Prefer public IPv4 for broad GeoIP coverage.
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        candidates = [public_ip(record[4][0]) for record in records]
        candidates = [value for value in candidates if value]
        return next((ip for ip in candidates if ipaddress.ip_address(ip).version == 4),
                    candidates[0] if candidates else None)
    except (socket.gaierror, UnicodeError, ValueError, OSError):
        return None


def load_cache(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(parsed, dict):
            return {}
        now = time.time()
        return {ip: row for ip, row in parsed.items()
                if public_ip(ip) and isinstance(row, dict)
                and isinstance(row.get('country_code'), str)
                and len(row['country_code']) == 2
                and isinstance(row.get('timestamp'), (int, float))
                and 0 <= now - row['timestamp'] <= CACHE_SECONDS}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_cache(path: Path, cache: dict) -> None:
    """Persist locally. This file should be excluded from Git publishing."""
    try:
        path.parent.mkdir(exist_ok=True, parents=True)
        newest = sorted(cache.items(), key=lambda item: item[1]['timestamp'], reverse=True)[:MAX_CACHE_ITEMS]
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(json.dumps(dict(newest), ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
    except OSError as e:
        print(f'[GEO] Could not save local IP cache ({type(e).__name__})', flush=True)


def lookup_country(ip: str, timeout: float = 5.0) -> str | None:
    """Return verified two-letter country code or None (429/errors stay unknown)."""
    if public_ip(ip) is None:
        return None
    try:
        request = urllib.request.Request(GEO_URL + urllib.parse.quote(ip, safe=':'),
                                         headers={'User-Agent': 'LocalXraySubscription/2.0'})
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            if reply.status != 200:
                return None
            data = reply.read(8192)
        payload = json.loads(data.decode('utf-8'))
        if payload.get('success') is not True:
            return None
        cc = payload.get('country_code', '')
        if isinstance(cc, str) and len(cc) == 2 and cc.isascii() and cc.isalpha():
            return cc.upper()
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, UnicodeError, ValueError):
        pass
    return None


def geolocate_hosts(hosts: list[str], cache_path: Path, workers: int = 6,
                    max_new_lookups: int = 300) -> dict[str, CountryInfo]:
    """Returns {host: CountryInfo}. Unknown is honest; no flags fabricated.

    Tests are completed before invoking this function. The API only receives IP
    addresses, not proxy passwords/URLs. Persistent cache reduces repeated calls.
    """
    hosts = list(dict.fromkeys(hosts))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        ip_by_host = dict(zip(hosts, pool.map(resolve_endpoint, hosts)))
    cache = load_cache(cache_path)
    required = list(dict.fromkeys(ip for ip in ip_by_host.values() if ip and ip not in cache))
    if len(required) > max_new_lookups:
        print(f'[GEO] Only looking up {max_new_lookups} new IPs (free quota protection)', flush=True)
    # Record requests locally to respect the free endpoint's 1,000/day quota.
    # The quota applies to all clients using this public IP, so this is a best-effort cap.
    usage_path = cache_path.with_suffix('.usage.json')
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        usage = json.loads(usage_path.read_text(encoding='utf-8'))
        used = max(0, int(usage.get('count', 0))) if usage.get('day') == today else 0
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
        used = 0
    remaining = max(0, MAX_FREE_LOOKUPS_PER_UTC_DAY - used)
    # Keep concurrency moderate to avoid bursts to the public free geolocation API.
    to_lookup = required[:min(max_new_lookups, remaining)]
    if len(to_lookup) < len(required):
        print(f'[GEO] Daily lookup budget or per-run limit reached; {len(required)-len(to_lookup)} IPs unknown', flush=True)
    # Reserve quota *before* requests, including failed requests.
    if to_lookup:
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_tmp = usage_path.with_suffix('.json.tmp')
        try:
            usage_tmp.write_text(json.dumps({'day': today, 'count': used+len(to_lookup)}), encoding='utf-8')
            usage_tmp.replace(usage_path)
        except OSError as e:
            print(f'[GEO] Cannot save quota tracker: {type(e).__name__}', flush=True)
    with ThreadPoolExecutor(max_workers=min(workers, 3)) as pool:
        futures = {pool.submit(lookup_country, ip): ip for ip in to_lookup}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                code = future.result()
            except Exception:
                code = None
            if code:
                cache[ip] = {'country_code': code, 'timestamp': time.time()}
    save_cache(cache_path, cache)
    output = {}
    for host in hosts:
        ip = ip_by_host[host]
        code = cache.get(ip, {}).get('country_code') if ip else None
        output[host] = CountryInfo(ip, code, country_flag(code))
    known = sum(1 for v in output.values() if v.country_code)
    print(f'[GEO] {known}/{len(hosts)} endpoint hosts resolved to country flags; '
          f'{len(to_lookup)} external lookups', flush=True)
    return output
