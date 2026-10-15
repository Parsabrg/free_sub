"""Validate generated subscriptions and publish only exact project output paths."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from collector import links, parse

OUTPUTS = ('sub.txt','sub.b64','xray.json','status.json','index.html')


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
    plain=(docs/'sub.txt').read_bytes()
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
    return report


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
    def git(*args,check=True):
        return subprocess.run(['git',*args],cwd=root,check=check,capture_output=True,text=True,
                              encoding='utf-8',env=noninteractive_env(),timeout=90)
    if Path(git('rev-parse','--show-toplevel').stdout.strip()).resolve()!=root:
        raise RuntimeError('Publish from a dedicated clone of this project')
    if git('diff','--cached','--name-only').stdout.strip():
        raise RuntimeError('Git index contains staged work; publication refused')
    branch=git('branch','--show-current').stdout.strip()
    if not branch:
        raise RuntimeError('Detached HEAD cannot publish')
    # Never silently merge or force-push. A stale clone needs explicit reconciliation.
    git('fetch','origin',branch)
    if git('merge-base','--is-ancestor',f'origin/{branch}','HEAD',check=False).returncode:
        raise RuntimeError('Remote diverged; update the clone before publishing')
    git('add','--',*[f'docs/{p}' for p in OUTPUTS])
    if git('diff','--cached','--quiet',check=False).returncode:
        git('commit','-m','Publish locally Xray-verified subscription','--only','--',*[f'docs/{p}' for p in OUTPUTS])
    # Push even if output is unchanged: retries an earlier commit whose push failed.
    git('push','origin',f'HEAD:refs/heads/{branch}')
    print('[PUBLISH] Verified files pushed; check Pages deployment status.',flush=True)
