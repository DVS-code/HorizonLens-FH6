from __future__ import annotations

import datetime as dt
import ipaddress
from pathlib import Path

from . import config


def ensure_cert() -> tuple[Path, Path]:
    d = config.data_dir() / "tls"
    d.mkdir(parents=True, exist_ok=True)
    cert_p, key_p = d / "cert.pem", d / "key.pem"
    ips = set(config.lan_ipv4s()) | {"127.0.0.1"}
    if cert_p.is_file() and key_p.is_file():
        try:
            from cryptography.x509 import (
                IPAddress,
                SubjectAlternativeName,
                load_pem_x509_certificate,
            )
            c = load_pem_x509_certificate(cert_p.read_bytes())
            san = c.extensions.get_extension_for_class(SubjectAlternativeName).value
            have = {str(x) for x in san.get_values_for_type(IPAddress)}
            fresh = c.not_valid_after_utc > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)
            if ips <= have and fresh:
                return cert_p, key_p
        except Exception:
            pass

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "HorizonLens")])
    san = x509.SubjectAlternativeName(
        [x509.DNSName("localhost")]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in sorted(ips)]
    )
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_p.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_p, key_p


def cert_fingerprint() -> str | None:
    import hashlib
    import ssl
    try:
        pem = (config.data_dir() / "tls" / "cert.pem").read_text(encoding="ascii")
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    except (OSError, ValueError):
        return None
