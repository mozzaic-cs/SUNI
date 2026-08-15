#!/usr/bin/env bash
# Start SUNI (Linux / macOS). POSIX counterpart of start_suni.bat.
#
#   ./start_suni.sh              run in the foreground
#   SUNI_PORT=9000 ./start_suni.sh
#
# For an always-on service use deploy/suni.service (systemd) instead, which
# calls this script.
#
# Everything below is a DEFAULT: any value already set in the environment (or
# in .env, which web.py loads) wins, so this file never has to be edited.
set -euo pipefail

# Resolve the repo directory from this script's location — never a fixed path,
# so a clone anywhere works.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p logs
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting SUNI..." >> logs/startup.log

# Interpreter: explicit override, then the project venv, then the system python.
if [ -n "${SUNI_PYTHON:-}" ]; then
    PY="$SUNI_PYTHON"
elif [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi
if [ -z "${PY:-}" ] || ! "$PY" --version >/dev/null 2>&1; then
    echo "No usable Python found. Run install.py, or set SUNI_PYTHON." >&2
    exit 1
fi

# No model is set here. Which model to run is configured in the admin panel
# (Configuration → Model) and persists in SUNI's own config; the launcher
# pinning it would silently override whatever the admin chose. With nothing
# configured, SUNI uses the best model installed at the core tier.
#
# Ollama / GPU tuning belongs to the host, so set it in .env if the defaults do
# not suit your hardware. See .env.example.

# Print the resolved setup and exit — lets a launcher be checked without
# starting a second instance against the same data directory.
if [ -n "${SUNI_DRYRUN:-}" ]; then
    echo "python : $PY ($("$PY" --version 2>&1))"
    echo "workdir: $PWD"
    echo "model  : resolved by SUNI (admin config, else best installed)"
    exit 0
fi

exec "$PY" web.py >> logs/startup.log 2>&1
