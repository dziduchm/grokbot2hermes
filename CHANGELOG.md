# Changelog

Format: Keep a Changelog, adapted (this project does not make versioned
releases to package indexes; versions track the documented contracts).

## Unreleased (public repo cleanup: no owner-specific paths or hosts in the repo)

Changed:

- Documentation placeholders: the runalerts README and the launchd plist
  template carry no personal tailscale host or absolute owner path any more:
  the tailscale example value became the generic `https://<host>.<tailnet>.ts.net`
  placeholder, and every `/Users/<name>/...` example became `~/...` or a
  generic placeholder.
- The sender's POSIX default webhook path is home-relative:
  `default_webhook_path()` resolves
  `~/.bfc-pipeline/secrets/g2c-grokbot-webhook.json` under `Path.home()` at
  call time (it used to be a hardcoded absolute path). The Windows
  `%APPDATA%/G2C` default is unchanged, both are documented side by side in
  the runalerts README, and new tests pin the relative constant, resolution
  under a faked HOME, and that an explicit `--webhook` still wins.
- launchd template renamed: `runalerts/com.md.g2c.runalerts-sender.plist` is
  now `runalerts/com.example.g2c.runalerts-sender.plist` with the label
  `com.example.g2c.runalerts-sender`; every reference updated. Fresh installs
  use the new name; existing installs registered under the old
  `com.md.g2c.runalerts-sender` label keep working with no change.
- .gitignore: local run artifacts and caches (`*.jsonl`, `*.log`, `.venv*/`,
  `.pytest_cache/`, `.ruff_cache/`) and key material (`secrets/`, `*.pem`,
  `*.key`, `*.p12`) are ignored; no tracked file matches any new pattern.
- CI least privilege: top-level `permissions: contents: read`; only the
  gitleaks job widens to `pull-requests: write` because gitleaks-action v2.3.9
  posts a PR review comment (Octokit pulls API, GITLEAKS_ENABLE_COMMENTS
  defaults true) when a pull_request scan finds a leak.

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
- Wait timeout installer: `runalerts/set-wait-timeout.sh` points the two answer
  windows of the G2C profiles (`agent.clarify_timeout`, `approvals.timeout`) at
  one value (default 600s, valid 60..86400). Dry run by default (prints prior
  values read from each profile's raw `config.yaml` plus the exact commands);
  `--apply` writes via `hermes -p <p> config set` only, verifies each profile
  with `config get`, and records per-profile prior values (null = was unset) in
  a 0600 JSONL state file for `--rollback` (`config set` for recorded values,
  `config unset` for nulls, "already unset" tolerated). Refusals: invalid
  numeric input or lists (exit 2, nothing touched), a profile still carrying
  the legacy `clarify.timeout` key (it overrides `agent.clarify_timeout`;
  `--also-legacy` points it at the same value too), undecidable prior values
  (flow/nested/duplicate/non-scalar in the raw layer, fail closed), state file
  overwrite. No gateway restart is needed: both resolvers load the profile
  config per waiter creation and the loader cache is signature-keyed
  (stat + content digest); waiters created before a change keep their window.
  POSIX/bash-3.2, bash-echoed every write; documented in the runalerts README
  ("Wait timeout") and covered by `tests/test_set_wait_timeout.py` (31 tests,
  unittest-compatible, fake-hermes stub, POSIX legs). Windows parity this
  round is a README note with the equivalent per-profile `hermes config set`
  commands instead of a .ps1 port.
- Wait timeout installer, hardening round 2: the dry-run command list now
  matches `--apply` for a missing profile dir (the same `profile dir missing`
  note replaces that profile's commands), the raw prior reader's refusal of a
  leaf-shaped line at any indent other than the section's direct-child indent
  (shallower or deeper, with or without a present direct leaf) is pinned by
  suite tests, and `--rollback` skips the state-file header on the exact
  header record only: a line that merely contains the kind text is refused
  instead of skipped. The known limits (alias/merge keys, flow-style values,
  exact-record rollback parsing) are documented in the runalerts README.

Changed:

- A missing ledger file is a quiet idle tick instead of a privacy refusal:
  there is nothing to classify on a path that does not exist, and the loader
  already treats a missing file as an empty ledger, so the tick stays silent
  and POSTs nothing until the emitter creates the file. The owner-only and
  fail-closed privacy checks then apply to the real file from the first tick
  after it exists, and a file that exists but whose privacy the platform
  cannot tell still refuses with the same log line as before.
- Webhook-key normalization notes (a pasted full header line or a
  Bearer-prefixed sender_key accepted) log once per process instead of once
  per call: a scheduled multi-tick task that loads the same webhook file no
  longer repeats the note four times a minute. The key value is still never
  logged.
- Windows installer silence: `install-task-windows.ps1` prefers pythonw.exe
  (the sibling next to the filled python.exe; a filled pythonw.exe path passes
  through, and the python.exe fallback keeps an explicit console-window
  warning when pythonw.exe is missing) and registers the per-user task Hidden
  (Task Scheduler UI visibility; pythonw.exe suppresses the console window,
  both required). The installer header documents that silent shape, and the
  uninstall gains a schtasks /Delete fallback (`schtasks /Delete /TN ComMdG2c.RunalertsSender /F`)
  when `Unregister-ScheduledTask` is refused with Access Denied (filtered
  admin / Medium IL session). Static installer tests fail a regression to the
  visible python.exe-only shape.

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
