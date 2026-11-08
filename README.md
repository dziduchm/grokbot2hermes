# GrokBot2Hermes

Dispatch tasks from Grok Bot to Hermes Agent. Not a migration tool.

![Unofficial integration: a chat bubble dispatches task cards across a bridge toward the Hermes Agent logo.](docs/assets/hero.jpg)

> GrokBot2Hermes is an independent, unofficial tool. It is not affiliated
> with, endorsed by, or connected to xAI (Grok) or Nous Research (Hermes
> Agent). Grok and Hermes are trademarks of their respective owners.
> MIT licensed: see LICENSE.

## What it is and what it is not

It IS: a small Python 3.9+ command line client, `g2h`, single file plus a
three line launcher, standard library only. It dispatches a task to a
Hermes Agent gateway's Runs API, tracks it to a terminal state, writes a
crash-safe receipt, reports by exit code, and leaves human approvals to
humans. Two opt-in observers sit on top of the receipt store: a local
read-only dashboard and an approval event relay with one optional
notifier destination.

It is NOT: a migration tool, a framework, a hosted service, a hosted
dashboard, a team wrapper, or a library with API promises. It adds no
auto-anything: no auto-merge, no auto-approve, no auto-retry of caller
errors, no automatic notifier retries either.

## Architecture

![Architecture diagram: Grok Bot and example adapters dispatch tasks through the g2h CLI over a bearer-authenticated loopback HTTP connection to the local Hermes Agent gateway Runs API, with crash-safe 0600 receipts, exit codes as the control plane, per-profile routing, and a manual human approval flow.](docs/architecture/grokbot2hermes.svg)

The editable source is `docs/architecture/grokbot2hermes.drawio`. Open it
at https://app.diagrams.net (draw.io) or in the draw.io desktop app, edit,
and export an updated SVG preview over `docs/architecture/grokbot2hermes.svg`
so the two stay in sync. The diagram shows only shipped behavior;
unimplemented ideas are deliberately not drawn.

## Requirements

- Python 3.9+ on macOS or Linux.
- A Hermes Agent gateway with the API server enabled and `API_SERVER_KEY`
  set (the gateway is what runs the agent; see the upstream Hermes Agent
  repository: https://github.com/NousResearch/hermes-agent).

## Install

```console
$ git clone https://github.com/dziduchm/grokbot2hermes
$ cd grokbot2hermes
$ chmod +x g2h
```

No pip install, no dependencies beyond the standard library.

## Quickstart

```console
$ export G2H_API_KEY=<your gateway key>        # or use a 0600 key file
$ export G2H_BASE_URL=http://127.0.0.1:8642    # optional, this is the default
$ ./g2h dispatch --input "Summarize the inbox and file it"
run_id: run_abc123
status: completed
output: Filed 3 threads.
$ echo $?
0
```

Prefer a key file over an env var:

```console
$ ./g2h dispatch --key-file /path/to/key-0600 --input "..."
```

Named profiles route under a prefix (`--profile coder` becomes
`/p/coder/v1/...`) and use that profile's own key. Receipts land in
`~/.local/state/grokbot2hermes/` (override with `G2H_RECEIPT_DIR`): mode
0600 files in a 0700 directory, one per dispatched task, safe to keep and
replay.

## Exit codes

Exit codes are the control plane: automation branches on them, never on
prose. The table is the single source of truth; it is mirrored in
`g2h.py`.

| Code | Meaning | Typical trigger |
|------|---------|-----------------|
| 0 | completed, or a clean non-terminal `status` read | run reached terminal `completed`; `status` on `running`, `stopping` or `unknown` also reports 0 |
| 1 | internal error | unexpected client bug; scrubbed traceback |
| 2 | usage error | bad flags, empty input, invalid idempotency key chars |
| 3 | credential or config error | key missing, key file perms not 0600, receipt dir not 0700 |
| 5 | run failed | terminal `failed` |
| 6 | run cancelled | terminal `cancelled` (including after stop) |
| 7 | run interrupted | terminal `interrupted` (gateway shutdown / drain) |
| 10 | auth rejected | HTTP 401 (wrong key for this profile prefix) |
| 11 | not found | HTTP 404 (unknown run id, wrong profile, or expired retention) |
| 12 | idempotency conflict | HTTP 409 `idempotency_key_conflict` (same key, different payload) |
| 13 | rate limited or unavailable | 429/503 budget exhausted after retries |
| 14 | timeout | `--timeout` expired while non-terminal (includes parked `unknown`) |
| 16 | awaiting human control | `waiting_for_approval` / `pending_controls` observed by dispatch, wait or status |
| 18 | transport error | connection failures after retry budget |

Note: 409 `permission_denied` (a `session_id` another surface owns)
reports as a scrubbed diagnostic in the exit 12 family with its own
message text, since it is a caller error, not a key collision. 409
`stale_generation` on resolve subcommands maps to exit 12, and 409
`not_found` on them maps to exit 11, each with its own message.

## Examples

Cron one-shot, check later (the receipt carries the full task):

```console
$ ./g2h dispatch --no-wait --input "Nightly report"
run_id: run_abc123
receipt: ~/.local/state/grokbot2hermes/default/run-....json
$ ./g2h wait --receipt ~/.local/state/grokbot2hermes/default/run-....json
```

If the client or the machine dies mid-wait, rerunning `wait --receipt`
resends the stored bytes with the stored idempotency key: the gateway
either replays the original run or admits it once. Nothing runs twice.

CI job with a strict deadline:

```console
$ ./g2h dispatch --timeout 900 --max-retries 8 --input "Regressions?"
```

Exit 14 means "still running at your deadline": keep the receipt and poll
again; the task did not vanish.

Adapters that auto-dispatch tasks (the "Grok Bot" side) should read
docs/adapters/grokbot.md, including the never-auto-answer rules for human
controls.

## Local dashboard (opt-in, read-only)

`g2h dashboard` renders an HTML view over the receipts g2h itself owns.
It is for the operator sitting at the same machine: it binds to loopback
only (the `--bind` values `127.0.0.1` and `localhost` are the only ones
accepted), answers GET requests only, sets
`Content-Security-Policy: default-src 'none'; style-src 'self'` and
`X-Content-Type-Options: nosniff`, ships no JavaScript, cookies, sessions
or forms, and never mutates anything: it never POSTs to the gateway and
never touches controls. It reads the same 0600 receipts; it is not an
auth layer, a remote console, or a second control plane, and it is off
unless you run it.

The key is only needed for `--serve`, because serve mode polls the
gateway while it runs. Without any key, the static surfaces (`--out`,
`--json`, or stdout) still render the local receipt store: the pass
skips every gateway read, shows `gateway: not-contacted` and adds a
receipts-only limitation to the page, so nothing remote happens and
nothing is sent anywhere. Serve mode never starts without a key, so
there is no remote access without credentials.

```console
$ ./g2h dashboard --out snapshot.html   # render one static page, exit
$ ./g2h dashboard --serve               # serve and poll until Ctrl+C (key required)
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--serve` | off | serve on loopback and poll (default prints once) |
| `--out FILE` | stdout | write the snapshot to FILE |
| `--port N` | 8790 or `G2H_DASHBOARD_PORT` | serve port |
| `--bind HOST` | 127.0.0.1 | loopback only: 127.0.0.1 or localhost |
| `--refresh SECS` | 10 | page meta refresh; 0 disables (min 5) |
| `--poll-interval SECS` | 15 | serve-mode gateway poll (min 5) |
| `--max-runs N` | 50 | newest N receipts shown, the rest summarized |

## Approval event relay

`g2h relay` turns receipts into a small, local event history for
automation watchers. One pass reads each receipt's current state and
writes events next to the receipts (one 0600 JSON file per event, stamped
name in `receipts/events/`):

- `approval.requested`: a pending control appeared (pending approvals
  only; dispatched-but-approved never fires).
- `approval.resolved`: a pending control left the pending set, with a
  resolution: `answered`, `expired`, `withdrawn` or `resolved_unknown`
  (vanished while the client was down).
- `run.terminal`: a tracked run reached a terminal status.
- `gateway.unreachable`: every contact in a pass failed.

`receipts/approvals.json` is the dedup ledger (mode 0600, key = sha256 of
run id plus the control identity): a resumed pass reports a control once,
across restarts, and never re-fires one that was already reported. An
entry's own files are dropped together with it by `--prune-days` (30
default; receipts are never touched).

| Flag | Default | Meaning |
|------|---------|---------|
| `--once` | default | one pass (also chosen when neither flag given) |
| `--watch` | off | pass every `--interval` seconds until Ctrl+C |
| `--interval SECS` | 30 | `--watch` cadence (min 10) |
| `--notify-url URL` / `G2H_NOTIFY_URL` | unset | one webhook destination |
| `--notify-cmd CMD` / `G2H_NOTIFY_CMD` | unset | one command destination |
| `--redeliver PREFIX` | none | one human-initiated reattempt for one event |
| `--prune-days N` | 30 | drop resolved entries/event files older than N |

The notifier is generic and opt-in: exactly one destination (a URL and a
command together are refused as a usage error; a CLI flag beats the
same-kind env var), one delivery attempt per event per pass, failures
recorded in the ledger and never retried automatically. The webhook
receives the full event JSON with an `Authorization: Bearer` header
carrying the token from `G2H_NOTIFY_TOKEN` (required for the webhook;
name only in
output; the value never appears in argv, logs or receipts); the command
destination gets the same JSON on stdin and exit 0 means delivered. No
destination exists by default: this repository ships no URL, no token
and no integration endpoint, and wiring one (for example to any agent
bus) is a separate, explicitly approved deployment decision.

Exit rules: 0 when the pass ran (even with zero events), 18 when every
gateway read failed, and the same receipt/credential/usage codes as the
table above otherwise. `--json` adds `events`, `delivered`,
`notify_failed`, `notifier`, `contacts`, `failures` and `pruned` to the
payload.

## Run the tests

The suite runs the real client as a subprocess against an in-process mock
gateway; all api keys are generated throwaway strings:

```console
$ python3 -m unittest discover -s tests -v
```

What Hermes already gives you natively versus what this adds:

| Already in Hermes Agent | Added by g2h |
|---|---|
| Kanban, review lanes, webhooks, dashboards | crash-safe 0600 receipts |
| agent runs, sessions, approvals UI | idempotent dispatch with replay detection |
| multiplex profiles under /p/ | backoff with Retry-After honoring |
| stop and drain handling | a stable exit-code contract for automation |

## Security notes

Full policy: SECURITY.md. The api key grants full agent access, including
terminal commands on the agent host: treat it like a password. The key is
only ever read from `G2H_API_KEY`, a 0600 key file or stdin; it never
appears in argv, output, logs or receipts (a redaction wrapper scrubs
every output path). Authorization is never forwarded across redirects;
plain http to non-loopback hosts is refused unless you pass
`--allow-insecure-http` (which warns). Approval and clarify are manual
operator subcommands only.

## Version support and provenance

Developed against the Hermes Agent v0.22.7 Runs API as documented in the
upstream api server documentation; every HTTP contract was verified
against that documentation before implementation. Contracts that the
upstream doc leaves open are listed in docs/api-contracts.md under
"Implementation notes". Degradation behavior on older or newer gateways:
the capabilities probe fails soft to the core runs surface, unrecognized
statuses are polled with a warning instead of a guess, and the optional
SSE endpoint is skipped silently when absent.

Tested live (sanitised): one dispatch against a local gateway's named
profile route reached terminal `completed` with exit 0 and its reply
output; during that run the gateway reported a status the client had
never seen, and the client warned once in diagnostics, kept polling and
completed. Receipts were 0600 in a 0700 directory, and the api key
appeared in no output, receipt or log. Stop, replay, wrong-key and
wrong-profile paths were verified against the mock suite, not against
real runs.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup, the exact lint and
test commands CI runs, how the mock gateway in `tests/mock_hermes.py`
works, optional live testing against your own local gateway, and the
branch and pull request process.

## Licence and attribution

> Hermes Agent is made by Nous Research and is MIT licensed:
> https://github.com/NousResearch/hermes-agent
> This project contains no Hermes source code; it only calls its
> documented HTTP API.

MIT licensed; see LICENSE.
