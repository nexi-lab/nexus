# Envelope encryption for `PostgresAuthProfileStore` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-side envelope encryption rails to `PostgresAuthProfileStore` so rows can carry resolved credentials alongside the routing metadata they already carry. Ciphertext is opt-in per call through a new sub-protocol; PR 1 rows stay readable.

**Architecture:** New `EncryptionProvider` trait with AES-256-GCM DEK per row wrapped by a KEK held by Vault Transit or AWS KMS. AAD binds `tenant_id|principal_id|profile_id`. `CredentialCarryingProfileStore(AuthProfileStore)` sub-protocol adds `upsert_with_credential` / `get_with_credential` on the Postgres store; SQLite/InMemory stores untouched. CLI-driven `rotate_kek_for_tenant` admin helper sweeps old `kek_version` rows in `SKIP LOCKED` batches. DEK cache amortizes KMS round-trips on hot reads.

**Tech Stack:** Python 3.11+, `cryptography` (already in repo, used by `auth/oauth/crypto.py`), SQLAlchemy 2.x, Click for CLI, `prometheus_client` (already in repo), pytest + `@pytest.mark.postgres` + `xdist_group`. Optional deps (opt-in providers): `hvac` (Vault), `boto3` (AWS KMS) — lazy-imported at module level.

**Spec:** `docs/superpowers/specs/2026-04-18-issue-3803-envelope-encryption-design.md`

---

## File Structure

### New files

- `src/nexus/bricks/auth/envelope.py` — `EncryptionProvider` Protocol, `AESGCMEnvelope`, `DEKCache`, `EnvelopeError` hierarchy.
- `src/nexus/bricks/auth/envelope_metrics.py` — Prometheus counters/histograms.
- `src/nexus/bricks/auth/envelope_providers/__init__.py` — empty package marker.
- `src/nexus/bricks/auth/envelope_providers/in_memory.py` — `InMemoryEncryptionProvider` test fake (real AEAD, call counters).
- `src/nexus/bricks/auth/envelope_providers/vault_transit.py` — `VaultTransitProvider` (lazy `hvac`).
- `src/nexus/bricks/auth/envelope_providers/aws_kms.py` — `AwsKmsProvider` (lazy `boto3`).
- `src/nexus/bricks/auth/tests/test_envelope.py` — unit tests for primitives.
- `src/nexus/bricks/auth/tests/test_envelope_contract.py` — parametrized contract suite.
- `src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py` — acceptance tests (postgres-gated).
- `src/nexus/bricks/auth/tests/test_rotate_kek_cli.py` — rotation CLI tests (postgres-gated).
- `src/nexus/bricks/auth/tests/test_envelope_providers_vault.py` — `@pytest.mark.vault` integration.
- `src/nexus/bricks/auth/tests/test_envelope_providers_aws_kms.py` — `@pytest.mark.kms` integration.
- `docs/guides/auth-envelope-encryption.md` — deployment guidance (Vault vs KMS).

### Modified files

- `src/nexus/bricks/auth/profile.py` — add `CredentialCarryingProfileStore` Protocol. Import `ResolvedCredential` already present.
- `src/nexus/bricks/auth/postgres_profile_store.py` — schema delta (5 new nullable columns + CHECK), `_upgrade_shape_in_place` additions, new `upsert_with_credential`/`get_with_credential` methods, ctor accepts `encryption_provider`, module-level `rotate_kek_for_tenant` helper + `RotationReport` dataclass.
- `src/nexus/bricks/auth/cli_commands.py` — new `rotate-kek` subcommand.
- `pyproject.toml` — add `vault` and `kms` optional-dep groups for `hvac` and `boto3` if not already present (verify first).

---

## Task ordering principle

TDD end-to-end: each task writes a failing test, verifies failure, implements, verifies pass, commits. Tasks are ordered bottom-up (primitives → providers → store integration → CLI) so every step has dependencies already in place.

---

### Task 1: `AESGCMEnvelope` primitive

**Files:**
- Create: `src/nexus/bricks/auth/envelope.py`
- Test: `src/nexus/bricks/auth/tests/test_envelope.py`

- [ ] **Step 1: Write the failing test**

Create `src/nexus/bricks/auth/tests/test_envelope.py`:

```python
"""Unit tests for envelope encryption primitives (issue #3803)."""

from __future__ import annotations

import pytest

from nexus.bricks.auth.envelope import AESGCMEnvelope, CiphertextCorrupted


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py -v
```

Expected: collection error / `ModuleNotFoundError: No module named 'nexus.bricks.auth.envelope'`.

- [ ] **Step 3: Implement `envelope.py` with `AESGCMEnvelope` + error types**

Create `src/nexus/bricks/auth/envelope.py`:

```python
"""Envelope encryption primitives for PostgresAuthProfileStore (issue #3803).

Provides:
  - AESGCMEnvelope: AES-256-GCM wrapper for per-row ciphertext + DEK.
  - EncryptionProvider: Protocol for KEK-level DEK wrap/unwrap (impls land in
    envelope_providers/).
  - DEKCache: in-process TTL+LRU cache of unwrapped DEKs.
  - EnvelopeError hierarchy with no-plaintext repr discipline.

Design: docs/superpowers/specs/2026-04-18-issue-3803-envelope-encryption-design.md
"""

from __future__ import annotations

import secrets
import uuid
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Error hierarchy — no plaintext, wrapped-DEK, or ciphertext bytes in str/repr
# ---------------------------------------------------------------------------


class EnvelopeError(Exception):
    """Root of every error raised by the envelope subsystem."""


class EnvelopeConfigurationError(EnvelopeError):
    """Provider is misconfigured (e.g. Vault transit key not derived=true)."""


class DecryptionFailed(EnvelopeError):
    """Generic decrypt failure. Concrete subclasses narrow the reason."""


class AADMismatch(DecryptionFailed):
    """Stored AAD column does not match the expected tenant|principal|id."""


class WrappedDEKInvalid(DecryptionFailed):
    """Provider refused to unwrap the DEK (IAM, mismatched context, corrupt)."""


class CiphertextCorrupted(DecryptionFailed):
    """AES-GCM tag verification failed — ciphertext/nonce/AAD tampered."""


# ---------------------------------------------------------------------------
# AES-256-GCM primitive
# ---------------------------------------------------------------------------


class AESGCMEnvelope:
    """Thin wrapper over `cryptography`'s AESGCM with our invariants baked in.

    - DEK is 32 bytes (AES-256).
    - Nonce is 12 bytes, freshly generated per ``encrypt`` call.
    - AAD is bound into the AEAD tag; tamper raises ``CiphertextCorrupted``.
    """

    NONCE_LEN = 12
    DEK_LEN = 32

    def encrypt(self, dek: bytes, plaintext: bytes, *, aad: bytes) -> tuple[bytes, bytes]:
        if len(dek) != self.DEK_LEN:
            raise ValueError(f"DEK must be {self.DEK_LEN} bytes, got {len(dek)}")
        nonce = secrets.token_bytes(self.NONCE_LEN)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        return nonce, ciphertext

    def decrypt(self, dek: bytes, nonce: bytes, ciphertext: bytes, *, aad: bytes) -> bytes:
        if len(dek) != self.DEK_LEN:
            raise ValueError(f"DEK must be {self.DEK_LEN} bytes, got {len(dek)}")
        try:
            return AESGCM(dek).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise CiphertextCorrupted("AES-GCM tag verification failed") from exc


# ---------------------------------------------------------------------------
# EncryptionProvider Protocol (impls in envelope_providers/)
# ---------------------------------------------------------------------------


@runtime_checkable
class EncryptionProvider(Protocol):
    """KEK-level DEK wrap/unwrap.

    ``wrap_dek`` always uses the provider's current version and returns it
    alongside the wrapped bytes. ``unwrap_dek`` takes the stored ``kek_version``
    because rows persist history — Vault Transit takes ``key_version`` on
    decrypt natively; AWS KMS ignores it (the ciphertext blob embeds the key
    version internally) so ``kek_version`` is bookkeeping-only there.
    """

    def current_version(self, *, tenant_id: uuid.UUID) -> int: ...

    def wrap_dek(
        self,
        dek: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
    ) -> tuple[bytes, int]:
        """Return ``(wrapped_bytes, kek_version)``."""
        ...

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
        kek_version: int,
    ) -> bytes: ...
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/envelope.py src/nexus/bricks/auth/tests/test_envelope.py
git commit -m "feat(auth): AESGCMEnvelope primitive + EnvelopeError hierarchy (#3803)"
```

---

### Task 2: Error `repr`/`str` no-plaintext guarantee

**Files:**
- Modify: `src/nexus/bricks/auth/envelope.py` (add `__str__` / `__repr__` to errors)
- Test: `src/nexus/bricks/auth/tests/test_envelope.py` (append tests)

- [ ] **Step 1: Append failing tests**

Append to `src/nexus/bricks/auth/tests/test_envelope.py`:

```python
import re

from nexus.bricks.auth.envelope import (
    AADMismatch,
    DecryptionFailed,
    EnvelopeConfigurationError,
    EnvelopeError,
    WrappedDEKInvalid,
)

# Regex: any base64 or hex blob of 16+ bytes shouldn't appear in error text.
_BLOB_RE = re.compile(r"(?:[A-Za-z0-9+/]{22,}={0,2}|[0-9a-fA-F]{32,})")


class TestErrorReprDiscipline:
    def test_all_errors_carry_context_not_secrets(self) -> None:
        import uuid

        tenant = uuid.uuid4()
        pid = "google/alice"
        for cls in (EnvelopeConfigurationError, DecryptionFailed, AADMismatch, WrappedDEKInvalid):
            err = cls.from_row(tenant_id=tenant, profile_id=pid, kek_version=7, cause="RuntimeError")
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py::TestErrorReprDiscipline -v
```

Expected: FAIL — `AttributeError: type object 'EnvelopeConfigurationError' has no attribute 'from_row'`.

- [ ] **Step 3: Add `from_row` classmethod + `__str__`/`__repr__` to every error**

Edit `src/nexus/bricks/auth/envelope.py` — replace the error-class block with:

```python
import uuid


class EnvelopeError(Exception):
    """Root of every error raised by the envelope subsystem.

    All subclasses expose ``from_row(tenant_id, profile_id, kek_version, cause)``
    so call sites never build error messages inline — that discipline is what
    keeps plaintext / wrapped-DEK bytes out of ``__str__`` / ``__repr__``.
    """

    def __init__(
        self,
        message: str,
        *,
        tenant_id: uuid.UUID | None = None,
        profile_id: str | None = None,
        kek_version: int | None = None,
        cause: str | None = None,
    ) -> None:
        super().__init__(message)
        self._message = message
        self.tenant_id = tenant_id
        self.profile_id = profile_id
        self.kek_version = kek_version
        self.cause = cause

    @classmethod
    def from_row(
        cls,
        *,
        tenant_id: uuid.UUID,
        profile_id: str,
        kek_version: int,
        cause: str,
    ) -> "EnvelopeError":
        return cls(
            f"{cls.__name__} tenant={tenant_id} profile={profile_id} "
            f"kek_version={kek_version} cause={cause}",
            tenant_id=tenant_id,
            profile_id=profile_id,
            kek_version=kek_version,
            cause=cause,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(tenant_id={self.tenant_id!s}, "
            f"profile_id={self.profile_id!r}, kek_version={self.kek_version!r}, "
            f"cause={self.cause!r})"
        )


class EnvelopeConfigurationError(EnvelopeError):
    pass


class DecryptionFailed(EnvelopeError):
    pass


class AADMismatch(DecryptionFailed):
    pass


class WrappedDEKInvalid(DecryptionFailed):
    pass


class CiphertextCorrupted(DecryptionFailed):
    pass
```

Also update `AESGCMEnvelope.decrypt`'s raise site:

```python
        except InvalidTag as exc:
            raise CiphertextCorrupted(
                "AES-GCM tag verification failed",
                cause="InvalidTag",
            ) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py -v
```

Expected: all tests pass (5 from Task 1 + 2 new).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/envelope.py src/nexus/bricks/auth/tests/test_envelope.py
git commit -m "feat(auth): EnvelopeError no-plaintext repr discipline (#3803)"
```

---

### Task 3: `DEKCache` with TTL + LRU + metrics hooks

**Files:**
- Modify: `src/nexus/bricks/auth/envelope.py` (append `DEKCache`)
- Test: `src/nexus/bricks/auth/tests/test_envelope.py` (append tests)

- [ ] **Step 1: Append failing tests**

```python
import hashlib
import time

from nexus.bricks.auth.envelope import DEKCache


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py::TestDEKCache -v
```

Expected: FAIL — `ImportError: cannot import name 'DEKCache'`.

- [ ] **Step 3: Append `DEKCache` to `envelope.py`**

At the top, add:

```python
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass


def _monotonic() -> float:
    """Indirection so tests can monkeypatch the clock."""
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class DEKCacheKey:
    tenant_id: str
    kek_version: int
    wrapped_dek_sha256: str

    def __repr__(self) -> str:
        return (
            f"DEKCacheKey(tenant={self.tenant_id}, v={self.kek_version}, "
            f"sha256={self.wrapped_dek_sha256})"
        )


class DEKCache:
    """TTL + LRU cache for unwrapped DEKs.

    Keyed by ``(tenant_id, kek_version, sha256(wrapped_dek))`` — the hash, not
    the wrapped bytes, so cache-key logging is safe. Does not cache negative
    results: a KMS/Vault blip shouldn't pin decrypt-failed for the TTL window.
    """

    def __init__(self, *, ttl_seconds: int = 300, max_entries: int = 1024) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: OrderedDict[DEKCacheKey, tuple[float, bytes]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(
        *, tenant_id: str | uuid.UUID, kek_version: int, wrapped_dek: bytes
    ) -> DEKCacheKey:
        return DEKCacheKey(
            tenant_id=str(tenant_id),
            kek_version=kek_version,
            wrapped_dek_sha256=hashlib.sha256(wrapped_dek).hexdigest(),
        )

    def get(self, key: DEKCacheKey) -> bytes | None:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, dek = entry
        if _monotonic() >= expires_at:
            self._store.pop(key, None)
            self.misses += 1
            return None
        self._store.move_to_end(key)  # mark MRU
        self.hits += 1
        return dek

    def put(self, key: DEKCacheKey, dek: bytes) -> None:
        self._store[key] = (_monotonic() + self._ttl, dek)
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)  # evict LRU
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/envelope.py src/nexus/bricks/auth/tests/test_envelope.py
git commit -m "feat(auth): DEKCache with TTL + LRU (#3803)"
```

---

### Task 4: `InMemoryEncryptionProvider` test fake + package init

**Files:**
- Create: `src/nexus/bricks/auth/envelope_providers/__init__.py`
- Create: `src/nexus/bricks/auth/envelope_providers/in_memory.py`
- Test: `src/nexus/bricks/auth/tests/test_envelope.py` (append)

- [ ] **Step 1: Append failing tests**

```python
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider


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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py::TestInMemoryEncryptionProvider -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.auth.envelope_providers'`.

- [ ] **Step 3: Create the package and the in-memory provider**

Create `src/nexus/bricks/auth/envelope_providers/__init__.py`:

```python
"""Concrete EncryptionProvider implementations (issue #3803).

- in_memory.InMemoryEncryptionProvider: test fake + default for development.
- vault_transit.VaultTransitProvider: Vault Transit (``derived=true``).
- aws_kms.AwsKmsProvider: AWS KMS per-tenant CMK.
"""
```

Create `src/nexus/bricks/auth/envelope_providers/in_memory.py`:

```python
"""In-process fake EncryptionProvider for tests + development.

Uses real AES-256-GCM so the wrapped bytes are actual ciphertext (not
identity). Holds a dict of KEKs keyed by version. ``rotate()`` bumps the
current version and mints a new KEK. Exposes ``wrap_count`` /
``unwrap_count`` so contract tests can assert cache amortization.

Never use in production: keys live in process memory and die with it.
"""

from __future__ import annotations

import secrets
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nexus.bricks.auth.envelope import (
    AESGCMEnvelope,
    EncryptionProvider,
    WrappedDEKInvalid,
)


class InMemoryEncryptionProvider(EncryptionProvider):
    """Fake provider with real AEAD under the hood.

    Tenant scoping is modelled by mixing ``tenant_id`` into the AAD that wraps
    the DEK — unwrap with the wrong ``tenant_id`` fails the AES-GCM tag, so
    cross-tenant DEK reuse is rejected just like the real providers reject
    mismatched encryption-context / derivation-context.
    """

    def __init__(self) -> None:
        self._versions: dict[int, bytes] = {1: secrets.token_bytes(32)}
        self._current_version = 1
        self._nonce_len = 12
        self.wrap_count = 0
        self.unwrap_count = 0

    def rotate(self) -> None:
        """Bump current_version and mint a new KEK (keep old versions readable)."""
        self._current_version += 1
        self._versions[self._current_version] = secrets.token_bytes(32)

    def current_version(self, *, tenant_id: uuid.UUID) -> int:
        return self._current_version

    def _context_aad(self, tenant_id: uuid.UUID, aad: bytes) -> bytes:
        return f"v=inmem|tenant={tenant_id}|".encode() + aad

    def wrap_dek(
        self, dek: bytes, *, tenant_id: uuid.UUID, aad: bytes
    ) -> tuple[bytes, int]:
        self.wrap_count += 1
        version = self._current_version
        kek = self._versions[version]
        nonce = secrets.token_bytes(self._nonce_len)
        ct = AESGCM(kek).encrypt(nonce, dek, self._context_aad(tenant_id, aad))
        return nonce + ct, version

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
        kek_version: int,
    ) -> bytes:
        self.unwrap_count += 1
        if kek_version not in self._versions:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause="kek_version not known to InMemoryEncryptionProvider",
            )
        kek = self._versions[kek_version]
        nonce, ct = wrapped[: self._nonce_len], wrapped[self._nonce_len :]
        try:
            return AESGCM(kek).decrypt(nonce, ct, self._context_aad(tenant_id, aad))
        except InvalidTag as exc:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause="AES-GCM tag mismatch (wrong tenant_id/aad/kek_version)",
            ) from exc


__all__ = ["InMemoryEncryptionProvider"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/envelope_providers/ src/nexus/bricks/auth/tests/test_envelope.py
git commit -m "feat(auth): InMemoryEncryptionProvider test fake (#3803)"
```

---

### Task 5: Shared contract test suite

**Files:**
- Create: `src/nexus/bricks/auth/tests/test_envelope_contract.py`

- [ ] **Step 1: Write the contract tests**

Create `src/nexus/bricks/auth/tests/test_envelope_contract.py`:

```python
"""Shared contract tests every EncryptionProvider must pass (issue #3803).

Parametrized against InMemoryEncryptionProvider by default. Provider-specific
test modules (Vault, AWS KMS) import these and re-parametrize with their own
``provider_factory`` fixtures gated by ``@pytest.mark.vault`` / ``@pytest.mark.kms``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from nexus.bricks.auth.envelope import EncryptionProvider, WrappedDEKInvalid
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider


@pytest.fixture()
def provider_factory() -> Callable[[], EncryptionProvider]:
    return InMemoryEncryptionProvider


class EnvelopeProviderContract:
    """Subclassed by provider-specific modules with their own fixture."""

    def test_wrap_unwrap_roundtrip(self, provider_factory) -> None:
        prov = provider_factory()
        dek = b"\x77" * 32
        tid = uuid.uuid4()
        wrapped, version = prov.wrap_dek(dek, tenant_id=tid, aad=b"aad-x")
        assert version >= 1
        assert prov.unwrap_dek(wrapped, tenant_id=tid, aad=b"aad-x", kek_version=version) == dek

    def test_unwrap_with_wrong_tenant_fails(self, provider_factory) -> None:
        prov = provider_factory()
        dek = b"\x78" * 32
        wrapped, v = prov.wrap_dek(dek, tenant_id=uuid.uuid4(), aad=b"aad")
        with pytest.raises(WrappedDEKInvalid):
            prov.unwrap_dek(wrapped, tenant_id=uuid.uuid4(), aad=b"aad", kek_version=v)

    def test_unwrap_with_wrong_aad_fails(self, provider_factory) -> None:
        prov = provider_factory()
        tid = uuid.uuid4()
        wrapped, v = prov.wrap_dek(b"\x79" * 32, tenant_id=tid, aad=b"aad-A")
        with pytest.raises(WrappedDEKInvalid):
            prov.unwrap_dek(wrapped, tenant_id=tid, aad=b"aad-B", kek_version=v)


class TestInMemoryContract(EnvelopeProviderContract):
    """Runs the full contract suite against the in-memory fake."""
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope_contract.py -v
```

Expected: 3 tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/auth/tests/test_envelope_contract.py
git commit -m "test(auth): shared EncryptionProvider contract suite (#3803)"
```

---

### Task 6: `envelope_metrics.py` (Prometheus)

**Files:**
- Create: `src/nexus/bricks/auth/envelope_metrics.py`

- [ ] **Step 1: Create the metrics module**

```python
"""Prometheus metrics for envelope encryption (issue #3803).

Low-cardinality labels only: tenant_id is acceptable (single-digit tenants at
this scale); principal_id and profile_id are NOT labels — they'd explode the
time-series count.

Metrics:
  - auth_dek_cache_hits_total{tenant_id}
  - auth_dek_cache_misses_total{tenant_id}
  - auth_dek_unwrap_errors_total{tenant_id,error_class}
  - auth_dek_unwrap_latency_seconds{tenant_id}
  - auth_kek_rotate_rows_total{tenant_id,from_version,to_version}
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

DEK_CACHE_HITS = Counter(
    "auth_dek_cache_hits_total",
    "Number of DEK cache hits on the decrypt path.",
    labelnames=["tenant_id"],
)

DEK_CACHE_MISSES = Counter(
    "auth_dek_cache_misses_total",
    "Number of DEK cache misses (KMS/Vault round-trip required).",
    labelnames=["tenant_id"],
)

DEK_UNWRAP_ERRORS = Counter(
    "auth_dek_unwrap_errors_total",
    "EncryptionProvider.unwrap_dek failures.",
    labelnames=["tenant_id", "error_class"],
)

DEK_UNWRAP_LATENCY = Histogram(
    "auth_dek_unwrap_latency_seconds",
    "Time spent in EncryptionProvider.unwrap_dek.",
    labelnames=["tenant_id"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

KEK_ROTATE_ROWS = Counter(
    "auth_kek_rotate_rows_total",
    "Rows rewrapped to a new kek_version by rotate_kek_for_tenant.",
    labelnames=["tenant_id", "from_version", "to_version"],
)

__all__ = [
    "DEK_CACHE_HITS",
    "DEK_CACHE_MISSES",
    "DEK_UNWRAP_ERRORS",
    "DEK_UNWRAP_LATENCY",
    "KEK_ROTATE_ROWS",
]
```

- [ ] **Step 2: Smoke-import test**

Append to `src/nexus/bricks/auth/tests/test_envelope.py`:

```python
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
```

- [ ] **Step 3: Run tests**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py::TestMetricsImport -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/auth/envelope_metrics.py src/nexus/bricks/auth/tests/test_envelope.py
git commit -m "feat(auth): envelope_metrics Prometheus counters (#3803)"
```

---

### Task 7: `CredentialCarryingProfileStore` sub-protocol

**Files:**
- Modify: `src/nexus/bricks/auth/profile.py`
- Test: `src/nexus/bricks/auth/tests/test_envelope.py` (append protocol smoke-check)

- [ ] **Step 1: Append failing test**

```python
class TestCredentialCarryingProtocol:
    def test_protocol_defines_two_methods(self) -> None:
        from nexus.bricks.auth.profile import CredentialCarryingProfileStore

        assert hasattr(CredentialCarryingProfileStore, "upsert_with_credential")
        assert hasattr(CredentialCarryingProfileStore, "get_with_credential")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py::TestCredentialCarryingProtocol -v
```

Expected: FAIL — `ImportError`.

- [ ] **Step 3: Extend `profile.py`**

Append to `src/nexus/bricks/auth/profile.py`, after the existing `AuthProfileStore` Protocol:

```python
# ---------------------------------------------------------------------------
# CredentialCarryingProfileStore sub-protocol (issue #3803)
# ---------------------------------------------------------------------------


@runtime_checkable
class CredentialCarryingProfileStore(AuthProfileStore, Protocol):
    """Sub-protocol for stores that additionally hold encrypted credentials.

    Only ``PostgresAuthProfileStore`` implements this today. Consumers that
    need the resolved credential inline (rather than via a ``CredentialBackend``
    pointer) type-annotate against this protocol instead of the base one.

    Rows written via plain ``upsert`` are compatible: ``get_with_credential``
    returns ``(profile, None)`` in that case.
    """

    def upsert_with_credential(
        self, profile: AuthProfile, credential: ResolvedCredential
    ) -> None:
        """Insert or update ``profile`` and store ``credential`` encrypted."""
        ...

    def get_with_credential(
        self, profile_id: str
    ) -> tuple[AuthProfile, ResolvedCredential | None] | None:
        """Return ``(profile, credential | None)`` or ``None`` if absent.

        ``credential`` is ``None`` when the row has no ciphertext columns (PR 1
        routing-only rows).
        """
        ...
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope.py::TestCredentialCarryingProtocol -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/profile.py src/nexus/bricks/auth/tests/test_envelope.py
git commit -m "feat(auth): CredentialCarryingProfileStore sub-protocol (#3803)"
```

---

### Task 8: Schema delta — 5 encryption columns + CHECK + upgrade path

**Files:**
- Modify: `src/nexus/bricks/auth/postgres_profile_store.py`
- Test: `src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py` (new)

- [ ] **Step 1: Create new postgres test module with the CHECK-constraint test**

Create `src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py`:

```python
"""Integration tests for envelope encryption on PostgresAuthProfileStore (#3803).

Postgres-gated; uses the same TEST_POSTGRES_URL + xdist_group shape as
test_postgres_profile_store.py.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    drop_schema,
    ensure_principal,
    ensure_schema,
    ensure_tenant,
)
from nexus.bricks.auth.tests.conftest import make_profile

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+psycopg2://postgres:nexus@localhost:5432/nexus",
)


def _pg_is_available() -> bool:
    try:
        eng = create_engine(PG_URL)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.xdist_group("postgres_auth_profile_store"),
    pytest.mark.skipif(
        not _pg_is_available(),
        reason=(
            "PostgreSQL not reachable at TEST_POSTGRES_URL. "
            "Start with: docker compose -f dockerfiles/compose.yaml up postgres -d"
        ),
    ),
]


@pytest.fixture(scope="module")
def pg_engine() -> Generator[Engine, None, None]:
    engine = create_engine(PG_URL, future=True)
    drop_schema(engine)
    ensure_schema(engine)
    yield engine
    drop_schema(engine)
    engine.dispose()


@pytest.fixture()
def tenant_id(pg_engine: Engine) -> uuid.UUID:
    return ensure_tenant(pg_engine, f"env-tenant-{uuid.uuid4()}")


@pytest.fixture()
def principal_id(pg_engine: Engine, tenant_id: uuid.UUID) -> uuid.UUID:
    return ensure_principal(
        pg_engine,
        tenant_id=tenant_id,
        kind="human",
        external_sub=f"sub-{uuid.uuid4()}",
        auth_method="test",
    )


class TestSchema:
    def test_check_constraint_rejects_half_written_row(
        self, pg_engine: Engine, tenant_id: uuid.UUID, principal_id: uuid.UUID
    ) -> None:
        """Direct INSERT with 4 of 5 encryption columns set must fail."""
        with pg_engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO auth_profiles "
                    "(tenant_id, principal_id, id, provider, account_identifier, "
                    " backend, backend_key, "
                    " ciphertext, wrapped_dek, nonce, aad) "  # 4 of 5, missing kek_version
                    "VALUES "
                    "(:tid, :pid, 'broken', 'p', 'p', 'b', 'k', "
                    " :ct, :wd, :n, :a)"
                ),
                {
                    "tid": tenant_id,
                    "pid": principal_id,
                    "ct": b"ct",
                    "wd": b"wd",
                    "n": b"n",
                    "a": b"a",
                },
            )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py -v
```

Expected: FAIL — columns `ciphertext`/`wrapped_dek`/`nonce`/`aad` don't exist (`UndefinedColumn`).

- [ ] **Step 3: Add columns + CHECK to `_TABLE_STATEMENTS` in `postgres_profile_store.py`**

In `src/nexus/bricks/auth/postgres_profile_store.py`, extend the `auth_profiles` CREATE TABLE inside `_TABLE_STATEMENTS`:

```python
    """
    CREATE TABLE IF NOT EXISTS auth_profiles (
        tenant_id          UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        principal_id       UUID NOT NULL,
        id                 TEXT NOT NULL,
        provider           TEXT NOT NULL,
        account_identifier TEXT NOT NULL,
        backend            TEXT NOT NULL,
        backend_key        TEXT NOT NULL,
        last_synced_at     TIMESTAMPTZ,
        sync_ttl_seconds   INTEGER NOT NULL DEFAULT 300,
        last_used_at       TIMESTAMPTZ,
        success_count      INTEGER NOT NULL DEFAULT 0,
        failure_count      INTEGER NOT NULL DEFAULT 0,
        cooldown_until     TIMESTAMPTZ,
        cooldown_reason    TEXT,
        disabled_until     TIMESTAMPTZ,
        raw_error          TEXT,
        ciphertext         BYTEA,
        wrapped_dek        BYTEA,
        nonce              BYTEA,
        aad                BYTEA,
        kek_version        INTEGER,
        created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (tenant_id, principal_id, id),
        FOREIGN KEY (principal_id, tenant_id)
            REFERENCES principals(id, tenant_id) ON DELETE CASCADE,
        CONSTRAINT auth_profiles_envelope_all_or_none CHECK (
            (ciphertext IS NULL) = (wrapped_dek IS NULL)
            AND (ciphertext IS NULL) = (nonce IS NULL)
            AND (ciphertext IS NULL) = (aad IS NULL)
            AND (ciphertext IS NULL) = (kek_version IS NULL)
        )
    )
    """,
```

- [ ] **Step 4: Add upgrade path in `_upgrade_shape_in_place`**

Append inside `_upgrade_shape_in_place`, after the existing `auth_profiles` upgrade block:

```python
    # --- auth_profiles: envelope encryption columns (issue #3803) ---
    for col, decl in (
        ("ciphertext", "BYTEA"),
        ("wrapped_dek", "BYTEA"),
        ("nonce", "BYTEA"),
        ("aad", "BYTEA"),
        ("kek_version", "INTEGER"),
    ):
        conn.execute(
            text(
                f"ALTER TABLE auth_profiles ADD COLUMN IF NOT EXISTS {col} {decl}"
            )
        )
    # CHECK constraint. Use DROP ... IF EXISTS + ADD for idempotency.
    conn.execute(
        text(
            "ALTER TABLE auth_profiles "
            "DROP CONSTRAINT IF EXISTS auth_profiles_envelope_all_or_none"
        )
    )
    conn.execute(
        text(
            "ALTER TABLE auth_profiles "
            "ADD CONSTRAINT auth_profiles_envelope_all_or_none CHECK ("
            "    (ciphertext IS NULL) = (wrapped_dek IS NULL)"
            "    AND (ciphertext IS NULL) = (nonce IS NULL)"
            "    AND (ciphertext IS NULL) = (aad IS NULL)"
            "    AND (ciphertext IS NULL) = (kek_version IS NULL)"
            ")"
        )
    )
```

- [ ] **Step 5: Run the new test + the existing postgres test suite**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py -v
pytest src/nexus/bricks/auth/tests/test_postgres_profile_store.py -v
```

Expected: the CHECK test passes; the existing PR 1 tests all still pass (schema is backwards-compatible).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/auth/postgres_profile_store.py src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py
git commit -m "feat(auth): envelope encryption schema columns + CHECK (#3803)"
```

---

### Task 9: `upsert_with_credential` + `get_with_credential` on `PostgresAuthProfileStore`

**Files:**
- Modify: `src/nexus/bricks/auth/postgres_profile_store.py`
- Test: `src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py` (append)

- [ ] **Step 1: Append failing test**

```python
from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider


@pytest.fixture()
def encryption_provider() -> InMemoryEncryptionProvider:
    return InMemoryEncryptionProvider()


@pytest.fixture()
def pg_store_crypto(
    pg_engine: Engine,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    encryption_provider: InMemoryEncryptionProvider,
) -> Generator[PostgresAuthProfileStore, None, None]:
    store = PostgresAuthProfileStore(
        PG_URL,
        tenant_id=tenant_id,
        principal_id=principal_id,
        engine=pg_engine,
        encryption_provider=encryption_provider,
    )
    yield store
    store.close()


class TestEncryptedUpsertAndGet:
    def test_roundtrip(self, pg_store_crypto: PostgresAuthProfileStore) -> None:
        profile = make_profile("google/alice")
        cred = ResolvedCredential(
            kind="bearer_token",
            access_token="ya29.fake",
            scopes=("https://www.googleapis.com/auth/userinfo.email",),
        )
        pg_store_crypto.upsert_with_credential(profile, cred)
        got = pg_store_crypto.get_with_credential("google/alice")
        assert got is not None
        p, c = got
        assert p.id == "google/alice"
        assert c is not None
        assert c.access_token == "ya29.fake"
        assert c.scopes == ("https://www.googleapis.com/auth/userinfo.email",)

    def test_get_returns_none_for_missing(
        self, pg_store_crypto: PostgresAuthProfileStore
    ) -> None:
        assert pg_store_crypto.get_with_credential("does-not-exist") is None

    def test_pr1_row_returns_none_credential(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        """A row written via plain upsert reads back (profile, None)."""
        plain_store = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=tenant_id,
            principal_id=principal_id,
            engine=pg_engine,
        )
        plain_store.upsert(make_profile("openai/bob"))
        got = pg_store_crypto.get_with_credential("openai/bob")
        assert got is not None
        p, c = got
        assert p.id == "openai/bob"
        assert c is None

    def test_ctor_without_provider_rejects_crypto_methods(
        self,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        store = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=tenant_id,
            principal_id=principal_id,
            engine=pg_engine,
        )
        try:
            with pytest.raises(RuntimeError, match="encryption_provider"):
                store.upsert_with_credential(
                    make_profile("x"),
                    ResolvedCredential(kind="api_key", api_key="k"),
                )
            with pytest.raises(RuntimeError, match="encryption_provider"):
                store.get_with_credential("x")
        finally:
            store.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py::TestEncryptedUpsertAndGet -v
```

Expected: FAIL — `upsert_with_credential` / `get_with_credential` don't exist; ctor doesn't accept `encryption_provider`.

- [ ] **Step 3: Implement the methods**

In `src/nexus/bricks/auth/postgres_profile_store.py`:

Add imports near the top:

```python
import hashlib
import json
import secrets
from dataclasses import asdict
from datetime import datetime

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.envelope import (
    AADMismatch,
    AESGCMEnvelope,
    DEKCache,
    EncryptionProvider,
    WrappedDEKInvalid,
)
from nexus.bricks.auth.envelope_metrics import (
    DEK_CACHE_HITS,
    DEK_CACHE_MISSES,
    DEK_UNWRAP_ERRORS,
    DEK_UNWRAP_LATENCY,
)
```

Extend the ctor signature:

```python
    def __init__(
        self,
        db_url: str,
        *,
        tenant_id: uuid.UUID | str,
        principal_id: uuid.UUID | str,
        engine: Engine | None = None,
        pool_size: int = 5,
        encryption_provider: EncryptionProvider | None = None,
        dek_cache: DEKCache | None = None,
    ) -> None:
        self._tenant_id = uuid.UUID(str(tenant_id))
        self._principal_id = uuid.UUID(str(principal_id))
        if engine is None:
            self._engine = create_engine(
                db_url,
                pool_size=pool_size,
                pool_pre_ping=True,
                future=True,
            )
            self._owns_engine = True
        else:
            self._engine = engine
            self._owns_engine = False
        self._encryption_provider = encryption_provider
        self._aesgcm = AESGCMEnvelope()
        self._dek_cache = dek_cache or DEKCache()
```

Add helpers + new methods. Place them right before `# Tenant-wide helpers`:

```python
    # ------------------------------------------------------------------
    # Envelope encryption (issue #3803)
    # ------------------------------------------------------------------

    def _require_provider(self) -> EncryptionProvider:
        if self._encryption_provider is None:
            raise RuntimeError(
                "encryption_provider is required for upsert_with_credential / "
                "get_with_credential — construct PostgresAuthProfileStore(..., "
                "encryption_provider=...)"
            )
        return self._encryption_provider

    def _aad_for(self, profile_id: str) -> bytes:
        return f"{self._tenant_id}|{self._principal_id}|{profile_id}".encode("utf-8")

    @staticmethod
    def _serialize_credential(cred: ResolvedCredential) -> bytes:
        # Canonical JSON: sorted keys, compact separators. Deterministic for
        # rotation rewrap; any change here breaks existing ciphertext readability.
        payload = asdict(cred)
        # datetime → ISO 8601 string
        if payload.get("expires_at") is not None:
            payload["expires_at"] = cred.expires_at.isoformat()  # type: ignore[union-attr]
        # tuple → list (JSON has no tuple)
        payload["scopes"] = list(cred.scopes)
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _deserialize_credential(data: bytes) -> ResolvedCredential:
        raw = json.loads(data.decode("utf-8"))
        expires_at = raw.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        return ResolvedCredential(
            kind=raw["kind"],
            api_key=raw.get("api_key"),
            access_token=raw.get("access_token"),
            expires_at=expires_at,
            scopes=tuple(raw.get("scopes", ())),
            metadata=raw.get("metadata", {}) or {},
        )

    def upsert_with_credential(
        self, profile: AuthProfile, credential: ResolvedCredential
    ) -> None:
        provider = self._require_provider()
        aad = self._aad_for(profile.id)
        dek = secrets.token_bytes(32)
        nonce, ciphertext = self._aesgcm.encrypt(
            dek, self._serialize_credential(credential), aad=aad
        )
        wrapped_dek, kek_version = provider.wrap_dek(
            dek, tenant_id=self._tenant_id, aad=aad
        )
        params = _profile_params(
            profile, tenant_id=self._tenant_id, principal_id=self._principal_id
        )
        params.update(
            ciphertext=ciphertext,
            wrapped_dek=wrapped_dek,
            nonce=nonce,
            aad=aad,
            kek_version=kek_version,
        )
        lock_key = f"{self._tenant_id}/{profile.id}"
        with self._scoped() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": lock_key},
            )
            conn.execute(text(_UPSERT_WITH_CREDENTIAL_SQL), params)

    def get_with_credential(
        self, profile_id: str
    ) -> tuple[AuthProfile, ResolvedCredential | None] | None:
        provider = self._require_provider()
        with self._scoped() as conn:
            row = conn.execute(
                text(
                    "SELECT *, ciphertext, wrapped_dek, nonce, aad, kek_version "
                    "FROM auth_profiles "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "tid": self._tenant_id,
                    "pid": self._principal_id,
                    "id": profile_id,
                },
            ).fetchone()
        if row is None:
            return None
        profile = _row_to_profile(row)
        if row.ciphertext is None:
            return profile, None
        expected_aad = self._aad_for(profile_id)
        if bytes(row.aad) != expected_aad:
            raise AADMismatch.from_row(
                tenant_id=self._tenant_id,
                profile_id=profile_id,
                kek_version=row.kek_version,
                cause="stored AAD does not match tenant|principal|profile_id",
            )
        cache_key = self._dek_cache.make_key(
            tenant_id=self._tenant_id,
            kek_version=row.kek_version,
            wrapped_dek=bytes(row.wrapped_dek),
        )
        tenant_label = str(self._tenant_id)
        dek = self._dek_cache.get(cache_key)
        if dek is None:
            DEK_CACHE_MISSES.labels(tenant_id=tenant_label).inc()
            try:
                with DEK_UNWRAP_LATENCY.labels(tenant_id=tenant_label).time():
                    dek = provider.unwrap_dek(
                        bytes(row.wrapped_dek),
                        tenant_id=self._tenant_id,
                        aad=expected_aad,
                        kek_version=row.kek_version,
                    )
            except Exception as exc:  # real error class recorded below
                DEK_UNWRAP_ERRORS.labels(
                    tenant_id=tenant_label, error_class=type(exc).__name__
                ).inc()
                raise
            self._dek_cache.put(cache_key, dek)
        else:
            DEK_CACHE_HITS.labels(tenant_id=tenant_label).inc()
        plaintext = self._aesgcm.decrypt(
            dek, bytes(row.nonce), bytes(row.ciphertext), aad=expected_aad
        )
        return profile, self._deserialize_credential(plaintext)
```

Add the new upsert SQL constant near `_UPSERT_SQL`:

```python
_UPSERT_WITH_CREDENTIAL_SQL = """
INSERT INTO auth_profiles (
    tenant_id, principal_id, id,
    provider, account_identifier, backend, backend_key,
    last_synced_at, sync_ttl_seconds,
    last_used_at, success_count, failure_count,
    cooldown_until, cooldown_reason, disabled_until, raw_error,
    ciphertext, wrapped_dek, nonce, aad, kek_version,
    updated_at
) VALUES (
    :tenant_id, :principal_id, :id,
    :provider, :account_identifier, :backend, :backend_key,
    :last_synced_at, :sync_ttl_seconds,
    :last_used_at, :success_count, :failure_count,
    :cooldown_until, :cooldown_reason, :disabled_until, :raw_error,
    :ciphertext, :wrapped_dek, :nonce, :aad, :kek_version,
    NOW()
)
ON CONFLICT (tenant_id, principal_id, id) DO UPDATE SET
    provider           = EXCLUDED.provider,
    account_identifier = EXCLUDED.account_identifier,
    backend            = EXCLUDED.backend,
    backend_key        = EXCLUDED.backend_key,
    last_synced_at     = EXCLUDED.last_synced_at,
    sync_ttl_seconds   = EXCLUDED.sync_ttl_seconds,
    last_used_at       = EXCLUDED.last_used_at,
    success_count      = EXCLUDED.success_count,
    failure_count      = EXCLUDED.failure_count,
    cooldown_until     = EXCLUDED.cooldown_until,
    cooldown_reason    = EXCLUDED.cooldown_reason,
    disabled_until     = EXCLUDED.disabled_until,
    raw_error          = EXCLUDED.raw_error,
    ciphertext         = EXCLUDED.ciphertext,
    wrapped_dek        = EXCLUDED.wrapped_dek,
    nonce              = EXCLUDED.nonce,
    aad                = EXCLUDED.aad,
    kek_version        = EXCLUDED.kek_version,
    updated_at         = NOW()
"""
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py -v
```

Expected: all `TestEncryptedUpsertAndGet` pass; previous `TestSchema::test_check_constraint_rejects_half_written_row` still passes.

- [ ] **Step 5: Run the full PR 1 suite to make sure nothing regressed**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_profile_store.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/auth/postgres_profile_store.py src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py
git commit -m "feat(auth): upsert_with_credential + get_with_credential on Postgres store (#3803)"
```

---

### Task 10: Acceptance tests — swap attack, mixed versions, cache amortization

**Files:**
- Modify: `src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py` (append)

- [ ] **Step 1: Append three acceptance tests**

```python
class TestSwapAttackRejected:
    def test_ciphertext_copied_cross_tenant_fails_decrypt(
        self,
        pg_engine: Engine,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """Attacker with raw DB access copies A's ciphertext+wrapped_dek+nonce+aad+kek_version
        into B under a different tenant. Decrypt on B must fail.

        The InMemoryEncryptionProvider mixes tenant_id into AAD at the wrap
        level, so this raises WrappedDEKInvalid. Real providers fail at their
        native layer (KMS EncryptionContext, Vault derivation context). The
        stored ``aad`` column also wouldn't match B's ``tenant|principal|id``
        — AADMismatch would fire at the row-level check before unwrap.
        """
        t_a = ensure_tenant(pg_engine, f"atk-a-{uuid.uuid4()}")
        t_b = ensure_tenant(pg_engine, f"atk-b-{uuid.uuid4()}")
        p_a = ensure_principal(
            pg_engine, tenant_id=t_a, external_sub=f"sa-{uuid.uuid4()}", auth_method="t"
        )
        p_b = ensure_principal(
            pg_engine, tenant_id=t_b, external_sub=f"sb-{uuid.uuid4()}", auth_method="t"
        )
        store_a = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=t_a,
            principal_id=p_a,
            engine=pg_engine,
            encryption_provider=encryption_provider,
        )
        store_b = PostgresAuthProfileStore(
            PG_URL,
            tenant_id=t_b,
            principal_id=p_b,
            engine=pg_engine,
            encryption_provider=encryption_provider,
        )
        try:
            # Write row A with a real credential.
            store_a.upsert_with_credential(
                make_profile("shared-id"),
                ResolvedCredential(kind="api_key", api_key="A-SECRET"),
            )
            # Write B as routing-only (so row exists), then copy A's ciphertext.
            store_b.upsert(make_profile("shared-id"))
            with pg_engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.current_tenant = :tid"), {"tid": str(t_a)}
                )
                row_a = conn.execute(
                    text(
                        "SELECT ciphertext, wrapped_dek, nonce, aad, kek_version "
                        "FROM auth_profiles WHERE tenant_id = :tid AND id = :id"
                    ),
                    {"tid": t_a, "id": "shared-id"},
                ).fetchone()
            assert row_a is not None
            with pg_engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.current_tenant = :tid"), {"tid": str(t_b)}
                )
                conn.execute(
                    text(
                        "UPDATE auth_profiles SET "
                        "    ciphertext = :ct, wrapped_dek = :wd, "
                        "    nonce = :n, aad = :a, kek_version = :v "
                        "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                    ),
                    {
                        "ct": bytes(row_a.ciphertext),
                        "wd": bytes(row_a.wrapped_dek),
                        "n": bytes(row_a.nonce),
                        "a": bytes(row_a.aad),
                        "v": row_a.kek_version,
                        "tid": t_b,
                        "pid": p_b,
                        "id": "shared-id",
                    },
                )
            with pytest.raises((AADMismatch, WrappedDEKInvalid)):
                store_b.get_with_credential("shared-id")
        finally:
            store_a.close()
            store_b.close()


class TestMixedVersionReads:
    def test_reads_span_rotation(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("v1-row"),
            ResolvedCredential(kind="api_key", api_key="v1"),
        )
        encryption_provider.rotate()
        pg_store_crypto.upsert_with_credential(
            make_profile("v2-row"),
            ResolvedCredential(kind="api_key", api_key="v2"),
        )
        a = pg_store_crypto.get_with_credential("v1-row")
        b = pg_store_crypto.get_with_credential("v2-row")
        assert a is not None and a[1] is not None and a[1].api_key == "v1"
        assert b is not None and b[1] is not None and b[1].api_key == "v2"


class TestCacheAmortizes:
    def test_two_reads_one_unwrap(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("cached"),
            ResolvedCredential(kind="api_key", api_key="k"),
        )
        start = encryption_provider.unwrap_count
        pg_store_crypto.get_with_credential("cached")
        pg_store_crypto.get_with_credential("cached")
        assert encryption_provider.unwrap_count - start == 1


class TestAADMismatch:
    def test_aad_column_tampered_raises(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("aad-tamper"),
            ResolvedCredential(kind="api_key", api_key="k"),
        )
        with pg_engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)}
            )
            conn.execute(
                text(
                    "UPDATE auth_profiles SET aad = :bad "
                    "WHERE tenant_id = :tid AND principal_id = :pid AND id = :id"
                ),
                {
                    "bad": b"bogus-aad-bytes",
                    "tid": tenant_id,
                    "pid": principal_id,
                    "id": "aad-tamper",
                },
            )
        with pytest.raises(AADMismatch):
            pg_store_crypto.get_with_credential("aad-tamper")
```

Also add the imports at the top of the file:

```python
from nexus.bricks.auth.envelope import AADMismatch, WrappedDEKInvalid
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py -v
```

Expected: all new tests pass; previous tests still pass.

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py
git commit -m "test(auth): swap-attack + mixed-version + cache amortization (#3803)"
```

---

### Task 11: `rotate_kek_for_tenant` helper + `RotationReport`

**Files:**
- Modify: `src/nexus/bricks/auth/postgres_profile_store.py` (append helper)
- Test: `src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py` (append)

- [ ] **Step 1: Append failing tests**

```python
from nexus.bricks.auth.postgres_profile_store import (
    RotationReport,
    rotate_kek_for_tenant,
)


class TestRotateKEKForTenant:
    def test_noop_when_all_rows_current(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("current-1"),
            ResolvedCredential(kind="api_key", api_key="k"),
        )
        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=encryption_provider,
        )
        assert report.rows_rewrapped == 0
        assert report.rows_remaining == 0

    def test_rotates_stale_rows(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        pg_store_crypto.upsert_with_credential(
            make_profile("a"),
            ResolvedCredential(kind="api_key", api_key="k-a"),
        )
        pg_store_crypto.upsert_with_credential(
            make_profile("b"),
            ResolvedCredential(kind="api_key", api_key="k-b"),
        )
        encryption_provider.rotate()
        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=encryption_provider,
            batch_size=1,
        )
        assert report.rows_rewrapped == 2
        assert report.rows_remaining == 0
        assert report.target_version == 2
        # Reads still succeed, and now both rows are at v2.
        a = pg_store_crypto.get_with_credential("a")
        b = pg_store_crypto.get_with_credential("b")
        assert a is not None and a[1] is not None and a[1].api_key == "k-a"
        assert b is not None and b[1] is not None and b[1].api_key == "k-b"

    def test_respects_max_rows(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        for i in range(3):
            pg_store_crypto.upsert_with_credential(
                make_profile(f"m-{i}"),
                ResolvedCredential(kind="api_key", api_key=f"k{i}"),
            )
        encryption_provider.rotate()
        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=encryption_provider,
            batch_size=2,
            max_rows=2,
        )
        assert report.rows_rewrapped == 2
        assert report.rows_remaining == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py::TestRotateKEKForTenant -v
```

Expected: FAIL — `ImportError: cannot import name 'rotate_kek_for_tenant'`.

- [ ] **Step 3: Implement `rotate_kek_for_tenant` + `RotationReport`**

Append to `src/nexus/bricks/auth/postgres_profile_store.py` at module level (outside the store class):

```python
# ---------------------------------------------------------------------------
# KEK rotation (issue #3803) — module-level admin helper
# ---------------------------------------------------------------------------


from dataclasses import dataclass as _dataclass  # local alias; already imported

from nexus.bricks.auth.envelope_metrics import KEK_ROTATE_ROWS


@_dataclass(frozen=True, slots=True)
class RotationReport:
    """Result of a ``rotate_kek_for_tenant`` invocation."""

    rows_rewrapped: int
    rows_failed: int
    rows_remaining: int
    target_version: int


def rotate_kek_for_tenant(
    engine: Engine,
    *,
    tenant_id: uuid.UUID,
    encryption_provider: EncryptionProvider,
    batch_size: int = 100,
    max_rows: int | None = None,
) -> RotationReport:
    """Rewrap every row in ``tenant_id`` whose ``kek_version`` is older than
    the provider's current version.

    Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so the helper is resumable and
    does not block concurrent writers. Rewraps ``wrapped_dek`` + ``kek_version``
    only; ``ciphertext``, ``nonce``, ``aad`` are untouched so a reader mid-
    rotation decrypts successfully regardless of which version wrote.
    """
    target = encryption_provider.current_version(tenant_id=tenant_id)
    rewrapped = 0
    failed = 0
    tenant_label = str(tenant_id)
    while True:
        if max_rows is not None and rewrapped + failed >= max_rows:
            break
        this_batch = batch_size
        if max_rows is not None:
            this_batch = min(this_batch, max_rows - (rewrapped + failed))
        with engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :tid"),
                {"tid": str(tenant_id)},
            )
            rows = conn.execute(
                text(
                    "SELECT tenant_id, principal_id, id, wrapped_dek, aad, kek_version "
                    "FROM auth_profiles "
                    "WHERE tenant_id = :tid "
                    "  AND ciphertext IS NOT NULL "
                    "  AND kek_version < :target "
                    "ORDER BY principal_id, id "
                    "FOR UPDATE SKIP LOCKED "
                    "LIMIT :lim"
                ),
                {"tid": tenant_id, "target": target, "lim": this_batch},
            ).fetchall()
            if not rows:
                break
            for row in rows:
                try:
                    dek = encryption_provider.unwrap_dek(
                        bytes(row.wrapped_dek),
                        tenant_id=tenant_id,
                        aad=bytes(row.aad),
                        kek_version=row.kek_version,
                    )
                    new_wrapped, new_version = encryption_provider.wrap_dek(
                        dek, tenant_id=tenant_id, aad=bytes(row.aad)
                    )
                except Exception as exc:
                    logger.error(
                        "rotate_kek_for_tenant: per-row failure "
                        "tenant=%s principal=%s profile=%s kek_version=%s cause=%s",
                        tenant_id,
                        row.principal_id,
                        row.id,
                        row.kek_version,
                        type(exc).__name__,
                    )
                    failed += 1
                    continue
                conn.execute(
                    text(
                        "UPDATE auth_profiles SET "
                        "    wrapped_dek = :wd, kek_version = :v, updated_at = NOW() "
                        "WHERE tenant_id = :tid "
                        "  AND principal_id = :pid AND id = :id"
                    ),
                    {
                        "wd": new_wrapped,
                        "v": new_version,
                        "tid": tenant_id,
                        "pid": row.principal_id,
                        "id": row.id,
                    },
                )
                KEK_ROTATE_ROWS.labels(
                    tenant_id=tenant_label,
                    from_version=str(row.kek_version),
                    to_version=str(new_version),
                ).inc()
                rewrapped += 1
    # Final remaining count
    with engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.current_tenant = :tid"),
            {"tid": str(tenant_id)},
        )
        remaining = conn.execute(
            text(
                "SELECT COUNT(*) FROM auth_profiles "
                "WHERE tenant_id = :tid AND ciphertext IS NOT NULL "
                "  AND kek_version < :target"
            ),
            {"tid": tenant_id, "target": target},
        ).scalar_one()
    return RotationReport(
        rows_rewrapped=rewrapped,
        rows_failed=failed,
        rows_remaining=int(remaining),
        target_version=target,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py::TestRotateKEKForTenant -v
```

Expected: all 3 pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/postgres_profile_store.py src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py
git commit -m "feat(auth): rotate_kek_for_tenant admin helper (#3803)"
```

---

### Task 12: Per-row rotation failure continues the batch

**Files:**
- Modify: `src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py` (append)

- [ ] **Step 1: Append test + a throwing provider shim**

```python
class TestRotateKEKFailures:
    def test_per_row_failure_continues_batch(
        self,
        pg_store_crypto: PostgresAuthProfileStore,
        pg_engine: Engine,
        tenant_id: uuid.UUID,
        encryption_provider: InMemoryEncryptionProvider,
    ) -> None:
        """An unwrap failure on one row leaves that row on the old version; the
        batch continues to completion for other rows."""
        from nexus.bricks.auth.envelope import WrappedDEKInvalid

        for i in range(3):
            pg_store_crypto.upsert_with_credential(
                make_profile(f"fail-{i}"),
                ResolvedCredential(kind="api_key", api_key=f"k{i}"),
            )
        encryption_provider.rotate()

        # Wrap the provider: fail on the middle row's wrapped_dek only.
        middle_wrapped = None
        with pg_engine.begin() as conn:
            conn.execute(
                text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)}
            )
            row = conn.execute(
                text(
                    "SELECT wrapped_dek FROM auth_profiles "
                    "WHERE tenant_id = :tid AND id = 'fail-1'"
                ),
                {"tid": tenant_id},
            ).fetchone()
            assert row is not None
            middle_wrapped = bytes(row.wrapped_dek)

        real = encryption_provider

        class _FlakyProvider:
            def current_version(self, *, tenant_id):
                return real.current_version(tenant_id=tenant_id)

            def wrap_dek(self, dek, *, tenant_id, aad):
                return real.wrap_dek(dek, tenant_id=tenant_id, aad=aad)

            def unwrap_dek(self, wrapped, *, tenant_id, aad, kek_version):
                if wrapped == middle_wrapped:
                    raise WrappedDEKInvalid.from_row(
                        tenant_id=tenant_id,
                        profile_id="fail-1",
                        kek_version=kek_version,
                        cause="simulated flake",
                    )
                return real.unwrap_dek(
                    wrapped, tenant_id=tenant_id, aad=aad, kek_version=kek_version
                )

        report = rotate_kek_for_tenant(
            pg_engine,
            tenant_id=tenant_id,
            encryption_provider=_FlakyProvider(),
        )
        assert report.rows_rewrapped == 2
        assert report.rows_failed == 1
        assert report.rows_remaining == 1
```

- [ ] **Step 2: Run test**

```bash
pytest src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py::TestRotateKEKFailures -v
```

Expected: pass (implementation from Task 11 already handles per-row failure).

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py
git commit -m "test(auth): per-row rotation failure continues batch (#3803)"
```

---

### Task 13: `nexus auth rotate-kek` CLI subcommand

**Files:**
- Modify: `src/nexus/bricks/auth/cli_commands.py`
- Test: `src/nexus/bricks/auth/tests/test_rotate_kek_cli.py` (new)

- [ ] **Step 1: Create the failing CLI test**

Create `src/nexus/bricks/auth/tests/test_rotate_kek_cli.py`:

```python
"""Tests for `nexus auth rotate-kek` (issue #3803)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("click")

from click.testing import CliRunner
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.bricks.auth.cli_commands import auth
from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    drop_schema,
    ensure_principal,
    ensure_schema,
    ensure_tenant,
)
from nexus.bricks.auth.tests.conftest import make_profile

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql+psycopg2://postgres:nexus@localhost:5432/nexus",
)


def _pg_is_available() -> bool:
    try:
        eng = create_engine(PG_URL)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.xdist_group("postgres_auth_profile_store"),
    pytest.mark.skipif(
        not _pg_is_available(),
        reason="PostgreSQL not reachable at TEST_POSTGRES_URL.",
    ),
]


@pytest.fixture(scope="module")
def pg_engine() -> Generator[Engine, None, None]:
    engine = create_engine(PG_URL, future=True)
    drop_schema(engine)
    ensure_schema(engine)
    yield engine
    drop_schema(engine)
    engine.dispose()


@pytest.fixture()
def seeded_tenant(pg_engine: Engine) -> tuple[uuid.UUID, InMemoryEncryptionProvider]:
    """Return (tenant_id, encryption_provider) with 2 rows at v1, provider at v2."""
    t = ensure_tenant(pg_engine, f"rot-{uuid.uuid4()}")
    p = ensure_principal(
        pg_engine, tenant_id=t, external_sub=f"s-{uuid.uuid4()}", auth_method="t"
    )
    prov = InMemoryEncryptionProvider()
    store = PostgresAuthProfileStore(
        PG_URL, tenant_id=t, principal_id=p, engine=pg_engine, encryption_provider=prov
    )
    try:
        store.upsert_with_credential(
            make_profile("a"), ResolvedCredential(kind="api_key", api_key="k-a")
        )
        store.upsert_with_credential(
            make_profile("b"), ResolvedCredential(kind="api_key", api_key="k-b")
        )
    finally:
        store.close()
    prov.rotate()
    return t, prov


class TestRotateKekCLI:
    def test_dry_run_reports_counts_no_writes(
        self,
        seeded_tenant,
        pg_engine: Engine,
    ) -> None:
        t, prov = seeded_tenant
        runner = CliRunner()
        # Inject the provider via an env-var hook the CLI honors for tests.
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            # Provider registry: tests register a factory keyed by the env var.
            from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

            _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant",
                    _tenant_name(pg_engine, t),
                ],
            )
            assert result.exit_code == 0, result.output
            assert "dry-run" in result.output.lower()
            assert "2" in result.output  # 2 stale rows
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)

    def test_apply_rewraps_all(self, seeded_tenant, pg_engine: Engine) -> None:
        t, prov = seeded_tenant
        runner = CliRunner()
        os.environ["NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID"] = "inmem"
        try:
            from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY

            _TEST_PROVIDER_REGISTRY["inmem"] = lambda: prov
            result = runner.invoke(
                auth,
                [
                    "rotate-kek",
                    "--db-url",
                    PG_URL,
                    "--tenant",
                    _tenant_name(pg_engine, t),
                    "--apply",
                ],
            )
            assert result.exit_code == 0, result.output
            # Every row now at v2
            with pg_engine.begin() as conn:
                conn.execute(text("SET LOCAL app.current_tenant = :tid"), {"tid": str(t)})
                versions = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT DISTINCT kek_version FROM auth_profiles "
                            "WHERE tenant_id = :tid AND ciphertext IS NOT NULL"
                        ),
                        {"tid": t},
                    ).fetchall()
                ]
            assert versions == [2]
        finally:
            os.environ.pop("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID", None)
            _TEST_PROVIDER_REGISTRY.pop("inmem", None)


def _tenant_name(engine: Engine, tenant_id: uuid.UUID) -> str:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT name FROM tenants WHERE id = :tid"), {"tid": tenant_id}
        ).fetchone()
    assert row is not None
    return row[0]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/auth/tests/test_rotate_kek_cli.py -v
```

Expected: FAIL — `rotate-kek` subcommand doesn't exist; `_TEST_PROVIDER_REGISTRY` doesn't exist.

- [ ] **Step 3: Implement the CLI subcommand**

Append to `src/nexus/bricks/auth/cli_commands.py`, after `auth_migrate_to_postgres`:

```python
# ---------------------------------------------------------------------------
# auth rotate-kek (issue #3803 — envelope KEK rotation)
# ---------------------------------------------------------------------------

# Test hook: lets test modules register a provider factory without building
# real Vault/KMS wiring. Production code paths never use this registry — they
# construct a provider from CLI args (lands with the Phase D consumer).
_TEST_PROVIDER_REGISTRY: dict[str, "object"] = {}


@auth.command("rotate-kek")
@click.option(
    "--db-url",
    required=True,
    help="PostgreSQL URL (e.g. postgresql+psycopg2://user:pw@host:5432/db).",
)
@click.option(
    "--tenant",
    required=True,
    help="Tenant name whose rows should be rewrapped at the provider's current version.",
)
@click.option(
    "--apply", is_flag=True, default=False, help="Actually rewrap rows (default: dry-run)."
)
@click.option(
    "--batch-size", default=100, show_default=True, help="Rows per batch."
)
@click.option(
    "--max-rows",
    default=None,
    type=int,
    help="Upper bound on rows rewrapped across all batches (omit for no cap).",
)
def auth_rotate_kek(
    db_url: str,
    tenant: str,
    apply: bool,
    batch_size: int,
    max_rows: int | None,
) -> None:
    """Rewrap auth_profiles rows at the current provider KEK version.

    Dry-run by default. Operator promotes the provider version out-of-band
    (Vault: ``vault write -f transit/keys/<name>/rotate``; AWS KMS: managed);
    then runs this command to sweep rows stuck at older versions.
    """
    import os

    from sqlalchemy import create_engine, text

    from nexus.bricks.auth.postgres_profile_store import (
        ensure_schema,
        rotate_kek_for_tenant,
    )

    test_provider_id = os.environ.get("NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID")
    if test_provider_id:
        factory = _TEST_PROVIDER_REGISTRY.get(test_provider_id)
        if factory is None:
            raise click.ClickException(
                f"Test provider id {test_provider_id!r} not registered"
            )
        provider = factory()
    else:
        raise click.ClickException(
            "No production provider wiring yet. This command is consumer-driven "
            "— Phase D wires a real EncryptionProvider factory. Tests use "
            "NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID."
        )

    engine = create_engine(db_url, future=True)
    try:
        ensure_schema(engine)
        # Resolve tenant_id from name
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT id FROM tenants WHERE name = :n"), {"n": tenant}
            ).fetchone()
        if row is None:
            raise click.ClickException(f"Tenant {tenant!r} not found")
        import uuid as _uuid

        tenant_id = _uuid.UUID(str(row[0]))

        if not apply:
            # Dry-run: count stale rows at each version without rewrapping
            target = provider.current_version(tenant_id=tenant_id)
            with engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.current_tenant = :tid"), {"tid": str(tenant_id)}
                )
                stale = conn.execute(
                    text(
                        "SELECT kek_version, COUNT(*) FROM auth_profiles "
                        "WHERE tenant_id = :tid AND ciphertext IS NOT NULL "
                        "  AND kek_version < :target "
                        "GROUP BY kek_version ORDER BY kek_version"
                    ),
                    {"tid": tenant_id, "target": target},
                ).fetchall()
            total = sum(r[1] for r in stale)
            click.echo(f"dry-run: target_version={target}, stale_rows={total}")
            for version, count in stale:
                click.echo(f"  kek_version={version}: {count} rows")
            click.echo("Pass --apply to rewrap.")
            return

        report = rotate_kek_for_tenant(
            engine,
            tenant_id=tenant_id,
            encryption_provider=provider,
            batch_size=batch_size,
            max_rows=max_rows,
        )
        click.echo(
            f"rewrapped={report.rows_rewrapped} "
            f"failed={report.rows_failed} "
            f"remaining={report.rows_remaining} "
            f"target_version={report.target_version}"
        )
        if report.rows_failed:
            click.echo(
                f"WARNING: {report.rows_failed} rows failed to rewrap — "
                "see logs for details",
                err=True,
            )
    finally:
        engine.dispose()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/auth/tests/test_rotate_kek_cli.py -v
```

Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/cli_commands.py src/nexus/bricks/auth/tests/test_rotate_kek_cli.py
git commit -m "feat(auth): nexus auth rotate-kek CLI subcommand (#3803)"
```

---

### Task 14: `VaultTransitProvider` (opt-in)

**Files:**
- Create: `src/nexus/bricks/auth/envelope_providers/vault_transit.py`
- Create: `src/nexus/bricks/auth/tests/test_envelope_providers_vault.py`

- [ ] **Step 1: Create the gated test module**

Create `src/nexus/bricks/auth/tests/test_envelope_providers_vault.py`:

```python
"""Vault Transit provider contract tests (issue #3803).

Gated behind @pytest.mark.vault. Requires a running Vault dev server at
VAULT_ADDR with a transit mount and a derived-context key named
``nexus-test``. See docs/guides/auth-envelope-encryption.md for setup.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from nexus.bricks.auth.envelope import EncryptionProvider
from nexus.bricks.auth.tests.test_envelope_contract import EnvelopeProviderContract

pytestmark = [pytest.mark.vault]

VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "root")
VAULT_TRANSIT_KEY = os.environ.get("VAULT_TRANSIT_KEY", "nexus-test")


def _vault_available() -> bool:
    try:
        import hvac  # noqa: F401
    except ImportError:
        return False
    try:
        import hvac

        client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
        return client.sys.is_initialized()
    except Exception:
        return False


pytestmark += [
    pytest.mark.skipif(not _vault_available(), reason="Vault dev server not reachable"),
]


@pytest.fixture()
def provider_factory() -> Callable[[], EncryptionProvider]:
    import hvac

    from nexus.bricks.auth.envelope_providers.vault_transit import VaultTransitProvider

    def _make() -> EncryptionProvider:
        client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
        return VaultTransitProvider(client, key_name=VAULT_TRANSIT_KEY)

    return _make


class TestVaultTransitContract(EnvelopeProviderContract):
    """Runs the shared EncryptionProvider contract suite against Vault Transit."""
```

- [ ] **Step 2: Run the test in a no-Vault environment to confirm it SKIPS (never fails)**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope_providers_vault.py -v
```

Expected: SKIPPED (Vault dev server not reachable) — no test errors.

- [ ] **Step 3: Implement `VaultTransitProvider`**

Create `src/nexus/bricks/auth/envelope_providers/vault_transit.py`:

```python
"""VaultTransitProvider — wraps DEKs via Vault's transit secrets engine.

Requires a derived-context key (``derived=true``) so the per-tenant
``context`` param produces a per-tenant subkey without creating one key per
tenant. See:
  https://developer.hashicorp.com/vault/docs/secrets/transit

Optional dependency: ``hvac``. Install via the ``vault`` extra.
"""

from __future__ import annotations

import base64
import uuid
from typing import TYPE_CHECKING

from nexus.bricks.auth.envelope import (
    EncryptionProvider,
    EnvelopeConfigurationError,
    WrappedDEKInvalid,
)

if TYPE_CHECKING:
    import hvac


class VaultTransitProvider(EncryptionProvider):
    def __init__(
        self,
        vault_client: "hvac.Client",
        key_name: str,
        *,
        mount_point: str = "transit",
    ) -> None:
        try:
            import hvac  # noqa: F401
        except ImportError as exc:  # pragma: no cover — optional dep
            raise EnvelopeConfigurationError(
                "VaultTransitProvider requires the `hvac` package. "
                "Install with: pip install 'nexus[vault]'"
            ) from exc
        self._client = vault_client
        self._key_name = key_name
        self._mount = mount_point
        self._validate_key()

    def _validate_key(self) -> None:
        try:
            resp = self._client.secrets.transit.read_key(
                name=self._key_name, mount_point=self._mount
            )
        except Exception as exc:
            raise EnvelopeConfigurationError(
                f"Vault transit key {self._key_name!r} not readable "
                f"on mount {self._mount!r}: {type(exc).__name__}"
            ) from exc
        data = resp.get("data", {})
        if not data.get("derived"):
            raise EnvelopeConfigurationError(
                f"Vault transit key {self._key_name!r} must have derived=true. "
                f"Run: vault write -f {self._mount}/keys/{self._key_name} derived=true"
            )

    def current_version(self, *, tenant_id: uuid.UUID) -> int:
        resp = self._client.secrets.transit.read_key(
            name=self._key_name, mount_point=self._mount
        )
        data = resp.get("data", {})
        return int(data.get("latest_version", 1))

    def _context_b64(self, tenant_id: uuid.UUID) -> str:
        return base64.b64encode(str(tenant_id).encode("utf-8")).decode("ascii")

    def wrap_dek(
        self,
        dek: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
    ) -> tuple[bytes, int]:
        resp = self._client.secrets.transit.encrypt_data(
            name=self._key_name,
            plaintext=base64.b64encode(dek).decode("ascii"),
            context=self._context_b64(tenant_id),
            mount_point=self._mount,
        )
        ciphertext = resp["data"]["ciphertext"]  # "vault:v<N>:<base64>"
        version = int(ciphertext.split(":")[1].lstrip("v"))
        return ciphertext.encode("utf-8"), version

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
        kek_version: int,
    ) -> bytes:
        try:
            resp = self._client.secrets.transit.decrypt_data(
                name=self._key_name,
                ciphertext=wrapped.decode("utf-8"),
                context=self._context_b64(tenant_id),
                mount_point=self._mount,
            )
        except Exception as exc:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause=f"Vault transit decrypt rejected: {type(exc).__name__}",
            ) from exc
        return base64.b64decode(resp["data"]["plaintext"])


__all__ = ["VaultTransitProvider"]
```

- [ ] **Step 4: Re-run the gated test (still skipped unless Vault is reachable)**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope_providers_vault.py -v
```

Expected: SKIPPED when no Vault; when Vault reachable with a `nexus-test` transit key (`derived=true`), 3 contract tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/envelope_providers/vault_transit.py src/nexus/bricks/auth/tests/test_envelope_providers_vault.py
git commit -m "feat(auth): VaultTransitProvider (opt-in) (#3803)"
```

---

### Task 15: `AwsKmsProvider` (opt-in)

**Files:**
- Create: `src/nexus/bricks/auth/envelope_providers/aws_kms.py`
- Create: `src/nexus/bricks/auth/tests/test_envelope_providers_aws_kms.py`

- [ ] **Step 1: Create the gated test module**

```python
"""AWS KMS provider contract tests (issue #3803).

Gated behind @pytest.mark.kms. Requires a reachable KMS endpoint (real AWS or
LocalStack) with a CMK accessible via AWS_KMS_KEY_ID.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from nexus.bricks.auth.envelope import EncryptionProvider
from nexus.bricks.auth.tests.test_envelope_contract import EnvelopeProviderContract

pytestmark = [pytest.mark.kms]

KMS_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL")  # e.g. http://localhost:4566
KMS_REGION = os.environ.get("AWS_REGION", "us-east-1")
KMS_KEY_ID = os.environ.get("AWS_KMS_KEY_ID")


def _kms_available() -> bool:
    if not KMS_KEY_ID:
        return False
    try:
        import boto3  # noqa: F401
    except ImportError:
        return False
    try:
        import boto3

        kwargs: dict = {"region_name": KMS_REGION}
        if KMS_ENDPOINT:
            kwargs["endpoint_url"] = KMS_ENDPOINT
        client = boto3.client("kms", **kwargs)
        client.describe_key(KeyId=KMS_KEY_ID)
        return True
    except Exception:
        return False


pytestmark += [
    pytest.mark.skipif(
        not _kms_available(),
        reason="AWS KMS not reachable or AWS_KMS_KEY_ID unset",
    ),
]


@pytest.fixture()
def provider_factory() -> Callable[[], EncryptionProvider]:
    import boto3

    from nexus.bricks.auth.envelope_providers.aws_kms import AwsKmsProvider

    def _make() -> EncryptionProvider:
        kwargs: dict = {"region_name": KMS_REGION}
        if KMS_ENDPOINT:
            kwargs["endpoint_url"] = KMS_ENDPOINT
        kms = boto3.client("kms", **kwargs)
        return AwsKmsProvider(kms, key_id=KMS_KEY_ID)

    return _make


class TestAwsKmsContract(EnvelopeProviderContract):
    """Runs the shared EncryptionProvider contract suite against AWS KMS."""
```

- [ ] **Step 2: Confirm SKIP in the default env**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope_providers_aws_kms.py -v
```

Expected: SKIPPED.

- [ ] **Step 3: Implement `AwsKmsProvider`**

Create `src/nexus/bricks/auth/envelope_providers/aws_kms.py`:

```python
"""AwsKmsProvider — wraps DEKs via AWS KMS.

Uses ``EncryptionContext`` to bind the wrap to ``(tenant_id, aad_fingerprint)``
— any cross-row DEK reuse or tampered AAD fails at KMS rather than only
at the AES-GCM layer locally. Optional dependency: ``boto3``.

AWS KMS automatically rotates the key material behind a CMK annually when
``EnableKeyRotation`` is set. Our ``kek_version`` then tracks provider-config
changes (e.g. swapping the CMK alias) rather than AWS key-material rotation.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING

from nexus.bricks.auth.envelope import (
    EncryptionProvider,
    EnvelopeConfigurationError,
    WrappedDEKInvalid,
)

if TYPE_CHECKING:
    import botocore.client


class AwsKmsProvider(EncryptionProvider):
    def __init__(
        self,
        kms_client: "botocore.client.BaseClient",
        key_id: str,
        *,
        config_version: int = 1,
    ) -> None:
        self._kms = kms_client
        self._key_id = key_id
        self._config_version = config_version
        self._validate_key()

    def _validate_key(self) -> None:
        try:
            self._kms.describe_key(KeyId=self._key_id)
        except Exception as exc:
            raise EnvelopeConfigurationError(
                f"AWS KMS key {self._key_id!r} not accessible: "
                f"{type(exc).__name__}. Grant kms:Encrypt, kms:Decrypt, kms:DescribeKey."
            ) from exc

    @staticmethod
    def _context(tenant_id: uuid.UUID, aad: bytes) -> dict[str, str]:
        return {
            "tenant_id": str(tenant_id),
            "aad_fingerprint": hashlib.sha256(aad).hexdigest(),
        }

    def current_version(self, *, tenant_id: uuid.UUID) -> int:
        return self._config_version

    def wrap_dek(
        self, dek: bytes, *, tenant_id: uuid.UUID, aad: bytes
    ) -> tuple[bytes, int]:
        resp = self._kms.encrypt(
            KeyId=self._key_id,
            Plaintext=dek,
            EncryptionContext=self._context(tenant_id, aad),
        )
        return resp["CiphertextBlob"], self._config_version

    def unwrap_dek(
        self,
        wrapped: bytes,
        *,
        tenant_id: uuid.UUID,
        aad: bytes,
        kek_version: int,
    ) -> bytes:
        try:
            resp = self._kms.decrypt(
                CiphertextBlob=wrapped,
                EncryptionContext=self._context(tenant_id, aad),
            )
        except Exception as exc:
            raise WrappedDEKInvalid.from_row(
                tenant_id=tenant_id,
                profile_id="<unknown>",
                kek_version=kek_version,
                cause=f"KMS decrypt rejected: {type(exc).__name__}",
            ) from exc
        return resp["Plaintext"]


__all__ = ["AwsKmsProvider"]
```

- [ ] **Step 4: Run the gated test again**

```bash
pytest src/nexus/bricks/auth/tests/test_envelope_providers_aws_kms.py -v
```

Expected: SKIPPED unless KMS is reachable.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/envelope_providers/aws_kms.py src/nexus/bricks/auth/tests/test_envelope_providers_aws_kms.py
git commit -m "feat(auth): AwsKmsProvider (opt-in) (#3803)"
```

---

### Task 16: Deployment guide (Vault vs KMS)

**Files:**
- Create: `docs/guides/auth-envelope-encryption.md`

- [ ] **Step 1: Write the guide**

```markdown
# Envelope encryption for auth profiles

`PostgresAuthProfileStore` supports storing resolved credentials encrypted
alongside their routing metadata. This guide covers picking and configuring an
`EncryptionProvider` for your deployment. Issue: #3803. Spec:
`docs/superpowers/specs/2026-04-18-issue-3803-envelope-encryption-design.md`.

## Pick a provider

| Deployment shape | Provider | Rationale |
|---|---|---|
| Vault-native shop | `VaultTransitProvider` | Free self-hosted transit engine; per-tenant scoping via `context` parameter with `derived=true`. |
| AWS-native shop | `AwsKmsProvider` | Managed CMK; AWS-managed rotation; IAM-scoped access; per-tenant CMK keeps blast radius per-tenant. |
| Development only | `InMemoryEncryptionProvider` | Keys live in process memory — never use in production. |

## Vault Transit setup

1. Enable the transit mount: `vault secrets enable transit`
2. Create a derived-context key: `vault write -f transit/keys/nexus derived=true`
3. Grant the Nexus role the `encrypt` and `decrypt` policies on `transit/*/nexus`.
4. Construct the provider: `VaultTransitProvider(hvac.Client(...), key_name="nexus")`.

Rotation:

1. `vault write -f transit/keys/nexus/rotate` — bumps the key version.
2. `nexus auth rotate-kek --tenant acme --apply` — per tenant, sweeps rows at the old version.

## AWS KMS setup

1. Create a CMK per tenant (customer-managed key). Enable automatic key rotation: `aws kms enable-key-rotation --key-id <id>`.
2. Grant the Nexus IAM principal `kms:Encrypt`, `kms:Decrypt`, `kms:DescribeKey` on the CMK.
3. Constrain via `kms:EncryptionContext:tenant_id` in the key policy to match the tenant value the provider sends — prevents cross-tenant decrypt even if IAM is too broad.
4. Construct the provider: `AwsKmsProvider(boto3.client("kms"), key_id="arn:aws:kms:...")`.

Rotation: AWS rotates the underlying key material annually with `EnableKeyRotation`. Our `kek_version` in that case tracks provider-config changes (e.g. swapping the CMK alias) — `nexus auth rotate-kek` is a no-op as long as `config_version` is unchanged.

## Rotation CLI

```
nexus auth rotate-kek --db-url <url> --tenant <name> [--apply] [--batch-size 100] [--max-rows N]
```

Dry-run by default: reports how many rows are stuck at old `kek_version`. `--apply` rewraps them in `SKIP LOCKED` batches — resumable, doesn't block concurrent writers.

## Metrics

Provider-level metrics exposed under `/metrics`:

- `auth_dek_cache_hits_total{tenant_id}`
- `auth_dek_cache_misses_total{tenant_id}`
- `auth_dek_unwrap_errors_total{tenant_id,error_class}`
- `auth_dek_unwrap_latency_seconds{tenant_id}`
- `auth_kek_rotate_rows_total{tenant_id,from_version,to_version}`
```

- [ ] **Step 2: Commit**

```bash
git add docs/guides/auth-envelope-encryption.md
git commit -m "docs(auth): envelope encryption deployment guide (#3803)"
```

---

### Task 17: Final acceptance sweep + full test run

**Files:** none (verification only)

- [ ] **Step 1: Run every new/touched test module**

```bash
pytest \
  src/nexus/bricks/auth/tests/test_envelope.py \
  src/nexus/bricks/auth/tests/test_envelope_contract.py \
  src/nexus/bricks/auth/tests/test_postgres_envelope_integration.py \
  src/nexus/bricks/auth/tests/test_rotate_kek_cli.py \
  src/nexus/bricks/auth/tests/test_envelope_providers_vault.py \
  src/nexus/bricks/auth/tests/test_envelope_providers_aws_kms.py \
  -v
```

Expected: all pass except Vault/KMS tests (SKIPPED unless the corresponding dev endpoint is running).

- [ ] **Step 2: Run the full auth brick suite to confirm zero regressions**

```bash
pytest src/nexus/bricks/auth/tests/ -v
```

Expected: all pass; pre-existing `test_postgres_profile_store.py` still green.

- [ ] **Step 3: Verify CLI surface**

```bash
python -c "from nexus.bricks.auth.cli_commands import auth; \
import click; \
ctx = click.Context(auth); \
print([c for c in auth.list_commands(ctx)])"
```

Expected output contains both `migrate-to-postgres` and `rotate-kek`.

- [ ] **Step 4: Dry-run the CLI end-to-end against a running Postgres**

Prerequisites: `docker compose -f dockerfiles/compose.yaml up postgres -d`

```bash
# Seed a tenant + a single encrypted row for smoke, then dry-run
python -c "
import os, uuid
from sqlalchemy import create_engine
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore, ensure_schema, ensure_tenant, ensure_principal,
)
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.tests.conftest import make_profile

url = 'postgresql+psycopg2://postgres:nexus@localhost:5432/nexus'
engine = create_engine(url, future=True)
ensure_schema(engine)
t = ensure_tenant(engine, 'smoke-tenant')
p = ensure_principal(engine, tenant_id=t, external_sub='sub-x', auth_method='smoke')
prov = InMemoryEncryptionProvider()
store = PostgresAuthProfileStore(url, tenant_id=t, principal_id=p, engine=engine, encryption_provider=prov)
store.upsert_with_credential(make_profile('smoke-id'), ResolvedCredential(kind='api_key', api_key='k'))
prov.rotate()
"

# Expect: 1 stale row at kek_version=1
NEXUS_AUTH_ROTATE_KEK_TEST_PROVIDER_ID=smoke \
python -c "
from nexus.bricks.auth.cli_commands import _TEST_PROVIDER_REGISTRY
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
# Register a fresh provider (won't match the seeded one — expected to report all rows stale since current_version=1)
_TEST_PROVIDER_REGISTRY['smoke'] = InMemoryEncryptionProvider
"
```

(This step is informational — the test module already covers dry-run; the point is to have run the CLI against a real DB once.)

- [ ] **Step 5: No commit — this task is verification only**

---

## Self-Review

### Spec coverage

| Spec section | Task(s) |
|---|---|
| `EncryptionProvider` Protocol with `wrap_dek/unwrap_dek`, `current_version`, tenant-scoped | Task 1 (trait) + Task 4 (first impl) |
| `CredentialCarryingProfileStore` sub-protocol | Task 7 |
| `AESGCMEnvelope` + DEK-per-row + fresh nonce | Task 1 |
| `DEKCache` keyed by `sha256(wrapped_dek)`, TTL + LRU, no-negative-cache | Task 3 |
| `EnvelopeError` hierarchy with no-plaintext repr | Tasks 1 + 2 |
| `InMemoryEncryptionProvider` test fake | Task 4 |
| Shared contract suite | Task 5 (in-memory) + Tasks 14/15 (Vault/KMS re-param) |
| Prometheus metrics module | Task 6 |
| Schema delta (5 nullable cols + CHECK) + upgrade path | Task 8 |
| `upsert_with_credential` / `get_with_credential` with provider = None default, AAD verify, cache lookup | Task 9 |
| Swap-attack test, PR 1 compat test, mixed-version test, cache-amortization test, AAD-tamper test, CHECK-violation test | Tasks 8 + 9 + 10 |
| `rotate_kek_for_tenant` + `RotationReport` + `SKIP LOCKED` | Task 11 |
| Per-row rotation failure semantics | Task 12 |
| CLI `nexus auth rotate-kek` dry-run + apply | Task 13 |
| `VaultTransitProvider` with `derived=true` validation | Task 14 |
| `AwsKmsProvider` with `EncryptionContext` | Task 15 |
| Deployment docs | Task 16 |

### Type consistency spot-check

- `EncryptionProvider.wrap_dek` → `tuple[bytes, int]` everywhere (Task 1 defines, Tasks 4/11/14/15 match).
- `DEKCache.make_key` signature is consistent (Task 3 defines, Task 9 calls).
- `RotationReport` fields (`rows_rewrapped`, `rows_failed`, `rows_remaining`, `target_version`) match between Task 11 (definition) and Tasks 12/13 (consumers).
- `AADMismatch` / `WrappedDEKInvalid` catch sites in Task 10 (swap test) match raise sites in Tasks 1 and 4.

### Placeholder scan

No "TBD", no "implement later", no "similar to Task N" — every step shows the code it needs.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-18-issue-3803-envelope-encryption.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
