"""Pre-push gate: regression tests plus real encrypted Xray end-to-end fixtures."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from publishing import source_fingerprint
from state import atomic_write

ROOT=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--xray',required=True)
    args=ap.parse_args()
    xray=Path(args.xray).expanduser().resolve()
    if not xray.is_file(): raise SystemExit('An actual Xray binary is required; verification cannot skip it')
    env=os.environ.copy()
    env.update(XRAY_E2E_BINARY=str(xray),PYTHONIOENCODING='utf-8')
    started=time.monotonic()
    # Capture for the durable report, display complete non-secret regression results.
    result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-v'],
                          cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',timeout=150)
    output=result.stdout+result.stderr
    # Test chatter (including a test's own "[PUBLISH] ... pushed" line) read like real run
    # events in the log; show it only when something failed. verification.json keeps it all.
    if result.returncode: print(output,end='',flush=True)
    else: print('[VERIFY] '+(output.strip().splitlines() or [''])[-3:][0].strip()+' / '+(output.strip().splitlines() or [''])[-1].strip(),flush=True)
    evidence={'verified_utc_epoch':time.time(),'passed':result.returncode==0,
              'source_sha256':source_fingerprint(ROOT),'xray_binary_sha256':hashlib.sha256(xray.read_bytes()).hexdigest(),
              'elapsed_seconds':round(time.monotonic()-started,2),'output':output,
              'scope':'Actual Xray core with local Trojan TLS server and trusted HTTPS origin; no public-node availability claim'}
    atomic_write(ROOT/'.local'/'verification.json',json.dumps(evidence,indent=2)+'\n')
    if result.returncode: raise SystemExit(result.returncode)
    print('[VERIFY] Regression and actual Xray end-to-end checks passed.',flush=True)


if __name__=='__main__':
    main()
