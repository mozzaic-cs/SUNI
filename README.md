# SUNI — System of Unified Networked Intelligence

A **self-hosted, privacy-first AI assistant and orchestrator**. Voice, a 3D persona,
long-term memory, tools, messaging channels, and — uniquely — **multiple models that
collaborate on one answer**. Runs on your own hardware; your data stays yours.

![license: MIT](https://img.shields.io/badge/license-MIT-blue) ![python: 3.12](https://img.shields.io/badge/python-3.12-blue)

---

## Highlights

- **Local-first & private** — runs on your machine with [Ollama](https://ollama.com); no cloud required for core use.
- **Multi-model collaboration** *(the differentiator)* — an opt-in mode where capable models (e.g. Claude Code + Codex) **draft → cross-critique → synthesise** a single best answer.
- **Three interfaces** — an **Orb** (WebGL), a **Chat** UI, and a **voice-first 3D Persona** (Face) with webcam head-tracking.
- **Reach it anywhere** — **Telegram, Discord & Slack** gateways that connect *out* (no public URL / port-forwarding), plus WhatsApp (webhook) and email.
- **Memory + knowledge base** — episodic + collective memory, document RAG, and a **learned-skills** store (procedural memory) that grows as you use it.
- **Pluggable models** — local Ollama/vLLM **plus** Claude, OpenAI, Gemini, and **no-key subscription CLIs** (Claude Code, Codex).
- **Governance** — RBAC, intent judge, output guard, tool policies, approval previews, OIDC/SSO, rate limiting, audit log.
- **Batteries included** — **19 starter skills** and a **22-server MCP catalog** you can one-click add.

---

## Quickstart

**Prerequisites:** Python 3.12 and [Ollama](https://ollama.com).

```bash
git clone <your-repo-url> suni && cd suni
ollama pull qwen2.5:7b          # the default local model
python install.py               # creates a venv and installs dependencies
```

Then start SUNI:

```bash
# Windows
.venv\Scripts\python.exe web.py
# Linux / macOS
.venv/bin/python web.py
```

Open **https://localhost:8765**, accept the self-signed certificate, and **create your
admin account** on the first-run screen. That's it.

> SUNI generates its own signing secrets and databases on first run — nothing to configure to get started.

---

## Optional extras

- **Document knowledge base** — semantic search over your own files. Install the heavy
  embedding stack (pulls PyTorch): see [`requirements-embeddings.txt`](requirements-embeddings.txt).
- **Frontier models & the collaborate mode** — install the **Claude Code** and/or
  **OpenAI Codex** CLIs and log in; SUNI auto-detects them. No API keys needed (they use
  your existing subscriptions). API-key providers (Claude, OpenAI, Gemini) also work.
- **Messaging channels** — enable Telegram / Discord / Slack in the admin panel
  (Settings → Channels). They connect out, so no public URL is required.

---

## Configuration

- Copy `.env.example` to `.env` for optional email/channel credentials.
- Everything else lives in the **admin panel** (`/admin` → Configuration): model,
  voice/language, memory, security, channels, and the multi-model collaboration pool.

---

## Architecture

SUNI is a FastAPI server (HTTPS) with tiered model routing, async background workers
(memory, document indexing, channels, schedulers), a pluggable provider registry, and
three WebGL/HTML front-ends. A live architecture diagram is available in-app at
`/architecture`.

- **Models & routing:** `suni/models/`, `suni/core/` (orchestrator, router, tiers)
- **Memory & skills:** `suni/memory/`, `suni/skills.py`, `bundled_skills/`
- **Channels:** `suni/telegram/`, `suni/discord/`, `suni/slack/`
- **Web & API:** `suni/web/` (server, Orb/Chat/Face/Admin UIs)

---

## Contributing

Issues and pull requests are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT © 2026 MOZZAIC — see [`LICENSE`](LICENSE).
