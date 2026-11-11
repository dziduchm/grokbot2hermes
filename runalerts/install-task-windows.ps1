# G2C run-approvals chat alert sender: Windows Task Scheduler installer
# (Marcin-only install; nothing in this repo registers or starts anything by itself).
#
# Fill in the two placeholder values below, review the script, then run it from
# a regular (non-admin) PowerShell window in the repo checkout:
#
#   powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1
#
# It creates a per-user scheduled task "ComMdG2c.RunalertsSender" that fires
# every minute while you are logged on (starting on logon) and runs
# <python.exe> <sender.py> --log-file <log> --ticks 4 --tick-interval 15:
# four sender ticks 15 seconds apart inside that one minute. Task Scheduler
# rejects any sub-minute repetition interval (PT15S: the minimum is PT1M),
# so the 15-second cadence lives in the multi-tick run, not the trigger; the
# macOS launchd timer keeps firing its single tick every 15 seconds. It never
# runs hidden, never elevated, and holds no credentials. The webhook file
# stays at %APPDATA%\G2C\g2c-grokbot-webhook.json
# (owner-only ACL; see runalerts/README.md Step C) and the ledger path comes
# from the G2C_RUNALERTS_LEDGER user environment variable.
#
# Uninstall (removes the task only; ledger, webhook file and logs stay):
#   powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -Uninstall
#
# Placeholders:
#   $SenderPython : e.g. "C:\Users\me\AppData\Local\Programs\Python\Python39\python.exe"
#   $SenderScript : e.g. "C:\Users\me\src\GrokBot2Hermes\runalerts\sender.py"

param(
    [switch]$Uninstall
)

$TaskName = "ComMdG2c.RunalertsSender"

# ---- placeholders (edit me) -------------------------------------------------
$SenderPython = "__SENDER_PYTHON__"
$SenderScript = "__SENDER_SCRIPT__"
$LogPath      = Join-Path $env:APPDATA "G2C\g2c-runalerts-sender.log"
# -----------------------------------------------------------------------------

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task $TaskName"
    } else {
        Write-Output "Task $TaskName not present: nothing to remove"
    }
    exit 0
}

foreach ($path in @($SenderPython, $SenderScript)) {
    if ($path -like "__*__") {
        throw "Edit the placeholder values in this script first (see the header)"
    }
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Not found: $path"
    }
}

if ([Environment]::GetEnvironmentVariable("G2C_RUNALERTS_LEDGER", "User") -eq $null) {
    throw "Set the user environment variable G2C_RUNALERTS_LEDGER first: setx G2C_RUNALERTS_LEDGER ""C:\path\to\alerts.jsonl"""
}

$NewDirectory = Split-Path -Parent $LogPath
if (-not (Test-Path -LiteralPath $NewDirectory)) {
    New-Item -ItemType Directory -Path $NewDirectory | Out-Null
}

$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
              -RepetitionInterval (New-TimeSpan -Minutes 1) `
              -RepetitionDuration (New-TimeSpan -Days 3650)
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
# ExecutionTimeLimit stays strictly under the 1-minute repetition: a stuck run
# is stopped before the next firing, and IgnoreNew forbids any overlap.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit (New-TimeSpan -Seconds 55) -MultipleInstances IgnoreNew
$action   = New-ScheduledTaskAction -Execute $SenderPython `
              -Argument ('"' + $SenderScript + '" --log-file "' + $LogPath +
                         '" --ticks 4 --tick-interval 15')

Register-ScheduledTask -TaskName $TaskName -Trigger $trigger, $logonTrigger `
    -Settings $settings -Action $action -Description `
    "G2C run-approvals chat alert sender (send-only, no credentials, human settles approvals on the phone page)" | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started scheduled task $TaskName (four 15-second-apart ticks every minute while logged on)"
Write-Output "Log file: $LogPath"
Write-Output "First tick behavior: missing webhook file is a clean exit; check the log for 'sent key' after a waiter fires"
