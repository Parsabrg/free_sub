"""Collect, verify, remember and publish public proxy links on the operator's network."""
from __future__ import annotations
import argparse
import base64
import copy
from collections import Counter, deque
import concurrent.futures as cf
from dataclasses import asdict
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from collector import collect, parse
from geolocation import geolocate_hosts, country_flag
from naming import name_for_position, rename_share_link
from publishing import publish, validate_outputs
from state import atomic_write, keep_awake, observe, read_state, reliability, run_lock, save_state
from xray_test import ProbeResult, run_probe

ROOT=Path(__file__).resolve().parent


def balanced(groups, seed):
    rng=random.Random(seed)
    pools={name:deque(rng.sample(items,len(items))) for name,items in groups.items()}
    seen=set(); result=[]
    while any(pools.values()):
        for pool in pools.values():
            if pool:
                candidate=pool.popleft()
                if candidate.key not in seen:
                    seen.add(candidate.key); result.append(candidate)
    return result


def rank_key(candidate, result, state, now):
    row=state['nodes'].get(candidate.key,{'history':[]})
    return (-reliability(row,now), not result.full_size, result.latency_ms,
            result.jitter_ms, candidate.key)


def test_batch(nodes, xray, workers, timeout, label, probe=run_probe, quick=False, screen_timeout=None, retry_unexpected=True):
    found=[]; results={}; counts=Counter(); unexpected=[]; start=time.monotonic()
    probe_options={'quick':quick}
    if screen_timeout is not None:
        probe_options['screen_timeout']=screen_timeout
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures={executor.submit(probe,c,str(xray),timeout,**probe_options):c for c in nodes}
        for index,job in enumerate(cf.as_completed(futures),1):
            candidate=futures[job]
            try:
                result=job.result()
            except Exception as exc:
                result=ProbeResult(False,error='ProbeError/'+type(exc).__name__)
                unexpected.append(candidate)
            results[candidate.key]=result
            counts['pass' if result.passed else result.error or 'failed']+=1
            if result.passed: found.append((candidate,result))
            if index%25==0 or index==len(nodes):
                print(f'[{label}] {index}/{len(nodes)}; passed={len(found)}; elapsed={time.monotonic()-start:.0f}s',flush=True)
    if unexpected and retry_unexpected:
        errors={k:v for k,v in counts.items() if k.startswith('ProbeError/')}
        print(f'[{label}] Retrying {len(unexpected)} runner errors with at most 16 workers: {errors}',flush=True)
        recovered,retried,_=test_batch(unexpected,xray,min(workers,16),timeout,label+'-RETRY',probe,
                                      quick=quick,screen_timeout=screen_timeout,retry_unexpected=False)
        found.extend(recovered); results.update(retried)
        counts=Counter('pass' if r.passed else r.error or 'failed' for r in results.values())
    return found,results,dict(counts)


def write_outputs(docs, selected, state, report, now, ttl):
    docs=Path(docs)
    rows=[]; urls=[]; outbounds=[]
    for rank,(candidate,result) in enumerate(selected,1):
        row=state['nodes'][candidate.key]
        name=name_for_position(country_flag(row.get('country_code')),row['number'])
        url=rename_share_link(candidate.url,name)
        if parse(url).key != candidate.key:
            raise ValueError('PublishedConfigDiffersFromTestedConfig')
        urls.append(url)
        outbound=copy.deepcopy(candidate.outbound)
        outbound['tag']=name
        outbounds.append(outbound)
        rows.append({'id':candidate.key,'name':name,'rank':rank,'protocol':candidate.protocol,
                     'country_code':row.get('country_code'),'endpoint_ip':row.get('endpoint_ip'),
                     'last_success_epoch':row['last_success'],'expires_epoch':row['last_success']+ttl,
                     'success_rate':round(reliability(row,now),4),'latency_ms':result.latency_ms,
                     'jitter_ms':result.jitter_ms,'body_bytes':result.body_bytes,
                     'transfer_kbps_estimate':result.kbps,'full_size':result.full_size,
                     'verified_this_run':False})
    # Set membership separately: do not mutate metadata while iterating.
    current=set(report.pop('_verified_keys',[]))
    for row in rows: row['verified_this_run']=row['id'] in current
    report.update(schema=2,generated_epoch=now,
                  generated_utc=datetime.fromtimestamp(now,timezone.utc).isoformat(timespec='seconds'),
                  published=len(rows),nodes=rows,
                  flag_definition='IP pinned for the successful proxy connection; may be CDN ingress, not exit',
                  recommended_client_refresh_seconds=3600,health_interval_seconds=1800,
                  discovery_interval_seconds=14400,node_ttl_seconds=ttl)
    data='\n'.join(urls)+('\n' if urls else '')
    staging=docs.parent/'.local'/'output-staging'
    staging.mkdir(parents=True,exist_ok=True)
    atomic_write(staging/'sub.txt',data)
    atomic_write(staging/'sub.b64',base64.b64encode(data.encode('utf-8')).decode()+'\n')
    atomic_write(staging/'xray.json',json.dumps({'outbounds':outbounds},indent=2,ensure_ascii=False)+'\n')
    atomic_write(staging/'status.json',json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    atomic_write(staging/'index.html',INDEX)
    validate_outputs(staging)
    # Each replacement is atomic; the Pages deployment publishes one complete Git snapshot.
    docs.mkdir(parents=True,exist_ok=True)
    for name in ('sub.txt','sub.b64','xray.json','status.json','index.html'):
        atomic_write(docs/name,(staging/name).read_text(encoding='utf-8'))
    return report


INDEX='''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Free Xray subscription</title>
<style>body{font:16px system-ui;max-width:760px;margin:4rem auto;padding:0 1rem;line-height:1.6;background:#10151d;color:#dce6f4}a{color:#86c9ff}code{overflow-wrap:anywhere}.warning{color:#ffce83}</style>
<h1>Free Xray subscription</h1><p id="health">Loading test status…</p>
<p><a href="sub.txt">Plain subscription</a> · <a href="sub.b64">Base64 subscription</a> · <a href="xray.json">Xray feed for v2rayN</a> · <a href="status.json">Test report</a></p>
<p>Real HTTPS transfers through Xray, tested on the operator’s network. Country flags describe the connection endpoint, which can be a CDN ingress. Public proxies are untrusted.</p>
<p>Refresh the subscription hourly. Clients should ignore nodes after their individual expiry in status.json.</p>
<script>fetch('status.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()}).then(s=>{const now=Date.now()/1000;const fresh=s.nodes.filter(n=>n.expires_epoch>now).length;const age=Math.max(0,Math.round((now-s.generated_epoch)/60));const e=document.querySelector('#health');e.textContent=`${fresh} unexpired of ${s.published} nodes; last attempt ${age} minutes ago; mode: ${s.mode}; state: ${s.health}.`;if(age>60||s.health!=='ok'||fresh===0)e.className='warning';}).catch(()=>document.querySelector('#health').textContent='Status unavailable; freshness unknown.');</script></html>'''


def run_cycle(args, root=ROOT, probe=run_probe, collect_fn=collect, geo_fn=geolocate_hosts, now=None):
    root=Path(root); live_clock=now is None; now=time.time() if live_clock else now
    local=root/'.local'; history_path=local/'history.json'
    state=read_state(history_path)
    mode=args.mode
    if mode=='auto':
        mode='full' if now-state.get('last_discovery',0)>=args.discovery_hours*3600 else 'health'
    stats={}; groups={}
    if mode=='full':
        groups=collect_fn(stats=stats)
    # Existing entries are always tested first, including when all sources are unavailable.
    old=[]
    for key in state.get('published',[]):
        row=state['nodes'].get(key,{})
        candidate=parse(row.get('url',''),row.get('source','history'))
        if candidate and row.get('verified'): old.append(candidate)
    remaining=[]
    published_keys={c.key for c in old}
    for key,row in state['nodes'].items():
        if key in published_keys or not row.get('verified') or now-row.get('last_success',0)>args.ttl_hours*3600:
            continue
        candidate=parse(row.get('url',''),row.get('source','history'))
        if candidate: remaining.append(candidate)
    remaining.sort(key=lambda c:-reliability(state['nodes'][c.key],now))
    exploration=balanced(groups,str(int(now//1800))) if mode=='full' else remaining[:args.recheck]
    queue=[]; seen=set()
    for candidate in old+exploration:
        if candidate.key not in seen:
            queue.append(candidate); seen.add(candidate.key)
    if mode=='full' and args.max_tests:
        # Preserve the budget for exploration even if the live list occupies most slots.
        queue=queue[:max(args.max_tests,len(old))]
    print(f'[RUN] mode={mode}; candidates={len(queue)}; previous={len(old)}',flush=True)
    screen_timeout=args.screen_timeout if mode=='full' else None
    first_workers=args.workers if mode=='full' else min(args.workers,16)
    first,first_results,first_counts=test_batch(queue,args.xray,first_workers,args.timeout,'CHECK',probe,
                                               quick=(mode=='full'),screen_timeout=screen_timeout)
    first.sort(key=lambda pair:rank_key(pair[0],pair[1],state,now))
    second=[]; second_results={}; second_counts={}
    if mode=='full':
        # Prefer distinct IP:port endpoints before spending finalist budget on clones.
        finalists=[]; endpoints=Counter()
        for c,r in first:
            endpoint=(r.endpoint_ip,c.port)
            if endpoints[endpoint]>=4: continue
            endpoints[endpoint]+=1
            finalists.append(c)
            if len(finalists)>=args.recheck: break
        second,second_results,second_counts=test_batch(finalists,args.xray,min(args.workers,16),args.timeout,
                                                       'VERIFY',probe,quick=False)
        passed={c.key:(c,r) for c,r in second}
    else:
        passed={c.key:(c,r) for c,r in first if state['nodes'].get(c.key,{}).get('verified')}
    if live_clock: now=time.time()
    # Observe each real completion time, not the time the full batch eventually finishes.
    for c in queue:
        result=second_results.get(c.key,first_results[c.key])
        observe(state,c,result,result.completed_epoch if live_clock and result.completed_epoch else now,
                verified=c.key in passed and mode=='full')
    passed={key:pair for key,pair in passed.items()
            if now-state['nodes'][key]['last_success']<args.ttl_hours*3600}
    selected=sorted(passed.values(),key=lambda pair:rank_key(pair[0],pair[1],state,now))
    diverse=[]; endpoints=Counter()
    for c,r in selected:
        endpoint=(r.endpoint_ip,c.port)
        if endpoints[endpoint]>=4: continue
        endpoints[endpoint]+=1; diverse.append((c,r))
        if len(diverse)>=args.limit: break
    selected=diverse
    if mode=='full' and any(groups.values()):
        state['last_discovery']=now
    health='ok' if selected else 'degraded'
    if not selected:
        # A whole-run outage may be local. Retain only bounded, explicitly marked old results.
        # Never change last_success just because an old node was retained.
        for c in old:
            row=state['nodes'][c.key]
            if now-row['last_success']<args.ttl_hours*3600:
                selected.append((c,ProbeResult(**row['metrics'])))
        health='retained_previous' if selected else 'expired_or_empty'
    # Query countries ONLY for IPs actually used by successful probes; no independent hostname lookup.
    ips=list(dict.fromkeys(r.endpoint_ip for c,r in selected if c.key in passed and r.endpoint_ip))
    if ips:
        geo=geo_fn(ips,local/'geo_cache.json',workers=args.geo_workers)
        for c,r in selected:
            if c.key in passed:
                row=state['nodes'][c.key]
                prior_ip=row.get('country_ip')
                info=geo.get(r.endpoint_ip)
                if info and info.country_code:
                    row.update(country_code=info.country_code,country_ip=r.endpoint_ip)
                elif prior_ip!=r.endpoint_ip:
                    row.update(country_code=None,country_ip=r.endpoint_ip)
    if args.require_country:
        selected=[pair for pair in selected if state['nodes'][pair[0].key].get('country_code')]
    report={'mode':mode,'health':health,'tested_on':'operator network; local Xray',
            'xray_version':args.xray_version,'source_status':stats,'unique_selected_for_test':len(queue),
            'first_pass':first_counts,'second_pass':second_counts,'_verified_keys':list(passed),
            'max_tests':args.max_tests,'workers':first_workers,'verification_workers':min(args.workers,16),
            'first_pass_timeout_seconds':args.timeout,'screen_network_timeout_seconds':screen_timeout,
            'verification_timeout_seconds':args.timeout,
            'failure_reason':None if passed else 'No current verified transfer; previous nodes age normally'}
    report=write_outputs(root/'docs',selected,state,report,now,args.ttl_hours*3600)
    state['published']=[c.key for c,r in selected]
    state['last_attempt']=now
    save_state(history_path,state)
    atomic_write(local/'last_run.json',json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(f'[DONE] {len(selected)} nodes; health={health}',flush=True)
    return report


def arguments(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--xray',default=str(ROOT/'tools'/('xray.exe' if os.name=='nt' else 'xray')))
    ap.add_argument('--mode',choices=('auto','full','health'),default='auto')
    ap.add_argument('--limit',type=int,default=300)
    ap.add_argument('--max-tests',type=int,default=0,help='0 means all supported source entries; no per-source truncation')
    ap.add_argument('--workers',type=int,default=64)
    ap.add_argument('--screen-timeout',type=float,default=5,help='Discovery real-delay network deadline after Xray readiness; startup remains bounded by --timeout')
    ap.add_argument('--timeout',type=float,default=30,help='Full verification and health deadline per candidate including DNS and all probes')
    ap.add_argument('--recheck',type=int,default=400)
    ap.add_argument('--geo-workers',type=int,default=3)
    ap.add_argument('--require-country',action='store_true',help='Optional; otherwise preserve good nodes with a globe flag')
    ap.add_argument('--discovery-hours',type=float,default=4)
    ap.add_argument('--ttl-hours',type=float,default=6)
    ap.add_argument('--publish',action='store_true')
    args=ap.parse_args(argv)
    if not (1<=args.limit<=300 and 1<=args.workers<=64 and 1<=args.geo_workers<=8 and
            args.max_tests>=0 and 5<=args.timeout<=120 and 1<=args.screen_timeout<=args.timeout and args.recheck>=args.limit and
            1<=args.discovery_hours<=24 and 1<=args.ttl_hours<=24):
        ap.error('Invalid budget, worker count, deadline, TTL or discovery interval')
    binary=Path(args.xray).expanduser()
    resolved=binary.resolve() if binary.is_file() else shutil.which(args.xray)
    if not resolved: ap.error('Xray missing; run python download_xray.py first')
    args.xray=str(resolved)
    version=subprocess.run([args.xray,'version'],capture_output=True,text=True,timeout=10,check=True)
    args.xray_version=version.stdout.splitlines()[0]
    return args


def main(argv=None):
    args=arguments(argv)
    with run_lock(ROOT/'.local'/'run.lock'), keep_awake():
        run_cycle(args)
        if args.publish:
            publish(ROOT,args.xray,ROOT/'docs')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        # Redact source URLs, credentials, subprocess stderr and Git remote tokens.
        print(f'[ERROR] {type(exc).__name__}: run failed; inspect .local/last_run.json and logs.',file=sys.stderr)
        sys.exit(2)
