"""Opt-in media, UDP, IPv6 and browser diagnostics through the candidate."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

from xray_test import (VIDEO_BYTES, allocate_port, check_ready, fixture_address,
                       https_get_via_socks, make_config, socks_address, socks_command)

UDP_TARGET = ('1.1.1.1',53)
IPV6_TARGET = 'https://[2606:4700:4700::1111]/dns-query?name=example.com&type=AAAA'
PROBE_ERRORS = (OSError,ValueError,http.client.HTTPException)


def udp_dns_probe(port, target, timeout=3):
    """Check an actual UDP DNS reply over a SOCKS5 UDP association (RFC 1928)."""
    address = socks_address(*target)
    transaction = secrets.token_bytes(2)
    query = transaction + struct.pack('>5H',0x0100,1,0,0,0) + b'\x07example\x03com\x00\x00\x01\x00\x01'
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as datagram:
        datagram.settimeout(timeout)
        datagram.bind(('127.0.0.1',0))
        control, (relay_host,relay_port) = socks_command(port,3,*datagram.getsockname(),timeout)
        with control:
            relay_ip = ipaddress.ip_address(relay_host)
            if not relay_port or not (relay_ip.is_loopback or relay_ip.is_unspecified):
                return False
            datagram.connect(('127.0.0.1',relay_port))
            datagram.sendall(b'\0\0\0'+address+query)
            response = datagram.recv(4096)
    header = b'\0\0\0'+address
    if not response.startswith(header) or len(response)<len(header)+12:
        return False
    identifier, flags, questions, answers = struct.unpack_from('>2s3H',response,len(header))
    return identifier==transaction and flags & 0x800F==0x8000 and questions==1 and answers>0


def playback(port, video_id, timeout_ms):
    """Observe an embedded YouTube player advance with an actual media response."""
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError:
        return None,'InstallPlaywrightChromium'
    probe_page = 'https://parsabrg.github.io/free_sub/playback-probe'
    page_html = '''<!doctype html><meta name="referrer" content="strict-origin-when-cross-origin">
<div id="player"></div><script>
let player, playbackError = null, playingFrom = null;
function onYouTubeIframeAPIReady() {
  player = new YT.Player('player', {
    videoId: VIDEO_ID, width: 480, height: 270,
    playerVars: {origin: location.origin, playsinline: 1},
    events: {
      onReady: ({target}) => {target.mute(); target.playVideo()},
      onStateChange: ({data, target}) => {
        if (data === YT.PlayerState.PLAYING && playingFrom === null) playingFrom = target.getCurrentTime();
      },
      onError: ({data}) => {playbackError = 'YouTubePlayerError' + data},
      onAutoplayBlocked: () => {playbackError = 'YouTubeAutoplayBlocked'}
    }
  });
}
</script><script src="https://www.youtube.com/iframe_api"></script>'''
    started=time.monotonic()
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(timeout=min(8000,timeout_ms),
                      proxy={'server':f'socks5://127.0.0.1:{port}'},
                      args=['--disable-quic','--autoplay-policy=no-user-gesture-required'])
            try:
                page = browser.new_page(service_workers='block')
                page.route(probe_page, lambda route: route.fulfill(
                    content_type='text/html',body=page_html.replace('VIDEO_ID',json.dumps(video_id))))
                media_received=[]
                def video_response(response):
                    url=urlsplit(response.url)
                    if (url.hostname or '').endswith('.googlevideo.com') and url.path=='/videoplayback' and response.ok:
                        media_received.append(True)
                page.on('response',video_response)
                page.goto(probe_page,wait_until='domcontentloaded',timeout=min(8000,timeout_ms))
                remaining=max(1,int(timeout_ms-(time.monotonic()-started)*1000))
                result=page.wait_for_function('''() => playbackError || (playingFrom !== null &&
                    player.getPlayerState() === 1 && player.getCurrentTime() - playingFrom >= 2 && 'played')''',
                    timeout=remaining).json_value()
                if result=='played':
                    return (True,'') if media_received else (False,'YouTubeMediaNotObserved')
                return False,result
            finally:
                browser.close()
    except TimeoutError:
        return False,'YouTubePlaybackTimeout'
    except Error:
        return None,'YouTubeBrowserUnavailable'


def run_diagnostics(payload, outbound, mirrors, context):
    fixture=payload.get('fixture')
    udp=payload['check_udp']
    port=allocate_port(also_udp=udp)
    deadline=time.monotonic()+payload['timeout']-2
    output={}
    with tempfile.TemporaryDirectory(prefix='diagnostics-',dir=payload['work_dir'],ignore_cleanup_errors=True) as folder:
        config=Path(folder)/'xray.json'
        config.write_text(json.dumps(make_config(outbound,port,udp=udp)),encoding='utf-8')
        proc=subprocess.Popen([payload['xray_bin'],'run','-c',str(config)],cwd=folder,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            check_ready(port,proc)
            if udp:
                target = tuple(fixture['udp_target']) if fixture and 'udp_target' in fixture else UDP_TARGET
                try:
                    if fixture: fixture_address(target[0])
                    output['udp']=udp_dns_probe(port,target)
                except PROBE_ERRORS:
                    output['udp']=False
            if payload['check_ipv6']:
                target = fixture.get('ipv6_target',IPV6_TARGET) if fixture else IPV6_TARGET
                try:
                    if fixture:
                        fixture_address(urlsplit(target).hostname)
                    body,_=https_get_via_socks(port,target,4,context,accept='application/dns-json',
                                               connect_host='::1' if fixture else None)
                    output['ipv6']=bool(body)
                except PROBE_ERRORS:
                    output['ipv6']=False
            if payload['stream_segments']>1:
                index=payload['video_probe_index']
                url=mirrors[index] if 0<=index<len(mirrors) else None
                output.update(stream_segments=1,stream_start_ms=payload['video_ms'],stream_stalls=0,stream_error='')
                elapsed=payload['video_ms']/1000
                # Each additional 128 KiB range has a two-second delivery budget for stall accounting.
                if url:
                    for index in range(1,payload['stream_segments']):
                        remaining=deadline-time.monotonic()-(12 if payload['youtube_id'] else 0)
                        if remaining<0.5:
                            output['stream_error']='StreamBudgetExhausted'
                            break
                        try:
                            _,seconds=https_get_via_socks(port,url,min(4,remaining),context,VIDEO_BYTES,
                                                         video_range=True,range_start=index*VIDEO_BYTES)
                        except PROBE_ERRORS:
                            output['stream_stalls']+=1
                            output['stream_error']='StreamSegmentFailed'
                            break
                        output['stream_segments']+=1
                        output['stream_stalls']+=int(seconds>2)
                        elapsed+=seconds
                    output['stream_kbps']=round(output['stream_segments']*VIDEO_BYTES*8/1000/max(elapsed,0.001),1)
                else:
                    output['stream_error']='VideoMirrorUnavailable'
            if payload['youtube_id'] and not fixture:
                remaining=int((deadline-time.monotonic())*1000)
                if remaining>=4000:
                    output['youtube_played'],output['youtube_error']=playback(port,payload['youtube_id'],remaining)
                else:
                    output['youtube_error']='YouTubeBudgetExhausted'
            return output
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
