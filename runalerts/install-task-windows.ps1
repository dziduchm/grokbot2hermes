# G2C run-approvals chat alert sender: Windows Task Scheduler installer
# (Marcin-only install; nothing in this repo registers or starts anything by itself).
#
# Fill in the two placeholder values below, review the script, then run it from
# a regular (non-admin) PowerShell window in the repo checkout:
#
#   powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1
#
# It creates a per-user scheduled task "G2C.RunalertsSender" that fires
# every minute while you are logged on (starting on logon) and runs the
# resolved interpreter (pythonw.exe preferred, see below) with
# <sender.py> --log-file <log> --ticks 4 --tick-interval 15: four sender
# ticks 15 seconds apart inside that one minute. Task Scheduler rejects any
# sub-minute repetition interval (PT15S: the minimum is PT1M), so the
# 15-second cadence lives in the multi-tick run, not the trigger; the macOS
# launchd timer keeps firing its single tick every 15 seconds.
#
# The run is silent: the task is registered Hidden (Task Scheduler UI
# visibility only) and the registered interpreter is pythonw.exe whenever
# pythonw.exe sits next to the filled python.exe (a filled pythonw.exe path is
# used as-is, any other interpreter passes through), because pythonw.exe runs
# without a console window. When pythonw.exe is missing the installer falls
# back to the filled python.exe and prints a warning that a console window
# may appear each minute. The task runs as the logged-on user, not elevated,
# and holds no credentials. The webhook file stays at
# %APPDATA%\G2C\g2c-grokbot-webhook.json (owner-only ACL; see
# runalerts/README.md Step 3) and the ledger path comes from the
# G2C_RUNALERTS_LEDGER user environment variable.
#
# Uninstall (removes the task only; ledger, webhook file and logs stay):
#   powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -Uninstall
#
# Installs made with an older script registered the task under this
# legacy name "ComMdG2c.RunalertsSender"; those existing installs need no
# change and the -Uninstall run below removes both names (the registered
# one first). Re-running the installer registers the new name while the
# legacy task keeps running alongside until it is removed.
#
# If Unregister-ScheduledTask refuses with Access Denied (seen in a filtered
# admin / Medium IL session), delete the same task with:
#   schtasks /Delete /TN G2C.RunalertsSender /F
# (for an install made with an older script the manual fallback names
# the legacy name /TN ComMdG2c.RunalertsSender instead)
#
# Placeholders:
#   $SenderPython : e.g. "C:\Users\me\AppData\Local\Programs\Python\Python39\python.exe"
#                   (a pythonw.exe path works too; the installer prefers the
#                   pythonw.exe sitting next to the filled python.exe)
#   $SenderScript : e.g. "C:\Users\me\src\GrokBot2Hermes\runalerts\sender.py"

param(
    [switch]$Uninstall
)

$TaskName = "G2C.RunalertsSender"
# Installs made with an older script registered the task under this legacy
# name; the -Uninstall block below removes it too (see the header note).
$LegacyTaskName = "ComMdG2c.RunalertsSender"

# ---- placeholders (edit me) -------------------------------------------------
$SenderPython = "__SENDER_PYTHON__"
$SenderScript = "__SENDER_SCRIPT__"
$LogPath      = Join-Path $env:APPDATA "G2C\g2c-runalerts-sender.log"
# -----------------------------------------------------------------------------

if ($Uninstall) {
    foreach ($task in @($TaskName, $LegacyTaskName)) {
        if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
            try {
                Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction Stop
                Write-Output "Removed scheduled task $task"
            } catch {
                # Unregister-ScheduledTask is refused with Access Denied in a
                # filtered admin / Medium IL session; schtasks deletes the same
                # task from the same session.
                Write-Output "Unregister-ScheduledTask failed: $($_.Exception.Message)"
                Write-Output "Falling back to: schtasks /Delete /TN $task /F"
                & schtasks.exe /Delete /TN $task /F
                if ($LASTEXITCODE -eq 0) {
                    Write-Output "Removed scheduled task $task (schtasks fallback)"
                } else {
                    Write-Output "The schtasks fallback also failed (exit code $LASTEXITCODE); remove the task manually or retry the -Uninstall script"
                    exit 1
                }
            }
        } else {
            Write-Output "Task $task not present: nothing to remove"
        }
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

# Silence preference: pythonw.exe runs without opening a console window,
# python.exe opens one every minute. When the filled interpreter is python.exe,
# the sibling pythonw.exe in the same directory wins; a filled pythonw.exe path
# is used as-is (any other interpreter name passes through untouched). Only
# when pythonw.exe is missing does the task fall back to the filled python.exe,
# with a warning about the console window it may open each minute.
$PythonLeaf = Split-Path -Leaf $SenderPython
$SenderPythonResolved = $SenderPython
if ($PythonLeaf -eq "python.exe") {
    $PythonwPath = Join-Path (Split-Path -Parent $SenderPython) "pythonw.exe"
    if (Test-Path -LiteralPath $PythonwPath) {
        $SenderPythonResolved = $PythonwPath
        Write-Output "Using pythonw.exe so the scheduled run opens no console window: $PythonwPath"
    } else {
        Write-Output "pythonw.exe not found next to the filled python.exe: a console window may appear each minute"
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
# Hidden only hides the task in the Task Scheduler UI tree; pythonw.exe is what
# actually suppresses the console window. The two together keep the task from
# popping a console over every desktop window each minute.
$settings.Hidden = $true
$action   = New-ScheduledTaskAction -Execute $SenderPythonResolved `
              -Argument ('"' + $SenderScript + '" --log-file "' + $LogPath +
                         '" --ticks 4 --tick-interval 15')

Register-ScheduledTask -TaskName $TaskName -Trigger $trigger, $logonTrigger `
    -Settings $settings -Action $action -Description `
    "G2C run-approvals chat alert sender (send-only, no credentials, human settles approvals on the phone page)" | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered and started scheduled task $TaskName (registered executable: $SenderPythonResolved)"
Write-Output "Four 15-second-apart sender ticks every minute while logged on; the task is Hidden in the Task Scheduler UI"
Write-Output "Log file: $LogPath"
Write-Output "First tick behavior: missing webhook file is a clean exit; check the log for 'sent key' after a waiter fires"
