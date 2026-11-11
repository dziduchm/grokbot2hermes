# Summary (one or two sentences; what changed, not how you fixed it)

# Verification (copy the exact commands and their tails)
- CI: `ruff check g2h.py runalerts/ tests/` (pinned ruff==0.15.8)
- Tests: `python -m unittest discover -s tests -v`
- Dash scan and gitleaks run in CI; no em or en dashes anywhere, including
  SVG label text.

# Checklist
- Changes stay stdlib-only (Python 3.9+); no new runtime dependency.
- No em or en dashes in any file (CI byte scan).
- No secrets in the diff; argv, logs and receipts stay scrubbed.
- If network behavior changed: the mock tests cover it, and no automatic
  retry of caller errors was added (never auto-anything).
- If `runalerts/` changed: the allowlist, the ledger gate and the fail-closed
  permission checks stay intact, and `sender.py` still imports no Hermes
  key material.
