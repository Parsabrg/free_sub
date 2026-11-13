# How it works

- [Schedule](#schedule)
- [Sources](#sources)
- [Parsing](#parsing)
- [Testing](#testing)
- [The network being measured](#the-network-being-measured)
- [Selection](#selection)
- [Names and flags](#names-and-flags)
- [Publishing](#publishing)
- [When things fail](#when-things-fail)
- [What is not certified](#what-is-not-certified)

## Schedule

| Operation | Default |
|---|---|
| A run (panel repeat, or the scheduled task) | Every 30 minutes; the panel's "Run every" field changes it |
| Health check of the published and standby nodes | Every run |
| Discovery: fetch all sources, screen and verify | Every 4 hours in `--mode auto` |
| Search GitHub for new feeds | Once a day, during a discovery |
| A node leaves the list after its last success | 6 hours |
| Suggested client refresh | Hourly |

The client guidance is only guidance: this project builds a subscription, it is not
a VPN app. A plain share-link subscription has no per-node expiry, so an ordinary
client can keep a cached link indefinitely; only an app that reads `status.json`
can enforce `expires_epoch`. If the PC is off, GitHub Pages keeps serving the last
deployment, and the status page works out its age in the browser rather than
presenting a stopped runner as healthy.

## Sources

Three kinds of source feed a discovery. None is trusted: a source only supplies
candidates, and every candidate goes through the same parser and tests.

**Built-in feeds.** The list in `collector.py` and in the
[README](../README.md#sources-and-acknowledgments). Each can be switched off in the
control panel.

**Your own.** Any `https://` subscription address added in the control panel. It is
tested in full on every discovery, like a built-in feed. In `status.json` it
appears under the name you gave it, or `Custom N`, never under its address.

**Found on GitHub.** Once a day a discovery asks GitHub's repository search for
collectors pushed in the last two days, reads each new repository's README for raw
files in that same repository, and keeps the up to three files that together carry
the most supported configurations. Split-by-protocol copies of one list add nothing
after the first and are not chosen twice. From the development environment on
2026-11-12 the search returned 171 repositories, and the 25 most starred gave 13
usable feeds. Whether a found feed has anything that works from the operator's
network is only known once it has been tested there, so:

- A found feed is tested only on configurations that no listed source carries:
  150 per run, never-tested ones first, until one of them passes a test.
- After that it gets up to 1,500 per run for seven days.
- A feed with nothing working waits 8 hours before its next sample, then 16, 32 and
  so on up to a week.
- At most 40 found feeds are tested per discovery (`--found-sources`; `0` turns
  the feature off).

**Fetching.** Sources are fetched eight at a time with an 8 MiB limit each and one
retry. When a source cannot be fetched directly (connection reset, timeout, or a
403/429 refusal), the fetch is repeated through one of the five best published
nodes. The site's HTTPS certificate is still verified on the PC, so the node can
fail the fetch but cannot alter what is fetched. No node is started while direct
fetches work. A discovery in which fewer than half of the listed sources answered
is repeated after one hour instead of four.

## Parsing

A share link becomes an Xray outbound or is rejected. It is never tested as a
simplified version of itself.

Supported share links include VLESS with TLS, REALITY or VLESS Encryption, VMess
AEAD in URI and Base64 JSON forms, Trojan, Shadowsocks, Hysteria 2 (`hysteria2://`
and `hy2://`, including Salamander and port lists), and SOCKS5. Supported stream transports
include RAW (TCP), mKCP, WebSocket, gRPC, HTTPUpgrade and XHTTP where the protocol
allows them. Shadowsocks accepts AEAD and Shadowsocks 2022 ciphers.

Rejected:

- anything that disables certificate verification (`allowInsecure`, `insecure`);
- VLESS without TLS, REALITY or VLESS Encryption, and Trojan without TLS or REALITY;
- legacy VMess `alterId` and VMess without payload encryption or outer security;
- RAW HTTP camouflage, XHTTP `downloadSettings`, external Shadowsocks plugins,
  and Hysteria 2 obfuscation other than Salamander;
- any unknown extension that carries a value.

An extension with an empty value sets nothing and is ignored, as is a `host` or
`path` on RAW transport without an HTTP header. Duplicates are merged by the
effective Xray configuration, so the same server under two names is one candidate.
The accepted parameters are preserved on output, and every renamed link is parsed
again to confirm that it still describes the configuration that was tested.

The panel’s private tester also accepts standalone Xray JSON outbounds, including
userspace WireGuard, HTTPS CONNECT, and XHTTP with a separate download endpoint.
The tester validates the outbound and pins its endpoints before running Xray.
Private inputs stay outside the discovery history and publishing pipeline.

## Testing

Every candidate runs in **its own Xray process** with a loopback SOCKS inbound and
routing to that candidate only. The tester has no direct fallback outbound.

**Endpoint rules.** DNS resolution runs out of process with a deadline. Private,
loopback, link-local and mixed public/private answers are rejected. Xray is pinned
to the validated public IP while SNI and transport Host values stay as given.

**Screen (discovery, first pass).** Up to 64 workers. Once Xray is ready, the
candidate has five seconds (`--screen-timeout`) to return a certificate-verified
HTTPS response from Google or Apple. Python, DNS and Xray startup are covered by a separate
30-second total deadline, so a slow CPU is not mistaken for a slow proxy. The
screen favors speed and can reject a working but slow node. It follows the approach
of [v2rayNG](https://github.com/2dust/v2rayNG/blob/master/V2rayNG/app/src/main/java/com/v2ray/ang/AppConfig.kt)
and [v2rayN](https://github.com/2dust/v2rayN/blob/7.25.4/v2rayN/ServiceLib/Handler/ConnectionHandler.cs).

**Verify (discovery, second pass) and health checks.** At most 16 workers and a
30-second deadline (`--timeout`). A node must pass both a small HTTPS request and
a 128 KiB MP4 range request to a separate site in the same fresh Xray process.
The verifier checks status `206`, the range and content type, the exact byte count,
and that the range contains media data. The 600 best-ranked screen passes are
verified (`--recheck`). Only full checks update the publication lifetime and
reliability score. An upgrade rechecks saved nodes against the video gate.

**Probe sites.** Wikipedia and Apple supply the small HTTPS response. The video
comes from MDN, with a W3C video as fallback. The video measurement covers a
bounded transfer and includes connection setup time. It measures whether the node
can carry video bytes at test time.

Use `--video-mirror` to supply alternative public HTTPS MP4 origins and
`--probe-control` with a local file containing a known-good share link. If the
control can carry HTTPS but every video mirror fails, the run preserves node
history and lets existing verification timestamps age. A full scan refreshes
the current feed and up to 20 standby nodes before fetching new sources.

Optional checks use the same verified outbound. `--stream-segments 4` measures
four ranges from the video, and `--check-udp --check-ipv6` records those
capabilities separately. For YouTube, install Playwright and its Chromium build
with `python -m pip install playwright` and `python -m playwright install chromium`,
then pass `--youtube-id` with an embeddable video ID. The browser check requires
both playback progress and a media response through Xray.

**Resting.** A configuration that failed its last three tests and has not worked
for three days is retested once a day rather than on every discovery
(`--retry-failed-hours`; `0` retests every time). In one discovery on 2026-11-12,
5,127 of 7,439 screening tests ended in a timeout, and each of those configurations
was tested again four hours later. Published nodes are always tested.

**Containment.** A worker's DNS, Xray startup, TLS and body reads all sit inside
the candidate deadline. Process groups on Linux and a kill-on-close job object on
Windows end Xray with its worker; if a Windows process cannot be contained, the
probe fails rather than leaving a stray child. An operating-system lock prevents
two runs at once. Unexpected runner errors get one retry with at most 16 workers.

## The network being measured

Results describe the network the tests leave from. Every test connection is bound
to the PC's real network adapter (Wi-Fi or Ethernet; VPN, TUN and proxy adapters
are never chosen), so a VPN such as Nebula or v2rayN's TUN mode can stay on and the
operator's own network is still what is measured.

Before each run the egress country is checked through that same path. If it is not
Iran the run is skipped and the published list is left as it is
(`--allow-non-iran` overrides). A full-tunnel kill switch can still block the
bypass: the egress check then reads "unknown", every test fails, and the previous
list is kept until its six-hour expiry.

Source lists are downloaded with Python's normal HTTP client, which follows the
system proxy setting. That affects only where the candidate list is fetched from,
never how a candidate is tested.

## Selection

- **History.** At most 20,000 nodes from the last 30 days, each with its last 48
  results.
- **Reliability.** A recency-weighted score in which a result loses half its weight
  each day. A single fresh success cannot score as 100% reliable.
- **Ranking.** Reliability first, then a full-size transfer, latency, and the
  variation between the two transfers.
- **Diversity.** At most four credentials per connection IP and port, so one server
  cannot fill the list with clones.
- **Limit.** Every working node is listed, unless a maximum is set (`--limit`, or the panel's "Max nodes in the list"); then the most reliable ones up to that number. A larger list is rechecked on every run, so each run takes longer.

A historical score improves selection. It cannot guarantee uptime or capacity.

## Names and flags

Names have the form `🇩🇪 DE-07 | VLESS-WS-TLS`: flag, country code, a number within
that country, then the tested protocol, transport and security. An unknown country
is shown as `🌐 XX`. A node keeps its number while it is listed and for a day after
its last success; after that the number is free for another node. Numbers are
identities, not ranks; `status.json` gives the rank.

The flag refers to the exact IP the last passing test connected to. That can be a
CDN ingress and is not necessarily the exit country. Country lookups are cached for
14 days and limited to 300 new lookups per run and 900 per day. A failed lookup
leaves a globe and keeps the node; `--require-country` is an optional stricter
filter.

## Publishing

Before each push, `verify.py` runs the regression tests and real end-to-end tests
with the installed Xray binary. The fixture generates its own temporary TLS
certificate, starts loopback Xray proxy fixtures and an HTTPS origin, fetches a source over
HTTPS, runs the whole pipeline, rejects wrong credentials and a wrong TLS hostname,
rejects malformed MP4 responses, enforces a hard deadline, rechecks health, keeps names stable and expires old
nodes. The loopback-only fixture override exists only in the tests: production has
no switch to bypass the public-address checks or accept an untrusted certificate.

- The plain, Base64 and Xray outputs and the report must agree, or nothing is
  written.
- A separate Git index builds the `subscriptions` branch from the generated
  feed files and a snapshot signal workflow. The Pages workflow deploys the feed
  using the site and validation code from the default branch.
- Git uses time limits and noninteractive authentication. A failed push can be
  retried on the next run. Concurrent updates use normal fast-forward rules.
- Share-link passwords are URL-escaped on output so that reserved characters
  survive import into a client.

GitHub CI runs the same loopback-only Xray tests on Windows and Linux and parses
the PowerShell scripts. It never tests public proxies, registers tasks, wakes
hardware or publishes results; that stays on the operator's PC.

For a client release smoke test, serve a generated `xray.json` over local HTTP
and update that subscription in v2rayN **7.25.6**. Import the generated `sub.b64`
from a local file in v2rayNG **2.3.15**. A SOCKS5 fixture lets you confirm both
the imported label and the network path. Select the imported profile and request
an HTTP `204` test path through the client’s SOCKS port. On an Android emulator,
use `adb reverse` for the host-side fixture ports and `adb forward` for the
client SOCKS port. Both client releases imported the fixture and returned `204`
through their running cores.

## When things fail

- After a run in which nothing passed, the previous list may be kept **only within
  each node's original expiry**, and the report says `retained_previous`.
- A failed run never renews a last-success time. An expired list becomes empty on
  the next completed run (`expired_or_empty`).
- A failure before the outputs are written leaves the previous deployment in place,
  and its timestamps keep aging.
- No success is ever invented to fill the list.

## What is not certified

An HTTPS probe says that a node carried a few requests from one network at one
moment. It does not establish:

- that the node works on another network or ISP;
- UDP support, gaming latency, packet loss or sustained throughput;
- the absence of DNS or TUN leaks in your client;
- privacy, or anything about who operates the node;
- unlimited capacity or future uptime.
