"""Validate generated subscriptions and publish only exact project output paths."""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from collector import links, parse
from network import pin_endpoint, public_address
from state import atomic_write

PINNED_FEED = 'xray-pinned.json'
PLAYBACK_FEED = 'sub-playback.b64'
DELIVERY = 'delivery.json'
OUTPUTS = ('sub.txt','sub.b64','xray.json','status.json','index.html','sub-compact.b64',PINNED_FEED,PLAYBACK_FEED)
FEED_BRANCH = 'subscriptions'
SNAPSHOT_WORKFLOW = '.github/workflows/subscription-snapshot.yml'
COMPACT_LIMIT = 20


def source_fingerprint(root):
    paths = sorted(p for p in Path(root).rglob('*') if p.is_file()
                   and (p.suffix in ('.py','.ps1','.bat') or '.github/workflows' in p.as_posix())
                   and not any(part in ('.local','.git','tools','__pycache__') for part in p.relative_to(root).parts))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode()+b'\0'+path.read_bytes()+b'\0')
    return digest.hexdigest()


def validate_outputs(docs):
    return read_snapshot(docs)[0]


def read_snapshot(docs):
    docs=Path(docs)
    plain=(docs/'sub.txt').read_text(encoding='utf-8').encode('utf-8')
    encoded=(docs/'sub.b64').read_text(encoding='utf-8').strip()
    if base64.b64decode(encoded,validate=True)!=plain:
        raise ValueError('Subscription encodings differ')
    report=json.loads((docs/'status.json').read_text(encoding='utf-8'))
    urls=links(plain.decode('utf-8'))
    if len(urls)!=report['published'] or len(report['nodes'])!=len(urls):
        raise ValueError('Subscription count mismatch')
    seen=set()
    outbounds=json.loads((docs/'xray.json').read_text(encoding='utf-8'))['outbounds']
    if len(outbounds)!=len(urls):
        raise ValueError('Xray subscription count mismatch')
    for url,row,outbound in zip(urls,report['nodes'],outbounds):
        c=parse(url)
        if c is None or c.key in seen or c.key!=row['id']:
            raise ValueError('Invalid or mismatched published configuration')
        if row['last_success_epoch']>row['expires_epoch'] or row['expires_epoch']<=report['generated_epoch']:
            raise ValueError('Expired node in publication')
        seen.add(c.key)
        expected=dict(c.outbound,tag=row['name'])
        if outbound!=expected:
            raise ValueError('Xray export differs from tested configuration')
    compact = docs/'sub-compact.b64'
    if 'compact_ids' in report or compact.exists():
        expected_ids, expected_data = compact_selection(urls,report['nodes'])
        if report.get('compact_ids')!=expected_ids or not compact.is_file():
            raise ValueError('Compact subscription IDs differ')
        if base64.b64decode(compact.read_text(encoding='utf-8').strip(),validate=True)!=expected_data:
            raise ValueError('Compact subscription differs from the verified feed')
    pinned = docs/PINNED_FEED
    if 'pinned_ids' in report or pinned.exists():
        ids, expected = pinned_selection(outbounds,report['nodes'])
        if (report.get('pinned_ids')!=ids or not pinned.is_file()
                or json.loads(pinned.read_text(encoding='utf-8'))!=expected):
            raise ValueError('Pinned Xray export differs from verified endpoints')
    playback=docs/PLAYBACK_FEED
    if 'playback_ids' in report or playback.exists():
        ids, data=playback_selection(urls,report['nodes'])
        if report.get('playback_ids')!=ids or not playback.is_file() or playback.read_bytes().strip()!=data.strip():
            raise ValueError('Playback subscription differs from verified playback results')
    return report,urls,outbounds


def compact_selection(urls, rows):
    endpoints=set()
    selected=[]
    for url,row in zip(urls,rows):
        endpoint=row.get('endpoint_ip') or parse(url).host
        if endpoint in endpoints:
            continue
        endpoints.add(endpoint)
        selected.append((url,row['id']))
        if len(selected)==COMPACT_LIMIT:
            break
    return [key for _,key in selected], ''.join(f'{url}\n' for url,_ in selected).encode('utf-8')


def write_compact_feed(docs, urls, rows, report):
    report['compact_ids'], data = compact_selection(urls,rows)
    (Path(docs)/'sub-compact.b64').write_bytes(base64.b64encode(data)+b'\n')


def pinned_selection(outbounds, rows):
    selected=[]
    for outbound,row in zip(outbounds,rows):
        address=row.get('endpoint_ip')
        if isinstance(address,str) and public_address(address) and row.get('video_bytes',0)>0:
            # Use the same address substitution as the probe. Transport and TLS settings stay intact.
            selected.append((row['id'],pin_endpoint(outbound,[address])))
    return [key for key,_ in selected], {'outbounds':[outbound for _,outbound in selected]}


def write_pinned_feed(docs, outbounds, rows, report):
    report['pinned_ids'], pinned = pinned_selection(outbounds,rows)
    (Path(docs)/PINNED_FEED).write_text(json.dumps(pinned,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def playback_selection(urls, rows):
    selected=[(url,row['id']) for url,row in zip(urls,rows) if row.get('playback_seconds',0)>=15
              and row.get('playback_completed_epoch',0)>=row['last_success_epoch'] and row.get('verified_this_run')]
    return [key for _,key in selected],base64.b64encode(''.join(f'{url}\n' for url,_ in selected).encode('utf-8'))+b'\n'


def fresh_outputs(snapshot, now):
    report,urls,outbounds=snapshot
    fresh=[(url,row,outbound) for url,row,outbound in zip(urls,report['nodes'],outbounds) if row['expires_epoch']>now]
    urls=[url for url,_,_ in fresh]
    rows=[dict(row,rank=rank) for rank,(_,row,_) in enumerate(fresh,1)]
    outbounds=[outbound for _,_,outbound in fresh]
    compact_ids,compact=compact_selection(urls,rows)
    pinned_ids,pinned=pinned_selection(outbounds,rows)
    playback_ids,playback=playback_selection(urls,rows)
    status=dict(report,nodes=rows,published=len(rows),compact_ids=compact_ids,pinned_ids=pinned_ids,playback_ids=playback_ids)
    if not rows:
        status['health']='expired_or_empty'
    def encode(value):
        return (json.dumps(value,ensure_ascii=False)+'\n').encode('utf-8')
    plain=''.join(f'{url}\n' for url in urls).encode('utf-8')
    return {'sub.txt':plain,'sub.b64':base64.b64encode(plain)+b'\n','xray.json':encode({'outbounds':outbounds}),
            'status.json':encode(status),'sub-compact.b64':base64.b64encode(compact)+b'\n',
            PINNED_FEED:encode(pinned),PLAYBACK_FEED:playback}


def prepare_legacy_feed(docs):
    """Keep only entries the current parser can verify against a pre-migration snapshot."""
    plain=(docs/'sub.txt').read_text(encoding='utf-8').encode('utf-8')
    encoded=(docs/'sub.b64').read_text(encoding='utf-8').strip()
    if base64.b64decode(encoded,validate=True)!=plain:
        raise ValueError('Subscription encodings differ')
    report=json.loads((docs/'status.json').read_text(encoding='utf-8'))
    urls=links(plain.decode('utf-8'))
    outbounds=json.loads((docs/'xray.json').read_text(encoding='utf-8'))['outbounds']
    if len(urls)!=report['published'] or len(report['nodes'])!=len(urls) or len(outbounds)!=len(urls):
        raise ValueError('Legacy subscription count mismatch')
    retained=[]
    for url,row,outbound in zip(urls,report['nodes'],outbounds):
        candidate=parse(url)
        if candidate and candidate.key==row['id'] and dict(candidate.outbound,tag=row['name'])==outbound:
            retained.append((url,row,outbound))
    if len(retained)!=len(urls):
        data=''.join(f'{url}\n' for url,_,_ in retained).encode('utf-8')
        (docs/'sub.txt').write_bytes(data)
        (docs/'sub.b64').write_bytes(base64.b64encode(data)+b'\n')
        (docs/'xray.json').write_text(json.dumps({'outbounds':[o for _,_,o in retained]},ensure_ascii=False)+'\n',encoding='utf-8')
        report['nodes']=[dict(row,rank=rank) for rank,(_,row,_) in enumerate(retained,1)]
        report['published']=len(retained)
        print(f'[PAGES] Skipped {len(urls)-len(retained)} legacy entries that require reverification.',flush=True)
    write_compact_feed(docs,[url for url,_,_ in retained],report['nodes'],report)
    (docs/'status.json').write_text(json.dumps(report,ensure_ascii=False)+'\n',encoding='utf-8')


def prepare_pages(destination, ref=None, *, now=None):
    """Copy only feed data and render the public page from the deployment code."""
    from public_page import INDEX

    destination=Path(destination)
    destination.mkdir(parents=True,exist_ok=True)
    optional={'sub-compact.b64',PINNED_FEED,PLAYBACK_FEED}
    for name in OUTPUTS:
        if name=='index.html':
            continue
        if ref:
            result=subprocess.run(['git','show',f'{ref}:docs/{name}'],capture_output=True)
            if name in optional and result.returncode:
                (destination/name).unlink(missing_ok=True)
                continue
            result.check_returncode()
            data=result.stdout
        else:
            source=Path('docs')/name
            if name in optional and not source.is_file():
                (destination/name).unlink(missing_ok=True)
                continue
            data=source.read_bytes()
        (destination/name).write_bytes(data)
    if not ref and not (destination/'sub-compact.b64').exists():
        prepare_legacy_feed(destination)
    for name,data in fresh_outputs(read_snapshot(destination),time.time() if now is None else now).items():
        (destination/name).write_bytes(data)
    (destination/'index.html').write_bytes(INDEX.encode('utf-8'))
    report=validate_outputs(destination)
    revision=subprocess.check_output(['git','rev-parse',ref or 'HEAD'],text=True).strip()
    manifest={'snapshot':revision,'tested_epoch':report['generated_epoch'],'published':report['published'],
              'files':{name:hashlib.sha256((destination/name).read_bytes()).hexdigest() for name in OUTPUTS}}
    (destination/DELIVERY).write_text(json.dumps(manifest,sort_keys=True)+'\n',encoding='utf-8')


def fetch_public(base, name, limit=1024*1024*32):
    request=Request(f'{base.rstrip("/")}/{name}?check={time.time_ns()}',headers={'Cache-Control':'no-cache'})
    with urlopen(request,timeout=12) as response:
        data=response.read(limit+1)
    if len(data)>limit:
        raise ValueError(f'Oversized response for {name}')
    return data


def verify_live(base, destination):
    manifest=json.loads((Path(destination)/DELIVERY).read_bytes())
    if json.loads(fetch_public(base,DELIVERY,4096))!=manifest:
        raise ValueError('Live snapshot differs from the prepared deployment')
    verify_public_files(base,manifest)
    return manifest


def verify_public_files(base, manifest):
    def verify(name):
        if hashlib.sha256(fetch_public(base,name)).hexdigest()!=manifest['files'].get(name):
            raise ValueError(f'Live checksum mismatch for {name}')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify,OUTPUTS))


def noninteractive_env():
    env=os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never',GIT_ASKPASS='',SSH_ASKPASS='')
    env.setdefault('GIT_SSH_COMMAND','ssh -oBatchMode=yes -oConnectTimeout=15')
    return env


def publish(root, xray, docs):
    root,docs=Path(root).resolve(),Path(docs).resolve()
    if docs != root/'docs':
        raise RuntimeError('Only project docs/ may be published')
    # Run offline tests AND a real Xray/TLS/HTTPS local end-to-end fixture before each push.
    subprocess.run([sys.executable,str(root/'verify.py'),'--xray',str(xray)],cwd=root,check=True,timeout=180)
    report=validate_outputs(docs)
    if report.get('nodes') and not any(row['expires_epoch']>time.time() for row in report['nodes']):
        raise RuntimeError('Verified snapshot has expired; run a new check')
    def git(*args,check=True,env=None):
        try:
            return subprocess.run(['git',*args],cwd=root,check=check,capture_output=True,text=True,
                                  encoding='utf-8',env=noninteractive_env() if env is None else env,timeout=90)
        except subprocess.CalledProcessError as exc:
            error=(exc.stderr or '').lower()
            category=('authentication' if any(s in error for s in ('authentication','credential','username'))
                      else 'network' if any(s in error for s in ('resolve','connect','ssl','tls','timeout','http/')) else 'repository')
            # Never log raw stderr: a remote or credential helper can include secrets.
            print(f'[PUBLISH ERROR] Git {args[0]} exited {exc.returncode}; category={category}',file=sys.stderr,flush=True)
            raise
    if Path(git('rev-parse','--show-toplevel').stdout.strip()).resolve()!=root:
        raise RuntimeError('Publish from a dedicated clone of this project')
    if git('diff','--cached','--name-only').stdout.strip():
        raise RuntimeError('Git index contains staged work; publication refused')
    if not git('branch','--show-current').stdout.strip():
        raise RuntimeError('Detached HEAD cannot publish')
    ref=f'refs/heads/{FEED_BRANCH}'
    remote=git('ls-remote','--heads','origin',ref).stdout.strip()
    parent=None
    if remote:
        git('fetch','--no-tags','origin',ref)
        parent=git('rev-parse','FETCH_HEAD').stdout.strip()
        previous=json.loads(git('show',f'{parent}:docs/status.json').stdout)
        if previous['generated_epoch']>report['generated_epoch']:
            raise RuntimeError('A newer verified snapshot is already published')
    # A separate index and an orphan root keep the checkout, user index, and source history out of the feed.
    with tempfile.TemporaryDirectory(prefix='feed-index-') as folder:
        env=noninteractive_env()
        env['GIT_INDEX_FILE']=str(Path(folder)/'index')
        git('read-tree','--empty',env=env)
        git('add','-f','--',SNAPSHOT_WORKFLOW,*[f'docs/{name}' for name in OUTPUTS],env=env)
        tree=git('write-tree',env=env).stdout.strip()
    if parent and tree==git('rev-parse',f'{parent}^{{tree}}').stdout.strip():
        atomic_write(root/'.local'/'pushed.json',json.dumps({'snapshot':parent,'tested_epoch':report.get('generated_epoch')})+'\n')
        print('[PUBLISH] The verified subscription is already on the publishing branch.',flush=True)
        return
    parents=['-p',parent] if parent else []
    commit=git('commit-tree',tree,*parents,'-m','Publish locally Xray-verified subscription').stdout.strip()
    for attempt in range(3):
        if report.get('nodes') and not any(row['expires_epoch']>time.time() for row in report['nodes']):
            raise RuntimeError('Verified snapshot has expired; run a new check')
        try:
            git('push','origin',f'{commit}:{ref}')
            break
        except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
            error=str(getattr(exc,'stderr','') or '').lower()
            if attempt==2 or (not isinstance(exc,subprocess.TimeoutExpired) and not any(
                    item in error for item in ('resolve','connect','timeout','429','502','503','504','unexpected disconnect'))):
                raise
            print('[PUBLISH] Retrying the same verified snapshot.',flush=True)
            time.sleep(4*(attempt+1))
    atomic_write(root/'.local'/'pushed.json',json.dumps({'snapshot':commit,'tested_epoch':report.get('generated_epoch')})+'\n')
    print('[PUBLISH] Verified files pushed; check Pages deployment status.',flush=True)
