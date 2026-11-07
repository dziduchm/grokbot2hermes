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

Supported version: the latest code on `main`.
