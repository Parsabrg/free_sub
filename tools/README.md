# Bundled Xray

`xray.exe` is the unmodified official **Xray-core v26.3.27** binary for Windows x64,
taken from `Xray-windows-64.zip` on the project's release page:
https://github.com/XTLS/Xray-core/releases/tag/v26.3.27

- Archive SHA-256: `d004c39288ce9ada487c6f398c7c545f7d749e44bdfdd59dbc9f865afba4e1ad`
  (equal to the release's own `Xray-windows-64.zip.dgst`)
- `xray.exe` SHA-256: `15c2d007954ac53ba69b80ec91242786b3c0b71d52649165b4ca1d5cc96ef8f1`

It is here so that a fresh clone runs without a download that may be blocked.
The test suite refuses a file with any other hash.

Xray-core is licensed under the Mozilla Public License 2.0 (`LICENSE-Xray.txt`).
Its source code for this version: https://github.com/XTLS/Xray-core/tree/v26.3.27

Linux, or another version: run `python download_xray.py` (see `download_xray.py`
for what to change when the pinned version changes).
