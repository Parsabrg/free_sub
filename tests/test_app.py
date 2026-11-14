import http.client
import http.server
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import app
import sources


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


class PanelTests(unittest.TestCase):
    def setUp(self):
        # Whatever this PC has installed, a panel test starts without the scheduled task.
        task = patch('app.task_installed', return_value=False)
        task.start()
        self.addCleanup(task.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.script = 'print("[CHECK] 25/100; passed=3; elapsed=1s", flush=True)'
        command = lambda root, mode, publish: [sys.executable, '-u', '-c', self.script, mode, str(publish)]
        self.runner = app.Runner(self.root, command, interval=0.2)
        self.panel = app.Panel(self.root, self.runner)
        self.server = app.serve(self.panel)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.runner.stop)

    def request(self, method, path, body=None, token=True, host=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Host': host or f'127.0.0.1:{self.port}'}
        if token:
            headers['X-Token'] = self.panel.token
        connection.request(method, path, None if body is None else json.dumps(body), headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def state(self):
        return json.loads(self.request('GET', '/api/state')[1])

    def test_private_test_is_authenticated_and_returns_no_credentials(self):
        from xray_test import ProbeResult
        tools=self.root/'tools'
        tools.mkdir()
        (tools/('xray.exe' if os.name=='nt' else 'xray')).touch()
        secret='credential-fixture'
        body={'text':f'trojan://{secret}@example.com:443\nvless://invalid','udp':True}
        self.assertEqual(self.request('POST','/api/test',body,token=False)[0],403)
        with patch('app.physical_interface',return_value='Ethernet'), patch('xray_test.run_probe',
                return_value=ProbeResult(True,stage='passed',video_bytes=131072,udp=True)) as probe:
            status,response=self.request('POST','/api/test',body)
        self.assertEqual(status,200)
        self.assertNotIn(secret.encode(),response)
        rows=json.loads(response)['results']
        self.assertEqual([(row['stage'],row['passed']) for row in rows],[('passed',True),('parse',False)])
        self.assertEqual(probe.call_args.kwargs['interface'],'Ethernet')
        self.assertFalse(self.panel.local.exists())

    def test_only_this_computers_own_page_can_use_the_panel(self):
        status, page = self.request('GET', '/', token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.panel.token.encode(), page)
        self.assertEqual(self.request('GET', '/', host='evil.example')[0], 403)
        self.assertEqual(self.request('GET', '/api/state', token=False)[0], 403)   # a stale page reloads
        self.assertEqual(self.request('GET', '/api/nothing', token=False)[0], 404)
        self.assertEqual(self.request('POST', '/api/start', {'mode': 'full'}, token=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/start', host=f'evil.example:{self.port}')[0], 403)
        self.assertEqual(self.state()['run']['state'], 'idle')

    def test_one_run_then_idle_and_settings_are_remembered(self):
        status, _ = self.request('POST', '/api/start', {'mode': 'full', 'publish': False, 'repeat': False})
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))
        state = self.state()
        self.assertEqual(state['run']['exit'], 0)
        self.assertTrue(any('[CHECK] 25/100' in line for line in state['log']))
        self.assertEqual(state['settings'], {'mode': 'full', 'publish': False, 'repeat': False, 'limit': 0, 'interval': 30})
        self.assertEqual(self.request('POST', '/api/start', {'mode': 'everything'})[0], 400)

    def test_a_run_that_ends_before_it_is_contained_still_counts(self):
        # Windows refuses to put an already finished process in a job. On CI a run that
        # printed one line and exited was then reported as "could not start" (exit -1).
        from unittest.mock import patch
        def too_late(proc):
            proc.wait()
            raise OSError('Cannot contain probe process tree')
        with patch('app.ProcessTree', too_late):
            self.request('POST', '/api/start', {'mode': 'health', 'publish': False, 'repeat': False})
            self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))
        state = self.state()
        self.assertEqual(state['run']['exit'], 0)
        self.assertTrue(any('[CHECK] 25/100' in line for line in state['log']))

    def test_quit_stops_the_run_and_ends_the_server(self):
        ended = threading.Event()
        self.panel.shutdown = ended.set
        self.script = 'import time; print("started", flush=True); time.sleep(60)'
        self.request('POST', '/api/start', {'mode': 'auto', 'publish': True, 'repeat': True})
        self.assertTrue(wait_for(lambda: any('started' in line for line in self.state()['log'])))
        self.assertEqual(self.request('POST', '/api/quit', {})[0], 200)
        self.assertTrue(ended.wait(10), 'the web server was never asked to end')
        self.assertEqual(self.state()['run']['state'], 'idle')
        self.assertFalse(self.runner.thread.is_alive())
        self.assertEqual(self.request('POST', '/api/quit', {}, token=False)[0], 403)

    def test_an_unexpected_error_is_answered_and_the_panel_keeps_serving(self):
        with patch.object(self.panel, 'update', side_effect=RuntimeError('boom')), patch('sys.stderr', new=io.StringIO()):
            status, body = self.request('POST', '/api/update', {})
        self.assertEqual(status, 500)
        self.assertIn('unexpected error', json.loads(body)['error'])
        self.assertNotIn('boom', body.decode())      # internals stay in the log
        self.assertEqual(self.state()['run']['state'], 'idle')

    def test_ping_names_the_program_and_a_strange_token_is_only_refused(self):
        status, body = self.request('GET', '/api/ping', token=False)
        self.assertEqual((status, json.loads(body)['app']), (200, app.APP_NAME))
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request('GET', '/api/state', headers={'Host': f'127.0.0.1:{self.port}', 'X-Token': 'caf\u00e9'})
        self.assertEqual(connection.getresponse().status, 403)
        connection.close()

    def test_the_maximum_and_the_interval_are_checked_remembered_and_passed_on(self):
        for bad in ({'limit': -1}, {'limit': 1001}, {'limit': 2.5}, {'limit': True}, {'limit': 'ten'},
                    {'interval': 4}, {'interval': 1441}, {'interval': None}, {'interval': float('nan')}):
            status, body = self.request('POST', '/api/start', dict({'mode': 'health'}, **bad))
            self.assertEqual(status, 400, bad)
            self.assertIn('must be a whole number from', json.loads(body)['error'])
        self.assertEqual(self.state()['run']['state'], 'idle')
        self.assertEqual(app.clean_settings({}), {'limit': 0, 'interval': 30})
        self.assertEqual(app.clean_settings({'limit': 250.0, 'interval': 60}), {'limit': 250, 'interval': 60})
        self.assertEqual(self.request('POST', '/api/start', {'mode': 'health', 'repeat': True, 'limit': 250, 'interval': 5})[0], 200)
        self.assertEqual(self.runner.interval, 5 * 60)
        self.assertEqual(self.runner.settings, {'limit': 250, 'interval': 5})
        self.request('POST', '/api/stop', {})
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))
        self.assertEqual({k: self.state()['settings'][k] for k in ('limit', 'interval')}, {'limit': 250, 'interval': 5})
        # A damaged settings file falls back to the defaults instead of breaking the page.
        (self.root / '.local' / 'panel.json').write_text('{"limit": -7, "interval": "x", "mode": "full"}')
        self.assertEqual({k: self.state()['settings'][k] for k in ('limit', 'interval', 'mode')},
                         {'limit': 0, 'interval': 30, 'mode': 'full'})

    def test_the_tile_counts_what_worked_in_the_last_run_not_what_is_listed(self):
        docs = self.root / 'docs'
        docs.mkdir()
        status = {'published': 300, 'working_this_run': 412, 'list_limit': 300, 'mode': 'health', 'nodes': []}
        (docs / 'status.json').write_text(json.dumps(status))
        last = self.state()['last']
        self.assertEqual((last['working'], last['published'], last['limit'], last['limit_known']), (412, 300, 300, True))
        # An older report has no such field: the listed count stands in, and the page says nothing of a limit.
        (docs / 'status.json').write_text(json.dumps({'published': 300, 'mode': 'health', 'nodes': []}))
        last = self.state()['last']
        self.assertEqual((last['working'], last['limit_known']), (300, False))

    def test_the_default_command_hands_the_settings_to_main(self):
        (self.root / 'main.py').write_text('import sys; print("ARGS", *sys.argv[1:], flush=True)')
        runner = app.Runner(self.root)
        self.addCleanup(runner.stop)
        runner.start('full', False, False, {'limit': 0, 'interval': 45}, 45 * 60)
        self.assertTrue(wait_for(lambda: runner.status['state'] == 'idle' and any('ARGS' in x for x in runner.log)))
        line = next(x for x in runner.log if 'ARGS' in x)
        self.assertIn('ARGS --mode full --limit 0 --interval-minutes 45', line)
        self.assertNotIn('--publish', line)

    def test_stop_kills_a_run_and_ends_the_repeat(self):
        self.script = 'import time; print("started", flush=True); time.sleep(60)'
        self.request('POST', '/api/start', {'mode': 'auto', 'publish': True, 'repeat': True})
        self.assertTrue(wait_for(lambda: any('started' in line for line in self.state()['log'])))
        self.assertEqual(self.request('POST', '/api/start', {'mode': 'auto'})[0], 400)
        began = time.monotonic()
        self.assertEqual(self.request('POST', '/api/stop', {})[0], 200)
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))
        self.assertLess(time.monotonic() - began, 8)
        self.assertTrue(any('Stopped.' in line for line in self.state()['log']))

    def test_repeat_runs_again_until_stopped(self):
        self.request('POST', '/api/start', {'mode': 'full', 'publish': False, 'repeat': True})
        self.assertTrue(wait_for(lambda: sum('Starting a' in line for line in self.state()['log']) >= 2))
        log = self.state()['log']
        self.assertTrue(any('Starting a full run' in line for line in log))
        self.assertTrue(any('Starting a auto run' in line for line in log))
        self.request('POST', '/api/stop', {})
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))

    def test_the_published_address_comes_from_the_git_remote(self):
        for remote in ('https://github.com/Owner/free_sub.git\n', 'git@github.com:Owner/free_sub.git',
                       'ssh://git@github.com/Owner/free_sub'):
            self.assertEqual(app.pages_address(remote), 'https://owner.github.io/free_sub/', remote)
        for remote in ('', None, 'https://gitlab.com/owner/free_sub.git', 'https://github.com/owner',
                       'https://token@github.com/owner/free_sub.git', 'https://github.com/owner/a/b'):
            self.assertIsNone(app.pages_address(remote), remote)
        state = self.state()
        self.assertIsNone(state['pages'])   # the temporary folder is no clone
        self.assertFalse(state['task'])
        with patch('app.task_installed', return_value=True):
            self.panel.task = (0, False)
            self.assertTrue(self.state()['task'])

    def git(self, folder, *args):
        return subprocess.run(['git', *args], cwd=folder, check=True, capture_output=True, text=True)

    def test_only_a_folder_that_is_itself_a_clone_gets_its_address_and_its_update(self):
        # A copy of the project inside another repository (an operator's home folder can be one)
        # must not take that repository's address, and must never run its `git pull`.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as outer:
            self.git(outer, 'init', '-q')
            self.git(outer, 'remote', 'add', 'origin', 'https://github.com/Other/system_reporter.git')
            inner = Path(outer) / 'Documents' / 'free_sub'
            inner.mkdir(parents=True)
            self.assertFalse(app.clone_root(inner))
            self.assertTrue(app.clone_root(outer))
            self.assertIsNone(app.Panel(inner).pages)
            self.assertEqual(app.Panel(outer).pages, 'https://other.github.io/system_reporter/')
            self.git(inner, 'init', '-q')
            self.git(inner, 'remote', 'add', 'origin', 'https://github.com/Owner/free_sub.git')
            self.assertTrue(app.clone_root(inner))
            self.assertEqual(app.Panel(inner).pages, 'https://owner.github.io/free_sub/')
            # Not a clone: Update refuses before any `git pull`.
            plain = Path(outer) / 'copy'
            plain.mkdir()
            panel = app.Panel(plain)
            real = subprocess.run
            def guarded(command, **kwargs):
                self.assertNotEqual(command[:2], ['git', 'pull'], 'git pull must not run')
                return real(command, **kwargs)
            with patch('app.subprocess.run', guarded), self.assertRaisesRegex(ValueError, 'only in a Git clone'):
                panel.update()

    def test_update_pulls_only_while_idle_and_never_shows_the_remote(self):
        import subprocess
        from unittest.mock import patch
        calls = []
        def pull(command, **kwargs):
            if command[0] != 'git':   # the scheduled-task query on Windows
                return subprocess.CompletedProcess(command, 1, stdout='', stderr='')
            calls.append((command, (kwargs.get('env') or {}).get('GIT_TERMINAL_PROMPT')))
            return subprocess.CompletedProcess(command, 0, stdout='Updating 1..2\nFast-forward\n app.py | 2 +-\n',
                                               stderr='From https://token@github.com/owner/free_sub\n')
        with patch('app.subprocess.run', pull), patch('app.clone_root', return_value=True):
            self.assertEqual(self.request('POST', '/api/update', {})[0], 200)
        log = '\n'.join(self.state()['log'])
        self.assertEqual([c for c in calls if c[0][:2] == ['git', 'pull']], [(['git', 'pull', '--no-rebase', '--no-edit'], '0')])
        self.assertIn('Fast-forward', log)
        self.assertIn('Restarting the panel', log)
        self.assertNotIn('token', log)
        self.script = 'import time; print("started", flush=True); time.sleep(60)'
        self.request('POST', '/api/start', {'mode': 'health', 'publish': False, 'repeat': False})
        self.assertTrue(wait_for(lambda: any('started' in line for line in self.state()['log'])))
        status, body = self.request('POST', '/api/update', {})
        self.assertEqual((status, json.loads(body)['error']), (400, 'Stop the run before updating'))

    def pulled(self, stdout):
        def pull(command, **kwargs):
            if command[:2] == ['git', 'pull']:
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr='')
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='')
        return pull

    def start_waiting_schedule(self):
        self.runner.interval = 60
        self.assertEqual(self.request('POST', '/api/start', {'mode': 'health', 'repeat': True, 'publish': False,
                                                              'limit': 0, 'interval': 5})[0], 200)
        self.runner.interval = 60
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'waiting'))

    def test_an_update_between_two_rounds_restarts_the_panel_and_keeps_the_schedule(self):
        ended = threading.Event()
        self.panel.shutdown = ended.set
        self.start_waiting_schedule()
        with patch('app.subprocess.run', self.pulled('Updating 1..2\nFast-forward\n')), patch('app.clone_root', return_value=True):
            status, body = self.request('POST', '/api/update', {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['restarting'])
        self.assertEqual(self.runner.status['state'], 'idle')           # paused for the update
        marker = json.loads((self.root / '.local' / 'update-restart.json').read_text())
        self.assertTrue(marker['resume'])
        self.assertTrue(ended.wait(10), 'the panel was not asked to end')
        self.assertTrue(self.panel.respawn)

    def test_an_update_with_nothing_new_leaves_everything_as_it_was(self):
        self.panel.shutdown = lambda: self.fail('the panel must not restart')
        self.start_waiting_schedule()
        with patch('app.subprocess.run', self.pulled('Already up to date.\n')), patch('app.clone_root', return_value=True):
            self.assertEqual(self.request('POST', '/api/update', {})[0], 200)
        state = self.state()
        self.assertFalse(state['restarting'])
        self.assertIn(state['run']['state'], ('running', 'waiting'))     # the schedule was put back
        self.assertTrue(any('already the newest version' in line for line in state['log']))
        self.assertFalse((self.root / '.local' / 'update-restart.json').exists())
        self.request('POST', '/api/stop', {})
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))

    def test_a_failed_update_puts_the_schedule_back_too(self):
        self.start_waiting_schedule()
        failed = lambda command, **kw: subprocess.CompletedProcess(command, 1, stdout='', stderr='boom')
        with patch('app.subprocess.run', failed), patch('app.clone_root', return_value=True):
            self.assertEqual(self.request('POST', '/api/update', {})[0], 200)
        self.assertIn(self.state()['run']['state'], ('running', 'waiting'))
        self.assertFalse(self.state()['restarting'])
        self.request('POST', '/api/stop', {})
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))

    def test_the_restarted_panel_says_so_and_carries_on_but_ignores_an_old_marker(self):
        marker = self.root / '.local' / 'update-restart.json'
        marker.parent.mkdir(exist_ok=True)
        marker.write_text(json.dumps({'at': time.time() - 3600, 'resume': True}))
        self.panel.after_restart()
        self.assertFalse(marker.exists())
        self.assertEqual(self.runner.status['state'], 'idle')
        self.assertFalse(any('restarted' in line for line in self.runner.log))
        marker.write_text(json.dumps({'at': time.time(), 'resume': True}))
        self.runner.interval = 60
        self.panel.after_restart()
        self.assertFalse(marker.exists())
        log = '\n'.join(self.runner.log)
        self.assertIn('Updated', log)
        self.assertIn('and restarted.', log)
        self.assertIn('The repeat schedule continues.', log)
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] in ('running', 'waiting')))
        self.request('POST', '/api/stop', {})
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))

    def test_a_damaged_source_list_does_not_take_the_panel_down(self):
        (self.root / '.local').mkdir(exist_ok=True)
        (self.root / '.local/sources.json').write_text('{"schema": 99}')
        self.assertEqual({row['kind'] for row in self.state()['sources']}, {'builtin'})
        self.assertEqual(self.request('POST', '/api/source/add', {'url': 'https://mine.example/sub'})[0], 400)

    def test_sources_are_added_switched_off_and_removed(self):
        status, _ = self.request('POST', '/api/source/add', {'url': 'https://mine.example/sub', 'name': 'Mine'})
        self.assertEqual(status, 200)
        status, body = self.request('POST', '/api/source/add', {'url': 'http://plain.example/sub'})
        self.assertEqual((status, json.loads(body)['error']), (400, 'A subscription must be one https:// address'))
        self.request('POST', '/api/source/enable', {'name': 'Barry', 'enabled': False})
        rows = {row['name']: row for row in self.state()['sources']}
        self.assertEqual((rows['Mine']['kind'], rows['Mine']['enabled'], rows['Barry']['enabled']), ('manual', True, False))
        registry = sources.load(self.root / '.local/sources.json')
        self.assertEqual((list(registry['manual']), registry['off']), (['Mine'], ['Barry']))
        # A run that finishes later must not undo what the panel changed meanwhile.
        running = sources.Registry(self.root / '.local/sources.json', discover=0)
        self.request('POST', '/api/source/add', {'url': 'https://second.example/sub'})
        running.record({}, time.time())
        self.assertEqual(list(sources.load(self.root / '.local/sources.json')['manual']), ['Mine', 'Custom 1'])
        self.assertEqual(self.request('POST', '/api/source/remove', {'name': 'Barry'})[0], 400)
        self.assertEqual(self.request('POST', '/api/source/remove', {'name': 'Mine'})[0], 200)
        self.assertNotIn('Mine', {row['name'] for row in self.state()['sources']})



def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class BackgroundPanelTests(unittest.TestCase):
    """The real launcher, in real processes: the panel must outlive the window that started it."""

    def launcher(self, folder, port):
        return subprocess.run([sys.executable, str(Path(app.__file__).resolve()), '--root', folder,
                               '--port', str(port), '--no-browser'], capture_output=True, text=True, timeout=60)

    def quit(self, port):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        connection.request('GET', '/')
        token = re.search(r"TOKEN = '([^']+)'", connection.getresponse().read().decode()).group(1)
        connection.close()
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        connection.request('POST', '/api/quit', '{}', {'X-Token': token})
        status = connection.getresponse().status
        connection.close()
        return status

    def test_the_panel_outlives_its_launcher_is_started_once_and_quits(self):
        port = free_port()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            self.addCleanup(lambda: app.probe(port)[0] == 'panel' and self.quit(port))
            first = self.launcher(folder, port)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn('started in the background', first.stdout)
            self.assertIn('does not stop it', first.stdout)
            # The launcher has exited; the panel is still there.
            state, info = app.probe(port)
            self.assertEqual(state, 'panel')
            second = self.launcher(folder, port)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn('already running', second.stdout)
            self.assertEqual(app.probe(port)[1]['pid'], info['pid'])
            self.assertEqual(self.quit(port), 200)
            log_path = Path(folder) / '.local' / 'logs' / 'panel.log'
            report = lambda: f'probe={app.probe(port)} log={log_path.read_text(errors="replace") if log_path.exists() else None!r}'
            self.assertTrue(wait_for(lambda: app.probe(port)[0] == 'free', 40), 'the panel did not end: ' + report())
            self.assertIn('panel listening on', log_path.read_text(encoding='utf-8'))
            self.assertTrue(wait_for(lambda: 'panel ended' in log_path.read_text(encoding='utf-8')), report())

    def test_running_out_of_time_connecting_means_free_but_a_silent_listener_means_busy(self):
        # Windows is slow to refuse a connection to a closed port: that must not read as "busy".
        with patch('app.socket.create_connection', side_effect=TimeoutError):
            self.assertEqual(app.probe(free_port()), ('free', {}))
        with patch('app.socket.create_connection', side_effect=ConnectionRefusedError):
            self.assertEqual(app.probe(free_port()), ('free', {}))
        with patch('app.socket.create_connection', side_effect=OSError('unreachable')):
            self.assertEqual(app.probe(free_port())[0], 'busy')
        with socket.socket() as silent:
            silent.bind(('127.0.0.1', 0))
            silent.listen(1)
            self.assertEqual(app.probe(silent.getsockname()[1], timeout=0.5)[0], 'busy')

    def test_update_in_a_real_clone_replaces_the_panel_with_a_new_process(self):
        def git(cwd, *args):
            return subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', *args],
                                  cwd=cwd, check=True, capture_output=True, text=True)
        port = free_port()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as base:
            base = Path(base)
            remote, seed, clone = base / 'remote.git', base / 'seed', base / 'free_sub'
            git(base, 'init', '-q', '--bare', '-b', 'main', str(remote))
            seed.mkdir()
            git(seed, 'init', '-q', '-b', 'main')
            (seed / 'a.txt').write_text('one')
            git(seed, 'add', '.'); git(seed, 'commit', '-q', '-m', 'first')
            git(seed, 'remote', 'add', 'origin', str(remote)); git(seed, 'push', '-q', '-u', 'origin', 'main')
            git(base, 'clone', '-q', str(remote), str(clone))
            (seed / 'b.txt').write_text('two')
            git(seed, 'add', '.'); git(seed, 'commit', '-q', '-m', 'second commit for the update'); git(seed, 'push', '-q')
            self.addCleanup(lambda: app.probe(port)[0] == 'panel' and self.quit(port))
            self.assertEqual(self.launcher(str(clone), port).returncode, 0)
            state, info = app.probe(port)
            self.assertEqual(state, 'panel')
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
            conn.request('GET', '/')
            token = re.search(r"TOKEN = '([^']+)'", conn.getresponse().read().decode()).group(1)
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
            conn.request('POST', '/api/update', '{}', {'X-Token': token})
            response = conn.getresponse()
            answer = json.loads(response.read())
            conn.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(answer['restarting'], answer['log'])
            # The old program ends and a different one answers on the same port.
            def replacement_ready():
                status, current = app.probe(port)
                return status == 'panel' and current['pid'] != info['pid']
            self.assertTrue(wait_for(replacement_ready, 60), 'no new panel came up')
            self.assertTrue((clone / 'b.txt').exists(), 'the update was not pulled')
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
            conn.request('GET', '/')
            fresh = re.search(r"TOKEN = '([^']+)'", conn.getresponse().read().decode()).group(1)
            conn.close()
            self.assertNotEqual(fresh, token)    # the open page sees a 403 and reloads itself
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
            conn.request('GET', '/api/state', headers={'X-Token': token})
            self.assertEqual(conn.getresponse().status, 403)
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
            conn.request('GET', '/api/state', headers={'X-Token': fresh})
            log = '\n'.join(json.loads(conn.getresponse().read())['log'])
            conn.close()
            self.assertIn('and restarted.', log)
            self.assertIn('second commit for the update', log)
            self.assertFalse((clone / '.local' / 'update-restart.json').exists())

    def test_another_program_on_the_port_is_reported_not_trampled(self):
        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404); self.end_headers()
            def log_message(self, *args):
                pass
        other = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            result = self.launcher(folder, other.server_address[1])
        self.assertEqual(result.returncode, 1)
        self.assertIn('used by another program', result.stderr)
        self.assertEqual(app.probe(other.server_address[1])[0], 'busy')

    def test_a_panel_that_cannot_start_says_where_to_look(self):
        port = free_port()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            blocker = socket.socket()
            blocker.bind(('127.0.0.1', port))      # bound, not listening: refuses, yet the port is taken
            self.addCleanup(blocker.close)
            result = self.launcher(folder, port)
        self.assertEqual(result.returncode, 1)
        self.assertTrue('could not start' in result.stderr or 'used by another program' in result.stderr, result.stderr)


if __name__ == '__main__':
    unittest.main()
