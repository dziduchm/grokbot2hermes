"""Tests for the Windows port runalerts/set-wait-timeout.ps1.

Mirrors the key tests of tests/test_set_wait_timeout.py (the .sh suite):
each test drives the real ps1 with a FAKE hermes stub via HERMES_BIN, sets
up a fake Hermes home in a temp dir, and asserts on exit code, stderr
FAIL text, exact hermes call order, the recorded state JSONL and the stub's
config values. The ps1 must reach the hermes CLI only; this suite pins
that writes never bypass it.

Skipped when no PowerShell interpreter is found: this repo's own Mac has no
pwsh; TheCow runs this suite under pwsh 7.4 on Linux and CI runs it on
ubuntu, macos and windows (GitHub images ship pwsh; windows also carries
powershell.exe 5.1, the compatibility target of the script).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import Optional


def _find_pwsh() -> Optional[str]:
    for name in ("pwsh", "powershell", "powershell.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


PWSH = _find_pwsh()

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(REPO_ROOT, "runalerts", "set-wait-timeout.ps1")

STATE_KIND = "g2c-wait-timeout-state"

# What the fake hermes logs: one JSON line per call.
FAKE_HERMES_SOURCE = '''#!/usr/bin/env python3
"""Fake hermes CLI for set-wait-timeout.ps1 tests.

Behaviour: logs the argv as a JSON line to FAKE_HERMES_LOG; config subcommands
mutate an in-memory dict persisted to FAKE_HERMES_STATE (JSON: profile ->
key -> value string); `config get` re-loads the file first so an apply/verify
round sees the stub's own writes; exits 2 for bad subcommands, 1 for
`config unset` of an absent key (and for `config get` of an absent key,
printing an empty line like the real CLI); everything else exits 0.
"""
import json
import os
import sys

LOG = os.environ.get("FAKE_HERMES_LOG")
STATE = os.environ.get("FAKE_HERMES_STATE")

def _log(argv):
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(argv) + "\\n")

def _load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}

def _save(data):
    with open(STATE, "w") as fh:
        json.dump(data, fh)

def _profile(argv):
    if len(argv) >= 2 and argv[0] == "-p":
        return argv[1], argv[2:]
    return "default", argv

def main():
    argv = sys.argv[1:]
    _log(argv)
    prof, rest = _profile(argv)
    if len(rest) < 3 or rest[0] != "config":
        sys.stderr.write("fake-hermes: unexpected arguments: %r\\n" % (rest,))
        return 2
    sub, key = rest[1], rest[2]
    data = _load()
    bucket = data.setdefault(prof, {})
    if sub == "set":
        bucket[key] = rest[3]
        _save(data)
        sys.stdout.write("Set %s = %s\\n" % (key, rest[3]))
        return 0
    if sub == "get":
        _load()
        val = bucket.get(key)
        sys.stdout.write("%s\\n" % (val if val is not None else ""))
        return 0 if val is not None else 1
    if sub == "unset":
        if key not in bucket:
            sys.stderr.write("Config key not set\\n")
            return 1
        del bucket[key]
        _save(data)
        return 0
    sys.stderr.write("fake-hermes: unknown subcommand %r\\n" % (sub,))
    return 2

if __name__ == "__main__":
    sys.exit(main())
'''

# Fixture content mirrors the .sh suite shapes, but the default profile's
# config lives in the Hermes home root, not under profiles/.
HERMES_HOME_CONFIG = """\
# default profile fixture: both keys set, plus the legacy key
app:
  name: goodferry-test
agent:
  # clarify_timeout: the question window in seconds
  clarify_timeout: 90
  model_count: 2
approvals:
  timeout: 600        # approval card window
clarify:
  timeout: 450
"""

PROFILE_ALPHA = """\
# fixture: named profile, both keys set
app:
  name: alpha-test
agent:
  clarify_timeout: 30
  model_count: 1
approvals:
  timeout: 300
"""

PROFILE_IMILK = """\
# fixture: named profile with the legacy key present
agent:
  clarify_timeout: 84
  model_count: 2
approvals:
  timeout: 700
clarify:
  timeout: 300
"""

PROFILE_BAD_VALUE = """\
# fixture: prior value is not an integer scalar: the raw layer refuses
agent:
  clarify_timeout: soon
approvals:
  timeout: 600
"""

PROFILE_EMPTY_COLON = """\
# fixture: leaf line with nothing after the colon: null or nested block
agent:
  clarify_timeout:
  name: x
approvals:
  timeout: 600
"""

PROFILE_FLOW_EMPTY = """\
# fixture: empty flow section counts the key as absent
agent: {}
approvals:
  timeout: 600
"""

PROFILE_FLOW_LEAF = """\
# fixture: flow section mentions the leaf
agent: {clarify_timeout: 99}
approvals:
  timeout: 600
"""

PROFILE_DEEP_LEAF = """\
# fixture: leaf nested deeper than the direct children: the raw layer cannot
# decide, so the reader refuses
agent:
  model:
    clarify_timeout: 42
approvals:
  timeout: 600
"""

PROFILE_DEEP_LEAF_WITH_DIRECT = """\
# fixture: BOTH a nested leaf-shaped line and a present direct leaf
agent:
  clarify_timeout: 90
  model:
    clarify_timeout: 42
approvals:
  timeout: 600
"""

PROFILE_SHALLOW_LEAF = """\
# fixture: a leaf-shaped line SHALLOWER than the direct children
agent:
    sub:
      x: 1
  clarify_timeout: 90
approvals:
  timeout: 600
"""

PROFILE_NESTED_BLOCK = """\
# fixture: nested-block: a map opens under the section but never names the leaf
agent:
  sub:
    x: 1
approvals:
  timeout: 600
"""

PROFILE_NESTED_WITH_SIBLING = """\
# real-coder shape: nested map plus a direct sibling leaf at child indent
agent:
  personalities:
    helpful: You are helpful.
    concise: Keep it brief.
  clarify_timeout: 84
approvals:
  timeout: 600
"""

PROFILE_INDENT4 = """\
# fixture: the first child fixes the direct-child indent at 4 spaces
agent:
    clarify_timeout: 71
    model_count: 3
approvals:
    timeout: 700
"""


def state_header_text(wait_timeout):
    return ('{"kind": "%s", "created_utc": "%s", "wait_timeout": %d}'
            % (STATE_KIND, "2026-11-21T12:00:00Z", wait_timeout))


@unittest.skipUnless(PWSH, "pwsh (or powershell.exe on win32) not found: ps1 tests skipped")
class TestSetWaitTimeoutPs1(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="g2c-wtps1-")
        self.hermes_home = os.path.join(self.tmp, "hermes-home")
        self.state_dir = os.path.join(self.tmp, "state-root")
        os.makedirs(self.hermes_home, exist_ok=True)
        os.makedirs(self.state_dir, exist_ok=True)
        self.log_path = os.path.join(self.tmp, "hermes.log")
        self.stub_state = os.path.join(self.tmp, "hermes-state.json")
        self.stub = self._make_stub()
        self.last_out = ""
        self.last_err = ""

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def _make_stub(self):
        stub_dir = os.path.join(self.tmp, "stub")
        os.makedirs(stub_dir, exist_ok=True)
        py = os.path.join(stub_dir, "fake-hermes.py")
        with open(py, "w", encoding="utf-8") as fh:
            fh.write(FAKE_HERMES_SOURCE)
        if sys.platform == "win32":
            stub = os.path.join(stub_dir, "fake-hermes.cmd")
            wrapper = '@echo off\r\n"%s" "%%~dp0fake-hermes.py" %%*\r\n' % (
                sys.executable,)
            with open(stub, "w", newline="") as fh:
                fh.write(wrapper)
        else:
            stub = os.path.join(stub_dir, "fake-hermes")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write(FAKE_HERMES_SOURCE)
            os.chmod(stub, 0o755)
        return stub

    def run_ps(self, *args):
        base = [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-File", SCRIPT] + [str(x) for x in args]
        env = dict(os.environ)
        env.update({
            "HERMES_BIN": self.stub,
            "HERMES_HOME": self.hermes_home,
            "G2C_STATE_DIR": self.state_dir,
            "FAKE_HERMES_LOG": self.log_path,
            "FAKE_HERMES_STATE": self.stub_state,
        })
        proc = subprocess.run(base, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        self.last_out = proc.stdout.decode("utf-8", "replace")
        self.last_err = proc.stderr.decode("utf-8", "replace")
        return proc

    def calls(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def assert_no_calls(self):
        self.assertEqual(self.calls(), [])

    def write_default_profile(self, content):
        with open(os.path.join(self.hermes_home, "config.yaml"), "w",
                  encoding="utf-8") as fh:
            fh.write(content)

    def write_named(self, profile, content):
        pdir = os.path.join(self.hermes_home, "profiles", profile)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "config.yaml"), "w",
                  encoding="utf-8") as fh:
            fh.write(content)

    def profile_ops(self, profile, sub, key=None):
        values = []
        for argv in self.calls():
            if profile == "default":
                head = 0
            else:
                head = 2
                if not (len(argv) >= 5 and argv[0] == "-p"
                        and argv[1] == profile):
                    continue
            rest = argv[head:]
            if (len(rest) >= 3 and rest[0] == "config" and rest[1] == sub
                    and (key is None or rest[2] == key)):
                values.append(rest[3] if sub == "set" else sub)
        return values

    def stub_config(self, profile):
        with open(self.stub_state, encoding="utf-8") as fh:
            return json.load(fh).get(profile, {})

    def assert_stub_config(self, profile, expected):
        self.assertEqual(self.stub_config(profile), expected)

    def read_state(self, path):
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh.read().splitlines()
                     if line.strip()]
        return lines[0], lines[1:]

    def write_state(self, path, wait_timeout, records):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(state_header_text(wait_timeout) + "\n")
            for profile, key, prior in records:
                prior_text = "null" if prior is None else str(prior)
                fh.write('{"profile": "%s", "key": "%s", "prior": %s}\n'
                         % (profile, key, prior_text))

    def write_state_raw(self, wait_timeout, lines, name="manual.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(state_header_text(wait_timeout) + "\n")
            for line in lines:
                fh.write(line + "\n")
        return path

    # -- dry run ------------------------------------------------------------

    def test_dry_run_default_600_makes_no_config_calls(self):
        self.write_default_profile(HERMES_HOME_CONFIG)
        proc = self.run_ps()
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("DRY RUN", self.last_out)
        self.assertIn("wait timeout: 600s", self.last_out)
        self.assertIn("profiles: default", self.last_out)
        self.assertIn(self.stub + " config set agent.clarify_timeout 600",
                      self.last_out)
        self.assertIn(self.stub + " config set approvals.timeout 600",
                      self.last_out)
        self.assertIn("restart: none needed", self.last_out)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(
            os.path.join(self.state_dir, "wait-timeout-state-600.jsonl")))

    def test_dry_run_reads_priors_from_the_raw_layer(self):
        self.write_default_profile(HERMES_HOME_CONFIG)
        proc = self.run_ps()
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("agent.clarify_timeout=90", self.last_out)
        self.assertIn("approvals.timeout=600", self.last_out)
        self.assertIn("legacy clarify.timeout=450", self.last_out)
        self.assert_no_calls()

    def test_dry_run_refuses_a_leaf_deeper_than_the_children(self):
        self.write_named("solo", PROFILE_DEEP_LEAF)
        proc = self.run_ps("-Profiles", "solo")
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing fail closed", self.last_err)
        self.assertIn("direct children", self.last_err)
        self.assert_no_calls()

    def test_dry_run_refuses_a_leaf_shallower_than_the_children(self):
        self.write_named("solo", PROFILE_SHALLOW_LEAF)
        proc = self.run_ps("-Profiles", "solo")
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing fail closed", self.last_err)
        self.assertIn("direct children", self.last_err)
        self.assert_no_calls()

    def test_nested_block_without_the_leaf_leaves_the_key_absent(self):
        self.write_named("solo", PROFILE_NESTED_BLOCK)
        proc = self.run_ps("-Profiles", "solo")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("agent.clarify_timeout=(unset)", self.last_out)
        self.assert_no_calls()

    def test_nested_map_with_a_direct_sibling_leaf_counts_the_leaf(self):
        self.write_named("solo", PROFILE_NESTED_WITH_SIBLING)
        proc = self.run_ps("-Profiles", "solo")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("agent.clarify_timeout=84", self.last_out)
        self.assert_no_calls()

    def test_children_at_the_first_child_indent_still_count(self):
        self.write_named("solo", PROFILE_INDENT4)
        proc = self.run_ps("-Profiles", "solo")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("agent.clarify_timeout=71", self.last_out)
        self.assertIn("approvals.timeout=700", self.last_out)
        self.assert_no_calls()

    def test_dry_run_skips_commands_for_a_missing_named_profile(self):
        self.write_named("alpha", PROFILE_ALPHA)
        proc = self.run_ps("-Profiles", "alpha,missing-n")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertEqual(self.last_out.count("profile config missing"), 2)
        self.assertNotIn("missing-n config", self.last_out)
        self.assertIn(self.stub + " -p alpha config set "
                      "agent.clarify_timeout 600", self.last_out)
        self.assertIn(self.stub + " -p alpha config set "
                      "approvals.timeout 600", self.last_out)
        self.assert_no_calls()

    def test_dry_run_notes_a_missing_default_config_without_commands(self):
        # default: the config lives in the home root; nothing was written yet.
        proc = self.run_ps()
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertEqual(self.last_out.count("profile config missing"), 2)
        self.assertNotIn("config set", self.last_out)
        self.assert_no_calls()

    # -- validation ---------------------------------------------------------

    def test_invalid_wait_timeout_values_exit_2_and_do_nothing(self):
        self.write_default_profile(HERMES_HOME_CONFIG)
        for value in ["", "abc", "0", "059", "59", "86401",
                      "12345678901234567890"]:
            proc = self.run_ps("-Apply", "-State",
                               os.path.join(self.state_dir, "s.jsonl"),
                               "-WaitTimeout", value)
            self.assertEqual(proc.returncode, 2, value)
            self.assertIn("FAIL", self.last_err)
        self.assert_no_calls()

    def test_profile_name_validation_rejects_bad_lists(self):
        for value in ["A,b", "a,,b", "a,a", "a b", "a/.."]:
            proc = self.run_ps("-Apply", "-Profiles", value,
                               "-State", os.path.join(self.state_dir, "s"))
            self.assertEqual(proc.returncode, 2, value)
            self.assertIn("FAIL", self.last_err)
        self.assert_no_calls()

    def test_profiles_starting_with_a_dash_are_rejected(self):
        proc = self.run_ps("-Profiles", "--apply")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must not start with '-'", self.last_err)
        proc = self.run_ps("-Apply", "-Profiles", "--apply",
                           "-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 2)
        self.assert_no_calls()
        proc = self.run_ps("-Apply", "-Profiles", "default,--imilk",
                           "-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 2, self.last_err)
        self.assert_no_calls()

    def test_state_flag_without_apply_is_rejected(self):
        proc = self.run_ps("-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("FAIL", self.last_err)
        self.assert_no_calls()

    def test_unknown_argument_exits_2(self):
        proc = self.run_ps("-Bogus")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown argument", self.last_err)
        proc = self.run_ps("-WaitTimeout")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("needs a value", self.last_err)
        self.assert_no_calls()

    def test_help_exits_0(self):
        proc = self.run_ps("-Help")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("Usage", self.last_out)
        self.assert_no_calls()

    # -- apply --------------------------------------------------------------

    def test_apply_with_also_legacy_sets_verifies_and_records(self):
        self.write_default_profile(HERMES_HOME_CONFIG)
        self.write_named("b2", "# fixture: unset profile, no keys yet\n")
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "default,b2",
                           "-State", state_path, "-AlsoLegacy",
                           "-WaitTimeout", "600")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assert_stub_config("default", {
            "agent.clarify_timeout": "600",
            "approvals.timeout": "600",
            "clarify.timeout": "600",
        })
        self.assert_stub_config("b2", {
            "agent.clarify_timeout": "600",
            "approvals.timeout": "600",
        })
        self.assertEqual(len(self.profile_ops(
            "b2", "set", "agent.clarify_timeout")), 1)
        self.assertEqual(len(self.profile_ops("b2", "unset")), 0)
        self.assertEqual(len(self.profile_ops("default", "get")), 3)
        self.assertEqual(len(self.profile_ops("b2", "get")), 2)
        header, records = self.read_state(state_path)
        self.assertEqual(header["kind"], STATE_KIND)
        self.assertEqual(header["wait_timeout"], 600)
        self.assertEqual([
            (r["profile"], r["key"], r["prior"]) for r in records], [
            ("default", "agent.clarify_timeout", 90),
            ("default", "approvals.timeout", 600),
            ("default", "clarify.timeout", 450),
            ("b2", "agent.clarify_timeout", None),
            ("b2", "approvals.timeout", None),
        ])
        if sys.platform != "win32":
            mode = stat.S_IMODE(os.stat(state_path).st_mode)
            self.assertEqual(mode, 0o600, (
                "state file must be private to the user (0600), got %o"
                % mode))
        self.assertIn("verify ok", self.last_out)
        self.assertIn("rollback later", self.last_out)
        self.assertIn("restart: none needed", self.last_out)

    def test_apply_refuses_legacy_without_also_legacy(self):
        self.write_default_profile(HERMES_HOME_CONFIG)
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-State", state_path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing to apply", self.last_err)
        self.assertIn("clarify.timeout=450", self.last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))

    def test_apply_refuses_undecidable_prior_value(self):
        self.write_named("solo", PROFILE_BAD_VALUE)
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "solo", "-State", state_path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing fail closed", self.last_err)
        self.assertIn("not an integer scalar", self.last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))

    def test_apply_refuses_null_like_colon_value(self):
        self.write_named("solo", PROFILE_EMPTY_COLON)
        proc = self.run_ps("-Apply", "-Profiles", "solo",
                           "-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("nothing after the colon", self.last_err)
        self.assert_no_calls()

    def test_apply_refuses_a_leaf_deeper_than_the_children(self):
        self.write_named("solo", PROFILE_DEEP_LEAF)
        proc = self.run_ps("-Apply", "-Profiles", "solo",
                           "-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing fail closed", self.last_err)
        self.assertIn("direct children", self.last_err)
        self.assert_no_calls()

    def test_apply_refuses_a_deeper_leaf_alongside_a_present_direct_leaf(self):
        self.write_named("solo", PROFILE_DEEP_LEAF_WITH_DIRECT)
        proc = self.run_ps("-Apply", "-Profiles", "solo",
                           "-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing fail closed", self.last_err)
        self.assertIn("direct children", self.last_err)
        self.assert_no_calls()

    def test_empty_flow_section_counts_as_absent(self):
        self.write_named("solo", PROFILE_FLOW_EMPTY)
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "solo", "-State", state_path)
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assert_stub_config("solo", {
            "agent.clarify_timeout": "600",
            "approvals.timeout": "600",
        })
        header, records = self.read_state(state_path)
        self.assertEqual(header["wait_timeout"], 600)
        self.assertEqual([
            (r["profile"], r["key"], r["prior"]) for r in records], [
            ("solo", "agent.clarify_timeout", None),
            ("solo", "approvals.timeout", 600),
        ])

    def test_flow_section_naming_the_leaf_refuses(self):
        self.write_named("solo", PROFILE_FLOW_LEAF)
        proc = self.run_ps("-Apply", "-Profiles", "solo",
                           "-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("flow section value mentions", self.last_err)
        self.assert_no_calls()

    def test_apply_refuses_a_missing_named_profile_config(self):
        self.write_named("alpha", PROFILE_ALPHA)
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "alpha,missing-n",
                           "-State", state_path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("profile config", self.last_err)
        self.assertIn("missing-n", self.last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))
        # and the dry run prints notes for it, consistently
        proc = self.run_ps("-Profiles", "alpha,missing-n")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("profile config missing", self.last_out)
        self.assertIn("agent.clarify_timeout=30", self.last_out)

    def test_apply_refuses_when_default_config_missing(self):
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-State", state_path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("profile config file(s) missing", self.last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))

    def test_apply_refuses_to_overwrite_a_state_file(self):
        self.write_named("solo", PROFILE_ALPHA)
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "solo", "-State", state_path)
        self.assertEqual(proc.returncode, 0, self.last_err)
        proc = self.run_ps("-Apply", "-Profiles", "solo", "-State", state_path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing to overwrite", self.last_err)

    def test_custom_wait_timeout_values_set_and_record(self):
        self.write_default_profile(PROFILE_ALPHA)
        for value in ("900", "86400"):
            state_path = os.path.join(
                self.state_dir, "state-%s.jsonl" % value)
            proc = self.run_ps("-Apply", "-State", state_path,
                               "-WaitTimeout", value)
            self.assertEqual(proc.returncode, 0, self.last_err)
            self.assert_stub_config("default", {
                "agent.clarify_timeout": value,
                "approvals.timeout": value,
            })
            header, records = self.read_state(state_path)
            self.assertEqual(header["wait_timeout"], int(value))
            self.assertEqual([
                (r["profile"], r["key"], r["prior"]) for r in records], [
                ("default", "agent.clarify_timeout", 30),
                ("default", "approvals.timeout", 300),
            ])

    def test_default_and_named_profile_p_handling(self):
        # Pins the -p contract: the default profile is called WITHOUT -p,
        # named profiles WITH -p <name>.
        self.write_default_profile(PROFILE_ALPHA)
        self.write_named("al", PROFILE_ALPHA)
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "default,al",
                           "-State", state_path)
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertEqual(self.profile_ops(
            "default", "set", "agent.clarify_timeout"), ["600"])
        self.assertEqual(self.profile_ops(
            "al", "set", "agent.clarify_timeout"), ["600"])
        calls = self.calls()
        self.assertEqual(calls[0][:3], ["config", "set",
                                        "agent.clarify_timeout"])
        self.assertEqual(calls[2][:3], ["config", "get",
                                        "agent.clarify_timeout"])
        self.assertEqual(calls[4][:5], ["-p", "al", "config", "set",
                                        "agent.clarify_timeout"])
        header, records = self.read_state(state_path)
        self.assertEqual(header["wait_timeout"], 600)
        self.assertEqual([
            (r["profile"], r["key"], r["prior"]) for r in records], [
            ("default", "agent.clarify_timeout", 30),
            ("default", "approvals.timeout", 300),
            ("al", "agent.clarify_timeout", 30),
            ("al", "approvals.timeout", 300),
        ])

    def test_legacy_profile_with_also_legacy(self):
        self.write_named("imilk", PROFILE_IMILK)
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "imilk", "-State",
                           state_path, "-AlsoLegacy")
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assert_stub_config("imilk", {
            "agent.clarify_timeout": "600",
            "approvals.timeout": "600",
            "clarify.timeout": "600",
        })
        header, records = self.read_state(state_path)
        self.assertEqual(header["wait_timeout"], 600)
        self.assertEqual([
            (r["profile"], r["key"], r["prior"]) for r in records], [
            ("imilk", "agent.clarify_timeout", 84),
            ("imilk", "approvals.timeout", 700),
            ("imilk", "clarify.timeout", 300),
        ])

    def test_legacy_profile_refused_without_also_legacy(self):
        self.write_named("imilk", PROFILE_IMILK)
        proc = self.run_ps("-Apply", "-Profiles", "imilk",
                           "-State", os.path.join(self.state_dir, "s"))
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("refusing to apply", self.last_err)
        self.assertIn("clarify.timeout=300", self.last_err)
        self.assert_no_calls()

    # -- rollback -----------------------------------------------------------

    def test_rollback_restores_prior_values_including_unset(self):
        self.write_default_profile(HERMES_HOME_CONFIG)
        self.write_named("b2", "# fixture: unset profile\n")
        state_path = os.path.join(self.state_dir, "state.jsonl")
        proc = self.run_ps("-Apply", "-Profiles", "default,b2",
                           "-State", state_path, "-AlsoLegacy")
        self.assertEqual(proc.returncode, 0, self.last_err)
        # The fake CLI log accumulates across invocations; truncate it so the
        # profile_ops assertions below see only the rollback's own calls.
        open(self.log_path, "w").close()
        proc = self.run_ps("-Rollback", state_path)
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertEqual(self.profile_ops(
            "default", "set", "agent.clarify_timeout"), ["90"])
        self.assertEqual(self.profile_ops(
            "default", "set", "approvals.timeout"), ["600"])
        self.assertEqual(self.profile_ops(
            "default", "set", "clarify.timeout"), ["450"])
        self.assertEqual(self.profile_ops(
            "b2", "unset", "agent.clarify_timeout"), ["unset"])
        self.assertEqual(self.profile_ops(
            "b2", "unset", "approvals.timeout"), ["unset"])
        self.assert_stub_config("default", {
            "agent.clarify_timeout": "90",
            "approvals.timeout": "600",
            "clarify.timeout": "450",
        })
        self.assert_stub_config("b2", {})
        self.assertGreater(len(self.calls()), 0)  # rollback made its calls
        self.assertIn("rollback done", self.last_out)

    def test_rollback_tolerates_already_unset_keys(self):
        state_path = os.path.join(self.state_dir, "manual.jsonl")
        self.write_state(state_path, 600, [
            ("solo", "agent.clarify_timeout", None),
        ])
        proc = self.run_ps("-Rollback", state_path)
        self.assertEqual(proc.returncode, 0, self.last_err)
        self.assertIn("already unset (ok)", self.last_out)

    def test_rollback_refuses_a_non_state_file(self):
        path = os.path.join(self.tmp, "junk.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("not a state file\n")
        proc = self.run_ps("-Rollback", path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("does not look like", self.last_err)
        self.assert_no_calls()

    def test_rollback_refuses_a_malformed_record_line(self):
        path = self.write_state_raw(600, [
            '{"profile": "solo", "key": "evil.key", "prior": "90"}',
        ])
        proc = self.run_ps("-Rollback", path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("not understood", self.last_err)
        self.assert_no_calls()

    def test_rollback_refuses_a_kind_substring_line(self):
        path = self.write_state_raw(600, [
            '{"other": "g2c-wait-timeout-state", "value": 600}',
        ])
        proc = self.run_ps("-Rollback", path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("not understood", self.last_err)
        self.assert_no_calls()

    def test_rollback_rejects_conflicting_flags(self):
        path = self.write_state_raw(600, [
            '{"profile": "solo", "key": "agent.clarify_timeout",'
            ' "prior": "90"}',
        ], name="conflict.jsonl")
        proc = self.run_ps("-Rollback", path, "-Apply")
        self.assertEqual(proc.returncode, 2)
        proc = self.run_ps("-Rollback", path, "-AlsoLegacy")
        self.assertEqual(proc.returncode, 2, self.last_err)
        proc = self.run_ps("-Rollback", path, "-WaitTimeout", "700")
        self.assertEqual(proc.returncode, 2, self.last_err)
        self.assert_no_calls()

    def test_rollback_refuses_a_state_file_without_a_final_newline(self):
        path = os.path.join(self.tmp, "truncated.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(state_header_text(600) + "\n"
                     '{"profile": "solo", "key": "agent.clarify_timeout",'
                     ' "prior": "90"}')
        proc = self.run_ps("-Rollback", path)
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("does not end with a newline", self.last_err)
        self.assert_no_calls()

    def test_rollback_refuses_a_missing_state_file(self):
        proc = self.run_ps("-Rollback", os.path.join(self.tmp, "nope.jsonl"))
        self.assertEqual(proc.returncode, 1, self.last_err)
        self.assertIn("state file not found", self.last_err)
        self.assert_no_calls()


if __name__ == "__main__":
    unittest.main()
