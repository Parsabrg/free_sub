"""Install a pinned official Xray release, verifying its release-API SHA-256 digest."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import urllib.request
import zipfile
import io

RELEASE='v26.3.27'
ROOT=Path(__file__).resolve().parent


def get(url,limit):
    request=urllib.request.Request(url,headers={'User-Agent':'FreeXraySubscription/3.0'})
    with urllib.request.urlopen(request,timeout=60) as response:
        data=response.read(limit+1)
    if len(data)>limit: raise ValueError('Oversized official release response')
    return data


def install():
    if platform.machine().lower() not in ('x86_64','amd64'):
        raise RuntimeError('This installer supports Windows/Linux x64; install the official platform binary manually')
    name='Xray-windows-64.zip' if os.name=='nt' else 'Xray-linux-64.zip'
    release=json.loads(get(f'https://api.github.com/repos/XTLS/Xray-core/releases/tags/{RELEASE}',2*1024*1024))
    asset=next(a for a in release['assets'] if a['name']==name)
    url=asset['browser_download_url']
    if not url.startswith(f'https://github.com/XTLS/Xray-core/releases/download/{RELEASE}/'):
        raise ValueError('Unexpected release URL')
    data=get(url,80*1024*1024)
    digest='sha256:'+hashlib.sha256(data).hexdigest()
    if digest!=asset.get('digest'):
        raise ValueError('Missing or mismatched official asset SHA256')
    dest=ROOT/'tools'; dest.mkdir(exist_ok=True)
    executable='xray.exe' if os.name=='nt' else 'xray'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        binary=archive.read(executable)
    temporary=dest/(executable+'.tmp')
    temporary.write_bytes(binary)
    temporary.chmod(0o700)
    temporary.replace(dest/executable)
    (dest/'release.json').write_text(json.dumps({'release':RELEASE,'archive_digest':digest,
                    'binary_sha256':hashlib.sha256(binary).hexdigest()},indent=2)+'\n')
    print(f'Installed official Xray {RELEASE}; SHA256 verified.')


if __name__=='__main__':
    install()
