# Issue #3818 — Server-side Credential Consumption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the read path for the multi-tenant Postgres-backed auth epic so that `/v1/auth/token-exchange` (RFC 8693) accepts a daemon JWT, decrypts the matching `auth_profiles` envelope, materializes a provider-native bearer (AWS or GitHub), writes a read-audit row, and returns the credential to the caller.

**Architecture:** New `CredentialConsumer` orchestrator inside `bricks/auth/` reuses the existing `EncryptionProvider`, `DEKCache`, `JwtSigner`, and `PostgresAuthProfileStore` infra from PRs #3802/#3809/#3816. Two provider adapters (AWS, GitHub) handle envelope-payload-shape decoding. A new `auth_profile_reads` partitioned table records every credential access (100% on cache-miss, 1% sample on cache-hit). The `token_exchange.py` router gets gutted and rewritten to wire JWT verify → consumer → audit → response.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x, PyJWT (ES256), `cryptography` (AES-256-GCM, already used by envelope), pytest, prometheus-client. Test infra adds `localstack` and `responses` (mocking) on top of existing patterns.

**Spec:** `docs/superpowers/specs/2026-04-23-issue-3818-server-credential-consumption-design.md`

---

## File structure

**Create:**
- `src/nexus/bricks/auth/consumer.py` — `MaterializedCredential` dataclass, `CredentialConsumer.resolve()`, `ConsumerError` taxonomy
- `src/nexus/bricks/auth/consumer_cache.py` — `ResolvedCredCache` with TTL = `min(300, expires_at-60)`
- `src/nexus/bricks/auth/consumer_providers/__init__.py` — `default_adapters()` registry
- `src/nexus/bricks/auth/consumer_providers/base.py` — `ProviderAdapter` Protocol
- `src/nexus/bricks/auth/consumer_providers/aws.py` — AWS payload → `MaterializedCredential`
- `src/nexus/bricks/auth/consumer_providers/github.py` — GitHub payload → `MaterializedCredential`
- `src/nexus/bricks/auth/read_audit.py` — `ReadAuditWriter` w/ 1% cache-hit sampling
- `src/nexus/bricks/auth/consumer_metrics.py` — Prometheus counters/histograms
- `src/nexus/bricks/auth/tests/test_consumer.py`
- `src/nexus/bricks/auth/tests/test_consumer_cache.py`
- `src/nexus/bricks/auth/tests/test_consumer_providers_aws.py`
- `src/nexus/bricks/auth/tests/test_consumer_providers_github.py`
- `src/nexus/bricks/auth/tests/test_read_audit.py`
- `src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py`
- `tests/e2e/auth_consumption/__init__.py`
- `tests/e2e/auth_consumption/test_s3_as_user.py`
- `tests/e2e/auth_consumption/test_github_as_user.py`

**Modify:**
- `src/nexus/bricks/auth/postgres_profile_store.py` — add `decrypt_profile()` method, append `auth_profile_reads` to `_DDL_STATEMENTS` + `_RLS_STATEMENTS`
- `src/nexus/server/api/v1/routers/token_exchange.py` — full rewrite (was 501 stub)
- `src/nexus/server/api/v1/tests/test_token_exchange_router.py` — full rewrite
- `src/nexus/server/fastapi_server.py` — pass new deps to `make_token_exchange_router`

---

## Task 1: Add `auth_profile_reads` schema (DDL only)

**Files:**
- Modify: `src/nexus/bricks/auth/postgres_profile_store.py` (append to `_DDL_STATEMENTS` near line 261; append to `_RLS_STATEMENTS` near line 280)
- Modify: `src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py` (create)

- [ ] **Step 1.1: Write the failing test**

Create `src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py`:

```python
"""Integration tests for read-path additions to PostgresAuthProfileStore (#3818).

Requires a running Postgres (env: NEXUS_TEST_DATABASE_URL). Skip cleanly when absent.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def test_auth_profile_reads_table_exists(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'auth_profile_reads' ORDER BY ordinal_position"
            )
        ).fetchall()
    cols = [r[0] for r in rows]
    assert cols == [
        "id",
        "read_at",
        "tenant_id",
        "principal_id",
        "auth_profile_id",
        "caller_machine_id",
        "caller_kind",
        "provider",
        "purpose",
        "cache_hit",
        "kek_version",
    ]


def test_auth_profile_reads_has_rls_enabled(engine):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'auth_profile_reads'"
            )
        ).fetchone()
    assert row == (True, True)
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py -v`
Expected: FAIL with column list mismatch (table doesn't exist or missing columns).

- [ ] **Step 1.3: Append the DDL**

In `src/nexus/bricks/auth/postgres_profile_store.py`, append to `_DDL_STATEMENTS` tuple (immediately after the `auth_profile_writes` index entry near line 260; insertion ordered before the closing `)`):

```python
    """
    CREATE TABLE IF NOT EXISTS auth_profile_reads (
        id                BIGSERIAL,
        read_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        tenant_id         UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        principal_id      UUID NOT NULL,
        auth_profile_id   TEXT NOT NULL,
        caller_machine_id UUID NOT NULL,
        caller_kind       TEXT NOT NULL,
        provider          TEXT NOT NULL,
        purpose           TEXT NOT NULL,
        cache_hit         BOOLEAN NOT NULL,
        kek_version       INTEGER NOT NULL,
        PRIMARY KEY (read_at, id)
    ) PARTITION BY RANGE (read_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_profile_reads_default
        PARTITION OF auth_profile_reads DEFAULT
    """,
    "CREATE INDEX IF NOT EXISTS idx_auth_profile_reads_tenant_principal_provider "
    "ON auth_profile_reads(tenant_id, principal_id, provider, read_at DESC)",
```

In the `_RLS_STATEMENTS` tuple (around line 280, after the `auth_profile_writes` FORCE entry):

```python
    "ALTER TABLE auth_profile_reads ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE auth_profile_reads FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY auth_profile_reads_tenant_isolation ON auth_profile_reads
        USING (tenant_id = current_setting('app.current_tenant')::UUID)
    """,
```

Note: the `CREATE POLICY` may already exist on a re-run — wrap it in a `DO $$ BEGIN ... EXCEPTION WHEN duplicate_object THEN NULL; END $$` block matching whatever pattern PR 3 used for the other `CREATE POLICY` statements (search for `CREATE POLICY auth_profile_writes` to copy the same idempotency wrapper).

- [ ] **Step 1.4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py -v`
Expected: PASS (both tests).

- [ ] **Step 1.5: Commit**

```bash
git add src/nexus/bricks/auth/postgres_profile_store.py src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py
git commit -m "feat(#3818): add auth_profile_reads partitioned table + RLS"
```

---

## Task 2: `decrypt_profile()` helper on `PostgresAuthProfileStore`

**Files:**
- Modify: `src/nexus/bricks/auth/postgres_profile_store.py` (append a method to the class)
- Modify: `src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py` (extend)

- [ ] **Step 2.1: Write the failing test**

Append to `test_postgres_decrypt_integration.py`:

```python
from datetime import UTC, datetime, timedelta

from nexus.bricks.auth.envelope import (
    AESGCMEnvelope,
    DEKCache,
)
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider


def _seed_envelope_row(
    *,
    engine,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    profile_id: str,
    provider: str,
    plaintext: bytes,
    encryption,
):
    """Helper: seed a fully-formed envelope row for decrypt tests."""
    aad = (
        str(tenant_id).encode() + b"|" + str(principal_id).encode() + b"|" + profile_id.encode()
    )
    dek = b"\x00" * 32  # AES-256 zero key — fine for an in-memory test fake
    nonce, ciphertext = AESGCMEnvelope().encrypt(dek, plaintext, aad=aad)
    wrapped, kek_version = encryption.wrap_dek(dek, tenant_id=tenant_id, aad=aad)
    with engine.begin() as conn:
        conn.execute(
            text("SET LOCAL app.current_tenant = :t"),
            {"t": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO tenants (id, name) VALUES (:id, :n) ON CONFLICT DO NOTHING"
            ),
            {"id": str(tenant_id), "n": "test"},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal_id), "t": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, :id, :prov, 'acct', 'envelope', 'k', NOW(), 300, "
                " :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant_id),
                "p": str(principal_id),
                "id": profile_id,
                "prov": provider,
                "ct": ciphertext,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kek_version,
            },
        )


def test_decrypt_profile_returns_plaintext_and_kek_version(engine):
    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    plaintext = b'{"token":"ghp_test"}'
    _seed_envelope_row(
        engine=engine,
        tenant_id=tenant_id,
        principal_id=principal_id,
        profile_id="github-default",
        provider="github",
        plaintext=plaintext,
        encryption=encryption,
    )

    store = PostgresAuthProfileStore(engine=engine, tenant_id=tenant_id)
    out = store.decrypt_profile(
        principal_id=principal_id,
        provider="github",
        encryption=encryption,
        dek_cache=DEKCache(),
    )

    assert out.plaintext == plaintext
    assert out.profile_id == "github-default"
    assert out.kek_version == 1
    assert out.last_synced_at is not None


def test_decrypt_profile_raises_profile_not_found(engine):
    from nexus.bricks.auth.postgres_profile_store import ProfileNotFound

    tenant_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    # Seed tenant but no profile
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'tx') ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id)},
        )

    store = PostgresAuthProfileStore(engine=engine, tenant_id=tenant_id)
    encryption = InMemoryEncryptionProvider()
    with pytest.raises(ProfileNotFound):
        store.decrypt_profile(
            principal_id=principal_id,
            provider="aws",
            encryption=encryption,
            dek_cache=DEKCache(),
        )
```

- [ ] **Step 2.2: Run to verify failure**

Run: `pytest src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py::test_decrypt_profile_returns_plaintext_and_kek_version -v`
Expected: FAIL with `AttributeError: PostgresAuthProfileStore has no attribute 'decrypt_profile'`.

- [ ] **Step 2.3: Implement `decrypt_profile()`**

In `src/nexus/bricks/auth/postgres_profile_store.py`:

Add near the top (alongside `CrossPrincipalConflict`):

```python
class ProfileNotFound(Exception):
    """No envelope-carrying auth_profile row for (tenant, principal, provider)."""

    def __init__(self, *, tenant_id: uuid.UUID, principal_id: uuid.UUID, provider: str):
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        self.provider = provider
        super().__init__(
            f"no auth_profile row tenant={tenant_id} principal={principal_id} "
            f"provider={provider}"
        )


@dataclass(frozen=True)
class DecryptedProfile:
    """Output of ``PostgresAuthProfileStore.decrypt_profile``.

    ``plaintext`` is the daemon-pushed envelope payload (provider-specific JSON).
    Caller (CredentialConsumer) hands this to the matching ProviderAdapter.

    ``last_synced_at`` lets the consumer return 409 stale_source when the row
    is older than ``sync_ttl_seconds``.
    """

    plaintext: bytes
    profile_id: str
    kek_version: int
    last_synced_at: datetime
    sync_ttl_seconds: int
```

Add a method on `PostgresAuthProfileStore` (locate the class body — append after the existing `upsert_with_credential` method):

```python
    def decrypt_profile(
        self,
        *,
        principal_id: uuid.UUID,
        provider: str,
        encryption: EncryptionProvider,
        dek_cache: DEKCache,
    ) -> DecryptedProfile:
        """Decrypt the envelope row matching (tenant, principal, provider).

        Selects the most-recently-updated row for that triple (the daemon may
        have pushed multiple over time; we always read newest). Raises
        ``ProfileNotFound`` if no row exists.

        DEK is unwrapped via ``encryption.unwrap_dek`` with cache-through on
        ``dek_cache``. AES-GCM decrypt failures bubble as ``EnvelopeError``
        subclasses (no plaintext in repr).
        """
        with self._tenant_scoped_connection() as conn:
            row = conn.execute(
                text(
                    "SELECT id, ciphertext, wrapped_dek, nonce, aad, kek_version, "
                    "       last_synced_at, sync_ttl_seconds "
                    "FROM auth_profiles "
                    "WHERE tenant_id = :t AND principal_id = :p AND provider = :prov "
                    "  AND ciphertext IS NOT NULL "
                    "ORDER BY updated_at DESC LIMIT 1"
                ),
                {"t": str(self._tenant_id), "p": str(principal_id), "prov": provider},
            ).fetchone()

        if row is None:
            raise ProfileNotFound(
                tenant_id=self._tenant_id,
                principal_id=principal_id,
                provider=provider,
            )

        profile_id, ciphertext, wrapped_dek, nonce, aad, kek_version, lsa, sttl = row
        cache_key = DEKCache.make_key(
            tenant_id=self._tenant_id,
            kek_version=kek_version,
            wrapped_dek=bytes(wrapped_dek),
        )
        dek = dek_cache.get(cache_key)
        if dek is None:
            dek = encryption.unwrap_dek(
                bytes(wrapped_dek),
                tenant_id=self._tenant_id,
                aad=bytes(aad),
                kek_version=kek_version,
            )
            dek_cache.put(cache_key, dek)

        plaintext = AESGCMEnvelope().decrypt(
            dek, bytes(nonce), bytes(ciphertext), aad=bytes(aad)
        )
        return DecryptedProfile(
            plaintext=plaintext,
            profile_id=profile_id,
            kek_version=kek_version,
            last_synced_at=lsa,
            sync_ttl_seconds=sttl,
        )
```

If `_tenant_scoped_connection()` doesn't exist by that name, search the file for the existing pattern (look for `SET LOCAL app.current_tenant`) and reuse the same context-manager helper. If it's inlined elsewhere, factor it out as a private method on the class (`@contextmanager def _tenant_scoped_connection(self)`) so this new method and existing methods share one path.

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `pytest src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py -v`
Expected: all tests PASS.

- [ ] **Step 2.5: Commit**

```bash
git add src/nexus/bricks/auth/postgres_profile_store.py src/nexus/bricks/auth/tests/test_postgres_decrypt_integration.py
git commit -m "feat(#3818): add decrypt_profile() helper on PostgresAuthProfileStore"
```

---

## Task 3: `MaterializedCredential` + `ProviderAdapter` Protocol

**Files:**
- Create: `src/nexus/bricks/auth/consumer.py`
- Create: `src/nexus/bricks/auth/consumer_providers/__init__.py`
- Create: `src/nexus/bricks/auth/consumer_providers/base.py`

This task lays down the typed contracts. No tests yet — these are pure Protocol/dataclass definitions; they'll be exercised by Tasks 4-7.

- [ ] **Step 3.1: Create `consumer.py` with `MaterializedCredential`**

```python
"""CredentialConsumer: server-side read path for envelope-encrypted auth profiles (#3818).

The consumer is the orchestrator that ties together:
  - PostgresAuthProfileStore.decrypt_profile() — envelope → plaintext
  - ProviderAdapter.materialize() — plaintext → MaterializedCredential
  - ResolvedCredCache — TTL = min(300, expires_at - 60)
  - ReadAuditWriter — auth_profile_reads row per resolve

Callers: ``/v1/auth/token-exchange`` router (wire path), and any in-process
server-side agent that needs to act as a user.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.auth.consumer_cache import ResolvedCredCache
    from nexus.bricks.auth.consumer_providers.base import ProviderAdapter
    from nexus.bricks.auth.envelope import DEKCache, EncryptionProvider
    from nexus.bricks.auth.postgres_profile_store import PostgresAuthProfileStore
    from nexus.bricks.auth.read_audit import ReadAuditWriter
    from nexus.server.api.v1.jwt_signer import DaemonClaims


@dataclass(frozen=True)
class MaterializedCredential:
    """Provider-native credential ready for the wire / in-process use.

    ``access_token`` is the time-bounded part (AWS session_token, GitHub PAT).
    For multi-part credentials (AWS), ``metadata`` carries the static parts
    (access_key_id, secret_access_key, region, account_id) — the wire response
    surfaces these under ``nexus_credential_metadata``.

    ``__repr__`` masks ``access_token`` to keep it out of logs / tracebacks.
    """

    provider: str
    access_token: str
    expires_at: datetime | None
    metadata: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"MaterializedCredential(provider={self.provider!r}, "
            f"access_token='***', expires_at={self.expires_at!r}, "
            f"metadata_keys={sorted(self.metadata)!r})"
        )


# ---------------------------------------------------------------------------
# Error taxonomy — no plaintext / token bytes ever in repr
# ---------------------------------------------------------------------------


class ConsumerError(Exception):
    """Root of every CredentialConsumer error."""

    def __init__(
        self,
        message: str,
        *,
        tenant_id: uuid.UUID | None = None,
        principal_id: uuid.UUID | None = None,
        provider: str | None = None,
        cause: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        self.provider = provider
        self.cause = cause

    @classmethod
    def from_row(
        cls,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        provider: str,
        cause: str,
    ) -> "ConsumerError":
        return cls(
            f"{cls.__name__} tenant={tenant_id} principal={principal_id} "
            f"provider={provider} cause={cause}",
            tenant_id=tenant_id,
            principal_id=principal_id,
            provider=provider,
            cause=cause,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(tenant_id={self.tenant_id!s}, "
            f"principal_id={self.principal_id!s}, provider={self.provider!r}, "
            f"cause={self.cause!r})"
        )


class ProfileNotFoundForCaller(ConsumerError):
    """Tenant/principal/provider triple has no envelope row."""


class ProviderNotConfigured(ConsumerError):
    """No ProviderAdapter registered for this provider name."""


class StaleSource(ConsumerError):
    """Envelope row's ``last_synced_at`` is past ``sync_ttl_seconds`` —
    daemon is offline; caller should retry once daemon catches up.
    """


class AdapterMaterializeFailed(ConsumerError):
    """Provider adapter raised while decoding the envelope payload."""


# ---------------------------------------------------------------------------
# CredentialConsumer (orchestrator) — implementation in Task 7
# ---------------------------------------------------------------------------


class CredentialConsumer:
    """Implementation lands in Task 7. Type-only declaration here so other
    modules can import the symbol without circular references.
    """
```

- [ ] **Step 3.2: Create `consumer_providers/base.py`**

```python
"""ProviderAdapter Protocol — provider-specific envelope-payload decoders.

Each adapter is pure deserialization: takes envelope plaintext bytes (JSON,
provider-shape), returns a ``MaterializedCredential``. No network calls, no
state. Adapters are registered in ``consumer_providers/__init__.py``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus.bricks.auth.consumer import MaterializedCredential


@runtime_checkable
class ProviderAdapter(Protocol):
    """Pure-function interface: envelope plaintext → MaterializedCredential."""

    name: str  # "aws" | "github" | future providers

    def materialize(self, plaintext_payload: bytes) -> MaterializedCredential:
        """Decode envelope plaintext into a MaterializedCredential.

        Raises:
            ValueError | KeyError on malformed payload — CredentialConsumer
            wraps these as AdapterMaterializeFailed before exiting.
        """
        ...
```

- [ ] **Step 3.3: Create `consumer_providers/__init__.py`**

```python
"""Provider adapter registry. Updated by each adapter task."""

from __future__ import annotations

from nexus.bricks.auth.consumer_providers.base import ProviderAdapter


def default_adapters() -> dict[str, ProviderAdapter]:
    """Return the adapter registry used by CredentialConsumer.

    Adapters are imported lazily so missing optional deps (e.g. AWS payload
    parsing only needs stdlib ``json``, but future providers may need boto3)
    don't cascade-break unrelated code paths.
    """
    from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter
    from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter

    return {
        AwsProviderAdapter.name: AwsProviderAdapter(),
        GithubProviderAdapter.name: GithubProviderAdapter(),
    }
```

(`AwsProviderAdapter` and `GithubProviderAdapter` are added in Tasks 4 and 5; the import will fail until then. That's fine — `default_adapters()` is only called from the consumer wiring in Task 7.)

- [ ] **Step 3.4: Verify it imports**

Run: `python -c "from nexus.bricks.auth.consumer import MaterializedCredential, ConsumerError, ProfileNotFoundForCaller, ProviderNotConfigured, StaleSource, AdapterMaterializeFailed; print('OK')"`
Expected: `OK`.

- [ ] **Step 3.5: Commit**

```bash
git add src/nexus/bricks/auth/consumer.py src/nexus/bricks/auth/consumer_providers/__init__.py src/nexus/bricks/auth/consumer_providers/base.py
git commit -m "feat(#3818): add MaterializedCredential + ConsumerError taxonomy + adapter Protocol"
```

---

## Task 4: AWS provider adapter

**Files:**
- Create: `src/nexus/bricks/auth/consumer_providers/aws.py`
- Create: `src/nexus/bricks/auth/tests/test_consumer_providers_aws.py`

- [ ] **Step 4.1: Write the failing test**

```python
"""Tests for AwsProviderAdapter — pure JSON → MaterializedCredential decoding (#3818)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter


def test_materialize_extracts_session_token_and_metadata():
    payload = json.dumps(
        {
            "access_key_id": "ASIA1234",
            "secret_access_key": "wJalrXUtnFEMI",
            "session_token": "FwoGZXIvYXdz...",
            "expiration": "2026-04-23T18:00:00+00:00",
            "region": "us-west-2",
            "account_id": "123456789012",
        }
    ).encode()

    out = AwsProviderAdapter().materialize(payload)

    assert out.provider == "aws"
    assert out.access_token == "FwoGZXIvYXdz..."
    assert out.expires_at == datetime(2026, 4, 23, 18, 0, 0, tzinfo=UTC)
    assert out.metadata == {
        "access_key_id": "ASIA1234",
        "secret_access_key": "wJalrXUtnFEMI",
        "region": "us-west-2",
        "account_id": "123456789012",
    }


def test_materialize_handles_missing_optional_fields():
    payload = json.dumps(
        {
            "access_key_id": "ASIA1234",
            "secret_access_key": "wJalrXUtnFEMI",
            "session_token": "tok",
            "expiration": "2026-04-23T18:00:00+00:00",
            "region": "us-east-1",
        }
    ).encode()
    out = AwsProviderAdapter().materialize(payload)
    assert "account_id" not in out.metadata


def test_materialize_rejects_malformed_json():
    with pytest.raises(ValueError):
        AwsProviderAdapter().materialize(b"not json")


def test_materialize_rejects_missing_required_field():
    payload = json.dumps({"access_key_id": "x"}).encode()
    with pytest.raises(KeyError):
        AwsProviderAdapter().materialize(payload)


def test_repr_masks_access_token():
    payload = json.dumps(
        {
            "access_key_id": "ASIA1234",
            "secret_access_key": "wJalrXUtnFEMI",
            "session_token": "supersecret",
            "expiration": "2026-04-23T18:00:00+00:00",
            "region": "us-east-1",
        }
    ).encode()
    out = AwsProviderAdapter().materialize(payload)
    assert "supersecret" not in repr(out)
    assert "***" in repr(out)
```

- [ ] **Step 4.2: Run to verify failure**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer_providers_aws.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4.3: Implement `aws.py`**

```python
"""AWS provider adapter — daemon-pushed STS payload → MaterializedCredential (#3818).

Payload shape (JSON, daemon-pushed by aws sso login / aws sts get-caller-identity):
    {
      "access_key_id":     "ASIA...",       # required
      "secret_access_key": "...",           # required
      "session_token":     "...",           # required (the time-bounded part)
      "expiration":        "ISO 8601",      # required (UTC)
      "region":            "us-...",        # required
      "account_id":        "123456789012"   # optional
    }

The wire response carries ``session_token`` in ``access_token`` and the rest
in ``nexus_credential_metadata``. Caller-side SDK builds ``boto3.Session``
from the pair.
"""

from __future__ import annotations

import json
from datetime import datetime

from nexus.bricks.auth.consumer import MaterializedCredential


class AwsProviderAdapter:
    name: str = "aws"

    def materialize(self, plaintext_payload: bytes) -> MaterializedCredential:
        data = json.loads(plaintext_payload.decode("utf-8"))
        # Required fields — KeyError surfaces as AdapterMaterializeFailed in the consumer.
        session_token = data["session_token"]
        access_key_id = data["access_key_id"]
        secret_access_key = data["secret_access_key"]
        expiration_iso = data["expiration"]
        region = data["region"]

        expires_at = datetime.fromisoformat(expiration_iso)

        metadata = {
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
            "region": region,
        }
        if "account_id" in data:
            metadata["account_id"] = data["account_id"]

        return MaterializedCredential(
            provider=self.name,
            access_token=session_token,
            expires_at=expires_at,
            metadata=metadata,
        )
```

- [ ] **Step 4.4: Run tests to verify pass**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer_providers_aws.py -v`
Expected: 5 PASS.

- [ ] **Step 4.5: Commit**

```bash
git add src/nexus/bricks/auth/consumer_providers/aws.py src/nexus/bricks/auth/tests/test_consumer_providers_aws.py
git commit -m "feat(#3818): add AwsProviderAdapter — STS payload → MaterializedCredential"
```

---

## Task 5: GitHub provider adapter

**Files:**
- Create: `src/nexus/bricks/auth/consumer_providers/github.py`
- Create: `src/nexus/bricks/auth/tests/test_consumer_providers_github.py`

- [ ] **Step 5.1: Write the failing test**

```python
"""Tests for GithubProviderAdapter — pure JSON → MaterializedCredential decoding (#3818)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter


def test_materialize_classic_pat_no_expiry():
    payload = json.dumps(
        {"token": "ghp_classic", "scopes": ["repo", "read:user"]}
    ).encode()
    out = GithubProviderAdapter().materialize(payload)
    assert out.provider == "github"
    assert out.access_token == "ghp_classic"
    assert out.expires_at is None
    assert out.metadata == {"scopes_csv": "repo,read:user", "token_type": "classic"}


def test_materialize_fine_grained_with_expiry():
    payload = json.dumps(
        {
            "token": "github_pat_xyz",
            "scopes": [],
            "expires_at": "2026-07-01T00:00:00+00:00",
            "token_type": "fine_grained",
        }
    ).encode()
    out = GithubProviderAdapter().materialize(payload)
    assert out.access_token == "github_pat_xyz"
    assert out.expires_at == datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
    assert out.metadata["token_type"] == "fine_grained"
    assert out.metadata["scopes_csv"] == ""


def test_materialize_rejects_missing_token():
    with pytest.raises(KeyError):
        GithubProviderAdapter().materialize(json.dumps({"scopes": []}).encode())


def test_materialize_rejects_malformed_json():
    with pytest.raises(ValueError):
        GithubProviderAdapter().materialize(b"<html>not json</html>")


def test_repr_masks_token():
    payload = json.dumps({"token": "ghp_supersecret", "scopes": []}).encode()
    out = GithubProviderAdapter().materialize(payload)
    assert "ghp_supersecret" not in repr(out)
```

- [ ] **Step 5.2: Run to verify failure**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer_providers_github.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 5.3: Implement `github.py`**

```python
"""GitHub provider adapter — daemon-pushed `gh auth token` payload → MaterializedCredential (#3818).

Payload shape (JSON, daemon-pushed by `gh auth token`):
    {
      "token":      "ghp_..." | "github_pat_...",   # required
      "scopes":     ["repo", "read:user", ...],     # required (may be [])
      "expires_at": "ISO 8601",                     # optional (fine-grained PATs)
      "token_type": "classic" | "fine_grained"      # optional, defaults "classic"
    }
"""

from __future__ import annotations

import json
from datetime import datetime

from nexus.bricks.auth.consumer import MaterializedCredential


class GithubProviderAdapter:
    name: str = "github"

    def materialize(self, plaintext_payload: bytes) -> MaterializedCredential:
        data = json.loads(plaintext_payload.decode("utf-8"))
        token = data["token"]  # KeyError → AdapterMaterializeFailed
        scopes = data.get("scopes", [])
        token_type = data.get("token_type", "classic")

        expires_at: datetime | None = None
        if "expires_at" in data and data["expires_at"]:
            expires_at = datetime.fromisoformat(data["expires_at"])

        return MaterializedCredential(
            provider=self.name,
            access_token=token,
            expires_at=expires_at,
            metadata={
                "scopes_csv": ",".join(scopes),
                "token_type": token_type,
            },
        )
```

- [ ] **Step 5.4: Run tests**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer_providers_github.py -v`
Expected: 5 PASS.

- [ ] **Step 5.5: Commit**

```bash
git add src/nexus/bricks/auth/consumer_providers/github.py src/nexus/bricks/auth/tests/test_consumer_providers_github.py
git commit -m "feat(#3818): add GithubProviderAdapter — PAT payload → MaterializedCredential"
```

---

## Task 6: `ResolvedCredCache` with bounded TTL

**Files:**
- Create: `src/nexus/bricks/auth/consumer_cache.py`
- Create: `src/nexus/bricks/auth/tests/test_consumer_cache.py`

- [ ] **Step 6.1: Write the failing test**

```python
"""Tests for ResolvedCredCache — TTL = min(300, expires_at - 60) (#3818)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from nexus.bricks.auth.consumer import MaterializedCredential
from nexus.bricks.auth.consumer_cache import ResolvedCredCache, _compute_ttl_seconds


def _cred(*, expires_at: datetime | None = None) -> MaterializedCredential:
    return MaterializedCredential(
        provider="github",
        access_token="t",
        expires_at=expires_at,
        metadata={},
    )


def test_compute_ttl_uses_ceiling_when_no_expiry():
    assert _compute_ttl_seconds(now=datetime.now(UTC), expires_at=None) == 300


def test_compute_ttl_caps_at_expiry_minus_60():
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    exp = now + timedelta(seconds=200)
    assert _compute_ttl_seconds(now=now, expires_at=exp) == 140


def test_compute_ttl_clamps_to_zero_when_already_near_expiry():
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    exp = now + timedelta(seconds=30)
    assert _compute_ttl_seconds(now=now, expires_at=exp) == 0


def test_get_returns_cached_then_evicts_after_ttl():
    cache = ResolvedCredCache(ceiling_seconds=300)
    key = ("t1", "p1", "github")
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    cred = _cred(expires_at=now + timedelta(seconds=200))

    cache.put(key, cred, now=now)
    # Hit immediately
    assert cache.get(key, now=now) is cred
    # 140s later: still warm (ttl is 200-60 = 140)
    assert cache.get(key, now=now + timedelta(seconds=139)) is cred
    # Just after TTL boundary: expired
    assert cache.get(key, now=now + timedelta(seconds=141)) is None


def test_put_with_no_expiry_uses_ceiling():
    cache = ResolvedCredCache(ceiling_seconds=300)
    key = ("t1", "p1", "github")
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    cache.put(key, _cred(expires_at=None), now=now)
    assert cache.get(key, now=now + timedelta(seconds=299)) is not None
    assert cache.get(key, now=now + timedelta(seconds=301)) is None


def test_thread_safe_concurrent_put_get():
    cache = ResolvedCredCache(ceiling_seconds=300)
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    cred = _cred(expires_at=now + timedelta(seconds=600))

    def worker(i: int):
        for _ in range(50):
            cache.put((f"t{i}", "p", "github"), cred, now=now)
            cache.get((f"t{i}", "p", "github"), now=now)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No exception = pass
```

- [ ] **Step 6.2: Run to verify failure**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer_cache.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 6.3: Implement `consumer_cache.py`**

```python
"""ResolvedCredCache: TTL = min(ceiling, expires_at - 60s).

Holds plaintext access_tokens in memory bounded by both a ceiling (default
300s, matching DEKCache) and the upstream credential's own ``expires_at``.
This caps plaintext lifetime regardless of which bound triggers first.

Keyed by ``(tenant_id_str, principal_id_str, provider)``. Tenant in the key
is belt-and-braces against any future bug that forgets to ``SET LOCAL
app.current_tenant`` before calling the consumer.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from nexus.bricks.auth.consumer import MaterializedCredential

_REFRESH_HEADROOM_SECONDS = 60


def _compute_ttl_seconds(*, now: datetime, expires_at: datetime | None) -> int:
    """TTL = min(ceiling, expires_at - 60s). Clamped to >= 0.

    The 60s headroom means we evict before the upstream cred actually expires,
    so callers never see a 401 from the upstream provider mid-call.

    Ceiling is applied by the caller (``ResolvedCredCache.put``) — this helper
    only computes the expires-at-bound. Returns the smaller of the two there.
    """
    if expires_at is None:
        return 10**9  # effectively unbounded; ceiling will dominate
    delta = (expires_at - now).total_seconds() - _REFRESH_HEADROOM_SECONDS
    return max(0, int(delta))


@dataclass(frozen=True)
class _Entry:
    cred: MaterializedCredential
    expires_at_monotonic: float


class ResolvedCredCache:
    """Thread-safe TTL+LRU for MaterializedCredentials.

    Tests inject ``now`` for determinism; production calls pass
    ``datetime.now(UTC)``.
    """

    def __init__(self, *, ceiling_seconds: int = 300, max_entries: int = 1024) -> None:
        self._ceiling = ceiling_seconds
        self._max = max_entries
        self._store: OrderedDict[tuple[str, str, str], _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def get(
        self,
        key: tuple[str, str, str],
        *,
        now: datetime,
    ) -> MaterializedCredential | None:
        now_ts = now.timestamp()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if now_ts >= entry.expires_at_monotonic:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return entry.cred

    def put(
        self,
        key: tuple[str, str, str],
        cred: MaterializedCredential,
        *,
        now: datetime,
    ) -> None:
        ttl = min(
            self._ceiling,
            _compute_ttl_seconds(now=now, expires_at=cred.expires_at),
        )
        with self._lock:
            self._store[key] = _Entry(
                cred=cred,
                expires_at_monotonic=now.timestamp() + ttl,
            )
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)
```

- [ ] **Step 6.4: Run tests**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer_cache.py -v`
Expected: 6 PASS.

- [ ] **Step 6.5: Commit**

```bash
git add src/nexus/bricks/auth/consumer_cache.py src/nexus/bricks/auth/tests/test_consumer_cache.py
git commit -m "feat(#3818): add ResolvedCredCache — TTL = min(ceiling, expires_at-60s)"
```

---

## Task 7: `ReadAuditWriter` with 1% cache-hit sampling

**Files:**
- Create: `src/nexus/bricks/auth/read_audit.py`
- Create: `src/nexus/bricks/auth/tests/test_read_audit.py`

- [ ] **Step 7.1: Write the failing test**

```python
"""Tests for ReadAuditWriter — 100% on cache-miss, 1% sample on cache-hit (#3818)."""

from __future__ import annotations

import os
import random
import uuid

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.auth.postgres_profile_store import ensure_schema
from nexus.bricks.auth.read_audit import ReadAuditWriter


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _seed_tenant_principal(engine, tenant_id, principal_id):
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'rt') ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal_id), "t": str(tenant_id)},
        )


def _count_reads(engine, tenant_id):
    with engine.connect() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        return conn.execute(text("SELECT COUNT(*) FROM auth_profile_reads")).scalar()


def test_writes_100_percent_on_cache_miss(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    _seed_tenant_principal(engine, tenant, principal)
    writer = ReadAuditWriter(engine=engine, hit_sample_rate=0.01)

    for _ in range(20):
        writer.write(
            tenant_id=tenant,
            principal_id=principal,
            auth_profile_id="github-default",
            caller_machine_id=machine,
            caller_kind="daemon",
            provider="github",
            purpose="test",
            cache_hit=False,
            kek_version=1,
        )

    assert _count_reads(engine, tenant) == 20


def test_samples_one_percent_on_cache_hit(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    _seed_tenant_principal(engine, tenant, principal)
    # Fixed RNG: with seed 42, first 100 random() values include exactly N
    # below 0.01 — count them, then verify the writer wrote exactly N rows.
    rng = random.Random(42)
    expected = sum(1 for _ in range(100) if rng.random() < 0.01)

    writer = ReadAuditWriter(engine=engine, hit_sample_rate=0.01, rng=random.Random(42))
    for _ in range(100):
        writer.write(
            tenant_id=tenant,
            principal_id=principal,
            auth_profile_id="github-default",
            caller_machine_id=machine,
            caller_kind="daemon",
            provider="github",
            purpose="test",
            cache_hit=True,
            kek_version=1,
        )

    assert _count_reads(engine, tenant) == expected


def test_truncates_purpose_to_256_chars(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    _seed_tenant_principal(engine, tenant, principal)
    writer = ReadAuditWriter(engine=engine, hit_sample_rate=0.01)

    long_purpose = "x" * 1000
    writer.write(
        tenant_id=tenant,
        principal_id=principal,
        auth_profile_id="github-default",
        caller_machine_id=machine,
        caller_kind="daemon",
        provider="github",
        purpose=long_purpose,
        cache_hit=False,
        kek_version=1,
    )

    with engine.connect() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        row = conn.execute(text("SELECT purpose FROM auth_profile_reads")).fetchone()
    assert len(row[0]) == 256
```

- [ ] **Step 7.2: Run to verify failure**

Run: `pytest src/nexus/bricks/auth/tests/test_read_audit.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 7.3: Implement `read_audit.py`**

```python
"""ReadAuditWriter — auth_profile_reads row per credential resolution (#3818).

Sampling: 100% on cache-miss (real KMS unwrap → real credential access),
1% on cache-hit (operational telemetry, not access). Sampling rule documented
in deployment guide; deviation should be a deliberate operator choice.
"""

from __future__ import annotations

import logging
import random
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_PURPOSE_MAX_LEN = 256


class ReadAuditWriter:
    """Inserts ``auth_profile_reads`` rows. Caller passes RLS-set engine.

    Failures are logged and swallowed. We never block a credential resolution
    on audit-row insert — losing a single row is preferable to a blocked
    caller, and the cache-miss path will retry naturally on the next resolve.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        hit_sample_rate: float = 0.01,
        rng: random.Random | None = None,
    ) -> None:
        self._engine = engine
        self._hit_sample_rate = hit_sample_rate
        self._rng = rng or random.Random()

    def write(
        self,
        *,
        tenant_id: uuid.UUID,
        principal_id: uuid.UUID,
        auth_profile_id: str,
        caller_machine_id: uuid.UUID,
        caller_kind: str,
        provider: str,
        purpose: str,
        cache_hit: bool,
        kek_version: int,
    ) -> None:
        if cache_hit and self._rng.random() >= self._hit_sample_rate:
            return  # sampled out

        truncated_purpose = purpose[:_PURPOSE_MAX_LEN]

        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("SET LOCAL app.current_tenant = :t"),
                    {"t": str(tenant_id)},
                )
                conn.execute(
                    text(
                        "INSERT INTO auth_profile_reads "
                        "(tenant_id, principal_id, auth_profile_id, caller_machine_id, "
                        " caller_kind, provider, purpose, cache_hit, kek_version) "
                        "VALUES (:t, :p, :ap, :cm, :ck, :prov, :pur, :hit, :kv)"
                    ),
                    {
                        "t": str(tenant_id),
                        "p": str(principal_id),
                        "ap": auth_profile_id,
                        "cm": str(caller_machine_id),
                        "ck": caller_kind,
                        "prov": provider,
                        "pur": truncated_purpose,
                        "hit": cache_hit,
                        "kv": kek_version,
                    },
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "auth_profile_reads insert failed tenant=%s principal=%s provider=%s",
                tenant_id,
                principal_id,
                provider,
            )
```

- [ ] **Step 7.4: Run tests**

Run: `pytest src/nexus/bricks/auth/tests/test_read_audit.py -v`
Expected: 3 PASS.

- [ ] **Step 7.5: Commit**

```bash
git add src/nexus/bricks/auth/read_audit.py src/nexus/bricks/auth/tests/test_read_audit.py
git commit -m "feat(#3818): add ReadAuditWriter — 100% miss, 1% hit sampling"
```

---

## Task 8: `CredentialConsumer.resolve()` orchestrator

**Files:**
- Modify: `src/nexus/bricks/auth/consumer.py` (replace the stub `class CredentialConsumer:` from Task 3)
- Create: `src/nexus/bricks/auth/tests/test_consumer.py`

- [ ] **Step 8.1: Write the failing test**

```python
"""Tests for CredentialConsumer.resolve — orchestrator covering happy / cache /
stale / errors (#3818)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from nexus.bricks.auth.consumer import (
    AdapterMaterializeFailed,
    CredentialConsumer,
    ProfileNotFoundForCaller,
    ProviderNotConfigured,
    StaleSource,
)
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter
from nexus.bricks.auth.envelope import AESGCMEnvelope, DEKCache
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)
from nexus.bricks.auth.read_audit import ReadAuditWriter
from nexus.server.api.v1.jwt_signer import DaemonClaims


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _seed_github_envelope(*, engine, tenant_id, principal_id, encryption, sync_ttl=300, lsa_offset_seconds=0):
    """Seed a github profile with a pushed envelope."""
    payload = json.dumps({"token": "ghp_test", "scopes": ["repo"]}).encode()
    aad = (
        str(tenant_id).encode()
        + b"|" + str(principal_id).encode()
        + b"|" + b"github-default"
    )
    dek = b"\x01" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant_id, aad=aad)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant_id)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'tx') ON CONFLICT DO NOTHING"),
            {"id": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal_id), "t": str(tenant_id)},
        )
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, 'github-default', 'github', 'me', 'envelope', 'k', "
                " NOW() - (:off || ' seconds')::INTERVAL, :ttl, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant_id),
                "p": str(principal_id),
                "off": str(lsa_offset_seconds),
                "ttl": sync_ttl,
                "ct": ct,
                "wd": wrapped,
                "no": nonce,
                "aad": aad,
                "kv": kv,
            },
        )


def _make_consumer(engine, tenant_id, encryption=None, cache=None):
    encryption = encryption or InMemoryEncryptionProvider()
    cache = cache or ResolvedCredCache(ceiling_seconds=300)
    store = PostgresAuthProfileStore(engine=engine, tenant_id=tenant_id)
    return CredentialConsumer(
        store=store,
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=cache,
        adapters={"github": GithubProviderAdapter()},
        audit=ReadAuditWriter(engine=engine, hit_sample_rate=0.01),
    )


def _claims(tenant_id, principal_id):
    return DaemonClaims(
        tenant_id=tenant_id,
        principal_id=principal_id,
        machine_id=uuid.uuid4(),
    )


def test_resolve_happy_path_returns_materialized_cred(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    consumer = _make_consumer(engine, tenant, encryption=encryption)

    out = consumer.resolve(
        claims=_claims(tenant, principal),
        provider="github",
        purpose="list-repos",
    )
    assert out.access_token == "ghp_test"
    assert out.metadata["scopes_csv"] == "repo"


def test_resolve_warm_cache_skips_decrypt(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    consumer = _make_consumer(engine, tenant, encryption=encryption)

    first = consumer.resolve(
        claims=_claims(tenant, principal), provider="github", purpose="x"
    )
    # Drop the row so a second decrypt would fail
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(text("DELETE FROM auth_profiles WHERE tenant_id = :t"), {"t": str(tenant)})

    second = consumer.resolve(
        claims=_claims(tenant, principal), provider="github", purpose="x"
    )
    assert second is first  # cached, same object


def test_resolve_force_refresh_bypasses_cache(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine, tenant_id=tenant, principal_id=principal, encryption=encryption
    )
    consumer = _make_consumer(engine, tenant, encryption=encryption)

    first = consumer.resolve(
        claims=_claims(tenant, principal), provider="github", purpose="x"
    )
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(text("DELETE FROM auth_profiles WHERE tenant_id = :t"), {"t": str(tenant)})

    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(
            claims=_claims(tenant, principal),
            provider="github",
            purpose="x",
            force_refresh=True,
        )


def test_resolve_raises_profile_not_found(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'tx') ON CONFLICT DO NOTHING"),
            {"id": str(tenant)},
        )
    consumer = _make_consumer(engine, tenant)

    with pytest.raises(ProfileNotFoundForCaller):
        consumer.resolve(
            claims=_claims(tenant, principal), provider="github", purpose="x"
        )


def test_resolve_raises_provider_not_configured(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    consumer = _make_consumer(engine, tenant)
    with pytest.raises(ProviderNotConfigured):
        consumer.resolve(
            claims=_claims(tenant, principal),
            provider="unknown",
            purpose="x",
        )


def test_resolve_raises_stale_source_when_last_synced_past_ttl(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    _seed_github_envelope(
        engine=engine,
        tenant_id=tenant,
        principal_id=principal,
        encryption=encryption,
        sync_ttl=60,
        lsa_offset_seconds=120,  # 2 minutes ago, TTL is 60s → stale
    )
    consumer = _make_consumer(engine, tenant, encryption=encryption)
    with pytest.raises(StaleSource):
        consumer.resolve(
            claims=_claims(tenant, principal), provider="github", purpose="x"
        )
```

- [ ] **Step 8.2: Run to verify failure**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer.py -v`
Expected: FAIL — `CredentialConsumer.resolve` undefined.

- [ ] **Step 8.3: Replace the stub `CredentialConsumer` in `consumer.py`**

In `src/nexus/bricks/auth/consumer.py`, REPLACE the `class CredentialConsumer:` stub from Task 3 with:

```python
class CredentialConsumer:
    """Orchestrates: cache lookup → decrypt → materialize → cache write → audit.

    All errors raised by this class are subclasses of ``ConsumerError`` and
    carry no plaintext / token bytes in repr.
    """

    def __init__(
        self,
        *,
        store: "PostgresAuthProfileStore",
        encryption: "EncryptionProvider",
        dek_cache: "DEKCache",
        cred_cache: "ResolvedCredCache",
        adapters: dict[str, "ProviderAdapter"],
        audit: "ReadAuditWriter",
    ) -> None:
        self._store = store
        self._encryption = encryption
        self._dek_cache = dek_cache
        self._cred_cache = cred_cache
        self._adapters = adapters
        self._audit = audit

    def resolve(
        self,
        *,
        claims: "DaemonClaims",
        provider: str,
        purpose: str,
        force_refresh: bool = False,
    ) -> MaterializedCredential:
        from datetime import UTC, datetime  # local import to keep top clean

        from nexus.bricks.auth.postgres_profile_store import ProfileNotFound

        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ProviderNotConfigured.from_row(
                tenant_id=claims.tenant_id,
                principal_id=claims.principal_id,
                provider=provider,
                cause="adapter_not_registered",
            )

        cache_key = (
            str(claims.tenant_id),
            str(claims.principal_id),
            provider,
        )
        now = datetime.now(UTC)
        cache_hit = False
        kek_version = 0

        if not force_refresh:
            cached = self._cred_cache.get(cache_key, now=now)
            if cached is not None:
                cache_hit = True
                # We don't know the original kek_version on a cache hit; use 0
                # as a sentinel ("unknown — cache hit") in the audit row.
                self._audit.write(
                    tenant_id=claims.tenant_id,
                    principal_id=claims.principal_id,
                    auth_profile_id="cached",
                    caller_machine_id=claims.machine_id,
                    caller_kind="daemon",
                    provider=provider,
                    purpose=purpose,
                    cache_hit=True,
                    kek_version=0,
                )
                return cached

        try:
            decrypted = self._store.decrypt_profile(
                principal_id=claims.principal_id,
                provider=provider,
                encryption=self._encryption,
                dek_cache=self._dek_cache,
            )
        except ProfileNotFound as exc:
            raise ProfileNotFoundForCaller.from_row(
                tenant_id=claims.tenant_id,
                principal_id=claims.principal_id,
                provider=provider,
                cause="no_envelope_row",
            ) from exc

        # Stale-source check: last_synced_at + sync_ttl must be in the future.
        from datetime import timedelta

        ttl_window = timedelta(seconds=decrypted.sync_ttl_seconds)
        if decrypted.last_synced_at + ttl_window < now:
            raise StaleSource.from_row(
                tenant_id=claims.tenant_id,
                principal_id=claims.principal_id,
                provider=provider,
                cause=f"last_synced_at_age={int((now - decrypted.last_synced_at).total_seconds())}s",
            )

        kek_version = decrypted.kek_version
        try:
            materialized = adapter.materialize(decrypted.plaintext)
        except (ValueError, KeyError) as exc:
            raise AdapterMaterializeFailed.from_row(
                tenant_id=claims.tenant_id,
                principal_id=claims.principal_id,
                provider=provider,
                cause=f"{type(exc).__name__}",
            ) from exc

        self._cred_cache.put(cache_key, materialized, now=now)
        self._audit.write(
            tenant_id=claims.tenant_id,
            principal_id=claims.principal_id,
            auth_profile_id=decrypted.profile_id,
            caller_machine_id=claims.machine_id,
            caller_kind="daemon",
            provider=provider,
            purpose=purpose,
            cache_hit=False,
            kek_version=kek_version,
        )
        return materialized
```

- [ ] **Step 8.4: Run tests**

Run: `pytest src/nexus/bricks/auth/tests/test_consumer.py -v`
Expected: 6 PASS.

- [ ] **Step 8.5: Commit**

```bash
git add src/nexus/bricks/auth/consumer.py src/nexus/bricks/auth/tests/test_consumer.py
git commit -m "feat(#3818): implement CredentialConsumer.resolve orchestrator"
```

---

## Task 9: Prometheus metrics

**Files:**
- Create: `src/nexus/bricks/auth/consumer_metrics.py`
- Modify: `src/nexus/bricks/auth/consumer.py` (call into metrics)

- [ ] **Step 9.1: Create `consumer_metrics.py`**

```python
"""Prometheus metrics for the read path (#3818).

Low-cardinality labels only:
  - provider ∈ {aws, github}
  - result ∈ {ok, stale, denied, invalid_token, envelope_error}
  - cache ∈ {hit, miss}
  - reason (cache evictions) ∈ {ttl, lru, expires_at}
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

TOKEN_EXCHANGE_REQUESTS = Counter(
    "nexus_token_exchange_requests_total",
    "RFC 8693 token-exchange requests by outcome",
    labelnames=("provider", "result"),
)

TOKEN_EXCHANGE_LATENCY = Histogram(
    "nexus_token_exchange_latency_seconds",
    "Latency of /v1/auth/token-exchange end-to-end",
    labelnames=("provider", "cache"),
)

CONSUMER_CACHE_SIZE = Gauge(
    "nexus_consumer_cache_size",
    "Current entries in ResolvedCredCache",
)

CONSUMER_CACHE_EVICTIONS = Counter(
    "nexus_consumer_cache_evictions_total",
    "ResolvedCredCache evictions by reason",
    labelnames=("reason",),
)

READ_AUDIT_WRITES = Counter(
    "nexus_read_audit_writes_total",
    "Auth-profile-read audit rows written",
    labelnames=("cache",),
)
```

- [ ] **Step 9.2: Wire metrics into `consumer.py` and `read_audit.py`**

In `consumer.py`, add at the top:

```python
from nexus.bricks.auth.consumer_metrics import (
    TOKEN_EXCHANGE_LATENCY,
    TOKEN_EXCHANGE_REQUESTS,
)
```

In `CredentialConsumer.resolve`, wrap the body in a Histogram timer and record outcomes. Replace the existing `def resolve(...)` body's outermost block with:

```python
        from datetime import UTC, datetime
        import time

        start = time.monotonic()
        cache_label = "miss"
        result_label = "ok"
        try:
            # ... [keep the existing resolve() body unchanged here] ...
            # (where the body returns ``cached``, set cache_label="hit")
            # (where the body returns ``materialized``, leave cache_label="miss")
        except ProfileNotFoundForCaller:
            result_label = "denied"
            raise
        except ProviderNotConfigured:
            result_label = "denied"
            raise
        except StaleSource:
            result_label = "stale"
            raise
        except AdapterMaterializeFailed:
            result_label = "envelope_error"
            raise
        finally:
            TOKEN_EXCHANGE_LATENCY.labels(
                provider=provider, cache=cache_label
            ).observe(time.monotonic() - start)
            TOKEN_EXCHANGE_REQUESTS.labels(
                provider=provider, result=result_label
            ).inc()
```

(The existing return-cached path needs to set `cache_label = "hit"` before returning. Easiest: pull both return points into the try block and assign the label just before returning.)

In `read_audit.py`, add at the top:

```python
from nexus.bricks.auth.consumer_metrics import READ_AUDIT_WRITES
```

In `ReadAuditWriter.write`, after the successful `INSERT` (before the `except`), add:

```python
            READ_AUDIT_WRITES.labels(cache="hit" if cache_hit else "miss").inc()
```

- [ ] **Step 9.3: Verify imports + smoke test**

Run: `python -c "from nexus.bricks.auth.consumer_metrics import TOKEN_EXCHANGE_REQUESTS; TOKEN_EXCHANGE_REQUESTS.labels(provider='github', result='ok').inc(); print('OK')"`
Expected: `OK`.

Re-run the consumer tests: `pytest src/nexus/bricks/auth/tests/test_consumer.py -v` — should still pass (metrics are additive).

- [ ] **Step 9.4: Commit**

```bash
git add src/nexus/bricks/auth/consumer_metrics.py src/nexus/bricks/auth/consumer.py src/nexus/bricks/auth/read_audit.py
git commit -m "feat(#3818): add Prometheus metrics for read path"
```

---

## Task 10: Rewrite `/v1/auth/token-exchange` router

**Files:**
- Modify: `src/nexus/server/api/v1/routers/token_exchange.py` (full rewrite)
- Modify: `src/nexus/server/api/v1/tests/test_token_exchange_router.py` (full rewrite)

- [ ] **Step 10.1: Write the failing test**

Replace the entire contents of `src/nexus/server/api/v1/tests/test_token_exchange_router.py` with:

```python
"""Tests for /v1/auth/token-exchange router (#3818)."""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from nexus.bricks.auth.consumer import CredentialConsumer
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter
from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter
from nexus.bricks.auth.envelope import AESGCMEnvelope, DEKCache
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)
from nexus.bricks.auth.read_audit import ReadAuditWriter
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner
from nexus.server.api.v1.routers.token_exchange import make_token_exchange_router


def _make_signer():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization

    pk = ec.generate_private_key(ec.SECP256R1())
    pem = pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return JwtSigner.from_pem(pem, issuer="https://test.local")


@pytest.fixture
def engine():
    url = os.environ.get("NEXUS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    eng = create_engine(url, future=True)
    ensure_schema(eng)
    yield eng
    eng.dispose()


def _build_app(engine, tenant_id):
    encryption = InMemoryEncryptionProvider()
    signer = _make_signer()
    consumer = CredentialConsumer(
        store=PostgresAuthProfileStore(engine=engine, tenant_id=tenant_id),
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=ResolvedCredCache(),
        adapters={
            "aws": AwsProviderAdapter(),
            "github": GithubProviderAdapter(),
        },
        audit=ReadAuditWriter(engine=engine, hit_sample_rate=0.01),
    )
    app = FastAPI()
    app.include_router(
        make_token_exchange_router(
            enabled=True, signer=signer, consumer=consumer, encryption=encryption,
        )
    )
    return app, signer, encryption


def _seed_github(engine, tenant, principal, encryption):
    payload = json.dumps({"token": "ghp_real", "scopes": ["repo"]}).encode()
    aad = (
        str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-default"
    )
    dek = b"\x02" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant, aad=aad)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'tx') ON CONFLICT DO NOTHING"),
            {"id": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal), "t": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, 'github-default', 'github', 'me', 'envelope', 'k', "
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant), "p": str(principal),
                "ct": ct, "wd": wrapped, "no": nonce, "aad": aad, "kv": kv,
            },
        )


def test_token_exchange_happy_path_returns_200_with_bearer(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()
    app, signer, encryption = _build_app(engine, tenant)
    _seed_github(engine, tenant, principal, encryption)
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )

    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "list-repos",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] == "ghp_real"
    assert body["token_type"] == "Bearer"
    assert body["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert "nexus_credential_metadata" in body
    assert body["nexus_credential_metadata"]["scopes_csv"] == "repo"


def test_token_exchange_invalid_jwt_returns_401(engine):
    tenant = uuid.uuid4()
    app, _, _ = _build_app(engine, tenant)
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "garbage",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_token"


def test_token_exchange_unknown_resource_returns_400(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, _ = _build_app(engine, tenant)
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=uuid.uuid4()),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:slack",  # unknown
            "scope": "x",
        },
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_token_exchange_no_profile_returns_403(engine):
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    app, signer, _ = _build_app(engine, tenant)
    # Seed tenant + principal but no profile
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'tx') ON CONFLICT DO NOTHING"),
            {"id": str(tenant)},
        )
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=uuid.uuid4()),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "x",
        },
    )
    assert r.status_code == 403
    assert r.json()["error"] == "access_denied"


def test_token_exchange_disabled_returns_501(engine):
    tenant = uuid.uuid4()
    encryption = InMemoryEncryptionProvider()
    signer = _make_signer()
    consumer = CredentialConsumer(
        store=PostgresAuthProfileStore(engine=engine, tenant_id=tenant),
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=ResolvedCredCache(),
        adapters={"github": GithubProviderAdapter()},
        audit=ReadAuditWriter(engine=engine),
    )
    app = FastAPI()
    app.include_router(
        make_token_exchange_router(
            enabled=False, signer=signer, consumer=consumer, encryption=encryption,
        )
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "x", "subject_token_type": "x",
            "resource": "urn:nexus:provider:github", "scope": "x",
        },
    )
    assert r.status_code == 501
```

- [ ] **Step 10.2: Run to verify failure**

Run: `pytest src/nexus/server/api/v1/tests/test_token_exchange_router.py -v`
Expected: FAIL — `make_token_exchange_router` signature mismatch.

- [ ] **Step 10.3: Replace `token_exchange.py`**

Replace the entire contents of `src/nexus/server/api/v1/routers/token_exchange.py` with:

```python
"""FastAPI router: POST /v1/auth/token-exchange (RFC 8693, #3818).

Verifies the daemon's JWT (subject_token), looks up the matching envelope row
via CredentialConsumer, and returns a provider-native bearer token. Errors
follow RFC 6749 §5.2 shape: ``{"error": "...", "error_description": "..."}``.

When ``enabled=False`` (default until ops verifies KMS/Vault wiring) the route
returns 501 regardless of the request — the consumer/signer args are still
required so tests and dev wiring stay symmetric.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Form, HTTPException, status
from fastapi.responses import JSONResponse

from nexus.bricks.auth.consumer import (
    AdapterMaterializeFailed,
    CredentialConsumer,
    ProfileNotFoundForCaller,
    ProviderNotConfigured,
    StaleSource,
)
from nexus.bricks.auth.envelope import EncryptionProvider, EnvelopeError
from nexus.server.api.v1.jwt_signer import JwtSigner, JwtVerifyError

logger = logging.getLogger(__name__)

_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
_SUBJECT_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
_ISSUED_TYPE = "urn:ietf:params:oauth:token-type:access_token"

_RESOURCE_TO_PROVIDER = {
    "urn:nexus:provider:aws": "aws",
    "urn:nexus:provider:github": "github",
}


def _err(http_status: int, code: str, description: str) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": code, "error_description": description},
    )


def make_token_exchange_router(
    *,
    enabled: bool,
    signer: JwtSigner,
    consumer: CredentialConsumer,
    encryption: EncryptionProvider,  # for symmetry — not used directly here
) -> APIRouter:
    """Build the ``/v1/auth/token-exchange`` router.

    When ``enabled=False`` the route returns 501 — gives ops a single env-var
    flag to flip the read path on/off without redeploying.
    """
    del encryption  # Reserved for future direct-decrypt fallbacks; unused for now.
    router = APIRouter(prefix="/v1/auth", tags=["auth"])

    @router.post("/token-exchange")
    def exchange(
        grant_type: str = Form(...),
        subject_token: str = Form(...),
        subject_token_type: str = Form(...),
        resource: str = Form(...),
        scope: str = Form(...),
        audience: str | None = Form(None),
        nexus_force_refresh: str = Form("false"),
    ) -> Any:
        if not enabled:
            return _err(
                status.HTTP_501_NOT_IMPLEMENTED,
                "not_implemented",
                "token-exchange disabled (NEXUS_TOKEN_EXCHANGE_ENABLED=0)",
            )

        del audience  # MVP ignores audience field (always bound by JWT verify).

        if grant_type != _GRANT_TYPE:
            return _err(400, "invalid_request", f"unknown grant_type: {grant_type!r}")
        if subject_token_type != _SUBJECT_TYPE_JWT:
            return _err(
                400, "invalid_request",
                f"unsupported subject_token_type: {subject_token_type!r}"
            )
        provider = _RESOURCE_TO_PROVIDER.get(resource)
        if provider is None:
            return _err(400, "invalid_request", f"unknown resource: {resource!r}")

        try:
            claims = signer.verify(subject_token)
        except JwtVerifyError as exc:
            return _err(401, "invalid_token", str(exc))

        force_refresh = nexus_force_refresh.lower() in ("1", "true", "yes")

        try:
            cred = consumer.resolve(
                claims=claims,
                provider=provider,
                purpose=scope,
                force_refresh=force_refresh,
            )
        except (ProfileNotFoundForCaller, ProviderNotConfigured) as exc:
            return _err(403, "access_denied", exc.cause or "")
        except StaleSource as exc:
            return _err(409, "stale_source", exc.cause or "")
        except (AdapterMaterializeFailed, EnvelopeError) as exc:
            logger.warning("envelope_error: %r", exc)  # __repr__ masks plaintext
            return _err(500, "envelope_error", "see server logs")

        expires_in = 0
        if cred.expires_at is not None:
            expires_in = max(0, int((cred.expires_at - datetime.now(UTC)).total_seconds()))

        return {
            "access_token": cred.access_token,
            "issued_token_type": _ISSUED_TYPE,
            "token_type": "Bearer",
            "expires_in": expires_in,
            "nexus_credential_metadata": cred.metadata,
        }

    return router
```

- [ ] **Step 10.4: Run tests**

Run: `pytest src/nexus/server/api/v1/tests/test_token_exchange_router.py -v`
Expected: 5 PASS.

- [ ] **Step 10.5: Commit**

```bash
git add src/nexus/server/api/v1/routers/token_exchange.py src/nexus/server/api/v1/tests/test_token_exchange_router.py
git commit -m "feat(#3818): rewrite /v1/auth/token-exchange — RFC 8693 read path"
```

---

## Task 11: Wire consumer + new router signature into `fastapi_server.py`

**Files:**
- Modify: `src/nexus/server/fastapi_server.py` (token-exchange wiring block, around lines 992-1006)

- [ ] **Step 11.1: Read the existing wiring block**

Re-read `src/nexus/server/fastapi_server.py` lines 992-1075 to confirm the surrounding context (daemon router setup happens immediately after; we want token-exchange to share `_v1_engine`, `_v1_signer`).

- [ ] **Step 11.2: Move the token-exchange include INSIDE the `if _v1_signer is not None` block**

Locate the existing `make_token_exchange_router(enabled=_token_exchange_enabled)` call (line ~1003) and DELETE that block (lines ~992-1006). The new wiring goes inside the `if _v1_signer is not None:` block right after the `make_jwks_router` line.

Add this block immediately after `app.include_router(make_jwks_router(signer=_v1_signer))` (line ~1072):

```python
                    # Token-exchange: read path requires the same engine + signer
                    # as the daemon router, plus an EncryptionProvider for envelope
                    # decrypt. Default off (NEXUS_TOKEN_EXCHANGE_ENABLED) until ops
                    # verifies KMS/Vault wiring.
                    try:
                        from nexus.bricks.auth.consumer import CredentialConsumer
                        from nexus.bricks.auth.consumer_cache import ResolvedCredCache
                        from nexus.bricks.auth.consumer_providers import (
                            default_adapters,
                        )
                        from nexus.bricks.auth.envelope import DEKCache
                        from nexus.bricks.auth.envelope_providers.in_memory import (
                            InMemoryEncryptionProvider,
                        )
                        from nexus.bricks.auth.postgres_profile_store import (
                            PostgresAuthProfileStore,
                        )
                        from nexus.bricks.auth.read_audit import ReadAuditWriter
                        from nexus.server.api.v1.routers.token_exchange import (
                            make_token_exchange_router,
                        )
                    except ImportError as e:
                        logger.warning(
                            "v1 token-exchange disabled: import failed (%s)", e
                        )
                    else:
                        _token_exchange_enabled = (
                            os.environ.get("NEXUS_TOKEN_EXCHANGE_ENABLED", "")
                            .lower() in ("1", "true", "yes")
                        )
                        # MVP: InMemoryEncryptionProvider unless an operator has
                        # already wired Vault/KMS via app.state. Production
                        # deployments override this in their startup hook.
                        _enc = getattr(app.state, "encryption_provider", None) or (
                            InMemoryEncryptionProvider()
                        )
                        # Tenant-id stamping happens per-request inside
                        # PostgresAuthProfileStore via SET LOCAL; the singleton
                        # used here gets re-tenanted by the consumer per call.
                        # If the existing store API does not support per-call
                        # tenant rebinding, instantiate the store inside the
                        # consumer's resolve() instead — adjust to whichever
                        # pattern auth_profiles.py / daemon.py already uses.
                        _store = PostgresAuthProfileStore(
                            engine=_v1_engine,
                            tenant_id=None,  # set per request in consumer
                        )
                        _consumer = CredentialConsumer(
                            store=_store,
                            encryption=_enc,
                            dek_cache=DEKCache(),
                            cred_cache=ResolvedCredCache(),
                            adapters=default_adapters(),
                            audit=ReadAuditWriter(engine=_v1_engine),
                        )
                        app.include_router(
                            make_token_exchange_router(
                                enabled=_token_exchange_enabled,
                                signer=_v1_signer,
                                consumer=_consumer,
                                encryption=_enc,
                            )
                        )
                        logger.info(
                            "v1 token-exchange route registered (enabled=%s)",
                            _token_exchange_enabled,
                        )
```

**Note on `tenant_id=None` and per-request rebinding:** PR 3 (`make_auth_profiles_router(engine=_v1_engine, signer=_v1_signer)`) shows the existing pattern reads tenant from the verified JWT and sets RLS per request. Read `src/nexus/server/api/v1/routers/auth_profiles.py` to copy how it instantiates `PostgresAuthProfileStore` per request, then mirror that pattern in `token_exchange.py` and `consumer.py` if needed. If the existing `PostgresAuthProfileStore.__init__` requires a non-None `tenant_id`, the cleanest fix is to construct the store inside `CredentialConsumer.resolve` using `claims.tenant_id` — defer this construction by passing the engine to the consumer instead of the store.

- [ ] **Step 11.3: Adapt store construction if necessary**

If Step 11.2's `tenant_id=None` doesn't work, modify `CredentialConsumer.__init__` to accept `engine: Engine` instead of `store`, and construct `PostgresAuthProfileStore(engine=engine, tenant_id=claims.tenant_id)` at the top of every `resolve()` call. Update `test_consumer.py` accordingly.

- [ ] **Step 11.4: Smoke test the import chain**

Run: `python -c "from nexus.server.fastapi_server import create_app; print('imports OK')"`
Expected: `imports OK` (no ImportError, no startup error from the new wiring path — it's all lazy under the `if _v1_signer is not None` guard).

- [ ] **Step 11.5: Run all auth tests**

Run: `pytest src/nexus/bricks/auth/tests/ src/nexus/server/api/v1/tests/ -v --tb=short`
Expected: all PASS (Postgres-gated tests skip cleanly when `NEXUS_TEST_DATABASE_URL` is unset).

- [ ] **Step 11.6: Commit**

```bash
git add src/nexus/server/fastapi_server.py src/nexus/bricks/auth/consumer.py src/nexus/bricks/auth/tests/test_consumer.py
git commit -m "feat(#3818): wire CredentialConsumer + token-exchange router into create_app"
```

---

## Task 12: E2E test — AWS via LocalStack

**Files:**
- Create: `tests/e2e/auth_consumption/__init__.py` (empty)
- Create: `tests/e2e/auth_consumption/test_s3_as_user.py`

- [ ] **Step 12.1: Create the empty `__init__.py`**

```bash
touch tests/e2e/auth_consumption/__init__.py
```

- [ ] **Step 12.2: Write the e2e test**

```python
"""E2E: daemon push → /v1/auth/token-exchange → real S3 list-buckets (#3818).

PR-CI variant: uses LocalStack S3 (deterministic, no live AWS).
Nightly variant: set NEXUS_TEST_AWS_LIVE=1 to exercise live STS + S3.

Requires:
  - NEXUS_TEST_DATABASE_URL (Postgres)
  - LOCALSTACK_ENDPOINT (e.g. http://localhost:4566) — falls back to skip
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from nexus.bricks.auth.consumer import CredentialConsumer
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
from nexus.bricks.auth.consumer_providers.aws import AwsProviderAdapter
from nexus.bricks.auth.envelope import AESGCMEnvelope, DEKCache
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)
from nexus.bricks.auth.read_audit import ReadAuditWriter
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner
from nexus.server.api.v1.routers.token_exchange import make_token_exchange_router


def _maybe_skip():
    if not os.environ.get("NEXUS_TEST_DATABASE_URL"):
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    if not (os.environ.get("LOCALSTACK_ENDPOINT") or os.environ.get("NEXUS_TEST_AWS_LIVE")):
        pytest.skip("LOCALSTACK_ENDPOINT or NEXUS_TEST_AWS_LIVE required")


def _make_signer():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    pk = ec.generate_private_key(ec.SECP256R1())
    return JwtSigner.from_pem(
        pk.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        issuer="https://e2e.local",
    )


def test_s3_list_buckets_as_user_via_token_exchange():
    _maybe_skip()
    import boto3

    endpoint = os.environ.get("LOCALSTACK_ENDPOINT")
    using_live = os.environ.get("NEXUS_TEST_AWS_LIVE") == "1"

    # 1. Provision creds — for LocalStack the canonical "test" creds work.
    if using_live:
        # User / CI must export AWS creds for the sandbox account.
        aws_creds = {
            "access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
            "secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
            "session_token": os.environ.get("AWS_SESSION_TOKEN", ""),
            "expiration": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "region": os.environ.get("AWS_REGION", "us-east-1"),
        }
    else:
        aws_creds = {
            "access_key_id": "test",
            "secret_access_key": "test",
            "session_token": "test",
            "expiration": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "region": "us-east-1",
        }

    # 2. Set up DB + envelope + app stack.
    engine = create_engine(os.environ["NEXUS_TEST_DATABASE_URL"], future=True)
    ensure_schema(engine)
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()

    encryption = InMemoryEncryptionProvider()
    payload = json.dumps(aws_creds).encode()
    aad = (
        str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"aws-default"
    )
    dek = b"\x09" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant, aad=aad)

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'e2e') ON CONFLICT DO NOTHING"),
            {"id": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal), "t": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, 'aws-default', 'aws', 'me', 'envelope', 'k', "
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant), "p": str(principal),
                "ct": ct, "wd": wrapped, "no": nonce, "aad": aad, "kv": kv,
            },
        )

    signer = _make_signer()
    consumer = CredentialConsumer(
        store=PostgresAuthProfileStore(engine=engine, tenant_id=tenant),
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=ResolvedCredCache(),
        adapters={"aws": AwsProviderAdapter()},
        audit=ReadAuditWriter(engine=engine),
    )
    app = FastAPI()
    app.include_router(
        make_token_exchange_router(
            enabled=True, signer=signer, consumer=consumer, encryption=encryption,
        )
    )

    # 3. Call token-exchange.
    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:aws",
            "scope": "list-buckets",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    meta = body["nexus_credential_metadata"]

    # 4. Build a real boto3 session and call S3.
    s3_kwargs = dict(
        aws_access_key_id=meta["access_key_id"],
        aws_secret_access_key=meta["secret_access_key"],
        aws_session_token=body["access_token"],
        region_name=meta["region"],
    )
    if endpoint:
        s3_kwargs["endpoint_url"] = endpoint

    s3 = boto3.client("s3", **s3_kwargs)
    # In LocalStack: empty bucket list is fine — the call succeeding is the win.
    resp = s3.list_buckets()
    assert "Buckets" in resp

    engine.dispose()
```

- [ ] **Step 12.3: Run when env is set**

```bash
LOCALSTACK_ENDPOINT=http://localhost:4566 \
NEXUS_TEST_DATABASE_URL=postgresql://nexus@localhost/nexus_test \
pytest tests/e2e/auth_consumption/test_s3_as_user.py -v
```

Expected: PASS when both env vars set; SKIP otherwise.

- [ ] **Step 12.4: Commit**

```bash
git add tests/e2e/auth_consumption/__init__.py tests/e2e/auth_consumption/test_s3_as_user.py
git commit -m "test(#3818): e2e — daemon-push → token-exchange → S3 list-buckets via LocalStack"
```

---

## Task 13: E2E test — GitHub via real PAT

**Files:**
- Create: `tests/e2e/auth_consumption/test_github_as_user.py`

- [ ] **Step 13.1: Write the e2e test**

```python
"""E2E: daemon push → /v1/auth/token-exchange → real GitHub /user (#3818).

Requires:
  - NEXUS_TEST_DATABASE_URL (Postgres)
  - NEXUS_TEST_GITHUB_PAT (a real PAT with read:user) — falls back to skip.

The PAT is the "daemon-pushed" credential. We push it, exchange it, and
prove the returned token authenticates against GitHub's /user endpoint.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from nexus.bricks.auth.consumer import CredentialConsumer
from nexus.bricks.auth.consumer_cache import ResolvedCredCache
from nexus.bricks.auth.consumer_providers.github import GithubProviderAdapter
from nexus.bricks.auth.envelope import AESGCMEnvelope, DEKCache
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider
from nexus.bricks.auth.postgres_profile_store import (
    PostgresAuthProfileStore,
    ensure_schema,
)
from nexus.bricks.auth.read_audit import ReadAuditWriter
from nexus.server.api.v1.jwt_signer import DaemonClaims, JwtSigner
from nexus.server.api.v1.routers.token_exchange import make_token_exchange_router


def _maybe_skip():
    if not os.environ.get("NEXUS_TEST_DATABASE_URL"):
        pytest.skip("NEXUS_TEST_DATABASE_URL not set")
    if not os.environ.get("NEXUS_TEST_GITHUB_PAT"):
        pytest.skip("NEXUS_TEST_GITHUB_PAT not set")


def _make_signer():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    pk = ec.generate_private_key(ec.SECP256R1())
    return JwtSigner.from_pem(
        pk.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        issuer="https://e2e.local",
    )


def test_github_user_endpoint_as_user_via_token_exchange():
    _maybe_skip()

    pat = os.environ["NEXUS_TEST_GITHUB_PAT"]
    engine = create_engine(os.environ["NEXUS_TEST_DATABASE_URL"], future=True)
    ensure_schema(engine)
    tenant = uuid.uuid4()
    principal = uuid.uuid4()
    machine = uuid.uuid4()

    encryption = InMemoryEncryptionProvider()
    payload = json.dumps(
        {"token": pat, "scopes": ["read:user"], "token_type": "classic"}
    ).encode()
    aad = (
        str(tenant).encode() + b"|" + str(principal).encode() + b"|" + b"github-default"
    )
    dek = b"\x0a" * 32
    nonce, ct = AESGCMEnvelope().encrypt(dek, payload, aad=aad)
    wrapped, kv = encryption.wrap_dek(dek, tenant_id=tenant, aad=aad)

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_tenant = :t"), {"t": str(tenant)})
        conn.execute(
            text("INSERT INTO tenants (id, name) VALUES (:id, 'e2e') ON CONFLICT DO NOTHING"),
            {"id": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO principals (id, tenant_id, kind) VALUES (:id, :t, 'human') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(principal), "t": str(tenant)},
        )
        conn.execute(
            text(
                "INSERT INTO auth_profiles "
                "(tenant_id, principal_id, id, provider, account_identifier, "
                " backend, backend_key, last_synced_at, sync_ttl_seconds, "
                " ciphertext, wrapped_dek, nonce, aad, kek_version) "
                "VALUES (:t, :p, 'github-default', 'github', 'me', 'envelope', 'k', "
                " NOW(), 300, :ct, :wd, :no, :aad, :kv)"
            ),
            {
                "t": str(tenant), "p": str(principal),
                "ct": ct, "wd": wrapped, "no": nonce, "aad": aad, "kv": kv,
            },
        )

    signer = _make_signer()
    consumer = CredentialConsumer(
        store=PostgresAuthProfileStore(engine=engine, tenant_id=tenant),
        encryption=encryption,
        dek_cache=DEKCache(),
        cred_cache=ResolvedCredCache(),
        adapters={"github": GithubProviderAdapter()},
        audit=ReadAuditWriter(engine=engine),
    )
    app = FastAPI()
    app.include_router(
        make_token_exchange_router(
            enabled=True, signer=signer, consumer=consumer, encryption=encryption,
        )
    )

    jwt = signer.sign(
        DaemonClaims(tenant_id=tenant, principal_id=principal, machine_id=machine),
        ttl=timedelta(hours=1),
    )
    client = TestClient(app)
    r = client.post(
        "/v1/auth/token-exchange",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": jwt,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "resource": "urn:nexus:provider:github",
            "scope": "get-user",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    bearer = body["access_token"]

    # Real GitHub call.
    gh = httpx.get(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {bearer}", "User-Agent": "nexus-e2e"},
        timeout=10.0,
    )
    assert gh.status_code == 200, gh.text
    assert "login" in gh.json()

    engine.dispose()
```

- [ ] **Step 13.2: Run when env is set**

```bash
NEXUS_TEST_GITHUB_PAT=ghp_yourtoken \
NEXUS_TEST_DATABASE_URL=postgresql://nexus@localhost/nexus_test \
pytest tests/e2e/auth_consumption/test_github_as_user.py -v
```

Expected: PASS when env set; SKIP otherwise.

- [ ] **Step 13.3: Commit**

```bash
git add tests/e2e/auth_consumption/test_github_as_user.py
git commit -m "test(#3818): e2e — daemon-push → token-exchange → GitHub /user via real PAT"
```

---

## Task 14: Documentation updates

**Files:**
- Modify: existing deploy guide for envelope encryption (`docs/guides/auth-envelope-encryption.md`) — add a new section on token-exchange wiring
- (Or, if cleaner) Create: `docs/guides/auth-token-exchange.md`

- [ ] **Step 14.1: Decide which doc to extend**

Read `docs/guides/auth-envelope-encryption.md`. If it has a clean "Operator runbook" structure with one section per env-var, append a sibling section. If it's narrative, create the new file `docs/guides/auth-token-exchange.md`.

- [ ] **Step 14.2: Write the operator-facing content**

Cover:
1. **Env vars to set:**
   - `NEXUS_TOKEN_EXCHANGE_ENABLED=1` (default off)
   - Confirms `NEXUS_JWT_SIGNING_KEY` and `NEXUS_ENROLL_TOKEN_SECRET` already set (from PR 3)
   - KMS/Vault: how to swap `InMemoryEncryptionProvider` for `VaultTransitProvider` or `AwsKmsProvider` via `app.state.encryption_provider`

2. **What it does:** one paragraph linking to the spec.

3. **Audit / SOC 2:** point to `auth_profile_reads` table; document the 100%/1% sampling rule and how to query it (sample SQL).

4. **`purpose` field warning:** callers MUST NOT include credentials/PII; truncated at 256 chars.

5. **`mlockall(MCL_FUTURE)` recommendation** for production.

6. **Provider scope:** AWS + GitHub only; Gmail/gcloud follow-ups.

- [ ] **Step 14.3: Commit**

```bash
git add docs/guides/auth-token-exchange.md  # or auth-envelope-encryption.md if extended
git commit -m "docs(#3818): operator guide for /v1/auth/token-exchange"
```

---

## Task 15: Final verification + PR prep

- [ ] **Step 15.1: Run the full auth test suite**

Run: `pytest src/nexus/bricks/auth/ src/nexus/server/api/v1/ -v --tb=short`
Expected: all PASS (Postgres + provider tests skip cleanly when their envs are unset; everything else passes).

- [ ] **Step 15.2: Run mypy / ruff if the project uses them**

Run: `pre-commit run --all-files` (or whichever lint command the repo uses — check `.pre-commit-config.yaml`).
Expected: no errors on changed files.

- [ ] **Step 15.3: Manual smoke against a running server**

```bash
# Terminal 1: bring up a Postgres-backed Nexus stack with token-exchange enabled
NEXUS_AUTH_STORE=postgres \
NEXUS_TOKEN_EXCHANGE_ENABLED=1 \
NEXUS_JWT_SIGNING_KEY=$HOME/.nexus/dev_jwt.pem \
NEXUS_ENROLL_TOKEN_SECRET=$(openssl rand -hex 32) \
nexus serve

# Terminal 2: enroll daemon, push a fake github profile, then exchange
nexus daemon join --enroll-token <minted-token>
# (push happens automatically; or seed via direct DB insert)
curl -X POST http://localhost:8000/v1/auth/token-exchange \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=$(cat ~/.nexus/daemons/default/jwt)" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:jwt" \
  -d "resource=urn:nexus:provider:github" \
  -d "scope=manual-smoke"
# Expect: 200 + JSON body containing access_token
```

If running this manually is impractical in your environment (e.g. no AWS sandbox handy), document why in the PR description and rely on the integration + e2e suite.

- [ ] **Step 15.4: Open the PR**

```bash
gh pr create --title "feat(auth): server-side credential consumption (#3818)" --body "$(cat <<'EOF'
Closes #3818. Final piece of the multi-tenant Postgres-backed auth epic (#3788) — the read path.

## Summary

Implements RFC 8693 token-exchange against daemon-pushed envelopes. A caller presents a daemon JWT (`subject_token`); the server verifies it, decrypts the matching `auth_profiles` envelope, materializes a provider-native bearer (AWS or GitHub), writes a `auth_profile_reads` row, and returns the credential.

## What's new

- `CredentialConsumer` orchestrator (cache → decrypt → adapter → audit)
- `ResolvedCredCache` with TTL = `min(300s, expires_at - 60s)`
- AWS + GitHub provider adapters (pure JSON → `MaterializedCredential`)
- `auth_profile_reads` partitioned table + RLS (mirror of `auth_profile_writes`)
- 1% cache-hit / 100% cache-miss audit sampling
- `/v1/auth/token-exchange` rewritten from 501 stub to live (gated by `NEXUS_TOKEN_EXCHANGE_ENABLED`)
- E2E tests: LocalStack S3 list-buckets, real GitHub `/user` (both gated)
- Operator guide updates

## What's deferred (separate issues)

- Gmail adapter (OAuth refresh via `TokenManager`)
- gcloud adapter
- Service-identity JWTs (caller_kind beyond "daemon")
- Server-driven OAuth refresh (option B from spec)

## Spec

`docs/superpowers/specs/2026-04-23-issue-3818-server-credential-consumption-design.md`

## Test plan

- [x] Unit: consumer / cache / adapters / audit / metrics
- [x] Integration (Postgres): `decrypt_profile()` honors RLS, returns plaintext + kek_version
- [x] Router: all HTTP error mappings, force_refresh, 501-when-disabled
- [x] E2E (LocalStack): daemon-push → token-exchange → real S3 call
- [x] E2E (live PAT): daemon-push → token-exchange → GitHub `/user`
- [ ] Reviewer: confirm `mlockall` recommendation + `purpose`-PII warning are in the operator guide
- [ ] Reviewer: confirm token-exchange OFF-by-default in default env (NEXUS_TOKEN_EXCHANGE_ENABLED)
EOF
)"
```

- [ ] **Step 15.5: Move issue to in-review**

```bash
# (if your workflow uses Linear / GH project columns, transition #3818 here)
```

---

## Spec coverage checklist

- ✅ `/v1/auth/token-exchange` (RFC 8693): Tasks 10, 11
- ✅ `CredentialConsumer.resolve()`: Task 8
- ✅ `ResolvedCredCache` with `min(300, exp-60)` TTL: Task 6
- ✅ AWS + GitHub adapters: Tasks 4, 5
- ✅ `auth_profile_reads` partitioned + RLS: Task 1
- ✅ 100% miss / 1% hit sampling: Task 7
- ✅ `decrypt_profile()` on store: Task 2
- ✅ Daemon-driven refresh (StaleSource → 409 → daemon catches up): Task 8 + Task 10 (router error map)
- ✅ E2E LocalStack + nightly live: Tasks 12, 13
- ✅ Prometheus metrics: Task 9
- ✅ Operator guide: Task 14
- ✅ Final verification + PR: Task 15
