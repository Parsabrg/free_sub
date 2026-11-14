# Reference

- [Command line: main.py](#command-line-mainpy)
- [Command line: other programs](#command-line-other-programs)
- [Files and folders](#files-and-folders)
- [status.json](#statusjson)
- [Log messages](#log-messages)

## Command line: main.py

`python main.py [options]` performs one run.

| Option | Default | Meaning |
|---|---|---|
| `--mode auto\|full\|health` | `auto` | `full` fetches every source, screens and verifies. `health` rechecks the published and standby nodes. `auto` picks `full` when the last discovery is older than `--discovery-hours`, otherwise `health`. |
| `--publish` | off | Run the test gate and push the verified files to the `subscriptions` branch. |
| `--probe-control PATH` | off | Local file holding a known-good share link for identifying a video mirror outage. |
| `--video-mirror URL` | built-in mirrors | Override the video origins with up to four public HTTPS MP4 range URLs. Repeat the option for each URL. |
| `--stream-segments N` | 1 | Transfer up to eight consecutive MP4 ranges and record startup, throughput and stalls. |
| `--check-udp`, `--check-ipv6` | off | Record separate UDP relay and IPv6 destination checks. |
| `--youtube-id ID` | off | Measure playback of an embeddable YouTube video with Playwright Chromium. |
| `--xray PATH` | `tools\xray.exe` | The Xray binary (`tools/xray` on Linux). |
| `--limit N` | 0 | Most nodes in the published list (1 to 1000). `0` lists every working node. |
| `--interval-minutes N` | 30 | How often runs are started (1 to 1440), published in `status.json`. The panel or the scheduled task does the scheduling; this option only states it. |
| `--workers N` | 64 | Parallel screening tests (1 to 64). Verification and health checks use at most 16. |
| `--screen-timeout S` | 5 | Network deadline of a screening test, counted from when Xray is ready. |
| `--timeout S` | 30 | Total deadline of a verification or health test, and of a screening test including startup. |
| `--recheck N` | 600, or `--limit` if larger | Screen passes verified per discovery; standby nodes rechecked per health run. Not below `--limit`. |
| `--max-tests N` | 0 | Cap on candidates per discovery. `0` tests every supported candidate that is not resting. |
| `--discovery-hours H` | 4 | Interval between discoveries in `auto` mode (1 to 24). |
| `--ttl-hours H` | 6 | How long a node stays listed after its last success (1 to 24). |
| `--found-sources N` | 40 | Feeds found on GitHub tested per discovery. `0` uses only the listed sources. |
| `--retry-failed-hours H` | 24 | Wait before retesting a configuration that failed three times running. `0` retests every discovery. |
| `--require-country` | off | List only nodes whose country lookup succeeded. |
| `--geo-workers N` | 3 | Parallel country lookups (1 to 8). |
| `--allow-non-iran` | off | Test even when the PC does not reach the internet from Iran. |

Exit code `0` is a completed run, including one that was skipped for the egress
country. Exit code `2` is a run that failed, found another run active, or was given
an invalid option; the last log line says which.

## Command line: other programs

| Program | Purpose |
|---|---|
| `python app.py [--port 8765] [--no-browser]` | Starts the control panel as a background program and opens its page, or just opens the page if a panel already answers on the port. `panel_windows.bat` runs this. |
| `python app.py --serve` | Runs the panel in the current window instead, for diagnosis. Ctrl+C or **Quit panel** ends it. |
| `python verify.py --xray PATH` | The complete test gate: regression tests plus real Xray end-to-end tests. Required before a push; never skips the Xray tests. |
| `python -m unittest discover -s tests -v` | The tests alone. Skips the real Xray tests unless `XRAY_E2E_BINARY` is set. |
| `python download_xray.py` | Installs the pinned official Xray release for this platform, verifying its digest. Needed on Linux only. |
| `run_windows.bat` | One `--mode auto --publish` run in a console window. |
| `scripts\install-task.ps1 [-Uninstall]` | Installs or removes the scheduled task. Needs an elevated PowerShell window. |
| `scripts\check-task.ps1` | Shows the task's state, last run and the PC's wake timers. |

## Files and folders

| Path | In Git | Contents |
|---|---|---|
| `main.py` | yes | One run: collect, test, select, write the outputs. |
| `collector.py` | yes | Built-in feed list, fetching, the strict share-link parser. |
| `sources.py` | yes | Your own sources, switches, and feed discovery on GitHub. |
| `relay.py` | yes | Fetching a blocked source through a verified node. |
| `xray_test.py`, `process_guard.py`, `network.py` | yes | The probe, process containment, endpoint and adapter rules. |
| `state.py`, `naming.py`, `geolocation.py` | yes | Node history and reliability, names, country flags. |
| `publishing.py`, `verify.py` | yes | Output validation, the Git push, the pre-push test gate. |
| `app.py`, `panel.html` | yes | The control panel: launcher, local web server, page. |
| `tools/xray.exe` | yes | The pinned official Xray binary for Windows; see `tools/README.md`. |
| `docs/` | yes | The published outputs, deployed by GitHub Pages. Written by the program only. |
| `guide/` | yes | This documentation. |
| `tests/` | yes | Regression and end-to-end tests. |
| `.local/` | no | History, sources, caches, logs; see [setup](setup.md#local-data). |

The published outputs in `docs/`:

| File | Contents |
|---|---|
| `sub.txt` | The share links, one per line. |
| `sub.b64` | The same list, Base64-encoded. |
| `xray.json` | The tested Xray outbounds, for v2rayN. |
| `xray-pinned.json` | Xray outbounds that dial their verified IP while retaining the TLS and transport settings. |
| `sub-compact.b64` | Up to 20 ranked share links with distinct server IPs, for mobile clients. |
| `status.json` | The test report described below. |
| `index.html` | The public status page. |

## status.json

Top level:

| Field | Meaning |
|---|---|
| `schema` | Format version, currently `2`. |
| `generated_epoch`, `generated_utc` | When the run finished. |
| `mode` | `full` or `health`. |
| `health` | `ok`; `retained_previous` when nothing passed and the previous list is kept within its expiry; `expired_or_empty` when the list is empty. |
| `published` | Number of nodes in the list. |
| `working_this_run` | Configs that passed in this run. Equal to `published` unless a maximum is set, or an outage left older nodes listed within their expiry. |
| `list_limit` | The maximum set for the list, or `null` when every working node is listed. |
| `nodes` | One entry per node, in rank order. |
| `source_status` | Per-source discovery results. `working` counts uniquely credited full passes, `verified` counts all full passes carried by that source, and `screen_passed` counts quick passes. |
| `source_retained_24h` | Counts of credited configs with a fresh full pass at least 24 hours after their first credited pass. |
| `video_mirrors`, `run_comparison` | Per-mirror attempt outcomes and changes from the previous completed run. |
| `unique_selected_for_test` | Candidates tested in this run. |
| `first_pass`, `second_pass` | Result counts by outcome. Screening separates timeouts before Xray readiness from timeouts during the HTTPS test. |
| `resting_skipped` | Candidates left out because they are resting. |
| `node_ttl_seconds`, `health_interval_seconds`, `discovery_interval_seconds`, `recommended_client_refresh_seconds` | The schedule in force: node expiry, the run interval, the interval between discoveries, and the advice to clients. |
| `xray_version`, `workers`, `verification_workers`, `max_tests`, the `*_timeout_seconds` fields | The settings of the run. |
| `flag_definition`, `tested_on`, `failure_reason` | Explanatory text. |

Each entry of `nodes`:

| Field | Meaning |
|---|---|
| `id` | SHA-256 of the effective Xray configuration. Stable across renames. |
| `name`, `rank`, `protocol` | Client label, position in the list, and protocol identifier (`vless`, `vmess`, `trojan`, `ss`, `hysteria2` or `socks`). |
| `country_code`, `endpoint_ip` | Country of, and the exact IP used by, the last passing test. |
| `last_success_epoch`, `expires_epoch` | Last passing test, and when the node should be dropped without another. |
| `verified_this_run` | Whether the node passed in this run, or is carried over. |
| `success_rate` | The recency-weighted reliability score, 0 to 1. |
| `latency_ms`, `jitter_ms` | Median time of the test transfers, and the spread between them. |
| `body_bytes`, `transfer_kbps_estimate`, `full_size` | Bytes received, a rough rate that includes handshake time, and whether the required media range completed. |
| `video_host`, `video_bytes`, `video_ms` | Successful video origin, validated MP4 bytes and transfer time including connection setup. |
| `stream_segments`, `stream_kbps`, `stream_start_ms`, `stream_stalls`, `stream_error` | Optional consecutive-range results. |
| `udp`, `ipv6`, `youtube_played`, `youtube_error` | Optional capability results and YouTube diagnostics. A null capability is unmeasured. |

## Log messages

| Prefix | Meaning |
|---|---|
| `[NETWORK]` | The adapter the tests leave through and the egress country. |
| `[SKIP]` | The egress country is not Iran; nothing was tested or changed. |
| `[SOURCES]` | How many sources are about to be fetched. The control panel counts the `[SOURCE]` lines after it for its progress bar. |
| `[SOURCE]` | One line per source, in the order they finish: links found and supported, or `FAILED`. |
| `[RELAY]` | Direct fetches failed and a verified node is carrying them. |
| `[DISCOVER]` | The daily GitHub search and its result. |
| `[RUN]` | The mode and the number of candidates. |
| `[CHECK]`, `[VERIFY]` | Progress of the screen or health check, and of the verification. |
| `[GEO]` | Country lookups. |
| `[DONE]` | Nodes listed and the run's health. |
| `[PUBLISH]`, `[PUBLISH ERROR]` | The push, or the category of a Git failure. |
| `[BUSY]` | Another run holds the lock. |
| `[PROBE]` | The known-good control passed HTTPS while video mirrors failed. |
| `[ERROR]` | The run failed. Details are deliberately withheld from the log; see `.local\last_run.json`. |
