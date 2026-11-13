"""Local control panel: start and stop runs, watch the log, manage sources.

A page served on 127.0.0.1 only, by the Python standard library. Runs are the same
`main.py` the scheduled task starts; stopping one kills it and every Xray under it.
"""
from __future__ import annotations
import argparse
from collections import deque
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
import collector
import sources
from process_guard import ProcessTree
from state import atomic_write, keep_awake

ROOT = Path(__file__).resolve().parent
MODES = ('auto', 'full', 'health')
TASK = 'FreeXraySubscription'


def quiet(command, root=ROOT, timeout=10):
    """A short helper command's result, or None when it cannot be run."""
    try:
        return subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.SubprocessError):
        return None


def pages_address(remote):
    """https://owner.github.io/repo/ for a GitHub remote, where the list is published."""
    remote = (remote or '').strip().removesuffix('.git')
    for prefix in ('https://github.com/', 'git@github.com:', 'ssh://git@github.com/'):
        if remote.startswith(prefix):
            owner, _, repository = remote[len(prefix):].partition('/')
            if owner and repository and '/' not in repository and '@' not in owner:
                return f'https://{owner.lower()}.github.io/{repository}/'
    return None


def task_installed():
    """Whether the unattended scheduled task exists (Windows only)."""
    if os.name != 'nt':
        return False
    result = quiet(['schtasks', '/Query', '/TN', TASK])
    return bool(result and result.returncode == 0)


def run_command(root, mode, publish):
    return [sys.executable, '-u', str(Path(root) / 'main.py'), '--mode', mode] + (['--publish'] if publish else [])


class Runner:
    """One run at a time, optionally repeated, with its output kept for the page."""

    def __init__(self, root=ROOT, command=run_command, interval=1800):
        self.root = Path(root); self.command = command; self.interval = interval
        self.lock = threading.Lock()
        self.log = deque(maxlen=2000)
        self.thread = None; self.tree = None
        self.wake = threading.Event(); self.stopping = False
        self.status = {'state': 'idle', 'mode': None, 'started': None, 'next_run': None, 'exit': None}

    def say(self, text):
        self.log.append(time.strftime('%H:%M:%S ') + text)

    def start(self, mode, publish, repeat):
        if mode not in MODES:
            raise ValueError('Unknown mode')
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('A run is already active')
            self.stopping = False; self.wake.clear()
            self.status.update(state='running', mode=mode, started=time.time(), next_run=None, exit=None)
            self.thread = threading.Thread(target=self._loop, args=(mode, bool(publish), bool(repeat)), daemon=True)
            self.thread.start()

    def stop(self):
        with self.lock:
            self.stopping = True; self.wake.set()
            tree = self.tree
        if tree is not None:
            self.say('Stopping: ending the run and its Xray processes.')
            tree.close()

    def _loop(self, mode, publish, repeat):
        awake = ExitStack()
        try:
            if repeat:
                # While it repeats, Windows must not idle-sleep between runs: nothing would wake it.
                try:
                    awake.enter_context(keep_awake())
                except OSError:
                    self.say('Could not keep Windows awake; runs stop while the PC sleeps.')
            self._rounds(mode, publish, repeat)
        finally:
            awake.close()
            self.status.update(state='idle', next_run=None)

    def _rounds(self, mode, publish, repeat):
        while True:
            began = time.time()
            self.status.update(state='running', mode=mode, started=began, next_run=None)
            code = self._run(mode, publish)
            self.status['exit'] = code
            if self.stopping:
                self.say('Stopped.')
                break
            self.say('Run finished.' if code == 0 else f'Run ended with exit code {code}.')
            if not repeat:
                break
            # Later rounds decide by themselves between a health check and a discovery.
            mode = 'auto'
            due = began + self.interval
            self.status.update(state='waiting', next_run=due)
            if self.wake.wait(max(0, due - time.time())):
                self.say('Stopped.')
                break

    def _run(self, mode, publish):
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        self.say(f'Starting a {mode} run' + (' (publishing)' if publish else ' (not publishing)') + '.')
        try:
            proc = subprocess.Popen(self.command(self.root, mode, publish), cwd=self.root, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding='utf-8', errors='replace',
                                    start_new_session=os.name != 'nt',
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except OSError as exc:
            self.say(f'Could not start the run ({type(exc).__name__}).')
            return -1
        try:
            tree = ProcessTree(proc)
        except OSError:
            # Windows cannot contain a process that has already ended, and one that ended
            # needs no containing. ProcessTree has killed one that was still running.
            tree = None
        with self.lock:
            self.tree = tree
            stop_now = self.stopping
        try:
            if stop_now:
                return -1
            for line in proc.stdout:
                self.say(line.rstrip())
            return proc.wait()
        finally:
            with self.lock:
                self.tree = None
            try:
                if tree is not None:
                    tree.close()
            except (OSError, subprocess.SubprocessError):
                pass
            proc.stdout.close()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


class Panel:
    """State behind the page: the runner, the source registry and the last results."""

    def __init__(self, root=ROOT, runner=None):
        self.root = Path(root)
        self.local = self.root / '.local'
        self.runner = runner or Runner(self.root)
        self.registry_path = self.local / 'sources.json'
        self.edit = threading.Lock()
        self.token = secrets.token_urlsafe(24)
        remote = quiet(['git', 'remote', 'get-url', 'origin'], self.root)
        self.pages = pages_address(remote.stdout if remote and remote.returncode == 0 else '')
        self.task = (0, False)

    def task_active(self):
        # Asked at most once a minute: the page polls far more often than a task changes.
        if time.time() - self.task[0] > 60:
            self.task = (time.time(), task_installed())
        return self.task[1]

    def registry(self):
        try:
            return sources.load(self.registry_path)
        except ValueError:
            return sources.empty()

    def settings(self):
        saved = read_json(self.local / 'panel.json', {})
        return {'mode': saved.get('mode') if saved.get('mode') in MODES else 'auto',
                'publish': bool(saved.get('publish', True)), 'repeat': bool(saved.get('repeat', True))}

    def start(self, body):
        chosen = {'mode': body.get('mode', 'auto'), 'publish': bool(body.get('publish')), 'repeat': bool(body.get('repeat'))}
        self.runner.start(chosen['mode'], chosen['publish'], chosen['repeat'])
        atomic_write(self.local / 'panel.json', json.dumps(chosen) + '\n')

    def change(self, action, body):
        with self.edit:
            registry = sources.load(self.registry_path)
            if action == 'add':
                sources.add_manual(registry, body.get('url', ''), body.get('name', ''))
            elif action == 'remove':
                sources.remove(registry, str(body.get('name', '')))
            else:
                sources.set_enabled(registry, str(body.get('name', '')), bool(body.get('enabled')))
            sources.save(self.registry_path, registry)

    def source_rows(self):
        registry = self.registry()
        last = read_json(self.local / 'source_status.json', {})
        results = last.get('sources', {})
        off = set(registry['off'])
        def row(name, kind, where, **more):
            result = results.get(name, {})
            return dict(name=name, kind=kind, where=where, enabled=name not in off,
                        supported=result.get('supported'), tested=result.get('tested'),
                        working=result.get('working'), error=result.get('error'), **more)
        rows = [row(name, 'manual', entry['url']) for name, entry in registry['manual'].items()]
        rows += [row(name, 'builtin', url) for name, url in collector.SOURCES.items()]
        found = [(name, entry) for name, entry in registry['found'].items() if entry['urls']]
        found.sort(key=lambda item: (-item[1]['working'], -item[1]['last_worked'], item[0].lower()))
        rows += [row(sources.PREFIX + name, 'found', 'https://github.com/' + name, runs=entry['runs'],
                     all_tested=entry['tested'], all_working=entry['working'], next=entry['next'],
                     size=entry['size']) for name, entry in found]
        return rows, {'at': last.get('at'), 'searched': registry['refreshed'],
                      'repositories': len(registry['found'])}

    def snapshot(self):
        status = read_json(self.root / 'docs' / 'status.json', {})
        rows, discovery = self.source_rows()
        protocols = {}
        for node in status.get('nodes', []):
            protocols[node.get('protocol')] = protocols.get(node.get('protocol'), 0) + 1
        return {'run': dict(self.runner.status), 'log': list(self.runner.log)[-400:], 'settings': self.settings(),
                'now': time.time(), 'sources': rows, 'discovery': discovery,
                'pages': self.pages, 'task': self.task_active(),
                'last': {'published': status.get('published'), 'health': status.get('health'),
                         'mode': status.get('mode'), 'generated': status.get('generated_epoch'),
                         'protocols': protocols}}


def handler_for(panel, page):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'FreeSubPanel'

        def log_message(self, *args):
            pass

        def local(self):
            # A web page elsewhere must not reach this panel: neither by its own name for
            # 127.0.0.1 (DNS rebinding) nor by posting to it from the browser.
            port = self.server.server_address[1]
            return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

        def send(self, code, body, kind='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.local():
                return self.send(403, {'error': 'Open the panel at its 127.0.0.1 address'})
            if self.path == '/':
                return self.send(200, page.replace('__TOKEN__', panel.token).encode('utf-8'), 'text/html')
            if self.path == '/api/state' and secrets.compare_digest(self.headers.get('X-Token', ''), panel.token):
                return self.send(200, panel.snapshot())
            self.send(404, {'error': 'Not found'})

        def do_POST(self):
            if not self.local() or not secrets.compare_digest(self.headers.get('X-Token', ''), panel.token):
                return self.send(403, {'error': 'Reload the panel page'})
            try:
                length = int(self.headers.get('Content-Length') or 0)
                if not 0 <= length <= 8192:
                    raise ValueError('Request too large')
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('Bad request')
                if self.path == '/api/start':
                    panel.start(body)
                elif self.path == '/api/stop':
                    panel.runner.stop()
                elif self.path in ('/api/source/add', '/api/source/remove', '/api/source/enable'):
                    panel.change(self.path.rsplit('/', 1)[1], body)
                else:
                    return self.send(404, {'error': 'Not found'})
            except ValueError as exc:
                return self.send(400, {'error': str(exc)})
            self.send(200, panel.snapshot())
    return Handler


def serve(panel, port=0):
    page = (ROOT / 'panel.html').read_text(encoding='utf-8')
    return ThreadingHTTPServer(('127.0.0.1', port), handler_for(panel, page))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args(argv)
    panel = Panel()
    try:
        server = serve(panel, args.port)
    except OSError:
        # Started twice: show the panel that is already running rather than fail.
        address = f'http://127.0.0.1:{args.port}/'
        if not args.no_browser:
            webbrowser.open(address)
        raise SystemExit(f'Port {args.port} is in use: the panel is probably already open at {address}')
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    print(f'Control panel: {url}\nClose this window (or press Ctrl+C) to stop the panel and any run it started.', flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        panel.runner.stop()
        server.server_close()


if __name__ == '__main__':
    main()
