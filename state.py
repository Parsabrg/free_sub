"""Private durable node history, process lock, and Windows idle-sleep inhibition."""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
from dataclasses import asdict
import json
import os
from pathlib import Path
import time


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)
    if os.name != 'nt':
        path.chmod(0o600)


def read_state(path):
    if not Path(path).exists():
        return {'schema':3,'nodes':{},'next_number':1,'published':[],'last_discovery':0}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema') not in (2,3) or not isinstance(data.get('nodes'),dict):
        raise ValueError('InvalidState; refusing to overwrite private history')
    if data['schema'] == 2:
        for row in data['nodes'].values():
            # Old successes predate the video gate and could have been only a screen.
            row['last_success'] = 0
            row.pop('metrics', None)
            row['verification_history'] = []
        data['schema'] = 3
    return data


def save_state(path, state):
    # Keep recently useful history without growing forever.
    cutoff = time.time()-30*86400
    recent={k:v for k,v in state['nodes'].items() if v.get('last_seen',0)>cutoff}
    published=set(state.get('published',[]))
    ordered=sorted(recent,key=lambda k:(k in published,recent[k].get('last_success',0),recent[k].get('last_seen',0)),reverse=True)
    state['nodes']={k:recent[k] for k in ordered[:20000]}
    atomic_write(path, json.dumps(state,ensure_ascii=False,indent=2)+'\n')


@contextmanager
def run_lock(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with open(path, 'a+b') as lock:
        lock.seek(0)
        lock.write(b'0')
        lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another subscription run is active') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


@contextmanager
def keep_awake():
    """Keep Windows running during a scan; leave the display and normal idle policy alone."""
    if os.name == 'nt':
        from ctypes import wintypes
        setter=ctypes.windll.kernel32.SetThreadExecutionState
        setter.argtypes=[wintypes.DWORD]
        setter.restype=wintypes.DWORD
        result = setter(0x80000001)
        if not result:
            raise OSError('Cannot inhibit idle sleep during scan')
    try:
        yield
    finally:
        if os.name == 'nt':
            setter(0x80000000)


def observe(state, candidate, result, now, *, screening=False):
    row = state['nodes'].get(candidate.key)
    if row is None:
        row = {'url':candidate.url, 'source':candidate.source,'number':state['next_number'],
               'history':[], 'verification_history':[], 'last_success':0,
               'verified':False,'country_code':None}
        state['next_number'] += 1
        state['nodes'][candidate.key] = row
    row.update(url=candidate.url,source=candidate.source,last_seen=now,last_attempt=now,last_error=result.error if not result.passed else '')
    event = {'at':now,'ok':result.passed,'latency_ms':result.latency_ms}
    row['history'] = (row['history']+[event])[-48:]
    if not screening:
        row['verification_history'] = (row['verification_history']+[event])[-48:]
    if result.passed and not screening:
        row.update(last_success=now,metrics=asdict(result),endpoint_ip=result.endpoint_ip)
        row['verified'] = True
    return row


def reliability(row, now):
    # Recency-weighted Beta prior: brand-new one-off successes cannot score 100% reliability.
    events = row.get('verification_history',[])
    weights = [(0.5**(max(0,now-e['at'])/86400),e['ok']) for e in events]
    return (2+sum(w for w,ok in weights if ok))/(4+sum(w for w,_ in weights))


def retained_by_source(state, now, freshness):
    counts = {}
    for row in state['nodes'].values():
        source = row.get('verified_source')
        if (source and row['last_success'] >= row['first_source_success'] + 86400
                and now - row['last_success'] < freshness):
            counts[source] = counts.get(source, 0) + 1
    return counts


def sweep_stale_probe_dirs(base=None, max_age_seconds=3600, now=None):
    """Remove probe folders an earlier run could not (a locked file at the moment
    it ended). Only this project's own prefixes, and only when older than an hour,
    so a probe still running is never touched. Returns how many were removed."""
    import shutil, tempfile
    from pathlib import Path
    base = Path(base or tempfile.gettempdir())
    now = time.time() if now is None else now
    removed = 0
    for prefix in ('xray-probe-', 'xray-check-'):
        for folder in base.glob(prefix + '*'):
            try:
                if folder.is_dir() and now - folder.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(folder, ignore_errors=True)
                    removed += not folder.exists()
            except OSError:
                continue
    return removed
