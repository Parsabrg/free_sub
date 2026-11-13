"""Collect, verify, remember and publish public proxy links on the operator's network."""
from __future__ import annotations
import argparse
import base64
import copy
from collections import Counter, deque
from contextlib import nullcontext
from dataclasses import asdict
import concurrent.futures as cf
from datetime import datetime, timezone
from functools import partial
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
from public_page import INDEX
from publishing import OUTPUTS, publish, validate_outputs, write_compact_feed
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
                     'video_host':result.video_host,'video_bytes':result.video_bytes,'video_ms':result.video_ms,
                     'verified_this_run':False})
        metrics=asdict(result)
        for key in ('stream_segments','stream_kbps','stream_start_ms','stream_stalls','stream_error',
                    'udp','ipv6','youtube_played','youtube_error'):
            if key in metrics:
                rows[-1][key]=metrics[key]
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
    write_compact_feed(staging,urls,rows,report)
    atomic_write(staging/'status.json',json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    atomic_write(staging/'index.html',INDEX)
    validate_outputs(staging)
    # Each replacement is atomic; the Pages deployment publishes one complete Git snapshot.
    docs.mkdir(parents=True,exist_ok=True)
    for name in OUTPUTS:
        atomic_write(docs/name,(staging/name).read_text(encoding='utf-8'))
    return report


def run_cycle(args, root=ROOT, probe=run_probe, collect_fn=collect, geo_fn=geolocate_hosts, now=None,
              registry=None, relay=False, publish_refresh=None):
    root=Path(root); live_clock=now is None; now=time.time() if live_clock else now
    local=root/'.local'; history_path=local/'history.json'
    state=read_state(history_path)
    mode=args.mode
    if mode=='auto':
        mode='full' if now-state.get('last_discovery',0)>=args.discovery_hours*3600 else 'health'
    control = getattr(args,'control_candidate',None)
    control_result = None
    if control is not None:
        control_result = probe(control,args.xray,args.timeout,quick=False)
        if control_result.error == 'VideoVerificationFailed':
            print('[PROBE] Control passed HTTPS but no video mirror returned a valid range.',flush=True)
            return run_cycle_report(args,root,state,mode,now,[],{}, {},{},0,geo_fn,
                                    probe_health='video_unavailable',control_error=control_result.error)
    priority_passed={}
    if mode=='full' and publish_refresh is not None and state.get('published'):
        refresh_args=copy.copy(args)
        refresh_args.mode='health'
        refresh_args.recheck=min(20,args.recheck)
        refresh_args.control_candidate=None
        print('[RUN] Refreshing the current feed before discovery.',flush=True)
        refreshed=run_cycle(refresh_args,root,probe,collect_fn,geo_fn,now if not live_clock else None)
        if publish_refresh is not False:
            publish_refresh()
        state=read_state(history_path)
        for verified in refreshed['nodes']:
            if verified['verified_this_run']:
                row=state['nodes'][verified['id']]
                candidate=parse(row['url'],row.get('source','history'))
                priority_passed[candidate.key]=(candidate,ProbeResult(**row['metrics']))
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
        if key in published_keys or not row.get('verified') or (
                row.get('last_success') and now-row['last_success']>args.ttl_hours*3600):
            continue
        candidate=parse(row.get('url',''),row.get('source','history'))
        if candidate: remaining.append(candidate)
    remaining.sort(key=lambda c:-reliability(state['nodes'][c.key],now))
    exploration=balanced(groups,str(int(now//1800))) if mode=='full' else remaining[:args.recheck]
    queue=[]; seen=set(priority_passed)
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
        passed=priority_passed | {c.key:(c,r) for c,r in second}
    else:
        passed={c.key:(c,r) for c,r in first if state['nodes'].get(c.key,{}).get('verified')}
    if live_clock: now=time.time()
    # Observe each real completion time, not the time the full batch eventually finishes.
    for c in queue:
        result=second_results.get(c.key,first_results[c.key])
        observe(state,c,result,result.completed_epoch if live_clock and result.completed_epoch else now,
                screening=mode=='full' and c.key not in second_results)
    passed={key:pair for key,pair in passed.items()
            if now-state['nodes'][key]['last_success']<args.ttl_hours*3600}
    if mode=='full':
        # What each source was worth this run, so a feed can be judged by what works from here.
        # "working" is a pass in the last test a config got this run. Verification takes only
        # the --recheck best-ranked finalists, so a new feed's configs can pass and miss it.
        tested={c.key for c in queue}
        working={c.key for c in queue if second_results.get(c.key,first_results[c.key]).passed}
        tested.update(priority_passed)
        working.update(priority_passed)
        for name,group in groups.items():
            keys={c.key for c in group}
            stats.setdefault(name,{}).update(tested=len(keys&tested),working=len(keys&working))
        if registry is not None:
            registry.record({name:(stats[name]['tested'],stats[name]['working']) for name in found},now)
        # Health runs carry no source results; the control panel shows the last discovery's.
        atomic_write(local/'source_status.json',json.dumps({'at':now,'sources':stats},indent=2,ensure_ascii=False)+'\n')
    if mode=='full' and any(groups.values()):
        # With most listed sources unreachable this was a network problem, not a discovery:
        # look again in an hour. One run had 3 of 23 feeds and still waited four (2026-11-12).
        listed=[row for row in stats.values() if row.get('kind')!='found']
        reached=sum(1 for row in listed if row.get('error') in (None,'NoSupportedLinks'))
        complete=2*reached>=len(listed)
        state['last_discovery']=now if complete else now-args.discovery_hours*3600+3600
        if not complete:
            print(f'[RUN] Only {reached} of {len(listed)} listed sources answered; discovery repeats in an hour.',flush=True)
    return run_cycle_report(args,root,state,mode,now,queue,passed,first_counts,second_counts,rested,geo_fn,stats,
                            probe_health=('healthy' if control_result.passed else 'control_failed') if control_result else None,
                            control_error=control_result.error if control_result else None)


def run_cycle_report(args,root,state,mode,now,queue,passed,first_counts,second_counts,rested,geo_fn,stats=None,
                     *,probe_health=None,control_error=None):
    local=root/'.local'
    selected=[]; endpoints=Counter()
    for c,r in sorted(passed.values(),key=lambda pair:rank_key(pair[0],pair[1],state,now)):
        endpoint=(r.endpoint_ip,c.port)
        if endpoints[endpoint]>=4: continue
        endpoints[endpoint]+=1; selected.append((c,r))
        if args.limit and len(selected)>=args.limit: break
    health='ok' if selected else 'degraded'
    if not selected:
        # A whole-run outage may be local. Retain only bounded, explicitly marked old results.
        # Never change last_success just because an old node was retained.
        for key in state.get('published',[]):
            row=state['nodes'].get(key,{})
            if row.get('last_success') and now-row['last_success']<args.ttl_hours*3600:
                c=parse(row['url'],row.get('source','history'))
                if c is None or c.key!=key: continue
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
            'xray_version':args.xray_version,'source_status':stats or {},'unique_selected_for_test':len(queue),
            'first_pass':first_counts,'second_pass':second_counts,'_verified_keys':list(passed),
            'list_limit':args.limit or None,'working_this_run':len(passed),
            'health_interval_seconds':int(getattr(args,'interval_minutes',30)*60),
            'discovery_interval_seconds':int(args.discovery_hours*3600),
            'resting_skipped':rested,'max_tests':args.max_tests,'workers':args.workers if mode=='full' else min(args.workers,16),
            'verification_workers':min(args.workers,16),'first_pass_timeout_seconds':args.timeout,
            'screen_network_timeout_seconds':args.screen_timeout if mode=='full' else None,
            'verification_timeout_seconds':args.timeout,
            'failure_reason':None if passed else 'No current verified transfer; previous nodes age normally'}
    if probe_health:
        report.update(probe_health=probe_health,probe_control_error=control_error or None)
    if probe_health=='video_unavailable':
        report['failure_reason']='Video mirrors failed through the control proxy. Previous verification timestamps were preserved.'
    report=write_outputs(root/'docs',selected,state,report,now,args.ttl_hours*3600)
    state['published']=[c.key for c,r in selected]
    state['last_attempt']=now
    save_state(local/'history.json',state)
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
    ap.add_argument('--probe-control',type=Path,help='Local file containing a known-good proxy share link')
    ap.add_argument('--video-mirror',action='append',default=[],help='HTTPS MP4 range URL, repeatable')
    ap.add_argument('--stream-segments',type=int,default=1,choices=range(1,9),metavar='1..8')
    ap.add_argument('--check-udp',action='store_true',help='Record a separate UDP relay result')
    ap.add_argument('--check-ipv6',action='store_true',help='Record a separate IPv6 result')
    ap.add_argument('--youtube-id',help='Measure actual playback of an embeddable YouTube video using Playwright')
    args=ap.parse_args(argv)
    from xray_test import YOUTUBE_ID, validate_video_mirrors
    try:
        args.video_mirror=validate_video_mirrors(args.video_mirror) if args.video_mirror else None
    except ValueError as exc:
        ap.error(str(exc))
    if args.youtube_id and not YOUTUBE_ID.fullmatch(args.youtube_id):
        ap.error('YouTube video ID must contain 11 letters, digits, underscores or hyphens')
    args.control_candidate=None
    if args.probe_control:
        try:
            with args.probe_control.expanduser().open(encoding='utf-8-sig') as handle:
                args.control_candidate=parse(handle.read(16385).strip())
        except (OSError,UnicodeError):
            ap.error('Unable to read the control proxy file')
        if args.control_candidate is None:
            ap.error('The control file must contain one supported proxy share link')
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
        probe=partial(run_probe,video_mirrors=args.video_mirror,stream_segments=args.stream_segments,
                      check_udp=args.check_udp,check_ipv6=args.check_ipv6,youtube_id=args.youtube_id)
        refresh=partial(publish,ROOT,args.xray,ROOT/'docs') if args.publish else False
        run_cycle(args,probe=probe,collect_fn=lambda **kw:collect(registry.builtin(),**kw),registry=registry,relay=True,
                  publish_refresh=refresh)
        if args.publish:
            publish(ROOT,args.xray,ROOT/'docs')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        if str(exc)=='Another subscription run is active':
            # Safe to say, and the control panel needs to tell it from a failure.
            print('[BUSY] Another run is already active (the scheduled task, or a second window).',file=sys.stderr)
            sys.exit(2)
        # Redact source URLs, credentials, subprocess stderr and Git remote tokens.
        print(f'[ERROR] {type(exc).__name__}: run failed; inspect .local/last_run.json and logs.',file=sys.stderr)
        sys.exit(2)
