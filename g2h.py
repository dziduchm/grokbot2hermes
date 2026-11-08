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
import hashlib
import json
import os
import random
import re
import secrets
import socket
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

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

    def die(self, code: int, message: str, payload=None) -> None:
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
    return parser


HANDLERS = {"dispatch": cmd_dispatch, "status": cmd_status, "wait": cmd_wait,
            "stop": cmd_stop, "resolve": cmd_resolve,
            "capabilities": cmd_capabilities}


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
