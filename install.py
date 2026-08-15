#!/usr/bin/env python3
"""
SUNI installer — prepare a clean environment to run SUNI.

    python install.py

What it does:
  1. Checks Python >= 3.10.
  2. Creates a virtual environment (.venv).
  3. Installs the pinned core dependencies (requirements.txt).
  4. Ensures a .env exists (copied from .env.example for you to fill in).
  5. Checks whether Ollama is reachable and the default model is present.
  6. Prints how to start SUNI and finish setup in your browser.

SUNI generates its own secrets (JWT signing key, service API token) and databases
on first run, and the admin account is created in the browser the first time you
open it — so this installer only prepares the environment; it never handles secrets.

Optional: for the document knowledge base, also install requirements-embeddings.txt
(it pulls PyTorch — large and hardware-specific; SUNI runs fine without it).
"""
from __future__ import annotations
import os
import sys
import shutil
import subprocess
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
# 3.10, not 3.11: the whole test suite passes on 3.10 and no 3.11-only syntax
# or stdlib is used. Ubuntu 22.04 LTS — still the most widely deployed LTS —
# ships 3.10, so requiring 3.11 turned a working install into a hard stop for a
# large share of Linux users, for nothing.
PY_MIN = (3, 10)

# A model to SUGGEST when none is installed. SUNI itself hardcodes no model:
# the admin chooses one, and with nothing configured it uses the best model
# already present (model_inventory.resolve_model). This is installer guidance
# for an empty machine, not a default the application applies.
SUGGESTED_MODEL = "qwen2.5:7b"


def step(m: str) -> None: print(f"\n==> {m}")
def info(m: str) -> None: print(f"    {m}")


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    step("Checking Python version")
    if sys.version_info < PY_MIN:
        sys.exit(f"Python {PY_MIN[0]}.{PY_MIN[1]}+ required; you have {sys.version.split()[0]}")
    info(f"Python {sys.version.split()[0]} OK")

    step("Creating virtual environment (.venv)")
    if not VENV.exists():
        venv.EnvBuilder(with_pip=True).create(VENV)
        info(f"created {VENV}")
    else:
        info(".venv already exists — reusing")
    vpy = venv_python()

    step("Installing core dependencies (downloads packages — may take a minute)")
    subprocess.check_call([str(vpy), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    subprocess.check_call([str(vpy), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")])
    info("dependencies installed")

    step("Preparing config + folders")
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists() and example.exists():
        shutil.copy(example, env)
        info("created .env from .env.example — edit it to set email/channels (all optional)")
    else:
        info(".env present (or no template to copy)")
    for d in ("memory", "logs"):
        (ROOT / d).mkdir(exist_ok=True)

    step("Checking Ollama (local model backend)")
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
            tags = json.loads(r.read().decode("utf-8"))
        names = [m.get("name", "") for m in tags.get("models", [])]
        info("Ollama is running.")
        if names:
            info(f"{len(names)} model(s) available — SUNI will use the best "
                 f"installed one unless you choose another in the admin panel.")
        else:
            info(f"no models installed — get one with:  ollama pull {SUGGESTED_MODEL}")
    except Exception:
        info("Ollama not reachable at localhost:11434.")
        info("Install it from https://ollama.com, then:  ollama pull " + SUGGESTED_MODEL)

    py = str(vpy)
    step("Done — to start SUNI:")
    print(f"""
    {py} web.py

  Then open  https://localhost:8765  (accept the self-signed certificate) and
  create your admin account on the first-run screen.

  Optional — document knowledge base (heavy, pulls PyTorch):
      see requirements-embeddings.txt
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
