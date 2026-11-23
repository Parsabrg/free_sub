# free_sub review — 2026-11-20

Ranked by importance. Status: `open` / `done` / `skipped (reason)`.

## 1. The feed stopped 3 days ago: nothing restarts the panel after Windows restarts — done (Start with Windows)
- Last publish: 2026-11-17 23:07. Windows shut down 2026-11-18 00:01 during a run; no Python has run since.
- The panel only runs while someone has started it. The scheduled task (`install-task.ps1`) is not installed
  and needs admin + the Windows password; installing it next to the panel would also mean two runs
  publishing the same feed (one lock per clone).
- Fix: a "Start with Windows" option. At sign-in the panel starts hidden and resumes the saved repeat
  schedule (`.local/panel.json` already holds mode, publish, repeat, interval). No admin, no password.
- Also: the Documents clone is on b0036e8, without #13 and #14.
- 2026-11-23: Windows skipped the Run entry at every sign-in after 2026-11-18 (Windscribe's too, per the
  Shell-Core log), so the feed stopped again. Start with Windows is now a scheduled task at sign-in.

## 2. A crashed or interrupted run leaves no log on disk — done (run.log, last_error.txt)
- Run output lives only in the panel's memory (last 2000 lines). After the shutdown above there is
  nothing to read. `main.py` prints only the error type and points at `last_run.json`, which a failed
  run never writes.
- Fix: write each run's output to `.local/logs/run.log` (keep the last few) and the traceback of a
  failure to the same place. Both stay private in `.local`.

## 3. Up to 11% of configs may fail on a slow DNS helper, not on DNS — done (in-process lookup; 64 lookups 0.79 s → 0.02 s idle)
- Each config's hostname is looked up by starting another Python process with a 4 s limit.
  Last discovery: 972 of 13,427 configs hit that limit and 526 failed the lookup.
- With 64 tests in parallel, starting Python can take much of those 4 s, so some are likely false failures.
- Fix: look each hostname up once per run, in-process (many configs share hosts). Measure before/after.

## 4. The pass bar is light — open (your call)
- A config passes on one small HTTPS fetch plus 128 KB of video. Configs that pass can still be too
  slow for real use.
- Option: finalists also fetch ~1 MB (e.g. `--stream-segments 2..4`, already supported, off by default).
  Costs more time per discovery.

## 5. Every publish runs the full test suite twice per discovery — done (skipped when it passed today on the same code and Xray)
- `publish()` runs `verify.py` (~80 s here, 180 s limit) before each push; a discovery publishes twice.
  On a busy PC it can hit the limit and block publishing.
- Fix: skip it when code and Xray are unchanged since the last pass (`verification.json` already
  records `source_sha256` and the Xray hash; nothing reads them).

## 6. Small things — first two done
- "Update program" decides "nothing new" by Git's English message; compare HEAD before/after instead.
- `panel.log` lines get overwritten when the panel restarts itself ("el starting").
- The panel clone always shows modified `docs/` files, because runs rewrite tracked files.
