"""Tests for runalerts/install-task-windows.ps1's -DryRun switch and real run.

Three layers:

- InstallTaskWindowsDryRunTest drives the real script in a temporary filled
  copy under a stub harness: stub functions named New-Item,
  Register-ScheduledTask, Unregister-ScheduledTask, Start-ScheduledTask,
  Get-ScheduledTask, New-ScheduledTaskTrigger, New-ScheduledTaskSettingsSet,
  New-ScheduledTaskAction, schtasks.exe and schtasks shadow the real
  commands and append to a marker
  file, so the test proves -DryRun changes nothing (empty marker, no log
  directory created) while reporting the expected DRY RUN lines. The stub
  must be named schtasks.exe because PowerShell resolves commands by exact
  name and the script's uninstall fallback calls & schtasks.exe; the
  negative control test runs a scratch child script under the harness that
  calls & schtasks.exe /Query and asserts the marker line fires, proving
  the stub owns that token and the real exe never runs. These tests skip
  without a
  PowerShell host: pwsh anywhere, or powershell.exe on Windows. No real
  scheduled task, launchctl agent or machine state is ever touched: the
  script sees only the temporary directory (overridden APPDATA).
- InstallTaskWindowsRealRunTest drives the real script's real (non-dry-run)
  path through the same stub harness plus env knobs the harness stubs read
  (ledger value, per-stub failure modes, task presence for uninstall). The
  user-scope ledger read is a harness-shadowable function seam in the
  script (Get-G2CLedgerUserValue): an inline .NET static call could not be
  stubbed from PowerShell, and the stub-driven real-run tests must pass
  this gate on hosts without a User environment-variable scope. The
  harness normalizes the child's exit code to 0/1 (see the harness tail
  comment), so these tests assert non-zero exits dynamically and the
  distinct exit codes are pinned on the script text by
  InstallTaskWindowsScriptTextTest. Every machine boundary is stubbed, so
  these tests stay inert on every host.
- InstallTaskWindowsScriptTextTest and InstallTaskWindowsDryRunDocsTest
  are pure file reads (run everywhere, pwsh not needed) pinning the
  script's own text (the user-scoped logon trigger, -ErrorAction Stop, the
  exit codes, success lines only after both steps) and the header, README
  and CHANGELOG documentation of the switch, the manual fill-and-pin
  steps, the legacy-name migration order and the failure behavior.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(REPO_ROOT, "runalerts", "install-task-windows.ps1")

TASK_NAME = "G2C.RunalertsSender"
LEGACY_TASK_NAME = "ComMdG2c.RunalertsSender"

# pwsh 7 and Windows PowerShell 5.1 both take the call operator's child-script
# exit code differently enough that the harness normalizes it: after the child
# runs, the harness exits 0 on pipeline success ($?) and 1 otherwise, which
# matches the child's exit 0/exit 1 under either nesting behavior. A deeper
# consequence: the child's exact exit code is not observable through this
# harness (the tail collapses it to 0/1), so the real-run tests assert
# non-zero dynamically and the distinct exit codes are pinned on the script
# text by InstallTaskWindowsScriptTextTest.
STUB_HARNESS = """\
param(
    [switch]$DryRun,
    [switch]$Uninstall
)

function New-Item {
    param([Parameter(Position = 0)]$ItemType, $Path)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "New-Item $ItemType $Path"
    return $null
}

# The script reads the user-scope ledger value through Get-G2CLedgerUserValue
# (its harness-shadowable function seam: the script defines that function
# only when no caller already did, so this pre-definition wins). The stub
# supplies G2C_STUB_LEDGER when set and throws otherwise, mimicking a host
# without a User environment-variable scope (pwsh on Linux or macOS) so the
# dry-run wording matches that host.
function Get-G2CLedgerUserValue {
    if (-not $env:G2C_STUB_LEDGER) {
        throw "Get-G2CLedgerUserValue stub: no User-scope value available (set G2C_STUB_LEDGER to supply one)"
    }
    return $env:G2C_STUB_LEDGER
}

# Switch-shaped parameters are typed [switch] in the stubs: an untyped
# parameter under [CmdletBinding()] makes the script's bare -Once/-AtLogOn
# style flags fail parameter binding ("Missing an argument for parameter"),
# while [switch] binds a bare flag exactly like the real cmdlet does.
function New-ScheduledTaskTrigger {
    [CmdletBinding()] param([switch]$Once, $At, $RepetitionInterval, $RepetitionDuration, [switch]$AtLogOn, $User)
    $shape = "New-ScheduledTaskTrigger"
    if ($Once) { $shape = "$shape once=True" }
    if ($AtLogOn) { $shape = "$shape logon=True" }
    $shape = "$shape user=$User"
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value $shape
    return [pscustomobject]@{ Once = [bool]$Once; AtLogOn = [bool]$AtLogOn; User = $User }
}

function New-ScheduledTaskSettingsSet {
    [CmdletBinding()] param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit, $MultipleInstances)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "New-ScheduledTaskSettingsSet"
    return [pscustomobject]@{ Hidden = $false }
}

function New-ScheduledTaskAction {
    [CmdletBinding()] param($Execute, $Argument)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "New-ScheduledTaskAction $Execute"
    return [pscustomobject]@{ Execute = $Execute; Argument = $Argument }
}

# Task presence for the uninstall paths (the real Get-ScheduledTask is
# Windows-only and reads machine state): G2C_STUB_TASK_PRESENT=absent
# reports no task present, anything else (including unset) reports the
# queried name present. No marker on purpose: the dry-run tests assert the
# marker stays empty.
function Get-ScheduledTask {
    [CmdletBinding()] param($TaskName)
    if ($env:G2C_STUB_TASK_PRESENT -eq "absent") { return $null }
    return [pscustomobject]@{ TaskName = $TaskName }
}

# -ErrorAction Stop (the script's calls carry it) only binds once the stubs
# are advanced functions ([CmdletBinding()] brings the common parameters),
# and G2C_STUB_*_FAIL turns a stub into the Access-denied/failure shape the
# failure-path tests drive.
function Register-ScheduledTask {
    [CmdletBinding()] param($TaskName, $Trigger, $Settings, $Action, $Description)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value ("Register-ScheduledTask " + $TaskName + " triggers=" + @($Trigger).Count)
    if ($env:G2C_STUB_REGISTER_FAIL) {
        throw "Access is denied. (Exception from HRESULT: 0x80070005 (E_ACCESSDENIED))"
    }
}

function Unregister-ScheduledTask {
    [CmdletBinding()] param($TaskName, $Confirm)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "Unregister-ScheduledTask $TaskName"
    if ($env:G2C_STUB_UNREGISTER_FAIL) {
        throw "Access is denied. (Exception from HRESULT: 0x80070005 (E_ACCESSDENIED))"
    }
}

function Start-ScheduledTask {
    [CmdletBinding()] param($TaskName)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "Start-ScheduledTask $TaskName"
    if ($env:G2C_STUB_START_FAIL) {
        throw "Start-ScheduledTask : The scheduled task $TaskName failed to start"
    }
}

# PowerShell resolves commands by exact name: the uninstall fallback calls
# & schtasks.exe, so the stub must carry the .exe suffix to own that token
# (a function named schtasks never intercepts it). ValueFromRemainingArguments
# binds the script's four-argument fallback call (& schtasks.exe /Delete /TN
# $task /F), which a single positional parameter would refuse. The stub
# publishes its simulated exit code through $global:LASTEXITCODE because the
# script checks $LASTEXITCODE after the call (a function stub does not set it
# the way the real exe would).
function schtasks.exe {
    [CmdletBinding()] param([Parameter(ValueFromRemainingArguments = $true)]$ArgumentList)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "schtasks.exe $ArgumentList"
    if ($env:G2C_STUB_SCHTASKS_FAIL) { $global:LASTEXITCODE = 1 } else { $global:LASTEXITCODE = 0 }
}

function schtasks {
    param($ArgumentList)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "schtasks $ArgumentList"
}

& (Join-Path $PSScriptRoot "install-task-windows.filled.ps1") -DryRun:$DryRun -Uninstall:$Uninstall
if ($?) { exit 0 } else { exit 1 }
"""

# Negative control for mitch-dryrun-review.md LOW 1: the stub names the exact
# token the script's uninstall fallback calls (& schtasks.exe), and a scratch
# child script under the harness makes that exact call. Only the stub can
# write the marker line, so the marker firing proves the function owns the
# .exe token and the real exe never ran; a stub-name drift would leave the
# marker empty (real exe on Windows, command-not-found elsewhere) and the
# test would go red on the marker and the exit code instead.
NEGCTL_SCRATCH = """\\
& schtasks.exe /Query
"""

NEGCTL_HARNESS = STUB_HARNESS.replace(
    '& (Join-Path $PSScriptRoot "install-task-windows.filled.ps1")'
    ' -DryRun:$DryRun -Uninstall:$Uninstall',
    '& (Join-Path $PSScriptRoot "negctl.scratch.ps1")')


def find_powershell():
    """Prefer pwsh; on Windows fall back to powershell.exe (5.1)."""
    for name in ("pwsh", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return found
    if os.name == "nt":
        for name in ("powershell.exe", "powershell"):
            found = shutil.which(name)
            if found:
                return found
    return None


POWERSHELL = find_powershell()
REQUIRES_POWERSHELL = unittest.skipUnless(
    POWERSHELL,
    "no PowerShell host found (needs pwsh, or powershell.exe on Windows)")


@REQUIRES_POWERSHELL
class InstallTaskWindowsScriptHarness(unittest.TestCase):
    """Shared base for the dry-run and real-run classes: temp directory, a
    filled template copy, the stub harness invocation and marker reads.
    extra_env adds or overrides child env keys; a None value pops the key
    (popping USERDOMAIN exercises the bare-username logon-trigger fallback
    on every host)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="g2c-dryrun-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def write_filled(self, sender_python, sender_script):
        with open(TEMPLATE_PATH, encoding="utf-8") as handle:
            text = handle.read()
        filled = text.replace("__SENDER_PYTHON__", sender_python)
        filled = filled.replace("__SENDER_SCRIPT__", sender_script)
        path = os.path.join(self.tmp, "install-task-windows.filled.ps1")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(filled)
        return path

    def run_script(self, dry_run=True, uninstall=False, extra_env=None):
        harness = os.path.join(self.tmp, "harness.ps1")
        with open(harness, "w", encoding="utf-8", newline="") as handle:
            handle.write(STUB_HARNESS)
        marker = os.path.join(self.tmp, "marker.txt")
        appdata = os.path.join(self.tmp, "appdata")
        os.makedirs(appdata, exist_ok=True)
        env = dict(os.environ)
        env["G2C_DRYRUN_MARKER"] = marker
        env["APPDATA"] = appdata
        if extra_env:
            for key, value in extra_env.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
        args = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", harness]
        if dry_run:
            args.append("-DryRun")
        if uninstall:
            args.append("-Uninstall")
        proc = subprocess.run(args, env=env, capture_output=True, text=True,
                              timeout=600)
        return proc, marker, appdata

    @staticmethod
    def marker_text(marker):
        if not os.path.exists(marker):
            return ""
        with open(marker, "rb") as handle:
            return handle.read().decode("utf-8", errors="replace").strip()

    def make_interpreter(self, with_pythonw):
        python_dir = os.path.join(self.tmp, "python")
        os.makedirs(python_dir)
        python_exe = os.path.join(python_dir, "python.exe")
        with open(python_exe, "w", encoding="utf-8") as handle:
            handle.write("")
        if with_pythonw:
            pythonw_exe = os.path.join(python_dir, "pythonw.exe")
            with open(pythonw_exe, "w", encoding="utf-8") as handle:
                handle.write("")
        return python_exe

    def make_sender(self):
        sender = os.path.join(self.tmp, "sender.py")
        with open(sender, "w", encoding="utf-8") as handle:
            handle.write("# sender copy\n")
        return sender


@REQUIRES_POWERSHELL
class InstallTaskWindowsDryRunTest(InstallTaskWindowsScriptHarness):
    """Drive the real template in a temp filled copy under the stub harness."""

    def test_dry_run_reports_every_action_and_changes_nothing(self):
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, appdata = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout
        self.assertIn("DRY RUN: task name: " + TASK_NAME, out)
        self.assertIn("DRY RUN: would choose pythonw.exe", out)
        self.assertIn("DRY RUN: path exists: " + python_exe, out)
        self.assertIn("DRY RUN: path exists: " + sender, out)
        self.assertIn("G2C_RUNALERTS_LEDGER", out)
        self.assertIn("--log-file", out)
        self.assertIn("--ticks 4 --tick-interval 15", out)
        self.assertIn("DRY RUN: triggers: fire once starting now", out)
        self.assertIn("execution time limit 55 seconds", out)
        self.assertIn("IgnoreNew", out)
        self.assertIn("DRY RUN: the real run would now register the scheduled"
                      " task " + TASK_NAME, out)
        self.assertIn("DRY RUN: the real run would now start the scheduled"
                      " task " + TASK_NAME, out)
        # The real-run success line must never appear in a dry run.
        self.assertNotIn("Registered and started scheduled task", out)
        # No side effects: the stub marker stays empty and the log directory
        # (APPDATA/G2C) is never created.
        self.assertEqual(self.marker_text(marker), "")
        self.assertFalse(os.path.exists(os.path.join(appdata, "G2C")))

    def test_dry_run_log_directory_report_is_host_independent(self):
        # pwsh 7 treats the backslash inside the template's log leaf as a
        # path separator on every host (a real pwsh 7.4.6 run on Linux
        # disproved the "on Unix the leaf is one segment" assumption), so
        # which parent the report names (the existing APPDATA itself or its
        # missing G2C child) must not be predicted from os.name. The
        # portable invariants: the reported directory is the normalized
        # parent of the --log-file path in the action line, the exists
        # versus would-be-created wording matches the directory's real
        # pre-run existence, and the run creates nothing (so checking the
        # post-run state is the pre-run check the wording stands for).
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        appdata = os.path.join(self.tmp, "appdata")
        os.makedirs(appdata, exist_ok=True)
        pre_listing = sorted(os.listdir(appdata))
        proc, marker, appdata = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout

        create_prefix = "DRY RUN: the log directory would be created: "
        exists_prefix = "DRY RUN: the log directory exists: "
        dir_lines = [line for line in out.splitlines()
                     if line.startswith(exists_prefix)
                     or line.startswith(create_prefix)]
        self.assertEqual(len(dir_lines), 1, out)
        if dir_lines[0].startswith(create_prefix):
            wording_exists = False
            reported_dir = dir_lines[0][len(create_prefix):]
        else:
            wording_exists = True
            reported_dir = dir_lines[0][len(exists_prefix):]

        action_lines = [line for line in out.splitlines()
                        if line.startswith("DRY RUN: action: ")]
        self.assertEqual(len(action_lines), 1, out)
        log_marker = '--log-file "'
        start = action_lines[0].index(log_marker) + len(log_marker)
        end = action_lines[0].index('" --ticks 4 --tick-interval 15', start)
        action_log_path = action_lines[0][start:end]

        # The report names exactly the parent of the log file the action
        # line would use, whatever separator the host renders it with.
        self.assertEqual(os.path.normpath(reported_dir),
                         os.path.normpath(os.path.dirname(action_log_path)))
        # The wording matches the directory's real existence; the run
        # created nothing (asserted below), so the post-run isdir here is
        # the pre-run value the wording is judged against.
        self.assertEqual(wording_exists, os.path.isdir(reported_dir))
        # Nothing was created: APPDATA's listing is unchanged, the G2C
        # directory is still not a directory after the run, and no stub
        # fired.
        self.assertEqual(pre_listing, sorted(os.listdir(appdata)))
        self.assertFalse(os.path.isdir(os.path.join(appdata, "G2C")))
        self.assertEqual(self.marker_text(marker), "")

    def test_dry_run_warns_when_pythonw_is_missing(self):
        python_exe = self.make_interpreter(with_pythonw=False)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, _ = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DRY RUN: pythonw.exe not found next to the filled"
                      " python.exe", proc.stdout)
        self.assertIn("console window may appear", proc.stdout)
        self.assertNotIn("Registered and started scheduled task", proc.stdout)
        self.assertEqual(self.marker_text(marker), "")

    def test_dry_run_reports_unfilled_placeholders_and_exits_nonzero(self):
        # The checked-in template with placeholders untouched: the one case
        # where the dry run exits non-zero instead of throwing a bare error.
        self.write_filled("__SENDER_PYTHON__", "__SENDER_SCRIPT__")
        proc, marker, appdata = self.run_script()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DRY RUN: placeholder not filled: __SENDER_PYTHON__",
                      proc.stdout)
        self.assertIn("DRY RUN: placeholder not filled: __SENDER_SCRIPT__",
                      proc.stdout)
        self.assertEqual(self.marker_text(marker), "")
        self.assertFalse(os.path.exists(os.path.join(appdata, "G2C")))

    def test_uninstall_dry_run_lists_both_task_names_and_removes_nothing(self):
        # Placeholders are irrelevant on the uninstall branch; keep the
        # template as checked in. On hosts with the ScheduledTasks cmdlets
        # the presence check is a real read-only Get-ScheduledTask query; on
        # other hosts (pwsh on Linux or macOS) the report says so and lists
        # both names as would-be checked. Either way both names appear, and
        # nothing is removed.
        self.write_filled("__SENDER_PYTHON__", "__SENDER_SCRIPT__")
        proc, marker, _ = self.run_script(dry_run=True, uninstall=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        dryrun_lines = [line for line in proc.stdout.splitlines()
                        if line.startswith("DRY RUN:")]
        for name in (TASK_NAME, LEGACY_TASK_NAME):
            self.assertTrue(
                any(name in line for line in dryrun_lines),
                "missing DRY RUN line naming %s in %r" % (name, dryrun_lines))
        self.assertNotIn("Removed scheduled task", proc.stdout)
        self.assertEqual(self.marker_text(marker), "")

    def test_negative_control_schtasks_stub_owns_the_exe_token(self):
        # The uninstall fallback calls & schtasks.exe and PowerShell resolves
        # commands by exact name, so the stub must be named schtasks.exe.
        # The scratch child script under the harness makes that exact call;
        # the marker line can only come from the stub, so the assertion
        # proves the stub owns the .exe token and the real exe never ran.
        # Without the stub the real spelling fails as command-not-found on a
        # non-Windows host (non-zero exit) and on Windows would leave the
        # marker empty: both shapes go red here.
        scratch = os.path.join(self.tmp, "negctl.scratch.ps1")
        with open(scratch, "w", encoding="utf-8", newline="") as handle:
            handle.write(NEGCTL_SCRATCH)
        harness = os.path.join(self.tmp, "harness-negctl.ps1")
        with open(harness, "w", encoding="utf-8", newline="") as handle:
            handle.write(NEGCTL_HARNESS)
        marker = os.path.join(self.tmp, "marker-negctl.txt")
        env = dict(os.environ)
        env["G2C_DRYRUN_MARKER"] = marker
        args = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", harness]
        proc = subprocess.run(args, env=env, capture_output=True,
                              text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.marker_text(marker), "schtasks.exe /Query")

    def test_dry_run_shows_the_scoped_logon_user(self):
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, _ = self.run_script(extra_env={
            "USERDOMAIN": "TESTDOM", "USERNAME": "dzidu"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DRY RUN: triggers: fire once starting now, then"
                      " repeat every 1 minute for 3650 days, plus fire once"
                      " at logon of TESTDOM\\dzidu", proc.stdout)
        self.assertEqual(self.marker_text(marker), "")

    def test_dry_run_logon_user_falls_back_to_the_bare_username(self):
        # USERDOMAIN missing (popped, so the Windows CI leg sees it gone
        # too): the bare USERNAME scopes the trigger, as documented in the
        # script header and the README.
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, _ = self.run_script(extra_env={
            "USERDOMAIN": None, "USERNAME": "g2ctestuser"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("plus fire once at logon of g2ctestuser", proc.stdout)
        self.assertEqual(self.marker_text(marker), "")

    @unittest.skipIf(sys.platform == "win32",
                     "a child process with an empty USERNAME is unreliable"
                     " on Windows")
    def test_dry_run_without_user_names_reports_the_unavailable_host(self):
        # Both USERDOMAIN and USERNAME gone: the dry run says the user name
        # could not be read instead of printing an empty trigger user, and
        # the real run would refuse (pinned statically).
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, _ = self.run_script(extra_env={
            "USERDOMAIN": None, "USERNAME": None})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DRY RUN: triggers: fire once starting now, then"
                      " repeat every 1 minute for 3650 days, plus fire once"
                      " at logon of the current user (the user name could"
                      " not be read on this host)", proc.stdout)
        self.assertEqual(self.marker_text(marker), "")


class InstallTaskWindowsDryRunDocsTest(unittest.TestCase):
    """Pure file reads: the docs must carry the switch, the manual steps and
    the explicit legacy-name migration order (uninstall first, re-run the
    installer, exactly one task left). pwsh is never needed here."""

    @staticmethod
    def read(relpath):
        with open(os.path.join(REPO_ROOT, relpath), encoding="utf-8") as handle:
            return handle.read()

    def ps1_header(self):
        text = self.read(os.path.join("runalerts",
                                      "install-task-windows.ps1"))
        return "\n".join(line for line in text.splitlines()
                         if line.startswith("#"))

    def readme_windows_section(self):
        text = self.read(os.path.join("runalerts", "README.md"))
        section = text.split("### Windows: Task Scheduler", 1)[1]
        return section.split("\n## ", 1)[0]

    def changelog_unreleased(self):
        return self.read("CHANGELOG.md").split("## 0.2.0", 1)[0]

    @staticmethod
    def prose(text):
        """Whitespace-normalized, casefolded text: documentation prose wraps
        across lines, so the wording assertions below must not depend on
        where the line break or the sentence capital fell."""
        return " ".join(text.split()).lower()

    def test_header_documents_the_switch_the_manual_steps_and_the_order(self):
        header = self.prose(self.ps1_header())
        self.assertIn("-dryrun", header)
        self.assertIn("dry run:", header)
        self.assertIn("manual steps", header)
        self.assertIn("removes both names", header)
        self.assertIn("then re-run the installer", header)
        self.assertIn("exactly one task", header)

    def test_readme_windows_section_documents_dry_run_and_the_order(self):
        section = self.prose(self.readme_windows_section())
        self.assertIn("dry run first", section)
        self.assertIn("-dryrun", section)
        self.assertIn("manual steps", section)
        self.assertIn("scheduledtasks", section)
        self.assertIn("removes both names", section)
        self.assertIn("then re-run the installer", section)
        self.assertIn("exactly one task `g2c.runalertssender` exists",
                      section)

    def test_changelog_unreleased_documents_the_switch_and_the_order(self):
        unreleased = self.prose(self.changelog_unreleased())
        self.assertIn("-dryrun", unreleased)
        self.assertIn("removes both names", unreleased)
        self.assertIn("then re-run the installer", unreleased)
        self.assertIn("exactly one task", unreleased)

    def test_changelog_unreleased_documents_the_scoped_trigger_and_failures(self):
        unreleased = self.prose(self.changelog_unreleased())
        self.assertIn("per-user logon trigger", unreleased)
        self.assertIn("register failure exits 3", unreleased)
        self.assertIn("start failure exits 4", unreleased)
        self.assertIn("no success line is printed after a failure",
                      unreleased)
        self.assertIn("access is denied", unreleased)
        self.assertIn("says the task is registered", unreleased)

    def test_header_documents_the_scoped_trigger_and_the_failures(self):
        # prose() keeps the '#' comment markers (whitespace-collapsed), so a
        # pinned phrase must stay on one comment line of the header.
        header = self.prose(self.ps1_header())
        self.assertIn("plus a logon trigger scoped", header)
        self.assertIn("userdomain and username (when userdomain is empty",
                      header)
        self.assertIn("the bare user name is", header)
        self.assertIn("code 3 for a register failure", header)
        self.assertIn("exit code 4 for a start failure", header)
        self.assertIn("no success line", header)
        self.assertIn("says the task is registered", header)
        self.assertIn("re-run after fixing the cause", header)

    def test_readme_windows_section_documents_the_scoped_trigger_and_failures(self):
        section = self.prose(self.readme_windows_section())
        self.assertIn("per-user logon trigger scoped to the current user",
                      section)
        self.assertIn("computed once from `userdomain` and `username`",
                      section)
        self.assertIn("the bare user name is used", section)
        self.assertIn("without elevation", section)
        self.assertIn("a register failure exits 3", section)
        self.assertIn("a start failure exits 4", section)
        self.assertIn("never print a success line", section)
        self.assertIn("usually means an any-user logon trigger", section)
        self.assertIn("the task is registered", section)
        self.assertIn("re-run the installer after fixing the cause", section)


@REQUIRES_POWERSHELL
class InstallTaskWindowsRealRunTest(InstallTaskWindowsScriptHarness):
    """Drive the real script's real (non-dry-run) install path through the
    stub harness. Every machine boundary is stubbed: the ScheduledTasks
    cmdlets, schtasks.exe and the user-scope ledger read (the script reads
    it through the Get-G2CLedgerUserValue function seam, which this harness
    shadows; G2C_STUB_LEDGER supplies the value and the stub throws without
    it, mimicking a host without a User scope). Env knobs:
    G2C_STUB_REGISTER_FAIL, G2C_STUB_START_FAIL, G2C_STUB_UNREGISTER_FAIL
    and G2C_STUB_SCHTASKS_FAIL turn the corresponding stub into its
    failure shape; the unsubscribed defaults succeed. Nothing outside the
    temp directories is touched on any host."""

    def run_real(self, extra_env=None):
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        merged = {
            "USERDOMAIN": "TESTDOM",
            "USERNAME": "dzidu",
            "G2C_STUB_LEDGER": "C:\\stub\\ledger.jsonl",
        }
        if extra_env:
            merged.update(extra_env)
        return self.run_script(dry_run=False, extra_env=merged)

    @staticmethod
    def marker_lines(marker):
        text = InstallTaskWindowsScriptHarness.marker_text(marker)
        return [line for line in text.splitlines() if line]

    def test_real_run_happy_path_builds_the_user_scoped_logon_trigger(self):
        proc, marker, _ = self.run_real()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout
        self.assertIn("Registered and started scheduled task " + TASK_NAME,
                      out)
        self.assertIn("Four 15-second-apart sender ticks", out)
        self.assertIn("Log file: ", out)
        self.assertIn("First tick behavior", out)
        lines = self.marker_lines(marker)
        trigger_lines = [line for line in lines
                         if line.startswith("New-ScheduledTaskTrigger")]
        self.assertEqual(len(trigger_lines), 2, lines)
        logon_line = "New-ScheduledTaskTrigger logon=True user=TESTDOM\\dzidu"
        self.assertIn(logon_line, lines)
        once_lines = [line for line in trigger_lines if "once=True" in line]
        self.assertEqual(len(once_lines), 1, trigger_lines)
        self.assertNotIn("logon=True", once_lines[0])
        register_line = ("Register-ScheduledTask " + TASK_NAME
                         + " triggers=2")
        self.assertIn(register_line, lines)
        self.assertIn("Start-ScheduledTask " + TASK_NAME, lines)
        # Order: both triggers built (the logon one carrying the env-derived
        # user), then settings, then the registration, then the start.
        self.assertLess(lines.index(logon_line), lines.index(register_line))
        self.assertLess(lines.index("New-ScheduledTaskSettingsSet"),
                        lines.index(register_line))
        action_lines = [line for line in lines
                        if line.startswith("New-ScheduledTaskAction ")]
        self.assertEqual(len(action_lines), 1, lines)
        self.assertIn("pythonw.exe", action_lines[0])
        self.assertTrue(any(line.startswith("New-Item Directory")
                            for line in lines), lines)

    def test_real_run_register_access_denied_fails_loudly_without_starting(self):
        proc, marker, _ = self.run_real(
            extra_env={"G2C_STUB_REGISTER_FAIL": "1"})
        # The harness tail normalizes the child's exit code to 0/1 (see the
        # harness comment): the child took the register-failure branch, and
        # the exact exit code (3) is pinned statically by
        # InstallTaskWindowsScriptTextTest.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        out = proc.stdout
        self.assertIn("Register-ScheduledTask failed: Access is denied.", out)
        self.assertIn("any-user logon trigger", out)
        self.assertNotIn("Registered and started scheduled task", out)
        self.assertNotIn("Four 15-second-apart sender ticks", out)
        self.assertNotIn("Log file: ", out)
        self.assertNotIn("First tick behavior", out)
        lines = self.marker_lines(marker)
        self.assertIn("Register-ScheduledTask " + TASK_NAME + " triggers=2",
                      lines)
        self.assertNotIn("Start-ScheduledTask " + TASK_NAME, lines)

    def test_real_run_start_failure_reports_registered_but_not_started(self):
        proc, marker, _ = self.run_real(
            extra_env={"G2C_STUB_START_FAIL": "1"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        out = proc.stdout
        self.assertIn("Start-ScheduledTask failed:", out)
        self.assertIn("The task IS registered as " + TASK_NAME, out)
        self.assertIn("Start-ScheduledTask -TaskName " + TASK_NAME, out)
        self.assertIn("Unregister-ScheduledTask -TaskName " + TASK_NAME, out)
        self.assertIn("schtasks /Delete /TN " + TASK_NAME + " /F", out)
        self.assertNotIn("Registered and started scheduled task", out)
        self.assertNotIn("Four 15-second-apart sender ticks", out)
        lines = self.marker_lines(marker)
        self.assertIn("Register-ScheduledTask " + TASK_NAME + " triggers=2",
                      lines)
        self.assertIn("Start-ScheduledTask " + TASK_NAME, lines)

    def test_uninstall_real_run_removes_both_names_via_unregister(self):
        # The uninstall branch exits before the placeholder checks, but the
        # harness still needs the filled template copy to exist.
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, _ = self.run_script(dry_run=False, uninstall=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout
        for name in (TASK_NAME, LEGACY_TASK_NAME):
            self.assertIn("Removed scheduled task " + name, out)
        self.assertNotIn("schtasks fallback", out)
        lines = self.marker_lines(marker)
        for name in (TASK_NAME, LEGACY_TASK_NAME):
            self.assertEqual(
                lines.count("Unregister-ScheduledTask " + name), 1, lines)
        self.assertTrue(
            not any(line.startswith("schtasks.exe") for line in lines),
            lines)
        self.assertNotIn("Start-ScheduledTask", "\n".join(lines))

    def test_uninstall_real_run_falls_back_to_schtasks_on_refusal(self):
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, _ = self.run_script(
            dry_run=False, uninstall=True,
            extra_env={"G2C_STUB_UNREGISTER_FAIL": "1"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout
        for name in (TASK_NAME, LEGACY_TASK_NAME):
            self.assertIn("Removed scheduled task " + name
                          + " (schtasks fallback)", out)
        self.assertNotIn("fallback also failed", out)
        lines = self.marker_lines(marker)
        for name in (TASK_NAME, LEGACY_TASK_NAME):
            unregister = "Unregister-ScheduledTask " + name
            fallback = "schtasks.exe /Delete /TN " + name + " /F"
            self.assertIn(unregister, lines)
            self.assertIn(fallback, lines)
            self.assertLess(lines.index(unregister), lines.index(fallback))

    def test_uninstall_real_run_fallback_failure_exits_nonzero(self):
        python_exe = self.make_interpreter(with_pythonw=True)
        sender = self.make_sender()
        self.write_filled(python_exe, sender)
        proc, marker, _ = self.run_script(
            dry_run=False, uninstall=True,
            extra_env={"G2C_STUB_UNREGISTER_FAIL": "1",
                       "G2C_STUB_SCHTASKS_FAIL": "1"})
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        out = proc.stdout
        self.assertIn("Unregister-ScheduledTask failed: Access is denied.",
                      out)
        self.assertIn("The schtasks fallback also failed (exit code 1)", out)
        self.assertNotIn("Removed scheduled task", out)
        # The script stops at the first name's failed fallback: exactly one
        # unregister and one schtasks attempt reached the stubs.
        lines = self.marker_lines(marker)
        self.assertEqual(
            len([line for line in lines
                 if line.startswith("Unregister-ScheduledTask")]),
            1, lines)
        self.assertEqual(
            len([line for line in lines
                 if line.startswith("schtasks.exe")]),
            1, lines)


class InstallTaskWindowsScriptTextTest(unittest.TestCase):
    """Pure file reads of the installer template's non-comment text: pins
    the user-scoped logon trigger and the fail-loud structure. The stub
    harness normalizes the child's exit code to 0/1 (see the harness tail
    comment), so the distinct exit codes 3/4 are pinned here on the script
    text instead of in a live run."""

    @staticmethod
    def code_text():
        with open(TEMPLATE_PATH, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines()
                     if not line.lstrip().startswith("#")]
        return "\n".join(lines)

    def test_logon_trigger_is_scoped_to_the_current_user(self):
        code = self.code_text()
        self.assertIn("$LogonUser = $env:USERNAME", code)
        self.assertIn("-not [string]::IsNullOrEmpty($env:USERDOMAIN)", code)
        self.assertIn('$LogonUser = "$env:USERDOMAIN\\$env:USERNAME"', code)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn -User $LogonUser",
                      code)
        self.assertIn("plus fire once at logon of $LogonUser", code)
        # Fail closed on an unreadable user name instead of registering an
        # un-scoped (any-user) trigger that needs elevation.
        self.assertIn("cannot scope the logon trigger to the current user",
                      code)

    def test_register_and_start_fail_loudly_with_distinct_exit_codes(self):
        code = self.code_text()
        self.assertEqual(code.count("exit 3"), 1)
        self.assertEqual(code.count("exit 4"), 1)
        # Register, start and the uninstall unregister all run under Stop.
        self.assertEqual(code.count("-ErrorAction Stop"), 3)
        self.assertIn("Register-ScheduledTask -TaskName $TaskName"
                      " -Trigger $trigger, $logonTrigger", code)
        self.assertIn("Start-ScheduledTask -TaskName $TaskName"
                      " -ErrorAction Stop", code)
        self.assertIn("Register-ScheduledTask failed:"
                      " $($_.Exception.Message)", code)
        self.assertIn('if ($_.Exception.Message -match "Access is denied") {',
                      code)

    def test_success_lines_only_after_both_steps_succeed(self):
        code = self.code_text()
        order = [
            code.index("Register-ScheduledTask failed:"
                       " $($_.Exception.Message)"),
            code.index("exit 3"),
            code.index("Start-ScheduledTask failed:"
                       " $($_.Exception.Message)"),
            code.index("exit 4"),
            code.index("Registered and started scheduled task"),
        ]
        self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()
