# Validation before initial repository push

Date: 2026-10-15. Environment: Linux, Python 3.12.

- **30 tests passed**, including four actual Xray integration tests.
- Official Xray release **v26.3.27**. Download SHA-256 was checked against the official release API.
- Local source fingerprint: `87ed5510ff82396b5d5b6a3e2a43e170b7fa9640e3bc349c110882dc5900b77c`.
- Xray executable SHA-256: `8255dd939c34cf966cc91517b6324dd3c8d0bcf49ffac8beca049a38c46845ed`.
- Full verification duration: 9.9 seconds.

The integration fixture uses a real Xray core, a local Trojan TLS server and an
HTTPS origin with an ephemeral test CA. Actual response bodies cross the encrypted
proxy. Incorrect proxy credentials and the wrong TLS hostname fail. A stalled
connection hits the hard deadline. The complete source-fetch / first-pass /
second-pass / naming / output-validation / health-refresh / expiry pipeline is
exercised. A separate local bare Git repository receives a real output commit and
push, and its head and exact committed paths are checked. The nested pre-push gate
is intercepted only inside that Git fixture to avoid recursively rerunning the
entire suite; the outer verify.py gate runs all real Xray tests.

No OpenSSL installation, pip dependency, external VPN provider, real user
credential or live subscription is needed by the regression suite. Real-Xray
integration tests must not be skipped for a pre-push gate.

A separate **24-candidate live public-feed smoke scan found zero passing nodes**
from this execution environment. Six feeds produced supported candidates;
Shadowmere produced no usable share links in that attempt. Failures were DNS
resolution subprocess failures and connection resets. This result does not
establish availability on the operator's ISP, and no live proxy list or private
runtime history has been committed.

Python syntax and workflow YAML were parsed locally. The CI workflow repeats
regression and real loopback Xray tests on Windows and Linux and parses the
PowerShell scripts on Windows. Those CI results occur after the repository push.
Physical sleep/wake, firmware support, Windows account credentials, the operator's
ISP and actual Pages deployment still require the one-time setup on the user's
Windows PC/GitHub account. They have not been represented as tested remotely.

## Windows validation on the operator PC

- Python 3.14.7; official Windows Xray v26.3.27.
- All 30 tests passed, including all four actual Xray integration tests; no skips.
- Duration: 16.76 seconds.
- Windows Xray binary SHA-256: `15c2d007954ac53ba69b80ec91242786b3c0b71d52649165b4ca1d5cc96ef8f1`.
- Fixed the local fixture CA configuration: Windows Xray needs `disableSystemRoot`
  to use its custom CA pool. This applies only to the loopback fixture and keeps
  certificate and hostname verification enabled. Production TLS is unchanged.
- All three PowerShell installation/runtime/check scripts parsed without errors.
- Physical sleep/wake is not yet verified.
