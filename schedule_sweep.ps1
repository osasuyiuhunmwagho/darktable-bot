# Registers sweep.py in Windows Task Scheduler: every hour (and at log-on) while the laptop is on.
# It never wakes the laptop; a check missed while it was off or asleep runs as soon as it's back on.
# Runs on battery too, with no window (pythonw). Output goes to work\sweep.log.
#
#   powershell -ExecutionPolicy Bypass -File schedule_sweep.ps1            # every hour
#   powershell -ExecutionPolicy Bypass -File schedule_sweep.ps1 -Hours 2   # every 2 hours
#   Unregister-ScheduledTask -TaskName "RealVictors edit sweep"            # remove it

param([int]$Hours = 1)

$taskName = "RealVictors edit sweep"
$pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "No virtualenv at $pythonw. Set up .venv first (see README)." }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "sweep.py --log" -WorkingDirectory $PSScriptRoot
$triggers = @(
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours $Hours)),
    (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Edits new Real Victors shoots in Drive with darktable (sweep.py)." -Force | Out-Null
Write-Host "Scheduled '$taskName' every $Hours hour(s). Log: $(Join-Path $PSScriptRoot 'work\sweep.log')"
