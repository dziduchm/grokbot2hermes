# Changelog

Format: Keep a Changelog, adapted (this project does not make versioned
releases to package indexes; versions track the documented contracts).

## Unreleased

## 0.1.0 - 2026-11-23

First public GitHub release of this repository (tag v0.1.0). The shipped
`g2h` client version string is `0.1.0`, and the pyproject metadata is
aligned to `0.1.0` so the tag, the client and the metadata all agree.
The project remains deliberately not pip-installable: the version names
the release, not a package. The tag covers the current tree, including
all of the work below, promoted out of Unreleased on the same day
(public repo cleanup: no owner-specific paths or hosts in the repo).

Security (all five findings from the ac7807b code-security review, one branch):

- `g2h` notifier: a webhook destination must be https, or plain http to a
  loopback host (`127.0.0.1`, `localhost`, `::1` as a trial). Any other
  plain http destination is refused with a usage error before any
  delivery, and the URL or token is never echoed. The rule now stands in
  docs/api-contracts.md and SECURITY.md: it used to be silently absent
  for this one destination while the runalerts sender and the bus
  adapter example already enforced https outside loopback.
- `runalerts/sender.py`: the loopback trial classification is
  hostname-based (`urllib.parse.urlsplit`) instead of a substring scan of
  the whole URL, so a URL whose query or path merely contains
  `//127.0.0.1` can no longer carry the sender key to a remote host over
  plain http, and URLs that carry userinfo are refused (the same shape
  the documented bus adapter contract already enforces).
- `runalerts/sender.py`: the POST opener pins `ProxyHandler({})` so
  `http_proxy` / `https_proxy` can no longer route the sender key through
  a proxy; the bus adapter example already did this and the sender was
  the one network path without the guard.
- `runalerts/set-wait-timeout.sh`: the temp state file is created by
  `mktemp` (unpredictable name, O_EXCL, no symlink follow) instead of a
  pid-only name under a shared /tmp; an mktemp failure fails closed.
  The unused `state.ok` sidecar (never written) is gone with the same
  cleanup trap.
- `.gitleaks.toml`: the empty `[allowlist]` block is dropped (it fails to
  load on current gitleaks, which would break the secrets job on
  upgrade); the default rules stay active with zero suppressions and any
  future allowlist entry needs an owner review first.

Added:

- `runalerts/set-wait-timeout.ps1`: the Windows port of the wait timeout
  installer (PowerShell 5.1 compatible, no pwsh 7-only syntax). Same
  safety posture and exit codes as the .sh: dry run by default, fail-closed
  raw-layer prior reader (the same line-based block YAML semantics), state
  JSONL first (same schema and kind, private to the user), writes only via
  `hermes config set|unset|get` with per-profile verification, and the same
  legacy `clarify.timeout` guard. One documented divergence from the .sh: a
  negative prior value (the documented "<= 0 unlimited" semantic of
  `agent.clarify_timeout`) is displayed, recorded in the state file and
  restored on rollback by the ps1, where the .sh drops it to null (its
  printf eats the leading '-'); documented in runalerts/README.md
  "Wait timeout" and pinned by a dedicated test. Windows differences
  documented in
  runalerts/README.md: the Hermes home resolves from `HERMES_HOME` or
  `%LOCALAPPDATA%\hermes`; the `default` profile is called without `-p` and
  reads its config from the home root; the hermes binary is `$env:HERMES_BIN`
  or `hermes.exe` resolved with `Get-Command -CommandType Application` (a
  bare `hermes` can resolve to a non-exe launcher script); the default state
  dir is `%LOCALAPPDATA%\G2C` (`G2C_STATE_DIR` overrides for sandboxes). The
  ps1 refuses a batch when a profile's own config.yaml is missing (the .sh
  guards the profile dir only). Tests `tests/test_set_wait_timeout_ps1.py`
  drive the real ps1 with a fake hermes stub, skip when no PowerShell is
  found, and run in CI on ubuntu, macos and windows (on win32 the suite
  builds one test leg per discovered PowerShell interpreter, so the windows
  CI legs exercise both pwsh 7 and powershell.exe 5.1 when both are
  installed).
- CI security scanners: three pinned workflows next to ci.yml.
  `codeql.yml` (CodeQL over the Python code and the Actions workflows,
  security-extended suites, results in the Security tab), `sast.yml`
  (bandit over production code only: g2h.py, g2h_ui.py, runalerts/,
  .github/scripts; tests excluded because their findings are the
  review's documented test-only classes; the gate fails on medium severity
  and higher, semgrep with the community rules fails on any finding, and
  pip-audit fails on any known advisory) and `scorecard.yml` (OpenSSF
  Scorecard, published on pushes to main and a weekly schedule; PR runs
  analyze the head without publishing). The sast and lint jobs install
  their tools with `pip install --require-hashes` from hash-locked
  closure files generated with `pip-compile --generate-hashes`:
  requirements-repo.in (the source of truth: bandit 1.8.6, pip-audit
  2.9.0, semgrep 1.186.0, moved up from the review's 1.136.0 for a
  current scanner and for the dropped protobuf advisory in the audited
  closure: the accepted advisory count moved from four to eight, all
  inside semgrep's own dependency pins) and requirements-ruff.in (ruff
  0.15.8, in sync with the pins above and .pre-commit-config.yaml) for
  lint, which no longer installs the scanner tools. The pip-audit
  step carries eight documented --ignore-vuln entries for advisories
  inside semgrep's own dependency pins (mcp, opentelemetry-api,
  wcmatch; the step comment in sast.yml states the release-coupled
  conditions under which they must be revisited). The ten
  semgrep ifs-tampering findings in runalerts/set-wait-timeout.sh carry
  inline nosemgrep annotations with the review triage at each site
  (comma-only IFS scoped to the loop's own split, restored at the top of
  every iteration, input already validated to [a-z0-9-]+ with no leading
  dash). README gains the three workflow badges, and SECURITY.md and
  CONTRIBUTING.md document the scanners and how to run them locally.

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
- Windows Task Scheduler template: the installer registers the per-user task
  under the neutral name `G2C.RunalertsSender` (it was
  `ComMdG2c.RunalertsSender`). Existing Windows installs stay registered
  under the old name and need no change: `-Uninstall` removes both names
  (the same Unregister-ScheduledTask-then-`schtasks /Delete /F` fallback
  applied per name), re-running the installer registers the new name, and
  the runalerts README documents the manual fallback for both. To migrate
  an install from the old name, run `-Uninstall` first (it removes both
  names, including a freshly registered new one), then re-run the
  installer, then check that exactly one task `G2C.RunalertsSender`
  exists; re-running the installer without the uninstall would register
  the new name while the legacy task keeps running alongside.
- The Windows installer registers a per-user logon trigger and fails
  loudly: `New-ScheduledTaskTrigger -AtLogOn` now carries `-User
  <DOMAIN\user>`, computed once from `USERDOMAIN`/`USERNAME` (the bare
  user name when `USERDOMAIN` is empty), because a bare `-AtLogOn` trigger
  means any user logon and Task Scheduler refuses to register it from a
  regular (non-elevated) session with "Access is denied" while the old
  script kept printing its success line. `Register-ScheduledTask` and
  `Start-ScheduledTask` run with `-ErrorAction Stop` in try/catch: a
  register failure exits 3, a start failure exits 4, both print the
  failing step and the exception, and no success line is printed after a
  failure; a failed start says the task IS registered and names the
  start/remove commands, and the dry run prints the logon trigger's user.
  The uninstall path keeps its exit 1 on the schtasks fallback failure and
  never prints a success line after a failure there either.
  tests/test_install_task_windows_ps1.py drives the real script through
  the stub harness for the happy path (the user-scoped trigger
  parameters), the register "Access is denied" failure and the
  registered-but-not-started case, pins the uninstall fallback behavior
  and the exact exit codes statically, and keeps the suite PowerShell 5.1
  compatible.
- `install-task-windows.ps1` gained a `-DryRun` switch that reports every
  action the installer would take, each line prefixed `DRY RUN:`, and
  changes nothing (no task register, remove or start, no schtasks call, no
  directory creation, no file write): unfilled placeholders are reported
  and exit non-zero, and path existence, the pythonw.exe choice or the
  python.exe fallback warning, the `G2C_RUNALERTS_LEDGER` check, the log
  directory, the task name, the exact action, triggers, settings and the
  would-be register and start are all reported; `-Uninstall -DryRun`
  reports which of the two task names are present and would be removed,
  read-only, listing both names as would-be checked when the
  ScheduledTasks cmdlets are unavailable. Filling the placeholders and
  pinning the sender copy stay manual steps the dry run validates. Tests
  drive the real script in a temp filled copy under a stub harness (skip
  without pwsh; the script stays PowerShell 5.1 compatible).
- Static guard tests: the launchd template's Label must equal the label its
  filename carries (it now really does) and the template must contain no
  `com.md.g2c` string; a tracked-tree scan bounds every remaining
  `com.md.g2c` / `ComMdG2c` occurrence to CHANGELOG history or a
  legacy-removal/compatibility line, and the installer's uninstall must walk
  both the registered and the legacy task name.
- .gitignore: local run artifacts and caches (`*.jsonl`, `*.log`, `.venv*/`,
  `.pytest_cache/`, `.ruff_cache/`) and key material (`secrets/`, `*.pem`,
  `*.key`, `*.p12`) are ignored; no tracked file matches any new pattern.
- CI least privilege: top-level `permissions: contents: read`; only the
  gitleaks job widens to `pull-requests: write` because gitleaks-action v2.3.9
  posts a PR review comment (Octokit pulls API, GITLEAKS_ENABLE_COMMENTS
  defaults true) when a pull_request scan finds a leak.

## 0.2.0 (run-approvals chat alert sender, three-OS, hardening)

Historical note: the 0.2.0 section and the original 0.1.0 contract
section below predate this repository's git tagging and were never
separate GitHub releases; 0.2.0 was only ever a version heading in this
changelog and in pyproject metadata. The first public GitHub release is
`0.1.0 - 2026-11-23` above (tag v0.1.0), which covers the current tree.

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

## 0.1.0 (initial client contract, pre-tag)

- `g2h`: dispatch, status, wait, stop, cancel, resolve, dashboard, relay.
- Crash-safe 0600 receipts in a 0700 dir, idempotent dispatch with replay
  detection, exit codes as the control plane.
- Loopback-only read-only dashboard (static or key-required serve mode).
- Approval event relay with one optional notifier destination (webhook or
  command), no automatic retries.
- Grok Bot bus example adapter and phone cards (outbound only, no reply
  route).
