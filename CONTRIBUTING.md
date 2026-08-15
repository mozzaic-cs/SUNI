# Contributing to SUNI

Thanks for your interest in improving SUNI.

## Development setup

1. Install **Python 3.12** and [Ollama](https://ollama.com).
2. `python install.py` (creates `.venv` and installs `requirements.txt`).
3. Run it: `.venv\Scripts\python.exe web.py` (Windows) or `.venv/bin/python web.py`
   (Linux/macOS), then open `https://localhost:8765`.

## Guidelines

- Keep changes focused and match the surrounding code's style and idioms.
- For anything touching request routing or the core agent loop, prefer **additive,
  flag-gated** changes — SUNI's routing is easy to regress.
- **Never commit secrets or personal data.** `scripts/prepare_public_release.py` is the
  release scan gate: it verifies no credentials or personal identifiers are present.
- Run a quick smoke test (a normal chat works) before opening a pull request.

## Reporting security issues

Please report suspected vulnerabilities **privately** to the maintainer rather than in a
public issue, so a fix can be prepared before disclosure.
