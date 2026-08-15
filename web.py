"""Start SUNI's web UI — python web.py"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Load credentials from .env (never hardcode secrets in source)
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Non-secret defaults — overridden by environment if already set.
# SUNI_MODEL is deliberately absent: the model is chosen in the admin panel and
# resolved by model_inventory.resolve_model() (config, then this environment
# variable if an operator sets one, then the best model actually installed).
# Defaulting it here would override the admin's choice before it was ever read.
#
# OLLAMA_* variables are deliberately NOT set here. Ollama is a separate
# service, started independently of SUNI, and a child process cannot change an
# already-running server's environment — so setting them here never reached
# Ollama and only looked like tuning. (On this machine the illusion was visible:
# OLLAMA_GPU_LAYERS was 48 here and 43 in the environment Ollama actually
# started with.) SUNI also sends keep_alive per request in ollama_agent.py,
# which overrides OLLAMA_KEEP_ALIVE regardless. To tune Ollama, set the
# variables in ITS environment — see .env.example.
_DEFAULTS = {
    "SUNI_EMBED_MODEL":     "qwen2.5:1.5b",
    # Real, and used by SUNI's own process: selects the GPU for torch
    # (image generation, sentence-transformers), not for Ollama.
    "CUDA_VISIBLE_DEVICES": "0",
}
for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)

import uvicorn
from pathlib import Path
from suni.logger import setup as _log_setup
from suni.system_profile import log_profile as _log_profile
from suni.web.server import create_app

_log_setup()      # must be first — before any suni module logs
_log_profile()    # log detected hardware + derived limits

app = create_app()

if __name__ == '__main__':
    port = int(os.environ.get('SUNI_PORT', 8765))

    cert = Path(__file__).parent / 'certs' / 'cert.pem'
    key  = Path(__file__).parent / 'certs' / 'key.pem'

    if cert.exists() and key.exists():
        print(f'\n  SUNI is listening at  https://localhost:{port}  (TLS)\n')
        uvicorn.run(app, host='0.0.0.0', port=port, log_level='warning',
                    ssl_certfile=str(cert), ssl_keyfile=str(key))
    else:
        print(f'\n  SUNI is listening at  http://localhost:{port}  (no TLS — run gen_cert.py)\n')
        uvicorn.run(app, host='0.0.0.0', port=port, log_level='warning')
