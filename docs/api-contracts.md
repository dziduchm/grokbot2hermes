# GrokBot2Hermes API contracts

The HTTP surface implemented by `g2h`. Every contract below was verified
against the installed Hermes Agent api server documentation for
Hermes Agent v0.22.7. Where the upstream doc leaves a behavior open
(for example the exact success status or an optional header), the choice we
made is listed under "Implementation notes" at the bottom.

Conventions used throughout:

- Base URL: a local gateway by default, `http://127.0.0.1:8642`.
- Default profile routes look like `/v1/...`; a named profile routes like
  `/p/<profile>/v1/...` (multiplex mode; each named profile has its own key
  from that profile's env file).
- Auth: `Authorization: Bearer <key>` on every request. A missing or wrong
  key yields 401. There is always exactly one key per profile prefix.
- Errors are JSON objects shaped like `{"error": {"code": "...",
  "message": "..."}}`. g2h redacts anything it echoes.

## Capabilities

`GET /v1/capabilities` returns:

```json
{
  "object": "hermes.api_server.capabilities",
  "platform": "hermes-agent",
  "auth": {"type": "bearer", "required": true},
  "features": {"run_submission": true, "run_status": true,
               "run_events_sse": true, "run_stop": true,
               "run_approval": true, "reasoning_streaming": true}
}
```

g2h probes this once per invocation (5 second timeout). On any failure it
warns once in diagnostics, assumes the core runs surface
(`run_submission`, `run_status`, `run_events_sse`, `run_stop`) and
continues. The optional feature `run_unknown_resolution` (see resolve
unknown below) gates that subcommand.

## Create a run

`POST /v1/runs` with headers `Idempotency-Key: <1-255 visible ASCII
characters chosen by the caller>` and JSON body:

```json
{"input": "...", "instructions": null, "session_id": null,
 "model": null, "provider": null}
```

Only `input` is required and must be a non-empty string; every other field
is passed through only when set.

Responses:

- 202 accepted: an object with `run_id` and `status: "started"`.
- 202 + `Idempotency-Replayed: true` (same key, byte-identical payload):
  the ledger replays the original run id; no second run is created. The
  ledger holds keys together with the payload sha256 for about 24 hours.
- 409 `idempotency_key_conflict`: same key, different payload. Caller
  error, never retried.
- 409 `permission_denied`: `session_id` names a session that another
  surface owns. Caller error, never retried.
- 400 `invalid_params`: empty input or malformed body.
- 429 `too_many_concurrent` when the per-server run cap is reached
  (message like "Too many concurrent runs (max 10)"); `Retry-After` may be
  present and is honored when it is.
- 503 `runtime_draining` while the gateway is draining.

## Read run status

`GET /v1/runs/{run_id}` returns (fields depend on state):

```json
{"object": "hermes.run", "run_id": "run_abc123", "status": "completed",
 "session_id": "sess_abc123", "model": "...", "output": "...",
 "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
           "cache_read_tokens": 0, "cache_write_tokens": 0},
 "runtime": {"provider": "...", "model": "...", "route_source": "..."}}
```

State transitions observed by clients: `running` until a terminal status of
`completed`, `failed`, `cancelled` or `interrupted`. Two documented
non-terminal special cases:

- `waiting_for_approval` may carry `pending_controls`, a list of opaque
  control objects containing at least `prompt_id` (which g2h surfaces as
  the resolve `--request-id`) and `execution_generation`.
- `unknown` carries `admission_id` and `execution_generation` (the pieces
  resolve unknown needs).

404 means the run id is unknown to this profile prefix, or retention
expired: the server keeps a run's status only briefly after it goes
terminal.

## Events stream (SSE)

`GET /v1/runs/{run_id}/events` responds `text/event-stream`. Lines
starting with `:` are keepalive comments and are ignored. Event lines carry
`event:` names; terminal events are `run.completed`, `run.failed`,
`run.cancelled` and `run.interrupted`. g2h treats the `/status` endpoint as
the authoritative truth: event payloads are informational, and after any
stream failure the client reconnects (dropping at most to polling after 3
failed attempts within 60 seconds) and finally reconciles with one status
GET. Older gateways without the endpoint (404/405) fall back to polling
silently.

## Stop a run

`POST /v1/runs/{run_id}/stop` returns `{"run_id": "...", "status":
"stopping"}`. Stop is asynchronous and never instant: the run moves through
`stopping` and later settles to a terminal status (typically `cancelled`,
or `interrupted` during a gateway drain). g2h polls to terminal after
requesting the stop. Stopping a run whose admission the server cannot
resolve yields 409 `unknown_execution`.

## Resolve unknown (lost-execution recovery)

`POST /v1/runs/{run_id}/resolve-unknown` with a body of EXACTLY two fields:

```json
{"admission_id": "adm_abc123", "execution_generation": 7}
```

Advertised only while the capability `run_unknown_resolution` is on. The
200 response is a resolution receipt holding `admission_id`,
`execution_generation` and `outcome` (typically `interrupted`). The call
asserts that the possibly lost execution will never finish; g2h refuses to
run it without the explicit `--i-acknowledge-lost-execution` flag and
without the advertised capability. Errors: 400 `invalid_params` (wrong
field set), 409 `stale_generation` (generation no longer current), 409
`not_found` (the admission does not belong to this run).

## Resolve approval / clarify (manual, operator gated)

- `POST /v1/runs/{run_id}/approval` body: `{"request_id": "<prompt_id>",
  "execution_generation": <int>, "choice": "once" | "deny"}`
- `POST /v1/runs/{run_id}/clarify` body: `{"request_id": "<prompt_id>",
  "execution_generation": <int>, "answer": "<text response>"}`

Both return 200 on success. The run must actually be waiting and the
generation must be current, otherwise 409 `stale_generation`. A choice
outside `once | deny` is 400 `invalid_params`. g2h never sends these on its
own: they exist so a human can unblock `waiting_for_approval` runs.

## Rate limiting and availability

- 429: per-server concurrent run cap. g2h honors `Retry-After` when
  present, otherwise its own jittered backoff, within a retry budget.
- 503: runtime draining (gateway shutting down). Same retry policy.
- When the budget is exhausted g2h exits with code 13.

## Implementation notes (documented behaviors the upstream doc leaves open)

- A first create returning 200 or 202 is accepted as success either way.
- A stop reply of 200 or 202 is accepted as success; the upstream doc
  does not pin the stop success status.
- `Retry-After` is treated as optional on 429 and 503.
- Create replies: only `run_id` is parsed; anything else is ignored.
- `pending_controls` entries are printed verbatim; fields beyond
  `prompt_id` and `execution_generation` are never interpreted.
- A 404 on a status GET for a previously known run id is reported as
  retention expiry (the receipt keeps the last observed status).
- The upstream profile scoping list mentions a `steer` route which has no
  documented section; g2h never calls it.
- A 404 from the unprefixed path `/p/<profile>/v1/capabilities` is treated
  as a profile routing error (exit 11) with a hint about the prefix.

## Local contracts: receipt extensions, event documents, notifier

The fields below are g2h-local file formats (not gateway HTTP): receipts
written by g2h and consumed by the dashboard and relay. They carry
`"version": 1` and `"tool": "grokbot2hermes"`.

### Receipt additions (version 2 receipts)

Dispatch and every tracking command may update receipts with:

```json
{"version": 2,
 "pending_controls": [{"prompt_id": "...", "execution_generation": 4,
                        "...other gateway fields...": null}],
 "pending_controls_seen_at": "2026-01-01T00:00:00Z"}
```

`pending_controls` is `null` when the last known state had none and a
list while waiting. Unknown gateway fields inside a control element are
preserved verbatim and never interpreted; the relay only reads
`prompt_id`, `execution_generation` and the element's `kind` shape.

### Relay event document (schema v1)

One JSON file per event, `receipts/events/<UTC stamp>-<sha8>.json`,
mode 0600:

```json
{"version": 1,
 "tool": "grokbot2hermes",
 "kind": "approval.requested",
 "profile": "default",
 "base_url": "http://127.0.0.1:8642",
 "run_id": "run_abc123",
 "receipt": "run-2026...-abc123.json",
 "task": {"text": "...", "created_at": "2026-01-01T00:00:00Z"},
 "control": {"request_id": "...", "execution_generation": 4,
             "kind": "approval", "context": "<scrubbed json, capped>",
             "expiry": "..."},
 "resolution": null,
 "dedup_key": "<sha256 of run_id:prompt_id:execution_generation>",
 "first_seen_at": "2026-01-01T00:00:00Z",
 "observed_at": "2026-01-01T00:05:00Z",
 "source": "GET /v1/runs/run_abc123 pending_controls"}
```

Kinds: `approval.requested`, `approval.resolved` (with `resolution` one
of `answered`, `expired`, `withdrawn`, `resolved_unknown`),
`run.terminal` (run id and task present, no `control`; the run's last
known status stays in the receipt), `gateway.unreachable` (no run id).
`task.text` and `control.context` are scrubbed of the api key before
write.

### Dedup ledger (approvals.json)

`receipts/approvals.json`, mode 0600, one JSON object holding `entries`
keyed by `dedup_key` (state `seen`, `delivered`, `notify_failed` or
`resolved`; delivery metadata, request identity, `first_seen_at` /
`last_seen_at` / `delivered_at`, `source_event`), `deliveries` keyed by
event file name (attempts, delivered_at, last_error, kind, dedup_key),
and a `gateway` block (state and transition log). The ledger is the
restart-safe source of truth: a control reported once stays reported
across process restarts and is never re-emitted until it resolves.

### Dashboard snapshot additions (keyless receipts-only)

The `--json` snapshot gains a top-level `gateway_contact` string: the
ledger's gateway state (`ok` or `failing`) for any contacted pass, or
the literal `not-contacted` for a pass run without any key. A keyless
pass reads the local receipt store only: it makes no gateway request of
any kind, adds a receipts-only limitation entry to the snapshot's
`limitations` list, and writes nothing beyond the local event
directory. Serve mode always requires the key because it polls, so a
rendered page is the only keyless surface and it stays loopback-only.

### Notifier contract

Delivery happens after a pass persists events. Webhook: POST of the event
JSON to the configured URL, `Content-Type: application/json`,
`Authorization: Bearer <token>` when `G2H_NOTIFY_TOKEN` is set (required
for any webhook destination). The destination must return 2xx; anything
else, a transport error or a 10 second timeout records one failed attempt
(`notify_failed`) with the error text (token value never included).
Command: the event JSON on stdin; exit 0 delivered, any other exit
failed; 10 second timeout. Exactly one destination: if a URL and a
command both resolve, g2h exits with a usage error; a CLI flag beats a
same-kind env var (`--notify-url` vs `G2H_NOTIFY_URL`, `--notify-cmd` vs
`G2H_NOTIFY_CMD`). No destination means no delivery attempt, and the
only retry is the human-initiated `--redeliver`.
