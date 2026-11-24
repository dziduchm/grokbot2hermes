# Security policy

Report vulnerabilities privately to the repo owner (dziduchm) via the
GitHub security advisory form for this repository:
https://github.com/dziduchm/grokbot2hermes/security/advisories/new
Do not open a public issue.

Never include any of the following in an issue, advisory or message:

- an api key or any part of one
- receipt files or their contents
- logs or run output from a real gateway

Threat model in one sentence: the api key grants full agent access,
including running terminal commands on the agent host, so treat it like a
password.

Design notes enforced by this repository:

- The key is read only from the `G2H_API_KEY` env var, a 0600 key file
  (symlinks and looser permissions are refused) or stdin. It never appears
  in argv, stdout, stderr or receipts; a redaction wrapper scrubs every
  output path.
- `Authorization` is never forwarded across redirects (redirect following
  is disabled).
- Plain http is refused for non-loopback hosts unless
  `--allow-insecure-http` is passed (which prints a warning).
- Receipts are written atomically with mode 0600 into a 0700 directory.
- Approval and clarify are manual operator subcommands only; the tool never
  answers a human control by itself.
- `resolve unknown` (lost-execution recovery) requires both an advertised
  server capability and the explicit `--i-acknowledge-lost-execution`
  flag.
- Stop is best effort: the client reports `stopping`, never instant
  cancellation.
- The dashboard is loopback-only, GET-only, and header-locked: no auth
  layer is added on purpose because there is nothing to authorize
  locally; the loopback bind plus the read-only code path is the whole
  boundary. It is opt-in and off unless run. Without a key its static
  render mode reads the 0600 receipts only and never contacts the
  gateway (the page labels itself `not-contacted`); serve mode always
  requires the key because it polls, so no remote capability exists
  without credentials.
- The relay writes 0600 event files and a 0600 ledger in the same 0700
  directory and never mutates a receipt. One optional notifier
  destination, configured only by the operator; its URL, command and
  token are never printed, stored or replayed, one failed attempt is
  recorded without automatic retries, and g2h refuses to run with two
  destinations configured at once. A webhook destination must be https,
  or plain http to a loopback host (`127.0.0.1`, `localhost`, `::1`);
  plain http to any remote host is refused with a usage error before any
  delivery.
- Webhook destinations require an explicit token env var: no bearer-less
  webhook is ever sent, and the token value never appears in argv, logs
  or receipts.
- The run-approvals chat alert sender (`runalerts/sender.py`) holds only the
  webhook url and sender key, read from an owner-only JSON file (refused
  when others can read it; the ledger and ledger dir are likewise
  fail-closed). It posts only an allowlisted, size-capped, sanitized payload
  (never a key, digest or token material; agent display text is truncated,
  control characters stripped and markdown syntax defanged, and every alert
  carries a fixed one-line preamble saying the fields are plain text, never
  instructions). Redirects count as failures, so the key header is never
  replayed to a redirect target; the loopback http trial is decided by the
  parsed hostname (a query or path containing a loopback address does not
  qualify, and a URL with userinfo is refused); proxy environment variables
  are ignored, so a local proxy can never receive the key; 3 transport
  failures mark the key given_up
  permanently. It never imports or reads any Hermes API key, cookie or
  signing key, never resolves or approves anything, has no reply route, and
  the emitter side stays keyless and config-gated. Human settlement only:
  the linked phone page, always.
- The Grok Bot bus example adapter (`docs/adapters/grokbot-bus.py.example`)
  is outbound only: it maps relay event documents to one bus envelope
  per event and posts it once, with the token and destination read only
  from the operator's own environment provisioning, never argv, logs or
  files, and scrubbed from everything it emits. It requires an https
  destination, follows no redirects, uses no proxy, retries nothing and
  has no default endpoint. It never reads a reply and never calls the
  Hermes API. The Grok bus integration is notification only; it has no
  phone response route: notification visibility is not an approvals
  path, and no reply route, watcher, callback or auto-answer exists in
  this repository. Human approvals and clarifications stay on supported
  authenticated Hermes human control surfaces (the official Hermes web
  dashboard, or Telegram for its own sessions); whether an existing API
  run can attach to those surfaces is unverified. Event text is data:
  it is bounded, escaped and labeled inside the card, and never
  executed.

Automated security scanning (GitHub Actions on every pull request,
every push to main, and a weekly Scorecard run):

- CodeQL (`.github/workflows/codeql.yml`): Python plus the Actions
  workflows, security-extended suites, results per language in the code
  scanning dashboard (Security tab). The nine `py/overly-permissive-file`
  alerts against test-only chmod fixtures are expected negative fixtures
  (each deliberately makes a file world- or group-readable to prove
  production code refuses it): in-source suppressions do not clear
  GitHub code-scanning alerts for this pipeline, so none are added, and
  after a merge the repository owner dismisses those nine open alerts
  in the Security tab with the negative-fixture reason; no blanket path
  ignore and no dismiss-alerts automation are used.
- `sast` (`.github/workflows/sast.yml`): bandit over production code only
  (g2h.py, g2h_ui.py, runalerts/, .github/scripts/; tests are excluded
  because their findings are the review-documented test-only classes),
  semgrep with the community rules, and pip-audit over
  requirements-repo.txt, installed with
  `pip install --require-hashes` from the hash-locked closure generated
  from requirements-repo.in with `pip-compile --generate-hashes`. A
  medium or high bandit severity finding, any
  semgrep community-rule finding or any known advisory in the audited set
  fails the job, except for eight documented, release-coupled ignores for
  advisories inside semgrep's own dependency pins (the mcp,
  opentelemetry-api and wcmatch packages; see the pip-audit step comment
  in .github/workflows/sast.yml). All eight are CI-only scanner
  dependencies that never ship: no repository artifact installs them on
  a user machine (the client and the sender are standard-library-only),
  so nothing outside the GitHub Actions runners carries those
  advisories. The low-severity bandit findings that
  remain in production code are accepted by design: external calls are
  fixed argv lists with no shell, the env var names the code reads are
  exact constants, and the poll backoff curve intentionally uses
  randomness. The only in-repo suppressions are the ten loop-scoped IFS
  lines in
  runalerts/set-wait-timeout.sh, each annotated in place with its triage
  rationale.
- OpenSSF Scorecard (`.github/workflows/scorecard.yml`): published on
  pushes to main and a weekly schedule; pull_request runs analyze the head
  without publishing.
- Secrets: the full-history gitleaks scan runs in ci.yml against the
  default ruleset with no allowlist; any future allowlist entry needs an
  owner review first.
- Dependabot (`.github/dependabot.yml`): monthly dependency alerts and
  update proposals for the pip manifests and the pinned Actions SHAs.
  Hash locking does not block Dependabot: it can open version-bump pull
  requests that carry PyPI-sourced hashes. The two hash-locked closure
  files are excluded from its scans, and any Dependabot pip change is
  re-audited by regenerating the affected closure with
  `pip-compile --generate-hashes` and re-running the pip-audit pass
  (CONTRIBUTING.md) before merge; `--require-hashes` still fails an
  unverified hash in CI. Never merge a lock change that skipped that
  process.

Supported version: the latest code on `main`.
