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
# tools/xray.exe of this release is committed, so a clone on Windows needs no download.
# When RELEASE changes: run this on Windows, set the new hash here and in tools/README.md,
# and commit tools/xray.exe. The tests refuse a bundled file with any other hash.
BUNDLED_WINDOWS_SHA256='15c2d007954ac53ba69b80ec91242786b3c0b71d52649165b4ca1d5cc96ef8f1'
ROOT=Path(__file__).resolve().parent
RELEASE_API='https://api.github.com/repos/XTLS/Xray-core/releases/tags/'


def get(url,limit):
    request=urllib.request.Request(url,headers={'User-Agent':'FreeXraySubscription/3.0'})
    token=os.environ.get('GITHUB_TOKEN')
    if token and url.startswith(RELEASE_API):
        request.add_unredirected_header('Authorization',f'Bearer {token}')
    with urllib.request.urlopen(request,timeout=60) as response:
        data=response.read(limit+1)
    if len(data)>limit: raise ValueError('Oversized official release response')
    return data


def install(release_tag=RELEASE, dest=ROOT/'tools'):
    if platform.machine().lower() not in ('x86_64','amd64'):
        raise RuntimeError('This installer supports Windows/Linux x64; install the official platform binary manually')
    name='Xray-windows-64.zip' if os.name=='nt' else 'Xray-linux-64.zip'
    release=json.loads(get(RELEASE_API+release_tag,2*1024*1024))
    if release.get('tag_name')!=release_tag:
        raise ValueError('Unexpected release tag')
    asset=next(a for a in release['assets'] if a['name']==name)
    url=asset['browser_download_url']
    if url!=f'https://github.com/XTLS/Xray-core/releases/download/{release_tag}/{name}':
        raise ValueError('Unexpected release URL')
    data=get(url,80*1024*1024)
    digest='sha256:'+hashlib.sha256(data).hexdigest()
    if digest!=asset.get('digest'):
        raise ValueError('Missing or mismatched official asset SHA256')
    dest=Path(dest); dest.mkdir(parents=True,exist_ok=True)
    executable='xray.exe' if os.name=='nt' else 'xray'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        binary=archive.read(executable)
    temporary=dest/(executable+'.tmp')
    temporary.write_bytes(binary)
    temporary.chmod(0o700)
    temporary.replace(dest/executable)
    (dest/'release.json').write_text(json.dumps({'release':release_tag,'archive_digest':digest,
                    'binary_sha256':hashlib.sha256(binary).hexdigest()},indent=2)+'\n')
    print(f'Installed official Xray {release_tag}; SHA256 verified.')


if __name__=='__main__':
    import re
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',default=RELEASE)
    parser.add_argument('--directory',type=Path,default=ROOT/'tools')
    args=parser.parse_args()
    if not re.fullmatch(r'v\d+\.\d+\.\d+',args.release):
        parser.error('Specify an exact release tag, for example v26.11.9')
    if args.release!=RELEASE and args.directory.resolve()==(ROOT/'tools').resolve():
        parser.error('Store candidate releases in a separate directory')
    install(args.release,args.directory)
