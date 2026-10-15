"""Private durable node history, process lock, and Windows idle-sleep inhibition."""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
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
        return {'schema':2,'nodes':{},'next_number':1,'published':[],'last_discovery':0}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema') != 2 or not isinstance(data.get('nodes'),dict):
        raise ValueError('InvalidState; refusing to overwrite private history')
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


def observe(state, candidate, result, now, verified=False):
    row = state['nodes'].get(candidate.key)
    if row is None:
        row = {'url':candidate.url, 'source':candidate.source,'number':state['next_number'],
               'history':[], 'last_success':0,'verified':False,'country_code':None}
        state['next_number'] += 1
        state['nodes'][candidate.key] = row
    row.update(url=candidate.url,source=candidate.source,last_seen=now,last_attempt=now,last_error=result.error if not result.passed else '')
    row['history'] = (row['history']+[{'at':now,'ok':result.passed,'latency_ms':result.latency_ms}])[-48:]
    if result.passed:
        from dataclasses import asdict
        row.update(last_success=now,metrics=asdict(result),endpoint_ip=result.endpoint_ip)
        row['verified'] = row['verified'] or verified
    return row


def reliability(row, now):
    # Recency-weighted Beta prior: brand-new one-off successes cannot score 100% reliability.
    events = row.get('history',[])
    weights = [(0.5**(max(0,now-e['at'])/86400),e['ok']) for e in events]
    return (2+sum(w for w,ok in weights if ok))/(4+sum(w for w,_ in weights))
