# Grok Bot adapter guide

How a dispatching agent ("Grok Bot" in the tagline) wraps `g2h`. This
document is the adapter layer: the client itself contains zero
adapter-specific concepts (`g2h.py` never mentions adapters, queues or
bots).

## Rules for any adapter

1. Never pass the api key on argv, never log it. Use `G2H_API_KEY` in the
   environment, a 0600 key file via `--key-file`, or `--key-stdin`.
2. Never answer a human control automatically. `waiting_for_approval`
   (exit 16) goes to the operator, always.
3. Never rebuild a dispatch payload from scratch after a crash: replay the
   receipt (`g2h wait --receipt ...` resends the stored bytes with the
   stored idempotency key, so the gateway either replays the original run
   or admits it once).
4. Never retry caller errors (exit codes 2, 3, 12): they mean the adapter
   itself is wrong.
5. Do not inject turns into live sessions. The API refuses foreign
   sessions with 409 `permission_denied` by design.
6. Treat receipts as the adapter's durable queue, one receipt per
   dispatched task.

## Mapping intents to g2h calls

| Intent | Command |
|---|---|
| Hand a task to Hermes | `g2h dispatch --input "..."` |
| Fire and check later (cron) | `g2h dispatch --no-wait --input "..."` |
| Track a dispatched task | `g2h status --receipt ...` or `g2h status run_abc123` |
| Block until done | `g2h wait --receipt ...` (or `--follow` for SSE) |
| Give up on a task | `g2h stop run_abc123` (settles via `stopping`, never instant) |
| Unblock a waiting run | operator runs `resolve approval` / `resolve clarify` manually |

## Exit codes are the adapter's control plane

See the table in README.md. The ones an adapter must branch on:

- 0: task completed; `output` holds the result.
- 6 / 7: task was cancelled or interrupted; do not mistake it for failure.
- 11: run unknown to this profile prefix (typo, wrong `--profile`, or
  retention expired).
- 13: gateway busy or draining; back off and try later.
- 14: still running when your deadline passed; keep the receipt and poll
  again later.
- 16: a human decision is required; surface `pending_controls` verbatim
  and stop.
- 18: transport failure after retries; check gateway reachability.

## Example cron loop

`grokbot-poll.sh.example` in this directory shows a minimal POSIX sh loop
that walks a receipt directory and waits on every receipt. Human controls
(16) are surfaced and left untouched; the loop never resolves by itself.

## Worked examples

Dispatch and wait:

```console
$ # api key comes from G2H_API_KEY (or --key-file); see the README quickstart
$ ./g2h dispatch --json --input "Summarize the inbox and file it"
{"command": "dispatch", "diagnostics": [], "exit_code": 0,
 "idempotency_replayed": false, "output": "Filed 3 threads.",
 "output_truncated": false, "receipt_path": ".../run-...json",
 "run_id": "run_abc123", "status": "completed", "usage": {"total_tokens": 30}}
```

Recovering a dispatch interrupted mid-wait (exit code does not matter, the
receipt is the state):

```console
$ ./g2h wait --json --receipt .../run-...json
{"command": "wait", "diagnostics": [], "exit_code": 0,
 "idempotency_replayed": true, "run_id": "run_abc123", "status": "completed"}
```

`idempotency_replayed: true` means the gateway had already admitted the
task under the stored key; the original run was resumed, nothing ran twice.
