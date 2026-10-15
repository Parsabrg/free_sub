param([string]$TaskName = 'FreeXraySubscription')
$ErrorActionPreference = 'Stop'
$Task = Get-ScheduledTask -TaskName $TaskName
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
[PSCustomObject]@{Task=$Task.TaskName; State=$Task.State; WakeToRun=$Task.Settings.WakeToRun; LogonType=$Task.Principal.LogonType; LastRun=$Info.LastRunTime; NextRun=$Info.NextRunTime; LastResult=$Info.LastTaskResult} | Format-List
powercfg.exe /AVAILABLESLEEPSTATES
powercfg.exe /WAKETIMERS
