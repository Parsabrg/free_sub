"""Validate generated subscriptions and publish only exact project output paths."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from collector import links, parse

OUTPUTS = ('sub.txt','sub.b64','xray.json','status.json','index.html','sub-compact.b64')
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
    for url,row in zip(urls,report['nodes']):
        c=parse(url)
        if c is None or c.key in seen or c.key!=row['id']:
            raise ValueError('Invalid or mismatched published configuration')
        if row['last_success_epoch']>row['expires_epoch'] or row['expires_epoch']<=report['generated_epoch']:
            raise ValueError('Expired node in publication')
        seen.add(c.key)
    for url,row,outbound in zip(urls,report['nodes'],outbounds):
        expected=dict(parse(url).outbound,tag=row['name'])
        if outbound!=expected:
            raise ValueError('Xray export differs from tested configuration')
    compact = docs/'sub-compact.b64'
    if 'compact_ids' in report or compact.exists():
        expected_ids, expected_data = compact_selection(urls,report['nodes'])
        if report.get('compact_ids')!=expected_ids or not compact.is_file():
            raise ValueError('Compact subscription IDs differ')
        if base64.b64decode(compact.read_text(encoding='utf-8').strip(),validate=True)!=expected_data:
            raise ValueError('Compact subscription differs from the verified feed')
    return report


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


def prepare_pages(destination, ref=None):
    """Copy only feed data and render the public page from the deployment code."""
    from public_page import INDEX

    destination=Path(destination)
    destination.mkdir(parents=True,exist_ok=True)
    for name in ('sub.txt','sub.b64','xray.json','status.json','sub-compact.b64'):
        if ref:
            result=subprocess.run(['git','show',f'{ref}:docs/{name}'],capture_output=True)
            if name=='sub-compact.b64' and result.returncode:
                continue
            result.check_returncode()
            data=result.stdout
        else:
            source=Path('docs')/name
            if name=='sub-compact.b64' and not source.is_file():
                continue
            data=source.read_bytes()
        (destination/name).write_bytes(data)
    report=validate_outputs(destination)
    if 'compact_ids' not in report:
        urls=links((destination/'sub.txt').read_text(encoding='utf-8'))
        write_compact_feed(destination,urls,report['nodes'],report)
        (destination/'status.json').write_text(json.dumps(report,ensure_ascii=False)+'\n',encoding='utf-8')
    (destination/'index.html').write_text(INDEX,encoding='utf-8')
    validate_outputs(destination)


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
    validate_outputs(docs)
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
    # A separate index and an orphan root keep the checkout, user index, and source history out of the feed.
    with tempfile.TemporaryDirectory(prefix='feed-index-') as folder:
        env=noninteractive_env()
        env['GIT_INDEX_FILE']=str(Path(folder)/'index')
        git('read-tree','--empty',env=env)
        git('add','-f','--',SNAPSHOT_WORKFLOW,*[f'docs/{name}' for name in OUTPUTS],env=env)
        tree=git('write-tree',env=env).stdout.strip()
    if parent and tree==git('rev-parse',f'{parent}^{{tree}}').stdout.strip():
        print('[PUBLISH] The verified subscription is already on the publishing branch.',flush=True)
        return
    parents=['-p',parent] if parent else []
    commit=git('commit-tree',tree,*parents,'-m','Publish locally Xray-verified subscription').stdout.strip()
    git('push','origin',f'{commit}:{ref}')
    print('[PUBLISH] Verified files pushed; check Pages deployment status.',flush=True)
