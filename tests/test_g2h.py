"""Fixture matrix tests for g2h against the mock gateway.

Each test maps to one numbered fixture from the implementation brief
(section 10) and runs the real client as a subprocess against a fresh
MockHermes instance. All api keys are throwaway strings generated per run.
Example fixtures only: statuses, delays and behaviors are injected per test.
"""

import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import Optional

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)

from mock_hermes import MockHermes  # noqa: E402

G2H_PY = os.path.join(REPO_ROOT, "g2h.py")

KEY_ENV_VARS = ("G2H_API_KEY", "G2H_BASE_URL", "G2H_RECEIPT_DIR",
                "G2H_PROFILE", "G2H_OUTPUT_CAP")


class FixtureCase(unittest.TestCase):
    """Shared harness: fresh mock, fresh scratch receipt dir, subprocess.

    Note: the researcher profile name is used because it already appears in
    upstream Hermes documentation as an example profile; it mocks locally.
    """

    maxDiff = None
    mock: Optional[MockHermes]

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

    def client(self, *args, key=None, timeout=120):
        env = dict(os.environ)
        for name in KEY_ENV_VARS:
            env.pop(name, None)
        env["G2H_BASE_URL"] = self.mock.base_url
        env["G2H_API_KEY"] = key if key is not None else self.key
        env["G2H_RECEIPT_DIR"] = self.receipts
        env["PYTHONIOENCODING"] = "utf-8"
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


if __name__ == "__main__":
    unittest.main()
