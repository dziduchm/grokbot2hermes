"""Unit test for the committed dash-scan script (.github/scripts/dash_scan.py).

CI runs the script directly; locally a plain
``python3 -m unittest discover -s tests`` must cover it too, so the scan never
needs an ad hoc pipeline (cat, sed, or an inline heredoc) behind an exec
approval. The test invokes the committed script as a subprocess with
sys.executable from the repo root and asserts the exit contract; it does not
embed a second copy of the scanner.
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "dash_scan.py"


class DashScanScriptTest(unittest.TestCase):
    """Run the committed scanner and hold its exit contract."""

    def test_scan_exits_zero_on_current_tracked_tree(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0,
            "dash scan reported offenders:\n" + (proc.stderr or proc.stdout))
        self.assertIn("0 hits", proc.stdout)


if __name__ == "__main__":
    unittest.main()
