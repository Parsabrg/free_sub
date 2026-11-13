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
| `--publish` | off | Run the test gate, then commit and push the five files in `docs/`. |
| `--xray PATH` | `tools\xray.exe` | The Xray binary (`tools/xray` on Linux). |
| `--limit N` | 300 | Nodes in the published list (1 to 300). |
| `--workers N` | 64 | Parallel screening tests (1 to 64). Verification and health checks use at most 16. |
| `--screen-timeout S` | 5 | Network deadline of a screening test, counted from when Xray is ready. |
| `--timeout S` | 30 | Total deadline of a verification or health test, and of a screening test including startup. |
| `--recheck N` | 600 | Screen passes verified per discovery; standby nodes rechecked per health run. Not below `--limit`. |
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
| `python app.py [--port 8765] [--no-browser]` | The control panel. `panel_windows.bat` starts it. |
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
| `app.py`, `panel.html` | yes | The control panel. |
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
| `nodes` | One entry per node, in rank order. |
| `source_status` | Per source, for a `full` run: `raw` links found, `supported` unique configurations, `error`, `tested`, `working`. Found feeds also have `kind: "found"` and `new`, the configurations no listed source carries. Empty for a `health` run. |
| `unique_selected_for_test` | Candidates tested in this run. |
| `first_pass`, `second_pass` | Result counts by outcome for the screen and the verification. |
| `resting_skipped` | Candidates left out because they are resting. |
| `node_ttl_seconds`, `health_interval_seconds`, `discovery_interval_seconds`, `recommended_client_refresh_seconds` | The schedule in force. |
| `xray_version`, `workers`, `verification_workers`, `max_tests`, the `*_timeout_seconds` fields | The settings of the run. |
| `flag_definition`, `tested_on`, `failure_reason` | Explanatory text. |

Each entry of `nodes`:

| Field | Meaning |
|---|---|
| `id` | SHA-256 of the effective Xray configuration. Stable across renames. |
| `name`, `rank`, `protocol` | As shown in the client; position in the list; `vless`, `vmess`, `trojan` or `ss`. |
| `country_code`, `endpoint_ip` | Country of, and the exact IP used by, the last passing test. |
| `last_success_epoch`, `expires_epoch` | Last passing test, and when the node should be dropped without another. |
| `verified_this_run` | Whether the node passed in this run, or is carried over. |
| `success_rate` | The recency-weighted reliability score, 0 to 1. |
| `latency_ms`, `jitter_ms` | Median time of the test transfers, and the spread between them. |
| `body_bytes`, `transfer_kbps_estimate`, `full_size` | Bytes received, a rough rate that includes handshake time, and whether the larger download completed. |

## Log messages

| Prefix | Meaning |
|---|---|
| `[NETWORK]` | The adapter the tests leave through and the egress country. |
| `[SKIP]` | The egress country is not Iran; nothing was tested or changed. |
| `[SOURCE]` | One line per source: links found and supported, or `FAILED`. |
| `[RELAY]` | Direct fetches failed and a verified node is carrying them. |
| `[DISCOVER]` | The daily GitHub search and its result. |
| `[RUN]` | The mode and the number of candidates. |
| `[CHECK]`, `[VERIFY]` | Progress of the screen or health check, and of the verification. |
| `[GEO]` | Country lookups. |
| `[DONE]` | Nodes listed and the run's health. |
| `[PUBLISH]`, `[PUBLISH ERROR]` | The push, or the category of a Git failure. |
| `[BUSY]` | Another run holds the lock. |
| `[UPDATE]` | GitHub has a newer version; update before publishing can continue. |
| `[ERROR]` | The run failed. Details are deliberately withheld from the log; see `.local\last_run.json`. |
