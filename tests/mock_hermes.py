"""Mock Hermes Agent api server: an example fixture for the test suite.

Implements the documented subset in docs/api-contracts.md (verified against
Hermes Agent v0.22.7) over a threaded http.server. All keys are throwaway
strings generated per test run; nothing here is used by the production
client. Example fixture only: statuses, delays and behaviors are injected
per test.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

TERMINAL = ("completed", "failed", "cancelled", "interrupted")
RUN_DONE_SSE = (b"event: run.completed\n"
                b"data: {\"output\": \"MOCK_RUN_OUTPUT\"}\n\n")


class MockConfig:
    """Mutable per-test behavior knobs and server bookkeeping."""

    def __init__(self, default_key, profile_keys=None):
        self.default_key = default_key
        self.profile_keys = dict(profile_keys or {})
        self.lock = threading.Lock()
        self.total_requests = 0
        self.create_attempts = 0
        self.create_times = []
        self.resolve_attempts = 0
        self.stop_requests = 0
        self.runs = {}          # run_id -> RunState
        self.run_profiles = {}  # run_id -> profile name or None
        self.ledger = {}        # idempotency key -> {sha256, run_id}
        self.create_failures = []   # list of (status, code, retry_after)
        self.create_delay = 0.0
        self.create_status = 202    # first-create success status
        self.stop_status = 200      # stop success status (doc leaves open)
        self.default_seq = ["running", "completed"]
        self.default_output = "MOCK_RUN_OUTPUT"
        self.caps_status = 200
        self.features = {
            "run_submission": True,
            "run_status": True,
            "run_events_sse": True,
            "run_stop": True,
            "run_approval": True,
            "reasoning_streaming": True,
        }
        self.events_queue = []  # per-connection behaviors, consumed in order
        self.approval_after = ["running", "completed"]
        self.stop_seq = ["stopping", "cancelled"]
        self.foreign_sessions = set()
        self.pending_controls_sample = [
            {"control": "mcp_tool_approval",
             "prompt_id": "req_mock_pending1",
             "execution_generation": 4},
        ]
        self.admission_id = "adm_" + secrets.token_hex(6)
        self.admission_generation = 7


class RunState:
    def __init__(self, run_id, profile, payload, config):
        self.run_id = run_id
        self.profile = profile
        self.payload = payload
        self.session_id = "sess_mock_" + secrets.token_hex(6)
        self.status = "started"
        self.seq = list(config.default_seq)
        self.output = config.default_output
        self.usage = {"input_tokens": 10, "output_tokens": 20,
                      "total_tokens": 30, "cache_read_tokens": 0,
                      "cache_write_tokens": 0}
        self.runtime = {"provider": "mock-provider", "model": "mock-model",
                        "route_source": "global"}
        self.pending_controls = None
        self.waiting = False
        self.admission_id = config.admission_id
        self.execution_generation = config.admission_generation
        self.stop_requested = False
        self.stop_seq = []
        self.events_attempts = 0
        self.resolved_receipt = None
        self._config = config

    def next_status(self):
        if self.stop_requested:
            if self.stop_seq:
                status = self.stop_seq.pop(0)
                if status in TERMINAL:
                    self.status = status
                return status
            return self.status if self.status in TERMINAL else "cancelled"
        if self.seq:
            status = self.seq.pop(0)
            self.status = status
            return status
        return self.status

    def status_body(self):
        status = self.next_status()
        body = {"object": "hermes.run", "run_id": self.run_id,
                "status": status, "session_id": self.session_id,
                "model": "mock-model"}
        if status in TERMINAL:
            body["output"] = self.output
            body["usage"] = self.usage
            body["runtime"] = self.runtime
        if status == "waiting_for_approval":
            self.waiting = True
            if self.pending_controls is None:
                self.pending_controls = [dict(entry) for entry
                                         in self._config.pending_controls_sample]
            body["pending_controls"] = self.pending_controls
        if status == "unknown":
            body["admission_id"] = self.admission_id
            body["execution_generation"] = self.execution_generation
        return body


class MockHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    config: "Optional[MockConfig]" = None  # injected before serving


class MockHandler(BaseHTTPRequestHandler):
    server_version = "MockHermes/0.1"

    def log_message(self, format, *args):  # noqa: A002 - matches base class
        pass  # keep test output quiet

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method):
        try:
            self._handle(method)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # keep the server thread alive for tests
            try:
                self._send_json(500, {"error": {"code": "mock_internal",
                                                "detail": str(exc)[:200]}})
            except OSError:
                pass

    def _handle(self, method):
        config = self.server.config
        with config.lock:
            config.total_requests += 1
        profile, rest = split_route(self.path)
        if rest is None:
            return self._send_json(404, {"error": {"code": "not_found"}})
        token = self._bearer()
        if profile in (None, "default"):
            expected = config.default_key
        elif profile in config.profile_keys:
            expected = config.profile_keys[profile]
        else:
            return self._send_json(404, {"error": {
                "code": "not_found", "detail": "unknown profile"}})
        if token != expected:
            return self._send_json(401, {"error": {"code": "unauthorized"}})
        parts = rest.split("/")
        if parts == ["v1", "capabilities"] and method == "GET":
            return self._capabilities(config)
        if parts == ["v1", "runs"] and method == "POST":
            return self._create(config, profile)
        if len(parts) == 3 and parts[0] == "v1" and parts[1] == "runs" \
                and method == "GET":
            return self._status(config, profile, parts[2])
        if len(parts) == 4 and parts[0] == "v1" and parts[1] == "runs":
            run_id, action = parts[2], parts[3]
            if method == "GET" and action == "events":
                return self._events(config, profile, run_id)
            if method == "POST":
                with config.lock:
                    if action == "stop":
                        config.stop_requests += 1
                    if action == "resolve-unknown":
                        config.resolve_attempts += 1
                if action == "stop":
                    return self._stop(config, profile, run_id)
                if action in ("approval", "clarify"):
                    return self._control(config, profile, run_id, action)
                if action == "resolve-unknown":
                    return self._resolve_unknown(config, profile, run_id)
        return self._send_json(404, {"error": {"code": "not_found"}})

    def _bearer(self):
        header = self.headers.get("Authorization") or ""
        match = re.fullmatch(r"Bearer (.+)", header.strip())
        return match.group(1) if match else None

    def _read_raw(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def _read_json(self):
        raw = self._read_raw()
        try:
            return json.loads(raw.decode("utf-8")), raw
        except (ValueError, UnicodeDecodeError):
            return None, raw

    def _send_json(self, status, obj, headers=None):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ----- endpoints -----------------------------------------------------

    def _capabilities(self, config):
        if config.caps_status != 200:
            return self._send_json(config.caps_status,
                                   {"error": {"code": "not_found"}})
        return self._send_json(200, {
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "auth": {"type": "bearer", "required": True},
            "features": dict(config.features),
        })

    def _create(self, config, profile):
        with config.lock:
            config.create_attempts += 1
            config.create_times.append(time.monotonic())
        if config.create_failures:
            status, code, retry_after = config.create_failures.pop(0)
            body = {"error": {"code": code,
                              "message": "mock injected failure"}}
            if code == "too_many_concurrent":
                body["error"]["message"] = "Too many concurrent runs (max 10)"
            headers = {"Retry-After": str(retry_after)} if retry_after else {}
            return self._send_json(status, body, headers)
        body, raw = self._read_json()
        if body is None or not isinstance(body.get("input"), str) \
                or not body["input"].strip():
            return self._send_json(400, {"error": {
                "code": "invalid_params",
                "detail": "input must be a non-empty string"}})
        session_id = body.get("session_id")
        if session_id and session_id in config.foreign_sessions:
            return self._send_json(409, {"error": {
                "code": "permission_denied",
                "detail": "session belongs to another surface"}})
        idem = self.headers.get("Idempotency-Key")
        sha = hashlib.sha256(raw).hexdigest()
        with config.lock:
            entry = config.ledger.get(idem) if idem else None
        if entry:
            if entry["sha256"] != sha:
                return self._send_json(409, {"error": {
                    "code": "idempotency_key_conflict"}})
            return self._send_json(
                202, {"object": "hermes.run", "run_id": entry["run_id"],
                      "status": "started"},
                {"Idempotency-Replayed": "true"})
        run_id = "run_mock_" + secrets.token_hex(6)
        state = RunState(run_id, profile, body, config)
        with config.lock:
            config.runs[run_id] = state
            config.run_profiles[run_id] = profile
            if idem and config.ledger.get(idem) is None:
                config.ledger[idem] = {"sha256": sha, "run_id": run_id}
        if config.create_delay:
            time.sleep(config.create_delay)  # reserve before work: 2.3
        payload = {"object": "hermes.run", "run_id": run_id,
                   "status": "started", "session_id": state.session_id}
        return self._send_json(config.create_status, payload)

    def _status(self, config, profile, run_id):
        state = config.runs.get(run_id)
        if state is None or config.run_profiles.get(run_id) != profile:
            return self._send_json(404, {"error": {"code": "not_found"}})
        return self._send_json(200, state.status_body())

    def _stop(self, config, profile, run_id):
        state = config.runs.get(run_id)
        if state is None or config.run_profiles.get(run_id) != profile:
            return self._send_json(404, {"error": {"code": "not_found"}})
        if state.status == "unknown":
            return self._send_json(409, {"error": {
                "code": "unknown_execution"}})
        state.stop_requested = True
        state.stop_seq = list(config.stop_seq)
        return self._send_json(config.stop_status,
                               {"run_id": run_id, "status": "stopping"})

    def _events(self, config, profile, run_id):
        state = config.runs.get(run_id)
        if state is None or config.run_profiles.get(run_id) != profile:
            return self._send_json(404, {"error": {"code": "not_found"}})
        with config.lock:
            behavior = config.events_queue.pop(0) if config.events_queue \
                else "normal"
            state.events_attempts += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            if behavior == "drop_immediate":
                return
            self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()
            self.wfile.write(b"event: message.interim\n"
                             b"data: {\"text\": \"working\"}\n\n")
            self.wfile.flush()
            if behavior == "drop_after_event":
                return
            for _ in range(4):
                time.sleep(0.1)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            self.wfile.write(RUN_DONE_SSE)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _control(self, config, profile, run_id, action):
        state = config.runs.get(run_id)
        if state is None or config.run_profiles.get(run_id) != profile:
            return self._send_json(404, {"error": {"code": "not_found"}})
        body, _raw = self._read_json()
        if body is None or not isinstance(body, dict):
            return self._send_json(400, {"error": {"code": "invalid_params"}})
        control = None
        for entry in (state.pending_controls or []):
            if entry.get("prompt_id") == body.get("request_id"):
                control = entry
                break
        if control is None:
            return self._send_json(409, {"error": {
                "code": "not_found",
                "detail": "control does not belong to this run"}})
        generation = body.get("execution_generation")
        if not isinstance(generation, int) \
                or generation != control.get("execution_generation"):
            return self._send_json(409, {"error": {
                "code": "stale_generation"}})
        if not state.waiting or state.status != "waiting_for_approval":
            return self._send_json(409, {"error": {
                "code": "stale_generation", "detail": "run is not waiting"}})
        if action == "approval" and body.get("choice") not in ("once", "deny"):
            return self._send_json(400, {"error": {
                "code": "invalid_params", "detail": "choice must be once or "
                "deny"}})
        if action == "clarify" and not isinstance(body.get("answer"), str):
            return self._send_json(400, {"error": {"code": "invalid_params"}})
        state.waiting = False
        state.pending_controls = None
        state.seq = list(config.approval_after)
        state.status = "running"
        return self._send_json(200, {"run_id": run_id, "status": "ok"})

    def _resolve_unknown(self, config, profile, run_id):
        state = config.runs.get(run_id)
        if state is None or config.run_profiles.get(run_id) != profile:
            return self._send_json(404, {"error": {"code": "not_found"}})
        if not config.features.get("run_unknown_resolution"):
            return self._send_json(404, {"error": {
                "code": "not_found", "detail": "endpoint not advertised"}})
        if state.resolved_receipt is not None:
            return self._send_json(200, state.resolved_receipt)
        body, _raw = self._read_json()
        if body is None or set(body) != {"admission_id",
                                         "execution_generation"}:
            return self._send_json(400, {"error": {
                "code": "invalid_params"}})
        if body["admission_id"] != state.admission_id:
            return self._send_json(409, {"error": {"code": "not_found"}})
        generation = body["execution_generation"]
        if not isinstance(generation, int) \
                or generation != state.execution_generation \
                or state.status != "unknown":
            return self._send_json(409, {"error": {
                "code": "stale_generation"}})
        state.resolved_receipt = {"object": "hermes.run.resolution",
                                  "run_id": run_id,
                                  "admission_id": state.admission_id,
                                  "execution_generation": generation,
                                  "outcome": "interrupted"}
        state.seq = []
        state.stop_requested = False
        state.status = "interrupted"
        return self._send_json(200, state.resolved_receipt)


def split_route(path):
    """Return (profile, rest) where rest starts with 'v1/'; (None, None) for
    unknown paths."""
    path = path.split("?", 1)[0]
    match = re.fullmatch(r"/p/([A-Za-z0-9_-]+)/(v1/.*)", path)
    if match:
        return match.group(1), match.group(2)
    match = re.fullmatch(r"/(v1/.*)", path)
    if match:
        return None, match.group(1)
    return None, None


class MockHermes:
    def __init__(self, default_key, profile_keys=None, **overrides):
        self.config = MockConfig(default_key, profile_keys)
        for key, value in overrides.items():
            setattr(self.config, key, value)
        self._server = MockHTTPServer(("127.0.0.1", 0), MockHandler)
        self._server.config = self.config
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.port

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
