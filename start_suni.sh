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

# Which port to guard on. The environment wins; otherwise read .env (which
# web.py itself loads), else the 8765 default — so the check follows the port
# the server will really use rather than a guess.
if [ -z "${SUNI_PORT:-}" ] && [ -f .env ]; then
    SUNI_PORT="$(sed -n 's/^[[:space:]]*SUNI_PORT[[:space:]]*=[[:space:]]*//p' .env | tail -n1 | tr -d '
\"'"'"' ')"
fi
SUNI_PORT="${SUNI_PORT:-8765}"

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
    if (exec 3<>/dev/tcp/127.0.0.1/"$SUNI_PORT") 2>/dev/null; then
        exec 3<&-; echo "port   : $SUNI_PORT [IN USE - would decline]"
    else
        echo "port   : $SUNI_PORT [free - would start]"
    fi
    echo "model  : resolved by SUNI (admin config, else best installed)"
    exit 0
fi

# Refuse to start a SECOND instance against the same data directory.
#
# systemd restarts this unit, and the Windows counterpart retries on a repeating
# trigger, so an outage cannot last days. That is only safe if starting while
# SUNI is already up is a no-op: two processes on one memory/ directory means
# two writers on the same SQLite files, and whichever loses the port bind dies
# noisily while the service manager reports a run that went fine.
#
# The port is the check because it is the thing that is actually exclusive.
# /dev/tcp is bash-native, so this needs no ss, lsof or netstat to be installed.
#
# Exit 0, not an error, when the port is taken. Under systemd a non-zero exit
# would make the unit restart in a loop against an instance that is running
# perfectly well. This branch should never be reached under systemd anyway --
# it stops the old process fully before starting the new one -- so reaching it
# means something outside the unit holds the port, which is a situation to
# report rather than to fight.
if (exec 3<>/dev/tcp/127.0.0.1/"$SUNI_PORT") 2>/dev/null; then
    exec 3<&-
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Already listening on $SUNI_PORT - not starting a second instance" >> logs/launcher.log
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting SUNI..." >> logs/startup.log

exec "$PY" web.py >> logs/startup.log 2>&1
