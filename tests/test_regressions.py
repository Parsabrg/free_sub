import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from collector import collect, parse
from network import pin_endpoint, resolve_public
from process_guard import communicate_bounded
from publishing import validate_outputs
from state import read_state, observe, reliability, run_lock
from xray_test import ProbeResult


class RegressionTests(unittest.TestCase):
    def test_parallel_worker_budget(self):
        import sys
        from main import arguments
        version = subprocess.CompletedProcess([], 0, stdout='Xray fixture')
        with patch('main.subprocess.run', return_value=version):
            defaults=arguments(['--xray', sys.executable])
            self.assertEqual((defaults.workers, defaults.max_tests, defaults.screen_timeout, defaults.timeout), (64, 0, 2, 30))
            self.assertEqual(arguments(['--xray', sys.executable, '--workers', '64']).workers, 64)
            with self.assertRaises(SystemExit), patch('sys.stderr', new=io.StringIO()):
                arguments(['--xray', sys.executable, '--workers', '65'])
            with self.assertRaises(SystemExit), patch('sys.stderr', new=io.StringIO()):
                arguments(['--xray', sys.executable, '--screen-timeout', '0.5'])

    def test_uncapped_discovery_uses_fast_screen_and_full_verification(self):
        import sys
        from main import arguments, run_cycle
        candidates=[parse(f'trojan://fixture{i}@example.com:443?security=tls') for i in range(1601)]
        calls=[]
        def probe(candidate, xray, timeout, *, quick, screen_timeout=None):
            calls.append((candidate.key, timeout, quick, screen_timeout))
            return ProbeResult(candidate.key==candidates[0].key, endpoint_ip='8.8.8.8', body_bytes=16384)
        version=subprocess.CompletedProcess([], 0, stdout='Xray fixture')
        with patch('main.subprocess.run', return_value=version):
            args=arguments(['--xray', sys.executable, '--mode', 'full', '--workers', '4'])
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()):
            report=run_cycle(args, folder, probe, lambda **kw: {'Fixture':candidates}, lambda *a, **kw: {})
            self.assertEqual(report['unique_selected_for_test'],1601)
            self.assertEqual({k for k,t,q,n in calls if q},{c.key for c in candidates})
            self.assertTrue(all(t==30 and n==2 for k,t,q,n in calls if q))
            self.assertEqual([(t,q,n) for k,t,q,n in calls if not q],[(30,False,None)])
            self.assertEqual(report['published'],1)
            calls.clear()
            args.mode='health'
            run_cycle(args, folder, probe, lambda **kw: {}, lambda *a, **kw: {})
            self.assertEqual([(t,q,n) for k,t,q,n in calls],[(30,True,None)])

    def test_network_deadline_starts_after_worker_readiness(self):
        import sys
        command=[sys.executable, '-u', '-c',
                 "import time; time.sleep(0.4); print('READY',flush=True); time.sleep(0.3); print('done')"]
        output=communicate_bounded(command, '', 3, ready_timeout=0.5)
        self.assertEqual(output.strip(),'done')
        stalled=[sys.executable, '-u', '-c',
                 "import time; time.sleep(0.4); print('READY',flush=True); time.sleep(30)"]
        start=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            communicate_bounded(stalled, '', 3, ready_timeout=0.2)
        self.assertLess(time.monotonic()-start,2)

        startup_stall=[sys.executable, '-c', 'import time; time.sleep(30)']
        start=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            communicate_bounded(startup_stall, '', 0.2, ready_timeout=0.1)
        self.assertLess(time.monotonic()-start,2)

    def test_probe_deadline_removes_worker_temp_files(self):
        from xray_test import run_probe
        workspace=[]
        def timeout(command, payload, *args, **kwargs):
            folder=Path(json.loads(payload)['work_dir'])
            workspace.append(folder)
            (folder/'worker-files').mkdir()
            (folder/'worker-files/config.json').write_text('{}')
            raise subprocess.TimeoutExpired(command,2)
        with patch('xray_test.communicate_bounded',side_effect=timeout):
            result=run_probe(parse('trojan://fixture@example.com:443?security=tls'),'unused',30,
                             quick=True,screen_timeout=2)
        self.assertEqual(result.error,'CandidateDeadline')
        self.assertEqual(len(workspace),1)
        self.assertFalse(workspace[0].exists())

    def test_strict_url_settings_and_grpc_mode(self):
        base='vless://id@example.com:443?security=tls'
        self.assertIsNone(parse(base+'&type=xhttp&extra=%7B%7D'))
        self.assertIsNone(parse(base+'&headerType=http'))
        self.assertIsNone(parse(base+'&security=none'))
        self.assertIsNone(parse(base+'&allowinsecure=1'))
        self.assertIsNone(parse(base+'&encryption=unsupported'))
        self.assertIsNone(parse(base+'&unknown=1'))
        self.assertTrue(parse(base+'&type=grpc&mode=multi').outbound['streamSettings']['grpcSettings']['multiMode'])
        self.assertFalse(parse(base+'&type=grpc&mode=gun').outbound['streamSettings']['grpcSettings']['multiMode'])

    def test_vmess_insecure_and_extensions_rejected(self):
        obj={'add':'example.com','port':443,'id':'id','tls':'tls','net':'ws'}
        encode=lambda v:'vmess://'+base64.b64encode(json.dumps(v).encode()).decode()
        for extra in ({'allowInsecure':True},{'insecure':'1'},{'extra':{}},{'type':'http'}):
            self.assertIsNone(parse(encode(dict(obj,**extra))))
        self.assertIsNotNone(parse(encode(obj)))

    def test_all_source_entries_collected_without_hidden_cap(self):
        feed='\n'.join(f'trojan://password{i}@example.com:443?security=tls' for i in range(1805))
        with patch('collector.get',return_value=feed):
            self.assertEqual(len(collect({'Fixture':'https://example.com'})['Fixture']),1805)

    def test_no_private_or_mixed_dns_endpoint(self):
        for address in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','224.0.0.1'):
            with self.assertRaises(ValueError): resolve_public(address)
        completed=subprocess.CompletedProcess([],0,stdout='["8.8.8.8","127.0.0.1"]')
        with patch('network.subprocess.run',return_value=completed):
            with self.assertRaises(ValueError): resolve_public('mixed.example')
        c=parse('trojan://pw@example.com:443?security=tls&sni=example.com')
        pinned=pin_endpoint(c.outbound,['8.8.8.8'])
        self.assertEqual(pinned['settings']['servers'][0]['address'],'8.8.8.8')
        self.assertEqual(pinned['streamSettings']['tlsSettings']['serverName'],'example.com')
        self.assertEqual(c.outbound['settings']['servers'][0]['address'],'example.com')

    def test_lock_does_not_allow_overlapping_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'lock'
            with run_lock(path):
                with self.assertRaises(RuntimeError):
                    with run_lock(path): pass
            with run_lock(path): pass

    def test_stable_identity_and_history(self):
        with tempfile.TemporaryDirectory() as folder:
            state=read_state(Path(folder)/'history.json')
            c=parse('trojan://pw@example.com:443?security=tls')
            good=ProbeResult(True,latency_ms=10,endpoint_ip='8.8.8.8')
            row=observe(state,c,good,100,verified=True)
            number=row['number']
            observe(state,c,good,200,verified=True)
            self.assertEqual(row['number'],number)
            rate=reliability(row,200)
            observe(state,c,ProbeResult(False),300)
            self.assertLess(reliability(row,300),rate)
            self.assertEqual(row['last_success'],200)
            self.assertTrue(row['verified'])

    def test_output_validator_rejects_bad_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'sub.txt').write_text('one')
            (root/'sub.b64').write_text(base64.b64encode(b'two').decode())
            with self.assertRaises(ValueError): validate_outputs(root)

    def test_worker_deadline_cleans_process(self):
        import sys
        start=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            communicate_bounded([sys.executable,'-c','import time; time.sleep(30)'],'',0.2)
        self.assertLess(time.monotonic()-start,3)

    def test_publisher_refuses_staged_work_and_retries_push(self):
        from publishing import publish
        calls=[]
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();(root/'docs').mkdir()
            def fake(command,**kwargs):
                calls.append(command)
                if command[0]!='git': return subprocess.CompletedProcess(command,0)
                args=command[1:]
                out=''
                if args==['rev-parse','--show-toplevel']: out=str(root)+'\n'
                elif args==['branch','--show-current']: out='main\n'
                elif args==['diff','--cached','--name-only']: out='unrelated.txt\n'
                return subprocess.CompletedProcess(command,0,stdout=out,stderr='')
            with patch('publishing.subprocess.run',side_effect=fake),patch('publishing.validate_outputs'):
                with self.assertRaisesRegex(RuntimeError,'staged work'): publish(root,'xray',root/'docs')
            self.assertFalse(any(c[:2]==['git','push'] for c in calls))
            calls.clear()
            def clean(command,**kwargs):
                result=fake(command,**kwargs)
                if command[:5]==['git','diff','--cached','--name-only']: result.stdout=''
                return result
            with patch('publishing.subprocess.run',side_effect=clean),patch('publishing.validate_outputs'):
                publish(root,'xray',root/'docs')
            self.assertTrue(any(c[:2]==['git','push'] for c in calls))
            self.assertFalse(any(c[:2]==['git','commit'] for c in calls))
