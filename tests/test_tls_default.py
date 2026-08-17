"""TLS must be on by default, because both quickstarts promise it.

The bug this guards: `web.py` only enabled TLS if `certs/` already existed, and
nothing ever created it. Meanwhile README.md and docker-compose.yml both told a
new user to open https://localhost:8765. So the documented first step failed
with a TLS error, and reaching for http:// instead worked — which meant the
admin account and its password were created over cleartext, on a port compose
publishes on every host interface.

Nothing detected it because each half was individually correct: the code was a
truthful `if cert.exists()`, the docs described the intended deployment, and no
test compared the two. That is the same shape as the settings that reached
nothing — see tests/test_settings_are_wired.py.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_gen_cert_writes_a_usable_pair(tmp_path):
    """The generator itself works, in a directory that starts empty."""
    (tmp_path / "gen_cert.py").write_bytes((ROOT / "gen_cert.py").read_bytes())
    subprocess.run([sys.executable, "gen_cert.py"], cwd=tmp_path, check=True,
                   capture_output=True, timeout=120)
    cert = tmp_path / "certs" / "cert.pem"
    key = tmp_path / "certs" / "key.pem"
    assert cert.exists() and key.exists()
    assert cert.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert "PRIVATE KEY" in key.read_text()


def test_web_py_generates_before_it_decides():
    """The generation must run BEFORE the exists() check that picks the scheme.

    Ordering is the whole fix: generating afterwards would still serve the
    first run over plaintext.
    """
    src = (ROOT / "web.py").read_text(encoding="utf-8")
    gen = src.index("import gen_cert")
    decide = src.index("if cert.exists() and key.exists():\n        print")
    assert gen < decide, "the certificate is generated after the scheme is chosen"


def test_tls_can_be_declined_explicitly():
    """A proxy that terminates TLS needs an opt-out, or this is a downgrade."""
    src = (ROOT / "web.py").read_text(encoding="utf-8")
    assert "SUNI_NO_TLS" in src


@pytest.mark.parametrize("doc", ["README.md", "docker-compose.yml"])
def test_docs_promise_the_scheme_the_code_serves(doc):
    """Every localhost:8765 URL a user is told to open must be https.

    If TLS ever becomes opt-in again, this fails and forces the docs to change
    with it rather than quietly becoming wrong.
    """
    text = (ROOT / doc).read_text(encoding="utf-8")
    urls = re.findall(r"https?://localhost:8765", text)
    assert urls, f"{doc} no longer tells the user where to connect"
    plaintext = [u for u in urls if u.startswith("http://")]
    assert not plaintext, f"{doc} points at {plaintext}, but SUNI serves TLS by default"


def test_container_persists_the_certificate():
    """Without a volume the cert is regenerated on every restart, so the browser
    is asked to trust a new certificate each time and users learn to click
    through warnings."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/app/certs" in compose, "certs are not persisted across restarts"

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # A named volume inherits the ownership of the image path it covers; if
    # /app/certs is not created and chowned first, it lands root-owned and the
    # non-root runtime user cannot write the certificate into it.
    mkdir = dockerfile.index("mkdir -p")
    assert "/app/certs" in dockerfile[mkdir:dockerfile.index("VOLUME")], \
        "/app/certs must be created and chowned before it is declared a volume"
