import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
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

    def test_only_this_computers_own_page_can_use_the_panel(self):
        status, page = self.request('GET', '/', token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.panel.token.encode(), page)
        self.assertEqual(self.request('GET', '/', host='evil.example')[0], 403)
        self.assertEqual(self.request('GET', '/api/state', token=False)[0], 404)
        self.assertEqual(self.request('POST', '/api/start', {'mode': 'full'}, token=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/start', {'mode': 'full'}, host=f'evil.example:{self.port}')[0], 403)
        self.assertEqual(self.state()['run']['state'], 'idle')

    def test_one_run_then_idle_and_settings_are_remembered(self):
        status, _ = self.request('POST', '/api/start', {'mode': 'full', 'publish': False, 'repeat': False})
        self.assertEqual(status, 200)
        self.assertTrue(wait_for(lambda: self.state()['run']['state'] == 'idle'))
        state = self.state()
        self.assertEqual(state['run']['exit'], 0)
        self.assertTrue(any('[CHECK] 25/100' in line for line in state['log']))
        self.assertEqual(state['settings'], {'mode': 'full', 'publish': False, 'repeat': False})
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


if __name__ == '__main__':
    unittest.main()
