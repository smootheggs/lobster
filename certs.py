"""🐕 Sloth Lobster's own certificates, so every door of the app speaks HTTPS.

Why: on Android, any app can listen on 127.0.0.1 while Sloth Lobster is stopped and
pretend to be it. With HTTPS, an impostor can't prove it's Sloth Lobster (it doesn't
have the private key, which lives only in Termux's private storage), so the browser
refuses to connect, and your password and login never reach it. It also encrypts the
app on a hotspot, and makes the phones' app a "secure" page that can work offline.

How:
  - a private certificate authority (CA), made once with the openssl command
    (pkg install openssl-tool). You install its public certificate,
    "Sloth Lobster CA.crt", on each of your devices once.
  - the CA is name-constrained: it can only vouch for private addresses
    (127.x, 10.x, 172.16-31.x, 192.168.x, Tailscale's 100.64-127.x) and "localhost",
    never for a website like your bank, even if its key were stolen.
  - the server certificate lists the addresses the app currently listens on, and is
    re-issued automatically when they change (e.g. a new hotspot address).
"""
import hashlib
import ipaddress
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import DATA_DIR, WORK_DIR

TLS_DIR = DATA_DIR / "tls"
CA_KEY = TLS_DIR / "ca.key"
CA_CERT = TLS_DIR / "ca.crt"
EXPORT = WORK_DIR / "Sloth Lobster CA.crt"     # public: safe to copy to your devices
CA_DAYS = 3650
LEAF_DAYS = 397                                 # browsers reject longer-lived server certificates

PERMITTED = ("IP:127.0.0.0/255.0.0.0, IP:10.0.0.0/255.0.0.0, IP:172.16.0.0/255.240.0.0, "
             "IP:192.168.0.0/255.255.0.0, IP:100.64.0.0/255.192.0.0, DNS:localhost")


class CertError(Exception):
    pass


def available() -> bool:
    return shutil.which("openssl") is not None


def _run(*args: str) -> None:
    r = subprocess.run(["openssl", *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise CertError(f"openssl {args[0]} failed: {r.stderr.strip()[:300]}")


def _private(path: Path) -> None:
    path.chmod(0o600)


def ensure_ca() -> Path:
    """Create the CA once. Returns the path of its public certificate."""
    if not available():
        raise CertError("openssl isn't installed (pkg install openssl-tool)")
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    TLS_DIR.chmod(0o700)
    if not (CA_KEY.exists() and CA_CERT.exists()):
        _run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(CA_KEY))
        _private(CA_KEY)
        with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False, dir=TLS_DIR) as f:
            f.write("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=Sloth Lobster private CA\n"
                    "[ext]\nbasicConstraints=critical,CA:TRUE,pathlen:0\n"
                    "keyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n"
                    f"nameConstraints=critical,permitted;{PERMITTED.replace(', ', ',permitted;')}\n")
            cnf = f.name
        try:
            _run("req", "-x509", "-new", "-key", str(CA_KEY), "-sha256", "-days", str(CA_DAYS),
                 "-config", cnf, "-extensions", "ext", "-out", str(CA_CERT))
        finally:
            Path(cnf).unlink(missing_ok=True)
    export_ca()
    return CA_CERT


def export_ca() -> Path | None:
    """Put the CA's public certificate in the work folder (synced to Drive) for your devices."""
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        if not EXPORT.exists() or EXPORT.read_bytes() != CA_CERT.read_bytes():
            EXPORT.write_bytes(CA_CERT.read_bytes())
        return EXPORT
    except OSError:
        return None


def fingerprint() -> str:
    """SHA-256 fingerprint of the CA, to compare with what a device shows when installing it."""
    der = subprocess.run(["openssl", "x509", "-in", str(CA_CERT), "-outform", "DER"],
                         capture_output=True, timeout=30).stdout
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def allowed_address(ip: str) -> bool:
    """Only private addresses get certificates (and doors)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and (a.is_loopback or a.is_private or a in ipaddress.ip_network("100.64.0.0/10")) \
        and not (a.is_unspecified or a.is_link_local or a.is_multicast)


def leaf(addresses: list[str]) -> tuple[Path, Path]:
    """The server certificate for exactly these addresses, re-issued only when they change."""
    ensure_ca()
    ips = sorted({a for a in addresses if allowed_address(a)}, key=ipaddress.ip_address)
    if not ips:
        raise CertError("no private address to certify")
    tag = hashlib.sha256((",".join(ips)).encode()).hexdigest()[:12]
    key, cert = TLS_DIR / f"server-{tag}.key", TLS_DIR / f"server-{tag}.crt"
    if key.exists() and cert.exists():
        return cert, key
    csr = TLS_DIR / f"server-{tag}.csr"
    ext = TLS_DIR / f"server-{tag}.ext"
    _run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
    _private(key)
    _run("req", "-new", "-key", str(key), "-subj", "/CN=Sloth Lobster", "-out", str(csr))
    ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                   "extendedKeyUsage=serverAuth\nsubjectAltName=" +
                   ",".join([f"IP:{ip}" for ip in ips] + ["DNS:localhost"]) + "\n")
    _run("x509", "-req", "-in", str(csr), "-CA", str(CA_CERT), "-CAkey", str(CA_KEY), "-CAcreateserial",
         "-days", str(LEAF_DAYS), "-sha256", "-extfile", str(ext), "-out", str(cert))
    csr.unlink(missing_ok=True)
    ext.unlink(missing_ok=True)
    return cert, key


if __name__ == "__main__":
    ensure_ca()
    print(f"CA certificate: {EXPORT}\nFingerprint (SHA-256): {fingerprint()}")
