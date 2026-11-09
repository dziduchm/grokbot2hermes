# G2C run-approvals chat alert sender (Marcin-only install)

The Hermes multiplex gateway runs an env-gated, keyless emitter that watches the
live run-approval and clarify cards (pre-mint, no credentials) and appends one
redacted alert record per waiter (dedupe key `profile|run_id|prompt_id|generation`)
to a local JSONL ledger. This sender is the only component with network access:
every 15 seconds it takes the first pending record per key and POSTs it to a Grok
Bot routine webhook.

What the sender holds: ONLY the webhook url and sender key, read from a 0600 JSON
file. It never imports or reads any Hermes API key, cookie or signing key, and it
never touches the gateway or the DB. Stdlib only, Python 3.9+.

Safety rails (built in, none are configurable):
- POST payload is re-checked field by field against a strict allowlist; anything
  unknown, misplaced or over the 3KB cap marks the record `given_up` on the FIRST
  refusal (a broken record shape will not heal by retrying).
- 3 failed attempts per key (10s timeout each), then the key is marked
  `given_up` permanently: no infinite retries, no chat spam.
- Cross-process ledger lock (fcntl.flock on the sidecar file next to the ledger,
  `*.lock` 0600 inside the 0700 ledger dir, shared with the gateway emitter): the
  emitter takes it non-blocking for every append and closed rewrite, this sender
  holds it for the whole load, mutate, rewrite tick with a bounded wait, then the
  tick is skipped whole and retried on the next run.
- Full-file atomic rewrite of the ledger (tmp + rename, file 0600, dir 0700);
  records older than 14 days are pruned on write, hard cap 5000 records.
- Malformed ledger lines are skipped and quarantined once to
  `alerts.bad.jsonl` next to the ledger.
- Log lines are capped at 500 chars and never contain the webhook url or key.
- Loopback http (`127.0.0.1`, `localhost`, `[::1]`) is allowed for trials only;
  every other url must be https, and redirects count as failures (the sender
  key header is never replayed to a redirect target).
- Closed records (a waiter settled or vanished) stay ledger-only by default.
  Enable the one-line, link-free chat follow-up with the `--notify-closed` flag
  or `G2C_RUNALERTS_NOTIFY_CLOSED=1` (also accepts the gateway-side env name
  `HERMES_WEBAPP_RUN_ALERTS_NOTIFY_CLOSED=1`).

## Files

| File | Purpose |
| --- | --- |
| `runalerts/sender.py` | the sender process (`--self-test`, `--ledger`, `--webhook`, `--notify-closed`) |
| `runalerts/com.md.g2c.runalerts-sender.plist` | launchd timer template (paths only, no secrets) |
| `tests/test_runalerts_sender.py` | sender test suite (unittest, loopback mock) |

## Step 1: configure the gateway side (on the machine that runs the gateway)

The only env home is the gateway's launchd unit,
`~/Library/LaunchAgents/ai.hermes.gateway.plist`: put these keys in its
`EnvironmentVariables` dict (merge with existing entries). NEVER use `launchctl
setenv`, shell profiles (`bashrc`, `zshrc`, `launchctl subset` included) or any
other global mechanism: the alerts feature must only ever turn on inside that
one gateway unit, which owns both production ports 8642 and 9120. "Restart" in
this README always means kickstart that one unit, nothing else:

    launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway

- `HERMES_WEBAPP_RUN_ALERTS_OUTBOX` = absolute ledger path, e.g.
  `/Users/md/.bfc-pipeline/run/g2c/alerts.jsonl` (`/Users/md/.bfc-pipeline/run/g2c`
  must exist; the emitter tightens it to 0700).
- `HERMES_WEBAPP_RUN_ALERTS_PROFILES` = comma-separated BARE served profile ids,
  e.g. `coder,code-reviewer,imilk,researcher`. Bare ids only: values like
  `profiles/main` are directory paths, not server ids, and would silently match
  nothing (empty or unset means nothing alerts).
- Optional: `HERMES_WEBAPP_RUN_ALERTS_INTERVAL` (seconds, default 30, minimum 5),
  `HERMES_WEBAPP_RUN_ALERTS_PUBLIC_URL` (for deep links; falls back to the
  webapp listener's `public_url`), `HERMES_WEBAPP_RUN_ALERTS_NOTIFY_CLOSED=1`
  (closed chat follow-up, default off).

ALL gateway env unset means fully off. The emitter does no network I/O and holds
no credentials of any kind.

## Step 2: register the Grok Bot routine

Create the routine with an HTTP webhook trigger and note its url plus the sender
key you give it (the sender sends it under the `X-Sender-Key` header by default;
set `key_header` in the webhook file to change the header name).

## Step 3: place the webhook file

    mkdir -p /Users/md/.bfc-pipeline/secrets
    chmod 700 /Users/md/.bfc-pipeline/secrets
    # create the webhook file with ONLY two / three keys, then:
    chmod 600 /Users/md/.bfc-pipeline/secrets/g2c-grokbot-webhook.json

`/Users/md/.bfc-pipeline/secrets/g2c-grokbot-webhook.json` (0600, never in the
repo, never in logs):

    {"url": "<https webhook url>", "sender_key": "<the routine's key>"}

Optional third key: `"key_header": "<header name>"` (default `X-Sender-Key`).

## Step 4: install the launchd timer

Fill every `__PLACEHOLDER__` in `runalerts/com.md.g2c.runalerts-sender.plist`
(python3 path, sender script path, ledger path - MUST equal
`HERMES_WEBAPP_RUN_ALERTS_OUTBOX` from step 1 - and log path), then:

    cp runalerts/com.md.g2c.runalerts-sender.plist ~/Library/LaunchAgents/com.md.g2c.runalerts-sender.plist
    launchctl load ~/Library/LaunchAgents/com.md.g2c.runalerts-sender.plist

## Step 5: validate with the self-test

    python3 runalerts/sender.py --self-test

`--self-test` POSTs exactly one fixed message (`G2C sender self-test, no waiter`)
using only the webhook file: if the routine received it, url, key and wiring are
good. Exit code 0 on success, 1 on failure. The ledger is never touched by the
self-test (no `--ledger` argument exists on that path).

## Rollback

- Stop the sender: `launchctl unload ~/Library/LaunchAgents/com.md.g2c.runalerts-sender.plist`
  and delete the plist from `~/Library/LaunchAgents`.
- Turn the emitter off: remove the `HERMES_WEBAPP_RUN_ALERTS_*` env entries from
  `~/Library/LaunchAgents/ai.hermes.gateway.plist` EnvironmentVariables and
  kickstart that one gateway unit (all unset = fully off).
- The sidecar lock file next to the ledger (`<outbox>.lock`) carries no data; it
  can be deleted together with the ledger after stopping both processes (never
  while a sender or emitter tick can touch the ledger).
- Delete the webhook file (the routine stops being reachable from this sender).
- The ledger is plain JSONL and can simply be deleted. `alerts.bad.jsonl` keeps
  any malformed lines for inspection.

## Operational notes

- A tick that finds nothing pending does nothing and does not rewrite the file.
- `sent`, `given_up`, `closed` and `superseded` records are never re-POSTed, so
  restarts and repeated ticks cannot double-send a waiter.
- Attempts and status transitions live in the ledger records themselves
  (`status`, `attempts`; closed stamps `closed_sent_iso_utc` /
  `closed_gave_up_iso_utc`), so the state survives restarts and is auditable.
- The sender is a second writer to the emitter ledger: the gateway appends
  single small JSONL lines, the sender rewrites the whole file only on change.
  Both sides serialize every mutation on the same fcntl.flock gate (sidecar
  `*.lock` file, 0600, inside the 0700 ledger dir): the emitter non-blocking, so
  a contended mutation is skipped with its dedupe state untouched and retried
  next tick; the sender for its whole load, mutate, rewrite section with a
  bounded wait. This closes both review races end to end: no sender rewrite can
  swallow an emitter append, and no emitter closed rewrite can revert a sender
  status mark.
- "Restart" always means kickstarting the single gateway launchd unit
  (`ai.hermes.gateway`, owner of ports 8642 and 9120), never any global env
  mechanism (`launchctl setenv`, shell profiles).
- Tests: `python3 -m unittest discover -s tests -v` (repo CI rules: ruff 0.15.8
  on `g2h.py` and `tests/`, em/en-dash byte scan, gitleaks).
