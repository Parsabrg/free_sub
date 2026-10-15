# Free Sub — local Xray-verified subscriptions

Collect public VLESS, VMess, Trojan and Shadowsocks share links, verify real HTTPS
response data through the official Xray core on your Windows PC, remember node
reliability, and publish up to 300 working configurations to GitHub Pages.

**The scheduled task wakes your PC from supported sleep on AC power. You do not
need to sit at the keyboard, unlock Windows, or approve each test or push.**
Windows cannot execute a test while the CPU remains asleep: the task wakes the
computer, runs, releases its sleep inhibitor, and normal idle sleep resumes.
Shutdown is not supported. Firmware, Modern Standby, power settings, lid behavior,
Wi-Fi recovery and corporate policies can prevent wake; verify on your actual PC.

## One-time Windows setup

Requirements: Windows 10/11 x64, Python 3.11+ (python.org installation recommended),
Git for Windows, and a Windows account password. Task Scheduler's password entry
requires your **account password, not your Windows Hello PIN**. Avoid moving the
clone after installation. No pip dependencies or OpenSSL installation are needed.

1. Clone your dedicated repository and install the pinned official Xray binary:

   ```powershell
   git clone https://github.com/Parsabrg/free_sub.git
   cd free_sub
   python download_xray.py
   ```

   The installer verifies the SHA-256 digest from the official release API. It
   fails closed if the digest is missing or mismatched. Review and update the
   pinned version in `download_xray.py` periodically; it does not silently upgrade.

2. Configure Git author and authenticate **once**, using Git Credential Manager
   or an SSH key available to the scheduled account. Never put a GitHub token
   into a remote URL or this project. Check:

   ```powershell
   git config user.name "YOUR NAME"
   git config user.email "YOUR GITHUB COMMIT EMAIL"
   git ls-remote origin HEAD
   python verify.py --xray .\tools\xray.exe
   ```

   The first real `git push` may require one-time authentication. Run the first
   full scan manually so credentials are settled before unattended operation:

   ```powershell
   python main.py --mode full --publish
   ```

3. For free GitHub Pages hosting, the repository must be **public** on GitHub Free.
   Eligible paid GitHub plans can host Pages from private repositories. Choose
   the visibility yourself in Settings; the program never changes it. Enable
   **Settings → Pages → Source: GitHub Actions**. The workflow deploys `docs/`.
   Public subscription URLs expose the included public proxy credentials;
   never add personal or paid subscriptions you do not intend to redistribute.

4. In an **Administrator PowerShell window for your normal Windows account**, run:

   ```powershell
   powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\install-task.ps1
   ```

   Setup runs actual Xray verification, checks noninteractive Git read access,
   enables AC wake timers, and asks for your Windows account password once.
   Windows Task Scheduler stores the credential; the project never writes the
   password to disk. Runtime uses limited privileges. Existing Git authentication
   must belong to that same account. A read-access check cannot guarantee write
   permission; complete the manual `--publish` above before relying on the task.

5. Check the installed task:

   ```powershell
   powershell -NoProfile -File .\scripts\check-task.ps1
   ```

   For an actual wake test, leave the PC on AC, let it sleep before the next run,
   then inspect the task's LastRun/LastResult and `.local\logs\` after it wakes.
   This physical wake/sleep check cannot be performed from a remote Linux test
   environment. Windows still decides when to return to sleep afterward; the
   script does not force sleep while you are using the PC.

## Refresh policy

| Operation | Default |
|---|---|
| Task trigger | Every 30 minutes; wake on AC, catch up after missed runs |
| Published-node and standby health checks | Every task run |
| Source discovery and full verification | Every 4 hours in `--mode auto` |
| Suggested app subscription refresh | Every 1 hour, randomized across users |
| App connection failure | Try cached alternatives immediately; limit failure-triggered subscription downloads to one per 15 minutes |
| Retained node expiry after its last success | 6 hours |

The client policy is guidance: this project is a subscription builder, not your
VPN app. Plain share-link subscriptions have no universal per-node expiry
mechanism. An app must consume `status.json` to enforce `expires_epoch`; ordinary
clients can keep a cached link indefinitely. If the PC is shut down, Pages keeps
serving the last deployment. The status page calculates staleness in the browser
without pretending a failed or stopped runner is healthy.

The task ignores overlapping triggers, retries failures twice five minutes apart,
waits up to two minutes for Wi-Fi, and limits a run to three hours. The application
also holds an OS lock. A worker subprocess contains DNS, Xray startup, TLS and body
reads inside a total 30-second candidate deadline. Process groups on Linux and a
kill-on-close Windows Job Object contain Xray descendants. Failure to contain a
Windows process fails the probe rather than leaving an uncontrolled child.

After a whole-run outage, the previous list may be retained **only within its
original expiry**, explicitly marked `retained_previous`. Failed runs never renew
last-success timestamps. An expired list becomes empty on the next completed run.
Failures before output generation leave the prior deployment in place; its
existing timestamps continue to age. No success is invented to fill 300 slots.

## What actually gets tested

- Feeds: Barry, EbraSha, Epodonios, Shadowmere, 0xRadikal, Pawdroid, Mahdibland
  Public (`Eternity.txt`, not the account/airport list). These are untrusted
  candidate sources, not endorsed providers.
- Sources have an 8 MiB fetch limit. No hidden per-source 1,800-node cap.
- Source candidates are balanced and rotated between discovery runs. Previously
  published nodes are checked first. `--max-tests 0` tests every supported unique
  configuration collected within those documented fetch limits.
- Private, loopback, link-local and mixed public/private DNS endpoint answers are
  rejected. DNS resolution runs with a deadline. The Xray endpoint is pinned to
  the validated public IP; SNI and transport Host values remain intact.
- Each candidate runs in its own Xray process with a loopback SOCKS inbound and
  candidate-only routing. No direct fallback outbound exists in the tester.
- Initial checks retrieve real certificate-verified HTTPS body data. Discovery
  finalists require a new process and successful transfers to two different
  probe hostnames. Health checks revisit already fully verified nodes.
- Roughly 16 KiB download and smaller fallback endpoints prove connectivity.
  Transfer estimates include handshake time and **are not sustained-speed
  benchmarks**. Timing variation across different endpoints is reported separately
  from long-term reliability; it is not a gaming packet-jitter measurement.
- History keeps at most 20,000 nodes from 30 days and uses recent weighted successes/failures and penalizes one-off results.
  Up to four credentials per actual connection IP:port limit clones. Historical
  scores improve selection but cannot guarantee uptime or available capacity.
- Names are `server 🇩🇪 123` with persistent, globally allocated numbers. Numbers
  are stable identities, not current rank; use `status.json` for rank.
- Flags refer to the exact pinned connection IP of the last passing test, possibly
  a CDN ingress, not necessarily exit country. GeoIP is cached for 14 days, capped
  at 300 new lookups/run and 900/day. GeoIP failure keeps a globe flag by default;
  `--require-country` is an optional stricter filter.

Unsupported connection parameters fail closed. XHTTP `extra`, complex headers,
external Shadowsocks plugins, unsupported transports/ciphers, insecure TLS,
and unknown URL extensions are rejected rather than tested as a simplified
configuration. This deliberately sacrifices some coverage for accurate testing.
Original accepted share-link connection parameters are preserved on output, and
renamed links are parsed again to verify the effective configuration fingerprint.

Results describe your current test network. Another VPN or TUN on your PC can
change the route; this project does not disable adapters or promise direct ISP
measurement. Different users/ISPs still need local app checks. No TUN/DNS-leak,
UDP, gaming packet loss, sustained throughput, privacy, operator trust or
unlimited-capacity certification is provided by an HTTPS probe.

## Publish and verify

Before **each subscription push**, `verify.py` runs offline regressions and a
real local end-to-end test using the installed Xray binary. The fixture generates
its own temporary TLS certificate, starts a Trojan server and HTTPS origin, fetches
a source over HTTPS, exercises the full builder, rejects bad credentials, tests a
hard deadline, rechecks health, preserves stable names, and expires old nodes.
The loopback-only fixture override is internal to tests; production has no CLI
switch to bypass public-address checks or accept untrusted TLS certificates.

Generated plain/Base64 subscriptions and metadata must agree. Only the four
`docs/` output paths are staged; pre-existing staged changes, a detached HEAD, or
a divergent remote stop publishing. Git is noninteractive and bounded; there
are no force pushes, automatic merges, or private-history uploads. A failed push
is retried on the next run, including when it has already committed locally.

GitHub CI runs the same real **loopback-only** Xray tests on Windows and Linux,
plus Windows PowerShell syntax checks. Public proxy discovery/testing stays on
your machine. CI does not register tasks, wake hardware or publish proxy results.

```powershell
python main.py --mode health --publish
python main.py --mode full --max-tests 0
python verify.py --xray .\tools\xray.exe
python -m unittest discover -s tests -v
```

The last command alone skips real Xray integration unless `XRAY_E2E_BINARY` is set.
Use `verify.py` for the required complete gate. Evidence is stored locally in
`.local/verification.json`, with source fingerprint, binary hash, scope and results.
Public test evidence from development is in `VALIDATION.md`; runtime evidence and
proxy credentials are never published as CI artifacts.

After Pages is enabled and a successful local scan is pushed:

- Plain: `https://parsabrg.github.io/free_sub/sub.txt`
- Base64: `https://parsabrg.github.io/free_sub/sub.b64`
- Freshness/health: `https://parsabrg.github.io/free_sub/status.json`

Until then, the repository contains no fabricated tested subscription. The public
HTML page initially reports status unavailable.

## Sources and acknowledgments

Public candidate feeds come from the projects below. Thanks to their maintainers.
This project independently tests candidates; listing a source does not imply its
maintainers endorse this project or guarantee the proxy operators.

| Resource | Published repository license / terms |
|---|---|
| [Barry](https://github.com/barry-far/V2ray-Config) | MIT |
| [EbraSha](https://github.com/ebrasha/free-v2ray-public-list) | No license detected by GitHub; check upstream terms |
| [Epodonios](https://github.com/Epodonios/v2ray-configs) | GPL-3.0 |
| [Shadowmere](https://shadowmere.xyz/) | Check the provider's terms |
| [0xRadikal](https://github.com/0xRadikal/Free-v2ray-Configs) | MIT |
| [Pawdroid](https://github.com/Pawdroid/Free-servers) | No license detected by GitHub; check upstream terms |
| [Mahdibland / V2RayAggregator](https://github.com/mahdibland/V2RayAggregator) | GPL-3.0; public `Eternity.txt` feed only |
| [XTLS / Xray-core](https://github.com/XTLS/Xray-core) | MPL-2.0; official binary downloaded locally, not committed |

License metadata was checked on 2026-10-15 and may change. Consult each upstream
license and retain required copyright/license notices when redistributing covered
material. A repository's software license does not by itself establish permission
from every proxy operator to redistribute credentials. Attribution is not a
substitute for permission; public visibility alone does not grant a general reuse
license. Do not add private or paid subscription credentials.

## Local data, troubleshooting and removal

`.local/history.json` includes public feed credentials and stays off Git.
`.local/geo_cache*` and `.local/logs/` stay local. `tools/` stays off Git.
Do not upload these directories or add private subscription sources.

- Installer requires elevation; a runtime scan does not.
- Authentication fails unattended: run `git push` interactively once as the task
  owner; do not use `SYSTEM`, S4U, or an SSH key requiring unavailable prompts.
- Task runs but does not wake: check `powercfg /a`, `powercfg /waketimers`, the
  task's WakeToRun property, AC power, firmware wake support, and Modern Standby.
- Long full scans: reduce `--max-tests`/timeout or modestly increase workers;
  scheduled trigger overlap is suppressed. Do not assume a 30-minute trigger
  guarantees a completed update every 30 minutes during a long scan.
- Source/proxy/GeoIP access may be filtered; existing nodes are still rechecked
  when sources fail. Unknown country does not remove useful nodes by default.
- To remove the task:

  ```powershell
  powershell -NoProfile -File .\scripts\install-task.ps1 -Uninstall
  ```

The installer changes only the current power plan's AC wake-timer setting, and
uninstall leaves it unchanged so other wake tasks keep working. Restore your own
preferred setting in Windows Power Options if desired.

Testing public proxies still exposes your source IP and connection metadata to
operators. End-to-end HTTPS verification is not a privacy audit. Redistribute
only credentials you are authorized to publish and respect upstream limits.
