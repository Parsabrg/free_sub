"""Compare two Xray versions using loopback fixtures and an optional local feed snapshot."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from collector import parse
from network import physical_interface
from publishing import read_snapshot
from xray_test import run_probe


def replay(snapshot, binaries, maximum, interface):
    report,urls,_=read_snapshot(snapshot)
    groups=defaultdict(list)
    for row,url in zip(report['nodes'],urls):
        if row['expires_epoch']>time.time():
            candidate=parse(url)
            groups[candidate.protocol].append(candidate)
    if not groups:
        raise ValueError('No current configs to replay')
    sample=[]
    for index in range(max(map(len,groups.values()))):
        sample.extend(group[index] for group in groups.values() if index<len(group))
    def measure(item):
        index,candidate=item
        order=range(2) if index%2==0 else reversed(range(2))
        results=[None,None]
        for version in order:
            result=run_probe(candidate,binaries[version],interface=interface)
            results[version]={'passed':result.passed,'stage':result.stage,'error':result.error}
        return {'id':candidate.key,'protocol':candidate.protocol,'baseline':results[0],'candidate':results[1]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(measure,enumerate(sample[:maximum])))


def run_check(command, timeout):
    try:
        return subprocess.run(command,cwd=ROOT,timeout=timeout).returncode==0
    except subprocess.TimeoutExpired:
        print(f'{Path(command[1]).name} exceeded its deadline.',flush=True)
        return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline',type=Path)
    parser.add_argument('candidate',type=Path)
    parser.add_argument('--snapshot',type=Path)
    parser.add_argument('--sample',type=int,choices=range(1,41),default=12,metavar='1..40')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    binaries=[path.resolve(strict=True) for path in (args.baseline,args.candidate)]
    if binaries[0]==binaries[1]:
        parser.error('Provide two different Xray binaries')
    fixtures={}
    for role,binary in zip(('baseline','candidate'),binaries):
        version=subprocess.check_output([binary,'version'],text=True,timeout=10).splitlines()[0]
        print(f'{role}: {version}',flush=True)
        with TemporaryDirectory(prefix='free-sub-compare-') as tmp:
            regression=run_check([sys.executable,'verify.py','--xray',str(binary),'--evidence',
                                  str(Path(tmp)/'verification.json')],180)
        client=run_check([sys.executable,'scripts/check_client_release.py','--core-only','--xray',str(binary)],90)
        fixtures[role]={'version':version,'regression_passed':regression,'client_fixtures_passed':client}
    live=replay(args.snapshot,binaries,args.sample,physical_interface()) if args.snapshot else []
    losses=sum(row['baseline']['passed'] and not row['candidate']['passed'] for row in live)
    gains=sum(row['candidate']['passed'] and not row['baseline']['passed'] for row in live)
    result={'fixtures':fixtures,'live':live,'losses':losses,'gains':gains}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(f'{len(live)} live comparisons, {gains} gains, {losses} losses.')
    if losses or not all(row['regression_passed'] and row['client_fixtures_passed'] for row in fixtures.values()):
        raise SystemExit(1)


if __name__=='__main__':
    main()
