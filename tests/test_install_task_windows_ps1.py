"""Tests for runalerts/install-task-windows.ps1's -DryRun switch.

Two layers:

- InstallTaskWindowsDryRunTest drives the real script in a temporary filled
  copy under a stub harness: stub functions named New-Item,
  Register-ScheduledTask, Unregister-ScheduledTask, Start-ScheduledTask,
  schtasks.exe and schtasks shadow the real commands and append to a marker
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
- InstallTaskWindowsDryRunDocsTest is a pure file read (runs everywhere,
  pwsh not needed) pinning the header, README and CHANGELOG documentation of
  the switch, the manual fill-and-pin steps, and the explicit legacy-name
  migration order.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(REPO_ROOT, "runalerts", "install-task-windows.ps1")

TASK_NAME = "G2C.RunalertsSender"
LEGACY_TASK_NAME = "ComMdG2c.RunalertsSender"

# pwsh 7 and Windows PowerShell 5.1 both take the call operator's child-script
# exit code differently enough that the harness normalizes it: after the child
# runs, the harness exits 0 on pipeline success ($?) and 1 otherwise, which
# matches the child's exit 0/exit 1 under either nesting behavior.
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

function Register-ScheduledTask {
    param($TaskName, $Trigger, $Settings, $Action, $Description)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "Register-ScheduledTask $TaskName"
}

function Unregister-ScheduledTask {
    param($TaskName, $Confirm)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "Unregister-ScheduledTask $TaskName"
}

function Start-ScheduledTask {
    param($TaskName)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "Start-ScheduledTask $TaskName"
}

# PowerShell resolves commands by exact name: the uninstall fallback calls
# & schtasks.exe, so the stub must carry the .exe suffix to own that token
# (a function named schtasks never intercepts it). Both spellings stub into
# the marker, so any spelling of a future external-tool call is caught.
function schtasks.exe {
    param($ArgumentList)
    Add-Content -LiteralPath $env:G2C_DRYRUN_MARKER -Value "schtasks.exe $ArgumentList"
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
class InstallTaskWindowsDryRunTest(unittest.TestCase):
    """Drive the real template in a temp filled copy under the stub harness."""

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

    def run_script(self, dry_run=True, uninstall=False):
        harness = os.path.join(self.tmp, "harness.ps1")
        with open(harness, "w", encoding="utf-8", newline="") as handle:
            handle.write(STUB_HARNESS)
        marker = os.path.join(self.tmp, "marker.txt")
        appdata = os.path.join(self.tmp, "appdata")
        os.makedirs(appdata, exist_ok=True)
        env = dict(os.environ)
        env["G2C_DRYRUN_MARKER"] = marker
        env["APPDATA"] = appdata
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


if __name__ == "__main__":
    unittest.main()
