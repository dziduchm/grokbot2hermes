# Security policy

Report vulnerabilities privately to the repo owner (dziduchm) via a GitHub
security advisory on this repository. Do not open a public issue.

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
  destinations configured at once.
- Webhook destinations require an explicit token env var: no bearer-less
  webhook is ever sent, and the token value never appears in argv, logs
  or receipts.
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

Supported version: the latest code on `main`.
