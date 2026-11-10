# Changelog

Format: Keep a Changelog, adapted (this project does not make versioned
releases to package indexes; versions track the documented contracts).

## 0.2.0 (run-approvals chat alert sender, three-OS, hardening)

Added:

- `runalerts/`: a config-gated, stdlib-only sender for the Hermes gateway
  run-approvals chat alerts emitter. It POSTs the first pending record per
  waiter key to a Grok Bot routine webhook and closes the books with an
  atomic ledger rewrite. macOS launchd template, Linux cron/systemd recipe,
  and a Windows Task Scheduler installer template (never run automatically).
- Sender payload hardening: display fields (label, reason, command, question,
  note) are sanitized in the record-to-payload path only (truncated to the
  emitter caps first, control and zero-width characters stripped, backticks
  neutralized, markdown link syntax defanged when link-shaped), and every
  alert payload carries a fixed one-line preamble saying the fields are plain
  text, never instructions. The ledger keeps the raw agent text.
- Suppression notices: the sender posts the emitter's kind `suppression`
  summary records with label only (300-char cap); link, command, question,
  reason, note, identity fields or a blank label refuse the record
  permanently.
- Webhook credential normalization: a pasted `Authorization: Bearer <key>`
  line in either webhook-file field, or a bare `Bearer <key>` value,
  normalizes onto the header name plus bare token; a key with interior
  whitespace or a malformed header name refuses with an explanatory line
  naming the field and rule. The key value is never logged, never on argv.
- Windows cadence fix: Task Scheduler rejects sub-minute repetition
  intervals (PT15S: the minimum repetition is PT1M), so
  `install-task-windows.ps1` now registers the per-user task with a
  1-minute repetition, a 55-second execution time limit and
  MultipleInstances IgnoreNew (a stuck run is stopped before the next
  minute and never overlaps the next firing), and the registered command
  adds the new sender multi-tick flags `--ticks 4 --tick-interval 15`:
  four ticks 15 seconds apart inside that minute, the same effective
  latency as macOS launchd's 15-second timer (which stays unchanged, one
  tick per firing). Defaults stay one single tick per invocation, so a
  plain CLI run is unchanged too; invalid combinations (a tick count
  under 1, negative values, non-integer `--ticks` from argparse, a
  self-test combined with ticks, or any spread that cannot finish inside
  one minute) refuse fail closed BEFORE any POST or self-test call. A
  static unit test now fails any sub-minute `-RepetitionInterval` in the
  installer template.
- Sanitization trigger hardening: markdown link neutering catches
  `]( https://...` with uppercase and interior spaces too (case insensitive,
  whitespace tolerant), not only the lowercase `](http` substring; plain
  bracket text without a link is still left alone.
- Windows support: the ledger gate dispatches per platform (fcntl.flock,
  msvcrt.locking, fail closed without a primitive), owner-only icacls
  hardening for the webhook file, ledger, ledger dir and lock file, a
  platform-aware default webhook path (`%APPDATA%/G2C/...`), `--log-file`
  for scheduled runs hardened to 0600/owner-only on creation, and
  `--notify-closed` unchanged.
- CI: ruff now also lints `runalerts/`; windows-latest legs (Python 3.9,
  3.12) join the ubuntu and macos legs; the g2h client suite is documented
  and enforced as POSIX-only (module-level skip on Windows), with the
  cross-platform coverage in `tests/test_runalerts_sender.py`.
- Repo plumbing: pyproject metadata (no runtime deps, not pip-installable),
  GitHub issue and PR templates, CODEOWNERS, a pre-commit config, badges.

Security in this release, unchanged from design (see SECURITY.md):
human-only settlement, https-required webhooks outside loopback, redirect
refusal so the key is never replayed, fail-closed credential-file privacy
checks (group or other readable bits refuse; an inconclusive platform check
refuses too), 3-attempt give-up, suppression budget on the emitter side,
500-char capped logs without url or key.

## 0.1.0

- `g2h`: dispatch, status, wait, stop, cancel, resolve, dashboard, relay.
- Crash-safe 0600 receipts in a 0700 dir, idempotent dispatch with replay
  detection, exit codes as the control plane.
- Loopback-only read-only dashboard (static or key-required serve mode).
- Approval event relay with one optional notifier destination (webhook or
  command), no automatic retries.
- Grok Bot bus example adapter and phone cards (outbound only, no reply
  route).
