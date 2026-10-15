[CmdletBinding()]
param(
    [string]$TaskName = 'FreeXraySubscription',
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host 'Task removed. Wake timer power settings are unchanged.'
    exit 0
}
# Elevated once during setup; runtime itself uses the normal user account.
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$Admin = New-Object Security.Principal.WindowsPrincipal($Identity)
if (-not $Admin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer once in an elevated PowerShell window for your normal Windows account.'
}
$Python = (Get-Command python -ErrorAction Stop).Source
$Xray = Join-Path $Root 'tools\xray.exe'
if (-not (Test-Path -LiteralPath $Xray)) { throw 'Run python download_xray.py first.' }
if (-not (Test-Path -LiteralPath (Join-Path $Root '.git'))) { throw 'Install from your dedicated git clone, not an extracted ZIP.' }
# Verify Git can authenticate without opening a dialog BEFORE installing any repeating task.
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
$env:GIT_SSH_COMMAND = 'ssh -oBatchMode=yes -oConnectTimeout=15'
Push-Location -LiteralPath $Root
try {
    & git ls-remote --exit-code origin HEAD | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Configure Git credentials once before installing the task.' }
    & $Python (Join-Path $Root 'verify.py') --xray $Xray
    if ($LASTEXITCODE -ne 0) { throw 'Actual Xray verification failed; task not installed.' }
} finally { Pop-Location }
$Local = Join-Path $Root '.local'
New-Item -ItemType Directory -Force -Path $Local | Out-Null
@{ python=$Python; xray=$Xray } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Local 'task-config.json') -Encoding UTF8
# Allow AC wake timers. Do not change battery policy, sleep timeout, or force a suspend.
& powercfg.exe /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
if ($LASTEXITCODE -ne 0) { throw 'Could not enable AC wake timers.' }
& powercfg.exe /SETACTIVE SCHEME_CURRENT
if ($LASTEXITCODE -ne 0) { throw 'Could not activate power plan.' }
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "' + (Join-Path $PSScriptRoot 'run-unattended.ps1') + '"') -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 30)
$Settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)
# Password is requested ONCE and given directly to Windows Task Scheduler, never written to disk.
# S4U is deliberately avoided: it cannot supply normal network credentials for Git.
$Credential = Get-Credential -UserName $Identity.Name -Message 'One-time setup: Windows account PASSWORD (not PIN). Windows stores it for runs while signed out.'
if (-not $Credential) { throw 'Credential entry cancelled; no task installed.' }
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -RunLevel Limited -Description 'Wake on AC every 30 minutes; recheck live nodes; discover every four hours; real Xray E2E before push.' -Force | Out-Null
Write-Host 'Installed. Tests run without you present, including waking from supported sleep on AC.'
Write-Host 'Windows returns to its normal idle-sleep policy when testing ends. Shutdown cannot be woken by this task.'
Write-Host 'Check scripts\check-task.ps1, .local\logs, and powercfg /waketimers.'
