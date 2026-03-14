"""X.509 certificate generation for Nexus zone federation.

Uses EC P-256 (ECDSA) for maximum gRPC/OpenSSL/BoringSSL compatibility.
Ed25519 X.509 has spotty gRPC support across language stacks.

Typical usage::

    ca_cert, ca_key = generate_zone_ca("my-zone")
    save_pem(tls_dir / "ca.pem", ca_cert)
    save_pem(tls_dir / "ca-key.pem", ca_key, is_private=True)

    node_cert, node_key = generate_node_cert(1, "my-zone", ca_cert, ca_key)
    save_pem(tls_dir / "node.pem", node_cert)
    save_pem(tls_dir / "node-key.pem", node_key, is_private=True)

    print(cert_fingerprint(ca_cert))  # SHA256:abc...
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def generate_zone_ca(
    zone_id: str,
    validity_days: int = 3650,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """Generate a self-signed CA certificate for a Nexus zone.

    Returns:
        (ca_cert, ca_private_key)
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Nexus"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"nexus-zone-{zone_id}-ca"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


def generate_node_cert(
    node_id: int,
    zone_id: str,
    ca_cert: x509.Certificate,
    ca_key: ec.EllipticCurvePrivateKey,
    hostnames: list[str] | None = None,
    validity_days: int = 365,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """Generate a node certificate signed by the zone CA.

    The certificate includes both serverAuth and clientAuth extended key
    usage so it can be used for mTLS (both directions).

    Returns:
        (node_cert, node_private_key)
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Nexus"),
            x509.NameAttribute(NameOID.COMMON_NAME, f"nexus-zone-{zone_id}-node-{node_id}"),
        ]
    )

    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv6Address("::1")),
    ]
    for h in hostnames or []:
        san_entries.append(x509.DNSName(h))

    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.SERVER_AUTH,
                    ExtendedKeyUsageOID.CLIENT_AUTH,
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def cert_fingerprint(cert: x509.Certificate) -> str:
    """SSH-style fingerprint: ``SHA256:<base64>``."""
    digest = cert.fingerprint(hashes.SHA256())
    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


def save_pem(
    path: Path, obj: x509.Certificate | ec.EllipticCurvePrivateKey, *, is_private: bool = False
) -> None:
    """Write a certificate or key as PEM.  Private keys get ``chmod 0600``."""
    if is_private:
        assert isinstance(obj, ec.EllipticCurvePrivateKey)
        pem = obj.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    else:
        assert isinstance(obj, x509.Certificate)
        pem = obj.public_bytes(serialization.Encoding.PEM)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_private:
        from nexus.security.secret_file import write_secret_file

        write_secret_file(path, pem)
    else:
        path.write_bytes(pem)


def load_pem_cert(path: Path) -> x509.Certificate:
    """Load a PEM-encoded X.509 certificate."""
    return x509.load_pem_x509_certificate(path.read_bytes())


def load_pem_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """Load a PEM-encoded EC private key."""
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError(f"Expected EC private key, got {type(key).__name__}")
    return key
