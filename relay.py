"""Fetch a source through an already verified node when the direct fetch fails.

On 2026-11-12 one discovery run lost 20 of 23 feeds to connection resets on the way
to raw.githubusercontent.com while 300 verified nodes were sitting in the list.
The node only carries the bytes: the HTTPS certificate of the site is still checked
here, so a node can refuse or stall a fetch but cannot change what is fetched.
"""
from __future__ import annotations
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import collector
from network import pin_endpoint, resolve_public
from xray_test import allocate_port, check_ready, https_get_via_socks, make_config

CHECK = ('https://www.gstatic.com/generate_204', 204)
GIVE_UP_AFTER = 3   # failed fetches in a row before the next node is tried


class Relay:
    """One Xray at a time, started on the first failed direct fetch, never before."""

    def __init__(self, xray, candidates, fixture=None):
        self.xray = str(xray)
        self.waiting = list(candidates)
        self.fixture = fixture   # tests only: loopback node with its own trust anchor
        self.lock = threading.Lock()
        self.proc = None; self.port = None; self.folder = None; self.failures = 0
        self.context = ssl.create_default_context(cafile=fixture['ca_file'] if fixture else None)

    def _stop(self):
        if self.proc is not None:
            self.proc.kill(); self.proc.wait()
            self.proc = None
        if self.folder is not None:
            self.folder.cleanup(); self.folder = None

    def _start(self):
        """Bring up the next node that proves it carries an HTTPS request."""
        self._stop()
        while self.waiting:
            candidate = self.waiting.pop(0)
            try:
                if self.fixture:
                    if not ipaddress.ip_address(candidate.host).is_loopback:
                        raise ValueError('FixturesMustBeLoopback')
                    outbound = pin_endpoint(candidate.outbound, [candidate.host])
                    tls = outbound['streamSettings']['tlsSettings']
                    tls['disableSystemRoot'] = True
                    tls['certificates'] = [{'usage': 'verify', 'certificateFile': self.fixture['ca_file']}]
                    check = self.fixture['check']
                else:
                    outbound = pin_endpoint(candidate.outbound, resolve_public(candidate.host))
                    check = CHECK
                self.port = allocate_port()
                self.folder = tempfile.TemporaryDirectory(prefix='xray-relay-', ignore_cleanup_errors=True)
                config = Path(self.folder.name) / 'config.json'
                config.write_text(json.dumps(make_config(outbound, self.port)), encoding='utf-8')
                self.proc = subprocess.Popen([self.xray, 'run', '-c', str(config)], cwd=self.folder.name,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                check_ready(self.port, self.proc)
                https_get_via_socks(self.port, check[0], 8, self.context, expected_status=check[1])
                self.failures = 0
                print('[RELAY] Direct fetch failed; sources are fetched through a verified node.', flush=True)
                return
            except Exception:
                self._stop()
        raise OSError('NoWorkingRelay')

    def get(self, url, timeout=20):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None or self.failures >= GIVE_UP_AFTER:
                self._start()
            port = self.port
        try:
            body, _ = https_get_via_socks(port, url, timeout, self.context, max_bytes=collector.MAX_FETCH)
        except Exception:
            with self.lock:
                if port == self.port:
                    self.failures += 1
            raise
        with self.lock:
            if port == self.port:
                self.failures = 0
        return body.decode('utf-8-sig')

    def close(self):
        with self.lock:
            self.waiting = []
            self._stop()


@contextmanager
def fallback(xray, candidates, fixture=None):
    """While open, a failed direct collector.get is tried once more through a node."""
    relay = Relay(xray, candidates, fixture)
    collector.FALLBACK = relay.get
    try:
        yield relay
    finally:
        collector.FALLBACK = None
        relay.close()
