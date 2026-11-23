"""Serve fresh subscription entries from a local scanner's output directory."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import time
from urllib.parse import urlsplit

from public_page import INDEX
from publishing import OUTPUTS, fresh_outputs, read_snapshot


def render_feed(docs, name, now):
    return fresh_outputs(read_snapshot(docs),now)[name], 'application/json' if name.endswith('.json') else 'text/plain'


def handler_for(docs):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, content, content_type):
            self.send_response(status)
            self.send_header('Content-Type',content_type+'; charset=utf-8')
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Cache-Control','no-store, max-age=0')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            if self.command=='GET':
                self.wfile.write(content)

        def do_GET(self):
            try:
                route=urlsplit(self.path).path
            except ValueError:
                route=''
            if route in ('/','/index.html'):
                return self.send(200,INDEX.encode('utf-8'),'text/html')
            if route[1:] not in OUTPUTS:
                return self.send(404,b'Unknown feed\n','text/plain')
            try:
                body,content_type=render_feed(docs,route[1:],time.time())
            except (OSError,ValueError,KeyError,TypeError):
                # Individual files are swapped atomically. A mixed generation must never be served.
                return self.send(503,b'Subscription temporarily unavailable\n','text/plain')
            self.send(200,body,content_type)

        do_HEAD=do_GET

        def log_message(self, format, *args):
            pass

    return Handler


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs',type=Path,default=Path(__file__).resolve().parent/'docs')
    parser.add_argument('--bind',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8780)
    args=parser.parse_args()
    with ThreadingHTTPServer((args.bind,args.port),handler_for(args.docs)) as server:
        print(f'Serving expiry-filtered subscriptions at http://{args.bind}:{server.server_port}/',flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__=='__main__':
    main()
