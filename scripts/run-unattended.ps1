# Runs as the task owner's account; no dialogs, prompts or forced sleep.
$ErrorActionPreference = 'Stop'
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
# Wake-up Wi-Fi may need time to reconnect. Bounded retries need no human input.
$Ready = $false
for ($Attempt = 0; $Attempt -lt 12; $Attempt++) {
    if (Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.IPv4Connectivity -eq 'Internet' -or $_.IPv6Connectivity -eq 'Internet' }) { $Ready = $true; break }
    Start-Sleep -Seconds 10
}
# Some filtered networks never report Internet connectivity. Let actual source/proxy tests decide.
& $Config.python (Join-Path $Root 'main.py') --xray $Config.xray --mode auto --publish *>> $Log
$RunCode = $LASTEXITCODE
exit $RunCode
