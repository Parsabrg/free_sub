# Runs as the task owner's account; no dialogs, prompts or forced sleep.
$ErrorActionPreference = 'Stop'
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'
$Root = Split-Path -Parent $PSScriptRoot
$Config = Get-Content -LiteralPath (Join-Path $Root '.local\task-config.json') -Raw | ConvertFrom-Json
$env:GIT_TERMINAL_PROMPT = '0'
$env:GCM_INTERACTIVE = 'never'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -LiteralPath $Root
$Logs = Join-Path $Root '.local\logs'
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Get-ChildItem -LiteralPath $Logs -Filter '*.log' | Where-Object LastWriteTime -lt (Get-Date).AddDays(-14) | Remove-Item -Force
$Log = Join-Path $Logs ((Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
('START {0:o} user={1}' -f (Get-Date), [Security.Principal.WindowsIdentity]::GetCurrent().Name) | Out-File -LiteralPath $Log
# Wake-up Wi-Fi may need time to reconnect. Bounded retries need no human input.
$Ready = $false
for ($Attempt = 0; $Attempt -lt 12; $Attempt++) {
    if (Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.IPv4Connectivity -eq 'Internet' -or $_.IPv6Connectivity -eq 'Internet' }) { $Ready = $true; break }
    Start-Sleep -Seconds 10
}
# Some filtered networks never report Internet connectivity. Let actual source/proxy tests decide.
# Windows PowerShell 5 treats native stderr as a terminating error under Stop.
$ErrorActionPreference = 'Continue'
& $Config.python (Join-Path $Root 'main.py') --xray $Config.xray --mode auto --publish *>> $Log
$RunCode = $LASTEXITCODE
('END {0:o} exit={1}' -f (Get-Date), $RunCode) | Out-File -LiteralPath $Log -Append
exit $RunCode
