# Free Sub — local Xray-verified subscriptions

Collect public VLESS, VMess, Trojan and Shadowsocks share links, verify real HTTPS
response data through the official Xray core on your Windows PC, remember node
reliability, and publish up to 300 working configurations to GitHub Pages.

Use the **Xray core** for this subscription in your client. Passing Xray tests
does not establish compatibility with sing-box. Share-link passwords are URL
escaped on export so reserved characters survive client import.
For v2rayN, use `https://parsabrg.github.io/free_sub/xray.json`: its native Xray
outbounds retain the tested settings and select Xray on each subscription update.
This applies to that subscription group without changing other groups' defaults.
On Android **v2rayNG**, use `https://parsabrg.github.io/free_sub/sub.b64`
instead; the Xray JSON outbound feed is intended for desktop v2rayN.

**The scheduled task wakes your PC from supported sleep on AC power. You do not
need to sit at the keyboard, unlock Windows, or approve each test or push.**
Windows cannot execute a test while the CPU remains asleep: the task wakes the
computer, runs, releases its sleep inhibitor, and normal idle sleep resumes.
Shutdown is not supported. Firmware, Modern Standby, power settings, lid behavior,
Wi-Fi recovery and corporate policies can prevent wake; verify on your actual PC.

## Control panel

Double-click `panel_windows.bat` (or run `python app.py`) for a page at
`http://127.0.0.1:8765/` with Start and Stop, the live log, and the source list.

- **Start** runs the same `main.py` as the scheduled task: once, or every 30 minutes
  while the panel stays open. **Stop** ends the run and every Xray process under it;
  the published list and the history stay as the last completed run left them.
- While it repeats, the panel keeps Windows from idle-sleeping (the display may still
  turn off). Unlike the scheduled task it cannot wake a sleeping PC, and closing its
  window stops everything.
- **Your subscription** lists the published addresses with a Copy button each.
- **Publish the list to GitHub** can be unticked to test without pushing.
- **Add subscription** takes any `https://` subscription address. It is tested in full
  on every discovery run, like a built-in feed. Working configs from every source go
  into the public subscription: do not add a private or paid one.
- Every source, built-in or found, has a switch; the table shows how many configs each
  one carried, how many were tested and how many worked in the last full scan.

The page is served to this computer only, and needs the token it was loaded with.
Starting it a second time opens the panel that is already running.
The panel and the scheduled task never run at once: the second to start reports
`[BUSY]` and exits. The panel shows a notice while the task is installed.

### Moving from the scheduled task to the control panel

1. In an **Administrator** PowerShell window, end and remove the task (this works
   with any version of this project):

   ```powershell
   Stop-ScheduledTask -TaskName FreeXraySubscription
   Unregister-ScheduledTask -TaskName FreeXraySubscription -Confirm:$false
   ```

2. In the project folder, get the current version. Your node history in `.local\`
   and the installed Xray in `tools\` are kept:

   ```powershell
   git pull --no-rebase --no-edit
   ```

3. Double-click `panel_windows.bat` and press **Start**.

Nothing else needs deleting: the task was the only part installed outside this folder.
To go back to unattended runs that wake the PC, run `scripts\install-task.ps1` again
as in the setup below; the panel and the task can also be used side by side.

## One-time Windows setup

Requirements: Windows 10/11 x64, Python 3.11+ (python.org installation recommended),
Git for Windows, and a Windows account password. Task Scheduler's password entry
requires your **account password, not your Windows Hello PIN**. Avoid moving the
clone after installation. No pip dependencies or OpenSSL installation are needed.
Python 3.13+ with OpenSSL 3.5+ (python.org's 3.14 has it) is best: its TLS hello is browser-sized
(measured 1,521 bytes, with a post-quantum key share), so a config that only passes for small-hello
clients such as curl is rejected here too. Older OpenSSL sends a smaller hello.

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

- Feeds: Barry, Shadowmere, 0xRadikal, Pawdroid, Mahdibland
  Public (`Eternity.txt`, not the account/airport list), Patterniha, Whitedns,
  Kahrobas, ALIILAPRO, Ermaozi, MatinGhanbari, and (since 2026-10-26) the
  0xRadikal's full list, MohammadBahemmat, hamedcode (port 443) and morpheusadam
  (`iran`): the four of sixteen screened collectors with any config that worked from
  Iran. These are untrusted
  candidate sources, not endorsed providers. (EbraSha's and Epodonios' repositories
  were deleted, 404 on 2026-10-26 and 2026-11-12, and are no longer used.)
- The feed list is no longer fixed. Once a day a discovery run asks GitHub's
  repository search for collectors pushed in the last two days, reads each new one's
  README for raw files in that same repository, and keeps the up to three files that
  together carry the most supported configs. From this development environment on
  2026-11-12 the search returned 171 repositories; the 25 most starred gave 13 usable
  feeds. **Whether any of their configs works from Iran is not known until this PC
  tests them.** `--found-sources 0` turns this off.
- A found feed is tested only on configs no listed source carries, 150 per run
  (never-tested first) until one of them passes a test. Then up to 1,500
  per run for the next seven days. A feed with nothing working waits 8 hours, then
  16, 32 and so on up to a week. Found feeds are named `gh:owner/repository` in
  `status.json`, with `tested` and `working` counts for every source.
- Verification takes the 600 best-ranked configs that passed the screen (`--recheck`,
  400 before). 422 to 505 passed each screen on 2026-11-12, so with 400 the 22 to
  105 lowest-ranked passes each run were never verified or listed; a config with no
  history ranks below one that has worked before.
- A config that failed its last three tests and has not worked for three days is
  retested once a day rather than every discovery run (`--retry-failed-hours 24`;
  `0` retests every time). In one discovery run on 2026-11-12, 5,127 of 7,439
  screening tests ended in a timeout, and each of those configs was tested again four
  hours later. Published nodes are always tested.
- When a source cannot be fetched directly (connection reset, timeout, or a 403/429
  refusal), the fetch is repeated through one of the five best published nodes. The
  site's HTTPS certificate is still verified on this PC, so the node can fail the
  fetch but cannot alter it. No node is started while direct fetches work.
- A discovery in which fewer than half of the listed sources answered is repeated
  after one hour instead of four.
- Sources have an 8 MiB fetch limit, are fetched eight at a time, and get one retry.
  No hidden per-source 1,800-node cap on listed sources.
- Source candidates are balanced and rotated between discovery runs. Previously
  published nodes are checked first. Full discovery tests every supported unique
  configuration of the listed sources that is not resting (`--max-tests 0`), within
  the documented fetch limits.
  Set a positive `--max-tests` only when deliberately choosing a sample.
- Private, loopback, link-local and mixed public/private DNS endpoint answers are
  rejected. DNS resolution runs with a deadline. The Xray endpoint is pinned to
  the validated public IP; SNI and transport Host values remain intact.
- Each candidate runs in its own Xray process with a loopback SOCKS inbound and
  candidate-only routing. No direct fallback outbound exists in the tester.
- Discovery first-pass network checks have a five-second deadline (`--screen-timeout 5`)
  after Xray is ready. Python/DNS/Xray startup remains inside a separate 30-second
  total candidate deadline, so CPU startup delays are not mistaken for proxy timeouts.
  The screen requires a certificate-verified HTTPS 204 response, matching a real-delay
  check without spending the short budget on a body download. It favors
  speed and can reject working but slower nodes. Full verification and health checks
  retain a 30-second deadline (`--timeout 30`). Discovery
  finalists require a new process and successful transfers to two different
  probe hostnames. Every health check also requires successful transfers to two
  different probe hostnames before marking a node freshly verified.
  Discovery uses up to 64 workers; full verification and health checks use at most 16.
  Unexpected runner exceptions get one retry with at most 16 workers. Sanitized
  exception types are logged; the last failure reason is retained in local history.
  The lightweight screen follows the approach used by
  [v2rayNG](https://github.com/2dust/v2rayNG/blob/master/V2rayNG/app/src/main/java/com/v2ray/ang/AppConfig.kt)
  and [v2rayN](https://github.com/2dust/v2rayN/blob/7.25.4/v2rayN/ServiceLib/Handler/ConnectionHandler.cs).
- Roughly 16 KiB download and smaller fallback endpoints prove connectivity.
  Transfer estimates include handshake time and **are not sustained-speed
  benchmarks**. Timing variation across different endpoints is reported separately
  from long-term reliability; it is not a gaming packet-jitter measurement.
- History keeps at most 20,000 nodes from 30 days and uses recent weighted successes/failures and penalizes one-off results.
  Up to four credentials per actual connection IP:port limit clones. Historical
  scores improve selection but cannot guarantee uptime or available capacity.
- Names are `🇩🇪 DE-07 | VLESS-WS-TLS`: flag, country code, a number within that
  country, then the tested protocol, transport and security (`XX` and a globe when the
  country is unknown). A node keeps its number while it is listed and for a day after
  its last success; then the number is free for another node. Numbers
  are identities, not current rank; use `status.json` for rank.
- Flags refer to the exact pinned connection IP of the last passing test, possibly
  a CDN ingress, not necessarily exit country. GeoIP is cached for 14 days, capped
  at 300 new lookups/run and 900/day. GeoIP failure keeps a globe flag by default;
  `--require-country` is an optional stricter filter.

Unsupported connection parameters fail closed. XHTTP `extra`, complex headers,
external Shadowsocks plugins, unsupported transports/ciphers, insecure TLS,
and unknown URL extensions that carry a value are rejected rather than tested as a
simplified configuration. An extension with an empty value sets nothing and is
ignored, as is a `host` or `path` on RAW transport without an HTTP header. This deliberately sacrifices some coverage for accurate testing.
Original accepted share-link connection parameters are preserved on output, and
renamed links are parsed again to verify the effective configuration fingerprint.

Results describe your current test network. Every test connection is bound to this
PC's real network adapter (Wi-Fi or Ethernet; VPN, TUN and proxy adapters are never
chosen), so a VPN such as Nebula or v2rayN's TUN can stay on and the operator's
network is still what is measured. Before each run the egress country is checked
through that same path; if it is not Iran the run is skipped and the published
list is kept as it is (`--allow-non-iran` overrides). Nodes on this PC itself,
the self-test's fixtures, are not bound. A full-tunnel kill switch can still block
the bypass: the egress check then reads "unknown", every test fails, and the previous
list is kept until its 6-hour expiry. Different users/ISPs still need local app checks. No TUN/DNS-leak,
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

Generated plain/Base64/Xray subscriptions and metadata must agree. Only the five
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
- Xray outbounds for v2rayN: `https://parsabrg.github.io/free_sub/xray.json`
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
| [Shadowmere](https://shadowmere.xyz/) | Check the provider's terms |
| [0xRadikal](https://github.com/0xRadikal/Free-v2ray-Configs) | MIT |
| [Pawdroid](https://github.com/Pawdroid/Free-servers) | No license detected by GitHub; check upstream terms |
| [Mahdibland / V2RayAggregator](https://github.com/mahdibland/V2RayAggregator) | GPL-3.0; public `Eternity.txt` feed only |
| [Patterniha](https://github.com/patterniha/Free-Configs) | MIT |
| [Whitedns](https://github.com/iampedii/whitedns-sub) | No license detected by GitHub; check upstream terms |
| [Kahrobas](https://bot.kahrobas.shop/sub.txt) | Check the provider's terms |
| [ALIILAPRO](https://github.com/ALIILAPRO/v2rayNG-Config) | GPL-3.0 |
| [Ermaozi](https://github.com/ermaozi/get_subscribe) | GPL-3.0 |
| [MatinGhanbari](https://github.com/MatinGhanbari/v2ray-configs) | MIT |
| [hamedcode](https://github.com/hamedcode/port-based-v2ray-configs) | GPL-3.0 |
| [MohammadBahemmat](https://github.com/MohammadBahemmat/V2ray-Collector), [morpheusadam](https://github.com/morpheusadam/v2ray-config) | No license detected by GitHub; check upstream terms |
| [XTLS / Xray-core](https://github.com/XTLS/Xray-core) | MPL-2.0; official binary downloaded locally, not committed |

Feeds found automatically are not in this table and their licenses have not been
checked by anyone: `status.json` names each one (`gh:owner/repository`) on every
discovery run. Switch one off in the control panel, or all with `--found-sources 0`.

License metadata was checked on 2026-10-15 (ALIILAPRO, Ermaozi and MatinGhanbari on 2026-10-26) and may change. Consult each upstream
license and retain required copyright/license notices when redistributing covered
material. A repository's software license does not by itself establish permission
from every proxy operator to redistribute credentials. Attribution is not a
substitute for permission; public visibility alone does not grant a general reuse
license. Do not add private or paid subscription credentials.

## Local data, troubleshooting and removal

`.local/history.json` includes public feed credentials and stays off Git.
`.local/sources.json` holds your added subscriptions, switched-off sources and the
record of found feeds; it stays off Git too. Delete it to start discovery afresh.
`.local/geo_cache*` and `.local/logs/` stay local. `tools/` stays off Git.
Do not upload these directories or add private subscription sources.

- Installer requires elevation; a runtime scan does not.
- Authentication fails unattended: run `git push` interactively once as the task
  owner; do not use `SYSTEM`, S4U, or an SSH key requiring unavailable prompts.
- Task runs but does not wake: check `powercfg /a`, `powercfg /waketimers`, the
  task's WakeToRun property, AC power, firmware wake support, and Modern Standby.
- Full scans use 64 parallel workers by default (`--workers 1` through `64`).
  This operator PC has 20 logical CPUs and 32 GiB RAM; reduce workers on smaller PCs.
- Long full scans: reduce `--max-tests`/timeout or adjust workers;
  scheduled trigger overlap is suppressed. Do not assume a 30-minute trigger
  guarantees a completed update every 30 minutes during a long scan.
- Source/proxy/GeoIP access may be filtered; existing nodes are still rechecked
  when sources fail. Unknown country does not remove useful nodes by default.
- To remove the task, in an Administrator PowerShell window (it ends a run in
  progress, and fails loudly if the task is still there afterwards):

  ```powershell
  powershell -NoProfile -File .\scripts\install-task.ps1 -Uninstall
  ```

The installer changes only the current power plan's AC wake-timer setting, and
uninstall leaves it unchanged so other wake tasks keep working. Restore your own
preferred setting in Windows Power Options if desired.

Testing public proxies still exposes your source IP and connection metadata to
operators. End-to-end HTTPS verification is not a privacy audit. Redistribute
only credentials you are authorized to publish and respect upstream limits.
