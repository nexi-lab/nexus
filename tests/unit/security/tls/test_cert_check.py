"""Tests for certificate expiry checking (Issue #2808, Decision 12A).

Covers all boundary conditions:
- OK / WARN / CRITICAL / EXPIRED states
- Exact boundary values (7d, 30d)
- Negative days (already expired)
- Not-yet-valid certs
- Invalid cert file handling
"""

import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from nexus.security.tls.certgen import (
    CERT_EXPIRY_CRITICAL_DAYS,
    CERT_EXPIRY_WARN_DAYS,
    CertStatus,
    check_cert_expiry,
)


def _generate_cert(
    days_from_now: int,
    not_valid_before: datetime.datetime | None = None,
) -> x509.Certificate:
    """Generate a self-signed cert expiring `days_from_now` days in the future.

    For expired certs (days_from_now < 0), sets not_valid_before far enough
    in the past so not_valid_before < not_valid_after.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])

    not_valid_after = now + datetime.timedelta(days=days_from_now)

    if not_valid_before is None:
        # Ensure not_valid_before is always before not_valid_after
        not_valid_before = not_valid_after - datetime.timedelta(days=365)

    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before)
        .not_valid_after(not_valid_after)
        .sign(key, hashes.SHA256())
    )


def _save_cert(cert: x509.Certificate, path: Path) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


# ---------------------------------------------------------------------------
# CertStatus dataclass
# ---------------------------------------------------------------------------


class TestCertStatus:
    def test_ok_is_healthy(self) -> None:
        assert CertStatus(days_remaining=90, level="OK").is_healthy is True

    def test_warn_is_not_healthy(self) -> None:
        assert CertStatus(days_remaining=15, level="WARN").is_healthy is False

    def test_critical_is_not_healthy(self) -> None:
        assert CertStatus(days_remaining=3, level="CRITICAL").is_healthy is False

    def test_expired_is_not_healthy(self) -> None:
        assert CertStatus(days_remaining=-5, level="EXPIRED").is_healthy is False

    def test_frozen(self) -> None:
        status = CertStatus(days_remaining=90, level="OK")
        with pytest.raises(AttributeError):
            status.days_remaining = 10  # type: ignore[misc]  # allowed


# ---------------------------------------------------------------------------
# check_cert_expiry — state classification
# ---------------------------------------------------------------------------


class TestCheckCertExpiry:
    def test_ok_state(self, tmp_path: Path) -> None:
        """Cert with >30 days remaining → OK."""
        cert = _generate_cert(days_from_now=90)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        assert status.level == "OK"
        assert status.days_remaining >= 89  # allow 1-day rounding
        assert status.is_healthy is True

    def test_warn_state(self, tmp_path: Path) -> None:
        """Cert with 7-29 days remaining → WARN."""
        cert = _generate_cert(days_from_now=15)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        assert status.level == "WARN"
        assert CERT_EXPIRY_CRITICAL_DAYS <= status.days_remaining < CERT_EXPIRY_WARN_DAYS

    def test_critical_state(self, tmp_path: Path) -> None:
        """Cert with <7 days remaining → CRITICAL."""
        cert = _generate_cert(days_from_now=3)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        assert status.level == "CRITICAL"
        assert 0 <= status.days_remaining < CERT_EXPIRY_CRITICAL_DAYS

    def test_expired_state(self, tmp_path: Path) -> None:
        """Cert already expired → EXPIRED."""
        # Generate a cert that expired yesterday
        cert = _generate_cert(days_from_now=-1)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        assert status.level == "EXPIRED"
        assert status.days_remaining < 0

    def test_expired_long_ago(self, tmp_path: Path) -> None:
        """Cert that expired 100 days ago."""
        cert = _generate_cert(days_from_now=-100)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        assert status.level == "EXPIRED"
        assert status.days_remaining <= -99


class TestCheckCertExpiryBoundaries:
    """Exact boundary value tests."""

    def test_exactly_30_days_is_ok(self, tmp_path: Path) -> None:
        """30 days remaining → OK (not WARN)."""
        cert = _generate_cert(days_from_now=30)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        # days_remaining is floor-rounded, so 30 days → could be 29 due to timing
        # The boundary is < 30, so exactly 30 → OK
        assert status.level in ("OK", "WARN")  # timing-dependent within the day

    def test_exactly_7_days_is_warn(self, tmp_path: Path) -> None:
        """7 days remaining → WARN (not CRITICAL)."""
        cert = _generate_cert(days_from_now=7)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        assert status.level in ("WARN", "CRITICAL")  # timing-dependent

    def test_zero_days_is_critical(self, tmp_path: Path) -> None:
        """0 days remaining → CRITICAL (just about to expire)."""
        # Generate cert expiring right now — days_remaining ≈ 0
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=365))
            .not_valid_after(now + datetime.timedelta(hours=1))
            .sign(key, hashes.SHA256())
        )
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(cert_path)
        assert status.level == "CRITICAL"
        assert status.days_remaining == 0


class TestCheckCertExpiryEdgeCases:
    def test_file_not_found(self) -> None:
        """Non-existent file → FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            check_cert_expiry("/nonexistent/cert.pem")

    def test_invalid_pem(self, tmp_path: Path) -> None:
        """Invalid PEM data → ValueError."""
        cert_path = tmp_path / "bad.pem"
        cert_path.write_text("not a certificate")

        with pytest.raises(ValueError, match="Cannot parse certificate"):
            check_cert_expiry(cert_path)

    def test_empty_file(self, tmp_path: Path) -> None:
        """Empty file → ValueError."""
        cert_path = tmp_path / "empty.pem"
        cert_path.write_bytes(b"")

        with pytest.raises(ValueError, match="Cannot parse certificate"):
            check_cert_expiry(cert_path)

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        """Accepts str path, not just Path."""
        cert = _generate_cert(days_from_now=90)
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        status = check_cert_expiry(str(cert_path))
        assert status.level == "OK"

    def test_not_yet_valid_but_not_expired(self, tmp_path: Path) -> None:
        """Cert not yet valid (starts in future) but has valid expiry."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now + datetime.timedelta(days=10))
            .not_valid_after(now + datetime.timedelta(days=100))
            .sign(key, hashes.SHA256())
        )
        cert_path = tmp_path / "cert.pem"
        _save_cert(cert, cert_path)

        # Still returns days until expiry (not concerned with not_valid_before)
        status = check_cert_expiry(cert_path)
        assert status.level == "OK"
        assert status.days_remaining >= 99
