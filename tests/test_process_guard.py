import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from process_guard import stray_xray
from state import run_lock


class ProcessGuardTests(unittest.TestCase):
    def test_stray_xray_only_matches_orphans_of_this_binary(self):
        xray = str(Path('tools/xray.exe').resolve())
        rows = [
            (10, 1, 100, 'python.exe'),              # a live run
            (11, 10, 200, xray),                     # its Xray: owned
            (12, 99, 200, xray),                     # parent gone
            (13, 14, 200, xray),                     # PID 14 reused after 13 started
            (14, 1, 300, 'other.exe'),
            (15, 99, 200, str(Path('v2rayN/xray.exe').resolve())),   # someone else's Xray
        ]
        self.assertEqual(stray_xray(rows, xray), [12, 13])

    @unittest.skipUnless(os.name == 'nt', 'job objects are Windows only')
    def test_contained_process_takes_its_descendants_with_it(self):
        # A parent killed outright (Task Scheduler's limit, taskkill) must not leave its children.
        code = textwrap.dedent('''
            import subprocess, sys, time
            from process_guard import contain_self
            contain_self()
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            print(child.pid, flush=True)
            time.sleep(60)
        ''')
        root = Path(__file__).resolve().parent.parent
        parent = subprocess.Popen([sys.executable, '-c', code], cwd=root, stdout=subprocess.PIPE, text=True)
        child = int(parent.stdout.readline())
        parent.kill(); parent.wait(); parent.stdout.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            alive = str(child) in subprocess.run(['tasklist', '/FI', f'PID eq {child}', '/NH'],
                                                 capture_output=True, text=True, timeout=10).stdout
            if not alive:
                break
            time.sleep(0.2)
        self.assertFalse(alive, 'the grandchild outlived its contained parent')

    def test_run_lock_file_does_not_grow(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.lock'
            for _ in range(3):
                with run_lock(path): pass
            self.assertEqual(path.read_bytes(), b'0')


if __name__ == '__main__':
    unittest.main()
