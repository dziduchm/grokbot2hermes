"""Sender tests for the G2C run-approvals chat alert sender (runalerts/sender.py).

Each test drives the real sender against a throwaway local HTTP mock (stdlib only),
a throwaway 0600 webhook file and a throwaway ledger, mapping to the frozen design's
sender test plan: first pending per key, allowlist re-check, 3 attempts then given_up,
atomic rewrite with 0600 perms, prune and cap, closed chat delivery default off,
self-test, and no credential leakage in logs.
"""

import importlib.util
import json
import logging
import os
import shutil
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENDER_PATH = os.path.join(REPO_ROOT, "runalerts", "sender.py")

_spec = importlib.util.spec_from_file_location("g2c_sender_under_test", SENDER_PATH)
sender = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sender)

DUMMY_KEY = "dummy-sender-key-not-a-real-credential"

CLOSED_RECORD_TEMPLATE = {
    "v": 1, "key": "p1|run-1|t1|7", "profile": "p1", "kind": "approval",
    "run_id": "run-1", "prompt_id": "t1", "session_id": "s1",
    "execution_generation": 7, "actionable": True, "status": "closed",
    "first_seen_iso_utc": "2026-11-09T12:00:00+00:00",
    "closed_iso_utc": "2026-11-09T12:05:00+00:00", "outcome": "expired",
}


def approval_record(**overrides) -> Dict:
    record = {
        "v": 1, "key": "p1|run-1|t1|7", "profile": "p1", "kind": "approval",
        "run_id": "run-1", "prompt_id": "t1", "session_id": "s1",
        "execution_generation": 7, "actionable": True, "status": "pending",
        "first_seen_iso_utc": "2026-11-09T12:00:00+00:00",
        "expires_london": "13:05 BST London (approx)",
        "label": "excerpt: details", "reason": "details", "command": "cmd one",
        "link": "https://x.example/webapp/run-approvals?profile=p1#card-t1",
    }
    record.update(overrides)
    return record


def nonactionable_record(**overrides) -> Dict:
    record = {
        "v": 1, "key": "p1|run-2|t2|7", "profile": "p1", "kind": "approval",
        "run_id": "run-2", "prompt_id": "t2", "session_id": "s1",
        "execution_generation": 7, "actionable": False, "status": "pending",
        "first_seen_iso_utc": "2026-11-09T12:00:00+00:00",
        "binding_refusal": "display_redacted", "note": "duplicate_run_row",
        "link": "https://x.example/webapp/run-approvals?profile=p1#card-t2",
    }
    record.update(overrides)
    return record


def closed_record(**overrides) -> Dict:
    record = dict(CLOSED_RECORD_TEMPLATE)
    record.update(overrides)
    return record


class CaptureServer:
    """Loopback POST capture: configurable status, records every request."""

    def __init__(self, status: int = 200, body: bytes = b"{}"):
        holder = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length)
                holder.requests.append({
                    "path": self.path,
                    "body": payload,
                    "key": self.headers.get("X-Sender-Key"),
                    "content_type": self.headers.get("Content-Type"),
                })
                if holder.status >= 300:
                    self.send_error(holder.status)
                    return
                self.send_response(holder.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(holder.body)))
                self.end_headers()
                self.wfile.write(holder.body)

            def log_message(self, *args, **kwargs):
                pass

        self.status = status
        self.body = body
        self.requests: List[dict] = []
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self) -> str:
        return "http://127.0.0.1:%d/hook" % self.httpd.server_port

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class SenderCase(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="g2c-sender-test-")
        os.chmod(self.workdir, 0o700)
        self.ledger = os.path.join(self.workdir, "alerts.jsonl")
        self.logs: List[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                self.captured.append(record.getMessage())

        sender._log.setLevel(logging.INFO)
        handler = Capture()
        handler.setLevel(logging.INFO)
        handler.captured = self.logs
        sender._log.addHandler(handler)
        self.capture_handler = handler

    def tearDown(self):
        sender._log.removeHandler(self.capture_handler)
        shutil.rmtree(self.workdir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def webhook_file(self, server: Optional[CaptureServer] = None,
                     url: Optional[str] = None) -> str:
        path = os.path.join(self.workdir, "webhook.json")
        payload = {"url": url or (server.url() if server else ""),
                   "sender_key": DUMMY_KEY, "key_header": "X-Sender-Key"}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        os.chmod(path, 0o600)
        return path

    def write_ledger(self, records) -> None:
        with open(self.ledger, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.chmod(self.ledger, 0o600)

    def ledger_records(self) -> List[dict]:
        with open(self.ledger, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def now(self):
        return sender.datetime(2026, 11, 9, 12, 10, 0, tzinfo=sender.timezone.utc)

    # -- tests ---------------------------------------------------------------

    def test_posts_first_pending_per_key_marks_sent_supersedes(self):
        records = [
            approval_record(),
            approval_record(key="p1|run-1|t1|7"),  # duplicate key, must supersede
            nonactionable_record(),
            approval_record(key="p1|run-3|t3|7", prompt_id="t3", run_id="run-3",
                            status="sent"),  # already sent: never rePOSTed
        ]
        self.write_ledger(records)
        server = CaptureServer()
        try:
            counts = sender.run_once(self.ledger, self.webhook_file(server),
                                     now=self.now())
        finally:
            server.stop()
        self.assertEqual(counts["posted"], 2)
        self.assertEqual(counts["sent"], 2)
        self.assertEqual(counts["superseded"], 1)
        self.assertEqual(len(server.requests), 2)
        bodies = [json.loads(r["body"].decode("utf-8")) for r in server.requests]
        self.assertEqual(bodies[0]["prompt_id"], "t1")
        self.assertEqual(bodies[1]["prompt_id"], "t2")
        stored = self.ledger_records()
        self.assertEqual([r["status"] for r in stored],
                         ["sent", "superseded", "sent", "sent"])
        self.assertNotIn("attempts", stored[0])  # no failed tries: no counter
        self.assertEqual(stat.S_IMODE(os.stat(self.ledger).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.workdir).st_mode), 0o700)

    def test_sent_records_not_reposted_on_second_run(self):
        self.write_ledger([approval_record()])
        server = CaptureServer()
        try:
            sender.run_once(self.ledger, self.webhook_file(server), now=self.now())
            first = len(server.requests)
            sender.run_once(self.ledger, self.webhook_file(server), now=self.now())
            self.assertEqual(len(server.requests), first)
        finally:
            server.stop()

    def test_payload_holds_only_allowlisted_fields(self):
        self.write_ledger([approval_record(), nonactionable_record()])
        server = CaptureServer()
        try:
            sender.run_once(self.ledger, self.webhook_file(server), now=self.now())
        finally:
            server.stop()
        bodies = [json.loads(r["body"].decode("utf-8")) for r in server.requests]
        self.assertEqual(set(bodies[0]), {
            "v", "key", "profile", "kind", "run_id", "prompt_id", "session_id",
            "execution_generation", "actionable", "first_seen_iso_utc",
            "expires_london", "label", "reason", "command", "link"})
        self.assertEqual(set(bodies[1]), {
            "v", "key", "profile", "kind", "run_id", "prompt_id", "session_id",
            "execution_generation", "actionable", "first_seen_iso_utc",
            "binding_refusal", "note", "link"})
        for body in bodies:
            self.assertNotIn("status", body)
            self.assertNotIn("attempts", body)
        self.assertEqual(server.requests[0]["key"], DUMMY_KEY)
        self.assertEqual(server.requests[0]["content_type"], "application/json")

    def test_three_failures_mark_given_up_no_further_post(self):
        self.write_ledger([approval_record()])
        server = CaptureServer(status=500)
        try:
            webhook = self.webhook_file(server)
            counts_series = [sender.run_once(self.ledger, webhook, now=self.now())
                             for _ in range(3)]
            attempts = self.ledger_records()[0]["attempts"]
            self.assertEqual(counts_series[-1]["given_up"], 1)
            self.assertEqual(counts_series[-1]["posted"], 1)
            self.assertEqual(sum(c["posted"] for c in counts_series), 3)
            sender.run_once(self.ledger, webhook, now=self.now())
            self.assertEqual(len(self.ledger_records()), 1)  # nothing else happened
        finally:
            server.stop()
        self.assertEqual(attempts, 3)
        self.assertEqual(self.ledger_records()[0]["status"], "given_up")
        self.assertEqual(len(server.requests), 3)

    def test_missing_webhook_file_clean_exit_ledger_untouched(self):
        self.write_ledger([approval_record()])
        before = open(self.ledger, encoding="utf-8").read()
        counts = sender.run_once(self.ledger, os.path.join(self.workdir, "absent.json"),
                                 now=self.now())
        self.assertEqual(counts["posted"], 0)
        self.assertEqual(counts["rewrote"], False)
        self.assertEqual(open(self.ledger, encoding="utf-8").read(), before)
        self.assertTrue(any("webhook file not usable" in line for line in self.logs))

    def test_forbidden_field_refused_until_given_up(self):
        record = approval_record(action_digest="d" * 64)  # never allowlisted
        self.write_ledger([record])
        server = CaptureServer()
        try:
            webhook = self.webhook_file(server)
            counts = sender.run_once(self.ledger, webhook, now=self.now())
            self.assertEqual(len(server.requests), 0)
            self.assertEqual(counts["skipped"], 1)
            for _ in range(2):
                sender.run_once(self.ledger, webhook, now=self.now())
        finally:
            server.stop()
        stored = self.ledger_records()
        self.assertEqual(stored[0]["status"], "given_up")
        self.assertEqual(stored[0]["attempts"], 3)

    def test_unknown_kind_and_bad_v_refused(self):
        bad_records = [approval_record(kind="weird"),
                       approval_record(key="p1|r|t9|7", prompt_id="t9", v=2)]
        self.write_ledger(bad_records)
        server = CaptureServer()
        try:
            counts = sender.run_once(self.ledger, self.webhook_file(server),
                                     now=self.now())
        finally:
            server.stop()
        self.assertEqual(counts["skipped"], 2)
        self.assertEqual(len(server.requests), 0)

    def test_closed_chat_delivery_default_off_then_enabled_once(self):
        records = [approval_record(status="sent"),
                   closed_record()]
        self.write_ledger(records)
        server = CaptureServer()
        try:
            webhook = self.webhook_file(server)
            counts_off = sender.run_once(self.ledger, webhook, now=self.now())
            self.assertEqual(counts_off["closed_sent"], 0)
            self.assertEqual(len(server.requests), 0)
            counts_on = sender.run_once(self.ledger, webhook, notify_closed=True,
                                        now=self.now())
            self.assertEqual(counts_on["closed_sent"], 1)
            body = json.loads(server.requests[0]["body"].decode("utf-8"))
            self.assertEqual(body["message"], sender.CLOSED_MESSAGE)
            self.assertNotIn("link", body)
            self.assertNotIn("label", body)
            self.assertNotIn("command", body)
            self.assertNotIn("reason", body)
            self.assertNotIn("closed_sent_iso_utc", body)
            sender.run_once(self.ledger, webhook, notify_closed=True, now=self.now())
            self.assertEqual(len(server.requests), 1)  # stamped: never repeated
        finally:
            server.stop()

    def test_closed_delivery_stops_after_three_failures(self):
        self.write_ledger([closed_record()])
        server = CaptureServer(status=500)
        try:
            webhook = self.webhook_file(server)
            for _ in range(4):
                sender.run_once(self.ledger, webhook, notify_closed=True, now=self.now())
        finally:
            server.stop()
        stored = self.ledger_records()
        self.assertEqual(len(server.requests), 3)
        self.assertTrue(stored[0].get("closed_gave_up_iso_utc"))

    def test_environment_flag_enables_closed_delivery(self):
        self.write_ledger([closed_record()])
        server = CaptureServer()
        try:
            webhook = self.webhook_file(server)
            main_argv = ["--ledger", self.ledger, "--webhook", webhook]
            os.environ["G2C_RUNALERTS_NOTIFY_CLOSED"] = "1"
            try:
                exit_code = sender.main(main_argv)
            finally:
                os.environ.pop("G2C_RUNALERTS_NOTIFY_CLOSED", None)
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(server.requests), 1)
        finally:
            server.stop()

    def test_prune_drops_old_records_and_respects_cap(self):
        old_closed = closed_record(key="p1|r|old|7", run_id="run-old",
                                   closed_iso_utc="2026-10-20T12:00:00+00:00")
        bulk = [approval_record(key="p1|r|t%d|7" % i, prompt_id="t%d" % i,
                                status="sent") for i in range(4999)]
        fresh = [approval_record(key="p1|run-1|t1|7"),
                 nonactionable_record()]
        self.write_ledger([old_closed] + bulk + fresh)
        server = CaptureServer()
        try:
            counts = sender.run_once(self.ledger, self.webhook_file(server),
                                     now=self.now())
        finally:
            server.stop()
        self.assertEqual(counts["sent"], 2)
        stored = self.ledger_records()
        self.assertLessEqual(len(stored), sender.MAX_RECORDS)
        self.assertEqual(len(stored), 5000)
        self.assertNotIn(old_closed["key"], {r.get("key") for r in stored})
        by_key = {r["key"]: r["status"] for r in stored}
        self.assertEqual(by_key["p1|run-1|t1|7"], "sent")
        self.assertEqual(by_key["p1|run-2|t2|7"], "sent")

    def test_malformed_lines_quarantined_once(self):
        garbage = "not json at all\n"
        self.write_ledger([approval_record()])
        with open(self.ledger, "a", encoding="utf-8") as handle:
            handle.write(garbage)
        server = CaptureServer()
        try:
            sender.run_once(self.ledger, self.webhook_file(server), now=self.now())
        finally:
            server.stop()
        quarantine = sender.quarantine_path(sender.Path(self.ledger))
        self.assertTrue(quarantine.exists())
        self.assertEqual(quarantine.read_text(encoding="utf-8"), garbage)
        self.assertEqual(len(server.requests), 1)

    def test_https_required_outside_loopback(self):
        self.write_ledger([approval_record()])
        before = open(self.ledger, encoding="utf-8").read()
        counts = sender.run_once(self.ledger,
                                 self.webhook_file(url="http://example.com/hook"),
                                 now=self.now())
        self.assertEqual(counts["posted"], 0)
        self.assertEqual(open(self.ledger, encoding="utf-8").read(), before)

    def test_self_test_posts_fixed_message(self):
        server = CaptureServer()
        try:
            code_ok = sender.main(["--self-test", "--webhook", self.webhook_file(server)])
        finally:
            server.stop()
        self.assertEqual(code_ok, 0)
        body = json.loads(server.requests[0]["body"].decode("utf-8"))
        self.assertEqual(body, {"v": 1, "message": sender.SELF_TEST_MESSAGE})
        self.assertEqual(server.requests[0]["key"], DUMMY_KEY)

    def test_self_test_failure_returns_nonzero(self):
        server = CaptureServer(status=500)
        try:
            code = sender.main(["--self-test", "--webhook", self.webhook_file(server)])
        finally:
            server.stop()
        self.assertEqual(code, 1)

    def test_no_key_or_url_in_logs(self):
        record = approval_record()
        self.write_ledger([record])
        server = CaptureServer(status=500)
        try:
            webhook = self.webhook_file(server)
            for _ in range(3):
                sender.run_once(self.ledger, webhook, now=self.now())
        finally:
            server.stop()
        joined = "\n".join(self.logs)
        self.assertNotIn(DUMMY_KEY, joined)
        self.assertNotIn("127.0.0.1", joined)

    def test_main_without_ledger_arg_is_clean(self):
        code = sender.main([])
        self.assertEqual(code, 0)
        self.assertTrue(any("no ledger given" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
