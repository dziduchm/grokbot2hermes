"""G2C run-approvals chat alerts: the separate sender process (Marcin-only install).

Reads the gateway's alert ledger (the JSONL at HERMES_WEBAPP_RUN_ALERTS_OUTBOX, written
by the in-gateway emitter), takes the first pending record per dedupe key
(profile|run_id|prompt_id|generation) and POSTs it to the Grok Bot routine webhook that
a 0600 webhook file describes. This process holds ONLY the webhook url and sender key:
no Hermes API key, no cookie, no signing key, no gateway access of any kind. Stdlib
only, Python 3.9+, no dependencies.

Ledger contract (mirrors the emitter):
- one JSON object per line; single-line appends by the gateway, status transitions
  pending -> sent | given_up written here by full-file atomic rewrite (tmp + rename,
  file 0600, dir 0700);
- statuses: pending, sent, given_up, closed, superseded; only pending records are ever
  POSTed, and only the FIRST pending record per key (any later same-key pending record
  is marked superseded - belt and braces against any future second writer);
- failed POSTs increment the record bookkeeping field attempts; after MAX_ATTEMPTS the
  key is marked given_up permanently (no infinite retry, no spam);
- records older than PRUNE_DAYS (14) are pruned on write; hard cap MAX_RECORDS (5000);
- malformed lines are skipped on load and quarantined once to alerts.bad.jsonl next to
  the ledger.

The POST payload is re-checked field by field against a strict allowlist before any
byte leaves: only the alert's page-visible redacted fields, never any token, digest,
bookkeeping or secret material, and never a link for a closed record. Closed records
are ledger-only by default; with notify-closed enabled (flag or env, same names as the
gateway side) each closed key gets exactly one link-free one-line chat follow-up.

Log lines are capped (LOG_LINE_CAP) and never contain the webhook url or sender key:
argv carries paths and flags only.
"""
import argparse
import json
import logging
import os
import stat
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError

LOG_LINE_CAP = 500
PRUNE_DAYS = 14
MAX_RECORDS = 5000
MAX_ATTEMPTS = 3
POST_TIMEOUT_S = 10.0
MAX_PAYLOAD_BYTES = 3072
RECORD_VERSION = 1

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_GIVEN_UP = "given_up"
STATUS_CLOSED = "closed"
STATUS_SUPERSEDED = "superseded"
KNOWN_STATUSES = frozenset((STATUS_PENDING, STATUS_SENT, STATUS_GIVEN_UP, STATUS_CLOSED,
                            STATUS_SUPERSEDED))

LEDGER_DIR_MODE = 0o700
LEDGER_FILE_MODE = 0o600

DEFAULT_WEBHOOK_PATH = "/Users/md/.bfc-pipeline/secrets/g2c-grokbot-webhook.json"
DEFAULT_KEY_HEADER = "X-Sender-Key"
USER_AGENT = "g2c-runalerts-sender/1"
NOTIFY_CLOSED_ENV_NAMES = ("G2C_RUNALERTS_NOTIFY_CLOSED",
                           "HERMES_WEBAPP_RUN_ALERTS_NOTIFY_CLOSED")
CLOSED_MESSAGE = "waiter closed (settled or expired), no action needed"
SELF_TEST_MESSAGE = "G2C sender self-test, no waiter"

# The POST payload allowlist: exactly what the alert records carry, re-checked field
# by field before sending. Bookkeeping (status, attempts, closed bookkeeping stamps)
# never rides the payload; the alert-facing fields are the emitter's record allowlist.
PAYLOAD_ALLOWLIST = frozenset((
    "v", "key", "profile", "kind", "run_id", "prompt_id", "session_id",
    "execution_generation", "actionable", "first_seen_iso_utc", "expires_london",
    "label", "reason", "command", "question", "link", "binding_refusal", "note",
))
BOOKKEEPING_FIELDS = frozenset((
    "status", "attempts", "outcome", "closed_iso_utc", "closed_sent_iso_utc",
    "closed_gave_up_iso_utc",
))

_INT_FIELDS = ("execution_generation",)
_STR_FIELDS = ("key", "profile", "kind", "run_id", "prompt_id", "session_id",
               "first_seen_iso_utc", "expires_london", "label", "reason", "command",
               "question", "link", "binding_refusal", "note")

QUARANTINE_SUFFIX = ".bad.jsonl"


class AllowlistRefusal(Exception):
    """A record failed the strict field-by-field allowlist re-check: nothing POSTs."""


_log = logging.getLogger("g2c.runalerts")


def capped(message: str) -> str:
    text = str(message)
    if len(text) > LOG_LINE_CAP:
        text = text[:LOG_LINE_CAP - 3] + "..."
    return text


def log_event(message: str) -> None:
    _log.info(capped(message))


# ---------------------------------------------------------------------------
# webhook file
# ---------------------------------------------------------------------------

def load_webhook(path) -> Dict[str, str]:
    """The ONLY credential source: a 0600 JSON file with url, sender_key and optional
    key_header. Loopback http is allowed for trials; everything else must be https."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AllowlistRefusal("webhook file missing: " + str(path)) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AllowlistRefusal("webhook file unreadable") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise AllowlistRefusal("webhook file is not valid JSON") from exc
    if not isinstance(data, dict):
        raise AllowlistRefusal("webhook file must hold a JSON object")
    url = data.get("url")
    key = data.get("sender_key")
    header = data.get("key_header", DEFAULT_KEY_HEADER)
    if not isinstance(url, str) or not url:
        raise AllowlistRefusal("webhook file url missing")
    if not isinstance(key, str) or not key:
        raise AllowlistRefusal("webhook file sender_key missing")
    if not isinstance(header, str) or not header:
        raise AllowlistRefusal("webhook file key_header missing")
    lowered = url.lower()
    loopback = ("//127.0.0.1" in lowered or "//localhost" in lowered
                or "//[::1]" in lowered)
    if not lowered.startswith("https://") and not loopback:
        raise AllowlistRefusal("webhook url must be https (loopback http is trial-only)")
    try:
        mode = stat.S_IMODE(os.stat(str(path)).st_mode)
        if mode & 0o077:
            _log.warning(capped("webhook file mode is looser than 0600: "
                                + format(mode, "o")))
    except OSError:
        pass
    return {"url": url, "sender_key": key, "key_header": header}


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def quarantine_path(ledger: Path) -> Path:
    return ledger.parent / (ledger.name[: -len(".jsonl")] + QUARANTINE_SUFFIX
                            if ledger.name.endswith(".jsonl")
                            else ledger.name + QUARANTINE_SUFFIX)


def _quarantine(bad_lines: List[str], target: Path) -> None:
    if not bad_lines:
        return
    seen = set()
    if target.exists():
        try:
            seen.update(target.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            seen = set()
    fresh = [line for line in bad_lines if line not in seen]
    if not fresh:
        return
    try:
        os.makedirs(str(target.parent), exist_ok=True)
        os.chmod(str(target.parent), LEDGER_DIR_MODE)
        with open(str(target), "a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in fresh))
        os.chmod(str(target), LEDGER_FILE_MODE)
    except OSError:
        _log.warning(capped("sender: quarantine write refused"))


def load_records(ledger: Path) -> Tuple[List[dict], List[str]]:
    """Valid single JSON-object lines only; malformed lines are quarantined once each
    (the same guarantee as the emitter's loader) and the loop never crashes on them."""
    try:
        raw = Path(ledger).read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeDecodeError):
        _log.warning(capped("sender: ledger unreadable"))
        return [], []
    records: List[dict] = []
    bad: List[str] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad.append(line)
            continue
        if not isinstance(record, dict):
            bad.append(line)
            continue
        records.append(record)
    _quarantine(bad, quarantine_path(ledger))
    return records, bad


def _record_stamp(record: dict) -> Optional[datetime]:
    stamp_text = record.get("closed_iso_utc") if record.get("status") == STATUS_CLOSED \
        else record.get("first_seen_iso_utc")
    if not isinstance(stamp_text, str) or not stamp_text:
        return None
    try:
        stamp = datetime.fromisoformat(stamp_text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def prune(records: List[dict], now: datetime) -> List[dict]:
    """Drop records older than PRUNE_DAYS (by closed time for closed records, by
    first_seen otherwise), then keep at most the last MAX_RECORDS in file order."""
    kept: List[dict] = []
    for record in records:
        stamp = _record_stamp(record)
        if stamp is None:
            kept.append(record)
            continue
        if now - stamp < timedelta(days=PRUNE_DAYS):
            kept.append(record)
    return kept[-MAX_RECORDS:]


def _ensure_ledger(ledger: Path) -> None:
    os.makedirs(str(ledger.parent), exist_ok=True)
    try:
        os.chmod(str(ledger.parent), LEDGER_DIR_MODE)
    except OSError:
        _log.warning(capped("sender: ledger dir chmod refused"))


def rewrite_records(ledger: Path, records: List[dict]) -> None:
    """Full-file atomic rewrite: mkstemp in the ledger dir, fsync, chmod 0600, rename."""
    _ensure_ledger(ledger)
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records)
    fd, tmp_path = tempfile.mkstemp(dir=str(ledger.parent), prefix=".alerts-",
                                    suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, LEDGER_FILE_MODE)
        os.replace(tmp_path, str(ledger))
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# payload allowlist
# ---------------------------------------------------------------------------

def payload_from_record(record: dict) -> dict:
    """Re-check the record field by field and build the exact POST payload. Anything
    unknown, misplaced or oversized refuses the whole record: fail closed."""
    status = record.get("status")
    if status not in KNOWN_STATUSES:
        raise AllowlistRefusal("unknown status")
    unknown = set(record) - PAYLOAD_ALLOWLIST - BOOKKEEPING_FIELDS
    if unknown:
        raise AllowlistRefusal("fields outside the allowlist: " + ",".join(sorted(unknown)))
    payload: dict = {}
    for field in PAYLOAD_ALLOWLIST:
        if field not in record:
            continue
        value = record[field]
        if field == "v":
            if value != RECORD_VERSION:
                raise AllowlistRefusal("bad record version")
            payload["v"] = value
        elif field == "execution_generation":
            if type(value) is not int or value < 0:
                raise AllowlistRefusal("bad execution_generation")
            payload[field] = value
        elif field == "actionable":
            if type(value) is not bool:
                raise AllowlistRefusal("bad actionable")
            payload[field] = value
        elif field in _STR_FIELDS:
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > 4096:
                raise AllowlistRefusal("bad " + field)
            payload[field] = value
    kind = payload.get("kind")
    actionable = payload.get("actionable")
    if kind not in ("approval", "clarify"):
        raise AllowlistRefusal("unknown kind")
    if "link" in payload and status == STATUS_CLOSED:
        raise AllowlistRefusal("closed record must never carry a link")
    if kind == "approval" and "question" in payload:
        raise AllowlistRefusal("approval must not carry a question")
    if kind == "clarify" and "command" in payload:
        raise AllowlistRefusal("clarify must not carry a command")
    if actionable is False and any(f in payload for f in ("command", "question",
                                                          "reason", "label")):
        raise AllowlistRefusal("nonactionable record carries display text")
    if "attempts" in record:
        attempts = record["attempts"]
        if type(attempts) is not int or attempts < 0 or attempts > MAX_ATTEMPTS:
            raise AllowlistRefusal("bad attempts")
    if not payload.get("key"):
        raise AllowlistRefusal("missing dedupe key")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise AllowlistRefusal("payload over the size cap")
    return payload


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

def _no_redirect_opener():
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # any redirect is a failed attempt: never re-send the key

    return urllib.request.build_opener(_NoRedirect)


def post_payload(webhook: Dict[str, str], payload: dict) -> int:
    """One POST, 10s timeout, sender key only in the header. Returns the HTTP status;
    raises on any transport or HTTP error (callers count the attempt)."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        webhook["url"], data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 webhook["key_header"]: webhook["sender_key"]})
    with _no_redirect_opener().open(request, timeout=POST_TIMEOUT_S) as response:
        return int(response.getcode())


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _mark(record: dict, status: str) -> bool:
    if record.get("status") == status:
        return False
    record["status"] = status
    return True


def run_once(ledger, webhook_path, notify_closed: bool = False,
             now: Optional[datetime] = None) -> dict:
    """One sender tick: load the webhook file (missing means clean exit, ledger
    untouched), read the ledger, POST the first pending record per key, close the
    books with one atomic rewrite when anything changed. Returns per-run counts."""
    now = now or datetime.now(timezone.utc)
    ledger = Path(ledger)
    try:
        webhook = load_webhook(webhook_path)
    except AllowlistRefusal as exc:
        log_event("sender: webhook file not usable: " + str(exc))
        return {"posted": 0, "sent": 0, "given_up": 0, "superseded": 0,
                "closed_sent": 0, "rewrote": False, "skipped": 0}

    records, _bad = load_records(ledger)
    pre_count = len(records)
    counts = {"posted": 0, "sent": 0, "given_up": 0, "superseded": 0,
              "closed_sent": 0, "rewrote": False, "skipped": 0}
    changed = False
    chosen: List[dict] = []
    seen_keys = set()
    for record in records:
        if record.get("status") != STATUS_PENDING:
            continue
        key = record.get("key")
        if not isinstance(key, str) or not key:
            continue
        if key in seen_keys:
            if _mark(record, STATUS_SUPERSEDED):
                counts["superseded"] += 1
                changed = True
                log_event("sender: superseded duplicate pending for key " + key)
            continue
        seen_keys.add(key)
        chosen.append(record)

    for record in chosen:
        key = record.get("key", "?")
        try:
            payload = payload_from_record(record)
        except AllowlistRefusal as exc:
            attempts = record.get("attempts")
            attempts = attempts + 1 if type(attempts) is int else 1
            record["attempts"] = attempts
            changed = True
            counts["skipped"] += 1
            if attempts >= MAX_ATTEMPTS and _mark(record, STATUS_GIVEN_UP):
                counts["given_up"] += 1
            log_event("sender: refused record for key " + key + ": " + str(exc)
                      + " (attempt " + str(attempts) + ")")
            continue
        counts["posted"] += 1
        try:
            post_payload(webhook, payload)
        except HTTPError as exc:
            failed, detail = True, "http " + str(exc.code)
        except (URLError, OSError, ValueError) as exc:
            failed, detail = True, type(exc).__name__
        else:
            failed, detail = False, ""
        changed = True
        if not failed:
            _mark(record, STATUS_SENT)
            counts["sent"] += 1
            log_event("sender: sent key " + key)
            continue
        attempts = record.get("attempts")
        attempts = attempts + 1 if type(attempts) is int else 1
        record["attempts"] = attempts
        if attempts >= MAX_ATTEMPTS and _mark(record, STATUS_GIVEN_UP):
            counts["given_up"] += 1
            log_event("sender: given_up key " + key + " after " + detail
                      + " (" + str(attempts) + " attempts)")
        else:
            log_event("sender: retry later key " + key + " after " + detail
                      + " (attempt " + str(attempts) + ")")

    if notify_closed:
        for record in records:
            if record.get("status") != STATUS_CLOSED:
                continue
            if record.get("closed_sent_iso_utc"):
                continue
            gave_up_at = record.get("closed_gave_up_iso_utc")
            if isinstance(gave_up_at, str) and gave_up_at:
                continue
            key = record.get("key", "?")
            try:
                closed_payload = closed_payload_from_record(record)
            except AllowlistRefusal as exc:
                log_event("sender: refused closed record for key " + key + ": "
                          + str(exc))
                continue
            try:
                post_payload(webhook, closed_payload)
            except HTTPError as exc:
                detail = "http " + str(exc.code)
            except (URLError, OSError, ValueError) as exc:
                detail = type(exc).__name__
            else:
                record["closed_sent_iso_utc"] = now.replace(microsecond=0).isoformat()
                changed = True
                counts["closed_sent"] += 1
                log_event("sender: sent closed follow-up for key " + key)
                continue
            attempts = record.get("attempts")
            attempts = attempts + 1 if type(attempts) is int else 1
            record["attempts"] = attempts
            changed = True
            if attempts >= MAX_ATTEMPTS:
                record["closed_gave_up_iso_utc"] = now.replace(microsecond=0).isoformat()
                log_event("sender: stopped closed follow-up for key " + key
                          + " after " + detail)
            else:
                log_event("sender: closed follow-up retry later for key " + key
                          + " after " + detail)

    pruned = prune(records, now)
    if len(pruned) != pre_count or changed:
        rewrite_records(ledger, pruned)
        counts["rewrote"] = True
    return counts


def closed_payload_from_record(record: dict) -> dict:
    """The one-line closed follow-up: identity only plus the fixed sentence, never a
    link, never any alert display text."""
    payload = payload_from_record({**record, "link": None})  # drop link by the book
    for display in ("label", "reason", "command", "question", "note",
                    "binding_refusal", "expires_london"):
        payload.pop(display, None)
    payload["message"] = CLOSED_MESSAGE
    return payload


def self_test(webhook_path) -> int:
    """Marcin's wiring check: POST the fixed self-test message and nothing else. The
    ledger is never touched because no ledger argument exists on this path."""
    try:
        webhook = load_webhook(webhook_path)
    except AllowlistRefusal as exc:
        log_event("self-test refused: " + str(exc))
        return 1
    payload = {"v": RECORD_VERSION, "message": SELF_TEST_MESSAGE}
    try:
        post_payload(webhook, payload)
    except HTTPError as exc:
        log_event("self-test failed: http " + str(exc.code))
        return 1
    except (URLError, OSError, ValueError) as exc:
        log_event("self-test failed: " + type(exc).__name__)
        return 1
    log_event("self-test sent: the routine should have received '"
              + SELF_TEST_MESSAGE + "'")
    return 0


def _notify_closed_from_env(argv_value: bool) -> bool:
    if argv_value:
        return True
    for name in NOTIFY_CLOSED_ENV_NAMES:
        if os.environ.get(name, "") == "1":
            return True
    return False


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="G2C run-approvals chat alert sender (Marcin-only install)")
    parser.add_argument("--ledger", help="path to the alert ledger JSONL (the gateway's "
                                         "HERMES_WEBAPP_RUN_ALERTS_OUTBOX)")
    parser.add_argument("--webhook", default=DEFAULT_WEBHOOK_PATH,
                        help="path to the 0600 webhook JSON file")
    parser.add_argument("--notify-closed", action="store_true",
                        help="also POST the one-line, link-free closed follow-up "
                             "(default off)")
    parser.add_argument("--self-test", action="store_true",
                        help="POST the fixed self-test message and exit; no ledger")
    args = parser.parse_args(argv)
    if not _has_stream_handler(_log):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    if args.self_test:
        return self_test(args.webhook)
    ledger = args.ledger or os.environ.get("G2C_RUNALERTS_LEDGER") \
        or os.environ.get("HERMES_WEBAPP_RUN_ALERTS_OUTBOX")
    if not ledger:
        log_event("sender: no ledger given (use --ledger or the env)")
        return 0
    notify_closed = _notify_closed_from_env(args.notify_closed)
    run_once(ledger, args.webhook, notify_closed=notify_closed)
    return 0


def _has_stream_handler(logger) -> bool:
    return any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
