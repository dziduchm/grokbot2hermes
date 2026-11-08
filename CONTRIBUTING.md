# Contributing

Thanks for considering a contribution. GrokBot2Hermes is an independent,
unofficial tool: it is not affiliated with, endorsed by, or connected to
xAI (Grok) or Nous Research (Hermes Agent). It is MIT licensed; see
LICENSE.

There is no formal governance, no Discord, and no paid support: issues and
pull requests on this repository are the channel.

## Repo layout in one paragraph

`g2h.py` is the whole client (single file, Python 3.9+ standard library
only); `g2h` is a three line launcher for it. `tests/mock_hermes.py` is an
in-process mock of the documented Runs API subset and `tests/test_g2h.py`
runs the real client as a subprocess against it. `docs/api-contracts.md`
is the HTTP contract reference, `docs/adapters/` holds adapter guidance
(`grokbot.md`) and a polling example (`grokbot-poll.sh.example`), and
`docs/architecture/` holds the architecture diagram (editable `.drawio`
source plus an SVG preview, editable at https://app.diagrams.net or in the
draw.io desktop app; please re-export or update the SVG preview so it
stays in sync with the `.drawio` source). `.github/workflows/ci.yml` is
the pipeline.

## Local setup

```console
$ git clone https://github.com/dziduchm/grokbot2hermes
$ cd grokbot2hermes
$ chmod +x g2h
```

What you need:

- **Python 3.9+** for the client and the test suite. The CI matrix runs
  Python 3.9 and 3.12 on Linux and macOS. If you do not have the required
  Python locally, you can skip straight to reading the CI run on your
  pull request.
- **Linting** uses ruff; the CI pins the exact version (0.15.8). There are
  no third-party runtime dependencies anywhere in the client; ruff is the
  only dev-time tool you may want, so a throwaway virtualenv is enough:

```console
$ python -m venv /tmp/g2h-lint-venv
$ /tmp/g2h-lint-venv/bin/python -m pip install "ruff==0.15.8"
$ /tmp/g2h-lint-venv/bin/ruff --version
```

## Commands CI runs (copy these, do not invent your own)

CI (`.github/workflows/ci.yml`) does a lint pass, a test pass, a dash
scan, and a secrets scan over every pull request.

Lint, with the exact pinned ruff version CI installs:

```console
$ python -m pip install "ruff==0.15.8"
$ ruff check g2h.py tests/
```

Tests, exactly as CI invokes them:

```console
$ python -m unittest discover -s tests -v
```

Dash scan: the CI shell script byte-scans every tracked file and fails the
build if any U+2014 (em dash) or U+2013 (en dash) appears anywhere,
including inside SVG and draw.io label text. The check lives inline in
`.github/workflows/ci.yml` under its "dash scan" step; there is nothing to
install. If a reviewer asks you to avoid those characters, the CI rule is
why.

Secrets scan: CI also runs a full-history gitleaks pass using the pinned
gitleaks-action against its default ruleset, so new or historical secrets
in commits will fail the build.

## Test mechanics: the mock gateway

You normally do not need a real Hermes gateway. `tests/mock_hermes.py`
implements, on a loopback port bound per test, the documented subset of
the Runs API from `docs/api-contracts.md`:

- bearer auth with per-profile keys (the mock throws away every key it
  generates once the test ends),
- `GET /v1/capabilities`,
- `POST /v1/runs` with an idempotency ledger keyed on `Idempotency-Key`
  plus the request body hash, returning `Idempotency-Replayed: true` on
  replays,
- `GET /v1/runs/{run_id}`, including `pending_controls` for parked runs,
- `GET /v1/runs/{run_id}/events` as an SSE stream with injectable
  drop/failure behavior (which is what exercises the polling fallback),
- `POST /v1/runs/{run_id}/stop`,
- `POST /v1/runs/{run_id}/approval`, `/clarify` and `/resolve-unknown`
  with generation and replay-id checks, including the stale generation
  conflicts real gateways can return.

`tests/test_g2h.py` starts a fresh mock for every test (fresh scratch
receipt directory too) and runs the real `g2h.py` as a subprocess, then
asserts on stdout lines, stderr diagnostics, exit codes, and the receipt
files. Failure sequences, status sequences, retry budgets, 040x/05xx
paths and receipt round trips are all driven through this harness.

## Optional: live testing against your own gateway

Only ever test against **your own local** Hermes Agent gateway (its API
server enabled, `API_SERVER_KEY` set there).

- Never commit, paste, screenshot or share a key. The key grants full
  agent access, including terminal commands on the agent host; treat it
  like a password (threat model in SECURITY.md).
- The client never takes a key on the command line. Give it one via the
  `G2H_API_KEY` environment variable in your own shell, a `--key-file`
  with mode 0600, or `--key-stdin`; the client refuses a key file whose
  mode differs from 0600.
- Receipts land in a 0700 directory as 0600 files. If you test on shared
  media, remove the directory afterwards with your usual secure delete
  practice.
- Prefer the mock for anything reproducible; use live testing for a final
  smoke only, and keep the sanitised details (no keys, no session ids, no
  private run contents) out of issues and pull requests.

## Branch and pull request process

- Fork or branch off `main` (branch names like `docs/architecture`,
  `fix/exit-14`).
- One topic per pull request. Docs-only and single-behavior pull requests
  are welcome; keep refactors separate.
- The client itself takes no dependencies, so keep it that way: standard
  library only, single file.
- Every pull request must come back green on CI: lint, tests, the dash
  scan and the secrets scan. Run at least the lint and test commands
  above locally before pushing.
- Changes are merged after human review; do not force-push to shared
  branches (rebasing your own feature branch before asking for review is
  fine).
- If your change touches the HTTP contract behavior, update
  `docs/api-contracts.md` in the same pull request.

## Reporting security issues

See SECURITY.md. In short: use a private GitHub security advisory (the
mechanism GitHub exposes on this repository) instead of a public issue,
never attach keys, receipts or logs, and allow time for a fix. If
SECURITY.md conflicts with anything written here, SECURITY.md wins.

## Status and licence

Unofficial third-party integration; not affiliated with Nous Research or
xAI. MIT licensed; see LICENSE. There is currently no separate code of
conduct; GitHub's standard etiquette applies.
