# Contributing

Bug reports and pull requests are welcome.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests scripts
.venv/bin/pytest            # unit tests + simulated-camera end to end (needs ffmpeg with WHIP and mediamtx; skipped otherwise)
```

- Keep changes small and covered by a test. The tests use fake servers on `127.0.0.1` only: no account, no keys, no real cameras.
- Never put keys, tokens, camera passwords or real addresses in code, tests, fixtures or issues.
- By contributing you agree your work is released under the MIT license ([LICENSE](LICENSE)).
- Security problems: see [SECURITY.md](SECURITY.md); do not open a public issue.
