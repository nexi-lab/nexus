"""Unit tests for envelope encryption primitives (issue #3803)."""

from __future__ import annotations

import hashlib
import re

import pytest

from nexus.bricks.auth.envelope import (
    AADMismatch,
    AESGCMEnvelope,
    CiphertextCorrupted,
    DecryptionFailed,
    DEKCache,
    EnvelopeConfigurationError,
    EnvelopeError,
    WrappedDEKInvalid,
)
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider

# Regex: any base64 or hex blob of 16+ bytes shouldn't appear in error text.
# Real base64 of random secret bytes almost always contains a digit. Pure
# identifier names (class names, field names) don't. Requiring a digit in
# the base64 branch keeps the check sensitive to leaked secrets while
# avoiding false positives on long class names like "EnvelopeConfigurationError".
_BLOB_RE = re.compile(r"(?:(?=[A-Za-z0-9+/]*[0-9])[A-Za-z0-9+/]{22,}={0,2}|[0-9a-fA-F]{32,})")


class TestAESGCMEnvelope:
    def test_roundtrip(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x00" * 32
        plaintext = b"hello credential"
        aad = b"tenant|principal|id"
        nonce, ciphertext = env.encrypt(dek, plaintext, aad=aad)
        assert len(nonce) == 12
        assert ciphertext != plaintext
        assert env.decrypt(dek, nonce, ciphertext, aad=aad) == plaintext

    def test_wrong_aad_fails(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x01" * 32
        nonce, ct = env.encrypt(dek, b"secret", aad=b"aad-A")
        with pytest.raises(CiphertextCorrupted):
            env.decrypt(dek, nonce, ct, aad=b"aad-B")

    def test_ciphertext_tamper_fails(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x02" * 32
        nonce, ct = env.encrypt(dek, b"secret", aad=b"aad")
        tampered = bytes([ct[0] ^ 0x01]) + ct[1:]
        with pytest.raises(CiphertextCorrupted):
            env.decrypt(dek, nonce, tampered, aad=b"aad")

    def test_fresh_nonce_per_encrypt(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x03" * 32
        n1, _ = env.encrypt(dek, b"x", aad=b"aad")
        n2, _ = env.encrypt(dek, b"x", aad=b"aad")
        assert n1 != n2

    def test_dek_must_be_32_bytes(self) -> None:
        env = AESGCMEnvelope()
        with pytest.raises(ValueError):
            env.encrypt(b"\x00" * 16, b"x", aad=b"aad")


class TestErrorReprDiscipline:
    def test_all_errors_carry_context_not_secrets(self) -> None:
        import uuid

        tenant = uuid.uuid4()
        pid = "google/alice"
        for cls in (EnvelopeConfigurationError, DecryptionFailed, AADMismatch, WrappedDEKInvalid):
            err = cls.from_row(
                tenant_id=tenant, profile_id=pid, kek_version=7, cause="RuntimeError"
            )
            text = f"{err} || {err!r}"
            assert str(tenant) in text
            assert pid in text
            assert "7" in text
            assert "RuntimeError" in text
            assert _BLOB_RE.search(text) is None, f"{cls.__name__} repr leaked a blob: {text!r}"

    def test_envelope_error_root_is_catchable(self) -> None:
        import uuid

        with pytest.raises(EnvelopeError):
            raise DecryptionFailed.from_row(
                tenant_id=uuid.uuid4(), profile_id="x", kek_version=1, cause="y"
            )

    def test_regex_catches_actual_base64_secret(self) -> None:
        """Sanity check the blob-detection regex actually catches a real
        base64 of random bytes — we tightened it to avoid class-name false
        positives, make sure we didn't neuter it."""
        import base64
        import secrets

        fake_secret = base64.b64encode(secrets.token_bytes(24)).decode()
        # Should match (base64 of 24 random bytes is 32 chars, includes digits with
        # overwhelming probability).
        # Try up to 5 times in case we unluckily drew an all-letter base64.
        for _ in range(5):
            if _BLOB_RE.search(fake_secret):
                return
            fake_secret = base64.b64encode(secrets.token_bytes(24)).decode()
        pytest.fail("_BLOB_RE failed to match 5 consecutive real base64 secrets")


class TestDEKCache:
    def test_hit_after_put(self) -> None:
        cache = DEKCache(ttl_seconds=60, max_entries=8)
        key = cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=b"abcd")
        cache.put(key, b"\x00" * 32)
        assert cache.get(key) == b"\x00" * 32
        assert cache.hits == 1
        assert cache.misses == 0

    def test_miss_on_empty(self) -> None:
        cache = DEKCache(ttl_seconds=60, max_entries=8)
        key = cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=b"abcd")
        assert cache.get(key) is None
        assert cache.misses == 1
        assert cache.hits == 0

    def test_ttl_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1000.0]
        monkeypatch.setattr("nexus.bricks.auth.envelope._monotonic", lambda: now[0])
        cache = DEKCache(ttl_seconds=5, max_entries=8)
        key = cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=b"abcd")
        cache.put(key, b"\x11" * 32)
        now[0] += 4
        assert cache.get(key) == b"\x11" * 32
        now[0] += 2  # total 6s > ttl
        assert cache.get(key) is None

    def test_lru_eviction_on_size(self) -> None:
        cache = DEKCache(ttl_seconds=60, max_entries=2)
        k1 = cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=b"1")
        k2 = cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=b"2")
        k3 = cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=b"3")
        cache.put(k1, b"\x01" * 32)
        cache.put(k2, b"\x02" * 32)
        cache.get(k1)  # bump k1 to MRU
        cache.put(k3, b"\x03" * 32)  # evicts k2 (LRU)
        assert cache.get(k1) == b"\x01" * 32
        assert cache.get(k2) is None
        assert cache.get(k3) == b"\x03" * 32

    def test_key_uses_wrapped_dek_hash_not_bytes(self) -> None:
        cache = DEKCache(ttl_seconds=60, max_entries=8)
        key = cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=b"raw-wrapped-dek-bytes")
        # Repr of the key should not contain "raw-wrapped-dek-bytes"
        assert b"raw-wrapped-dek-bytes" not in repr(key).encode()
        expected_digest = hashlib.sha256(b"raw-wrapped-dek-bytes").hexdigest()
        assert expected_digest in repr(key)

    def test_concurrent_get_put_is_safe(self) -> None:
        """Under concurrent reads + writes, counters stay correct and the
        OrderedDict doesn't corrupt. Not a perf test — just a sanity check
        that the lock is in place."""
        import concurrent.futures
        import threading

        cache = DEKCache(ttl_seconds=60, max_entries=256)
        keys = [
            cache.make_key(tenant_id="t", kek_version=1, wrapped_dek=i.to_bytes(4, "big"))
            for i in range(32)
        ]
        dek = b"\x55" * 32
        for k in keys:
            cache.put(k, dek)

        barrier = threading.Barrier(8)

        def worker() -> int:
            barrier.wait()
            hits = 0
            for _ in range(200):
                for k in keys:
                    if cache.get(k) is not None:
                        hits += 1
                    cache.put(k, dek)
            return hits

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: worker(), range(8)))

        # All 8 workers each ran 200*32 = 6400 gets. All should have hit (keys
        # are repopulated before each get via the put loop). Exact counts
        # depend on interleaving but must be positive.
        assert sum(results) > 0
        # Under a race without a lock, counter increments would be lost and
        # cache.hits + cache.misses would be strictly less than the number
        # of get() calls. With a lock they're exactly equal.
        assert cache.hits + cache.misses == 8 * 200 * 32


class TestInMemoryEncryptionProvider:
    def test_roundtrip(self) -> None:
        prov = InMemoryEncryptionProvider()
        import uuid

        tid = uuid.uuid4()
        dek = b"\x22" * 32
        wrapped, version = prov.wrap_dek(dek, tenant_id=tid, aad=b"aad")
        assert version == 1
        assert wrapped != dek
        assert prov.unwrap_dek(wrapped, tenant_id=tid, aad=b"aad", kek_version=1) == dek

    def test_current_version_bumps_after_rotate(self) -> None:
        prov = InMemoryEncryptionProvider()
        import uuid

        tid = uuid.uuid4()
        assert prov.current_version(tenant_id=tid) == 1
        prov.rotate()
        assert prov.current_version(tenant_id=tid) == 2

    def test_wrap_at_v2_unwrap_at_v1_still_works(self) -> None:
        prov = InMemoryEncryptionProvider()
        import uuid

        tid = uuid.uuid4()
        dek = b"\x33" * 32
        wrapped_v1, v1 = prov.wrap_dek(dek, tenant_id=tid, aad=b"a")
        prov.rotate()
        wrapped_v2, v2 = prov.wrap_dek(dek, tenant_id=tid, aad=b"a")
        assert v1 == 1 and v2 == 2
        # Both readable
        assert prov.unwrap_dek(wrapped_v1, tenant_id=tid, aad=b"a", kek_version=v1) == dek
        assert prov.unwrap_dek(wrapped_v2, tenant_id=tid, aad=b"a", kek_version=v2) == dek

    def test_wrong_tenant_unwrap_fails(self) -> None:
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        prov = InMemoryEncryptionProvider()
        import uuid

        tid_a, tid_b = uuid.uuid4(), uuid.uuid4()
        dek = b"\x44" * 32
        wrapped, v = prov.wrap_dek(dek, tenant_id=tid_a, aad=b"a")
        with pytest.raises(WrappedDEKInvalid):
            prov.unwrap_dek(wrapped, tenant_id=tid_b, aad=b"a", kek_version=v)

    def test_wrong_aad_unwrap_fails(self) -> None:
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        prov = InMemoryEncryptionProvider()
        import uuid

        tid = uuid.uuid4()
        wrapped, v = prov.wrap_dek(b"\x55" * 32, tenant_id=tid, aad=b"aad-A")
        with pytest.raises(WrappedDEKInvalid):
            prov.unwrap_dek(wrapped, tenant_id=tid, aad=b"aad-B", kek_version=v)

    def test_counters(self) -> None:
        prov = InMemoryEncryptionProvider()
        import uuid

        tid = uuid.uuid4()
        w, v = prov.wrap_dek(b"\x66" * 32, tenant_id=tid, aad=b"a")
        prov.unwrap_dek(w, tenant_id=tid, aad=b"a", kek_version=v)
        prov.unwrap_dek(w, tenant_id=tid, aad=b"a", kek_version=v)
        assert prov.wrap_count == 1
        assert prov.unwrap_count == 2

    def test_kek_version_downgrade_unwrap_fails(self) -> None:
        """Unwrap a v1-wrapped DEK while claiming kek_version=2 must fail.

        Real providers reject this at the provider layer (Vault Transit:
        decrypt with wrong key_version fails; AWS KMS: kek_version is encoded
        in the opaque blob so the claim is ignored but downgrade can't succeed).
        The fake must match — otherwise contract tests pass against the fake
        and fail against real providers.
        """
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        prov = InMemoryEncryptionProvider()
        import uuid

        tid = uuid.uuid4()
        dek = b"\x77" * 32
        wrapped, v1 = prov.wrap_dek(dek, tenant_id=tid, aad=b"a")
        prov.rotate()  # current is now v2, v1 KEK still in _versions
        with pytest.raises(WrappedDEKInvalid):
            # Claim the row was wrapped at v2 even though it was v1
            prov.unwrap_dek(wrapped, tenant_id=tid, aad=b"a", kek_version=2)

    def test_unwrap_count_increments_on_failure(self) -> None:
        """Failed unwraps still count — models a real KMS round-trip that
        failed at the provider layer."""
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        prov = InMemoryEncryptionProvider()
        import uuid

        tid = uuid.uuid4()
        w, v = prov.wrap_dek(b"\x00" * 32, tenant_id=tid, aad=b"a")
        with pytest.raises(WrappedDEKInvalid):
            prov.unwrap_dek(w, tenant_id=uuid.uuid4(), aad=b"a", kek_version=v)
        assert prov.unwrap_count == 1


class TestMetricsImport:
    def test_all_metrics_defined(self) -> None:
        from nexus.bricks.auth import envelope_metrics as m

        for name in (
            "DEK_CACHE_HITS",
            "DEK_CACHE_MISSES",
            "DEK_UNWRAP_ERRORS",
            "DEK_UNWRAP_LATENCY",
            "KEK_ROTATE_ROWS",
        ):
            assert hasattr(m, name), f"missing metric: {name}"
