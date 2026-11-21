# G2C run-approvals chat alert sender: Windows Task Scheduler installer
# (Marcin-only install; nothing in this repo registers or starts anything by itself).
#
# Fill in the two placeholder values below, review the script, then run it from
# a regular (non-admin) PowerShell window in the repo checkout:
#
#   powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1
#
# It creates a per-user scheduled task "G2C.RunalertsSender" that fires
# every minute while you are logged on and runs the
# resolved interpreter (pythonw.exe preferred, see below) with
# <sender.py> --log-file <log> --ticks 4 --tick-interval 15: four sender
# ticks 15 seconds apart inside that one minute. Task Scheduler rejects any
# sub-minute repetition interval (PT15S: the minimum is PT1M), so the
# 15-second cadence lives in the multi-tick run, not the trigger; the macOS
# launchd timer keeps firing its single tick every 15 seconds.
#
# The trigger pair is a one-minute repetition plus a logon trigger scoped to
# the current user: -AtLogOn -User <DOMAIN\user>, computed once from
# USERDOMAIN and USERNAME (when USERDOMAIN is empty the bare user name is
# used). A bare -AtLogOn trigger means "any user logs on", and registering
# that needs an elevated session; the per-user form registers fine from a
# regular, non-elevated PowerShell window.
#
# The run is silent: the task is registered Hidden (Task Scheduler UI
# visibility only) and the registered interpreter is pythonw.exe whenever
# pythonw.exe sits next to the filled python.exe (a filled pythonw.exe path is
# used as-is, any other interpreter passes through), because pythonw.exe runs
# without a console window. When pythonw.exe is missing the installer falls
# back to the filled python.exe and prints a warning that a console window
# may appear each minute. The task runs as the logged-on user, not elevated,
# and holds no credentials.
#
# Failure behavior: Register-ScheduledTask and Start-ScheduledTask run with
# -ErrorAction Stop inside try/catch. A failure prints the failing step and
# the exception message, prints no success line and exits non-zero: exit
# code 3 for a register failure, exit code 4 for a start failure. An
# "Access is denied" from Register-ScheduledTask usually means an any-user
# logon trigger or another elevated-only setting; when registration
# succeeded but the start failed, the message says the task IS registered
# and names the start and remove commands. After a register failure nothing
# was registered (a name-already-exists failure wants -Uninstall first) and
# a re-run after fixing the cause completes the install.
# The webhook file stays at
# %APPDATA%\G2C\g2c-grokbot-webhook.json (owner-only ACL; see
# runalerts/README.md Step 3) and the ledger path comes from the
# G2C_RUNALERTS_LEDGER user environment variable.
#
# Dry run first: add -DryRun to report every action the script would take
# and change nothing - no task registration, removal or start, no schtasks,
# no directory creation, no file writes:
#   powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -DryRun
# Every report line carries a "DRY RUN:" prefix: the placeholder check
# (unfilled placeholders are reported and the run exits non-zero instead of
# throwing), path existence, the interpreter resolution (pythonw.exe chosen
# when it sits next to the filled python.exe, the python.exe fallback
# warning when it does not), the G2C_RUNALERTS_LEDGER user-variable check,
# the log directory it would create, the exact task name, the action
# (executable plus full argument string), the triggers (the logon trigger
# names its user), the settings (Hidden,
# IgnoreNew, 55-second execution time limit) and that it would register and
# start the task. With -Uninstall -DryRun it reports which of the two task
# names are present and would be removed (a read-only check; when the
# ScheduledTasks cmdlets are unavailable, e.g. on non-Windows hosts, it says
# so and lists both names as would-be checked). The run exits non-zero only
# for unfilled placeholders; every other cannot-verify case is reported and
# leaves the exit code at 0.
# Filling the two placeholders below and pinning the sender.py copy next to
# it are manual steps the dry run validates; the script itself copies no
# files and replaces no placeholders.
#
# Uninstall (removes the task only; ledger, webhook file and logs stay):
#   powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -Uninstall
#
# Installs made with an older script registered the task under this
# legacy name "ComMdG2c.RunalertsSender"; those existing installs need no
# change and the -Uninstall run below removes both names (the registered
# one first). Re-running the installer registers the new name while the
# legacy task keeps running alongside. To migrate such an install, run
# -Uninstall first (it removes both names, including a freshly registered
# new one), then re-run the installer, then check that exactly one task
# G2C.RunalertsSender exists.
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
#
# Both placeholder lines are filled by hand and the sender.py copy is pinned
# by hand: the script performs no file copies and no placeholder replacement;
# the -DryRun run validates that the filled paths exist.

param(
    [switch]$Uninstall,
    [switch]$DryRun
)

# Every dry-run report line goes through this single helper so a dry-run
# transcript can be told apart from a real run's output at a glance.
function Write-DryRun([string]$Message) {
    Write-Output "DRY RUN: $Message"
}

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
    if ($DryRun) {
        # Read-only report: names and presence only, nothing is unregistered
        # or deleted. Get-Command probes for the ScheduledTasks cmdlets
        # instead of calling them, so a host without the module (pwsh on
        # Linux or macOS) gets a plain report naming both task names.
        if ($null -eq (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
            Write-DryRun "the ScheduledTasks cmdlets are not available on this host, so task presence cannot be read here; the real uninstall would check both names and remove whichever task is present:"
            Write-DryRun "would check, and remove if present: $TaskName"
            Write-DryRun "would check, and remove if present: $LegacyTaskName"
            exit 0
        }
        foreach ($task in @($TaskName, $LegacyTaskName)) {
            if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
                Write-DryRun "task $task is present and would be removed (the same Unregister-ScheduledTask then schtasks /Delete /F fallback per name as a real uninstall)"
            } else {
                Write-DryRun "task $task is not present: nothing to remove"
            }
        }
        exit 0
    }
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

# Unfilled placeholders are the one condition a dry run fails on: they are a
# pure file-content mistake, while every other cannot-verify case is reported
# and leaves the exit code at 0.
$DryRunPlaceholderProblem = $false
foreach ($path in @($SenderPython, $SenderScript)) {
    if ($path -like "__*__") {
        if ($DryRun) {
            Write-DryRun "placeholder not filled: $path (edit the placeholder values in this script first, see the header)"
            $DryRunPlaceholderProblem = $true
        } else {
            throw "Edit the placeholder values in this script first (see the header)"
        }
        continue
    }
    if (-not (Test-Path -LiteralPath $path)) {
        if ($DryRun) {
            Write-DryRun "path not found: $path"
        } else {
            throw "Not found: $path"
        }
    } elseif ($DryRun) {
        Write-DryRun "path exists: $path"
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
        if ($DryRun) {
            Write-DryRun "would choose pythonw.exe so the scheduled run opens no console window: $PythonwPath"
        } else {
            Write-Output "Using pythonw.exe so the scheduled run opens no console window: $PythonwPath"
        }
    } else {
        if ($DryRun) {
            Write-DryRun "pythonw.exe not found next to the filled python.exe: the real run would register python.exe and warn that a console window may appear each minute"
        } else {
            Write-Output "pythonw.exe not found next to the filled python.exe: a console window may appear each minute"
        }
    }
}

# The User environment-variable scope exists on Windows only; reading it
# throws on hosts without one (pwsh on Linux or macOS). A dry run there
# reports that the check cannot run locally instead of crashing mid-report;
# a real run keeps refusing fail-closed.
$LedgerValue = $null
$LedgerUserScopeReadable = $true
# The user-scope read lives in one function so the unittest stub harness can
# shadow it (the harness pre-defines the same function name; the script then
# keeps its own definition): an inline .NET static call cannot be stubbed
# from PowerShell, and the stub-driven real-run tests must pass this gate on
# hosts without a User scope. On every real host the behavior is identical
# to the inline read.
if (-not (Get-Command Get-G2CLedgerUserValue -ErrorAction SilentlyContinue)) {
    function Get-G2CLedgerUserValue {
        [Environment]::GetEnvironmentVariable("G2C_RUNALERTS_LEDGER", "User")
    }
}
try {
    $LedgerValue = Get-G2CLedgerUserValue
} catch {
    $LedgerUserScopeReadable = $false
}
if ($null -eq $LedgerValue) {
    if ($DryRun) {
        if ($LedgerUserScopeReadable) {
            Write-DryRun "the user environment variable G2C_RUNALERTS_LEDGER is not set: the real run would stop (setx G2C_RUNALERTS_LEDGER ""C:\path\to\alerts.jsonl"")"
        } else {
            Write-DryRun "the G2C_RUNALERTS_LEDGER user-variable check needs the Windows user environment-variable scope, which this host does not provide: the real run checks it on Windows"
        }
    } else {
        throw "Set the user environment variable G2C_RUNALERTS_LEDGER first: setx G2C_RUNALERTS_LEDGER ""C:\path\to\alerts.jsonl"""
    }
} elseif ($DryRun) {
    Write-DryRun "the G2C_RUNALERTS_LEDGER user environment variable is set"
}

$NewDirectory = Split-Path -Parent $LogPath
if (-not (Test-Path -LiteralPath $NewDirectory)) {
    if ($DryRun) {
        Write-DryRun "the log directory would be created: $NewDirectory"
    } else {
        New-Item -ItemType Directory -Path $NewDirectory | Out-Null
    }
} elseif ($DryRun) {
    Write-DryRun "the log directory exists: $NewDirectory"
}

# The logon trigger's user, computed once and reused below (dry-run report
# and the real trigger): see the header note. USERDOMAIN is normally
# populated on Windows; when it is missing the bare USERNAME scopes the
# trigger, and when both are unavailable the run reports it or refuses
# instead of building an un-scoped trigger.
$LogonUser = $env:USERNAME
if (-not [string]::IsNullOrEmpty($env:USERDOMAIN)) {
    $LogonUser = "$env:USERDOMAIN\$env:USERNAME"
}

if ($DryRun) {
    # Everything below builds the task objects and registers and starts the
    # task; the report above is the complete preview, so the dry run exits
    # here without running any of it.
    Write-DryRun "task name: $TaskName"
    Write-DryRun ("action: " + $SenderPythonResolved + " " + '"' + $SenderScript +
                  '" --log-file "' + $LogPath + '" --ticks 4 --tick-interval 15')
    if ([string]::IsNullOrEmpty($LogonUser)) {
        Write-DryRun "triggers: fire once starting now, then repeat every 1 minute for 3650 days, plus fire once at logon of the current user (the user name could not be read on this host)"
    } else {
        Write-DryRun "triggers: fire once starting now, then repeat every 1 minute for 3650 days, plus fire once at logon of $LogonUser"
    }
    Write-DryRun "settings: Hidden, IgnoreNew (no overlapping instances), execution time limit 55 seconds, start and keep running on battery power"
    Write-DryRun "the real run would now register the scheduled task $TaskName (dry run: nothing registered)"
    Write-DryRun "the real run would now start the scheduled task $TaskName (dry run: nothing started)"
    if ($DryRunPlaceholderProblem) {
        Write-DryRun "one or more placeholders are still unfilled (see the report above): exit code 1"
        exit 1
    }
    Write-DryRun "nothing was registered, started, removed, created or written"
    exit 0
}

$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
              -RepetitionInterval (New-TimeSpan -Minutes 1) `
              -RepetitionDuration (New-TimeSpan -Days 3650)
# The logon trigger is scoped to the current user (see the $LogonUser note
# above): the any-user trigger form needs elevation to register.
if ([string]::IsNullOrEmpty($LogonUser)) {
    throw "The user name could not be read (USERDOMAIN and USERNAME are both empty): cannot scope the logon trigger to the current user"
}
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $LogonUser
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

# Fail loudly: a failed registration or start must never masquerade as a
# successful install. Every failure below prints the failing step and the
# exception and exits non-zero BEFORE any success line is printed; the four
# success lines are only reached when both the registration and the start
# succeeded. Exit codes: 3 register failure, 4 start failure (0 success,
# 1 unfilled placeholders or the uninstall fallback failure).
try {
    Register-ScheduledTask -TaskName $TaskName -Trigger $trigger, $logonTrigger `
        -Settings $settings -Action $action -Description `
        "G2C run-approvals chat alert sender (send-only, no credentials, human settles approvals on the phone page)" `
        -ErrorAction Stop | Out-Null
} catch {
    Write-Output "Register-ScheduledTask failed: $($_.Exception.Message)"
    if ($_.Exception.Message -match "Access is denied") {
        Write-Output "Hint: Access is denied from Register-ScheduledTask usually means the task definition carries an any-user logon trigger or another elevated-only setting; this installer scopes its logon trigger to the current user ($LogonUser), so check the task definition this run is registering"
    }
    exit 3
}
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Output "Start-ScheduledTask failed: $($_.Exception.Message)"
    Write-Output "The task IS registered as $TaskName but not started: start it with Start-ScheduledTask -TaskName $TaskName, or remove it with Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false (or schtasks /Delete /TN $TaskName /F) and re-run the installer after fixing the cause"
    exit 4
}
Write-Output "Registered and started scheduled task $TaskName (registered executable: $SenderPythonResolved)"
Write-Output "Four 15-second-apart sender ticks every minute while logged on; the task is Hidden in the Task Scheduler UI"
Write-Output "Log file: $LogPath"
Write-Output "First tick behavior: missing webhook file is a clean exit; check the log for 'sent key' after a waiter fires"
