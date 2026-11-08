"""Fixture matrix tests for g2h against the mock gateway.

Each test maps to one numbered fixture from the implementation brief
(section 10) and runs the real client as a subprocess against a fresh
MockHermes instance. All api keys are throwaway strings generated per run.
Example fixtures only: statuses, delays and behaviors are injected per test.
"""

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Optional

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)

import mock_hermes  # noqa: E402
from mock_hermes import MockHermes  # noqa: E402

G2H_PY = os.path.join(REPO_ROOT, "g2h.py")

LEDGER_NAME = "approvals.json"
EXPIRY_LABEL = ("derived: first_seen_at + 300s assumed "
                "(gateway config not visible)")

KEY_ENV_VARS = ("G2H_API_KEY", "G2H_BASE_URL", "G2H_RECEIPT_DIR",
                "G2H_PROFILE", "G2H_OUTPUT_CAP", "G2H_NOTIFY_URL",
                "G2H_NOTIFY_CMD", "G2H_NOTIFY_TOKEN", "G2H_DASHBOARD_PORT")


if TYPE_CHECKING:
    from typing import Callable


class _Harness(object):
    """Shared harness: fresh mock, fresh scratch receipt dir, subprocess.

    Note: the researcher profile name is used because it already appears in
    upstream Hermes documentation as an example profile; it mocks locally.

    Deliberately not a unittest.TestCase at runtime: the two matrix classes
    below mix it in, so this plumbing exists exactly once. The earlier
    DashboardRelayCase(FixtureCase) inheritance re-ran all 24 original
    tests a second time, doubling wall time without adding assurance.
    """

    maxDiff = None
    mock: Optional[MockHermes]

    if TYPE_CHECKING:
        # The runtime base is object; only these TestCase members are used
        # by the harness helpers, declared so the checker sees them.
        assertTrue: Callable[..., None]
        assertEqual: Callable[..., None]
        assertNotIn: Callable[..., None]

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="g2h-fixture-")
        self.receipts = os.path.join(self.ws, "receipts")
        self.key = "k_" + secrets.token_urlsafe(24)
        self.researcher_key = "rk_" + secrets.token_urlsafe(24)
        self.mock = None
        self.start_mock()

    def tearDown(self):
        if self.mock is not None:
            self.mock.stop()
        shutil.rmtree(self.ws, ignore_errors=True)

    def start_mock(self, **overrides):
        """(Re)create the mock gateway, optionally with per-test behavior."""
        if self.mock is not None:
            self.mock.stop()
        self.mock = MockHermes(self.key,
                               profile_keys={"researcher": self.researcher_key},
                               **overrides)
        return self.mock

    def client(self, *args, key=None, timeout=120, env_extra=None):
        env = dict(os.environ)
        for name in KEY_ENV_VARS:
            env.pop(name, None)
        env["G2H_BASE_URL"] = self.mock.base_url
        env["G2H_API_KEY"] = key if key is not None else self.key
        env["G2H_RECEIPT_DIR"] = self.receipts
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(env_extra or {})
        return subprocess.run([sys.executable, G2H_PY] + list(args),
                              capture_output=True, text=True, env=env,
                              timeout=timeout)

    def json_out(self, proc):
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, "expected --json output, got: %r / %r"
                        % (proc.stdout[-2000:], proc.stderr[-2000:]))
        return json.loads(lines[-1])

    def receipt_files(self):
        if not os.path.isdir(self.receipts):
            return []
        return sorted(os.path.join(self.receipts, name)
                      for name in os.listdir(self.receipts)
                      if name.endswith(".json"))

    def assert_mode(self, path, expected):
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), expected,
                         "%s has mode %s" % (path, oct(os.stat(path).st_mode)))

    def assert_key_absent_everywhere(self, proc, key):
        blobs = [proc.stdout, proc.stderr]
        for path in self.receipt_files():
            with open(path, "r", encoding="utf-8") as handle:
                blobs.append(handle.read())
        combined = "\n".join(blobs)
        self.assertNotIn(key, combined)

    def read_receipt(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)


class FixtureCase(_Harness, unittest.TestCase):
    """Original fixture matrix: dispatch, poll, exit codes, key handling."""

    # -----------------------------------------------------------------
    # Fixture 1: happy path dispatch, poll, terminal completed.
    # -----------------------------------------------------------------

    def test_01_happy_path_dispatch_polls_to_completed(self):
        proc = self.client("dispatch", "--json", "--input",
                           "hello from the fixture matrix")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.json_out(proc)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["run_id"].startswith("run_mock_"))
        self.assertFalse(payload["idempotency_replayed"])
        self.assertEqual(payload["output"], "MOCK_RUN_OUTPUT")
        self.assertEqual(payload["usage"]["total_tokens"], 30)
        self.assertEqual(self.mock.config.create_attempts, 1)
        self.assertEqual(len(self.mock.config.runs), 1)

        receipt_path = payload["receipt_path"]
        self.assertTrue(os.path.isfile(receipt_path))
        self.assertEqual(receipt_path, os.path.join(
            self.receipts, os.path.basename(receipt_path)))
        self.assert_mode(self.receipts, 0o700)
        self.assert_mode(receipt_path, 0o600)
        doc = self.read_receipt(receipt_path)
        self.assertEqual(doc["run_id"], payload["run_id"])
        self.assertEqual(doc["status"], "completed")
        self.assertEqual(doc["last_output"], "MOCK_RUN_OUTPUT")
        self.assertEqual(doc["profile"], "default")
        self.assertEqual(doc["base_url"], self.mock.base_url)
        self.assertEqual(len(doc["body_sha256"]), 64)
        self.assertGreaterEqual(len(doc["idempotency_key"]), 32)

    # -----------------------------------------------------------------
    # Fixture 2: idempotent replay, same key and same body.
    # -----------------------------------------------------------------

    def test_02_idempotent_replay_same_key_and_body(self):
        idem = "g2h-idem-key-" + secrets.token_hex(8)
        first = self.client("dispatch", "--json", "--input", "same body",
                            "--idempotency-key", idem)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.client("dispatch", "--json", "--input", "same body",
                             "--idempotency-key", idem)
        self.assertEqual(second.returncode, 0, second.stderr)
        a, b = self.json_out(first), self.json_out(second)
        self.assertFalse(a["idempotency_replayed"])
        self.assertTrue(b["idempotency_replayed"])
        self.assertEqual(a["run_id"], b["run_id"])
        self.assertEqual(self.mock.config.create_attempts, 2)
        self.assertEqual(len(self.mock.config.runs), 1)
        self.assertIn("idempotency_replayed",
                      [d["code"] for d in b["diagnostics"]])

    # -----------------------------------------------------------------
    # Fixture 3: same key, different payload, exit 12.
    # -----------------------------------------------------------------

    def test_03_idempotency_conflict_exit_12(self):
        idem = "g2h-idem-key-" + secrets.token_hex(8)
        first = self.client("dispatch", "--json", "--input", "body one",
                            "--idempotency-key", idem)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.client("dispatch", "--json", "--input", "body two",
                             "--idempotency-key", idem)
        self.assertEqual(second.returncode, 12)
        self.assertIn("idempotency_key_conflict", second.stderr)
        self.assertEqual(self.mock.config.create_attempts, 2)
        self.assertEqual(len(self.mock.config.runs), 1)

    # -----------------------------------------------------------------
    # Fixture 4: wrong key on a named profile prefix, exit 10, scrubbed.
    # -----------------------------------------------------------------

    def test_04_wrong_key_401_exit_10_scrubbed(self):
        proc = self.client("dispatch", "--json", "--input", "auth probe",
                           "--profile", "researcher")
        self.assertEqual(proc.returncode, 10)
        self.assertIn("auth rejected", proc.stderr)
        self.assert_key_absent_everywhere(proc, self.key)

    # -----------------------------------------------------------------
    # Fixture 5: valid key, another profile's run id, exit 11.
    # -----------------------------------------------------------------

    def test_05_wrong_profile_404_exit_11(self):
        first = self.client("dispatch", "--json", "--no-wait", "--input",
                            "run on the default profile")
        self.assertEqual(first.returncode, 0, first.stderr)
        run_id = self.json_out(first)["run_id"]
        proc = self.client("status", "--profile", "researcher", run_id,
                           key=self.researcher_key)
        self.assertEqual(proc.returncode, 11)
        self.assertIn("profile", proc.stderr)
        self.assertNotIn(self.researcher_key, proc.stderr)
        self.assertNotIn(self.researcher_key, proc.stdout)

    # -----------------------------------------------------------------
    # Fixture 6: 429 then success with Retry-After 1, no duplicate run.
    # -----------------------------------------------------------------

    def test_06_rate_limited_429_retry_after_honored(self):
        self.start_mock(create_failures=[(429, "too_many_concurrent", 1)])
        proc = self.client("dispatch", "--json", "--input", "throttle me")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        times = self.mock.config.create_times
        self.assertEqual(len(times), 2)
        self.assertGreaterEqual(times[1] - times[0], 0.9)
        payload = self.json_out(proc)
        texts = " ".join(d["code"] + " " + d["message"]
                         for d in payload["diagnostics"])
        self.assertIn("too_many_concurrent", texts)
        self.assertEqual(len(self.mock.config.runs), 1)

    # -----------------------------------------------------------------
    # Fixture 7: 503 runtime_draining then success, reason in diagnostics.
    # -----------------------------------------------------------------

    def test_07_503_draining_reason_in_diagnostics(self):
        self.start_mock(create_failures=[(503, "runtime_draining", None)])
        proc = self.client("dispatch", "--json", "--input", "drain probe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.json_out(proc)
        codes = [d["code"] for d in payload["diagnostics"]]
        self.assertIn("runtime_draining", codes)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(len(self.mock.config.runs), 1)

    # -----------------------------------------------------------------
    # Fixture 8: unknown-to-client status, keeps polling, final state.
    # -----------------------------------------------------------------

    def test_08_unknown_status_then_terminal(self):
        # "unknown" is documented and non-terminal: no warning, keep polling.
        # "teleported" is deliberately undocumented: 5.7 warns once (never
        # guesses), keeps polling, then the run reaches its terminal state.
        self.start_mock(default_seq=["unknown", "teleported", "completed"])
        proc = self.client("dispatch", "--json", "--input", "odd status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.json_out(proc)
        self.assertEqual(payload["status"], "completed")
        self.assertIn("unknown_status",
                      [d["code"] for d in payload["diagnostics"]])
        self.assertIn("teleported",
                      " ".join(d["message"] for d in payload["diagnostics"]))
        doc = self.read_receipt(payload["receipt_path"])
        self.assertEqual(doc["status"], "completed")
        self.assertEqual(doc["admission"]["admission_id"],
                         self.mock.config.admission_id)

    # -----------------------------------------------------------------
    # Fixture 9: stop settles cancelled, exit 6.
    # -----------------------------------------------------------------

    def test_09_stop_settles_cancelled(self):
        first = self.client("dispatch", "--json", "--no-wait", "--input",
                            "long running task")
        self.assertEqual(first.returncode, 0, first.stderr)
        run_id = self.json_out(first)["run_id"]
        proc = self.client("stop", "--json", run_id)
        self.assertEqual(proc.returncode, 6)
        payload = self.json_out(proc)
        self.assertEqual(payload["status"], "cancelled")
        self.assertEqual(self.mock.config.stop_requests, 1)

    # -----------------------------------------------------------------
    # Fixture 10: SSE drops, reconnect, fallback to polling, terminal.
    # -----------------------------------------------------------------

    def test_10_sse_drops_then_polling_fallback(self):
        self.start_mock(events_queue=["drop_immediate", "drop_after_event",
                                      "drop_immediate"])
        proc = self.client("dispatch", "--json", "--follow", "--input",
                           "stream me")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.json_out(proc)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["output"], "MOCK_RUN_OUTPUT")
        codes = [d["code"] for d in payload["diagnostics"]]
        self.assertIn("sse_fallback", codes)
        self.assertEqual(len(self.mock.config.runs), 1)

    # -----------------------------------------------------------------
    # Fixture 11: never terminal, exit 14 at --timeout, receipt holds it.
    # -----------------------------------------------------------------

    def test_11_timeout_exit_14_receipt_last_status(self):
        self.start_mock(default_seq=["running"])
        proc = self.client("dispatch", "--json", "--input", "run forever",
                           "--timeout", "0.7")
        self.assertEqual(proc.returncode, 14)
        self.assertIn("timeout", proc.stderr)
        payload = self.json_out(proc)
        self.assertEqual(payload["last_status"], "running")
        doc = self.read_receipt(payload["receipt_path"])
        self.assertEqual(doc["status"], "running")

    # -----------------------------------------------------------------
    # Fixture 12: crash resume re-POSTs stored bytes and key.
    # -----------------------------------------------------------------

    def test_12_crash_resume_replays_original_run(self):
        idem = "g2h-idem-key-" + secrets.token_hex(8)
        first = self.client("dispatch", "--json", "--no-wait", "--input",
                            "crash resume payload", "--idempotency-key", idem)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = self.json_out(first)
        receipt_path = original["receipt_path"]
        doc = self.read_receipt(receipt_path)
        self.assertEqual(doc["run_id"], original["run_id"])
        # Simulate a kill before the create response was recorded.
        doc["run_id"] = None
        doc["status"] = "pending_create"
        with open(receipt_path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        self.assert_mode(receipt_path, 0o600)
        proc = self.client("wait", "--json", "--receipt", receipt_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.json_out(proc)
        self.assertEqual(payload["run_id"], original["run_id"])
        self.assertTrue(payload["idempotency_replayed"])
        self.assertEqual(self.mock.config.create_attempts, 2)
        self.assertEqual(len(self.mock.config.runs), 1)

    # -----------------------------------------------------------------
    # Fixture 13: key file 0644 / symlink refused, no request sent.
    # -----------------------------------------------------------------

    def test_13_key_file_permissions_and_symlink_refused(self):
        key_file = os.path.join(self.ws, "loose-key.txt")
        with open(key_file, "w", encoding="utf-8") as handle:
            handle.write(self.key + "\n")
        os.chmod(key_file, 0o644)
        proc = self.client("dispatch", "--json", "--key-file", key_file,
                           "--input", "should never launch")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("0600", proc.stderr)
        self.assertEqual(self.mock.config.total_requests, 0)

        target = os.path.join(self.ws, "tight-key.txt")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(self.key + "\n")
        os.chmod(target, 0o600)
        link = os.path.join(self.ws, "key-link.txt")
        os.symlink(target, link)
        proc = self.client("dispatch", "--json", "--key-file", link,
                           "--input", "should never launch")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("symlink", proc.stderr)
        self.assertEqual(self.mock.config.total_requests, 0)

    # -----------------------------------------------------------------
    # Fixture 14: weird key appears nowhere after a run.
    # -----------------------------------------------------------------

    def test_14_redaction_key_nowhere_in_output_or_files(self):
        weird = "Sneaky~K3y!@#$%^&*<>?"
        proc = self.client("dispatch", "--json", "--profile", "researcher",
                           "--input", "redaction probe", key=weird)
        self.assertEqual(proc.returncode, 10)
        self.assert_key_absent_everywhere(proc, weird)
        self.assert_key_absent_everywhere(proc, self.key)
        self.assert_key_absent_everywhere(proc, self.researcher_key)
        for path in self.receipt_files():
            self.assert_mode(path, 0o600)

    # -----------------------------------------------------------------
    # Fixture 15: 409 permission_denied, distinct message, no retry loop.
    # -----------------------------------------------------------------

    def test_15_foreign_session_permission_denied(self):
        self.start_mock(foreign_sessions={"sess_other_surface"})
        proc = self.client("dispatch", "--json", "--input",
                           "turn injection attempt",
                           "--session-id", "sess_other_surface")
        self.assertEqual(proc.returncode, 12)
        self.assertIn("permission_denied", proc.stderr)
        self.assertIn("session", proc.stderr)
        self.assertEqual(self.mock.config.create_attempts, 1)

    # -----------------------------------------------------------------
    # Fixture 16: waiting_for_approval exits 16; manual resolve approval.
    # -----------------------------------------------------------------

    def test_16_waiting_approval_exit_16_then_manual_resolve(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.client("dispatch", "--json", "--input",
                           "needs approval")
        self.assertEqual(proc.returncode, 16)
        payload = self.json_out(proc)
        # The whole stdout stream must stay one JSON document in --json
        # mode: no pretty-printed controls on top of the payload.
        self.assertEqual(json.loads(proc.stdout), payload)
        self.assertEqual(payload["pending_controls"],
                         self.mock.config.pending_controls_sample)
        self.assertIn("resolve approval", proc.stderr)
        self.assertIn("req_mock_pending1", proc.stderr)
        self.assertIn("--generation 4", proc.stderr)
        doc = self.read_receipt(payload["receipt_path"])
        self.assertEqual(doc["status"], "waiting_for_approval")
        self.assertEqual(doc["pending_controls"],
                         self.mock.config.pending_controls_sample)

        run_id = payload["run_id"]
        receipt_path = payload["receipt_path"]
        resolved = self.client("resolve", "--json", "approval", run_id,
                               "--request-id", "req_mock_pending1",
                               "--generation", "4", "--choice", "once")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        waited = self.client("wait", "--json", "--receipt", receipt_path)
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(self.json_out(waited)["status"], "completed")
        self.assertEqual(self.mock.config.create_attempts, 1)

    # -----------------------------------------------------------------
    # Fixture 17: capabilities 404 warns once, dispatch still completes.
    # -----------------------------------------------------------------

    def test_17_capabilities_absent_warns_once_still_dispatches(self):
        self.start_mock(caps_status=404)
        proc = self.client("dispatch", "--json", "--input", "no caps probe")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stderr.count("capabilities probe unavailable (http 404)"),
            1)
        payload = self.json_out(proc)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(self.mock.config.create_attempts, 1)

    # -----------------------------------------------------------------
    # Fixture 18: resolve unknown refusals and the advertised path.
    # -----------------------------------------------------------------

    def test_18a_resolve_unknown_refused_without_capability_or_flag(self):
        proc = self.client("resolve", "--json", "unknown", "run_abc123",
                           "--admission-id", "adm_abc123", "--generation", "1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--i-acknowledge-lost-execution", proc.stderr)
        self.assertEqual(self.mock.config.total_requests, 0)

    def test_18b_resolve_unknown_stale_generation_then_success(self):
        features = dict(self.mock.config.features)
        features["run_unknown_resolution"] = True
        self.start_mock(default_seq=["unknown"], features=features)
        first = self.client("dispatch", "--json", "--no-wait", "--input",
                            "maybe lost")
        self.assertEqual(first.returncode, 0, first.stderr)
        run_id = self.json_out(first)["run_id"]

        staged = self.client("status", "--json", run_id)
        self.assertEqual(staged.returncode, 0, staged.stderr)
        self.assertEqual(self.json_out(staged)["detail"]["status"], "unknown")

        wrong_gen = self.client(
            "resolve", "--json", "unknown", run_id,
            "--admission-id", self.mock.config.admission_id,
            "--generation", str(self.mock.config.admission_generation + 1),
            "--i-acknowledge-lost-execution")
        self.assertEqual(wrong_gen.returncode, 12)
        self.assertIn("stale_generation", wrong_gen.stderr)

        good = self.client(
            "resolve", "--json", "unknown", run_id,
            "--admission-id", self.mock.config.admission_id,
            "--generation", str(self.mock.config.admission_generation),
            "--i-acknowledge-lost-execution")
        self.assertEqual(good.returncode, 0, good.stderr)

        final = self.client("status", "--json", run_id)
        self.assertEqual(final.returncode, 7)
        self.assertEqual(self.json_out(final)["status"], "interrupted")
        self.assertEqual(self.mock.config.resolve_attempts, 2)

    # -----------------------------------------------------------------
    # Bonus: manual resolve clarify round trip (never auto-answered).
    # -----------------------------------------------------------------

    def test_19_clarify_manual_resolution_round_trip(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.client("dispatch", "--json", "--input",
                           "needs a clarification")
        self.assertEqual(proc.returncode, 16)
        payload = self.json_out(proc)
        run_id = payload["run_id"]
        resolved = self.client("resolve", "--json", "clarify", run_id,
                               "--request-id", "req_mock_pending1",
                               "--generation", "4",
                               "--answer", "The answer is 42")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        waited = self.client("wait", "--json", "--receipt",
                             payload["receipt_path"])
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertEqual(self.json_out(waited)["status"], "completed")
        self.assertEqual(self.mock.config.create_attempts, 1)

    # -----------------------------------------------------------------
    # Gateway down: connection refused is exit 18, never "a g2h bug".
    # -----------------------------------------------------------------

    def _closed_port_base_url(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return "http://127.0.0.1:%d" % port

    def test_20_gateway_down_exit_18_dispatch_and_status(self):
        # A refused connection exhausts the retry budget: dispatch is run
        # with --max-retries 1 so the backoff is short; status polls with
        # its built-in budget of 8 and sleeps through the backoff curve.
        base = self._closed_port_base_url()
        dispatch = self.client("dispatch", "--json", "--base-url", base,
                               "--max-retries", "1",
                               "--input", "gateway down probe", timeout=240)
        self.assertEqual(dispatch.returncode, 18, dispatch.stderr)
        payload = self.json_out(dispatch)
        self.assertEqual(payload["exit_code"], 18)
        self.assertIn("reachability", payload["error"])
        self.assertIn("reachability", dispatch.stderr)
        self.assert_key_absent_everywhere(dispatch, self.key)

        status = self.client("status", "--json", "--base-url", base,
                             "run_refused0001", timeout=240)
        self.assertEqual(status.returncode, 18, status.stderr)
        payload = self.json_out(status)
        self.assertEqual(payload["exit_code"], 18)
        self.assertIn("reachability", payload["error"])
        self.assert_key_absent_everywhere(status, self.key)
        self.assertEqual(self.mock.config.total_requests, 0)

    # -----------------------------------------------------------------
    # Mock knob: a first create answering 200 is accepted like 202.
    # -----------------------------------------------------------------

    def test_21_create_200_accepted(self):
        self.start_mock(create_status=200)
        proc = self.client("dispatch", "--json", "--input", "plain 200 create")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.json_out(proc)["status"], "completed")
        self.assertEqual(self.mock.config.create_attempts, 1)
        self.assertEqual(len(self.mock.config.runs), 1)

    # -----------------------------------------------------------------
    # Mock knob: a stop answering 202 is accepted like 200.
    # -----------------------------------------------------------------

    def test_22_stop_202_accepted(self):
        self.start_mock(stop_status=202)
        first = self.client("dispatch", "--json", "--no-wait", "--input",
                            "long running task")
        self.assertEqual(first.returncode, 0, first.stderr)
        run_id = self.json_out(first)["run_id"]
        proc = self.client("stop", "--json", run_id)
        self.assertEqual(proc.returncode, 6, proc.stderr)
        self.assertEqual(self.json_out(proc)["status"], "cancelled")
        self.assertEqual(self.mock.config.stop_requests, 1)

    # -----------------------------------------------------------------
    # --key-stdin together with --stdin is a usage error, reads nothing.
    # -----------------------------------------------------------------

    def test_23_key_stdin_with_stdin_is_usage_error(self):
        proc = self.client("dispatch", "--json", "--key-stdin", "--stdin")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("--key-stdin", proc.stderr)
        self.assertIn("--stdin", proc.stderr)
        self.assertEqual(self.mock.config.total_requests, 0)

# ---------------------------------------------------------------------------
# Section 6 mock fixtures for the read-only dashboard and approval relay.
# Every dispatched input starts with the MOCK_SCENARIO marker so the scripted
# lifecycle is unmistakable, every mock knob lives in mock_hermes.py, and the
# negative never-touch-control-endpoints assertion closes each scenario.
# ---------------------------------------------------------------------------


class DashboardRelayCase(_Harness, unittest.TestCase):
    """Mock fixtures for g2h dashboard and g2h relay (approval lifecycle,
    dedup, notifier ok/fail/timeout, outbox hygiene, never-POST check)."""

    def setUp(self):
        super().setUp()
        self.g2h_py = G2H_PY

    # ----- helpers --------------------------------------------------------

    def scenario(self, marker, **spec):
        """mock knob: script one lifecycle (seq/output/pending_script)."""
        self.mock.config.scenarios[marker] = spec

    def dispatch_scenario(self, marker, text, *extra):
        """Dispatch a run whose input carries the mock scenario marker."""
        inp = mock_hermes.SCENARIO_INPUT_PREFIX + marker + ": " + text
        return self.client("dispatch", "--json", "--input", inp, *extra)

    def clear_recorder(self):
        mock_hermes.clear_recorder(self.mock.config)

    def assert_no_control_posts(self, note):
        mock_hermes.assert_no_control_posts(self.mock.config, note)

    def status_reads(self):
        with self.mock.config.lock:
            rows = list(self.mock.config.recorded)
        return [path for method, path in rows
                if method == "GET" and "/v1/runs/" in path]

    def events_dir(self):
        return os.path.join(self.receipts, "events")

    def event_files(self):
        outdir = self.events_dir()
        if not os.path.isdir(outdir):
            return []
        names = sorted(name for name in os.listdir(outdir)
                       if name.endswith(".json"))
        return [os.path.join(outdir, name) for name in names]

    def read_events(self):
        docs = []
        for path in self.event_files():
            with open(path, "r", encoding="utf-8") as handle:
                docs.append(json.load(handle))
        return docs

    def read_ledger(self):
        path = os.path.join(self.receipts, LEDGER_NAME)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def rewrite_ledger(self, mutate):
        path = os.path.join(self.receipts, LEDGER_NAME)
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        mutate(doc)
        text = json.dumps(doc, sort_keys=True, indent=2)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o600)

    def iso_ago(self, seconds):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(time.time() - seconds))

    def make_script_notifier(self, behavior):
        """Mock notifier command: 'ok' exits 0, 'fail' exits 7,
        'timeout' sleeps beyond the notifier timeout. Records the exact
        JSON it received on stdin."""
        path = os.path.join(self.ws, "mock-notify-%s.py" % behavior)
        record = os.path.join(self.ws, "mock-notify-%s.log" % behavior)
        script = (
            "#!/usr/bin/env python3\n"
            "import sys, time\n"
            "raw = sys.stdin.read()\n"
            "with open(%r, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(raw.strip() + '<<<END>>>' + chr(10))\n"
            "behavior = %r\n"
            "if behavior == 'fail':\n"
            "    sys.stderr.write('mock notifier refusing\\n')\n"
            "    sys.exit(7)\n"
            "if behavior == 'timeout':\n"
            "    time.sleep(15)\n"  # longer than the notifier budget
            "sys.exit(0)\n"
        ) % (record, behavior)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(script)
        os.chmod(path, 0o755)
        return path, record

    def serve(self, *extra_args):
        """Start g2h dashboard --serve on a free loopback port (mock)."""
        port = _free_loopback_port()
        log_path = os.path.join(self.ws, "mock-serve-%d.log" % port)
        env = dict(os.environ)
        for name in KEY_ENV_VARS:
            env.pop(name, None)
        env["G2H_BASE_URL"] = self.mock.base_url
        env["G2H_API_KEY"] = self.key
        env["G2H_RECEIPT_DIR"] = self.receipts
        env["PYTHONIOENCODING"] = "utf-8"
        log_handle = open(log_path, "wb")  # child-only stderr for diagnosis
        proc = subprocess.Popen(
            [sys.executable, self.g2h_py, "dashboard", "--serve",
             "--port", str(port), "--poll-interval", "5"]
            + list(extra_args),
            stdout=subprocess.DEVNULL, stderr=log_handle, env=env)
        log_handle.close()
        self.addCleanup(self._stop_serve, proc)
        deadline = time.time() + 10
        page = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # died at startup; report its stderr below
            try:
                page = self.http(port)
                break
            except (ConnectionError, urllib.error.URLError):
                time.sleep(0.2)
        if page is None:
            self.fail(self._serve_failure(proc, log_path))
        return proc, port

    def _scrub_text(self, text):
        scrubbed = text
        for value in (self.key, getattr(self.mock, "base_url", None)):
            if value and value in scrubbed:
                scrubbed = scrubbed.replace(value, "<scrubbed>")
        return scrubbed

    def _serve_failure(self, proc, log_path):
        state = "still alive" if proc.poll() is None else "exited %s" % proc.poll()
        detail = ""
        try:
            with open(log_path, "r", encoding="utf-8",
                      errors="replace") as handle:
                raw = handle.read()
            tail = self._scrub_text(raw)[-2000:]
            detail = (", log=%s (%d bytes, scrubbed tail follows):\n%s"
                      % (log_path, len(raw), tail))
        except OSError as exc:
            detail = (", log=%s (unreadable: %s)" % (log_path, exc))
        return "mock serve never answered GET / (%s%s)" % (state, detail)

    @staticmethod
    def _stop_serve(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)

    def http(self, port, path="/", method="GET", host=None):
        url = "http://127.0.0.1:%d%s" % (port, path)
        headers = {"Host": host} if host else {}
        request = urllib.request.Request(
            url, data=None if method == "GET" else b"", headers=headers,
            method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (response.status, dict(response.headers),
                        response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except OSError:
                body = ""
            return exc.code, dict(exc.headers), body

    def assert_event_shape(self, doc, kind, run_id=None):
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["tool"], "grokbot2hermes")
        self.assertEqual(doc["kind"], kind)
        if doc["run_id"]:
            self.assertTrue(doc["source"].startswith("GET /v1/runs/"))
        else:
            self.assertTrue(doc["source"].startswith(
                "gateway contact probe (transport errors)"))
        self.assertEqual(len(doc["dedup_key"]), 64)
        if run_id:
            self.assertEqual(doc["run_id"], run_id)

    def relay(self, *extra, env_extra=None):
        return self.client("relay", "--once", "--json", *extra,
                           env_extra=env_extra)

    # -----------------------------------------------------------------
    # Fixture: a completed run -> relay writes no events at all.
    # -----------------------------------------------------------------

    def test_24_relay_mock_completed_run_emits_nothing(self):
        proc = self.client("dispatch", "--json", "--input",
                           "mock completed relay run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.clear_recorder()
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = self.json_out(snap)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["events"], [])
        self.assertEqual(self.event_files(), [])
        ledger = self.read_ledger()
        self.assertTrue(ledger is None or ledger["entries"] == {})
        self.assert_no_control_posts("completed-run relay pass")

    # -----------------------------------------------------------------
    # Fixture: waiting for approval -> approval.requested event, ledger seen.
    # -----------------------------------------------------------------

    def test_25_relay_mock_requested_event_and_ledger_seen(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("WAIT1", "needs mock approval")
        self.assertEqual(proc.returncode, 16)
        self.clear_recorder()
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = self.json_out(snap)
        self.assertEqual([event["kind"] for event in payload["events"]],
                         ["approval.requested"])
        docs = self.read_events()
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assert_event_shape(doc, "approval.requested",
                                self.json_out(proc)["run_id"])
        self.assertEqual(doc["control"]["request_id"], "req_mock_pending1")
        self.assertEqual(doc["control"]["execution_generation"], 4)
        self.assertEqual(doc["control"]["kind"], "mcp_tool_approval")
        self.assertEqual(doc["control"]["expiry"], EXPIRY_LABEL)
        self.assertIn("WAIT1: needs mock approval", doc["task"]["text"])
        ledger = self.read_ledger()
        entry = ledger["entries"][doc["dedup_key"]]
        self.assertEqual(entry["state"], "seen")
        self.assertIsNone(entry["delivered_at"])
        self.assertEqual(entry["attempts"], 0)
        self.assertEqual(entry["request"]["prompt_id"], "req_mock_pending1")
        mode = stat.S_IMODE(os.stat(self.events_dir()).st_mode)
        self.assertEqual(mode, 0o700)
        file_mode = stat.S_IMODE(os.stat(self.event_files()[0]).st_mode)
        self.assertEqual(file_mode, 0o600)
        self.assert_no_control_posts("requested-event relay pass")

    # -----------------------------------------------------------------
    # Fixture: the mock's scripted human answers -> approval.resolved
    # answered, then the run completes -> run.terminal.
    # -----------------------------------------------------------------

    def test_26_relay_mock_resolved_answered_then_terminal(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("ANSWER1", "scripted human answers")
        self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0)
        self.assertEqual([e["kind"] for e in self.json_out(snap)["events"]],
                         ["approval.requested"])
        run_id = self.json_out(proc)["run_id"]
        self.clear_recorder()
        resolve = self.client("resolve", "--json", "approval", run_id,
                              "--request-id", "req_mock_pending1",
                              "--generation", "4", "--choice", "once")
        self.assertEqual(resolve.returncode, 0, resolve.stderr)
        self.clear_recorder()  # the human's POST is sanctioned; relay is not
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        events = self.json_out(snap)["events"]
        self.assertEqual([e["kind"] for e in events],
                         ["approval.resolved"])
        self.assertEqual(events[0]["resolution"], "answered")
        ledger = self.read_ledger()
        resolved = [entry for entry in ledger["entries"].values()
                    if entry["state"] == "resolved"]
        self.assertTrue(resolved)
        self.assertEqual(resolved[0]["resolution"], "answered")
        self.clear_recorder()
        status = self.client("wait", "--json", run_id)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(self.json_out(status)["status"], "completed")
        snap = self.relay()
        self.assertEqual(snap.returncode, 0)
        self.assertEqual([e["kind"] for e in self.json_out(snap)["events"]],
                         ["run.terminal"])
        self.assert_no_control_posts("answered relay passes")

    # -----------------------------------------------------------------
    # Fixture: pending vanishes while waiting -> resolved_unknown.
    # -----------------------------------------------------------------

    def test_27_relay_mock_pending_vanish_resolved_unknown(self):
        sample = self.mock_pending_sample()
        self.scenario("VAGUE", seq=["waiting_for_approval"],
                      pending_script=[sample, sample, []])
        proc = self.dispatch_scenario("VAGUE", "mock pending vanishes")
        self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0)
        self.assertEqual([e["kind"] for e in self.json_out(snap)["events"]],
                         ["approval.requested"])
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        events = self.json_out(snap)["events"]
        self.assertEqual([e["kind"] for e in events], ["approval.resolved"])
        self.assertEqual(events[0]["resolution"], "resolved_unknown")
        ledger = self.read_ledger()
        entry = ledger["entries"][events[0]["dedup_key"]]
        self.assertEqual(entry["state"], "resolved")
        self.assertEqual(entry["resolution"], "resolved_unknown")
        self.assert_no_control_posts("vanish relay passes")

    def mock_pending_sample(self):
        return [dict(entry)
                for entry in self.mock.config.pending_controls_sample]

    # -----------------------------------------------------------------
    # Fixture: run cancelled while pending -> withdrawn + run.terminal.
    # -----------------------------------------------------------------

    def test_28_relay_mock_cancelled_pending_withdrawn(self):
        sample = self.mock_pending_sample()
        self.scenario("CANC1",
                      seq=["waiting_for_approval", "waiting_for_approval",
                           "cancelled"],
                      pending_script=[sample, sample])
        proc = self.dispatch_scenario("CANC1", "mock cancels a pending run")
        self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual([e["kind"] for e in self.json_out(snap)["events"]],
                         ["approval.requested"])
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        events = self.json_out(snap)["events"]
        kinds = sorted(event["kind"] for event in events)
        self.assertEqual(kinds, ["approval.resolved", "run.terminal"])
        resolved = [e for e in events if e["kind"] == "approval.resolved"]
        self.assertEqual(resolved[0]["resolution"], "withdrawn")
        ledger = self.read_ledger()
        entry = ledger["entries"][resolved[0]["dedup_key"]]
        self.assertEqual(entry["resolution"], "withdrawn")
        self.assert_no_control_posts("withdrawn relay pass")

    # -----------------------------------------------------------------
    # Fixture: pending gone after the derived window -> expired (derived).
    # -----------------------------------------------------------------

    def test_29_relay_mock_pending_ages_out_expired(self):
        sample = self.mock_pending_sample()
        self.scenario("EXPIR", seq=["waiting_for_approval"],
                      pending_script=[sample, sample, []])
        proc = self.dispatch_scenario("EXPIR", "mock ages a pending out")
        self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0)
        key = self.json_out(snap)["events"][0]["dedup_key"]

        def age_entry(doc):
            entry = doc["entries"][key]
            entry["first_seen_at"] = self.iso_ago(400)
            entry["last_seen_at"] = self.iso_ago(390)

        self.rewrite_ledger(age_entry)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        events = self.json_out(snap)["events"]
        self.assertEqual([e["kind"] for e in events], ["approval.resolved"])
        self.assertEqual(events[0]["resolution"], "expired")
        ledger = self.read_ledger()
        self.assertEqual(ledger["entries"][key]["resolution"], "expired")
        self.assert_no_control_posts("expired relay pass")

    # -----------------------------------------------------------------
    # Fixture: same prompt at a new execution_generation is a new request.
    # -----------------------------------------------------------------

    def test_30_relay_mock_new_generation_is_a_distinct_request(self):
        sample = self.mock_pending_sample()
        self.assertEqual(sample[0]["execution_generation"], 4)
        bumped = dict(sample[0])
        bumped["execution_generation"] = 5
        self.scenario("GEN2", seq=["waiting_for_approval"],
                      pending_script=[sample, [bumped], [bumped]])
        proc = self.dispatch_scenario("GEN2", "mock re-issues the request")
        self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0,
                         snap.stdout[-400:] + snap.stderr[-400:])
        first = self.json_out(snap)["events"]
        self.assertTrue(first)
        self.assertEqual([e["kind"] for e in first], ["approval.requested"])
        snap = self.relay()
        self.assertEqual(snap.returncode, 0,
                         snap.stdout[-400:] + snap.stderr[-400:])
        events = self.json_out(snap)["events"]
        # Same gen-5 control re-presented: the ledger dedups across resume.
        self.assertEqual([e["kind"] for e in events], [])
        ledger = self.read_ledger()
        entry = ledger["entries"][first[0]["dedup_key"]]
        self.assertEqual(entry["request"]["execution_generation"], 5)
        self.assertEqual(entry["state"], "seen")
        self.assertEqual(len(ledger["entries"]), 1)
        self.assert_no_control_posts("generation relay passes")

    # -----------------------------------------------------------------
    # Fixture: restart mid-wait delivers exactly once (no duplicate).
    # -----------------------------------------------------------------

    def test_31_relay_mock_restart_delivers_exactly_once(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("DEDUP", "mock delivers once only")
        self.assertEqual(proc.returncode, 16)
        script, record = self.make_script_notifier("ok")
        snap = self.relay("--notify-cmd", script)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = self.json_out(snap)
        self.assertEqual(payload["delivered"], 1)
        self.assertEqual(payload["notify_failed"], 0)
        with open(record, "r", encoding="utf-8") as handle:
            blob = handle.read()
        delivered = [part for part in blob.split("<<<END>>>")
                     if part.strip()]
        self.assertEqual(len(delivered), 1)
        event = json.loads(delivered[0])
        self.assertEqual(event["kind"], "approval.requested")
        self.assertNotIn(self.key, blob)
        ledger = self.read_ledger()
        key_file = payload["events"][0]["file"]
        info = ledger["deliveries"][key_file]
        self.assertIsNotNone(info["delivered_at"])
        self.assertIsNone(info["last_error"])
        self.assertEqual(info["attempts"], 1)
        delivered_at = info["delivered_at"]
        self.clear_recorder()
        snap = self.relay("--notify-cmd", script)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = self.json_out(snap)
        self.assertEqual(payload["delivered"], 0)
        self.assertEqual(payload["notify_failed"], 0)
        with open(record, "r", encoding="utf-8") as handle:
            blob = handle.read()
        sends = [part for part in blob.split("<<<END>>>") if part.strip()]
        self.assertEqual(len(sends), 1)
        ledger = self.read_ledger() or {}
        deliveries = ledger.get("deliveries") or {}
        self.assertIn(key_file, deliveries)
        info = deliveries[key_file]
        self.assertEqual(info["delivered_at"], delivered_at)
        self.assertEqual(info["attempts"], 1)
        self.assert_key_absent_everywhere(snap, self.key)
        self.assert_no_control_posts("dedup relay passes")

    # -----------------------------------------------------------------
    # Fixture: failing notifier records notify_failed; retries are only
    # possible through --redeliver.
    # -----------------------------------------------------------------

    def test_32_relay_mock_notify_failed_then_redeliver_once(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("FAIL1", "mock refuses to deliver")
        self.assertEqual(proc.returncode, 16)
        script, _record = self.make_script_notifier("fail")
        snap = self.relay("--notify-cmd", script)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = self.json_out(snap)
        self.assertEqual(payload["delivered"], 0)
        self.assertEqual(payload["notify_failed"], 1)
        key = payload["events"][0]["dedup_key"]
        ledger = self.read_ledger()
        entry = ledger["entries"][key]
        self.assertEqual(entry["state"], "notify_failed")
        self.assertIn("exit 7", entry["last_error"])
        self.clear_recorder()
        snap = self.relay("--notify-cmd", script)  # no --redeliver: skips
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(self.json_out(snap)["notify_failed"], 0)
        self.assertEqual(self.read_ledger()["entries"][key]["attempts"], 1)
        snap = self.relay("--notify-cmd", script, "--redeliver", key[:12])
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = self.json_out(snap)
        self.assertEqual(payload["redeliver"]["failed"], 1)
        self.assertEqual(payload["redeliver"]["delivered"], 0)
        self.assertEqual(self.read_ledger()["entries"][key]["attempts"], 2)
        self.assert_key_absent_everywhere(snap, self.key)
        self.assert_no_control_posts("notify-failed relay passes")

    # -----------------------------------------------------------------
    # Fixture: a hanging notifier times out (mock sleeps 15s vs 10s cap).
    # -----------------------------------------------------------------

    def test_33_relay_mock_notifier_timeout_is_a_failure(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("SLOW1", "mock notifier hangs")
        self.assertEqual(proc.returncode, 16)
        script, _record = self.make_script_notifier("timeout")
        snap = self.relay("--notify-cmd", script)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(self.json_out(snap)["notify_failed"], 1)
        ledger = self.read_ledger()
        entry = next(iter(ledger["entries"].values()))
        self.assertEqual(entry["state"], "notify_failed")
        self.assertIn("timed out", entry["last_error"])
        self.assert_no_control_posts("notifier-timeout relay pass")

    # -----------------------------------------------------------------
    # Fixture: --notify-url without a token refuses before any request.
    # -----------------------------------------------------------------

    def test_34_relay_mock_url_without_token_never_sends(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("TOKN0", "mock checks token rule")
        self.assertEqual(proc.returncode, 16)
        self.clear_recorder()
        with self.mock.config.lock:
            before = self.mock.config.total_requests
        snap = self.relay("--notify-url", self.mock.base_url + "/v1/notify")
        self.assertEqual(snap.returncode, 3, snap.stdout)
        with self.mock.config.lock:
            self.assertEqual(self.mock.config.total_requests, before)
        self.assert_key_absent_everywhere(snap, self.key)
        self.assert_no_control_posts("missing-token relay pass")

    def test_35_relay_mock_url_notifier_ok_and_500(self):
        self.start_mock(default_seq=["waiting_for_approval"],
                        notify_status=200)
        url = self.mock.base_url + "/v1/notify"
        proc = self.dispatch_scenario("URL1", "mock delivers a webhook")
        self.assertEqual(proc.returncode, 16)
        self.clear_recorder()
        snap = self.relay("--notify-url", url,
                          env_extra={"G2H_NOTIFY_TOKEN":
                                     "mock_notify_token_42"})
        self.assertEqual(snap.returncode, 0, snap.stderr)
        payload = self.json_out(snap)
        self.assertEqual(payload["delivered"], 1)
        self.assertEqual(payload["notify_failed"], 0)
        ref = self.read_ledger()
        entry = ref["entries"][payload["events"][0]["dedup_key"]]
        self.assertEqual(entry["state"], "delivered")
        self.assertIsNotNone(entry["delivered_at"])
        self.assertEqual(payload["notifier"].replace("notifier: ", ""),
                         "webhook via G2H_NOTIFY_URL")
        self.assertNotIn(url, snap.stdout)
        self.assertNotIn("mock_notify_token_42", snap.stdout)
        self.assertNotIn("mock_notify_token_42", snap.stderr)
        # second waiting run against a now-failing sink: one attempt, one 500
        proc = self.dispatch_scenario("URL2", "mock webhook refuses")
        self.assertEqual(proc.returncode, 16)
        with self.mock.config.lock:
            self.mock.config.notify_status = 500
        snap = self.relay("--notify-url", url,
                          env_extra={"G2H_NOTIFY_TOKEN":
                                     "mock_notify_token_42"})
        self.assertEqual(snap.returncode, 0, snap.stderr)
        failed = self.json_out(snap)
        self.assertEqual(failed["delivered"], 0)
        self.assertEqual(failed["notify_failed"], 1)
        ref = self.read_ledger()
        entry = ref["entries"][failed["events"][0]["dedup_key"]]
        self.assertEqual(entry["state"], "notify_failed")
        self.assertIn("http 500", entry["last_error"])
        with self.mock.config.lock:
            posts = [(m, p) for (m, p) in self.mock.config.recorded
                     if m == "POST" and p.startswith("/v1/notify")]
        self.assertEqual(len(posts), 2, "exactly one POST per event")
        self.assert_no_control_posts(
            "webhook relay passes (the notify sink is not a control endpoint)")

    def test_36_relay_mock_url_notifier_timeout(self):
        self.start_mock(default_seq=["waiting_for_approval"],
                        notify_status=200, notify_delay=15.0)
        proc = self.dispatch_scenario("SLOWU", "mock webhook hangs")
        self.assertEqual(proc.returncode, 16)
        url = self.mock.base_url + "/v1/notify"
        snap = self.relay("--notify-url", url,
                          env_extra={"G2H_NOTIFY_TOKEN":
                                     "mock_notify_token_42"})
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(self.json_out(snap)["notify_failed"], 1)
        entry = next(iter(self.read_ledger()["entries"].values()))
        self.assertEqual(entry["state"], "notify_failed")
        self.assertIn("timed out", entry["last_error"])
        self.assert_no_control_posts("webhook-timeout relay pass")

    # -----------------------------------------------------------------
    # Fixture: gateway down -> stale plus exactly one unreachable event.
    # -----------------------------------------------------------------

    def test_37_relay_mock_gateway_down_single_unreachable_event(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("DOWN1", "mock gateway goes away")
        self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0)
        self.assertEqual([e["kind"] for e in self.json_out(snap)["events"]],
                         ["approval.requested"])
        self.mock.stop()
        snap = self.relay()
        self.assertEqual(snap.returncode, 18, "every contact failed")
        payload = self.json_out(snap)
        self.assertEqual([e["kind"] for e in payload["events"]],
                         ["gateway.unreachable"])
        self.assertEqual(payload["contacts"], 0)
        self.clear_recorder()
        snap = self.relay()
        self.assertEqual(snap.returncode, 18)
        self.assertEqual(self.json_out(snap)["events"], [])
        unreachable = [doc for doc in self.read_events()
                       if doc["kind"] == "gateway.unreachable"]
        self.assertEqual(len(unreachable), 1)
        self.assert_event_shape(unreachable[0], "gateway.unreachable")
        self.assert_no_control_posts("gateway-down relay passes")

    # -----------------------------------------------------------------
    # Fixture: no destination configured -> outbox only, dashboard says
    # "notifier: none (local only)".
    # -----------------------------------------------------------------

    def test_38_relay_mock_local_only_and_dashboard_notifier_none(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("LOCAL1", "mock stays local")
        self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(self.json_out(snap)["delivered"], 0)
        self.assertEqual(len(self.event_files()), 1)
        dash = self.client("dashboard", "--json")
        self.assertEqual(dash.returncode, 0, dash.stderr)
        snapshot = self.json_out(dash)["snapshot"]
        self.assertEqual(snapshot["notifier"],
                         "notifier: none (local only)")
        self.assertEqual(len(snapshot["runs"]), 1)
        row = snapshot["runs"][0]
        self.assertEqual(row["status"], "waiting_for_approval")
        self.assertEqual(row["blockers"], ["waiting for approval"])
        decision = row["decisions"][0]
        self.assertEqual(decision["request_id"], "req_mock_pending1")
        self.assertEqual(decision["execution_generation"], 4)
        self.assertEqual(decision["expiry"], EXPIRY_LABEL)
        self.assertEqual(decision["note"], "answer in your Hermes client; "
                                           "this dashboard cannot answer")
        self.assertEqual(snapshot["counts"]["open_decisions"], 1)
        self.assertGreaterEqual(len(snapshot["limitations"]), 3)
        self.assert_no_control_posts("local-only relay and dashboard pass")

    # -----------------------------------------------------------------
    # Fixture: caps, escaping and the derived PR link in one page.
    # -----------------------------------------------------------------

    def test_39_dashboard_mock_caps_escaping_and_pr_link(self):
        self.start_mock(default_output="L" * 2500)
        long_input = "<script>alert('mock-canary')</script> " + "y" * 500
        proc = self.client("dispatch", "--json", "--input", long_input)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = self.client("dispatch", "--json", "--input",
                           "mock review https://github.com/octocat/mock-repo"
                           "/pull/42")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dash = self.client("dashboard", "--json")
        self.assertEqual(dash.returncode, 0, dash.stderr)
        snapshot = self.json_out(dash)["snapshot"]
        self.assertEqual(snapshot["counts"]["shown"], 2)
        pr_row = next(row for row in snapshot["runs"]
                      if row["pr_link"] is not None)
        self.assertEqual(pr_row["pr_link"]["url"],
                         "https://github.com/octocat/mock-repo/pull/42")
        self.assertEqual(pr_row["pr_link"]["number"], "42")
        self.assertEqual(pr_row["pr_link"]["derived_from"], "input")
        long_row = next(row for row in snapshot["runs"]
                        if row["pr_link"] is None)
        self.assertLessEqual(len(long_row["task"]), 200)
        self.assertLessEqual(len(long_row["output"]), 2000)
        self.assertIn("<script>alert('mock-canary')</script>",
                      long_row["task"])
        # the HTML render escapes the planted tag; raw markup never survives
        html_path = os.path.join(self.ws, "mock-caps.html")
        dash = self.client("dashboard", "--out", html_path)
        self.assertEqual(dash.returncode, 0, dash.stderr)
        with open(html_path, "r", encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("&lt;script&gt;alert(&#x27;mock-canary&#x27;)",
                      html)
        self.assertNotIn("<script", html)
        self.assertIn("&lt;/script&gt;", html)
        self.assert_key_absent_everywhere(dash, self.key)
        self.assert_no_control_posts("caps dashboard passes")

    def test_40_dashboard_mock_out_and_serve_render_identically(self):
        for text in ("mock static one", "mock static two"):
            proc = self.client("dispatch", "--json", "--input", text)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        out_path = os.path.join(self.ws, "mock-dashboard.html")
        dash = self.client("dashboard", "--out", out_path,
                           "--poll-interval", "5")
        self.assertEqual(dash.returncode, 0, dash.stderr)
        with open(out_path, "r", encoding="utf-8") as handle:
            static_html = handle.read()
        self.assertIn("mock static one", static_html)
        _proc, port = self.serve()
        _status, _headers, serve_html = self.http(port)
        self.assertIn("mock static one", serve_html)
        self.assertEqual(
            ISO_STAMP_RE.sub("<ts>", static_html).replace("\n", ""),
            ISO_STAMP_RE.sub("<ts>", serve_html).replace("\n", ""))
        self.assert_no_control_posts("identical out-vs-serve render")

    def test_41_dashboard_mock_serve_headers_and_methods(self):
        proc = self.client("dispatch", "--json", "--input", "mock serve page")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before_reads = len(self.status_reads())
        _proc, port = self.serve()
        status, headers, html = self.http(port)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Security-Policy"],
                         "default-src 'none'; style-src 'self'")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("mock serve page", html)
        self.assertIn('http-equiv="refresh" content="10"', html)
        status, headers, _css = self.http(port, "/style.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(headers["Content-Security-Policy"],
                         "default-src 'none'; style-src 'self'")
        status, headers, _body = self.http(port, method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")
        status, _headers, _body = self.http(port, "/style.css", method="DELETE")
        self.assertEqual(status, 405)
        status, _headers, _body = self.http(
            port, host="evil.example:%d" % port)
        self.assertEqual(status, 403)
        status, _headers, _body = self.http(port, "/not-a-route")
        self.assertEqual(status, 404)
        status, _headers, html = self.http(port, "/?refresh=5")
        self.assertEqual(status, 200)
        self.assertIn('content="5"', html)
        self.assertNotIn('content="0"', html)
        status, _headers, html = self.http(port, "/?refresh=0")
        self.assertNotIn("http-equiv=\"refresh\"", html)
        _status, _headers, _html = self.http(port)
        _p, _h, _b = self.http(port)
        self.assertEqual(len(self.status_reads()), before_reads)
        self.assert_no_control_posts("serve-mode page fetches")

    def test_42_relay_mock_prune_keeps_receipts_and_recent_entries(self):
        sample = self.mock_pending_sample()
        self.scenario("PRUNE", seq=["waiting_for_approval"],
                      pending_script=[sample, sample, []])
        for text in ("mock prune old", "mock prune kept"):
            proc = self.dispatch_scenario("PRUNE", text)
            self.assertEqual(proc.returncode, 16)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0,
                         snap.stdout[-300:] + snap.stderr[-300:])
        self.assertEqual(len(self.json_out(snap)["events"]), 2,
                         snap.stdout[-300:])
        snap = self.relay()
        self.assertEqual(snap.returncode, 0,
                         snap.stdout[-300:] + snap.stderr[-300:])
        self.assertEqual(len(self.json_out(snap)["events"]), 2,
                         snap.stdout[-300:])
        resolved = {}
        for doc in self.read_events():
            if doc["kind"] == "approval.resolved":
                resolved[doc["task"]["text"]] = doc["dedup_key"]
        resolved_names = {"MOCK_SCENARIO:PRUNE: mock prune old",
                          "MOCK_SCENARIO:PRUNE: mock prune kept"}
        self.assertEqual(set(resolved), resolved_names)
        old_key = resolved["MOCK_SCENARIO:PRUNE: mock prune old"]
        kept_key = resolved["MOCK_SCENARIO:PRUNE: mock prune kept"]
        ledger = self.read_ledger()
        old_event = ledger["entries"][old_key]["source_event"]
        keep_event = ledger["entries"][kept_key]["source_event"]
        # age the old event file the way a real file would: rename its stamp
        # and point the ledger entry at the renamed file
        new_name = self.age_event_file(old_event)

        def age_old(doc):
            entry = doc["entries"][old_key]
            entry["first_seen_at"] = self.iso_ago(45 * 86400)
            entry["last_seen_at"] = self.iso_ago(45 * 86400)
            entry["source_event"] = new_name

        self.rewrite_ledger(age_old)
        receipts_before = sorted(os.listdir(self.receipts))
        snap = self.relay("--prune-days", "30")
        self.assertEqual(snap.returncode, 0, snap.stderr)
        json_out = self.json_out(snap)
        self.assertEqual(json_out["pruned"]["entries"], 1)
        # A pruned entry takes all of its event files with it: the aged
        # requested file AND the resolved file for the same dedup key.
        self.assertEqual(json_out["pruned"]["files"], 2)
        ledger = self.read_ledger()
        self.assertNotIn(old_key, ledger["entries"])
        entry = ledger["entries"][kept_key]
        self.assertEqual(entry["source_event"], keep_event)
        names = [os.path.basename(path) for path in self.event_files()]
        self.assertNotIn(new_name, names)
        self.assertIn(keep_event, names)
        self.assertEqual(sorted(os.listdir(self.receipts)), receipts_before)
        self.assert_no_control_posts("prune relay pass")

    def age_event_file(self, name):
        """Mock file-ageing: swap the stamp prefix for a 45-day-old one."""
        hash_part = name[17:]  # drop "<stamp>-"
        stamp = time.strftime("%Y%m%dT%H%M%SZ",
                              time.gmtime(time.time() - 45 * 86400))
        new_name = stamp + "-" + hash_part
        src = os.path.join(self.events_dir(), name)
        dst = os.path.join(self.events_dir(), new_name)
        os.rename(src, dst)
        os.chmod(dst, 0o600)
        return new_name

    def test_43_dashboard_mock_legacy_v1_receipt_and_unknown_run(self):
        raw_body = json.dumps({"input": "mock legacy v1 run"}).encode("utf-8")
        receipt = {
            "version": 1,
            "tool": "grokbot2hermes",
            "profile": "default",
            "base_url": "mock://unset",
            "idempotency_key": "idem_mock_legacy0001",
            "body_b64": base64.b64encode(raw_body).decode("ascii"),
            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
            "run_id": "run_mock_missing404",
            "status": "running",
            "idempotency_replayed": False,
            "created_at": self.iso_ago(60),
            "updated_at": self.iso_ago(60),
            "legacy_untouched": "keep",
        }
        os.makedirs(self.receipts, mode=0o700, exist_ok=True)
        path = os.path.join(self.receipts, "legacy.mock.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, sort_keys=True, indent=2))
        os.chmod(path, 0o600)
        dash = self.client("dashboard", "--json")
        self.assertEqual(dash.returncode, 18, "its only run is a 404 read")
        snapshot = self.json_out(dash)["snapshot"]
        self.assertEqual(len(snapshot["runs"]), 1)
        row = snapshot["runs"][0]
        self.assertEqual(row["run_id"], "run_mock_missing404")
        self.assertEqual(row["status"], "running")
        self.assertTrue(row["freshness"]["stale"])
        self.assertIn("stale", row["freshness"]["label"])
        with open(path, "r", encoding="utf-8") as handle:
            after = json.load(handle)
        self.assertEqual(after["legacy_untouched"], "keep")
        self.assertTrue(after.get("last_error"))
        self.assert_no_control_posts("legacy-receipt dashboard pass")

    def test_44_dashboard_mock_out_keyless_receipts_only(self):
        out_path = os.path.join(self.ws, "mock-keyless-empty.html")
        proc = self.client("dashboard", "--out", out_path, key="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no gateway contact will be made", proc.stderr)
        with open(out_path, "r", encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("No receipts yet", html)
        self.assertIn("notifier: none (local only)", html)
        self.assertIn("gateway: not-contacted", html)
        self.assertIn("the gateway was never contacted", html)
        self.assertEqual(self.status_reads(), [])
        self.assert_no_control_posts("keyless out render")

    def test_45_dashboard_mock_serve_keyless_exits_credential(self):
        proc = self.client("dashboard", "--serve", key="")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("no api key found", proc.stderr)
        self.assertEqual(self.status_reads(), [])
        self.assert_no_control_posts("keyless serve")

    def test_46_dashboard_mock_out_keyless_with_receipts(self):
        proc = self.client("dispatch", "--json", "--input",
                           "mock keyless page")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.clear_recorder()
        out_path = os.path.join(self.ws, "mock-keyless-page.html")
        proc = self.client("dashboard", "--out", out_path, key="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out_path, "r", encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("mock keyless page", html)
        self.assertIn("gateway: not-contacted", html)
        self.assertIn("the gateway was never contacted", html)
        self.assertEqual(self.status_reads(), [])
        self.assert_no_control_posts("keyless out with receipts")

    def test_47_dashboard_mock_partial_failure_freshness_scoped(self):
        """One dead run must not mark an alive run stale (per-row scope).

        The dead run's own receipt records last_error and its row goes
        stale; the alive run keeps its own fresh, age-based verdict.
        """
        raw_body = json.dumps({"input": "mock partial freshness"}).encode(
            "utf-8")
        receipt = {
            "version": 1,
            "tool": "grokbot2hermes",
            "profile": "default",
            "base_url": "mock://unset",
            "idempotency_key": "idem_mock_gone404",
            "body_b64": base64.b64encode(raw_body).decode("ascii"),
            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
            "run_id": "run_mock_gone404",
            "status": "running",
            "idempotency_replayed": False,
            "created_at": self.iso_ago(60),
            "updated_at": self.iso_ago(60),
        }
        os.makedirs(self.receipts, mode=0o700, exist_ok=True)
        path = os.path.join(self.receipts, "gone404.mock.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, sort_keys=True, indent=2))
        os.chmod(path, 0o600)
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("FRESHB", "alive pending run")
        self.assertEqual(proc.returncode, 16, proc.stderr)
        b_run_id = self.json_out(proc)["run_id"]
        dash = self.client("dashboard", "--json")
        self.assertEqual(dash.returncode, 0,
                         "one live contact keeps the transport alive")
        rows = {row["receipt"]: row
                for row in self.json_out(dash)["snapshot"]["runs"]}
        dead = rows["gone404.mock.json"]
        alive = next(row for row in rows.values()
                     if row["run_id"] == b_run_id)
        self.assertIsNot(dead, alive)
        self.assertTrue(dead["freshness"]["stale"], dead["freshness"])
        self.assertIn("stale", dead["freshness"]["label"])
        self.assertIn("last status read failed", dead["freshness"]["label"])
        self.assertFalse(alive["freshness"]["stale"],
                         alive["freshness"]["label"])
        self.assertIn("fresh", alive["freshness"]["label"])
        self.assert_no_control_posts("partial-failure freshness pass")

    def test_48_relay_mock_resolved_reappearance_never_reemits(self):
        self.start_mock(default_seq=["waiting_for_approval"])
        proc = self.dispatch_scenario("GHOST1", "ghost control reappears")
        self.assertEqual(proc.returncode, 16, proc.stderr)
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        docs = self.read_events()
        self.assertEqual([d["kind"] for d in docs], ["approval.requested"])
        dedup = docs[0]["dedup_key"]
        ledger = self.read_ledger()
        assert ledger is not None
        seen_at = ledger["entries"][dedup]["last_seen_at"]

        def mark_resolved(doc):
            entry = doc["entries"][dedup]
            entry["state"] = "resolved"
            entry["resolution"] = "answered"

        self.rewrite_ledger(mark_resolved)
        # The identical identity (same run_id:prompt_id:generation) presents
        # again. A resolved entry observes, holds its state, emits nothing.
        self.clear_recorder()
        snap = self.relay()
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual([e["kind"] for e in self.json_out(snap)["events"]],
                         [], "resolved identity must not re-emit")
        self.assertEqual([d["kind"] for d in self.read_events()],
                         ["approval.requested"])
        ledger = self.read_ledger()
        assert ledger is not None
        entry = ledger["entries"][dedup]
        self.assertEqual(entry["state"], "resolved")
        self.assertGreaterEqual(entry["last_seen_at"], seen_at)
        self.assert_no_control_posts("resolved reappearance pass")

    def test_49_relay_notify_invalid_url_not_echoed(self):
        proc = self.client("relay", "--once", "--json", "--notify-url",
                           "nota-host.example")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("invalid notifier URL", proc.stderr)
        self.assertIn("never echoed", proc.stderr)
        self.assertNotIn("nota-host.example", proc.stderr)
        self.assert_no_control_posts("invalid notify url")


def _free_loopback_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


ISO_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def receipt_sha_mock(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
