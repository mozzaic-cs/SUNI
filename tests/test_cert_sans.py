"""
The certificate has to cover the address SUNI is actually reached on.

`gen_cert.py` used to hardcode `localhost`, `suni.local` and `127.0.0.1`. That
works right up until somebody opens SUNI from another device — a phone, a laptop
on the LAN, a machine across a VPN — because they arrive by IP, and an IP absent
from the certificate is a name mismatch. Browsers present that as a connection
that cannot be established, which reads as the server being down.

It was read that way. On 2026-09-04 SUNI was reported down while it was three
days into an uninterrupted run, listening and answering: the reporter was
opening https://192.168.1.66:8765 over a VPN, and 192.168.1.66 was not a name
the certificate had ever heard of.

The tests run the generator as a subprocess in a temp directory. gen_cert.py
does its work at MODULE LEVEL, so importing it here would overwrite the running
instance's real certificate and key.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GEN = ROOT / "gen_cert.py"

cryptography = pytest.importorskip("cryptography")
from cryptography import x509                                    # noqa: E402
from cryptography.x509.oid import ExtensionOID                   # noqa: E402


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """Run the real generator somewhere harmless and read back what it made."""
    d = tmp_path_factory.mktemp("certgen")
    shutil.copy(GEN, d / "gen_cert.py")
    r = subprocess.run([sys.executable, str(d / "gen_cert.py")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"generator failed: {r.stderr[-800:]}"
    pem = (d / "certs" / "cert.pem").read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    san = cert.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    return {
        "cert": cert,
        "dns": set(san.get_values_for_type(x509.DNSName)),
        "ips": {str(i) for i in san.get_values_for_type(x509.IPAddress)},
        "stdout": r.stdout,
    }


def test_loopback_still_works(generated):
    """The local case must not regress while fixing the remote one."""
    assert "localhost" in generated["dns"]
    assert "127.0.0.1" in generated["ips"]


def test_it_covers_an_address_other_devices_can_reach(generated):
    """The whole point. A certificate naming only loopback is unusable from any
    machine except the one it runs on."""
    routable = {
        ip for ip in generated["ips"]
        if not ip.startswith(("127.", "::1", "fe80", "FE80"))
    }
    assert routable, (
        "the certificate covers only loopback — every remote client will get a "
        f"name mismatch. SANs were: {sorted(generated['ips'])}")


def test_it_covers_the_machines_own_hostname(generated):
    """People reach servers by name too, not only by address."""
    import socket
    host = socket.gethostname().lower()
    names = {n.lower() for n in generated["dns"]}
    assert host in names or f"{host}.localdomain" in names, \
        f"hostname {host!r} missing from {sorted(names)}"


def test_extra_hosts_can_be_supplied(tmp_path):
    """A NAT or VPN address the host itself never sees cannot be discovered, so
    there has to be a way to name it explicitly."""
    shutil.copy(GEN, tmp_path / "gen_cert.py")
    import os
    env = dict(os.environ, SUNI_CERT_HOSTS="suni.example.com,10.8.0.1")
    r = subprocess.run([sys.executable, str(tmp_path / "gen_cert.py")],
                       capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stderr[-800:]

    cert = x509.load_pem_x509_certificate((tmp_path / "certs" / "cert.pem").read_bytes())
    san = cert.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert "suni.example.com" in set(san.get_values_for_type(x509.DNSName))
    assert "10.8.0.1" in {str(i) for i in san.get_values_for_type(x509.IPAddress)}


def test_nothing_is_hardcoded_to_one_machines_addresses(generated):
    """The old version listed its SANs literally. Discovery is what makes the
    certificate correct on a machine that is not this one."""
    src = GEN.read_text(encoding="utf-8")
    assert "gethostname" in src and "getaddrinfo" in src, \
        "the generator no longer discovers the host's own addresses"


def test_the_generator_says_what_it_covered(generated):
    """An operator has to be able to see whether their address made it in
    without reaching for openssl."""
    assert "Addresses:" in generated["stdout"]
    assert "Names:" in generated["stdout"]
