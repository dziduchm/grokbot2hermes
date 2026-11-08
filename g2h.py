#!/usr/bin/env python3
"""g2h: a small stdlib-only client for the Hermes Agent Runs API.

Dispatches long-running tasks to a Hermes Agent gateway's Runs API, tracks
them with crash-safe receipts, and stops them. Python 3.9+, standard library
only. The HTTP contracts live in docs/api-contracts.md and were verified
against Hermes Agent v0.22.7. This file contains zero adapter-specific
concepts; see docs/adapters/grokbot.md for the adapter layer.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import hashlib
import json
import os
import random
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from typing import NoReturn

import g2h_ui

VERSION = "0.1.0"
USER_AGENT = "g2h/%s (grokbot2hermes)" % VERSION

# Exit codes: the single source of truth, mirrored in README.md.
EXIT_OK = 0             # terminal status "completed" (or a clean query)
EXIT_INTERNAL = 1       # unexpected client bug or undocumented server reply
EXIT_USAGE = 2          # bad flags, empty input, invalid key characters
EXIT_CREDENTIAL = 3     # key missing; key file not 0600; receipt dir not 0700
EXIT_FAILED = 5         # terminal status "failed"
EXIT_CANCELLED = 6      # terminal status "cancelled" (including after stop)
EXIT_INTERRUPTED = 7    # terminal status "interrupted" (gateway shutdown)
EXIT_AUTH = 10          # HTTP 401: key not valid for this base URL/profile
EXIT_NOT_FOUND = 11     # HTTP 404: unknown run id, wrong profile, retention
EXIT_CONFLICT = 12      # HTTP 409 caller errors (conflict / bind / stale gen)
EXIT_UNAVAILABLE = 13   # 429/503 retry budget exhausted
EXIT_TIMEOUT = 14       # --timeout expired while non-terminal
EXIT_HUMAN = 16         # waiting_for_approval / pending_controls observed
EXIT_TRANSPORT = 18     # connection failures after retry budget

TERMINAL_STATUSES = ("completed", "failed", "cancelled", "interrupted")
KNOWN_NON_TERMINAL = ("running", "waiting_for_approval", "stopping", "unknown")
EXIT_BY_STATUS = {"completed": EXIT_OK, "failed": EXIT_FAILED,
                  "cancelled": EXIT_CANCELLED, "interrupted": EXIT_INTERRUPTED}
STATUS_BY_EXIT = {code: status for status, code in EXIT_BY_STATUS.items()}
TERMINAL_EVENTS = ("run.completed", "run.failed", "run.cancelled",
                   "run.interrupted")
FALLBACK_FEATURES = {"run_submission": True, "run_status": True,
                     "run_events_sse": True, "run_stop": True}

DEFAULT_BASE_URL = "http://127.0.0.1:8642"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
POLL_INTERVAL = 2.0
POLL_FACTOR = 1.6
POLL_CAP = 30.0
DEFAULT_TIMEOUT = 1800.0
RETRY_AFTER_CAP = 120.0
CAP_TIMEOUT = 5.0
HTTP_TIMEOUT = 30.0
SSE_READ_TIMEOUT = 45.0
MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_OUTPUT_CAP = 2000

_ERROR_CODES = ("idempotency_key_conflict", "permission_denied",
                "stale_generation", "unknown_execution", "invalid_params",
                "runtime_draining", "storage_unavailable", "not_found")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_JSON_CODE_RE = re.compile(r'"(?:error_?code|code)":\s*"([a-z_]{3,60})"')
_BEARER_RE = re.compile(r"(?i)bearer\s+[^\s\",;]{4,}")
_AUTH_RE = re.compile(
    r"(?i)(authorization\s*[\"']?\s*[:=]\s*(?:bearer\s+)?)[^,;\s\"']{4,}")


_REDACT_VALUES: list[str] = []


# ---------------------------------------------------------------------------
# Redaction: every output path goes through scrub().
# ---------------------------------------------------------------------------

def scrub_register(value: str) -> None:
    if value and value not in _REDACT_VALUES:
        _REDACT_VALUES.append(value)


def scrub(text) -> str:
    """Replace the api key value, any Authorization header and any Bearer
    token with [REDACTED]."""
    if not isinstance(text, str):
        text = str(text)
    out = text
    for value in sorted(_REDACT_VALUES, key=len, reverse=True):
        if value in out:
            out = out.replace(value, "[REDACTED]")
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = _AUTH_RE.sub(r"\1[REDACTED]", out)
    return out


# ---------------------------------------------------------------------------
# Small exceptions and views.
# ---------------------------------------------------------------------------

class G2hExit(Exception):
    """Raised by die(); carries the verified exit code."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class ApiError(Exception):
    """An HTTP error response (status, headers, scrubbed body available)."""

    def __init__(self, status: int, headers, text: str):
        super().__init__("http %s" % status)
        self.status = status
        self.headers = HeaderView(headers)
        self.text = text


class TransportError(Exception):
    pass


class HeaderView:
    """Case-insensitive read-only view over response headers."""

    def __init__(self, items):
        if hasattr(items, "items"):
            items = items.items()
        self._items = [(str(k).lower(), v) for k, v in items]

    def get(self, name: str, default=None):
        for key, value in self._items:
            if key == name.lower():
                return value
        return default


class Ctx:
    """Per-invocation state: json mode, diagnostics, warn-once flag."""

    def __init__(self, json_mode: bool, command: str):
        self.json_mode = json_mode
        self.command = command
        self.diagnostics: list[dict] = []
        self._caps_warned = False

    def diagnostic(self, code: str, message: str) -> None:
        self.diagnostics.append({"code": code, "message": scrub(str(message))})

    def warn(self, message: str) -> None:
        print("g2h: " + scrub(str(message)), file=sys.stderr)

    def warn_once(self, message: str) -> None:
        if not self._caps_warned:
            self._caps_warned = True
            self.warn(message)

    def emit(self, payload: dict) -> None:
        if not self.json_mode:
            return
        doc = dict(payload)
        doc["command"] = self.command
        doc["diagnostics"] = self.diagnostics
        print(scrub(json.dumps(doc, sort_keys=True, separators=(",", ":"))))

    def finish(self, payload: dict, human_lines=None) -> None:
        if self.json_mode:
            self.emit(payload)
            return
        for line in human_lines or []:
            print(scrub(line))

    def die(self, code: int, message: str,
            payload=None) -> NoReturn:
        message = scrub(str(message))
        doc = dict(payload or {})
        doc["error"] = message
        doc["exit_code"] = code
        self.emit(doc)
        self.warn(message)
        raise G2hExit(code, message)


# ---------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------

class Config:
    def __init__(self, base_url: str, profile: str, receipt_dir: str):
        self.base_url = base_url.rstrip("/")
        self.profile = profile or ""
        self.receipt_dir = receipt_dir

    @classmethod
    def from_args(cls, ctx: Ctx, args) -> "Config":
        base = (args.base_url or os.environ.get("G2H_BASE_URL")
                or DEFAULT_BASE_URL).rstrip("/")
        profile = args.profile or os.environ.get("G2H_PROFILE") or ""
        if profile == "default":
            profile = ""
        if profile and not _PROFILE_RE.match(profile):
            ctx.die(EXIT_USAGE, "invalid --profile value: %r" % profile)
        receipt_dir = (args.receipt_dir or os.environ.get("G2H_RECEIPT_DIR")
                       or default_receipt_dir(profile))
        validate_base_url(ctx, base, bool(args.allow_insecure_http))
        return cls(base, profile, receipt_dir)

    def api_url(self, path: str) -> str:
        if self.profile:
            return "%s/p/%s/%s" % (self.base_url, self.profile, path)
        return "%s/%s" % (self.base_url, path)


def default_receipt_dir(profile: str) -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "grokbot2hermes", profile or "default")


def validate_base_url(ctx: Ctx, base: str, allow_insecure: bool) -> None:
    parts = urllib.parse.urlparse(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        ctx.die(EXIT_USAGE, "invalid --base-url: %r" % base)
    if parts.scheme == "https":
        return
    host = (parts.hostname or "").lower()
    if host in LOOPBACK_HOSTS:
        return
    if not allow_insecure:
        ctx.die(EXIT_USAGE,
                "refusing plain http for a non-loopback host; use https, or "
                "pass --allow-insecure-http (which prints a warning)")
    ctx.warn("WARNING: sending the api key over plain http to %s; prefer "
             "https" % base)


# ---------------------------------------------------------------------------
# Credentials. Never on argv; never printed.
# ---------------------------------------------------------------------------

def read_key_file(ctx: Ctx, path: str) -> str:
    if os.path.islink(path):
        ctx.die(EXIT_CREDENTIAL, "refusing symlinked key file: %s" % path)
    try:
        st = os.lstat(path)
    except OSError as exc:
        ctx.die(EXIT_CREDENTIAL, "cannot read key file: %s" % exc)
    if not stat.S_ISREG(st.st_mode):
        ctx.die(EXIT_CREDENTIAL, "key file is not a regular file: %s" % path)
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        ctx.die(EXIT_CREDENTIAL,
                "key file permissions must be 0600 (found %s): %s"
                % (oct(mode), path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError as exc:
        ctx.die(EXIT_CREDENTIAL, "cannot read key file: %s" % exc)


def resolve_key(ctx: Ctx, args) -> str:
    if args.key_stdin and args.key_file:
        ctx.die(EXIT_USAGE, "pass only one of --key-file and --key-stdin")
    if args.key_stdin:
        raw = sys.stdin.read()
    elif args.key_file:
        raw = read_key_file(ctx, args.key_file)
    else:
        raw = os.environ.get("G2H_API_KEY") or ""
        if not raw.strip():
            ctx.die(EXIT_CREDENTIAL,
                    "no api key found: set G2H_API_KEY, or pass --key-file "
                    "(a 0600 file), or --key-stdin")
    key = raw.strip()
    for ch in key:
        order = ord(ch)
        if order < 0x20 or order == 0x7F or order > 0xFF:
            ctx.die(EXIT_CREDENTIAL,
                    "api key contains unusable characters (control "
                    "characters or non-latin-1) and was rejected")
    scrub_register(key)
    return key


# ---------------------------------------------------------------------------
# Receipts: 0600 files in a 0700 dir, atomic tmp+rename.
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def receipt_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def ensure_receipt_dir(ctx: Ctx, path: str) -> None:
    if os.path.islink(path):
        ctx.die(EXIT_CREDENTIAL, "refusing symlinked receipt dir: %s" % path)
    if os.path.exists(path):
        if not os.path.isdir(path):
            ctx.die(EXIT_CREDENTIAL, "receipt dir is not a directory: %s"
                    % path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            ctx.die(EXIT_CREDENTIAL,
                    "receipt dir permissions must be 0700 (found %s): %s"
                    % (oct(mode), path))
        return
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as exc:
        ctx.die(EXIT_CREDENTIAL, "cannot create receipt dir: %s" % exc)
    if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
        ctx.die(EXIT_CREDENTIAL, "umask loosened the receipt dir mode: %s"
                % path)


def receipt_file_name(directory: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return os.path.join(directory, "run-%s-%s.json"
                        % (stamp, secrets.token_urlsafe(8)))


def write_receipt(ctx: Ctx, path: str, doc: dict) -> None:
    text = scrub(json.dumps(doc, sort_keys=True, indent=2))
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                    prefix=".g2h-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        ctx.die(EXIT_INTERNAL, "cannot write receipt: %s" % exc)


def new_receipt(base_url: str, profile: str, idem_key: str, body: bytes) -> dict:
    return {
        "version": 1,
        "tool": "grokbot2hermes",
        "profile": profile or "default",
        "base_url": base_url,
        "idempotency_key": idem_key,
        "body_b64": base64.b64encode(body).decode("ascii"),
        "body_sha256": receipt_sha(body),
        "run_id": None,
        "status": "pending_create",
        "idempotency_replayed": False,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "last_output": None,
        "usage": None,
        "runtime": None,
        "pending_controls": None,
        "admission": None,
    }


def load_receipt(ctx: Ctx, path: str):
    if os.path.islink(path):
        ctx.die(EXIT_CREDENTIAL, "refusing symlinked receipt: %s" % path)
    try:
        st = os.lstat(path)
    except OSError as exc:
        ctx.die(EXIT_USAGE, "cannot read receipt: %s" % exc)
    if not stat.S_ISREG(st.st_mode):
        ctx.die(EXIT_CREDENTIAL, "receipt is not a regular file: %s" % path)
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        ctx.die(EXIT_CREDENTIAL,
                "receipt permissions must be 0600 (found %s): %s"
                % (oct(mode), path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        ctx.die(EXIT_INTERNAL, "cannot parse receipt: %s" % exc)
    for field in ("idempotency_key", "body_b64", "body_sha256"):
        if not isinstance(doc.get(field), str):
            ctx.die(EXIT_INTERNAL, "receipt missing field %s" % field)
    try:
        raw = base64.b64decode(doc["body_b64"], validate=True)
    except Exception as exc:  # binascii.Error subclasses ValueError
        ctx.die(EXIT_INTERNAL, "receipt body_b64 is corrupt: %s" % exc)
    if receipt_sha(raw) != doc["body_sha256"]:
        ctx.die(EXIT_INTERNAL,
                "receipt body sha256 mismatch; refusing to replay a possibly "
                "altered payload")
    return doc, raw


# ---------------------------------------------------------------------------
# HTTP transport: no redirects (Authorization never crosses origins),
# retry 429/503/transport with Retry-After or the polling backoff curve.
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # raise instead of following; auth must not be forwarded


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_request(cfg: Config, method: str, url: str, *, body=None,
                 extra_headers=None, timeout: float = HTTP_TIMEOUT):
    data = None
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/json, text/event-stream"}
    if body is not None:
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        headers["Content-Type"] = "application/json"
    headers.update(extra_headers or {})
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    try:
        response = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise ApiError(exc.code, exc.headers, _read_stream(exc)) from None
    except urllib.error.URLError as exc:
        raise TransportError("urlopen failed: %s" % (exc.reason,)) from None
    except (OSError, socket.timeout) as exc:
        raise TransportError("%s: %s" % (type(exc).__name__,
                                         scrub(str(exc)))) from None
    try:
        text = _read_stream(response)
        status = response.status
        reply_headers = response.headers
    finally:
        response.close()
    return status, reply_headers, text


def _read_stream(stream) -> str:
    try:
        raw = stream.read(MAX_BODY_BYTES + 1)
    except (OSError, socket.timeout) as exc:
        raise TransportError("response read failed: %s" % exc) from None
    if len(raw) > MAX_BODY_BYTES:
        raise TransportError("response body exceeds %d bytes" % MAX_BODY_BYTES)
    return raw.decode("utf-8", errors="replace")


def backoff_delay(attempt: int) -> float:
    return random.uniform(0.0, min(POLL_CAP, POLL_INTERVAL
                                   * POLL_FACTOR ** attempt))


def retry_after_delay(headers) -> float:
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return 0.0
    value = str(value).strip()
    if value.isdigit():
        return min(RETRY_AFTER_CAP, float(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if when is None or when.tzinfo is None:
        return 0.0
    return min(RETRY_AFTER_CAP, max(0.0, when.timestamp() - time.time()))


def classify_reason(text: str, status: int) -> str:
    for code in _ERROR_CODES:
        if code in (text or ""):
            return code
    if "Too many concurrent runs" in (text or ""):
        return "too_many_concurrent_runs"
    return "http_%s" % status


def error_code(text: str):
    for code in _ERROR_CODES:
        if code in (text or ""):
            return code
    match = _JSON_CODE_RE.search(text or "")
    return match.group(1) if match else None


def send_with_retry(cfg: Config, ctx: Ctx, method: str, url: str, *,
                    body=None, extra_headers=None, timeout: float = HTTP_TIMEOUT,
                    max_retries: int = 8, what: str = "request"):
    attempt = 0
    while True:
        try:
            return http_request(cfg, method, url, body=body,
                                extra_headers=extra_headers, timeout=timeout)
        except ApiError as exc:
            if exc.status in (429, 503) and attempt < max_retries:
                delay = retry_after_delay(exc.headers) or backoff_delay(attempt)
                ctx.diagnostic(classify_reason(exc.text, exc.status),
                               "server limited or draining; retrying %s" % what)
                time.sleep(delay)
                attempt += 1
                continue
            map_http_error(ctx, exc.status, exc.text, what)  # always dies
            raise  # unreachable; keeps the type checker honest
        except TransportError as exc:
            if attempt < max_retries:
                ctx.diagnostic("transport_error",
                               "%s; retrying %s" % (exc, what))
                time.sleep(backoff_delay(attempt))
                attempt += 1
                continue
            raise


# ---------------------------------------------------------------------------
# JSON helpers.
# ---------------------------------------------------------------------------

def canonical_json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def snippet(text: str) -> str:
    if not text:
        return ""
    return " (body starts: %s)" % scrub(text[:200].replace("\n", " "))


def parse_json_object(ctx: Ctx, text: str, what: str) -> dict:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        ctx.die(EXIT_INTERNAL, "malformed JSON from %s: %s%s"
                % (what, exc, snippet(text)))
    if not isinstance(doc, dict):
        ctx.die(EXIT_INTERNAL, "unexpected JSON payload from %s%s"
                % (what, snippet(text)))
    return doc


# ---------------------------------------------------------------------------
# Runs API endpoints.
# ---------------------------------------------------------------------------

def auth_headers(key: str) -> dict:
    return {"Authorization": "Bearer " + key}


def validate_run_id(ctx: Ctx, run_id) -> str:
    if not run_id or not _RUN_ID_RE.match(str(run_id)):
        ctx.die(EXIT_USAGE, "invalid run id: %r" % (run_id,))
    return run_id


def validate_idem_key(ctx: Ctx, key: str) -> None:
    if not key or len(key) > 255:
        ctx.die(EXIT_USAGE, "invalid idempotency key: 1-255 characters required")
    for ch in key:
        if not 0x21 <= ord(ch) <= 0x7E:
            ctx.die(EXIT_USAGE,
                    "invalid idempotency key: visible ASCII characters only")


def validate_timeout(ctx: Ctx, timeout) -> None:
    if not timeout or timeout <= 0:
        ctx.die(EXIT_USAGE, "--timeout must be a positive number of seconds")


def map_http_error(ctx: Ctx, status: int, text: str, what: str,
                   extra_hint: str = "") -> None:
    code = error_code(text)
    body = snippet(text)
    label = (" code=%s" % code) if code else ""
    if status == 400:
        ctx.die(EXIT_USAGE, "bad request (400 invalid_params) on %s%s"
                % (what, body))
    if status in (401, 403):
        ctx.die(EXIT_AUTH,
                "auth rejected (http %s): the api key is not valid for this "
                "base url/profile (%s)%s" % (status, what, body))
    if status == 404:
        ctx.die(EXIT_NOT_FOUND,
                "not found (404) on %s. Check the profile prefix (named "
                "profiles are served under /p/<profile>/) and note that a run "
                "keeps its status only briefly after a terminal state.%s"
                % (what, extra_hint or body))
    if status == 409:
        if code == "idempotency_key_conflict":
            ctx.die(EXIT_CONFLICT,
                    "idempotency_key_conflict: this idempotency key was "
                    "already used with a different payload; caller error, "
                    "not retryable")
        if code == "permission_denied":
            ctx.die(EXIT_CONFLICT,
                    "permission_denied: the server refused the request before "
                    "admission (typical cause: session_id names a session "
                    "another surface owns); caller error, not retryable")
        if code == "stale_generation":
            ctx.die(EXIT_CONFLICT,
                    "stale_generation: the referenced execution_generation is "
                    "not current; re-read the run status and retry with the "
                    "fresh values")
        if code == "not_found":
            ctx.die(EXIT_NOT_FOUND,
                    "not_found: the referenced admission or control does not "
                    "belong to this run")
        if code == "unknown_execution":
            ctx.die(EXIT_CONFLICT,
                    "unknown_execution: the run admission is unknown to the "
                    "server; not retryable")
        ctx.die(EXIT_CONFLICT, "conflict (409%s) on %s%s" % (label, what, body))
    if status in (429, 503):
        ctx.die(EXIT_UNAVAILABLE,
                "rate limited or unavailable (http %s%s) on %s: the retry "
                "budget is exhausted" % (status, label, what))
    ctx.die(EXIT_INTERNAL, "unexpected http status %s from %s%s"
            % (status, what, body))


def create_run(cfg: Config, ctx: Ctx, key: str, idem_key: str, body: bytes,
               max_retries: int) -> dict:
    headers = auth_headers(key)
    headers["Idempotency-Key"] = idem_key
    status, reply_headers, text = send_with_retry(
        cfg, ctx, "POST", cfg.api_url("v1/runs"), body=body,
        extra_headers=headers, max_retries=max_retries, what="run create")
    if status not in (200, 202):
        map_http_error(ctx, status, text, "run create")
    doc = parse_json_object(ctx, text, "run create")
    run_id = doc.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        ctx.die(EXIT_INTERNAL, "run create response has no run_id%s"
                % snippet(text))
    replayed = str(HeaderView(reply_headers).get("Idempotency-Replayed")
                   or "").strip().lower() == "true"
    return {"run_id": run_id, "replayed": replayed}


def get_run(cfg: Config, ctx: Ctx, key: str, run_id: str,
            max_retries: int) -> dict:
    status, _headers, text = send_with_retry(
        cfg, ctx, "GET", cfg.api_url("v1/runs/%s" % run_id),
        extra_headers=auth_headers(key), max_retries=max_retries,
        what="run status")
    if status != 200:
        map_http_error(ctx, status, text, "run status")
    doc = parse_json_object(ctx, text, "run status")
    if not isinstance(doc.get("status"), str):
        ctx.die(EXIT_INTERNAL, "run status response has no status field%s"
                % snippet(text))
    return doc


def stop_run(cfg: Config, ctx: Ctx, key: str, run_id: str,
             max_retries: int) -> dict:
    status, _headers, text = send_with_retry(
        cfg, ctx, "POST", cfg.api_url("v1/runs/%s/stop" % run_id),
        extra_headers=auth_headers(key), max_retries=max_retries,
        what="run stop")
    if status not in (200, 202):
        map_http_error(ctx, status, text, "run stop")
    return parse_json_object(ctx, text, "run stop")


def post_run_control(cfg: Config, ctx: Ctx, key: str, run_id: str, name: str,
                     payload_obj: dict) -> dict:
    status, _headers, text = send_with_retry(
        cfg, ctx, "POST", cfg.api_url("v1/runs/%s/%s" % (run_id, name)),
        body=canonical_json_bytes(payload_obj), extra_headers=auth_headers(key),
        max_retries=8, what=name)
    if status not in (200, 202):
        map_http_error(ctx, status, text, name)
    return parse_json_object(ctx, text, name)


def feature_true(caps, name: str) -> bool:
    features = (caps or {}).get("features")
    return isinstance(features, dict) and features.get(name) is True


def probe_capabilities(cfg: Config, ctx: Ctx, key: str,
                       enabled: bool = True) -> dict:
    """5.8: probe once; on any failure assume the core runs surface."""
    if not enabled:
        return {"features": dict(FALLBACK_FEATURES)}
    try:
        status, _headers, text = http_request(
            cfg, "GET", cfg.api_url("v1/capabilities"),
            extra_headers=auth_headers(key), timeout=CAP_TIMEOUT)
    except ApiError as exc:
        ctx.warn_once("capabilities probe unavailable (http %s); assuming the "
                      "core runs surface" % exc.status)
        return {"features": dict(FALLBACK_FEATURES)}
    except TransportError:
        ctx.warn_once("capabilities probe failed (transport error); assuming "
                      "the core runs surface")
        return {"features": dict(FALLBACK_FEATURES)}
    if status != 200:
        ctx.warn_once("capabilities probe unavailable (http %s); assuming the "
                      "core runs surface" % status)
        return {"features": dict(FALLBACK_FEATURES)}
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("features"), dict):
        ctx.warn_once("capabilities response missing features; assuming the "
                      "core runs surface")
        return {"features": dict(FALLBACK_FEATURES)}
    return doc


# ---------------------------------------------------------------------------
# Waiting: polling with backoff, SSE with reconnect + fallback, receipts.
# ---------------------------------------------------------------------------

def update_receipt_from_status(receipt_doc, status_doc: dict) -> None:
    if receipt_doc is None:
        return
    for src, dst in (("output", "last_output"), ("usage", "usage"),
                     ("runtime", "runtime"),
                     ("pending_controls", "pending_controls")):
        value = status_doc.get(src)
        if value is not None:
            receipt_doc[dst] = value
    if status_doc.get("admission_id") is not None \
            and status_doc.get("execution_generation") is not None:
        receipt_doc["admission"] = {
            "admission_id": status_doc.get("admission_id"),
            "execution_generation": status_doc.get("execution_generation"),
        }
    receipt_doc["status"] = status_doc["status"]
    receipt_doc["updated_at"] = utc_now()


def trunc_output(doc, cap: int):
    output = doc.get("last_output") if doc else None
    if isinstance(output, str) and cap and len(output) > cap:
        return output[:cap], True
    return output, False


def _print_controls(controls) -> None:
    print(scrub(json.dumps(controls, sort_keys=True, indent=2)))


def fail_wait_controls(ctx: Ctx, run_id: str, controls, base_payload: dict,
                       receipt_doc, receipt_path) -> None:
    """5.7: never auto-answer; print controls verbatim, record, exit 16."""
    if receipt_doc is not None:
        receipt_doc["pending_controls"] = controls
        receipt_doc["updated_at"] = utc_now()
        if receipt_path:
            write_receipt(ctx, receipt_path, receipt_doc)
    if controls is not None and not ctx.json_mode:
        _print_controls(controls)  # --json carries them in the payload
    request_id = ""
    generation = ""
    if isinstance(controls, list):
        for entry in controls:
            if isinstance(entry, dict) and (entry.get("prompt_id")
                                            or entry.get("request_id")):
                request_id = entry.get("prompt_id") or entry.get("request_id")
                gen = entry.get("execution_generation")
                generation = str(gen) if gen is not None else ""
                break
    ctx.die(EXIT_HUMAN,
            "run %s is waiting for a human decision (exit 16). Resolve it "
            "manually, for example: g2h resolve approval %s --request-id %s "
            "--generation %s --choice once|deny"
            % (run_id, run_id, request_id or "<prompt_id>",
               generation or "<execution_generation>"),
            payload=dict(base_payload, pending_controls=controls))


def fail_timeout(ctx: Ctx, base_payload: dict, timeout: float,
                 last_status) -> None:
    ctx.die(EXIT_TIMEOUT,
            "timeout: %.0fs elapsed while the run stayed non-terminal (last "
            "status: %s)" % (timeout, last_status or "none"),
            payload=dict(base_payload, last_status=last_status))


def _read_sse(response) -> bool:
    """Read one SSE stream. True on a terminal event, False on EOF/drop.
    Skips comment lines such as ': keepalive'."""
    event = None
    while True:
        raw_line = response.readline()
        if not raw_line:
            return False
        line = raw_line.rstrip(b"\r\n")
        if line.startswith(b":"):
            continue
        if not line.strip():
            if event in TERMINAL_EVENTS:
                return True
            event = None
            continue
        if line.startswith(b"event:"):
            event = line[len(b"event:"):].strip().decode("utf-8", "replace")
        elif line.startswith(b"data:"):
            pass  # payloads are informational; /status stays authoritative


def sse_wait(cfg: Config, ctx: Ctx, key: str, run_id: str, deadline: float) -> None:
    """Consume the events stream until a terminal event, a fallback to
    polling, or the deadline. The caller reconciles with one final GET."""
    failures: list[float] = []
    url = cfg.api_url("v1/runs/%s/events" % run_id)
    headers = dict({"User-Agent": USER_AGENT, "Accept": "text/event-stream"},
                   **auth_headers(key))
    while time.monotonic() < deadline:
        now = time.monotonic()
        if len([t for t in failures if now - t < 60.0]) >= 3:
            ctx.diagnostic("sse_fallback",
                           "3 failed events-stream attempts within 60s; "
                           "polling for the rest of this wait")
            return
        try:
            response = _OPENER.open(urllib.request.Request(
                url, headers=headers, method="GET"), timeout=SSE_READ_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.status in (404, 405):
                return  # endpoint missing on older gateways: silently poll
            failures.append(time.monotonic())
            ctx.diagnostic("sse_http_error", "events stream http %s"
                           % exc.status)
            time.sleep(1.0)
            continue
        except (urllib.error.URLError, OSError, socket.timeout) as exc:
            failures.append(time.monotonic())
            ctx.diagnostic("sse_transport_error", scrub(str(exc)))
            time.sleep(1.0)
            continue
        terminal = False
        try:
            terminal = _read_sse(response)
        except (OSError, socket.timeout) as exc:
            ctx.diagnostic("sse_transport_error", scrub(str(exc)))
        finally:
            try:
                response.close()
            except OSError:
                pass
        if terminal:
            return
        failures.append(time.monotonic())  # EOF or drop without terminal


def run_to_terminal(cfg: Config, ctx: Ctx, key: str, run_id: str,
                    receipt_doc, receipt_path, *, follow: bool,
                    timeout: float, max_retries: int, caps=None) -> int:
    deadline = time.monotonic() + timeout
    base_payload: dict = {"run_id": run_id}
    if receipt_path:
        base_payload["receipt_path"] = receipt_path
    if follow and feature_true(caps, "run_events_sse"):
        sse_wait(cfg, ctx, key, run_id, deadline)
    attempt = 0
    last_status = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail_timeout(ctx, base_payload, timeout, last_status)
        try:
            doc = get_run(cfg, ctx, key, run_id, max_retries)
        except ApiError as exc:
            map_http_error(ctx, exc.status, exc.text, "run status")
        except TransportError as exc:
            ctx.die(EXIT_TRANSPORT, "%s; transport retry budget exhausted"
                    % exc, payload=base_payload)
        status = doc["status"]
        last_status = status
        update_receipt_from_status(receipt_doc, doc)
        if receipt_doc is not None and receipt_path:
            write_receipt(ctx, receipt_path, receipt_doc)
        if status in TERMINAL_STATUSES:
            return EXIT_BY_STATUS[status]
        if status == "waiting_for_approval" or doc.get("pending_controls"):
            fail_wait_controls(ctx, run_id, doc.get("pending_controls"),
                               base_payload, receipt_doc, receipt_path)
        if status not in KNOWN_NON_TERMINAL:
            ctx.diagnostic("unknown_status",
                           "server reported an unrecognised status (%r); "
                           "treating it as non-terminal and polling on"
                           % status)
        time.sleep(min(remaining, backoff_delay(attempt)))
        attempt += 1


# ---------------------------------------------------------------------------
# CLI commands.
# ---------------------------------------------------------------------------

def resolve_input(ctx: Ctx, args) -> str:
    count = sum(1 for provided in (args.input is not None,
                                   args.input_file is not None,
                                   args.stdin) if provided)
    if count == 0:
        ctx.die(EXIT_USAGE,
                "provide exactly one of --input, --input-file or --stdin")
    if count > 1:
        ctx.die(EXIT_USAGE, "use only one of --input, --input-file, --stdin")
    if args.input is not None:
        return args.input
    if args.input_file is not None:
        try:
            with open(args.input_file, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            ctx.die(EXIT_USAGE, "cannot read --input-file: %s" % exc)
    return sys.stdin.read()


def load_target(ctx: Ctx, args):
    """Resolve (run_id, receipt_path, receipt_doc, stored_body)."""
    run_id = getattr(args, "run_id", None)
    receipt_path = getattr(args, "receipt", None)
    if run_id and receipt_path:
        ctx.die(EXIT_USAGE, "pass either a run id or --receipt, not both")
    if not run_id and not receipt_path:
        ctx.die(EXIT_USAGE, "pass a run id or --receipt PATH")
    doc = None
    raw = None
    if receipt_path:
        doc, raw = load_receipt(ctx, receipt_path)
        if doc.get("run_id"):
            run_id = doc["run_id"]
    return run_id, receipt_path, doc, raw


def resume_pending_receipt(cfg: Config, ctx: Ctx, key: str, receipt_path: str,
                           doc: dict, raw: bytes, max_retries: int) -> None:
    """5.2: a receipt without run_id re-POSTs the STORED bytes with the
    STORED key; the body is never rebuilt from flags."""
    result = create_run(cfg, ctx, key, doc["idempotency_key"], raw,
                        max_retries)
    doc["run_id"] = result["run_id"]
    doc["idempotency_replayed"] = bool(doc.get("idempotency_replayed")) \
        or result["replayed"]
    doc["status"] = "started"
    doc["updated_at"] = utc_now()
    write_receipt(ctx, receipt_path, doc)


def _output_human_lines(doc, run_id: str, receipt_path: str) -> list:
    lines = ["run_id: %s" % run_id]
    if receipt_path:
        lines.append("receipt: %s" % receipt_path)
    status = doc.get("status") if doc else None
    lines.append("status: %s" % status)
    if doc:
        output, truncated = trunc_output(doc, output_cap(False))
        if isinstance(output, str):
            if truncated:
                output += "\n... [output truncated; use --json for the full text]"
            lines.append("output: %s" % output)
        usage = doc.get("usage")
        if isinstance(usage, dict):
            lines.append("usage: %s" % json.dumps(usage, sort_keys=True))
    return lines


def output_cap(json_mode: bool) -> int:
    if json_mode:
        return 0  # full text in json mode
    raw = os.environ.get("G2H_OUTPUT_CAP")
    if raw is None:
        return DEFAULT_OUTPUT_CAP
    try:
        cap = int(raw)
    except ValueError:
        return DEFAULT_OUTPUT_CAP
    return cap if cap > 0 else 0


def cmd_dispatch(cfg: Config, ctx: Ctx, args) -> int:
    if args.key_stdin and args.stdin:
        ctx.die(EXIT_USAGE,
                "--key-stdin consumes stdin for the api key, so it cannot "
                "be combined with --stdin input (use --input or --input-file)")
    key = resolve_key(ctx, args)
    text = resolve_input(ctx, args)
    if not text.strip():
        ctx.die(EXIT_USAGE, "empty input: the server rejects empty runs")
    if not args.no_wait:
        validate_timeout(ctx, args.timeout)
    body_obj = {"input": text}
    for flag in ("instructions", "session_id", "model", "provider"):
        value = getattr(args, flag)
        if value:
            body_obj[flag] = value
    idem = args.idempotency_key or secrets.token_urlsafe(24)
    validate_idem_key(ctx, idem)
    ensure_receipt_dir(ctx, cfg.receipt_dir)
    receipt_path = receipt_file_name(cfg.receipt_dir)
    body = canonical_json_bytes(body_obj)
    receipt = new_receipt(cfg.base_url, cfg.profile, idem, body)
    write_receipt(ctx, receipt_path, receipt)
    caps = probe_capabilities(cfg, ctx, key,
                              enabled=not args.no_capabilities_check)
    result = create_run(cfg, ctx, key, idem, body, args.max_retries)
    receipt["run_id"] = result["run_id"]
    receipt["idempotency_replayed"] = result["replayed"]
    receipt["status"] = "started"
    receipt["updated_at"] = utc_now()
    write_receipt(ctx, receipt_path, receipt)
    payload = {"run_id": result["run_id"], "receipt_path": receipt_path,
               "idempotency_replayed": result["replayed"]}
    if result["replayed"]:
        ctx.diagnostic("idempotency_replayed",
                       "the server replayed the earlier run stored under "
                       "this idempotency key")
    if args.no_wait:
        ctx.finish(payload, ["run_id: %s" % result["run_id"],
                             "receipt: %s" % receipt_path,
                             "status: started (--no-wait)"])
        return EXIT_OK
    code = run_to_terminal(cfg, ctx, key, result["run_id"], receipt,
                           receipt_path, follow=args.follow,
                           timeout=args.timeout, max_retries=args.max_retries,
                           caps=caps)
    output, truncated = trunc_output(receipt, output_cap(ctx.json_mode))
    payload.update({"status": receipt["status"], "output": output,
                    "output_truncated": truncated,
                    "usage": receipt.get("usage"),
                    "runtime": receipt.get("runtime")})
    ctx.finish(payload, _output_human_lines(receipt, result["run_id"],
                                            receipt_path))
    return code


def cmd_status(cfg: Config, ctx: Ctx, args) -> int:
    key = resolve_key(ctx, args)
    run_id, receipt_path, doc, raw = load_target(ctx, args)
    if doc is not None and not doc.get("run_id"):
        resume_pending_receipt(cfg, ctx, key, receipt_path, doc, raw, 8)
        run_id = doc["run_id"]
    validate_run_id(ctx, run_id)
    status_doc = get_run(cfg, ctx, key, run_id, 8)
    update_receipt_from_status(doc, status_doc)
    if doc is not None and receipt_path:
        write_receipt(ctx, receipt_path, doc)
    status = status_doc["status"]
    code = EXIT_BY_STATUS.get(status, EXIT_OK)
    # waiting_for_approval observed by this one-shot query is a human
    # control: exit 16, the same contract dispatch and wait use.
    if status == "waiting_for_approval" or status_doc.get("pending_controls"):
        code = EXIT_HUMAN
    output, truncated = trunc_output(doc if doc else status_doc,
                                     output_cap(ctx.json_mode))
    payload = {"run_id": run_id, "status": status, "output": output,
               "output_truncated": truncated,
               "usage": status_doc.get("usage"),
               "runtime": status_doc.get("runtime"),
               "pending_controls": status_doc.get("pending_controls"),
               "detail": status_doc}
    if receipt_path:
        payload["receipt_path"] = receipt_path
    lines = ["run_id: %s" % run_id, "status: %s" % status]
    if isinstance(output, str):
        if truncated:
            output += "\n... [output truncated; use --json for the full text]"
        lines.append("output: %s" % output)
    ctx.finish(payload, lines)
    return code


def cmd_wait(cfg: Config, ctx: Ctx, args) -> int:
    key = resolve_key(ctx, args)
    validate_timeout(ctx, args.timeout)
    run_id, receipt_path, doc, raw = load_target(ctx, args)
    if doc is not None and not doc.get("run_id"):
        resume_pending_receipt(cfg, ctx, key, receipt_path, doc, raw,
                               args.max_retries)
        run_id = doc["run_id"]
    validate_run_id(ctx, run_id)
    caps = probe_capabilities(cfg, ctx, key,
                              enabled=not args.no_capabilities_check)
    code = run_to_terminal(cfg, ctx, key, run_id, doc, receipt_path,
                           follow=args.follow, timeout=args.timeout,
                           max_retries=args.max_retries, caps=caps)
    payload = {"run_id": run_id,
               "status": STATUS_BY_EXIT.get(code) or (doc or {}).get("status")}
    if doc is not None:
        output, truncated = trunc_output(doc, output_cap(ctx.json_mode))
        payload.update({"output": output, "output_truncated": truncated,
                        "usage": doc.get("usage"),
                        "runtime": doc.get("runtime"),
                        "idempotency_replayed":
                            bool(doc.get("idempotency_replayed"))})
    if receipt_path:
        payload["receipt_path"] = receipt_path
    ctx.finish(payload, _output_human_lines(doc, run_id, receipt_path))
    return code


def cmd_stop(cfg: Config, ctx: Ctx, args) -> int:
    key = resolve_key(ctx, args)
    validate_timeout(ctx, args.timeout)
    run_id, receipt_path, doc, _raw = load_target(ctx, args)
    validate_run_id(ctx, run_id)
    stop_run(cfg, ctx, key, run_id, args.max_retries)
    code = run_to_terminal(cfg, ctx, key, run_id, doc, receipt_path,
                           follow=False, timeout=args.timeout,
                           max_retries=args.max_retries, caps=None)
    status = STATUS_BY_EXIT.get(code) or (doc or {}).get("status")
    payload = {"run_id": run_id, "status": status}
    if receipt_path:
        payload["receipt_path"] = receipt_path
    ctx.finish(payload, ["stop requested; final status: %s" % status])
    return code


def cmd_resolve(cfg: Config, ctx: Ctx, args) -> int:
    key = resolve_key(ctx, args)
    validate_run_id(ctx, args.run_id)
    if args.resolve_command == "unknown":
        if not args.i_acknowledge_lost_execution:
            ctx.die(EXIT_USAGE,
                    "resolve unknown refuses to run without "
                    "--i-acknowledge-lost-execution: it asserts that the "
                    "lost execution will never finish")
        caps = probe_capabilities(cfg, ctx, key,
                                  enabled=not args.no_capabilities_check)
        if not feature_true(caps, "run_unknown_resolution"):
            ctx.die(EXIT_USAGE,
                    "this server does not advertise run_unknown_resolution; "
                    "resolve unknown is refused (it only exists while the "
                    "canonical session authority is active)")
        payload = {"admission_id": args.admission_id,
                   "execution_generation": args.generation}
    elif args.resolve_command == "approval":
        payload = {"request_id": args.request_id,
                   "execution_generation": args.generation,
                   "choice": args.choice}
    else:
        payload = {"request_id": args.request_id,
                   "execution_generation": args.generation,
                   "answer": args.answer}
    result = post_run_control(
        cfg, ctx, key, args.run_id,
        "resolve-unknown" if args.resolve_command == "unknown"
        else args.resolve_command, payload)
    ctx.finish({"run_id": args.run_id, "result": result},
               ["resolved: %s" % json.dumps(result, sort_keys=True)])
    return EXIT_OK


def cmd_capabilities(cfg: Config, ctx: Ctx, args) -> int:
    key = resolve_key(ctx, args)
    try:
        status, _headers, text = send_with_retry(
            cfg, ctx, "GET", cfg.api_url("v1/capabilities"),
            extra_headers=auth_headers(key), max_retries=8,
            what="capabilities probe")
    except ApiError as exc:
        map_http_error(ctx, exc.status, exc.text, "capabilities probe")
    except TransportError as exc:
        ctx.die(EXIT_TRANSPORT, "%s on capabilities probe" % exc)
    if status != 200:
        map_http_error(ctx, status, text, "capabilities probe")
    doc = parse_json_object(ctx, text, "capabilities probe")
    ctx.finish({"capabilities": doc},
               [json.dumps(doc, sort_keys=True, indent=2)])
    return EXIT_OK


# ---------------------------------------------------------------------------
# Read-only observation: poller, differ, event outbox, ledger, notifier and
# local dashboard. These paths only ever read run status from the gateway;
# they never create, cancel, or answer anything. Local files are the only
# state they change, and the optional notifier talks to exactly one
# operator-configured destination. The local server answers requests; the
# tool itself sends no requests from it.
# ---------------------------------------------------------------------------

NOTIFY_ENV_URL = "G2H_NOTIFY_URL"
NOTIFY_ENV_CMD = "G2H_NOTIFY_CMD"
NOTIFY_ENV_TOKEN = "G2H_NOTIFY_TOKEN"

DASHBOARD_PORT_DEFAULT = 8790
DASHBOARD_REFRESH_DEFAULT = 10.0
DASHBOARD_REFRESH_MIN = 5.0
DASHBOARD_POLL_DEFAULT = 15.0
DASHBOARD_POLL_MIN = 5.0
DASHBOARD_MAX_RUNS_DEFAULT = 50
RELAY_INTERVAL_DEFAULT = 30.0
RELAY_INTERVAL_MIN = 10.0
PRUNE_DAYS_DEFAULT = 30
POLLER_MAX_RETRIES = 2          # per status read; poller passes only
POLLER_POLITENESS_DELAY = 0.25
NOTIFY_TIMEOUT = 10.0
FRESH_INTERVALS = 2             # fresh while last_ok_at <= N poll intervals

CELL_CAP = 200                  # one dashboard cell
FIELD_CAP = 2000                # one expanded field
CONTROL_CONTEXT_CAP = 1000      # control context inside an event file
TASK_TEXT_CAP = 200             # task text in events, ledger and snapshot
ERROR_SNIPPET_CAP = 200         # error snippets everywhere
DERIVED_EXPIRY_SECONDS = 300    # documented default; the live config is not
DERIVED_EXPIRY_LABEL = ("derived: first_seen_at + 300s assumed "
                        "(gateway config not visible)")

LEDGER_NAME = "approvals.json"
EVENTS_DIRNAME = "events"
EVENT_REQUESTED = "approval.requested"
EVENT_RESOLVED = "approval.resolved"
EVENT_TERMINAL = "run.terminal"
EVENT_GATEWAY = "gateway.unreachable"
RESOLUTION_ANSWERED = "answered"
RESOLUTION_EXPIRED = "expired"
RESOLUTION_WITHDRAWN = "withdrawn"
RESOLUTION_UNKNOWN = "resolved_unknown"
LEDGER_OPEN_STATES = ("seen", "delivered", "notify_failed")
GATEWAY_OK = "ok"
GATEWAY_FAILING = "failing"
GATEWAY_LOG_CAP = 50

CSP_POLICY = "default-src 'none'; style-src 'self'"

NOTIFIER_LABELS = {
    "none": "notifier: none (local only)",
    "url": "notifier: webhook via G2H_NOTIFY_URL",
    "cmd": "notifier: command via --notify-cmd or G2H_NOTIFY_CMD",
}

_LIMITATIONS = (
    "runs dispatched by other clients (desktop app, webhooks, other tools) "
    "are not visible: g2h sees only the runs in its own receipts",
    "interactive surface prompts and their lifecycle are not visible: hooks "
    "would require an install",
    "kanban worker activity is not visible: separate surfaces, not Runs API "
    "runs",
    "plain session-chat human gates are not relayable: only run-scoped "
    "approvals surface pending controls",
)
RECEIPTS_ONLY_NOTE = (
    "no api key was provided: this pass read the local receipt store only; "
    "the gateway was never contacted and no run state was refreshed"
)

_PR_LINK_RE = re.compile(
    r"(?:https?://)?github\.com/([A-Za-z0-9_.-]+?)/([A-Za-z0-9_.-]+?)/pull/"
    r"([0-9]+)", re.IGNORECASE)


def _parse_iso_seconds(text):
    try:
        return calendar.timegm(time.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


def trunc_text(value, cap: int):
    if value is None:
        return None
    return str(value)[:cap]


def _field_text(value, cap: int):
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, sort_keys=True)
        except (TypeError, ValueError):
            value = repr(value)
    return scrub(value)[:cap]


def _control_request_id(element) -> str:
    if isinstance(element, dict):
        for field in ("prompt_id", "request_id"):
            value = element.get(field)
            if isinstance(value, str) and value:
                return value
    return ""


def _control_kind(element) -> str:
    if isinstance(element, dict):
        for field in ("control", "kind", "type"):
            value = element.get(field)
            if isinstance(value, str) and value:
                return value
    return "unspecified"


def _control_generation(element):
    if isinstance(element, dict):
        return element.get("execution_generation")
    return None


def _generation_text(element) -> str:
    generation = _control_generation(element)
    if generation is None:
        return ""
    if isinstance(generation, bool):
        return str(int(generation))
    if isinstance(generation, int):
        return str(generation)
    if isinstance(generation, str):
        return generation.strip()
    try:
        return json.dumps(generation, sort_keys=True)
    except (TypeError, ValueError):
        return repr(generation)


def dedup_key(run_id, element) -> str:
    """Control identity: sha256(run_id:prompt_id:execution_generation)."""
    raw = "%s:%s:%s" % (run_id, _control_request_id(element),
                        _generation_text(element))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def receipt_task_text(receipt, cap: int = TASK_TEXT_CAP):
    try:
        raw = base64.b64decode(str(receipt.get("body_b64") or ""),
                               validate=True)
        body = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    text = body.get("input") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text:
        return None
    return scrub(text)[:cap]


def _beyond_window(first_seen_at) -> bool:
    """Expiry is derived, never known: the pending deadline is invisible."""
    stamp = _parse_iso_seconds(first_seen_at)
    if stamp is None:
        return False
    return (time.time() - stamp) >= DERIVED_EXPIRY_SECONDS


def _resolution_for(status_after, elapsed_beyond_window: bool) -> str:
    """Observed-transition mapping; documented in docs/api-contracts.md."""
    if status_after in ("cancelled", "interrupted"):
        return RESOLUTION_WITHDRAWN
    if status_after in ("running", "stopping", "completed"):
        return RESOLUTION_ANSWERED
    return RESOLUTION_EXPIRED if elapsed_beyond_window else RESOLUTION_UNKNOWN


def _new_entry(run_id, element, kind: str, state: str, now: str) -> dict:
    return {"run_id": run_id, "state": state, "resolution": None,
            "request": {"run_id": run_id,
                        "prompt_id": _control_request_id(element) or None,
                        "execution_generation": _control_generation(element),
                        "kind": kind},
            "first_seen_at": now, "last_seen_at": now, "delivered_at": None,
            "attempts": 0, "last_error": None, "source_event": None}


def build_event_doc(kind: str, cfg: Config, receipt, receipt_name, run_id,
                    element, dedup: str, resolution, expiry, first_seen_at,
                    now: str) -> dict:
    task_text = receipt_task_text(receipt) if receipt else None
    control = None
    if kind in (EVENT_REQUESTED, EVENT_RESOLVED):
        if element is not None and not isinstance(element, dict):
            element_payload = {"opaque": repr(element)}
        else:
            element_payload = dict(element or {})
        try:
            context = json.dumps(element_payload, sort_keys=True)
        except (TypeError, ValueError):
            context = repr(element_payload)
        control = {"request_id": _control_request_id(element_payload) or None,
                   "execution_generation":
                       _control_generation(element_payload),
                   "kind": _control_kind(element_payload),
                   "context": scrub(context)[:CONTROL_CONTEXT_CAP],
                   "expiry": expiry}
    source = "GET /v1/runs/%s pending_controls" % run_id \
        if kind in (EVENT_REQUESTED, EVENT_RESOLVED) \
        else ("GET /v1/runs/%s" % run_id if kind == EVENT_TERMINAL
              else "gateway contact probe (transport errors)")
    return {
        "version": 1,
        "tool": "grokbot2hermes",
        "kind": kind,
        "profile": (receipt or {}).get("profile") or cfg.profile or "default",
        "base_url": (receipt or {}).get("base_url") or cfg.base_url,
        "run_id": run_id,
        "receipt": receipt_name,
        "task": {"text": task_text,
                 "created_at": (receipt or {}).get("created_at")},
        "control": control,
        "resolution": resolution if kind == EVENT_RESOLVED else None,
        "dedup_key": dedup,
        "first_seen_at": first_seen_at,
        "observed_at": now,
        "source": source,
    }


# ---------------------------------------------------------------------------
# Outbox hygiene: 0600 files in a 0700 dir under the receipt dir.
# ---------------------------------------------------------------------------

def ledger_path(cfg: Config) -> str:
    return os.path.join(cfg.receipt_dir, LEDGER_NAME)


def events_path(cfg: Config) -> str:
    return os.path.join(cfg.receipt_dir, EVENTS_DIRNAME)


def _fresh_ledger() -> dict:
    return {"version": 1, "entries": {}, "deliveries": {},
            "gateway": {"state": GATEWAY_OK, "last_transition_at": None,
                        "log": []}}


def _atomic_private_write(ctx: Ctx, path: str, text: str) -> None:
    if os.path.islink(path):
        ctx.die(EXIT_CREDENTIAL, "refusing symlinked target: %s" % path)
    target_dir = os.path.dirname(path) or "."
    try:
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".g2h-",
                                        suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        ctx.die(EXIT_INTERNAL, "cannot write %s: %s" % (path, exc))


def ensure_events_dir(ctx: Ctx, cfg: Config) -> bool:
    """Validate or create <receipts>/events 0700; False: no receipts root."""
    if not os.path.isdir(cfg.receipt_dir):
        return False
    if os.path.islink(cfg.receipt_dir):
        ctx.die(EXIT_CREDENTIAL, "refusing symlinked receipt dir: %s"
                % cfg.receipt_dir)
    mode = stat.S_IMODE(os.stat(cfg.receipt_dir).st_mode)
    if mode & 0o077:
        ctx.die(EXIT_CREDENTIAL, "receipt dir permissions must be 0700 "
                "(found %s): %s" % (oct(mode), cfg.receipt_dir))
    path = events_path(cfg)
    if os.path.islink(path):
        ctx.die(EXIT_CREDENTIAL, "refusing symlinked events dir: %s" % path)
    if os.path.exists(path):
        if not os.path.isdir(path):
            ctx.die(EXIT_CREDENTIAL, "events dir is not a directory: %s"
                    % path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            ctx.die(EXIT_CREDENTIAL, "events dir permissions must be 0700 "
                    "(found %s): %s" % (oct(mode), path))
        return True
    try:
        os.mkdir(path, 0o700)  # single level; the receipt root already exists
    except OSError as exc:
        ctx.die(EXIT_CREDENTIAL, "cannot create events dir: %s" % exc)
    if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
        ctx.die(EXIT_CREDENTIAL, "umask loosened the events dir mode: %s"
                % path)
    return True


def write_event_doc(ctx: Ctx, events_dir: str, doc: dict) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = "%s-%s" % (stamp, str(doc.get("dedup_key"))[:8])
    name = base + ".json"
    index = 1
    while os.path.exists(os.path.join(events_dir, name)):
        name = "%s-%d.json" % (base, index)
        index += 1
    text = scrub(scrub(json.dumps(doc, sort_keys=True, indent=2)))
    _atomic_private_write(ctx, os.path.join(events_dir, name), text)
    return name


def load_ledger(ctx: Ctx, cfg: Config) -> dict:
    path = ledger_path(cfg)
    if not os.path.exists(path):
        return _fresh_ledger()
    if os.path.islink(path):
        ctx.die(EXIT_CREDENTIAL, "refusing symlinked ledger: %s" % path)
    try:
        stats = os.lstat(path)
    except OSError as exc:
        ctx.die(EXIT_CREDENTIAL, "cannot read ledger: %s" % exc)
    if not stat.S_ISREG(stats.st_mode):
        ctx.die(EXIT_CREDENTIAL, "ledger is not a regular file: %s" % path)
    mode = stat.S_IMODE(stats.st_mode)
    if mode != 0o600:
        ctx.die(EXIT_CREDENTIAL, "ledger permissions must be 0600 "
                "(found %s): %s" % (oct(mode), path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as exc:
        ctx.die(EXIT_INTERNAL, "cannot parse ledger: %s" % exc)
    if not isinstance(doc, dict):
        ctx.die(EXIT_INTERNAL, "ledger is not a JSON object")
    ledger = _fresh_ledger()
    if isinstance(doc.get("entries"), dict):
        ledger["entries"] = dict(doc["entries"])
    if isinstance(doc.get("deliveries"), dict):
        ledger["deliveries"] = dict(doc["deliveries"])
    gateway = doc.get("gateway")
    if isinstance(gateway, dict):
        view = ledger["gateway"]
        if isinstance(gateway.get("state"), str):
            view["state"] = gateway["state"]
        if gateway.get("last_transition_at"):
            view["last_transition_at"] = gateway["last_transition_at"]
        if isinstance(gateway.get("log"), list):
            view["log"] = [str(line)
                           for line in gateway["log"][:GATEWAY_LOG_CAP]]
    return ledger


def write_ledger(ctx: Ctx, cfg: Config, ledger: dict) -> None:
    text = scrub(scrub(json.dumps(ledger, sort_keys=True, indent=2)))
    _atomic_private_write(ctx, ledger_path(cfg), text)


def scan_receipt_files(ctx: Ctx, cfg: Config) -> list:
    """Receipt (path, doc) pairs, newest first. Tolerates single bad files."""
    found = []
    for name in sorted(os.listdir(cfg.receipt_dir)):
        if not name.endswith(".json") or name == LEDGER_NAME:
            continue  # approvals.json is ledger state, not a receipt
        path = os.path.join(cfg.receipt_dir, name)
        if os.path.islink(path):
            ctx.diagnostic("receipt_skipped",
                           "%s: symlinked receipt rejected" % name)
            continue
        if not os.path.isfile(path):
            continue  # the events/ subdirectory and anything non-json
        try:
            doc, _raw_body = load_receipt(ctx, path)
        except G2hExit as exc:
            ctx.diagnostic("receipt_skipped", "%s: %s"
                           % (name, str(exc)[:ERROR_SNIPPET_CAP]))
            continue
        found.append((path, doc))
    found.sort(key=lambda pair: str((pair[1] or {}).get("created_at") or ""),
               reverse=True)
    return found


# ---------------------------------------------------------------------------
# Differ: consecutive observations become events; only the poller looks.
# ---------------------------------------------------------------------------

def _element_from_request(request: dict) -> dict:
    """Rebuild a minimal pending-controls element from a ledger request.

    The ledger stores only identity fields (run_id, prompt_id,
    execution_generation, kind); this maps them back onto the element keys
    the mock and the gateway use so older events stay inspectable.
    """
    element = {}
    if request.get("prompt_id"):
        element["prompt_id"] = request["prompt_id"]
    if isinstance(request.get("execution_generation"), int):
        element["execution_generation"] = request["execution_generation"]
    if request.get("kind"):
        element["control"] = request["kind"]
    return element


def diff_receipt(cfg: Config, receipt: dict, receipt_name: str, run_id: str,
                 status_doc: dict, ledger: dict) -> dict:
    old_status = receipt.get("status") \
        if isinstance(receipt.get("status"), str) else None
    new_status = status_doc.get("status")
    old_pending = receipt.get("pending_controls")
    old_pending = old_pending if isinstance(old_pending, list) else []
    new_pending = status_doc.get("pending_controls")
    new_pending = new_pending if isinstance(new_pending, list) else []
    old_map = {dedup_key(run_id, element): element
               for element in old_pending if isinstance(element, dict)}
    new_map = {dedup_key(run_id, element): element
               for element in new_pending if isinstance(element, dict)}
    now = utc_now()
    docs = []
    entries = ledger.setdefault("entries", {})
    # The ledger is the source of truth for what was already reported; the
    # receipt copy of pending_controls is stale by the time a pass runs.
    for entry_key, entry in list(entries.items()):
        if not isinstance(entry, dict) or entry.get("run_id") != run_id:
            continue
        if entry.get("state") not in LEDGER_OPEN_STATES:
            continue  # already requested; do not re-emit on resume
        if entry_key in new_map:
            continue  # still observed in this pass
        element = old_map.get(entry_key) or _element_from_request(
            entry.get("request") or {})
        beyond = _beyond_window(entry.get("first_seen_at"))
        resolution = _resolution_for(new_status, beyond)
        expiry = DERIVED_EXPIRY_LABEL \
            if resolution == RESOLUTION_EXPIRED else None
        docs.append(build_event_doc(
            EVENT_RESOLVED, cfg, receipt, receipt_name, run_id, element,
            entry_key, resolution, expiry, entry.get("first_seen_at"), now))
        entry["state"] = "resolved"
        entry["resolution"] = resolution
        entry["last_seen_at"] = now
        entries[entry_key] = entry
    for key, element in new_map.items():
        entry = entries.get(key)
        if entry is not None and entry.get("state") in LEDGER_OPEN_STATES:
            continue  # already requested; do not re-emit on resume
        if entry is not None and entry.get("state") == "resolved":
            # Pathological reappearance of an identical control identity
            # (same run_id:prompt_id:generation) after its entry already
            # resolved: record the observation, never re-emit. A genuinely
            # new control carries a new generation and a new dedup key.
            entry["last_seen_at"] = now
            entries[key] = entry
            continue
        if entry is None:
            entry = _new_entry(run_id, element, _control_kind(element),
                               "seen", now)
        if not entry.get("first_seen_at"):
            entry["first_seen_at"] = now
        entry["last_seen_at"] = now
        entries[key] = entry
        docs.append(build_event_doc(
            EVENT_REQUESTED, cfg, receipt, receipt_name, run_id, element,
            key, None, DERIVED_EXPIRY_LABEL, entry.get("first_seen_at"), now))
    if new_status in TERMINAL_STATUSES \
            and old_status not in TERMINAL_STATUSES:
        key = hashlib.sha256(("run.terminal:%s" % run_id)
                             .encode("utf-8")).hexdigest()
        entry = entries.get(key)
        if not (isinstance(entry, dict) and entry.get("state") == "resolved"):
            if entry is None:
                entry = _new_entry(run_id, None, EVENT_TERMINAL, "resolved",
                                   now)
            entry["state"] = "resolved"
            entry["last_seen_at"] = now
            entries[key] = entry
            docs.append(build_event_doc(
                EVENT_TERMINAL, cfg, receipt, receipt_name, run_id, None,
                key, None, None, entry.get("first_seen_at"), now))
    return {"events": docs, "status_changed": old_status != new_status}


def persist_events(ctx: Ctx, cfg: Config, ledger: dict, docs: list) -> list:
    names = []
    deliveries = ledger.setdefault("deliveries", {})
    entries = ledger.setdefault("entries", {})
    for doc in docs:
        name = write_event_doc(ctx, events_path(cfg), doc)
        names.append(name)
        deliveries[name] = {"attempts": 0, "delivered_at": None,
                            "last_error": None, "kind": doc["kind"],
                            "dedup_key": doc["dedup_key"]}
        entry = entries.get(doc["dedup_key"])
        if isinstance(entry, dict) and not entry.get("source_event"):
            entry["source_event"] = name
    ledger["_dirty"] = True
    return names


# ---------------------------------------------------------------------------
# Poller: one shared pass for dashboard and relay.
# ---------------------------------------------------------------------------

def _status_snippet(status_doc: dict) -> str:
    status = str(status_doc.get("status") or "")
    output = status_doc.get("output")
    if isinstance(output, str) and output:
        return scrub("%s: %s" % (status, output))[:CELL_CAP]
    return status


def _current_work(receipt: dict):
    event = receipt.get("last_event")
    if isinstance(event, dict) and event.get("snippet"):
        return _field_text(event.get("snippet"), CELL_CAP)
    return _field_text(receipt.get("last_output"), CELL_CAP)


def _row_base(receipt: dict, name: str, cfg: Config) -> dict:
    run_id = receipt.get("run_id")
    status = receipt.get("status")
    last_error = receipt.get("last_error")
    open_controls = []
    for element in receipt.get("pending_controls") or []:
        if element is None:
            continue
        open_controls.append(dict(element) if isinstance(element, dict)
                             else {"opaque": repr(element)})
    return {
        "receipt": name,
        "run_id": run_id if isinstance(run_id, str) else None,
        "profile": receipt.get("profile") or cfg.profile or "default",
        "status": status if isinstance(status, str) else None,
        "task": receipt_task_text(receipt),
        "task_created_at": receipt.get("created_at"),
        "created_at": receipt.get("created_at"),
        "updated_at": receipt.get("updated_at"),
        "last_ok_at": receipt.get("last_ok_at"),
        "last_error": scrub(str(last_error))[:ERROR_SNIPPET_CAP]
        if last_error else None,
        "last_event": dict(receipt["last_event"])
        if isinstance(receipt.get("last_event"), dict) else None,
        "current_work": _current_work(receipt),
        "output": _field_text(receipt.get("last_output"), FIELD_CAP),
        "usage": receipt.get("usage")
        if isinstance(receipt.get("usage"), dict) else None,
        "idem_present": bool(receipt.get("idempotency_key")),
        "open_controls": open_controls,
    }


def _poll_read_failed(ctx: Ctx, path: str, receipt: dict, message: str) -> None:
    """Keep the previous observed data; record the read error (runs stale)."""
    receipt["last_error"] = scrub(str(message))[:ERROR_SNIPPET_CAP]
    receipt["updated_at"] = utc_now()
    write_receipt(ctx, path, receipt)


def poll_pass(cfg: Config, ctx: Ctx, key: str, max_runs: int,
              contact: bool = True) -> dict:
    """One observation pass: GET run status only; local files are the only
    writes. Used by dashboard, relay and their serve/watch loops. With
    contact=False (keyless receipt-only dashboards) the run status fetch
    step is skipped entirely: every non-run receipt row is rendered from
    the receipt alone and no gateway request of any kind is made."""
    contacts = 0
    failures = 0
    total = 0
    selected = []
    if os.path.isdir(cfg.receipt_dir):
        ensure_events_dir(ctx, cfg)
        ledger = load_ledger(ctx, cfg)
        found = scan_receipt_files(ctx, cfg)
        total = len(found)
        selected = found[:max(0, int(max_runs))]
    else:
        ledger = _fresh_ledger()
    rows = []
    docs = []
    names = []
    for path, receipt in selected:
        name = os.path.basename(path)
        run_id = receipt.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            rows.append(_row_base(receipt, name, cfg))
            continue
        if not contact:
            rows.append(_row_base(receipt, name, cfg))
            continue
        status = receipt.get("status")
        if isinstance(status, str) and status in TERMINAL_STATUSES:
            rows.append(_row_base(receipt, name, cfg))
            continue
        if contacts + failures:
            time.sleep(POLLER_POLITENESS_DELAY)
        try:
            status_doc = get_run(cfg, ctx, key, run_id, POLLER_MAX_RETRIES)
        except G2hExit as exc:
            if exc.code == EXIT_AUTH:
                raise  # a bad key must stop everything loudly
            failures += 1
            _poll_read_failed(ctx, path, receipt, str(exc))
            rows.append(_row_base(receipt, name, cfg))
            continue
        except TransportError as exc:
            failures += 1
            _poll_read_failed(ctx, path, receipt, str(exc))
            rows.append(_row_base(receipt, name, cfg))
            continue
        contacts += 1
        diff = diff_receipt(cfg, receipt, name, run_id, status_doc, ledger)
        docs.extend(diff["events"])
        update_receipt_from_status(receipt, status_doc)
        if status_doc.get("pending_controls") is None:
            receipt["pending_controls"] = None
        now = utc_now()
        receipt["last_ok_at"] = now
        receipt["last_error"] = None
        if diff["events"]:
            receipt["last_event"] = {"name": diff["events"][-1]["kind"],
                                     "at": now,
                                     "snippet": _status_snippet(status_doc)}
        elif diff["status_changed"]:
            receipt["last_event"] = {"name": "status", "at": now,
                                     "snippet": _status_snippet(status_doc)}
        write_receipt(ctx, path, receipt)
        rows.append(_row_base(receipt, name, cfg))
    if docs:
        names.extend(persist_events(ctx, cfg, ledger, docs))
    gateway = ledger.setdefault("gateway", _fresh_ledger()["gateway"])
    ledger_dirty = bool(ledger.pop("_dirty", False))
    if contacts + failures:
        current = GATEWAY_FAILING if failures else GATEWAY_OK
        previous = gateway.get("state") or GATEWAY_OK
        gateway["log"] = [str(line)
                          for line in (gateway.get("log")
                                       or [])][-GATEWAY_LOG_CAP:]
        if current != previous:
            now = utc_now()
            gateway["state"] = current
            gateway["last_transition_at"] = now
            gateway["log"].append("unreachable at %s" % now
                                  if current == GATEWAY_FAILING
                                  else "recovered at %s" % now)
            ledger_dirty = True
            if current == GATEWAY_FAILING:
                dedup = hashlib.sha256(("gateway-unreachable:%s:%s"
                                        % (cfg.base_url, now))
                                       .encode("utf-8")).hexdigest()
                gateway_docs = [build_event_doc(
                    EVENT_GATEWAY, cfg, None, None, None, None, dedup, None,
                    None, None, now)]
                names.extend(persist_events(ctx, cfg, ledger, gateway_docs))
                docs.extend(gateway_docs)
    if ledger_dirty:
        write_ledger(ctx, cfg, ledger)
    return {"rows": rows, "events": docs, "event_names": names,
            "contacts": contacts, "failures": failures,
            "attempted": contacts + failures, "ledger": ledger,
            "receipts_total": total}


# ---------------------------------------------------------------------------
# Snapshot: freshness labels, blockers, decisions, PR links.
# ---------------------------------------------------------------------------

def find_pr_link(texts):
    for source, text in texts:
        if not isinstance(text, str) or not text:
            continue
        match = _PR_LINK_RE.search(text)
        if match:
            owner, repo, number = match.groups()
            return {"url": "https://github.com/%s/%s/pull/%s"
                    % (owner, repo, number),
                    "number": number, "derived_from": source}
    return None


def _freshness_label(row: dict, freshness_interval: float) -> tuple:
    """Per-row freshness verdict.

    A failed status read is recorded on that run's own receipt
    (last_error), so staleness from transport stays scoped to the run
    whose read failed; other rows keep their own age-based verdict.
    """
    status = row.get("status")
    if row.get("run_id") is None:
        return ("unknown (no run id yet; dispatch may have failed pre-create;"
                " see receipt)", True)
    if status in TERMINAL_STATUSES:
        return ("fresh (terminal; shown from receipt)", False)
    if status == "unknown":
        return ("unknown (gateway reports status unknown)", True)
    if row.get("last_error"):
        return ("stale (last status read failed; see the receipt's "
                "last_error)", True)
    last_ok = _parse_iso_seconds(row.get("last_ok_at"))
    if last_ok is None:
        return ("unknown (no successful status read yet)", True)
    age = max(0.0, time.time() - last_ok)
    window = FRESH_INTERVALS * max(0.0, freshness_interval)
    if age <= window:
        return ("fresh (last confirmed update %d seconds ago; within the "
                "%d second window)" % (int(age), int(window)), False)
    return ("stale (last confirmed update %d seconds ago; beyond the %d "
            "second window)" % (int(age), int(window)), True)


def _open_decisions(row: dict, entries: dict) -> list:
    run_id = row.get("run_id")
    decisions = []
    for element in row.get("open_controls") or []:
        key = dedup_key(run_id, element)
        entry = entries.get(key) or {}
        decisions.append({
            "kind": _control_kind(element),
            "request_id": _control_request_id(element) or None,
            "execution_generation": _control_generation(element),
            "expiry": DERIVED_EXPIRY_LABEL if entry.get("first_seen_at")
            else "unknown",
            "dedup_key": key,
            "note": "answer in your Hermes client; this dashboard cannot "
                    "answer",
        })
    return decisions


def _blockers(row: dict, stale_flag: bool, entries: dict) -> list:
    out = []
    status = row.get("status")
    if status == "waiting_for_approval":
        out.append("waiting for approval")
    if status == "stopping":
        out.append("stopping")
    if status == "failed":
        detail = row.get("last_error") or row.get("output") or ""
        out.append("failed" + (": " + detail[:CELL_CAP] if detail else ""))
    if stale_flag:
        out.append("transport stale")
    for entry in entries.values():
        request = (entry or {}).get("request") or {}
        if request.get("run_id") != row.get("run_id"):
            continue
        if (entry or {}).get("state") == "notify_failed":
            message = (entry or {}).get("last_error") or ""
            out.append("notify_failed" +
                       (": " + scrub(str(message))[:100] if message else ""))
    return out


def build_snapshot(cfg: Config, rows: list, ledger: dict, dest_kind: str,
                   contacts: int, failures: int, attempted: int,
                   freshness_interval: float, receipts_total: int,
                   max_runs: int, receipts_only: bool = False) -> dict:
    entries = ledger.get("entries") or {}
    runs = []
    for row in rows:
        label, stale_flag = _freshness_label(row, freshness_interval)
        decisions = _open_decisions(row, entries)
        blockers = _blockers(row, stale_flag, entries)
        pr_link = find_pr_link([("input", row.get("task")),
                                ("final output", row.get("output"))])
        finalized = dict(row)
        finalized["freshness"] = {"label": label, "stale": stale_flag}
        finalized["blockers"] = blockers
        finalized["decisions"] = decisions
        finalized["pr_link"] = pr_link
        finalized["open_controls"] = len(decisions)
        runs.append(finalized)
    gateway = ledger.get("gateway") or {}
    limitations: list = list(_LIMITATIONS)
    if receipts_only:
        limitations.append(RECEIPTS_ONLY_NOTE)
    return {
        "version": 1,
        "tool": "grokbot2hermes",
        "generated_at": utc_now(),
        "profile": cfg.profile or "default",
        "base_url": cfg.base_url,
        "gateway": {"state": gateway.get("state") or GATEWAY_OK,
                    "last_transition_at": gateway.get("last_transition_at"),
                    "contacts": contacts, "failures": failures,
                    "attempted": attempted},
        "gateway_contact": "not-contacted" if receipts_only
        else (gateway.get("state") or GATEWAY_OK),
        "notifier": NOTIFIER_LABELS.get(dest_kind, NOTIFIER_LABELS["none"]),
        "counts": {"shown": len(runs),
                   "hidden": max(0, receipts_total - len(runs)),
                   "total_receipts": receipts_total,
                   "open_decisions": sum(len(r["decisions"]) for r in runs)},
        "freshness_window_seconds": FRESH_INTERVALS
        * max(0.0, freshness_interval),
        "limitations": limitations,
        "runs": runs,
    }


def _empty_snapshot(cfg: Config, dest_kind: str, max_runs: int) -> dict:
    return build_snapshot(cfg, [], _fresh_ledger(), dest_kind, 0, 0, 0,
                          DASHBOARD_POLL_DEFAULT, 0, max_runs)


# ---------------------------------------------------------------------------
# (Renderer and local server live in g2h_ui.py: pure presentation layer,
# split out per the single-file size rule.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Local server: loopback only, GET-only answers, strict headers.
# ---------------------------------------------------------------------------

def _serve_poll_loop(cfg: Config, ctx: Ctx, key: str, args, dest_kind: str,
                     holder: dict, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            outcome = poll_pass(cfg, ctx, key, max_runs=args.max_runs)
            snapshot = build_snapshot(
                cfg, outcome["rows"], outcome["ledger"], dest_kind,
                outcome["contacts"], outcome["failures"],
                outcome["attempted"], args.poll_interval,
                outcome["receipts_total"], args.max_runs)
            with holder["lock"]:
                holder["snapshot"] = snapshot
                holder["error"] = None
        except G2hExit as exc:
            with holder["lock"]:
                holder["error"] = scrub(str(exc))
            if exc.code == EXIT_AUTH:
                break  # a bad key stops the poller; the page stays honest
        except Exception as exc:  # never let the poller thread die silently
            with holder["lock"]:
                holder["error"] = scrub("%s: %s" % (type(exc).__name__, exc))
        if stop.wait(args.poll_interval):
            break


def _serve_dashboard(cfg: Config, ctx: Ctx, key: str, args) -> int:
    port = _dashboard_port(ctx, args)
    dest_kind, _value = resolve_notifier_destination(ctx, args)
    lock = threading.Lock()
    holder = {"snapshot": None, "error": None, "lock": lock}
    try:
        outcome = poll_pass(cfg, ctx, key, max_runs=args.max_runs)
        snapshot = build_snapshot(
            cfg, outcome["rows"], outcome["ledger"], dest_kind,
            outcome["contacts"], outcome["failures"], outcome["attempted"],
            args.poll_interval, outcome["receipts_total"], args.max_runs)
        with lock:
            holder["snapshot"] = snapshot
    except G2hExit as exc:
        if exc.code in (EXIT_AUTH, EXIT_CREDENTIAL):
            raise
        with lock:
            holder["error"] = scrub(str(exc))
    stop = threading.Event()
    thread = threading.Thread(target=_serve_poll_loop, name="g2h-poller",
                              args=(cfg, ctx, key, args, dest_kind, holder,
                                    stop), daemon=True)
    thread.start()

    def page_fn(query: dict) -> bytes:
        refresh = args.refresh or 0.0
        if "refresh" in query and query["refresh"]:
            raw = query["refresh"][0].strip()
            try:
                value = float(raw)
            except ValueError:
                value = refresh
            if raw in ("0", "0.0"):
                refresh = 0.0
            else:
                refresh = max(DASHBOARD_REFRESH_MIN, value)
        with holder["lock"]:
            snapshot = holder["snapshot"]
            error = holder["error"]
        if snapshot is None:
            snapshot = _empty_snapshot(cfg, dest_kind, args.max_runs)
        page = g2h_ui.render_html(snapshot, error=error, refresh=refresh)
        return page.encode("utf-8")

    allowed_hosts = frozenset({"127.0.0.1:%d" % port,
                               "localhost:%d" % port})
    ctx.warn("serving the dashboard on http://127.0.0.1:%d/ (Ctrl+C stops;"
             " refresh %.0fs, poll interval %.0fs)"
             % (port, args.refresh or 0.0, args.poll_interval))
    try:
        g2h_ui.serve_dashboard(port, VERSION, allowed_hosts, page_fn)
    finally:
        stop.set()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Notifier: exactly one operator-configured destination; one attempt per
# event per invocation; a human does any retry with --redeliver.
# ---------------------------------------------------------------------------

def resolve_notifier_destination(ctx: Ctx, args) -> tuple:
    sources = []
    if getattr(args, "notify_url", None):
        sources.append(("url", args.notify_url.strip(), "--notify-url"))
    if getattr(args, "notify_cmd", None):
        sources.append(("cmd", args.notify_cmd, "--notify-cmd"))
    env_url = os.environ.get(NOTIFY_ENV_URL)
    if env_url and env_url.strip() and not args.notify_url:
        sources.append(("url", env_url.strip(), NOTIFY_ENV_URL))
    env_cmd = os.environ.get(NOTIFY_ENV_CMD)
    if env_cmd and env_cmd.strip() and not args.notify_cmd:
        sources.append(("cmd", env_cmd.strip(), NOTIFY_ENV_CMD))
    kinds = {source[0] for source in sources}
    if len(kinds) > 1:
        ctx.die(EXIT_USAGE,
                "conflicting notifier destinations: %s; configure exactly "
                "one" % ", ".join(source[2] for source in sources))
    if not sources:
        return "none", None
    kind, value, where = sources[0]
    if kind == "url":
        parts = urllib.parse.urlparse(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            ctx.die(EXIT_USAGE,
                    "invalid notifier URL from %s: expected an "
                    "http://host or https://host URL; the value is "
                    "never echoed" % where)
        token = os.environ.get(NOTIFY_ENV_TOKEN) or ""
        if not token.strip():
            ctx.die(EXIT_CREDENTIAL,
                    "%s needs a bearer token: set %s (the token is never "
                    "printed and never travels on argv)" % (where,
                                                            NOTIFY_ENV_TOKEN))
        scrub_register(token.strip())
        token_env_value = token.strip()
        scrub_register(token_env_value)
        return "url", value
    return "cmd", value


def notify_send(ctx: Ctx, dest: tuple, event_text: str) -> tuple:
    kind, value = dest
    try:
        if kind == "cmd":
            proc = subprocess.run([value], input=event_text.encode("utf-8"),
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE,
                                  timeout=NOTIFY_TIMEOUT)
            if proc.returncode == 0:
                return True, ""
            detail = proc.stderr.decode("utf-8", "replace").strip()
            if not detail:
                detail = proc.stdout.decode("utf-8", "replace").strip()
            return False, "exit %s: %s" % (proc.returncode,
                                           scrub(detail[:150]))
        headers = {"Content-Type": "application/json",
                   "User-Agent": USER_AGENT}
        token = (os.environ.get(NOTIFY_ENV_TOKEN) or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(value,
                                         data=event_text.encode("utf-8"),
                                         headers=headers)
        response = _OPENER.open(request, timeout=NOTIFY_TIMEOUT)
        with response:
            status = response.status
            body = response.read(CELL_CAP + 1).decode("utf-8", "replace")
        if 200 <= status < 300:
            return True, ""
        return False, "http %s: %s" % (status, scrub(body.strip()[:150]))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(CELL_CAP + 1).decode("utf-8", "replace")
        except OSError:
            body = ""
        return False, "http %s: %s" % (exc.code, scrub(body.strip()[:150]))
    except (urllib.error.URLError, OSError) as exc:
        # URLError covers DNS/refused; OSError covers socket.timeout in 3.9
        return False, "connection error: %s" % scrub(str(exc)[:150])
    except subprocess.TimeoutExpired:
        return False, "timed out after %ds" % int(NOTIFY_TIMEOUT)


def deliver_pass(ctx: Ctx, cfg: Config, dest: tuple, ledger: dict,
                 only_key=None) -> tuple:
    """One attempt per undelivered event; no automatic retry loops."""
    delivered = 0
    failed = 0
    lines = []
    deliveries = ledger.setdefault("deliveries", {})
    entries = ledger.setdefault("entries", {})
    outdir = events_path(cfg)
    for name in sorted(deliveries):
        info = deliveries[name]
        if only_key is not None:
            if info.get("dedup_key") != only_key:
                continue
        else:
            if info.get("delivered_at"):
                continue
            if info.get("attempts"):
                continue  # a failed attempt waits for --redeliver
        path = os.path.join(outdir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                event_text = handle.read()
        except OSError:
            continue  # pruned or missing: nothing to send
        key_short = str(info.get("dedup_key"))[:8]
        kind_label = str(info.get("kind"))
        delivered_at = utc_now()
        ok, err = notify_send(ctx, dest, event_text)
        info["attempts"] = int(info.get("attempts") or 0) + 1
        entry = entries.get(info.get("dedup_key"))
        if ok:
            info["delivered_at"] = delivered_at
            info["last_error"] = None
            delivered += 1
            lines.append("delivered via %s: %s (%s)"
                         % (dest[0], key_short, kind_label))
        else:
            info["last_error"] = scrub(str(err))[:CELL_CAP]
            failed += 1
            lines.append("notify_failed via %s: %s (%s): %s"
                         % (dest[0], key_short, kind_label,
                            info["last_error"]))
        if isinstance(entry, dict):
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            entry["last_error"] = None if ok else info["last_error"]
            if ok:
                entry["delivered_at"] = delivered_at
                if entry.get("state") in LEDGER_OPEN_STATES:
                    entry["state"] = "delivered"
            elif entry.get("state") in ("seen", "delivered"):
                entry["state"] = "notify_failed"
    if delivered or failed:
        ledger["_dirty"] = True
    return delivered, failed, lines


def redeliver_once(ctx: Ctx, cfg: Config, dest: tuple, ledger: dict,
                   key_prefix: str) -> dict:
    """Human-typed single reattempt; ambiguous or unknown keys are usage."""
    entries = ledger.setdefault("entries", {})
    deliveries = ledger.setdefault("deliveries", {})
    keys = {key for key in entries if str(key).startswith(key_prefix)}
    keys |= {str(info.get("dedup_key")) for info in deliveries.values()
             if str(info.get("dedup_key") or "").startswith(key_prefix)}
    if not keys:
        ctx.die(EXIT_USAGE, "--redeliver: no ledger entry matches %r"
                % key_prefix)
    if len(keys) > 1:
        ctx.die(EXIT_USAGE, "--redeliver: %d matches for %r; give a longer "
                "prefix" % (len(keys), key_prefix))
    only = keys.pop()
    delivered, failed, lines = deliver_pass(ctx, cfg, dest, ledger,
                                            only_key=only)
    return {"key": only, "delivered": delivered, "failed": failed,
            "lines": lines}


def prune_pass(ctx: Ctx, cfg: Config, ledger: dict, days: int) -> tuple:
    """Drop resolved entries and their files older than N days; no receipts."""
    if days < 0:
        return (0, 0)
    cutoff = time.time() - days * 86400
    outdir = events_path(cfg)
    try:
        dir_entries = os.scandir(outdir)
    except OSError:
        return (0, 0)
    file_names = [entry.name for entry in dir_entries
                  if entry.name.endswith(".json")]
    dir_entries.close()
    deliveries = ledger.get("deliveries") or {}
    entries = ledger.get("entries") or {}

    def stamp_age(name):
        stamp = name[:16]
        file_ts = _parse_stamp_seconds(stamp)
        if file_ts is None:
            return None
        return cutoff - file_ts

    old_resolved = []
    for key, entry in entries.items():
        if not isinstance(entry, dict) or entry.get("state") != "resolved":
            continue
        stamp = _parse_iso_seconds(entry.get("last_seen_at")) \
            or _parse_iso_seconds(entry.get("first_seen_at"))
        if stamp is not None and stamp <= cutoff:
            old_resolved.append(key)
    pruned_files = 0
    pruned_entries = 0
    dropped = set()

    def drop_file(name):
        if name in dropped:
            return False  # already unlinked in this pass; not an error
        full = os.path.join(outdir, name)
        if not os.path.exists(full):
            ctx.diagnostic("prune_skipped",
                           "event file already absent: %s" % name)
            return False
        if os.path.islink(full):
            ctx.diagnostic("prune_skipped",
                           "symlinked event file left in place: %s" % name)
            return False
        try:
            os.unlink(full)
        except OSError as exc:
            ctx.diagnostic("prune_skipped", "%s: %s" % (name, str(exc)))
            return False
        dropped.add(name)
        return True

    for key in old_resolved:
        for name in list(deliveries):
            info = deliveries.get(name) or {}
            if info.get("dedup_key") == key and stamp_age(name) is not None:
                if drop_file(name):
                    pruned_files += 1
                deliveries.pop(name, None)
        entry = entries.pop(key, None)
        still = (entry or {}).get("source_event")
        if still and stamp_age(still) is not None:
            if drop_file(still):
                pruned_files += 1
        pruned_entries += 1
    for name in file_names:
        if name in deliveries:
            continue  # its owner decides removal above
        age = stamp_age(name)
        if age is None or age < 0:
            continue
        dedup = (deliveries.get(name) or {}).get("dedup_key")
        if dedup and dedup in entries:
            continue  # entry not resolved: keep the file
        if not dedup and any(
                (entry or {}).get("source_event") == name
                for entry in entries.values()):
            continue
        if drop_file(name):
            pruned_files += 1
            deliveries.pop(name, None)
    if pruned_files or pruned_entries:
        ledger["_dirty"] = True
        ledger["deliveries"] = deliveries
        ledger["entries"] = entries
    return (pruned_files, pruned_entries)


def _parse_stamp_seconds(stamp: str):
    try:
        return calendar.timegm(time.strptime(stamp, "%Y%m%dT%H%M%SZ"))
    except (ValueError, TypeError):
        return None


def _transport_dead(outcome: dict) -> bool:
    return bool(outcome["attempted"]) and not outcome["contacts"]


def _dashboard_port(ctx: Ctx, args) -> int:
    raw = args.port
    if raw is None:
        raw = os.environ.get("G2H_DASHBOARD_PORT") or DASHBOARD_PORT_DEFAULT
    try:
        port = int(str(raw))
    except (TypeError, ValueError):
        ctx.die(EXIT_USAGE, "invalid dashboard port: %r" % (raw,))
    if not 1 <= port <= 65535:
        ctx.die(EXIT_USAGE, "dashboard port out of range: %d" % port)
    return port


def _validate_dashboard_flags(ctx: Ctx, args) -> None:
    if args.serve and args.out:
        ctx.die(EXIT_USAGE, "choose either --serve or --out, not both")
    if args.serve and args.json:
        ctx.die(EXIT_USAGE, "--json is not supported in serve mode")
    if args.bind not in ("127.0.0.1", "localhost"):
        ctx.die(EXIT_USAGE, "--bind accepts only 127.0.0.1 or localhost "
                "(loopback only)")
    if args.refresh and args.refresh != 0 \
            and args.refresh < DASHBOARD_REFRESH_MIN:
        ctx.die(EXIT_USAGE, "--refresh must be 0 or at least %s seconds"
                % DASHBOARD_REFRESH_MIN)
    if args.poll_interval < DASHBOARD_POLL_MIN:
        ctx.die(EXIT_USAGE, "--poll-interval must be at least %s seconds"
                % DASHBOARD_POLL_MIN)
    if args.max_runs < 1:
        ctx.die(EXIT_USAGE, "--max-runs must be at least 1")


def _validate_relay_flags(ctx: Ctx, args) -> None:
    if args.watch and args.once:
        ctx.die(EXIT_USAGE, "choose either --once or --watch, not both")
    if args.interval < RELAY_INTERVAL_MIN:
        ctx.die(EXIT_USAGE, "--interval must be at least %s seconds"
                % RELAY_INTERVAL_MIN)
    if args.prune_days < 0:
        ctx.die(EXIT_USAGE, "--prune-days cannot be negative")
    if args.max_runs < 1:
        ctx.die(EXIT_USAGE, "--max-runs must be at least 1")


def cmd_dashboard(cfg: Config, ctx: Ctx, args) -> int:
    _validate_dashboard_flags(ctx, args)
    serve_mode = bool(args.serve)
    # Serve mode polls the gateway continuously, so it always needs the
    # key: no remote capability without credentials. The snapshot surfaces
    # below are purely local reads of the receipt store; there the key is
    # optional and, when absent, contact=False keeps every gateway request
    # off the wire.
    if (serve_mode or args.key_stdin or args.key_file
            or (os.environ.get("G2H_API_KEY") or "").strip()):
        key = resolve_key(ctx, args)
        if serve_mode:
            # serve mode polls the gateway continuously: keyed only
            return _serve_dashboard(cfg, ctx, key, args)
        contact = True
    else:
        key = None
        contact = False
        ctx.warn("no api key found: rendering the receipt store only; "
                 "no gateway contact will be made")
    dest_kind, _value = resolve_notifier_destination(ctx, args)
    outcome = poll_pass(cfg, ctx, key or "", max_runs=args.max_runs,
                        contact=contact)
    snapshot = build_snapshot(
        cfg, outcome["rows"], outcome["ledger"], dest_kind,
        outcome["contacts"], outcome["failures"], outcome["attempted"],
        args.poll_interval, outcome["receipts_total"], args.max_runs,
        receipts_only=not contact)
    code = EXIT_TRANSPORT if _transport_dead(outcome) else EXIT_OK
    if args.out:
        text = json.dumps(snapshot, sort_keys=True, indent=2) \
            if args.json else g2h_ui.render_html(snapshot, refresh=args.refresh)
        parent = os.path.dirname(os.path.abspath(args.out))
        if not os.path.isdir(parent):
            ctx.die(EXIT_USAGE, "cannot write --out: directory does not "
                    "exist: %s" % parent)
        _atomic_private_write(ctx, args.out, scrub(scrub(text)))
        ctx.finish({"out": os.path.abspath(args.out),
                    "counts": snapshot["counts"]},
                   ["wrote: %s" % os.path.abspath(args.out)])
    elif args.json:
        ctx.emit({"snapshot": snapshot})
    else:
        text = g2h_ui.render_html(snapshot, refresh=args.refresh)
        print(scrub(scrub(text)))
    return code


def cmd_relay(cfg: Config, ctx: Ctx, args) -> int:
    key = resolve_key(ctx, args)
    _validate_relay_flags(ctx, args)
    dest_kind, dest_value = resolve_notifier_destination(ctx, args)
    if args.redeliver and dest_kind == "none":
        ctx.die(EXIT_USAGE, "--redeliver requires a notifier destination "
                "(--notify-url or --notify-cmd)")
    while True:
        outcome = poll_pass(cfg, ctx, key, max_runs=args.max_runs)
        ledger = outcome["ledger"]
        delivered = failed = 0
        delivery_lines = []
        if dest_kind != "none":
            delivered, failed, delivery_lines = deliver_pass(
                ctx, cfg, (dest_kind, dest_value), ledger)
        redelivery = None
        if args.redeliver:
            redelivery = redeliver_once(ctx, cfg, (dest_kind, dest_value),
                                        ledger, args.redeliver)
        pruned_files = pruned_entries = 0
        if args.prune_days:
            pruned_files, pruned_entries = prune_pass(ctx, cfg, ledger,
                                                      args.prune_days)
        if ledger.pop("_dirty", False):
            write_ledger(ctx, cfg, ledger)
        gateway_state = (ledger.get("gateway") or {}).get("state") \
            or GATEWAY_OK
        payload = {
            "events": [{"kind": doc["kind"],
                        "dedup_key": doc["dedup_key"],
                        "file": name,
                        "receipt": doc.get("receipt"),
                        "run_id": doc.get("run_id"),
                        "resolution": doc.get("resolution")}
                       for doc, name in zip(outcome["events"],
                                            outcome["event_names"])],
            "delivered": delivered,
            "notify_failed": failed,
            "notifier": NOTIFIER_LABELS[dest_kind],
            "gateway": gateway_state,
            "contacts": outcome["contacts"],
            "failures": outcome["failures"],
            "attempts": outcome["attempted"],
            "pruned": {"files": pruned_files, "entries": pruned_entries},
        }
        if redelivery:
            payload["redeliver"] = {"key": redelivery["key"],
                                    "delivered": redelivery["delivered"],
                                    "failed": redelivery["failed"]}
        lines = [NOTIFIER_LABELS[dest_kind],
                 "pass: %d receipts shown, %d reads ok, %d failed; gateway "
                 "%s" % (len(outcome["rows"]), outcome["contacts"],
                         outcome["failures"], gateway_state)]
        for doc, name in zip(outcome["events"], outcome["event_names"]):
            lines.append("event %s dedup=%s file=%s"
                         % (doc["kind"], str(doc["dedup_key"])[:8], name))
        lines.extend(delivery_lines)
        if redelivery:
            lines.append("redelivered %s: delivered %d, failed %d"
                         % (str(redelivery["key"])[:8],
                            redelivery["delivered"], redelivery["failed"]))
        if pruned_files or pruned_entries:
            lines.append("pruned %d event files and %d ledger entries"
                         % (pruned_files, pruned_entries))
        code = EXIT_TRANSPORT if _transport_dead(outcome) else EXIT_OK
        payload["exit_code"] = code
        ctx.finish(payload, lines)
        if not args.watch:
            return code
        time.sleep(args.interval)


# ---------------------------------------------------------------------------
# Parser and entry point.
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="g2h",
        description="Dispatch and track Hermes Agent runs. Not a migration "
                    "tool. See README.md.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default=None, metavar="URL",
                        help="gateway base url (env G2H_BASE_URL, default "
                        "http://127.0.0.1:8642)")
    common.add_argument("--profile", default=None, metavar="NAME",
                       help="named profile: routes become /p/<name>/ (env "
                        "G2H_PROFILE)")
    common.add_argument("--receipt-dir", default=None, metavar="DIR",
                        help="receipt directory, created 0700 (env "
                        "G2H_RECEIPT_DIR)")
    common.add_argument("--key-file", default=None, metavar="PATH",
                        help="read the api key from a 0600 file (no symlink)")
    common.add_argument("--key-stdin", action="store_true",
                        help="read the api key from stdin")
    common.add_argument("--json", action="store_true",
                        help="single-line JSON on stdout")
    common.add_argument("--no-capabilities-check", action="store_true",
                        help="skip the capabilities probe (dispatch/wait)")
    common.add_argument("--allow-insecure-http", action="store_true",
                        help="allow plain http to non-loopback hosts (warns)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dispatch", parents=[common],
                       help="create a run and (by default) wait for a "
                       "terminal state")
    p.add_argument("--input", default=None, metavar="TEXT")
    p.add_argument("--input-file", default=None, metavar="PATH")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--instructions", default=None, metavar="TEXT")
    p.add_argument("--session-id", default=None, dest="session_id",
                   metavar="ID")
    p.add_argument("--model", default=None, metavar="MODEL")
    p.add_argument("--provider", default=None, metavar="PROVIDER")
    p.add_argument("--idempotency-key", default=None, dest="idempotency_key",
                   metavar="KEY")
    p.add_argument("--no-wait", action="store_true", dest="no_wait")
    p.add_argument("--follow", action="store_true",
                   help="wait via the SSE events stream with polling fallback")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   metavar="SECS")
    p.add_argument("--max-retries", type=int, default=8, dest="max_retries",
                   metavar="N")

    p = sub.add_parser("status", parents=[common],
                       help="one status GET; exit code reflects the run")
    p.add_argument("run_id", nargs="?", default=None, metavar="RUN_ID")
    p.add_argument("--receipt", default=None, metavar="PATH")

    p = sub.add_parser("wait", parents=[common],
                       help="block until a terminal state")
    p.add_argument("run_id", nargs="?", default=None, metavar="RUN_ID")
    p.add_argument("--receipt", default=None, metavar="PATH")
    p.add_argument("--follow", action="store_true")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   metavar="SECS")
    p.add_argument("--max-retries", type=int, default=8, dest="max_retries",
                   metavar="N")

    p = sub.add_parser("stop", parents=[common],
                       help="POST stop, then poll until the run settles ("
                       "never instant)")
    p.add_argument("run_id", nargs="?", default=None, metavar="RUN_ID")
    p.add_argument("--receipt", default=None, metavar="PATH")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   metavar="SECS")
    p.add_argument("--max-retries", type=int, default=8, dest="max_retries",
                   metavar="N")

    p = sub.add_parser("resolve", parents=[common],
                       help="manual operator resolution (never automatic)")
    resolved = p.add_subparsers(dest="resolve_command", required=True)
    q = resolved.add_parser("approval")
    q.add_argument("run_id", metavar="RUN_ID")
    q.add_argument("--request-id", required=True, dest="request_id",
                   metavar="ID")
    q.add_argument("--generation", required=True, type=int, metavar="N")
    q.add_argument("--choice", required=True, choices=["once", "deny"])
    q = resolved.add_parser("clarify")
    q.add_argument("run_id", metavar="RUN_ID")
    q.add_argument("--request-id", required=True, dest="request_id",
                   metavar="ID")
    q.add_argument("--generation", required=True, type=int, metavar="N")
    q.add_argument("--answer", required=True, metavar="TEXT")
    q = resolved.add_parser("unknown")
    q.add_argument("run_id", metavar="RUN_ID")
    q.add_argument("--admission-id", required=True, dest="admission_id",
                   metavar="ID")
    q.add_argument("--generation", required=True, type=int, metavar="N")
    q.add_argument("--i-acknowledge-lost-execution", action="store_true",
                   dest="i_acknowledge_lost_execution")

    p = sub.add_parser("capabilities", parents=[common],
                       help="GET /v1/capabilities and pretty-print it")
    p = sub.add_parser("dashboard", parents=[common],
                       help="read-only local view over g2h's own receipts "
                       "(GET-only; never answers anything)")
    p.add_argument("--serve", action="store_true",
                   help="serve on loopback until Ctrl+C (default: print the "
                   "page once)")
    p.add_argument("--out", default=None, metavar="FILE",
                   help="write the page to FILE instead of stdout")
    p.add_argument("--port", type=int, default=None, metavar="N",
                   help="port for --serve (default 8790, env "
                   "G2H_DASHBOARD_PORT)")
    p.add_argument("--bind", default="127.0.0.1", metavar="HOST",
                   help="loopback only: 127.0.0.1 or localhost (anything "
                   "else exits with usage)")
    p.add_argument("--refresh", type=float, default=DASHBOARD_REFRESH_DEFAULT,
                   dest="refresh", metavar="SECS",
                   help="page meta refresh seconds; 0 disables (min %s)"
                   % DASHBOARD_REFRESH_MIN)
    p.add_argument("--poll-interval", type=float,
                   default=DASHBOARD_POLL_DEFAULT, dest="poll_interval",
                   metavar="SECS",
                   help="serve-mode background poll interval in seconds "
                   "(min %s)" % DASHBOARD_POLL_MIN)
    p.add_argument("--max-runs", type=int, default=DASHBOARD_MAX_RUNS_DEFAULT,
                   dest="max_runs", metavar="N",
                   help="newest N receipts, others summarized as a count")
    p = sub.add_parser("relay", parents=[common],
                       help="observe approval lifecycle events into a local "
                       "outbox; optional one-destination notifier")
    p.add_argument("--once", action="store_true",
                   help="one pass (default when neither --once nor --watch)")
    p.add_argument("--watch", action="store_true",
                   help="loop passes until Ctrl+C")
    p.add_argument("--interval", type=float, default=RELAY_INTERVAL_DEFAULT,
                   dest="interval", metavar="SECS",
                   help="seconds between passes in --watch (min %s)"
                   % RELAY_INTERVAL_MIN)
    p.add_argument("--notify-url", default=None, dest="notify_url",
                   metavar="URL",
                   help="deliver events to one webhook URL (bearer token "
                   "from %s; value never printed)" % NOTIFY_ENV_TOKEN)
    p.add_argument("--notify-cmd", default=None, dest="notify_cmd",
                   metavar="CMD",
                   help="deliver events to one command (event JSON on "
                   "stdin, exit 0 means delivered)")
    p.add_argument("--redeliver", default=None, metavar="KEY_PREFIX",
                   help="one human-initiated reattempt for one event whose "
                   "dedup_key starts with the prefix")
    p.add_argument("--prune-days", type=int, default=PRUNE_DAYS_DEFAULT,
                   dest="prune_days", metavar="N",
                   help="drop resolved events and ledger entries older than "
                   "N days (default %d; receipts are never touched)"
                   % PRUNE_DAYS_DEFAULT)
    p.add_argument("--max-runs", type=int, default=DASHBOARD_MAX_RUNS_DEFAULT,
                   dest="max_runs", metavar="N")
    return parser


HANDLERS = {"dispatch": cmd_dispatch, "status": cmd_status, "wait": cmd_wait,
            "stop": cmd_stop, "resolve": cmd_resolve,
            "capabilities": cmd_capabilities, "dashboard": cmd_dashboard,
            "relay": cmd_relay}


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    ctx = Ctx(bool(args.json), args.command)
    try:
        cfg = Config.from_args(ctx, args)
        code = HANDLERS[args.command](cfg, ctx, args)
    except G2hExit as exc:
        code = exc.code
    except TransportError as exc:
        # Transport failures are the control plane, not bugs: every command
        # dies EXIT_TRANSPORT at the retry budget (README exit table).
        message = ("transport failure after retries: %s; check gateway "
                   "reachability: is the gateway running, and is --base-url "
                   "or G2H_BASE_URL correct?" % scrub(str(exc)))
        ctx.diagnostic("transport_error", message)
        ctx.warn(message)
        ctx.emit({"error": message, "exit_code": EXIT_TRANSPORT})
        code = EXIT_TRANSPORT
    except KeyboardInterrupt:
        ctx.warn("interrupted by user")
        code = 130
    except Exception as exc:  # unexpected client bug: scrubbed report
        message = scrub("%s: %s" % (type(exc).__name__, exc))
        ctx.diagnostic("internal_error", message)
        ctx.warn("internal error: %s (this is a g2h bug)" % message)
        ctx.emit({"error": "internal error: %s" % message,
                  "exit_code": EXIT_INTERNAL})
        code = EXIT_INTERNAL
    sys.exit(code)


if __name__ == "__main__":
    main()
