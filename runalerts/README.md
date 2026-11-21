# G2C run-approvals chat alert sender (Marcin-only install)

The Hermes multiplex gateway runs a config-gated, keyless emitter that watches the
live run-approval and clarify cards (pre-mint, no credentials) and appends one
redacted alert record per waiter (dedupe key `profile|run_id|prompt_id|generation`)
to a local JSONL ledger. This sender is the only component with network access:
about every 15 seconds it takes the first pending record per key and POSTs it to a Grok
Bot routine webhook. macOS launchd fires one tick per 15-second timer. Windows checks
about every 15 seconds too, but Task Scheduler will not accept a sub-minute repetition
interval, so the Windows installer registers a 1-minute task whose invocation performs
four ticks 15 seconds apart (`--ticks 4 --tick-interval 15`).

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
  others can read it (0600 mode on POSIX, owner-only ACL on Windows: any
  group-read bit refuses too, and an inconclusive check refuses fail closed
  instead of continuing with one warning).
- Closed records (a waiter settled or vanished) stay ledger-only by default.
  Enable the one-line, link-free chat follow-up with the `--notify-closed` flag
  or `G2C_RUNALERTS_NOTIFY_CLOSED=1` (sender side only; the gateway has no
  switch for it).

## Files

| File | Purpose |
| --- | --- |
| `runalerts/sender.py` | the stdlib sender: gate, per-key POST, self-test, multi-tick loop (`--self-test`, `--ledger`, `--webhook`, `--notify-closed`, `--ticks`, `--tick-interval`) |
| `runalerts/com.example.g2c.runalerts-sender.plist` | macOS launchd timer template (paths only, no secrets) |
| `runalerts/install-task-windows.ps1` | Windows Task Scheduler installer template (edit two placeholder paths, review, dry run with `-DryRun`, run) |
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
        outbox: /absolute/path/to/alerts.jsonl
        profiles: [coder, code-reviewer, imilk, researcher]
        interval: 30
        public_url: https://<host>.<tailnet>.ts.net

- `outbox` = absolute ledger path (required; a home-relative choice such as
  `~/.bfc-pipeline/run/g2c` resolved under the home directory is the typical
  shape; the emitter tightens it to 0700).
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

The sender's default webhook path is home-relative: on macOS and Linux it reads
`~/.bfc-pipeline/secrets/g2c-grokbot-webhook.json` (resolved under the running
user's home directory via `Path.home()`), on Windows
`%APPDATA%\G2C\g2c-grokbot-webhook.json`. Pass `--webhook` to use another path.

    mkdir -p ~/.bfc-pipeline/secrets
    chmod 700 ~/.bfc-pipeline/secrets
    # create the webhook file with ONLY two / three keys, then:
    chmod 600 ~/.bfc-pipeline/secrets/g2c-grokbot-webhook.json

`~/.bfc-pipeline/secrets/g2c-grokbot-webhook.json` (0600, never in the
repo, never in logs):

    {"url": "<https webhook url>", "sender_key": "<the routine's key>"}

Optional third key: `"key_header": "<header name>"` (default `X-Sender-Key`).
A pasted full line is accepted and normalized: `sender_key` may be
`Bearer <key>` or a whole `Authorization: Bearer <key>` line, and either field
may carry the line that names the header. A key with interior whitespace or a
malformed header name refuses with an explanatory line (the key value is
never logged). The acceptance note for a normalized shape logs once per
process, not on every tick of a multi-tick schedule.

Windows: place it in a directory only the current user can enter and give the
file an owner-only ACL:

    powershell -Command "icacls 'C:\Users\me\AppData\Roaming\G2C\g2c-grokbot-webhook.json' /inheritance:r /grant:r 'me:F'"

The sender refuses any webhook file or ledger that group members or others
can read (0600 on POSIX: the macOS staff group alone spans every local
account; on Windows it checks the ACL the PowerShell-independent way it
can). An inconclusive check refuses too: fail closed when the platform
cannot tell (the file lives under a private profile directory either way).
A ledger path that does not exist yet is a quiet idle tick instead (there
is nothing to classify on a missing path), and the same check applies to
the real file from the first tick after the emitter creates it.

## Step 4: schedule the sender (pick the platform's section)

The command is the same everywhere (stdlib Python 3.9+; the ledger path MUST
equal the config `outbox` from step 1; run as the SAME user who runs the
gateway and owns the ledger dir):

    python3 runalerts/sender.py --ledger /path/to/alerts.jsonl

### macOS: launchd timer

Fill every `__PLACEHOLDER__` in `runalerts/com.example.g2c.runalerts-sender.plist`
(python3 path, sender script path, ledger path - MUST equal the config
`outbox` from step 1 - and log path), then:

    cp runalerts/com.example.g2c.runalerts-sender.plist ~/Library/LaunchAgents/com.example.g2c.runalerts-sender.plist
    launchctl load ~/Library/LaunchAgents/com.example.g2c.runalerts-sender.plist

Existing installs: an agent already registered under the old
`com.md.g2c.runalerts-sender.plist` name and label keeps running unchanged; the
rename is template-only and needs no action on those machines.

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

Dry run first: add `-DryRun` to that command to report every action the
installer would take, each line prefixed `DRY RUN:`, without registering,
removing or starting anything and without writing any file. The dry run exits
non-zero only when the placeholders are still unfilled; placeholder state,
path existence, the pythonw.exe choice or the python.exe fallback warning,
the `G2C_RUNALERTS_LEDGER` check, the log directory it would create,
the task name, the exact action (executable plus argument string), triggers
(the logon trigger names its user), settings and the would-be register and
start are all reported. With
`-Uninstall -DryRun` it checks, read only via `Get-ScheduledTask`, which of
the two task names are present and would be removed; on hosts without the
ScheduledTasks cmdlets (for example pwsh on Linux or macOS) it says so and
lists both names as would-be checked. Filling the two placeholder paths and
pinning the `sender.py` copy next to it are manual steps the dry run
validates; the script itself copies no files and replaces no placeholders.

How the task stays silent: the installer registers pythonw.exe as the
executable whenever pythonw.exe sits next to the filled python.exe placeholder
(a filled pythonw.exe path is used as-is; any other interpreter passes through
unchanged), because pythonw.exe opens no console window on a scheduled run.
When pythonw.exe is missing it falls back to the filled python.exe and prints
a warning that a console window may appear each minute. The task is registered
Hidden as well, which hides it in the Task Scheduler UI tree; pythonw.exe is
what suppresses the console window, so both are required. Re-running the
installer keeps the silent shape: it registers no visible-window task.

The trigger pair is a one-minute repetition plus a per-user logon trigger
scoped to the current user: `-AtLogOn -User <DOMAIN\user>`, computed once
from `USERDOMAIN` and `USERNAME` (when `USERDOMAIN` is empty the bare user
name is used, and when `USERNAME` is unavailable, whatever `USERDOMAIN`
holds, the dry run says so while the real run refuses). A bare `-AtLogOn` trigger means any user logon, and
Task Scheduler refuses to register that from a regular session with
"Access is denied"; the per-user form is what makes the install work
without elevation.

Failures fail loudly: `Register-ScheduledTask` and `Start-ScheduledTask`
run with `-ErrorAction Stop` in try/catch. A register failure exits 3, a
start failure exits 4, both print the failing step and the exception
message and never print a success line ("Registered and started ..."). An
"Access is denied" from `Register-ScheduledTask` usually means an any-user
logon trigger or another elevated-only setting. When registration
succeeded but the start failed, the message says the task IS registered
and names the manual `Start-ScheduledTask` command and the remove commands
(`-Uninstall`, or `schtasks /Delete /TN G2C.RunalertsSender /F`). Re-run
the installer after fixing the cause: a failed register leaves nothing
registered (a name-already-exists failure wants the `-Uninstall` first),
and a failed start needs the task started or removed before a clean
re-run.

Pin provenance (documentation only, recommended): a pinned sender copy drifts
from the repo checkout, so when you pin sender.py on the Windows box, write a
provenance file next to the pinned sender.py, e.g. `PIN.txt`, containing the
git commit SHA and the sha256 of that sender.py (and optionally the installer
ps1):

    git rev-parse HEAD
    certutil -hashfile C:\path\to\sender.py SHA256
    certutil -hashfile C:\path\to\install-task-windows.ps1 SHA256

Write those values plus the date into `PIN.txt`. Nothing in this repo reads or
writes `PIN.txt`, and no tooling here reaches the machine; the file only makes
a stale pin identifiable later.

Remove: `powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -Uninstall`

If `Unregister-ScheduledTask` is refused with Access Denied (seen in a filtered
admin / Medium IL session), delete the task from the same session:

    schtasks /Delete /TN G2C.RunalertsSender /F

Existing installs registered under the old task name `ComMdG2c.RunalertsSender`
keep working unchanged and need no change. To migrate such an install to the
new name, run the `-Uninstall` command first: it removes both names, including
a freshly registered new one. Then re-run the installer, then check that
exactly one task `G2C.RunalertsSender` exists; re-running the installer
without that uninstall would register the new name while the legacy task
keeps running alongside. On such an install the manual fallback names the
legacy task `/TN ComMdG2c.RunalertsSender`. The `-Uninstall` removes the task
only; the ledger, the webhook file and the logs stay.

How the cadence works on Windows: the task trigger repeats every 1 minute
(Task Scheduler rejects any sub-minute repetition interval, such as a
15-second PT15S), and the registered command adds `--ticks 4 --tick-interval
15`, so one invocation performs four sender ticks 15 seconds apart inside
that minute - the same effective latency as the 15-second launchd timer. The
invocation's execution time limit is 55 seconds and MultipleInstances is
IgnoreNew, so a stuck run is stopped before the next minute and can never
overlap the next firing. Each tick is the same idempotent, ledger-gated tick
as on the other platforms.

## Step 5: validate with the self-test

    python3 runalerts/sender.py --self-test

`--self-test` POSTs exactly one fixed message (`G2C sender self-test, no waiter`)
using only the webhook file: if the routine received it, url, key and wiring are
good. Exit code 0 on success, 1 on failure. The ledger is never touched by the
self-test (no `--ledger` argument exists on that path).

## Wait timeout (the alert-to-tap window)

When an alert reaches the phone, the pending question or approval waiter it
describes has a bounded answer window, set per profile by two config keys,
read at every waiter creation through the signature-cached config loader:

| Key                    | What it bounds            | Default | Resolved by |
|------------------------|---------------------------|---------|-------------|
| `agent.clarify_timeout`| clarify question window   | 90s     | `tools/clarify_gateway.py` (`resolve_clarify_timeout`: legacy `clarify.timeout` wins if set, else `agent.clarify_timeout`, else 90; <= 0 unlimited) |
| `approvals.timeout`    | approval card window      | 300s    | `tools/approval_context.py` (`_get_approval_timeout`, clamped to `agent.deadline.MAX_SAFE_TIMEOUT_S`) |

`runalerts/set-wait-timeout.sh` (macOS/Linux) and
`runalerts/set-wait-timeout.ps1` (Windows) point BOTH keys of the G2C
profiles at one value (default 600s), with a fail-closed prior-value record
and a rollback. Neither script edits a config file itself: every write goes
through `hermes config set|unset` (named profiles with `-p <p>`), verified
per profile with `config get`.

    bash runalerts/set-wait-timeout.sh --wait-timeout 900 --apply
    bash runalerts/set-wait-timeout.sh --rollback ~/.bfc-pipeline/run/g2c/wait-timeout-state-<stamp>.jsonl

Defaults: profiles `coder,code-reviewer,imilk,researcher`; valid range
60..86400 seconds (below 60 s the phone taps cannot land in time; above 24 h
make a deliberate edit instead). A profile that still sets the legacy
`clarify.timeout` key makes the run refuse (`--also-legacy` to point that key
at the same value too; the unset ones are left to `agent.clarify_timeout`).
Profile names are validated before anything runs (`[a-z0-9-]+`, no leading
dash, no duplicates, no empty items; exit 2), and `--apply` refuses the whole
batch when any named profile dir is missing under the profile root (a typo
would otherwise make `hermes config set` create a junk profile); the dry run
prints those profiles as `profile dir missing` in the prior list and the
command list instead of inventing commands for them. Rollback replays the
recorded priors from the state file `--apply` wrote (0600 JSONL, one record
per touched key; `--state` chooses where it lives).
Run it without `--apply` for a dry run: it prints the prior values (read from
the profiles' raw `config.yaml`, so seeded defaults are not mistaken for
user-set ones) and the exact commands. Exit codes: 0 ok, 1 refusal/failure,
2 invalid input.

Restart answer: NO gateway restart is needed after `config set`/`--rollback`.
Both resolvers load the profile config per waiter creation; the loader's cache
is keyed by the file signature (stat + content digest) in
`hermes_cli/config_backend.py`, so the next waiter picks up the new value
while waiters created before the change keep the window they were born with.
That also means a rollback only affects waiters created after it: if phone
taps failed during the temporary window, those agents still need a manual
resolve after the rollback.

Windows: `runalerts/set-wait-timeout.ps1` is the Windows port (PowerShell 5.1
compatible, no pwsh 7-only syntax; same safety posture and exit codes as the
.sh, with one documented divergence: how a negative prior value is treated,
see Known limits below). Windows Hermes today runs the `default` profile,
whose config lives in the Hermes home root
(`%LOCALAPPDATA%\hermes\config.yaml`; `HERMES_HOME` overrides the home),
and it is managed WITHOUT `-p`:

    powershell -ExecutionPolicy Bypass -File runalerts\set-wait-timeout.ps1
    powershell -ExecutionPolicy Bypass -File runalerts\set-wait-timeout.ps1 -WaitTimeout 900 -Apply
    powershell -ExecutionPolicy Bypass -File runalerts\set-wait-timeout.ps1 -Rollback "%LOCALAPPDATA%\G2C\wait-timeout-state-<stamp>.jsonl"

DRY RUN is the default (prints the priors read from the raw `config.yaml` and
the exact commands; nothing is written). `-Apply` writes the JSONL state file
first (same schema and kind as the .sh, private to the user by default in
`%LOCALAPPDATA%\G2C`, `G2C_STATE_DIR` overrides the default dir for test
sandboxes, `-State` an explicit file) and only then runs `hermes config set`,
verified per profile with `config get`; `-Rollback` replays the recorded
priors (`config unset` for keys that were unset before, `config set` for set
ones; "already unset" is tolerated). The batch refuses before anything is
touched when a named profile's `profiles\<p>\config.yaml` or the default
profile's home-root `config.yaml` is missing (a typo would make
`hermes config set` create a junk profile), on a legacy `clarify.timeout`
key (unless `-AlsoLegacy`), and on any undecidable raw prior. The hermes
binary is `$env:HERMES_BIN` when set, else `hermes.exe` resolved with
`Get-Command -CommandType Application`: a bare `hermes` can resolve to a
non-exe launcher script on Windows, so point `HERMES_BIN` at an .exe file
when in doubt. Tests for the port are `tests/test_set_wait_timeout_ps1.py`
(skipped when no PowerShell interpreter is found; CI runs them on ubuntu,
macos and windows, where the windows legs execute the suite once per
discovered interpreter: pwsh 7 AND powershell.exe 5.1 when both are
installed).

Fallback without the script (then verify each write with `get`). Windows
Hermes today runs only the `default` profile and it is managed WITHOUT
`-p` (the flagless call lands on the default profile; `-p default` instead
creates/points the CLI at a junk `profiles\default\` dir, exactly the junk
profile the script's own missing-config guard refuses to create for you):

    hermes.exe config set agent.clarify_timeout 600
    hermes.exe config set approvals.timeout 600
    hermes.exe config get agent.clarify_timeout
    hermes.exe config get approvals.timeout

A named profile takes `-p <p>`:

    hermes.exe -p <p> config set agent.clarify_timeout 600
    hermes.exe -p <p> config set approvals.timeout 600
    hermes.exe -p <p> config get agent.clarify_timeout
    hermes.exe -p <p> config get approvals.timeout

(or run `runalerts/set-wait-timeout.sh` under Git Bash/WSL on macOS and
Linux, where its awk/sed usage is POSIX; do NOT run the .sh against the
Windows Hermes: it assumes the POSIX profile root and Git Bash's HOME is
wrong there - use the .ps1).

Known limits: the prior-value reader is line-based block YAML. Alias and
merge keys (`<<`, `*`, `&`) are not consulted, so a profile that injects the
two keys through one of them must set them explicitly. Flow-style section
values are understood only as the empty single-line map (`agent: {}` counts
as absent there); a single-line flow value naming a key of interest refuses,
as does any multi-line flow value. The rollback parser accepts only the exact
header record and the exact profile/key/prior record lines (a line that
merely contains the header's kind text is refused, never skipped) and
restores only what `--apply` recorded; it is not a YAML editor and never
touches the profile files themselves. Negative priors:
`agent.clarify_timeout` may legitimately hold a negative value (the
documented `<= 0 unlimited` semantic). The ps1 displays such a prior in the
dry run, records it in the state file and restores it on rollback; the .sh
currently drops it to null (its `printf` eats the leading `-` of the format
string), so a .sh rollback would `config unset` the key instead of
restoring the negative value. This divergence is intended and test-pinned;
do not harmonize the ps1 down to the .sh behaviour (fixing the .sh is a
separate follow-up).

## Rollback

- Stop the sender on macOS: `launchctl unload
  ~/Library/LaunchAgents/com.example.g2c.runalerts-sender.plist` (fresh
  installs; an existing install keeps its old
  `com.md.g2c.runalerts-sender.plist` name) and delete the
  plist from `~/Library/LaunchAgents`. On Windows:
  `powershell -ExecutionPolicy Bypass -File .\runalerts\install-task-windows.ps1 -Uninstall`,
  and when that is refused with Access Denied,
  `schtasks /Delete /TN G2C.RunalertsSender /F` removes the task only (ledger,
  webhook file and logs stay). An existing install registered under the old
  task name `ComMdG2c.RunalertsSender` needs the same removal: the same
  `-Uninstall` covers both names, else
  `schtasks /Delete /TN ComMdG2c.RunalertsSender /F`.
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
