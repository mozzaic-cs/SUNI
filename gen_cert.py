"""Generate a self-signed TLS cert for SUNI (10-year expiry).

The certificate has to cover every address SUNI is actually reached on, not just
the one the machine calls itself.

This used to name only `localhost`, `suni.local` and `127.0.0.1`. That is fine
until somebody opens SUNI from another device — over a VPN, from a phone, from a
laptop on the same LAN — because they reach it by IP, and an IP that is not in
the certificate is a name mismatch. Browsers report that as a connection that
cannot be established, which reads as "the server is down" even though it is
running perfectly and answering. It happened: SUNI was reported down while it
was three days into an uninterrupted run, because `https://192.168.1.66:8765`
was not something its certificate had ever heard of.

So the SAN list is built from the host rather than hardcoded: the loopback
names, the machine's own hostname, and every non-loopback IPv4/IPv6 address it
currently holds. Set SUNI_CERT_HOSTS to add anything this cannot discover — a
DNS name, or the address SUNI is reached on through a NAT or VPN that the host
itself never sees:

    SUNI_CERT_HOSTS=suni.example.com,10.8.0.1 python gen_cert.py

It stays SELF-SIGNED, so a browser still warns once about an unknown issuer and
somebody has to accept it. That is a different warning from a name mismatch, and
it is one the browser lets you get past — the name mismatch on a modern browser
often cannot be dismissed at all.

Addresses change. Re-run this after the machine's IP changes, and restart SUNI:
web.py only generates a certificate when none exists, so an install keeps its
first one forever unless someone replaces it deliberately.
"""
import datetime, ipaddress, os, socket
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

OUT = Path(__file__).parent / "certs"
OUT.mkdir(exist_ok=True)


def _local_addresses() -> tuple[set[str], set[str]]:
    """Every name and address this host answers to. Best effort, never fatal."""
    names: set[str] = {"localhost", "suni.local"}
    addrs: set[str] = {"127.0.0.1", "::1"}

    try:
        host = socket.gethostname()
        if host:
            names.add(host)
            names.add(host.lower())
            fqdn = socket.getfqdn()
            if fqdn and fqdn != host:
                names.add(fqdn.lower())
    except Exception:
        pass

    # Every address the host currently holds. getaddrinfo on the hostname misses
    # interfaces on some setups, so ask for both families explicitly and keep
    # whatever comes back.
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, family):
                addrs.add(info[4][0].split("%")[0])   # strip any zone index
        except Exception:
            pass

    # A UDP "connect" to an off-host address picks the outbound interface
    # without sending a packet -- this is what finds the LAN address that a
    # remote client will actually use.
    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            addrs.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass

    for extra in (os.environ.get("SUNI_CERT_HOSTS") or "").split(","):
        extra = extra.strip()
        if not extra:
            continue
        try:
            ipaddress.ip_address(extra)
            addrs.add(extra)
        except ValueError:
            names.add(extra)

    return names, addrs


names, addrs = _local_addresses()

san: list[x509.GeneralName] = []
for n in sorted(names):
    try:
        san.append(x509.DNSName(n))
    except Exception:
        pass
valid_ips: list[str] = []
for a in sorted(addrs):
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(a)))
        valid_ips.append(a)
    except Exception:
        pass

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COMMON_NAME, "suni.local"),
])

now = datetime.datetime.now(datetime.timezone.utc)
cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now)
    .not_valid_after(now + datetime.timedelta(days=3650))
    .add_extension(x509.SubjectAlternativeName(san), critical=False)
    .sign(key, hashes.SHA256())
)

(OUT / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
(OUT / "key.pem").write_bytes(key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
))

print("Generated:")
print(f"  {OUT / 'cert.pem'}")
print(f"  {OUT / 'key.pem'}")
print(f"  Names:     {', '.join(sorted(names))}")
print(f"  Addresses: {', '.join(valid_ips)}")
print("  Valid for 10 years.")
print()
print("  Restart SUNI to serve it. Browsers that accepted the OLD certificate")
print("  will ask again -- the key changed, which is the point.")
