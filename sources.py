"""Sources beyond the built-in list: the operator's own, and feeds found on GitHub.

The registry is `.local/sources.json`, private like the node history. Nothing here
is trusted: every link still goes through the strict parser and the real Xray tests.
"""
from __future__ import annotations
import concurrent.futures as cf
import json
from pathlib import Path
import re
import time
import urllib.parse as up
import urllib.request
import collector
from state import atomic_write

# Repository searches, newest push first. GitHub allows 10 unauthenticated searches a minute.
SEARCHES = ('v2ray configs', 'vless subscription', 'topic:v2ray-config', 'topic:v2ray-configs')
REFRESH_SECONDS = 24 * 3600        # look for new repositories once a day
EVALUATE_SECONDS = 7 * 86400       # choose a repository's files again once a week
FORGET_SECONDS = 30 * 86400        # drop a repository the searches stopped returning
EVALUATE_PER_REFRESH = 60          # new repositories read per refresh; the rest wait a day
FILES_PER_REPOSITORY = 12          # README links fetched to choose from
URLS_PER_SOURCE = 3
MIN_SUPPORTED = 20                 # smaller feeds are not worth a slot
# A found feed is unknown until one of its configs works from here. Until then only a
# sample is tested: 16 collectors screened by hand on 2026-10-26 had 12 with none.
SAMPLE = 150
PROVEN = 1500
PROVEN_SECONDS = 7 * 86400
MAX_REST_SECONDS = 7 * 86400
PREFIX = 'gh:'

REPOSITORY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$')
LINK = re.compile(r'https://(?:raw\.githubusercontent\.com/([\w.-]+/[\w.-]+)/|'
                  r'github\.com/([\w.-]+/[\w.-]+)/(?:raw|blob)/)([^\s"\'<>()\[\]`|\\]+)')
NOT_A_FEED = re.compile(r'\.(png|jpe?g|gif|svg|webp|ico|md|py|sh|ps1|bat|ya?ml|json|exe|zip|apk|html?|css|js)$', re.I)


def empty():
    return {'schema': 1, 'refreshed': 0, 'manual': {}, 'found': {}, 'off': []}


def load(path):
    path = Path(path)
    if not path.exists():
        return empty()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 1 or not all(isinstance(data.get(k), dict) for k in ('manual', 'found')):
        raise ValueError('InvalidSources; refusing to overwrite the source list')
    data.setdefault('off', [])
    data.setdefault('refreshed', 0)
    return data


def save(path, registry):
    atomic_write(path, json.dumps(registry, ensure_ascii=False, indent=2) + '\n')


def add_manual(registry, url, name=''):
    """Add the operator's own subscription URL. Returns the name it is listed under."""
    url = str(url).strip()
    parts = up.urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or len(url) > 2000 or any(c.isspace() for c in url):
        raise ValueError('A subscription must be one https:// address')
    if any(row['url'] == url for row in registry['manual'].values()) or url in collector.SOURCES.values():
        raise ValueError('That subscription is already in the list')
    name = ' '.join(str(name).split())[:40]
    taken = set(registry['manual']) | set(collector.SOURCES)
    if not name:
        # Never derived from the address: status.json is public and lists source names.
        name = next(f'Custom {n}' for n in range(1, 10000) if f'Custom {n}' not in taken)
    if name in taken or name.startswith(PREFIX) or not all(c.isalnum() or c in ' ._-' for c in name):
        raise ValueError('Use a new name made of letters, digits, spaces, dots or dashes')
    registry['manual'][name] = {'url': url, 'added': time.time()}
    return name


def remove(registry, name):
    """Only the operator's own entries can be removed; the others can be switched off."""
    if registry['manual'].pop(name, None) is None:
        raise ValueError('No such source')
    registry['off'] = [n for n in registry['off'] if n != name]


def set_enabled(registry, name, enabled):
    known = set(collector.SOURCES) | set(registry['manual']) | {PREFIX + r for r in registry['found']}
    if name not in known:
        raise ValueError('No such source')
    registry['off'] = sorted((set(registry['off']) - {name}) | (set() if enabled else {name}))


def github_search(query):
    url = 'https://api.github.com/search/repositories?' + up.urlencode(
        {'q': query, 'sort': 'updated', 'order': 'desc', 'per_page': 100})
    request = urllib.request.Request(url, headers={'User-Agent': 'LocalXraySubscription/1.0 (+public feeds)',
                                                   'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read(8 * 1024 * 1024))['items']


def feed_urls(readme, repository):
    """Raw file links a README gives for its own repository, in the order written."""
    result = []
    for match in LINK.finditer(readme):
        if (match.group(1) or match.group(2)).lower() != repository.lower():
            continue  # a collector's README also links the collectors it copies from
        path = match.group(3).rstrip('.,;:!?*')
        if NOT_A_FEED.search(path.split('?', 1)[0]) or '..' in path:
            continue
        url = f'https://raw.githubusercontent.com/{repository}/{path}'
        if url not in result:
            result.append(url)
    return result[:FILES_PER_REPOSITORY]


def choose(feeds, limit=URLS_PER_SOURCE):
    """The few files that together carry the most configs: split-by-protocol and
    split-by-country copies of one list add nothing after the first."""
    chosen = []; covered = set(); remaining = dict(feeds)
    while remaining and len(chosen) < limit:
        url = max(sorted(remaining), key=lambda u: len(remaining[u] - covered))
        gain = len(remaining.pop(url) - covered)
        if gain < max(MIN_SUPPORTED, len(covered) // 20):
            break
        chosen.append(url); covered |= feeds[url]
    return chosen, len(covered)


def _keys(fetch, url):
    try:
        return {c.key for c in (collector.parse(link) for link in collector.links(fetch(url, timeout=12))) if c}
    except Exception:
        return set()


def evaluate(repositories, fetch):
    """{repository: (urls, size)} from each README's links; one failure costs one repository."""
    def readme(item):
        name, branch = item
        for filename in ('README.md', 'readme.md'):
            try:
                return name, feed_urls(fetch(f'https://raw.githubusercontent.com/{name}/{up.quote(branch)}/{filename}',
                                             timeout=12), name)
            except Exception:
                continue
        return name, []
    with cf.ThreadPoolExecutor(max_workers=8) as executor:
        listed = dict(executor.map(readme, repositories.items()))
        urls = [url for found in listed.values() for url in found]
        keys = dict(zip(urls, executor.map(lambda url: _keys(fetch, url), urls)))
    return {name: choose({url: keys[url] for url in found}) for name, found in listed.items()}


def refresh(registry, now, fetch=None, search=github_search):
    """Search GitHub for recently pushed collectors and choose files from the new ones.
    Returns False, changing nothing, when no search could be made."""
    fetch = fetch or collector.get
    since = time.strftime('%Y-%m-%d', time.gmtime(now - 2 * 86400))
    builtin = {'/'.join(u.split('/')[3:5]).lower() for u in collector.SOURCES.values()
               if u.startswith('https://raw.githubusercontent.com/')}
    seen = {}; answered = False
    for query in SEARCHES:
        try:
            for item in search(f'{query} pushed:>{since}'):
                name = str(item.get('full_name', ''))
                if (REPOSITORY.match(name) and name.lower() not in builtin
                        and not item.get('fork') and not item.get('archived') and not item.get('disabled')):
                    seen[name] = {'branch': str(item.get('default_branch') or 'main'),
                                  'stars': int(item.get('stargazers_count') or 0)}
            answered = True
        except Exception as exc:
            print(f'[DISCOVER] search failed ({type(exc).__name__})', flush=True)
    if not answered:
        return False
    found = registry['found']
    for name, info in seen.items():
        row = found.setdefault(name, {'urls': [], 'size': 0, 'evaluated': 0, 'runs': 0, 'tested': 0,
                                      'working': 0, 'dry': 0, 'last_worked': 0, 'next': 0})
        row.update(info, seen=now)
    waiting = sorted((n for n in seen if now - found[n]['evaluated'] >= EVALUATE_SECONDS),
                     key=lambda n: (-found[n]['stars'], n))[:EVALUATE_PER_REFRESH]
    for name, (urls, size) in evaluate({n: found[n]['branch'] for n in waiting}, fetch).items():
        found[name].update(urls=urls, size=size, evaluated=now)
    for name in [n for n, row in found.items() if now - row.get('seen', 0) > FORGET_SECONDS]:
        del found[name]
    registry['refreshed'] = now
    usable = sum(1 for row in found.values() if row['urls'])
    print(f'[DISCOVER] {len(seen)} repositories in search; {len(waiting)} read; {usable} usable feeds known', flush=True)
    return True


class Registry:
    """What run_cycle asks of the extra sources. fetch/search are replaced in tests."""

    def __init__(self, path, discover=40, fetch=None, search=github_search):
        self.path = Path(path)
        self.data = load(self.path)
        self.discover = discover
        self.fetch = fetch
        self.search = search

    def builtin(self):
        return {n: u for n, u in collector.SOURCES.items() if n not in self.data['off']}

    def collect(self, now, stats):
        """({name: candidates} of the operator's sources, the same of found feeds due a test)."""
        off = set(self.data['off'])
        manual = {n: row['url'] for n, row in self.data['manual'].items() if n not in off}
        own = collector.collect(manual, stats=stats) if manual else {}
        for name in own:
            stats[name]['kind'] = 'manual'
        found = {}
        if self.discover:
            if now - self.data['refreshed'] >= REFRESH_SECONDS:
                try:
                    refresh(self.data, now, self.fetch, self.search)
                except Exception as exc:
                    print(f'[DISCOVER] refresh failed ({type(exc).__name__})', flush=True)
                self.save()
            due = [(n, row) for n, row in self.data['found'].items()
                   if row['urls'] and row.get('next', 0) <= now and PREFIX + n not in off]
            # Feeds that worked first, then the newest finds.
            due.sort(key=lambda item: (-item[1]['last_worked'], -item[1]['evaluated'], -item[1]['stars'], item[0]))
            wanted = {PREFIX + n: row['urls'] for n, row in due[:self.discover]}
            found = collector.collect(wanted, stats=stats) if wanted else {}
            for name in found:
                stats[name]['kind'] = 'found'
        return own, found

    def quota(self, name, now):
        row = self.data['found'].get(name.removeprefix(PREFIX), {})
        return PROVEN if now - row.get('last_worked', 0) < PROVEN_SECONDS else SAMPLE

    def record(self, results, now):
        """results: {name: (tested, working)} for the found feeds tested this run."""
        for name, (tested, working) in results.items():
            row = self.data['found'].get(name.removeprefix(PREFIX))
            if row is None:
                continue
            row['runs'] += 1; row['tested'] += tested; row['working'] += working
            if working:
                row.update(dry=0, last_worked=now, next=0)
            else:
                # Nothing worked: 8 hours, then 16, 32... up to a week before the next sample.
                row['dry'] += 1
                row['next'] = now + min(MAX_REST_SECONDS, 4 * 3600 * 2 ** row['dry'])
        self.save()

    def save(self):
        # The control panel may have added or switched off a source while this run was testing.
        if self.path.exists():
            disk = load(self.path)
            self.data['manual'], self.data['off'] = disk['manual'], disk['off']
        save(self.path, self.data)
