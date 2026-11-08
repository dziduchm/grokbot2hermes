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

## Opt-in outbound cards: the Grok Bot bus example adapter

`grokbot-bus.py.example` in this directory is an opt-in, outbound-only
example. When the operator runs `g2h relay --notify-cmd <path to a
copied adapter>`, the relay passes one event document (schema v1 JSON)
on stdin per event; the adapter maps it to exactly one bus envelope and
posts it once to the Grok Bot bus webhook. The card a human sees on the
Grok Bot side shows: profile, run id, prompt id, execution generation,
event kind, capped task text, the capped gateway-provided control
context (data only, never interpreted), reason or resolution fields if
the event carries them, a freshness statement, expiry (assumed label or
"unknown (gateway config not visible)"), the immutable event
fingerprint, and a fixed notice that the channel is notification only.

Boundaries implemented in the adapter, not just promised:

- Outbound only: it reads nothing, answers nothing, resolves nothing
  and never calls the Hermes API.
- Phone approval and phone resolution are NOT and remain UNSUPPORTED:
  no human decision flows back through this channel, and no reply
  route, watcher, callback or auto-answer exists anywhere in this
  repository. Any future phone approval or resolution design needs
  its own explicit authorization and review before anything is built.
  Notification visibility is not approval.
- No default endpoint: `GROKBOT_HOOK_URL` and `GROKBOT_HOOK_KEY` must
  be exported by the operator himself (bus protocol managed scope). The
  adapter reads only the process environment, never argv, never files,
  and never echoes or logs either value; both are scrubbed from
  everything the adapter emits. This documents existing secure key
  handling; it is not a credential wrapper and it expands no access.
- Destination must be exactly `https`, host required, no userinfo, no
  redirects, no proxy (the token must not leave the direct TLS path),
  one attempt, bounded timeout, no retries (exit codes 0/3/4/5/6).
- Input bounded to 64 KiB, fields capped, injected text flattened to
  one line, HTML escaped and labeled, so event text can never form
  card structure, markup or instructions; event text is never executed.
- Per-event identity: `id` is the first 16 hex of a sha256 over the
  bounded canonical fields (kind, profile, run id, prompt id,
  generation, control kind, stored control context verbatim at its
  write-time scrub, resolution, first seen, dedup key). The preimage
  never depends on the local environment, so the same event always
  maps to the same id, and distinct lifecycle events of one control
  (requested versus resolved) never collide.

Installation remains manual and operator-run: copy the example, read
it, adapt it, make it executable, wire it with `--notify-cmd`. Nothing
in this repository configures it, schedules it, or contacts any
endpoint by itself.
