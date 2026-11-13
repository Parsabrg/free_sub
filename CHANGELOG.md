# Changelog

Changes to the program. The published subscription itself changes every 30 minutes
and is not recorded here.

## 2026-11-13

### Added
- **Control panel** (`panel_windows.bat`, `app.py`): start and stop runs, follow
  the log, copy the subscription addresses, add your own subscription sources,
  switch sources on and off, and update the program.
- **Feed discovery**: a daily GitHub search for recently updated collectors. Found
  feeds are sampled until one of their configurations works, then tested wider.
- **Relay for blocked fetches**: a source that cannot be fetched directly is
  fetched through an already verified node, with the site's certificate still
  checked on the PC.
- **Bundled Xray**: the pinned official Windows binary is part of the repository,
  with a test that refuses any other file.
- Per-source `tested` and `working` counts in `status.json`.
- Documentation split into a short README and the guides in `guide/`.

### Changed (later the same day)
- **The published list now holds every working node** instead of at most 300. A
  maximum can still be set, from 1 to 1,000.
- The control panel's first tile shows how many configs **worked in the last run**,
  with the number in the list below it.

### Added (later the same day)
- Two settings in the control panel: **Max nodes in the list** and **Run every N
  minutes**. `status.json` records `list_limit`, `working_this_run` and the run
  interval that was actually used.

### Fixed
- The test gate that runs before every push failed on a PC where the project sits
  inside another Git repository (the panel took that repository's address) or where
  the scheduled task is installed (a test assumed it was not), which stopped all
  publishing. The panel now trusts Git only in a folder that is itself a clone, and
  **Update program** refuses anywhere else rather than updating a different project.
  The panel tests no longer depend on what else is installed on the machine.
- The control panel ended whenever the window that started it was closed, and its
  page then kept showing the last state it had received as if the run were still
  going. The panel now runs as a background program without a window of its own,
  has a **Quit panel** button, and its page says **Panel stopped** (and disables its
  buttons) when the program is gone. A page left open across a restart reloads
  itself. A crash is recorded in `.local\logs\panel.log`.

### Changed
- **Node names** are now `🇩🇪 DE-07 | VLESS-WS-TLS` with small numbers per country,
  replacing `server 🇩🇪 50186`.
- A configuration that failed its last three tests is retested once a day instead
  of on every discovery.
- The `--recheck` default is 600 (was 400), so that new configurations which pass
  the screen are also verified.
- A discovery in which fewer than half of the listed sources answered is repeated
  after one hour instead of four.
- Sources are fetched eight at a time with one retry.
- The parser ignores extensions with an empty value, and `host` or `path` on RAW
  transport without an HTTP header. Extensions that carry a value are still
  rejected.
- The public status page shows the addresses with Copy buttons and a summary of
  protocols and countries.
- `install-task.ps1 -Uninstall` ends a run in progress and reports a removal that
  Windows refused.

### Removed
- The eight Epodonios feeds, whose repository returns 404.

## 2026-10-26 and earlier

Recorded in the source comments and in [guide/validation.md](guide/validation.md):
tests bound to the physical network adapter, the egress-country check, probe sites
outside Cloudflare, the 64-worker screen, and the replacement of the deleted
EbraSha feed.
