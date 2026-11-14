"""Serve fresh subscription entries from a local scanner's output directory."""
from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from public_page import INDEX
from publishing import PINNED_FEED, compact_selection, pinned_selection, read_snapshot


def render_feed(docs, name, now):
    report,urls,outbounds=read_snapshot(docs)
    retained=[(url,row,outbound) for url,row,outbound in zip(urls,report['nodes'],outbounds)
              if row['expires_epoch']>now]
    urls=[url for url,_,_ in retained]
    rows=[dict(row,rank=rank) for rank,(_,row,_) in enumerate(retained,1)]
    outbounds=[outbound for _,_,outbound in retained]
    if name in ('sub.txt','sub.b64'):
        plain=''.join(f'{url}\n' for url in urls).encode('utf-8')
        return (base64.b64encode(plain)+b'\n' if name=='sub.b64' else plain), 'text/plain'
    if name=='xray.json':
        data={'outbounds':outbounds}
    elif name==PINNED_FEED:
        _,data=pinned_selection(outbounds,rows)
    else:
        compact_ids,compact=compact_selection(urls,rows)
        if name=='sub-compact.b64':
            return base64.b64encode(compact)+b'\n', 'text/plain'
        pinned_ids,_=pinned_selection(outbounds,rows)
        data=dict(report,published=len(rows),nodes=rows,compact_ids=compact_ids,pinned_ids=pinned_ids)
        if not rows:
            data['health']='expired_or_empty'
    return (json.dumps(data,ensure_ascii=False)+'\n').encode('utf-8'), 'application/json'


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
            if route not in ('/sub.txt','/sub.b64','/sub-compact.b64','/status.json','/xray.json','/'+PINNED_FEED):
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
