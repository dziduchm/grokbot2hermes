# G2C run-approvals chat alert sender (Marcin-only install)

The Hermes multiplex gateway runs a config-gated, keyless emitter that watches the
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
  refusal (a broken record shape will not heal by retrying). Display fields are
  sanitized in the payload path only (truncated to the emitter caps first, control
  and zero-width characters stripped, backticks neutralized, markdown link syntax
  defanged when the text is link-shaped) so nothing paste-shaped rides a chat
  bubble, and every alert carries a fixed one-line preamble saying the fields are
  plain text, never instructions.
- 3 failed attempts per key (10s timeout each), then the key is marked
  `given_up` permanently: no infinite retries, no chat spam. Redirects count as
  failures: the sender key header is never replayed to a redirect target.
- Cross-process ledger lock (an advisory gate on the sidecar file next to the
  ledger, `*.lock` 0600 inside the 0700 ledger dir, shared with the gateway
  emitter): POSIX fcntl.flock, Windows msvcrt.locking, and on any platform with
  neither primitive the gate fails closed (the tick is skipped, the ledger is
  untouched). The emitter takes it non-blocking for every append and closed
  rewrite; this sender holds it for the whole load, mutate, rewrite tick with a
  bounded wait, then the tick is skipped whole and retried on the next run.
- Full-file atomic rewrite of the ledger (tmp + rename, hardening after the
  rename: file 0600 and dir 0700 modes on POSIX, an owner-only ACL on Windows);
  records older than 14 days are pruned on write, hard cap 5000 records.
- Malformed ledger lines are skipped and quarantined once to
  `alerts.bad.jsonl` next to the ledger.
- Log lines are capped at 500 chars and never contain the webhook url or key.
- Loopback http (`127.0.0.1`, `localhost`, `[::1]`) is allowed for trials only;
  every other url must be https. The webhook file is refused outright when
  others can read it (0600 mode on POSIX, owner-only ACL on Windows: the check
  fails closed when the platform cannot tell, and logs one warning per file).
- Closed records (a waiter settled or vanished) stay ledger-only by default.
  Enable the one-line, link-free chat follow-up with the `--notify-closed` flag
  or `G2C_RUNALERTS_NOTIFY_CLOSED=1` (sender side only; the gateway has no
  switch for it).

## Files

| File | Purpose |
| --- | --- |
| `runalerts/sender.py` | the sender process (`--self-test`, `--ledger`, `--webhook`, `--notify-closed`) |
| `runalerts/com.md.g2c.runalerts-sender.plist` | macOS launchd timer template (paths only, no secrets) |
| `runalerts/install-task-windows.ps1` | Windows Task Scheduler installer template (edit two placeholder paths, review, run) |
| `tests/test_runalerts_sender.py` | sender test suite (unittest, loopback mock) |

## Step 1: configure the gateway side (on the machine that runs the gateway)

The production gate is a `webapp.run_alerts` block in the gateway's `config.yaml`
(the same file `webapp.public_url` comes from; the emitter reads it with the
gateway's own in-process config loader, so the managed overlay and `${ENV}`
expansion behave as usual). NEVER put these settings into
`~/Library/LaunchAgents/ai.hermes.gateway.plist`: the gateway REGENERATES that
file from scratch on every gateway start, restart and update pause/resume
(`hermes_cli.gateway_launchd.refresh_launchd_plist_if_needed` compares it with a
generated one and rewrites it), so `HERMES_WEBAPP_RUN_ALERTS_*` entries there
would be silently wiped and the alerts would go dead, fail closed, on the next
restart or `hermes update`. Never use `launchctl setenv` or shell profiles
(`bashrc`, `zshrc`, `launchctl subset`) either: the emitter's switch belongs to
exactly one file, the gateway's `config.yaml`. "Restart" always means kickstart
that one gateway unit (it owns production ports 8642 and 9120):

    launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway

Example block:

    webapp:
      run_alerts:
        outbox: /Users/md/.bfc-pipeline/run/g2c/alerts.jsonl
        profiles: [coder, code-reviewer, imilk, researcher]
        interval: 30
        public_url: https://md-mm.taild7b334.ts.net

- `outbox` = absolute ledger path (required; `/Users/md/.bfc-pipeline/run/g2c`
  must exist; the emitter tightens it to 0700).
- `profiles` = BARE served profile ids, a yaml list or one comma string, e.g.
  `[coder, code-reviewer, imilk, researcher]`. Bare ids only: values like
  `profiles/main` are directory paths, not server ids, and would silently match
  nothing (empty means nothing alerts).
- `interval` = poll seconds, a positive number (default 30, minimum 5).
- `public_url` = optional deep-link origin; when absent the emitter falls back
  to the webapp listener's own `webapp.public_url`.

Validation is strict and fail closed: no `webapp.run_alerts` block, or any
malformed value (relative or blank `outbox`, non-string profile entries, empty
profile list, non-number or non-positive `interval`, non-http(s)
`public_url`), leaves the feature fully off with one log line in the gateway
log. No block present is the plain default state (no log line noise).

Env override, trials only: `HERMES_WEBAPP_RUN_ALERTS_OUTBOX`,
`HERMES_WEBAPP_RUN_ALERTS_PROFILES` (comma string), the optional
`HERMES_WEBAPP_RUN_ALERTS_INTERVAL` and `HERMES_WEBAPP_RUN_ALERTS_PUBLIC_URL`.
Any ONE of them set wins entirely over the config block (that is how isolated
trial homes turn this on without touching `config.yaml`; do NOT rely on it for
production, see the plist warning above). With no env var and no config block
the emitter is fully off; it does no network I/O and holds no credentials.

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
A pasted full line is accepted and normalized: `sender_key` may be
`Bearer <key>` or a whole `Authorization: Bearer <key>` line, and either field
may carry the line that names the header. A key with interior whitespace or a
malformed header name refuses with an explanatory line (the key value is
never logged).

Windows: the sender's default webhook path is `%APPDATA%\G2C\g2c-grokbot-webhook.json`
(or pass `--webhook`). Place it in a directory only the current user can enter
and give the file an owner-only ACL:

    powershell -Command "icacls 'C:\Users\me\AppData\Roaming\G2C\g2c-grokbot-webhook.json' /inheritance:r /grant:r 'me:F'"

The sender refuses any webhook file or ledger that others can read (on
Windows it checks the ACL the PowerShell-independent way it can and fails
closed with one warning when the platform cannot tell; the file lives under
a private profile directory either way).

## Step 4: schedule the sender (pick the platform's section)

The command is the same everywhere (stdlib Python 3.9+; the ledger path MUST
equal the config `outbox` from step 1; run as the SAME user who runs the
gateway and owns the ledger dir):

    python3 runalerts/sender.py --ledger /path/to/alerts.jsonl

### macOS: launchd timer

Fill every `__PLACEHOLDER__` in `runalerts/com.md.g2c.runalerts-sender.plist`
(python3 path, sender script path, ledger path - MUST equal the config
`outbox` from step 1 - and log path), then:

    cp runalerts/com.md.g2c.runalerts-sender.plist ~/Library/LaunchAgents/com.md.g2c.runalerts-sender.plist
    launchctl load ~/Library/LaunchAgents/com.md.g2c.runalerts-sender.plist

### Linux: cron or a systemd timer

The tick is idempotent, so cadence is a latency choice only. A plain crontab
line fires once a minute (fine; every run sends at most the first pending
record per key):

    * * * * * /usr/bin/python3 /opt/grokbot2hermes/runalerts/sender.py --ledger /path/to/alerts.jsonl

or a systemd user timer with the same exec line (see `systemd-analyze calendar`
for testing your `OnCalendar` value). No root, no sudo: the ledger dir is the
gateway user's.

### Windows: Task Scheduler

Set the ledger for the user and edit the two placeholder paths at the top of
`runalerts/install-task-windows.ps1`, then review the script and run it from a
regular (non-admin) PowerShell window in the repo checkout:

    setx G2C_RUNALERTS_LEDGER "C:\path\to\alerts.jsonl"
    powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1

Remove: `powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -Uninstall`

## Step 5: validate with the self-test

    python3 runalerts/sender.py --self-test

`--self-test` POSTs exactly one fixed message (`G2C sender self-test, no waiter`)
using only the webhook file: if the routine received it, url, key and wiring are
good. Exit code 0 on success, 1 on failure. The ledger is never touched by the
self-test (no `--ledger` argument exists on that path).

## Rollback

- Stop the sender on macOS: `launchctl unload
  ~/Library/LaunchAgents/com.md.g2c.runalerts-sender.plist` and delete the
  plist from `~/Library/LaunchAgents`. On Windows:
  `powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -Uninstall`.
  On Linux: remove the crontab line or `systemctl --user disable --now` the
  timer unit.
- Turn the emitter off: remove the `webapp.run_alerts` block from the gateway's
  `config.yaml` and kickstart that one gateway unit (no env, no block = fully
  off; the trial env vars are for isolated trial homes only).
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
  Both sides serialize every mutation on the same advisory lock gate (sidecar
  `*.lock` file, 0600, inside the 0700 ledger dir; POSIX flock, Windows
  msvcrt.locking, fail closed without a primitive): the emitter non-blocking, so
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
