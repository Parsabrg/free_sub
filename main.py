"""Collect, verify, remember and publish public proxy links on the operator's network."""
from __future__ import annotations
import argparse
import base64
import copy
from collections import Counter, deque
from contextlib import nullcontext
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
from naming import assign_labels, node_name, rename_share_link
from publishing import publish, validate_outputs
from relay import fallback
from sources import Registry
from state import atomic_write, keep_awake, observe, read_state, reliability, run_lock, save_state, sweep_stale_probe_dirs
from network import BIND_ENV, physical_interface
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


def resting(row, now, hours):
    """A config that failed its last three tests and has not worked for three days waits
    before the next one. In one discovery run on 2026-11-12, 5,127 of 7,439 screening
    tests ended in a timeout, and every one of those configs was tested again four hours
    later; found feeds need that room."""
    if not hours or not row or now-row.get('last_success',0)<3*86400:
        return False
    history=row.get('history',[])
    return (len(history)>=3 and not any(e['ok'] for e in history[-3:])
            and now-row.get('last_attempt',0)<hours*3600)


def pick(candidates, quota, state, rng):
    """Up to quota of a found feed's configs: never-tested ones first, then the longest untested."""
    if len(candidates)<=quota:
        return candidates
    shuffled=rng.sample(candidates,len(candidates))
    shuffled.sort(key=lambda c:state['nodes'].get(c.key,{}).get('last_attempt',0))
    return shuffled[:quota]


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
    assign_labels(selected,state['nodes'],now)
    for rank,(candidate,result) in enumerate(selected,1):
        row=state['nodes'][candidate.key]
        name=node_name(country_flag(row.get('country_code')),*row['label'],candidate.outbound)
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
                  recommended_client_refresh_seconds=3600,node_ttl_seconds=ttl)
    # run_cycle states what it was started with; these are only the figures of earlier versions.
    report.setdefault('health_interval_seconds',1800)
    report.setdefault('discovery_interval_seconds',14400)
    report.setdefault('list_limit',None)
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


INDEX='''<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Free Sub: tested Xray subscription</title>
<style>
:root{color-scheme:light dark;--bg:#f4f5f7;--panel:#fff;--ink:#18202b;--muted:#5f6b7a;--line:#dfe3ea;--accent:#0b6bcb;--good:#18794e;--warn:#a15c00;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f141b;--panel:#171e28;--ink:#e3eaf3;--muted:#93a0b2;--line:#2a3442;--accent:#5aa9f5;--good:#4cc38a;--warn:#f0b35a;--chip:#212b38}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px 56px;display:grid;gap:16px}
main>*{min-width:0}
h1{font-size:26px;margin:0;letter-spacing:-.01em}
h2{font-size:13px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
p{margin:0}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
#health{font-weight:600}
.good{color:var(--good)}.warn{color:var(--warn)}
.links{display:grid;gap:10px}
.links div{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px}
.links span{flex:0 0 150px;color:var(--muted)}
.links a{flex:1 1 220px;min-width:0;color:var(--accent);overflow-wrap:anywhere}
button{font:inherit;font-size:14px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);padding:3px 12px;cursor:pointer}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips span{background:var(--chip);border-radius:999px;padding:2px 10px;font-size:14px;font-variant-numeric:tabular-nums}
.notes{color:var(--muted);font-size:14px;display:grid;gap:6px}
</style>
<main>
<header><h1>Free Sub</h1><p>Public proxy configurations, each tested with real HTTPS traffic through the Xray core before it is listed.</p></header>
<section><h2>Status</h2><p id="health">Loading test status…</p></section>
<section><h2>Subscription</h2><div class="links" id="links"></div></section>
<section id="summary" hidden><h2>In the list</h2><div class="chips" id="protocols"></div><div class="chips" id="countries" style="margin-top:8px"></div></section>
<section class="notes"><p>Use the Xray core in your client and refresh the subscription about once an hour.</p>
<p>Tests run on the operator’s network; a node can behave differently on yours. A flag shows where the connection endpoint is, which may be a CDN rather than the exit country.</p>
<p>These are public proxies run by unknown operators. Do not send anything through them that you would not send over an untrusted network.</p></section>
</main>
<script>
const base=location.href.replace(/[^/]*$/,'');
for(const [label,file] of [['v2rayN (desktop)','xray.json'],['v2rayNG (Android)','sub.b64'],['Plain list','sub.txt'],['Test report','status.json']]){
  const row=document.createElement('div'),name=document.createElement('span'),link=document.createElement('a'),copy=document.createElement('button');
  name.textContent=label;link.href=file;link.textContent=base+file;copy.textContent='Copy';
  copy.onclick=async()=>{try{await navigator.clipboard.writeText(base+file);copy.textContent='Copied'}catch(e){copy.textContent='Select and copy'}setTimeout(()=>copy.textContent='Copy',1500)};
  row.append(name,link,copy);document.querySelector('#links').append(row);
}
function chips(id,counts){const box=document.querySelector(id);for(const [key,n] of Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,12)){const chip=document.createElement('span');chip.textContent=key+' '+n;box.append(chip)}}
fetch('status.json',{cache:'no-store'}).then(r=>{if(!r.ok)throw Error();return r.json()}).then(s=>{
  const now=Date.now()/1000,fresh=s.nodes.filter(n=>n.expires_epoch>now),age=Math.max(0,Math.round((now-s.generated_epoch)/60)),e=document.querySelector('#health');
  e.textContent=`${fresh.length} unexpired of ${s.published} nodes; last attempt ${age} minutes ago; mode: ${s.mode}; state: ${s.health}.`;
  e.className=(age>60||s.health!=='ok'||fresh.length===0)?'warn':'good';
  const protocols={},countries={};
  for(const n of fresh){protocols[n.protocol]=(protocols[n.protocol]||0)+1;const c=n.country_code||'unknown';countries[c]=(countries[c]||0)+1}
  if(fresh.length){chips('#protocols',protocols);chips('#countries',countries);document.querySelector('#summary').hidden=false}
}).catch(()=>{const e=document.querySelector('#health');e.textContent='Status unavailable; freshness unknown.';e.className='warn'});
</script>
</html>
'''


def run_cycle(args, root=ROOT, probe=run_probe, collect_fn=collect, geo_fn=geolocate_hosts, now=None, registry=None, relay=False):
    root=Path(root); live_clock=now is None; now=time.time() if live_clock else now
    local=root/'.local'; history_path=local/'history.json'
    state=read_state(history_path)
    mode=args.mode
    if mode=='auto':
        mode='full' if now-state.get('last_discovery',0)>=args.discovery_hours*3600 else 'health'
    # Existing entries are always tested first, including when all sources are unavailable.
    old=[]
    for key in state.get('published',[]):
        row=state['nodes'].get(key,{})
        candidate=parse(row.get('url',''),row.get('source','history'))
        if candidate and row.get('verified'): old.append(candidate)
    stats={}; groups={}; found=set(); rested=0
    if mode=='full':
        extra={}
        # The best published nodes carry a fetch that fails directly; none is started otherwise.
        with fallback(args.xray,old[:5]) if relay else nullcontext():
            groups=collect_fn(stats=stats)
            if registry is not None:
                own,extra=registry.collect(now,stats)
                groups.update(own)
        hours=getattr(args,'retry_failed_hours',0)
        for name,group in groups.items():
            awake=[c for c in group if not resting(state['nodes'].get(c.key),now,hours)]
            rested+=len(group)-len(awake); groups[name]=awake
        # A found feed is tested only on what the listed sources do not already carry.
        carried={c.key for group in groups.values() for c in group}
        rng=random.Random(str(int(now//1800)))
        for name,group in extra.items():
            fresh=[c for c in group if c.key not in carried
                   and not resting(state['nodes'].get(c.key),now,hours)]
            stats[name]['new']=len(fresh)
            groups[name]=pick(fresh,registry.quota(name,now),state,rng)
            found.add(name)
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
    working_count=len(passed)
    if mode=='full':
        # What each source was worth this run, so a feed can be judged by what works from here.
        # "working" is a pass in the last test a config got this run. Verification takes only
        # the --recheck best-ranked finalists, so a new feed's configs can pass and miss it.
        tested={c.key for c in queue}
        working={c.key for c in queue if second_results.get(c.key,first_results[c.key]).passed}
        for name,group in groups.items():
            keys={c.key for c in group}
            stats.setdefault(name,{}).update(tested=len(keys&tested),working=len(keys&working))
        if registry is not None:
            registry.record({name:(stats[name]['tested'],stats[name]['working']) for name in found},now)
        # Health runs carry no source results; the control panel shows the last discovery's.
        atomic_write(local/'source_status.json',json.dumps({'at':now,'sources':stats},indent=2,ensure_ascii=False)+'\n')
    selected=sorted(passed.values(),key=lambda pair:rank_key(pair[0],pair[1],state,now))
    diverse=[]; endpoints=Counter()
    for c,r in selected:
        endpoint=(r.endpoint_ip,c.port)
        if endpoints[endpoint]>=4: continue
        endpoints[endpoint]+=1; diverse.append((c,r))
        if args.limit and len(diverse)>=args.limit: break
    selected=diverse
    if mode=='full' and any(groups.values()):
        # With most listed sources unreachable this was a network problem, not a discovery:
        # look again in an hour. One run had 3 of 23 feeds and still waited four (2026-11-12).
        listed=[row for row in stats.values() if row.get('kind')!='found']
        reached=sum(1 for row in listed if row.get('error') in (None,'NoSupportedLinks'))
        complete=2*reached>=len(listed)
        state['last_discovery']=now if complete else now-args.discovery_hours*3600+3600
        if not complete:
            print(f'[RUN] Only {reached} of {len(listed)} listed sources answered; discovery repeats in an hour.',flush=True)
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
            'list_limit':args.limit or None,'working_this_run':working_count,
            'health_interval_seconds':int(getattr(args,'interval_minutes',30)*60),
            'discovery_interval_seconds':int(args.discovery_hours*3600),
            'resting_skipped':rested,'max_tests':args.max_tests,'workers':first_workers,'verification_workers':min(args.workers,16),
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
    ap.add_argument('--limit',type=int,default=0,help='Most nodes in the published list; 0 lists every working node')
    ap.add_argument('--max-tests',type=int,default=0,help='0 means all supported source entries; no per-source truncation')
    ap.add_argument('--workers',type=int,default=64)
    ap.add_argument('--screen-timeout',type=float,default=5,help='Discovery real-delay network deadline after Xray readiness; startup remains bounded by --timeout')
    ap.add_argument('--timeout',type=float,default=30,help='Full verification and health deadline per candidate including DNS and all probes')
    # 600, not 400: 422 to 505 configs passed each screen on 2026-11-12, so 22 to 105 passes a
    # run were never verified or listed. A config with no history ranks below a known one.
    ap.add_argument('--recheck',type=int,default=None,
                    help='Finalists verified per discovery run; standby nodes per health check (default 600, or --limit if larger)')
    ap.add_argument('--interval-minutes',type=int,default=30,
                    help='How often runs are started, as published in status.json; the panel or the scheduled task does the scheduling')
    ap.add_argument('--geo-workers',type=int,default=3)
    ap.add_argument('--require-country',action='store_true',help='Optional; otherwise preserve good nodes with a globe flag')
    ap.add_argument('--discovery-hours',type=float,default=4)
    ap.add_argument('--ttl-hours',type=float,default=6)
    ap.add_argument('--publish',action='store_true')
    ap.add_argument('--found-sources',type=int,default=40,
                    help='Feeds found on GitHub to test per discovery run; 0 uses only the listed sources')
    ap.add_argument('--retry-failed-hours',type=float,default=24,
                    help='Wait before retesting a config that failed three times running; 0 retests every run')
    ap.add_argument('--allow-non-iran',action='store_true',
                    help='Test even when this machine does not reach the internet from Iran (e.g. through a VPN)')
    args=ap.parse_args(argv)
    if args.recheck is None:
        args.recheck=max(600,args.limit)
    if not (0<=args.limit<=1000 and 1<=args.workers<=64 and 1<=args.geo_workers<=8 and
            args.max_tests>=0 and 5<=args.timeout<=120 and 1<=args.screen_timeout<=args.timeout and args.recheck>=args.limit and
            1<=args.discovery_hours<=24 and 1<=args.ttl_hours<=24 and 1<=args.interval_minutes<=1440 and
            0<=args.found_sources<=200 and 0<=args.retry_failed_hours<=168):
        ap.error('Invalid list size, budget, worker count, deadline, TTL, interval or source count')
    binary=Path(args.xray).expanduser()
    resolved=binary.resolve() if binary.is_file() else shutil.which(args.xray)
    if not resolved: ap.error('Xray missing; run python download_xray.py first')
    args.xray=str(resolved)
    version=subprocess.run([args.xray,'version'],capture_output=True,text=True,timeout=10,check=True)
    args.xray_version=version.stdout.splitlines()[0]
    return args


def egress_country(xray, interface=None, timeout=10):
    """The country tests reach the internet from, as Cloudflare sees it, or
    None if that cannot be told (offline, or the trace is filtered).

    Asked through Xray bound to [interface] when there is one, exactly as the
    tests dial: a VPN's default route, or a system proxy that urllib would
    follow, says nothing about the path the tests take."""
    import tempfile
    from xray_test import allocate_port, check_ready, https_get_via_socks
    import ssl
    port=allocate_port()
    out={'protocol':'freedom'}
    if interface:
        out['streamSettings']={'sockopt':{'interface':interface}}
    cfg={'log':{'loglevel':'none'},
         'inbounds':[{'listen':'127.0.0.1','port':port,'protocol':'socks','settings':{'udp':False}}],
         'outbounds':[out]}
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'egress.json'
        path.write_text(json.dumps(cfg),encoding='utf-8')
        proc=subprocess.Popen([xray,'run','-c',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            check_ready(port,proc)
            body,_=https_get_via_socks(port,'https://www.cloudflare.com/cdn-cgi/trace',timeout,
                                       ssl.create_default_context())
            for line in body.decode('ascii','replace').splitlines():
                if line.startswith('loc='):
                    return line[4:].strip() or None
        except Exception:
            return None
        finally:
            proc.kill(); proc.wait()
    return None


def main(argv=None):
    args=arguments(argv)
    # Results only mean something from the network the subscription is for.
    # This PC is often on a VPN; tests from there passed or failed configs for
    # the VPN's network, while status.json said "operator network".
    # Tests bind to the real adapter, so Nebula, v2rayN's TUN or any other VPN
    # can stay on: the operator's network is still what is measured.
    interface=physical_interface()
    if interface:
        os.environ[BIND_ENV]=interface
    country=egress_country(args.xray,interface)
    print(f'[NETWORK] testing through {interface or "the default route"}; egress country {country or "unknown"}',flush=True)
    if country and country!='IR' and not args.allow_non_iran:
        print(f'[SKIP] Tests would reach the internet from {country}, not Iran, and no real '
              'adapter bypasses it; the published list is kept as it is.',flush=True)
        return
    with run_lock(ROOT/'.local'/'run.lock'), keep_awake():
        removed=sweep_stale_probe_dirs()
        if removed: print(f'[CLEANUP] Removed {removed} stale probe folders from the temp directory.',flush=True)
        registry=Registry(ROOT/'.local'/'sources.json',discover=args.found_sources)
        run_cycle(args,collect_fn=lambda **kw:collect(registry.builtin(),**kw),registry=registry,relay=True)
        if args.publish:
            publish(ROOT,args.xray,ROOT/'docs')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        if str(exc)=='Remote diverged; update the clone before publishing':
            print('[UPDATE] GitHub has commits this copy lacks (usually a newer version of the program), so '
                  'nothing was published. Press "Update program" in the panel, or run: '
                  'git pull --no-rebase --no-edit',file=sys.stderr)
            sys.exit(2)
        if str(exc)=='Another subscription run is active':
            # Safe to say, and the control panel needs to tell it from a failure.
            print('[BUSY] Another run is already active (the scheduled task, or a second window).',file=sys.stderr)
            sys.exit(2)
        # Redact source URLs, credentials, subprocess stderr and Git remote tokens.
        print(f'[ERROR] {type(exc).__name__}: run failed; inspect .local/last_run.json and logs.',file=sys.stderr)
        sys.exit(2)
