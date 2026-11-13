# Setup

Everything here is done once on the Windows PC that will run the tests.

- [Requirements](#requirements)
- [1. Get the project](#1-get-the-project)
- [2. Sign in to Git](#2-sign-in-to-git)
- [3. Switch on GitHub Pages](#3-switch-on-github-pages)
- [4. Run it](#4-run-it)
- [Unattended runs (scheduled task)](#unattended-runs-scheduled-task)
- [Moving from the scheduled task to the control panel](#moving-from-the-scheduled-task-to-the-control-panel)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Local data](#local-data)
- [Removal](#removal)

## Requirements

- Windows 10 or 11, x64.
- Python 3.11 or newer; the python.org installer is recommended. No pip packages
  and no OpenSSL installation are needed.
- Git for Windows.
- For the scheduled task only: a Windows account **password**. Task Scheduler does
  not accept a Windows Hello PIN.

Python 3.13 or newer with OpenSSL 3.5 or newer (python.org's 3.14 has it) gives the
most realistic test: its TLS hello is browser-sized (measured at 1,521 bytes, with a
post-quantum key share), so a configuration that only passes for small-hello clients
such as curl is rejected here too. Older OpenSSL sends a smaller hello.

## 1. Get the project

```powershell
git clone https://github.com/Parsabrg/free_sub.git
cd free_sub
```

Do not move the folder after installing the scheduled task.

The pinned official Xray binary for Windows x64 is part of the repository
(`tools\xray.exe`, 36 MB), so nothing else is downloaded. `tools\README.md` gives
its origin and SHA-256, and the tests that run before every push refuse any other
file. On Linux, or to move to another Xray version, run `python download_xray.py`:
it verifies the SHA-256 digest from the official release API and fails closed if
the digest is missing or does not match. Review the pinned version from time to
time; it is never upgraded silently.

## 2. Sign in to Git

Publishing pushes to your repository, so Git needs a name, an email and a saved
sign-in. Use Git Credential Manager or an SSH key. Never put a GitHub token into a
remote URL or into this project.

```powershell
git config user.name "YOUR NAME"
git config user.email "YOUR GITHUB COMMIT EMAIL"
git ls-remote origin HEAD
python verify.py --xray .\tools\xray.exe
```

The first real `git push` may ask you to sign in once. Do the first full scan by
hand so that this is settled before anything runs unattended:

```powershell
python main.py --mode full --publish
```

## 3. Switch on GitHub Pages

In the repository on GitHub: **Settings → Pages → Source: GitHub Actions**. The
workflow in `.github/workflows/pages.yml` deploys the latest verified snapshot
from the `subscriptions` branch. The first successful publish creates that branch.

- On GitHub Free the repository must be **public** for Pages. Eligible paid plans
  can host Pages from a private repository. Choose the visibility yourself; the
  program never changes it.
- The public subscription addresses expose the listed proxy credentials. Never add
  a personal or paid subscription that you do not intend to redistribute.

After a successful scan has been pushed, the addresses in the
[README](../README.md#subscription-addresses) are live.

## 4. Run it

Double-click `panel_windows.bat` and press **Start** in the page that opens. The
[README](../README.md#control-panel) describes the panel.

The panel runs in the background with no window of its own. It keeps going when
the browser tab or the launcher window is closed, and ends only with **Quit panel**,
a Windows sign-out or restart, or a crash. It runs only while the PC is awake;
while it repeats it keeps Windows from idle-sleeping, but it cannot wake a sleeping
PC. For runs with nobody present, use the scheduled task below. The two can be used
side by side: they never run at the same time, and the panel shows a notice while
the task is installed.
## Unattended runs (scheduled task)

The task runs every 30 minutes, lists every working node, wakes the PC from supported sleep on AC power, and
needs nobody at the keyboard: no unlock, no approval for a test or a push.

In an **Administrator PowerShell window for your normal Windows account**:

```powershell
powershell -NoProfile -ExecutionPolicy RemoteSigned -File .\scripts\install-task.ps1
```

The installer runs the real Xray verification, checks that Git can read the
repository without a prompt, enables AC wake timers, and asks for your Windows
account password once. Task Scheduler stores that credential; the project never
writes it to disk. The task itself runs with limited privileges. Git must already
be signed in for that same account, and a read check cannot prove write access, so
complete the manual `--publish` in step 2 first.

Check the installed task:

```powershell
powershell -NoProfile -File .\scripts\check-task.ps1
```

The panel's settings (maximum list size, run interval) apply to runs started from the
panel only. The task uses the defaults: every working node, every 30 minutes.

What the task does and does not do:

- It wakes the computer, runs, releases its sleep inhibitor, and lets normal idle
  sleep resume. It never forces sleep while you are using the PC.
- It ignores overlapping triggers, retries a failure twice five minutes apart,
  waits up to two minutes for Wi-Fi, and limits a run to three hours.
- A PC that is shut down cannot be woken. Firmware, Modern Standby, power settings,
  lid behavior, Wi-Fi recovery and corporate policies can also prevent a wake.
- For a real wake test, leave the PC on AC power, let it sleep before the next run,
  then look at the task's LastRun and LastResult and at `.local\logs\` afterwards.
  This check has only ever been possible on the PC itself.

## Moving from the scheduled task to the control panel

1. In an **Administrator** PowerShell window, end and remove the task. These two
   commands work with any version of this project:

   ```powershell
   Stop-ScheduledTask -TaskName FreeXraySubscription
   ```

   ```powershell
   Unregister-ScheduledTask -TaskName FreeXraySubscription -Confirm:$false
   ```

2. In the project folder, get the current version. Do not delete the folder: your
   node history in `.local\` is kept this way.

   ```powershell
   git pull --no-rebase --no-edit
   ```

3. Double-click `panel_windows.bat` and press **Start**.

Nothing else needs deleting; the task was the only part installed outside the
folder. To return to unattended runs, install the task again as described above.

## Updating

Press **Update program** in the control panel. If a newer version was pulled, the panel
restarts itself and the page reloads by itself, so there is nothing to close or reopen.
It works while the panel is idle, and also between two rounds of a repeating schedule: the
schedule is paused for the update and carries on in the restarted panel. It is refused
while a run is active (stop it first), and when nothing new was found it changes nothing.

A panel older than this feature does not restart itself after its first update: press
**Quit panel** and open `panel_windows.bat` again once. From a terminal:

```powershell
git pull --no-rebase --no-edit
```

Update the program periodically to use the current verifier. Feed snapshots are
published to the `subscriptions` branch, separate from the program updates.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `Xray missing; run python download_xray.py first` | `tools\xray.exe` is absent. Run `git pull --no-rebase --no-edit`, or `python download_xray.py`. |
| The page says **Panel stopped** | The panel program ended: it was quit, ended in Task Manager, or crashed. Open `panel_windows.bat` again; an open page reloads itself. A crash is recorded in `.local\logs\panel.log`. A run in progress ended with the panel. |
| `Port 8765 is used by another program` | Something else holds the port. Start the panel on another one: `python app.py --port 8766`. |
| `[BUSY] Another run is already active` | The scheduled task or a second window is running. Wait, or remove the task. |
| `FAIL: …` lines, then `CalledProcessError` | The test gate that runs before every push failed, so nothing was published and the failing test is named just above. Update the program first (a newer version may already fix it); if the same test still fails, send those lines. |
| `Update program works only in a Git clone` | The folder is not itself a clone (a downloaded ZIP, or a copy inside another repository). Clone the project as in step 1 and run it from there. |
| `[SKIP] Tests would reach the internet from …` | The PC reaches the internet from outside Iran and no physical adapter bypasses the VPN. Results would describe the VPN's network, so the published list is left as it is. `--allow-non-iran` overrides this. |
| `[PUBLISH ERROR] … category=authentication` | Run `git push` once interactively as the account that runs the tests. Do not use `SYSTEM`, S4U, or an SSH key that needs a prompt. |
| The task runs but the PC does not wake | Check `powercfg /a`, `powercfg /waketimers`, the task's WakeToRun property, AC power, firmware wake support and Modern Standby. |
| A full scan takes too long | Lower `--workers` on a smaller PC (the default 64 was chosen on 20 logical CPUs and 32 GiB RAM), or set a positive `--max-tests`. A 30-minute trigger does not guarantee a finished update every 30 minutes during a long scan. |
| Sources cannot be fetched | Fetches are retried through a verified node. Existing nodes are still rechecked when every source fails. |
| The installer refuses to run | It needs an elevated PowerShell window. A scan does not. |

Run logs of the scheduled task are in `.local\logs\` and are kept for 14 days. The
last run's full report is `.local\last_run.json`.

## Local data

These stay on the PC and are never committed:

| Path | Contents |
|---|---|
| `.local\history.json` | Every tested node with its recent results. Contains public feed credentials. |
| `.local\sources.json` | Your added subscriptions, switched-off sources and the record of found feeds. Delete it to start discovery afresh. |
| `.local\geo_cache*` | Country lookups, cached for 14 days. |
| `.local\logs\` | Scheduled-task logs, and `panel.log` with the panel's own output and any crash. |
| `.local\verification.json` | Result of the last pre-push test gate. |
| `.local\panel.json` | The settings last used in the control panel. |
| `.local\update-restart.json` | Present for a moment while the panel restarts after an update; says whether to resume a paused schedule. |

Do not upload `.local\`, and do not add private subscription sources.

## Removal

Remove the scheduled task in an Administrator PowerShell window. The script ends a
run in progress and fails loudly if the task is still there afterwards:

```powershell
powershell -NoProfile -File .\scripts\install-task.ps1 -Uninstall
```

The installer changed one setting outside the folder: the current power plan's AC
wake-timer option. Uninstalling leaves it as it is so that other wake tasks keep
working; restore your own preference in Windows Power Options if you want to.
After that, deleting the folder removes everything else.
