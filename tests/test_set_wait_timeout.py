"""Tests for runalerts/set-wait-timeout.sh (G2C wait timeout installer).

Each test drives the real script with a FAKE hermes stub (HERMES_BIN) that
records every call into a JSONL log and simulates `config get|set|unset` with
per-profile state in a throwaway dir, so nothing touches the real Hermes
install. The profile fixtures are throwaway config.yaml files under a temp
profile root (G2C_PROFILE_ROOT, HOME), covering bare integers, quoted scalars,
inline comments, a legacy `clarify.timeout` key, fully unset profiles, a
four-space child indent, and nested/deeper-indent shapes the raw reader
must refuse.

The module is unittest-compatible on purpose: the repo's CI runs the stdlib
unittest suite, so it stays importable without pytest. pytest runs it fine
too (no pytest-only features used).
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "runalerts", "set-wait-timeout.sh")

KEY_CLARIFY = "agent.clarify_timeout"
KEY_APPROVALS = "approvals.timeout"
KEY_LEGACY = "clarify.timeout"

# Raw-layer shapes the reader must survive (or fail closed on).
PROFILE_CODER = (
    "# coder profile fixture: both keys set, legacy set\n"
    "app:\n"
    "  public_url: https://example.test\n"
    "agent:\n"
    "  # clarify window\n"
    "  clarify_timeout: 90\n"
    "  model_count: 2\n"
    "approvals:\n"
    "  timeout: 600        # approval card window\n"
    "clarify:\n"
    "  timeout: 450\n"
)
PROFILE_CODE_REVIEWER = (
    "# code-reviewer fixture: quoted approvals value\n"
    "agent:\n"
    "  clarify_timeout: 120\n"
    "approvals:\n"
    "  timeout: \"600\"\n"
)
PROFILE_IMILK = (
    "# imilk fixture: nothing relevant set\n"
    "webapp:\n"
    "  run_alerts:\n"
    "    interval: 30\n"
)
PROFILE_RESEARCHER = (
    "# researcher fixture: nothing relevant set\n"
    "webapp:\n"
    "  run_alerts:\n"
    "    interval: 30\n"
)
PROFILE_ALPHA = (
    "# alpha fixture: plain values\n"
    "agent:\n"
    "  clarify_timeout: 30\n"
    "approvals:\n"
    "  timeout: 300\n"
)
PROFILE_BAD_VALUE = "# broken fixture: non-numeric prior\nagent:\n  clarify_timeout: soon\n"
PROFILE_EMPTY_COLON = "# broken fixture: nothing after the colon\napprovals:\n  timeout:\n"
PROFILE_FLOW_EMPTY = "# flow fixture: empty agent map, plain approvals\nagent: {}\napprovals:\n  timeout: 600\n"
PROFILE_FLOW_LEAF = (
    "# flow fixture: leaf named inside the flow map\n"
    "agent: {clarify_timeout: 90}\n"
)
PROFILE_DEEP_LEAF = (
    "# broken fixture: the leaf sits deeper than the direct children\n"
    "agent:\n"
    "  sub:\n"
    "    clarify_timeout: 90\n"
)
PROFILE_DEEP_BLOCK = (
    "# nested-block fixture: a map opens under the section but never names the leaf\n"
    "approvals:\n"
    "  sub:\n"
    "    x: 1\n"
)
PROFILE_NESTED_WITH_SIBLING_LEAF = (
    "# real-coder shape: nested map plus a direct sibling leaf at child indent\n"
    "agent:\n"
    "  personalities:\n"
    "    helpful: You are helpful.\n"
    "    concise: Keep it brief.\n"
    "  clarify_timeout: 84\n"
)
PROFILE_INDENT4 = (
    "# four-space children fixture: the first child fixes the child indent\n"
    "agent:\n"
    "    clarify_timeout: 71\n"
    "approvals:\n"
    "    timeout: 700\n"
)

PROFILE_SHALLOW_LEAF = (
    "# broken fixture: a leaf-shaped line shallower than the direct children\n"
    "agent:\n"
    "    sub:\n"
    "      x: 1\n"
    "  clarify_timeout: 90\n"
)
PROFILE_DEEP_LEAF_WITH_DIRECT = (
    "# broken fixture: a direct leaf plus a deeper leaf-shaped line\n"
    "agent:\n"
    "  clarify_timeout: 90\n"
    "  sub:\n"
    "    clarify_timeout: 99\n"
)

FAKE_HERMES_SOURCE = """#!/usr/bin/env python3
# Fake hermes config CLI for the set-wait-timeout tests (record + simulate).
import json
import os
import sys

LOG = os.environ["FAKE_HERMES_LOG"]
STATE = os.environ["FAKE_HERMES_STATE"]


def load():
    if os.path.exists(STATE):
        with open(STATE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def main():
    args = sys.argv[1:]
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(args) + "\\n")
    profile = None
    if len(args) >= 2 and args[0] == "-p":
        profile = args[1]
        args = args[2:]
    if profile is None:
        print("fake hermes: expected -p PROFILE", file=sys.stderr)
        return 2
    if not (args and args[0] == "config" and len(args) >= 3
            and args[1] in ("set", "get", "unset")):
        print("fake hermes: unsupported call: %r" % (args,), file=sys.stderr)
        return 2
    sub, key = args[1], args[2]
    if sub == "set" and len(args) != 4:
        print("fake hermes: set needs a value: %r" % (args,), file=sys.stderr)
        return 2
    if sub != "set" and len(args) != 3:
        print("fake hermes: %s takes no value: %r" % (sub, args), file=sys.stderr)
        return 2
    state = load()
    bucket = state.setdefault(profile, {})
    if sub == "set":
        bucket[key] = args[3]
        save(state)
        print("OK Set %s = %s" % (key, args[3]))
        return 0
    if sub == "get":
        if key in bucket:
            print(bucket[key])
            return 0
        print("Config key not set: %s" % key, file=sys.stderr)
        return 1
    if key in bucket:
        del bucket[key]
        save(state)
        print("OK Unset %s" % key)
        return 0
    print("Config key not set: %s" % key, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""


def profile_ops(calls, profile, sub, key):
    """Recorded fake-hermes argv tail values for one profile/op/key."""
    values = []
    for argv in calls:
        if (len(argv) >= 5 and argv[0] == "-p" and argv[1] == profile
                and argv[2] == "config" and argv[3] == sub and argv[4] == key):
            if sub == "set":
                values.append(argv[5])
            else:
                values.append(sub)
    return values


def locale_available(name):
    """True when the system's `locale -a` listing carries <name>, compared
    dash/underscore-insensitively (glibc lists en_US.utf8 where macOS lists
    en_US.UTF-8 and both setlocale spellings are equivalent)."""
    try:
        proc = subprocess.run(
            ["locale", "-a"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
    except OSError:
        return False

    def key(part):
        return part.lower().replace("-", "").replace("_", "")

    wanted = key(name)
    return any(
        key(line.strip()) == wanted
        for line in proc.stdout.decode("utf-8", "replace").splitlines()
    )


@unittest.skipUnless(shutil.which("bash"), "bash shell not on PATH")
@unittest.skipIf(sys.platform == "win32", "POSIX script; Windows parity is documented, not tested here")
class TestSetWaitTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="g2c-wait-timeout-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profiles = os.path.join(self.tmp, "profiles")
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(self.profiles)
        os.makedirs(self.tmpdir)
        self.log = os.path.join(self.tmp, "hermes-call-log.jsonl")
        self.stub_state = os.path.join(self.tmp, "hermes-state.json")
        self.stub = os.path.join(self.tmp, "stub", "fake-hermes")
        os.makedirs(os.path.dirname(self.stub))
        with open(self.stub, "w", encoding="utf-8") as fh:
            fh.write(FAKE_HERMES_SOURCE)
        # The fake-hermes stub must stay executable: the installer runs it as
        # HERMES_BIN. Test-only fixture, temp dir only.
        os.chmod(self.stub, 0o755)
        self.state_path = ""
        self._last_out = ""
        self._last_err = ""

    # helpers ---------------------------------------------------------------

    def write_profile(self, name, content):
        pdir = os.path.join(self.profiles, name)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "config.yaml"), "w", encoding="utf-8") as fh:
            fh.write(content)

    def write_default_profiles(self):
        self.write_profile("coder", PROFILE_CODER)
        self.write_profile("code-reviewer", PROFILE_CODE_REVIEWER)
        self.write_profile("imilk", PROFILE_IMILK)
        self.write_profile("researcher", PROFILE_RESEARCHER)

    def run_script(self, args, extra_env=None):
        env = os.environ.copy()
        env["HOME"] = self.tmp
        env["TMPDIR"] = self.tmpdir
        env["G2C_PROFILE_ROOT"] = self.profiles
        env["HERMES_BIN"] = self.stub
        env["FAKE_HERMES_LOG"] = self.log
        env["FAKE_HERMES_STATE"] = self.stub_state
        # extra_env lands AFTER the controlled env: the point is to be able
        # to force a hostile locale (LC_ALL/LANG) into the subprocess env,
        # mirroring what the CI runners set.
        for key, value in (extra_env or {}).items():
            env[key] = value
        proc = subprocess.run(
            ["bash", SCRIPT_PATH] + list(args),
            env=env, cwd=self.tmp,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120,
        )
        self._last_out = proc.stdout.decode("utf-8", "replace")
        self._last_err = proc.stderr.decode("utf-8", "replace")
        return proc.returncode

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def assert_no_calls(self, msg=""):
        self.assertEqual(self.calls(), [], msg or "expected zero fake-hermes calls")

    def stub_config(self, profile):
        if not os.path.exists(self.stub_state):
            return {}
        with open(self.stub_state, "r", encoding="utf-8") as fh:
            return json.load(fh).get(profile, {})

    def read_state_records(self):
        if not self.state_path:
            raise AssertionError("test bug: state_path not set")
        with open(self.state_path, "r", encoding="utf-8") as fh:
            lines = [line.strip() for line in fh if line.strip()]
        header = json.loads(lines[0])
        self.assertEqual(header["kind"], "g2c-wait-timeout-state")
        self.assertEqual(header["wait_timeout"], 600)
        records = []
        for line in lines[1:]:
            data = json.loads(line)
            prior = data["prior"]
            records.append((data["profile"], data["key"],
                            None if prior is None else str(prior)))
        return header, records

    # dry run -----------------------------------------------------------------

    def test_dry_run_default_600_makes_no_config_calls(self):
        self.write_default_profiles()
        rc = self.run_script([])
        self.assertEqual(rc, 0, self._last_err)
        self.assertIn("DRY RUN", self._last_out)
        self.assertIn("wait timeout: 600s", self._last_out)
        for prof in ("coder", "code-reviewer", "imilk", "researcher"):
            self.assertIn("-p %s config set %s 600" % (prof, KEY_CLARIFY), self._last_out)
            self.assertIn("-p %s config set %s 600" % (prof, KEY_APPROVALS), self._last_out)
        self.assert_no_calls()
        default_state_dir = os.path.join(self.tmp, ".bfc-pipeline", "run", "g2c")
        self.assertFalse(os.path.exists(default_state_dir),
                         "dry run must not create the state dir")
        self.assertIn("restart: none needed", self._last_out)

    def test_dry_run_reads_priors_from_the_raw_layer(self):
        self.write_default_profiles()
        rc = self.run_script([])
        self.assertEqual(rc, 0)
        self.assertIn("agent.clarify_timeout=90", self._last_out)
        self.assertIn("approvals.timeout=600", self._last_out)
        self.assertIn("legacy clarify.timeout=450", self._last_out)
        self.assertIn("agent.clarify_timeout=120", self._last_out)
        self.assertIn("imilk", self._last_out)
        self.assertIn("researcher", self._last_out)

    def test_dry_run_refuses_a_leaf_deeper_than_the_children(self):
        self.write_profile("solo", PROFILE_DEEP_LEAF)
        rc = self.run_script(["--profiles", "solo"])
        self.assertEqual(rc, 1, self._last_err)
        self.assertIn("refusing fail closed", self._last_err)
        self.assertIn("direct children", self._last_err)
        self.assert_no_calls()

    def test_dry_run_refuses_a_leaf_shallower_than_the_children(self):
        # The child indent comes from the section's first content line; a
        # leaf-shaped line at ANY other indent (deeper or shallower) cannot
        # be agent.clarify_timeout and must refuse fail closed.
        self.write_profile("solo", PROFILE_SHALLOW_LEAF)
        rc = self.run_script(["--profiles", "solo"])
        self.assertEqual(rc, 1, self._last_err)
        self.assertIn("refusing fail closed", self._last_err)
        self.assertIn("direct children", self._last_err)
        self.assert_no_calls()

    def test_nested_block_without_the_leaf_leaves_the_key_absent(self):
        # The real four-profile installs nest maps under the target sections
        # (e.g. coder's agent.personalities) and the brief pins the real dry
        # run as byte-identical: nested content not naming the leaf proves
        # nothing against the direct child, so it stays ignored. A nested
        # LEAF is still refused (see the deeper-leaf tests below).
        self.write_profile("solo", PROFILE_DEEP_BLOCK)
        rc = self.run_script(["--profiles", "solo"])
        self.assertEqual(rc, 0, self._last_err)
        self.assertIn("approvals.timeout=(unset)", self._last_out)
        self.assert_no_calls()

    def test_nested_map_with_a_direct_sibling_leaf_counts_the_leaf(self):
        # The exact shape of the current real coder profile: a nested map
        # under the section plus the leaf as a direct child at the child
        # indent; the sibling leaf must still be read.
        self.write_profile("solo", PROFILE_NESTED_WITH_SIBLING_LEAF)
        rc = self.run_script(["--profiles", "solo"])
        self.assertEqual(rc, 0, self._last_err)
        self.assertIn("agent.clarify_timeout=84", self._last_out)
        self.assert_no_calls()

    def test_children_at_the_first_child_indent_still_count(self):
        self.write_profile("solo", PROFILE_INDENT4)
        rc = self.run_script(["--profiles", "solo"])
        self.assertEqual(rc, 0, self._last_err)
        self.assertIn("agent.clarify_timeout=71", self._last_out)
        self.assertIn("approvals.timeout=700", self._last_out)

    # apply -------------------------------------------------------------------

    def test_apply_with_also_legacy_sets_verifies_and_records(self):
        self.write_default_profiles()
        self.state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--state", self.state_path, "--also-legacy"])
        self.assertEqual(rc, 0, self._last_err)
        calls = self.calls()
        for prof in ("coder", "code-reviewer", "imilk", "researcher"):
            self.assertEqual(profile_ops(calls, prof, "set", KEY_CLARIFY), ["600"])
            self.assertEqual(profile_ops(calls, prof, "set", KEY_APPROVALS), ["600"])
            self.assertEqual(len(profile_ops(calls, prof, "get", KEY_CLARIFY)), 1,
                             "each profile is verified with one config get per key")
            self.assertEqual(len(profile_ops(calls, prof, "get", KEY_APPROVALS)), 1)
        self.assertEqual(profile_ops(calls, "coder", "set", KEY_LEGACY), ["600"])
        header, records = self.read_state_records()
        self.assertEqual(records, [
            ("coder", KEY_CLARIFY, "90"),
            ("coder", KEY_APPROVALS, "600"),
            ("coder", KEY_LEGACY, "450"),
            ("code-reviewer", KEY_CLARIFY, "120"),
            ("code-reviewer", KEY_APPROVALS, "600"),
            ("imilk", KEY_CLARIFY, None),
            ("imilk", KEY_APPROVALS, None),
            ("researcher", KEY_CLARIFY, None),
            ("researcher", KEY_APPROVALS, None),
        ])
        mode = stat.S_IMODE(os.stat(self.state_path).st_mode)
        self.assertEqual(mode, 0o600, "state file must be 0600")
        self.assertIn("verify ok", self._last_out)
        self.assertIn("rollback later", self._last_out)
        self.assertIn("restart: none needed", self._last_out)

    def test_apply_refuses_legacy_without_also_legacy(self):
        self.write_default_profiles()
        state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--state", state_path])
        self.assertEqual(rc, 1, self._last_err)
        self.assertIn("refusing to apply", self._last_err)
        self.assertIn("clarify.timeout=450", self._last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))

    def test_invalid_wait_timeout_values_exit_2_and_do_nothing(self):
        self.write_default_profiles()
        state_path = os.path.join(self.tmp, "state.jsonl")
        for bad in ["", "abc", "0", "059", "59", "86401", "12345678901234567890"]:
            rc = self.run_script(["--apply", "--state", state_path, "--wait-timeout", bad])
            self.assertEqual(rc, 2, repr(bad))
            self.assertIn("FAIL", self._last_err)
            self.assertFalse(os.path.exists(state_path), repr(bad))

    def test_profile_name_validation_rejects_bad_lists(self):
        self.write_default_profiles()
        for bad in ["A,b", "a,,b", "a,a", "a b", "a/.."]:
            rc = self.run_script(["--apply", "--profiles", bad])
            self.assertEqual(rc, 2, repr(bad))
            self.assertIn("FAIL", self._last_err)
        self.assert_no_calls()

    def test_profile_name_validation_under_en_us_utf8_locale(self):
        # CI regression (run 44902030637, both macos-latest legs): bash case
        # bracket RANGES collate by locale, so under LC_ALL=en_US.UTF-8 the
        # old [!a-z0-9-] class admitted 'A' and 'A,b' validated (surfacing
        # only as a later missing-profile-dir refusal, rc 1). The script now
        # pins LC_ALL=C AND enumerates the profile-name class explicitly;
        # this test forces the hostile locale into the subprocess env and
        # demands exit 2 with zero hermes calls.
        if not locale_available("en_US.UTF-8"):
            self.skipTest("en_US.UTF-8 locale not available on this machine")
        self.write_default_profiles()
        for bad in ["A,b", "Code-Reviewer", "abC9-"]:
            rc = self.run_script(
                ["--apply", "--profiles", bad],
                extra_env={"LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"},
            )
            self.assertEqual(rc, 2, repr(bad))
            self.assertIn("invalid profile name", self._last_err)
            self.assertIn("FAIL", self._last_err)
        self.assert_no_calls()

    def test_profiles_starting_with_a_dash_are_rejected(self):
        self.write_default_profiles()
        rc = self.run_script(["--profiles", "--apply"])
        self.assertEqual(rc, 2, self._last_err)
        self.assertIn("must not start with '-'", self._last_err)
        self.assert_no_calls()
        rc = self.run_script(["--apply", "--profiles", "--apply"])
        self.assertEqual(rc, 2, self._last_err)
        self.assert_no_calls()
        rc = self.run_script(["--apply", "--profiles", "coder,--imilk"])
        self.assertEqual(rc, 2, self._last_err)
        self.assert_no_calls()

    def test_empty_profile_item_from_a_trailing_comma_is_rejected(self):
        self.write_default_profiles()
        rc = self.run_script(["--apply", "--profiles", "code-reviewer,"])
        self.assertEqual(rc, 2, self._last_err)
        self.assertIn("invalid profile name ''", self._last_err)
        self.assert_no_calls()

    def test_state_flag_without_apply_is_rejected(self):
        self.write_default_profiles()
        rc = self.run_script(["--state", os.path.join(self.tmp, "s.jsonl")])
        self.assertEqual(rc, 2)

    def test_apply_refuses_undecidable_prior_value(self):
        self.write_profile("solo", PROFILE_BAD_VALUE)
        rc = self.run_script(["--apply", "--profiles", "solo"])
        self.assertEqual(rc, 1, self._last_err)
        self.assertIn("refusing fail closed", self._last_err)
        self.assert_no_calls()

    def test_apply_refuses_null_like_colon_value(self):
        self.write_profile("solo", PROFILE_EMPTY_COLON)
        rc = self.run_script(["--apply", "--profiles", "solo"])
        self.assertEqual(rc, 1)
        self.assertIn("nothing after the colon", self._last_err)

    def test_apply_refuses_a_leaf_deeper_than_the_children(self):
        self.write_profile("solo", PROFILE_DEEP_LEAF)
        state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--profiles", "solo", "--state", state_path])
        self.assertEqual(rc, 1, self._last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))

    def test_apply_refuses_a_deeper_leaf_alongside_a_present_direct_leaf(self):
        # The direct leaf is readable, but a deeper leaf-shaped line under
        # the same section still refuses: it can never be the direct
        # child's prior value, and ambiguity loses to refusal.
        self.write_profile("solo", PROFILE_DEEP_LEAF_WITH_DIRECT)
        state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--profiles", "solo", "--state", state_path])
        self.assertEqual(rc, 1, self._last_err)
        self.assertIn("refusing fail closed", self._last_err)
        self.assertIn("direct children", self._last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))

    def test_empty_flow_section_counts_as_absent(self):
        self.write_profile("solo", PROFILE_FLOW_EMPTY)
        self.state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--profiles", "solo", "--state", self.state_path])
        self.assertEqual(rc, 0, self._last_err)
        _, records = self.read_state_records()
        self.assertEqual(records, [
            ("solo", KEY_CLARIFY, None),
            ("solo", KEY_APPROVALS, "600"),
        ])

    def test_flow_section_naming_the_leaf_refuses(self):
        self.write_profile("solo", PROFILE_FLOW_LEAF)
        rc = self.run_script(["--apply", "--profiles", "solo"])
        self.assertEqual(rc, 1)
        self.assertIn("flow section value mentions", self._last_err)
        self.assert_no_calls()

    def test_apply_refuses_a_missing_profile_dir(self):
        self.write_profile("alpha", PROFILE_ALPHA)
        state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--profiles", "alpha,missing-p", "--state", state_path])
        self.assertEqual(rc, 1, self._last_err)
        self.assertIn("profile dir", self._last_err)
        self.assertIn("missing-p", self._last_err)
        self.assert_no_calls()
        self.assertFalse(os.path.exists(state_path))
        rc = self.run_script(["--profiles", "alpha,missing-p"])
        self.assertEqual(rc, 0, self._last_err)
        self.assertIn("profile dir missing", self._last_out)
        self.assertIn("agent.clarify_timeout=30", self._last_out)

    def test_dry_run_skips_commands_for_a_missing_profile_dir(self):
        # Note A: the printed command list must match what --apply would do.
        # --apply refuses the whole batch when a profile dir is missing, so
        # the dry run prints the same "profile dir missing" note in the
        # commands section instead of that profile's commands.
        self.write_profile("alpha", PROFILE_ALPHA)
        rc = self.run_script(["--profiles", "alpha,missing-p"])
        self.assertEqual(rc, 0, self._last_err)
        self.assertEqual(self._last_out.count("profile dir missing"), 2,
                         "the note appears once in the priors and once in the commands section")
        self.assertNotIn("-p missing-p config", self._last_out)
        self.assertIn("-p alpha config set %s 600" % KEY_CLARIFY, self._last_out)
        self.assert_no_calls()

    def test_custom_profile_list_applies_when_all_profile_dirs_exist(self):
        self.write_profile("alpha", PROFILE_ALPHA)
        self.write_profile("b-2", PROFILE_IMILK)
        state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--profiles", "alpha,b-2", "--state", state_path])
        self.assertEqual(rc, 0, self._last_err)
        calls = self.calls()
        self.assertEqual(profile_ops(calls, "alpha", "set", KEY_CLARIFY), ["600"])
        self.assertEqual(profile_ops(calls, "b-2", "set", KEY_APPROVALS), ["600"])
        self.assertEqual(profile_ops(calls, "coder", "set", KEY_CLARIFY), [],
                         "only the named profiles get touched")
        self.assertEqual(profile_ops(calls, "b-1", "set", KEY_CLARIFY), [],
                         "a profile never named is never touched")
        self.state_path = state_path
        _, records = self.read_state_records()
        self.assertEqual(records, [
            ("alpha", KEY_CLARIFY, "30"),
            ("alpha", KEY_APPROVALS, "300"),
            ("b-2", KEY_CLARIFY, None),
            ("b-2", KEY_APPROVALS, None),
        ])

    # rollback ----------------------------------------------------------------

    def test_rollback_restores_prior_values_including_unset(self):
        self.write_default_profiles()
        self.state_path = os.path.join(self.tmp, "state.jsonl")
        rc = self.run_script(["--apply", "--state", self.state_path, "--also-legacy"])
        self.assertEqual(rc, 0, self._last_err)
        self.assertEqual(self.stub_config("coder"),
                         {KEY_CLARIFY: "600", KEY_APPROVALS: "600", KEY_LEGACY: "600"})
        self.assertEqual(self.stub_config("imilk"),
                         {KEY_CLARIFY: "600", KEY_APPROVALS: "600"})
        calls_before = len(self.calls())
        rc = self.run_script(["--rollback", self.state_path])
        self.assertEqual(rc, 0, self._last_err)
        calls = self.calls()[calls_before:]
        self.assertEqual(profile_ops(calls, "coder", "set", KEY_CLARIFY), ["90"])
        self.assertEqual(profile_ops(calls, "coder", "set", KEY_APPROVALS), ["600"])
        self.assertEqual(profile_ops(calls, "coder", "set", KEY_LEGACY), ["450"])
        self.assertEqual(profile_ops(calls, "code-reviewer", "set", KEY_CLARIFY), ["120"])
        self.assertEqual(profile_ops(calls, "imilk", "unset", KEY_CLARIFY), ["unset"])
        self.assertEqual(profile_ops(calls, "imilk", "unset", KEY_APPROVALS), ["unset"])
        self.assertEqual(profile_ops(calls, "researcher", "unset", KEY_CLARIFY), ["unset"])
        self.assertEqual(self.stub_config("coder"),
                         {KEY_CLARIFY: "90", KEY_APPROVALS: "600", KEY_LEGACY: "450"})
        self.assertEqual(self.stub_config("imilk"), {})
        self.assertEqual(self.stub_config("researcher"), {})
        self.assertIn("rollback done", self._last_out)

    def test_rollback_tolerates_already_unset_keys(self):
        self.state_path = os.path.join(self.tmp, "hand-made.jsonl")
        with open(self.state_path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "g2c-wait-timeout-state", "created_utc": '
                     '"2026-11-20T10:00:00Z", "wait_timeout": 600}\n')
            fh.write('{"profile": "solo", "key": "agent.clarify_timeout", "prior": null}\n')
        rc = self.run_script(["--rollback", self.state_path])
        self.assertEqual(rc, 0, self._last_err)
        self.assertIn("already unset (ok)", self._last_out)
        self.assertEqual(profile_ops(self.calls(), "solo", "unset", KEY_CLARIFY), ["unset"])

    def test_rollback_refuses_a_non_state_file(self):
        junk = os.path.join(self.tmp, "junk.jsonl")
        with open(junk, "w", encoding="utf-8") as fh:
            fh.write("not a state file\n")
        rc = self.run_script(["--rollback", junk])
        self.assertEqual(rc, 1)
        self.assertIn("does not look like", self._last_err)

    def test_rollback_refuses_a_malformed_record_line(self):
        self.state_path = os.path.join(self.tmp, "malformed.jsonl")
        with open(self.state_path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "g2c-wait-timeout-state", "created_utc": '
                     '"2026-11-20T10:00:00Z", "wait_timeout": 600}\n')
            fh.write('{"profile": "solo", "key": "evil.key", "prior": 1}\n')
        rc = self.run_script(["--rollback", self.state_path])
        self.assertEqual(rc, 1)
        self.assertIn("not understood", self._last_err)

    def test_rollback_refuses_a_kind_substring_line(self):
        # N3: the header skip is the exact header record only. A line that
        # merely contains the kind text is a malformed record and must be
        # refused, never silently skipped (an incomplete rollback is worse
        # than none).
        self.state_path = os.path.join(self.tmp, "kind-substring.jsonl")
        with open(self.state_path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "g2c-wait-timeout-state", "created_utc": '
                     '"2026-11-20T10:00:00Z", "wait_timeout": 600}\n')
            fh.write('{"kind": "g2c-wait-timeout-state", "bogus": true}\n')
            fh.write('{"profile": "solo", "key": "agent.clarify_timeout", "prior": 1}\n')
        rc = self.run_script(["--rollback", self.state_path])
        self.assertEqual(rc, 1, self._last_err)
        self.assertIn("not understood", self._last_err)
        self.assert_no_calls()

    def test_rollback_refuses_a_state_file_without_a_final_newline(self):
        self.state_path = os.path.join(self.tmp, "truncated.jsonl")
        with open(self.state_path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "g2c-wait-timeout-state", "created_utc": '
                     '"2026-11-20T10:00:00Z", "wait_timeout": 600}\n')
            fh.write('{"profile": "solo", "key": "agent.clarify_timeout", "prior": 1}')
        rc = self.run_script(["--rollback", self.state_path])
        self.assertEqual(rc, 1)
        self.assertIn("does not end with a newline", self._last_err)
        self.assert_no_calls()

    def test_rollback_rejects_conflicting_flags(self):
        path = os.path.join(self.tmp, "state.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"kind": "g2c-wait-timeout-state", "created_utc": '
                     '"2026-11-20T10:00:00Z", "wait_timeout": 600}\n')
        rc = self.run_script(["--apply", "--rollback", path])
        self.assertEqual(rc, 2)
        rc = self.run_script(["--rollback", path, "--also-legacy"])
        self.assertEqual(rc, 2)
        self.assert_no_calls()

    def test_apply_refuses_to_overwrite_a_state_file(self):
        self.write_profile("solo", PROFILE_ALPHA)
        self.state_path = os.path.join(self.tmp, "state.jsonl")
        self.run_script(["--apply", "--profiles", "solo", "--state", self.state_path])
        rc = self.run_script(["--apply", "--profiles", "solo", "--state", self.state_path])
        self.assertEqual(rc, 1)
        self.assertIn("refusing to overwrite", self._last_err)


if __name__ == "__main__":
    unittest.main()
