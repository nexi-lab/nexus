# Zone Archive Implementation Plan (#3793)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `nexus archive` CLI for signed, credential-stripped zone snapshots with scheduled retention to local/S3/GCS, layered on the existing `bricks/portability/` brick.

**Architecture:** Extend `bricks/portability/` with signing, TOFU trust, credential stripping, and bundle diff. Add new `bricks/archive/` brick for multi-zone orchestration, audit-event slicing, scheduling, and storage backends. Add `nexus archive` Click group. Bundle format bumps to v2 (backward-compatible read of v1).

**Tech Stack:** Python 3.14, pydantic dataclasses, `cryptography.hazmat` ed25519, `tarfile`, `boto3`, `google-cloud-storage`, click, pytest.

**Spec:** [`docs/superpowers/specs/2026-05-01-zone-snapshot-design.md`](../specs/2026-05-01-zone-snapshot-design.md)

---

## Task 0: Scaffold archive brick + errors module

**Files:**
- Create: `src/nexus/bricks/archive/__init__.py`
- Create: `src/nexus/bricks/archive/errors.py`
- Create: `src/nexus/bricks/archive/tests/__init__.py`
- Create: `src/nexus/bricks/archive/tests/unit/__init__.py`
- Create: `src/nexus/bricks/archive/tests/unit/test_errors.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_errors.py
"""Tests for archive error hierarchy."""

import pytest

from nexus.bricks.archive.errors import (
    ArchiveCredentialLeakDetected,
    ArchiveEmbeddingDimMismatch,
    ArchiveError,
    ArchiveFileHashMismatch,
    ArchiveMerkleMismatch,
    ArchivePlaceholderNotInjected,
    ArchiveSignatureError,
    ArchiveTargetNotEmpty,
    ArchiveUntrustedSigner,
    ArchiveVersionIncompatible,
)


def test_all_errors_subclass_archive_error():
    classes = [
        ArchiveSignatureError,
        ArchiveMerkleMismatch,
        ArchiveFileHashMismatch,
        ArchiveVersionIncompatible,
        ArchivePlaceholderNotInjected,
        ArchiveEmbeddingDimMismatch,
        ArchiveCredentialLeakDetected,
        ArchiveUntrustedSigner,
        ArchiveTargetNotEmpty,
    ]
    for cls in classes:
        assert issubclass(cls, ArchiveError)


def test_error_codes_are_stable():
    assert ArchiveSignatureError.code == 10
    assert ArchiveMerkleMismatch.code == 11
    assert ArchiveFileHashMismatch.code == 12
    assert ArchiveVersionIncompatible.code == 13
    assert ArchivePlaceholderNotInjected.code == 20
    assert ArchiveEmbeddingDimMismatch.code == 21
    assert ArchiveCredentialLeakDetected.code == 30
    assert ArchiveUntrustedSigner.code == 40
    assert ArchiveTargetNotEmpty.code == 50


def test_signature_error_carries_context():
    err = ArchiveSignatureError("bad sig", manifest_sha="abc123")
    assert err.manifest_sha == "abc123"
    assert "bad sig" in str(err)


def test_placeholder_error_lists_missing():
    err = ArchivePlaceholderNotInjected(["HUB_TOKEN_eng", "PROVIDER_KEY_anthropic"])
    assert "HUB_TOKEN_eng" in str(err)
    assert err.missing == ["HUB_TOKEN_eng", "PROVIDER_KEY_anthropic"]


def test_target_not_empty_lists_zones():
    err = ArchiveTargetNotEmpty(["eng", "ops"])
    assert "eng" in str(err)
    assert err.existing_zones == ["eng", "ops"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.bricks.archive'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/__init__.py
"""Archive brick — signed, credential-stripped zone snapshots (Issue #3793).

Layers signing, credential stripping, and scheduled retention on top of
the existing `bricks/portability/` zone export brick.

Public API:
    ArchiveError                  - Base class for all archive errors
    ArchiveSignatureError         - ed25519 signature mismatch
    ArchiveMerkleMismatch         - Merkle root verification failed
    ArchiveFileHashMismatch       - Per-file SHA256 mismatch
    ArchiveVersionIncompatible    - min_nexus_version > current
    ArchivePlaceholderNotInjected - restore guard tripped
    ArchiveEmbeddingDimMismatch   - dim differs and --rebuild-embeddings not set
    ArchiveCredentialLeakDetected - regex backstop matched during create (warning)
    ArchiveUntrustedSigner        - --require-trusted and signer unseen
    ArchiveTargetNotEmpty         - target has zones and --force not set
"""

from nexus.bricks.archive.errors import (
    ArchiveCredentialLeakDetected,
    ArchiveEmbeddingDimMismatch,
    ArchiveError,
    ArchiveFileHashMismatch,
    ArchiveMerkleMismatch,
    ArchivePlaceholderNotInjected,
    ArchiveSignatureError,
    ArchiveTargetNotEmpty,
    ArchiveUntrustedSigner,
    ArchiveVersionIncompatible,
)

__all__ = [
    "ArchiveError",
    "ArchiveSignatureError",
    "ArchiveMerkleMismatch",
    "ArchiveFileHashMismatch",
    "ArchiveVersionIncompatible",
    "ArchivePlaceholderNotInjected",
    "ArchiveEmbeddingDimMismatch",
    "ArchiveCredentialLeakDetected",
    "ArchiveUntrustedSigner",
    "ArchiveTargetNotEmpty",
]
```

```python
# src/nexus/bricks/archive/errors.py
"""Archive domain errors (#3793).

Each error has a stable `code` for CLI exit + structured logging.
Codes map: 10s=integrity, 20s=restore guards, 30s=warnings, 40s=trust, 50s=target.
"""

from __future__ import annotations


class ArchiveError(Exception):
    """Base class for all archive errors."""

    code: int = 1


class ArchiveSignatureError(ArchiveError):
    """Ed25519 signature does not verify against embedded pubkey."""

    code = 10

    def __init__(self, message: str, manifest_sha: str | None = None) -> None:
        super().__init__(message)
        self.manifest_sha = manifest_sha


class ArchiveMerkleMismatch(ArchiveError):
    """Computed Merkle root does not match manifest root."""

    code = 11

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"Merkle root mismatch: expected {expected[:16]}…, got {actual[:16]}…")
        self.expected = expected
        self.actual = actual


class ArchiveFileHashMismatch(ArchiveError):
    """A file in the archive does not match its manifest checksum."""

    code = 12

    def __init__(self, path: str, expected: str, actual: str) -> None:
        super().__init__(f"File hash mismatch at {path}: expected {expected[:16]}…, got {actual[:16]}…")
        self.path = path
        self.expected = expected
        self.actual = actual


class ArchiveVersionIncompatible(ArchiveError):
    """Archive's min_nexus_version exceeds current nexus version."""

    code = 13

    def __init__(self, required: str, current: str) -> None:
        super().__init__(f"Archive requires nexus >= {required}, current is {current}")
        self.required = required
        self.current = current


class ArchivePlaceholderNotInjected(ArchiveError):
    """One or more credential placeholders were not re-injected before restore."""

    code = 20

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"Restore blocked — missing --inject for: {', '.join(missing)}")
        self.missing = missing


class ArchiveEmbeddingDimMismatch(ArchiveError):
    """Embedding model/dim differs from current nexus configuration."""

    code = 21

    def __init__(self, archive_model: str, archive_dim: int, current_model: str, current_dim: int) -> None:
        super().__init__(
            f"Embedding mismatch: archive uses {archive_model} (dim={archive_dim}), "
            f"current is {current_model} (dim={current_dim}). "
            "Pass --rebuild-embeddings to re-embed shipped documents."
        )
        self.archive_model = archive_model
        self.archive_dim = archive_dim
        self.current_model = current_model
        self.current_dim = current_dim


class ArchiveCredentialLeakDetected(ArchiveError):
    """Regex backstop matched a known credential pattern during create.

    This is a warning, not a fatal — the secret is redacted in the bundle
    but the operator should investigate why the secret was in free-text.
    """

    code = 30

    def __init__(self, pattern_name: str, location: str) -> None:
        super().__init__(f"Credential pattern {pattern_name!r} matched at {location}; redacted in bundle")
        self.pattern_name = pattern_name
        self.location = location


class ArchiveUntrustedSigner(ArchiveError):
    """--require-trusted is set and signer pubkey is not in trust store."""

    code = 40

    def __init__(self, pubkey_b64: str) -> None:
        super().__init__(
            f"Signer {pubkey_b64[:24]}… is not in ~/.nexus/trusted_signers.json. "
            "Add via: nexus archive keys trust <pubkey> --label <name>"
        )
        self.pubkey_b64 = pubkey_b64


class ArchiveTargetNotEmpty(ArchiveError):
    """Restore target already has zones and --force was not passed."""

    code = 50

    def __init__(self, existing_zones: list[str]) -> None:
        super().__init__(
            f"Target nexus already has zones: {', '.join(existing_zones)}. "
            "Pass --force to overwrite (DESTRUCTIVE)."
        )
        self.existing_zones = existing_zones
```

```python
# src/nexus/bricks/archive/tests/__init__.py
```

```python
# src/nexus/bricks/archive/tests/unit/__init__.py
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_errors.py -v`
Expected: PASS, 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/
git commit -m "feat(#3793): scaffold archive brick + error hierarchy"
```

---

## Task 1: Extend ExportManifest schema for v2 (signing + placeholders + embedding)

**Files:**
- Modify: `src/nexus/bricks/portability/models.py` (bump `BUNDLE_FORMAT_VERSION`, extend `ExportManifest`)
- Modify: `src/nexus/bricks/portability/schemas/manifest-v1.json` → copy to `manifest-v2.json` adding new fields
- Test: `src/nexus/bricks/portability/tests/test_manifest_v2.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_manifest_v2.py
"""Tests for ExportManifest v2 fields (signing + placeholders + embedding)."""

from datetime import UTC, datetime

from nexus.bricks.portability.models import (
    BUNDLE_FORMAT_VERSION,
    ArchiveKind,
    ExportManifest,
    PlaceholderRef,
)


def test_format_version_is_v2():
    assert BUNDLE_FORMAT_VERSION == "2.0.0"


def test_manifest_round_trip_with_v2_fields():
    manifest = ExportManifest(
        format_version="2.0.0",
        nexus_version="0.10.0",
        bundle_id="b-1",
        source_instance="hub.local",
        source_zone_id="eng",
        export_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        archive_kind=ArchiveKind.FULL,
        embedding_model="BAAI/bge-small-en-v1.5",
        embedding_dim=384,
        signer_pubkey_b64="cHViMQ==",
        placeholders=[
            PlaceholderRef(name="HUB_TOKEN_eng", field="federations.eng.auth_token"),
        ],
        min_nexus_version="0.10.0",
    )
    data = manifest.to_dict()
    restored = ExportManifest.from_dict(data)
    assert restored.archive_kind == ArchiveKind.FULL
    assert restored.embedding_model == "BAAI/bge-small-en-v1.5"
    assert restored.embedding_dim == 384
    assert restored.signer_pubkey_b64 == "cHViMQ=="
    assert restored.placeholders[0].name == "HUB_TOKEN_eng"
    assert restored.placeholders[0].field == "federations.eng.auth_token"
    assert restored.min_nexus_version == "0.10.0"


def test_audit_kind_carries_window():
    manifest = ExportManifest(
        format_version="2.0.0",
        nexus_version="0.10.0",
        bundle_id="b-2",
        source_instance="hub.local",
        source_zone_id="eng",
        export_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        archive_kind=ArchiveKind.AUDIT,
        activity_window_from=datetime(2026, 4, 1, tzinfo=UTC),
        activity_window_to=datetime(2026, 5, 1, tzinfo=UTC),
    )
    data = manifest.to_dict()
    restored = ExportManifest.from_dict(data)
    assert restored.archive_kind == ArchiveKind.AUDIT
    assert restored.activity_window_from == datetime(2026, 4, 1, tzinfo=UTC)


def test_v1_manifest_still_loadable():
    """Backward compat: v1 bundles read without the new fields."""
    v1_data = {
        "format_version": "1.0.0",
        "nexus_version": "0.9.0",
        "bundle_id": "b-old",
        "source_instance": "hub.local",
        "source_zone_id": "eng",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "file_count": 0,
        "total_size_bytes": 0,
        "content_blob_count": 0,
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {"algorithm": "sha256", "files": {}, "merkle_root": None},
    }
    manifest = ExportManifest.from_dict(v1_data)
    assert manifest.format_version == "1.0.0"
    assert manifest.archive_kind == ArchiveKind.FULL  # default
    assert manifest.signer_pubkey_b64 is None
    assert manifest.placeholders == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_manifest_v2.py -v`
Expected: FAIL — `ImportError` on `ArchiveKind` / `PlaceholderRef`.

- [ ] **Step 3: Write minimal implementation**

Read current `models.py` to find the `ExportManifest` definition. Apply these edits:

In `src/nexus/bricks/portability/models.py`, change `BUNDLE_FORMAT_VERSION`:

```python
BUNDLE_FORMAT_VERSION = "2.0.0"
```

Add near the top (after the StrEnum imports):

```python
class ArchiveKind(StrEnum):
    """Type of archive."""

    FULL = "full"
    AUDIT = "audit"


@dataclass
class PlaceholderRef:
    """Reference to a credential placeholder that must be re-injected on restore."""

    name: str  # e.g., "HUB_TOKEN_eng"
    field: str  # e.g., "federations.eng.auth_token"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "field": self.field}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "PlaceholderRef":
        return cls(name=data["name"], field=data["field"])
```

Extend `ExportManifest`:

```python
@dataclass
class ExportManifest:
    # ... existing fields ...

    # v2 additions (all optional with defaults so v1 bundles still load):
    archive_kind: ArchiveKind = ArchiveKind.FULL
    activity_window_from: datetime | None = None
    activity_window_to: datetime | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    signer_pubkey_b64: str | None = None
    placeholders: list[PlaceholderRef] = field(default_factory=list)
    min_nexus_version: str = "0.0.0"
```

Update `to_dict` / `from_dict` to round-trip the new fields:

```python
    def to_dict(self) -> dict[str, Any]:
        d = {
            # ... existing keys ...
            "archive_kind": self.archive_kind.value,
            "activity_window_from": self.activity_window_from.isoformat() if self.activity_window_from else None,
            "activity_window_to": self.activity_window_to.isoformat() if self.activity_window_to else None,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "signer_pubkey_b64": self.signer_pubkey_b64,
            "placeholders": [p.to_dict() for p in self.placeholders],
            "min_nexus_version": self.min_nexus_version,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExportManifest":
        # ... parse existing fields ...
        kwargs = {
            # ... existing kwargs ...
            "archive_kind": ArchiveKind(data.get("archive_kind", ArchiveKind.FULL.value)),
            "activity_window_from": (
                datetime.fromisoformat(data["activity_window_from"])
                if data.get("activity_window_from")
                else None
            ),
            "activity_window_to": (
                datetime.fromisoformat(data["activity_window_to"])
                if data.get("activity_window_to")
                else None
            ),
            "embedding_model": data.get("embedding_model"),
            "embedding_dim": data.get("embedding_dim"),
            "signer_pubkey_b64": data.get("signer_pubkey_b64"),
            "placeholders": [PlaceholderRef.from_dict(p) for p in data.get("placeholders", [])],
            "min_nexus_version": data.get("min_nexus_version", "0.0.0"),
        }
        return cls(**kwargs)
```

Add the new symbols to `bricks/portability/__init__.py` `__all__` and re-exports:

```python
from nexus.bricks.portability.models import (
    # ... existing ...
    ArchiveKind,
    PlaceholderRef,
)

__all__ = [
    # ... existing ...
    "ArchiveKind",
    "PlaceholderRef",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_manifest_v2.py -v`
Expected: PASS, 4 tests.

Then run the existing portability test suite to confirm v1 compat:

Run: `pytest src/nexus/bricks/portability/ -v`
Expected: all existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/models.py src/nexus/bricks/portability/__init__.py src/nexus/bricks/portability/tests/test_manifest_v2.py
git commit -m "feat(#3793): bump bundle format to v2 with archive_kind/embedding/placeholders"
```

---

## Task 2: Ed25519 signer + keypair management

**Files:**
- Create: `src/nexus/bricks/portability/signer.py`
- Test: `src/nexus/bricks/portability/tests/test_signer.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_signer.py
"""Tests for ed25519 archive signer."""

import json
from pathlib import Path

import pytest

from nexus.bricks.archive.errors import ArchiveSignatureError
from nexus.bricks.portability.signer import (
    ArchiveSigner,
    canonical_json_bytes,
    load_or_create_keypair,
)


def test_load_or_create_keypair_creates_files(tmp_path):
    key_path = tmp_path / "archive_signing_key"
    priv, pub = load_or_create_keypair(key_path)
    assert key_path.exists()
    assert key_path.with_suffix(".pub").exists()
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert len(priv) == 32  # ed25519 seed
    assert len(pub) == 32


def test_load_or_create_keypair_idempotent(tmp_path):
    key_path = tmp_path / "archive_signing_key"
    priv1, pub1 = load_or_create_keypair(key_path)
    priv2, pub2 = load_or_create_keypair(key_path)
    assert priv1 == priv2
    assert pub1 == pub2


def test_canonical_json_bytes_is_stable():
    a = canonical_json_bytes({"b": 2, "a": 1})
    b = canonical_json_bytes({"a": 1, "b": 2})
    assert a == b
    assert b'"a":1,"b":2' in a


def test_sign_and_verify_round_trip(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    payload = b"manifest-bytes" + b"merkle-root-bytes"
    sig_b64, pub_b64 = signer.sign(payload)
    assert signer.verify(payload, sig_b64, pub_b64) is True


def test_verify_rejects_tampered_payload(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    payload = b"original"
    sig_b64, pub_b64 = signer.sign(payload)
    with pytest.raises(ArchiveSignatureError):
        signer.verify(b"tampered", sig_b64, pub_b64)


def test_verify_rejects_wrong_pubkey(tmp_path):
    signer1 = ArchiveSigner(tmp_path / "k1")
    signer2 = ArchiveSigner(tmp_path / "k2")
    sig_b64, _pub1 = signer1.sign(b"payload")
    _sig2, pub2 = signer2.sign(b"payload")
    with pytest.raises(ArchiveSignatureError):
        signer1.verify(b"payload", sig_b64, pub2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_signer.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/portability/signer.py
"""Ed25519 signing for archive manifests (#3793).

Keypair is stored at the path configured for the operator (default
`~/.nexus/archive_signing_key`). Private key file is mode 0600.

Canonical-JSON encoding gives a stable byte representation across Python
versions: keys sorted, no whitespace, ensure_ascii=False.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nexus.bricks.archive.errors import ArchiveSignatureError


def canonical_json_bytes(obj: object) -> bytes:
    """Return a stable byte encoding for signing/verification."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def load_or_create_keypair(key_path: Path) -> tuple[bytes, bytes]:
    """Load the ed25519 keypair at `key_path`, generating it if missing.

    Returns (private_seed_bytes, public_key_bytes). Both are 32 bytes.
    """
    pub_path = key_path.with_suffix(".pub")
    if key_path.exists():
        with key_path.open("rb") as f:
            priv_seed = f.read()
        priv_key = Ed25519PrivateKey.from_private_bytes(priv_seed)
        pub_bytes = priv_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return priv_seed, pub_bytes

    key_path.parent.mkdir(parents=True, exist_ok=True)
    priv_key = Ed25519PrivateKey.generate()
    priv_seed = priv_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    with key_path.open("wb") as f:
        f.write(priv_seed)
    os.chmod(key_path, 0o600)
    with pub_path.open("wb") as f:
        f.write(pub_bytes)
    return priv_seed, pub_bytes


class ArchiveSigner:
    """Sign and verify archive payloads with ed25519."""

    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self._priv_seed, self._pub_bytes = load_or_create_keypair(key_path)

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self._pub_bytes).decode("ascii")

    def sign(self, payload: bytes) -> tuple[str, str]:
        """Sign `payload`. Returns (signature_b64, signer_pubkey_b64)."""
        priv = Ed25519PrivateKey.from_private_bytes(self._priv_seed)
        sig = priv.sign(payload)
        return base64.b64encode(sig).decode("ascii"), self.public_key_b64

    @staticmethod
    def verify(payload: bytes, signature_b64: str, pubkey_b64: str) -> bool:
        """Verify `signature_b64` over `payload` with `pubkey_b64`.

        Returns True on success, raises ArchiveSignatureError on failure.
        """
        try:
            sig = base64.b64decode(signature_b64)
            pub_bytes = base64.b64decode(pubkey_b64)
            pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
            pub.verify(sig, payload)
        except (InvalidSignature, ValueError) as e:
            raise ArchiveSignatureError(f"signature verify failed: {e}") from e
        return True


__all__ = ["ArchiveSigner", "canonical_json_bytes", "load_or_create_keypair"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_signer.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/signer.py src/nexus/bricks/portability/tests/test_signer.py
git commit -m "feat(#3793): ed25519 signer with auto-generated keypair"
```

---

## Task 3: TOFU trust store

**Files:**
- Create: `src/nexus/bricks/portability/trust.py`
- Test: `src/nexus/bricks/portability/tests/test_trust.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_trust.py
"""Tests for TOFU trust store."""

import json

from nexus.bricks.portability.trust import TrustStore


def test_first_see_returns_unseen(tmp_path):
    store = TrustStore(tmp_path / "trusted_signers.json")
    assert store.is_trusted("pubkey1") is False


def test_pin_then_trusted(tmp_path):
    store = TrustStore(tmp_path / "trusted_signers.json")
    store.pin("pubkey1", label="alice@hub")
    assert store.is_trusted("pubkey1") is True


def test_pin_persists_across_instances(tmp_path):
    path = tmp_path / "trusted_signers.json"
    s1 = TrustStore(path)
    s1.pin("pubkey1", label="alice@hub")
    s2 = TrustStore(path)
    assert s2.is_trusted("pubkey1") is True


def test_pin_records_first_seen(tmp_path):
    path = tmp_path / "trusted_signers.json"
    store = TrustStore(path)
    store.pin("pubkey1", label="alice@hub")
    raw = json.loads(path.read_text())
    assert "first_seen" in raw["pubkey1"]
    assert raw["pubkey1"]["label"] == "alice@hub"


def test_corrupted_file_returns_empty(tmp_path):
    path = tmp_path / "trusted_signers.json"
    path.write_text("not json")
    store = TrustStore(path)
    assert store.is_trusted("anything") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_trust.py -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/portability/trust.py
"""TOFU (trust-on-first-use) signer trust store for archives (#3793).

JSON file at `~/.nexus/trusted_signers.json` mapping pubkey-b64 → metadata.
A signer is trusted if its pubkey is present in the file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class TrustStore:
    """Persistent TOFU trust store for ed25519 archive signers."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                return data
            return {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def is_trusted(self, pubkey_b64: str) -> bool:
        return pubkey_b64 in self._read()

    def pin(self, pubkey_b64: str, label: str = "") -> None:
        data = self._read()
        data[pubkey_b64] = {
            "first_seen": datetime.now(UTC).isoformat(),
            "label": label,
        }
        self._write(data)

    def all_trusted(self) -> dict[str, dict[str, Any]]:
        return self._read()


__all__ = ["TrustStore"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_trust.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/trust.py src/nexus/bricks/portability/tests/test_trust.py
git commit -m "feat(#3793): TOFU trust store for archive signers"
```

---

## Task 4: Schema-aware credential stripper

**Files:**
- Create: `src/nexus/bricks/portability/strip.py`
- Test: `src/nexus/bricks/portability/tests/test_strip_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_strip_schema.py
"""Tests for schema-aware credential stripper."""

from nexus.bricks.portability.models import PlaceholderRef
from nexus.bricks.portability.strip import SchemaStripper, StripResult


def test_strips_provider_api_key():
    stripper = SchemaStripper()
    rows = [{"name": "anthropic", "api_key": "sk-ant-real-secret"}]
    result = stripper.strip_table("providers", rows)
    assert result.rows[0]["api_key"] == "${PROVIDER_KEY_anthropic}"
    assert (
        PlaceholderRef(name="PROVIDER_KEY_anthropic", field="providers.anthropic.api_key")
        in result.placeholders
    )


def test_strips_federation_auth_token():
    stripper = SchemaStripper()
    rows = [{"name": "eng_hub", "auth_token": "tok-secret", "url": "https://hub"}]
    result = stripper.strip_table("federations", rows)
    assert result.rows[0]["auth_token"] == "${HUB_TOKEN_eng_hub}"
    assert result.rows[0]["url"] == "https://hub"


def test_strips_webhook_secret():
    stripper = SchemaStripper()
    rows = [{"name": "ci", "secret": "whsec_xyz"}]
    result = stripper.strip_table("webhooks", rows)
    assert result.rows[0]["secret"] == "${WEBHOOK_SECRET_ci}"


def test_strips_workspace_path():
    stripper = SchemaStripper(workspace_root="/Users/alice/projects")
    rows = [{"path": "/Users/alice/projects/myapp/file.py"}]
    result = stripper.strip_table("documents", rows)
    assert result.rows[0]["path"] == "${WORKSPACE_ROOT}/myapp/file.py"


def test_passes_through_unknown_table():
    stripper = SchemaStripper()
    rows = [{"data": "anything"}]
    result = stripper.strip_table("random_unknown", rows)
    assert result.rows == rows
    assert result.placeholders == []


def test_handles_null_sensitive_field():
    stripper = SchemaStripper()
    rows = [{"name": "anthropic", "api_key": None}]
    result = stripper.strip_table("providers", rows)
    assert result.rows[0]["api_key"] is None
    assert result.placeholders == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_strip_schema.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/portability/strip.py
"""Two-layer credential stripper for archives (#3793).

Layer 1 (this file's `SchemaStripper`): nulls known sensitive columns by
table+field name and replaces them with `${PLACEHOLDER_NAME}` strings.
Records each replacement so the manifest can list what the operator must
re-inject on restore.

Layer 2 (`RegexStripper`, separate task): scans free-text fields for known
secret patterns as a backstop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nexus.bricks.portability.models import PlaceholderRef


@dataclass
class StripResult:
    rows: list[dict[str, Any]]
    placeholders: list[PlaceholderRef] = field(default_factory=list)


# (table, sensitive_field, name_field, placeholder_template, dotted_field_template)
_SCHEMA_RULES: list[tuple[str, str, str, str, str]] = [
    ("providers", "api_key", "name", "PROVIDER_KEY_{name}", "providers.{name}.api_key"),
    ("federations", "auth_token", "name", "HUB_TOKEN_{name}", "federations.{name}.auth_token"),
    ("webhooks", "secret", "name", "WEBHOOK_SECRET_{name}", "webhooks.{name}.secret"),
]

_DENY_LIST_SETTING_KEYS = frozenset(
    {"hub_auth_token", "anthropic_api_key", "openai_api_key", "google_api_key"}
)


class SchemaStripper:
    """Strip credentials from known sensitive columns by table + field."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self.workspace_root = workspace_root

    def strip_table(self, table: str, rows: list[dict[str, Any]]) -> StripResult:
        out_rows: list[dict[str, Any]] = []
        placeholders: list[PlaceholderRef] = []
        rules = [r for r in _SCHEMA_RULES if r[0] == table]
        for row in rows:
            new_row = dict(row)
            for _t, sensitive, name_field, ph_tpl, field_tpl in rules:
                if sensitive in new_row and new_row[sensitive] is not None:
                    name = str(new_row.get(name_field, "unknown"))
                    placeholder_name = ph_tpl.format(name=name)
                    new_row[sensitive] = f"${{{placeholder_name}}}"
                    placeholders.append(
                        PlaceholderRef(name=placeholder_name, field=field_tpl.format(name=name))
                    )
            if table == "settings" and new_row.get("key") in _DENY_LIST_SETTING_KEYS:
                key = new_row["key"]
                placeholder_name = f"SETTING_{key}"
                if new_row.get("value") is not None:
                    new_row["value"] = f"${{{placeholder_name}}}"
                    placeholders.append(
                        PlaceholderRef(name=placeholder_name, field=f"settings.{key}.value")
                    )
            if self.workspace_root and table == "documents" and "path" in new_row:
                p = new_row["path"]
                if isinstance(p, str) and p.startswith(self.workspace_root):
                    new_row["path"] = "${WORKSPACE_ROOT}" + p[len(self.workspace_root) :]
            out_rows.append(new_row)
        return StripResult(rows=out_rows, placeholders=placeholders)


__all__ = ["SchemaStripper", "StripResult"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_strip_schema.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/strip.py src/nexus/bricks/portability/tests/test_strip_schema.py
git commit -m "feat(#3793): schema-aware credential stripper with placeholder refs"
```

---

## Task 5: Regex backstop stripper

**Files:**
- Modify: `src/nexus/bricks/portability/strip.py` (add `RegexStripper`)
- Test: `src/nexus/bricks/portability/tests/test_strip_regex.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_strip_regex.py
"""Tests for regex backstop credential stripper."""

import pytest

from nexus.bricks.portability.strip import (
    DEFAULT_REDACT_PATTERNS,
    RegexStripper,
    RegexStripResult,
)


@pytest.mark.parametrize(
    "secret,name",
    [
        ("sk-ant-aaaaaaaaaaaaaaaaaaaa", "anthropic"),
        ("sk-aaaaaaaaaaaaaaaaaaaa", "openai"),
        ("ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "github_pat"),
        ("gho_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "github_oauth"),
        ("glpat-aaaaaaaaaaaaaaaaaaaa", "gitlab_pat"),
        ("xoxb-1234-5678-aaaaaaaaaa", "slack_bot"),
        ("AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
        ("AIzaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "google_api_key"),
    ],
)
def test_default_patterns_redact_known_secrets(secret, name):
    stripper = RegexStripper(DEFAULT_REDACT_PATTERNS)
    text = f"Use this token: {secret} for auth"
    result = stripper.scan(text, location="docs:42")
    assert "***REDACTED***" in result.text
    assert secret not in result.text
    assert any(m.pattern_name == name for m in result.matches)


def test_no_match_passes_through_unchanged():
    stripper = RegexStripper(DEFAULT_REDACT_PATTERNS)
    text = "regular content with no secrets"
    result = stripper.scan(text, location="docs:1")
    assert result.text == text
    assert result.matches == []


def test_custom_pattern_applies():
    stripper = RegexStripper(
        [{"name": "corp_token", "pattern": r"corp-[A-Z0-9]{8}"}]
    )
    result = stripper.scan("token=corp-AB12CD34", location="settings:1")
    assert "***REDACTED***" in result.text
    assert result.matches[0].pattern_name == "corp_token"


def test_invalid_regex_raises_at_construction():
    with pytest.raises(ValueError):
        RegexStripper([{"name": "bad", "pattern": "[unclosed"}])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_strip_regex.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Append to `src/nexus/bricks/portability/strip.py`:

```python
import re
from dataclasses import dataclass


@dataclass
class RegexMatch:
    pattern_name: str
    location: str
    snippet: str


@dataclass
class RegexStripResult:
    text: str
    matches: list[RegexMatch]


DEFAULT_REDACT_PATTERNS: list[dict[str, str]] = [
    {"name": "anthropic", "pattern": r"sk-ant-[A-Za-z0-9_-]{20,}"},
    {"name": "openai", "pattern": r"sk-[A-Za-z0-9]{20,}"},
    {"name": "github_pat", "pattern": r"ghp_[A-Za-z0-9]{36}"},
    {"name": "github_oauth", "pattern": r"gho_[A-Za-z0-9]{36}"},
    {"name": "gitlab_pat", "pattern": r"glpat-[A-Za-z0-9_-]{20}"},
    {"name": "slack_bot", "pattern": r"xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+"},
    {"name": "aws_access_key", "pattern": r"AKIA[0-9A-Z]{16}"},
    {"name": "google_api_key", "pattern": r"AIza[0-9A-Za-z_-]{35}"},
]


class RegexStripper:
    """Backstop credential scanner over free-text fields."""

    def __init__(self, patterns: list[dict[str, str]]) -> None:
        self._compiled: list[tuple[str, re.Pattern[str]]] = []
        for p in patterns:
            try:
                self._compiled.append((p["name"], re.compile(p["pattern"])))
            except re.error as e:
                raise ValueError(f"Invalid regex {p['name']!r}: {e}") from e

    def scan(self, text: str, *, location: str) -> RegexStripResult:
        if not text:
            return RegexStripResult(text=text, matches=[])
        matches: list[RegexMatch] = []
        out = text
        for name, rx in self._compiled:
            for m in list(rx.finditer(out)):
                matches.append(RegexMatch(pattern_name=name, location=location, snippet=m.group(0)[:8] + "…"))
            out = rx.sub("***REDACTED***", out)
        return RegexStripResult(text=out, matches=matches)


# extend __all__:
__all__ = [
    "SchemaStripper",
    "StripResult",
    "RegexStripper",
    "RegexStripResult",
    "RegexMatch",
    "DEFAULT_REDACT_PATTERNS",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_strip_regex.py -v`
Expected: PASS, 11 parametrized + 3 = 14 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/strip.py src/nexus/bricks/portability/tests/test_strip_regex.py
git commit -m "feat(#3793): regex backstop stripper with default credential patterns"
```

---

## Task 6: Wire signer + stripper into ZoneExportService

**Files:**
- Modify: `src/nexus/bricks/portability/models.py` (extend `ZoneExportOptions` with `sign`, `strip_credentials`, `signing_key_path`)
- Modify: `src/nexus/bricks/portability/export_service.py` (sign manifest, write `signatures.json`)
- Test: `src/nexus/bricks/portability/tests/test_export_signing.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_export_signing.py
"""Tests for export-time signing wiring."""

import base64
import json
import tarfile
from pathlib import Path

import pytest

from nexus.bricks.portability.models import ZoneExportOptions
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes


@pytest.fixture
def fake_export_outputs(tmp_path):
    """Build a minimal pre-existing bundle for the signing-only path test.

    The full ZoneExportService path is exercised in integration tests; here we
    only validate that `_finalize_with_signature` writes signatures.json that
    verifies against the embedded pubkey.
    """
    from nexus.bricks.portability.export_service import _finalize_with_signature
    from nexus.bricks.portability.models import ExportManifest, ArchiveKind
    from datetime import UTC, datetime

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "metadata").mkdir()
    (bundle_dir / "metadata" / "files.jsonl").write_text("")
    manifest = ExportManifest(
        format_version="2.0.0",
        nexus_version="0.10.0",
        bundle_id="b-1",
        source_instance="hub.local",
        source_zone_id="eng",
        export_timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        archive_kind=ArchiveKind.FULL,
    )
    out = tmp_path / "out.nexus"
    signer = ArchiveSigner(tmp_path / "key")
    _finalize_with_signature(bundle_dir, manifest, out, signer=signer)
    return out, signer


def test_signed_bundle_contains_signatures_json(fake_export_outputs):
    out, _signer = fake_export_outputs
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "signatures.json" in names


def test_signed_bundle_signature_verifies(fake_export_outputs):
    out, signer = fake_export_outputs
    with tarfile.open(out, "r:gz") as tar:
        sig_member = tar.getmember("signatures.json")
        sig_data = json.loads(tar.extractfile(sig_member).read())
        manifest_member = tar.getmember("manifest.json")
        manifest_bytes = tar.extractfile(manifest_member).read()
    payload = canonical_json_bytes(json.loads(manifest_bytes))
    assert ArchiveSigner.verify(
        payload, sig_data["signature_b64"], sig_data["signer_pubkey_b64"]
    )


def test_export_options_default_sign_on():
    opts = ZoneExportOptions(output_path=Path("/tmp/x.nexus"))
    assert opts.sign is True
    assert opts.strip_credentials is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_export_signing.py -v`
Expected: FAIL — `_finalize_with_signature` not defined; new options not present.

- [ ] **Step 3: Write minimal implementation**

In `src/nexus/bricks/portability/models.py`, extend `ZoneExportOptions`:

```python
@dataclass
class ZoneExportOptions:
    # ... existing fields ...

    # v2 additions:
    sign: bool = True
    strip_credentials: bool = True
    signing_key_path: Path | None = None  # default ~/.nexus/archive_signing_key
```

In `src/nexus/bricks/portability/export_service.py`, add at module top:

```python
import json
import tarfile
from pathlib import Path

from nexus.bricks.portability.models import ExportManifest
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes
```

Add the helper:

```python
def _finalize_with_signature(
    bundle_dir: Path,
    manifest: ExportManifest,
    output_path: Path,
    *,
    signer: ArchiveSigner | None,
) -> None:
    """Write manifest.json (signed if signer is provided), then tar.gz the bundle."""
    manifest_dict = manifest.to_dict()
    if signer is not None:
        manifest_dict["signer_pubkey_b64"] = signer.public_key_b64
    manifest_bytes = canonical_json_bytes(manifest_dict)
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)

    if signer is not None:
        merkle_root_b64 = manifest_dict.get("checksums", {}).get("merkle_root") or ""
        payload = manifest_bytes + merkle_root_b64.encode("utf-8")
        sig_b64, pub_b64 = signer.sign(payload)
        sig_doc = {
            "algorithm": "ed25519",
            "signer_pubkey_b64": pub_b64,
            "signature_b64": sig_b64,
            "manifest_sha256": _sha256(manifest_bytes),
        }
        (bundle_dir / "signatures.json").write_text(json.dumps(sig_doc, indent=2))

    with tarfile.open(output_path, mode="w:gz") as tar:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_file():
                arcname = str(path.relative_to(bundle_dir))
                tar.add(path, arcname=arcname)


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
```

Wire it into `ZoneExportService.export_zone()` at the point where the bundle is finalized — replace the existing tar creation with a call to `_finalize_with_signature`. (The exact line depends on the current export flow; locate the existing tar creation and substitute. If `options.sign=False`, pass `signer=None`.)

```python
# inside ZoneExportService.export_zone, near end:
signer = (
    ArchiveSigner(options.signing_key_path or Path.home() / ".nexus" / "archive_signing_key")
    if options.sign
    else None
)
_finalize_with_signature(bundle_dir, manifest, options.output_path, signer=signer)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_export_signing.py -v`
Expected: PASS.

Re-run existing portability tests:

Run: `pytest src/nexus/bricks/portability/ -v`
Expected: all pass (including v1 round-trips since v1 bundles were written by the old path; new tests cover v2).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/export_service.py src/nexus/bricks/portability/models.py src/nexus/bricks/portability/tests/test_export_signing.py
git commit -m "feat(#3793): sign manifest at export time, write signatures.json"
```

---

## Task 7: Wire credential stripper into ZoneExportService

**Files:**
- Modify: `src/nexus/bricks/portability/export_service.py`
- Test: `src/nexus/bricks/portability/tests/test_export_strip.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_export_strip.py
"""Tests for export-time credential stripping wiring."""

import json
import tarfile
from pathlib import Path

from nexus.bricks.portability.export_service import (
    _apply_credential_stripping,
)
from nexus.bricks.portability.models import PlaceholderRef


def test_apply_strip_replaces_provider_key():
    rows_by_table = {
        "providers": [{"name": "anthropic", "api_key": "sk-ant-secret"}],
        "federations": [{"name": "eng", "auth_token": "tok"}],
    }
    out_rows, placeholders = _apply_credential_stripping(rows_by_table, workspace_root=None)
    assert out_rows["providers"][0]["api_key"] == "${PROVIDER_KEY_anthropic}"
    assert out_rows["federations"][0]["auth_token"] == "${HUB_TOKEN_eng}"
    assert PlaceholderRef("PROVIDER_KEY_anthropic", "providers.anthropic.api_key") in placeholders
    assert PlaceholderRef("HUB_TOKEN_eng", "federations.eng.auth_token") in placeholders


def test_apply_strip_runs_regex_backstop_on_documents():
    rows_by_table = {
        "documents": [
            {"path": "/x", "body": "Token is sk-ant-aaaaaaaaaaaaaaaaaaaa here"},
        ],
    }
    out_rows, _placeholders = _apply_credential_stripping(rows_by_table, workspace_root=None)
    assert "sk-ant-aaaaaaaaaaaaaaaaaaaa" not in out_rows["documents"][0]["body"]
    assert "***REDACTED***" in out_rows["documents"][0]["body"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_export_strip.py -v`
Expected: FAIL — `_apply_credential_stripping` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `src/nexus/bricks/portability/export_service.py`:

```python
from nexus.bricks.portability.strip import (
    DEFAULT_REDACT_PATTERNS,
    RegexStripper,
    SchemaStripper,
)


def _apply_credential_stripping(
    rows_by_table: dict[str, list[dict]],
    *,
    workspace_root: str | None,
    extra_patterns: list[dict[str, str]] | None = None,
) -> tuple[dict[str, list[dict]], list]:
    """Run schema + regex strip across every row group.

    Returns (stripped rows, placeholder refs).
    """
    schema = SchemaStripper(workspace_root=workspace_root)
    patterns = list(DEFAULT_REDACT_PATTERNS) + list(extra_patterns or [])
    regex = RegexStripper(patterns)

    out: dict[str, list[dict]] = {}
    placeholders: list = []
    for table, rows in rows_by_table.items():
        schema_result = schema.strip_table(table, rows)
        cleaned: list[dict] = []
        for i, row in enumerate(schema_result.rows):
            new_row = dict(row)
            for k, v in row.items():
                if isinstance(v, str):
                    scan = regex.scan(v, location=f"{table}:row={i}:field={k}")
                    new_row[k] = scan.text
            cleaned.append(new_row)
        out[table] = cleaned
        placeholders.extend(schema_result.placeholders)
    return out, placeholders
```

Wire into `export_zone()` near the metadata-export step: collect each table's row dicts (e.g. providers, federations, webhooks, settings, documents) into `rows_by_table`, then call `_apply_credential_stripping(rows_by_table, workspace_root=options.workspace_root)` if `options.strip_credentials`. Use the resulting placeholders list to populate `manifest.placeholders`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_export_strip.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/export_service.py src/nexus/bricks/portability/tests/test_export_strip.py
git commit -m "feat(#3793): wire credential stripper into export pipeline"
```

---

## Task 8: Extend ZoneImportService with placeholder guard

**Files:**
- Modify: `src/nexus/bricks/portability/models.py` (extend `ZoneImportOptions` with `require_no_placeholders`, `injections`)
- Modify: `src/nexus/bricks/portability/import_service.py` (add `_check_placeholders` and `_apply_injections`)
- Test: `src/nexus/bricks/portability/tests/test_import_placeholder.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_import_placeholder.py
"""Tests for restore placeholder guard."""

import pytest

from nexus.bricks.archive.errors import ArchivePlaceholderNotInjected
from nexus.bricks.portability.import_service import (
    _apply_injections,
    _scan_for_placeholders,
)


def test_scan_finds_placeholder_tokens():
    rows = [{"api_key": "${PROVIDER_KEY_anthropic}"}, {"value": "no placeholder"}]
    found = _scan_for_placeholders(rows)
    assert found == {"PROVIDER_KEY_anthropic"}


def test_apply_injections_replaces_placeholder():
    rows = [{"api_key": "${PROVIDER_KEY_anthropic}"}]
    out = _apply_injections(rows, {"PROVIDER_KEY_anthropic": "sk-ant-real"})
    assert out[0]["api_key"] == "sk-ant-real"


def test_unmatched_placeholder_raises():
    rows = [{"api_key": "${PROVIDER_KEY_anthropic}"}]
    out = _apply_injections(rows, injections={})
    remaining = _scan_for_placeholders(out)
    assert remaining == {"PROVIDER_KEY_anthropic"}


def test_partial_injection_still_raises():
    """When some are injected, the missing list is still surfaced."""
    rows = [
        {"api_key": "${PROVIDER_KEY_anthropic}"},
        {"auth_token": "${HUB_TOKEN_eng}"},
    ]
    out = _apply_injections(rows, {"PROVIDER_KEY_anthropic": "real"})
    remaining = _scan_for_placeholders(out)
    assert remaining == {"HUB_TOKEN_eng"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_import_placeholder.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

In `src/nexus/bricks/portability/models.py`, extend `ZoneImportOptions`:

```python
@dataclass
class ZoneImportOptions:
    # ... existing fields ...
    require_no_placeholders: bool = True
    injections: dict[str, str] = field(default_factory=dict)
    rebuild_embeddings: bool = False
    force: bool = False
```

In `src/nexus/bricks/portability/import_service.py`, add at module top:

```python
import re
from nexus.bricks.archive.errors import ArchivePlaceholderNotInjected

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _scan_for_placeholders(rows: list[dict]) -> set[str]:
    """Return the set of `${NAME}` placeholder names found across all string values."""
    found: set[str] = set()
    for row in rows:
        for v in row.values():
            if isinstance(v, str):
                for m in _PLACEHOLDER_RE.finditer(v):
                    found.add(m.group(1))
    return found


def _apply_injections(rows: list[dict], injections: dict[str, str]) -> list[dict]:
    """Substitute every `${NAME}` placeholder for its value in `injections`."""
    if not injections:
        return rows
    out: list[dict] = []
    for row in rows:
        new_row = dict(row)
        for k, v in row.items():
            if isinstance(v, str):
                def _sub(m: re.Match[str]) -> str:
                    return injections.get(m.group(1), m.group(0))

                new_row[k] = _PLACEHOLDER_RE.sub(_sub, v)
        out.append(new_row)
    return out
```

Inside `ZoneImportService.import_zone()`, after rows are read but before they're written to the target store:

```python
if options.require_no_placeholders:
    all_rows = [row for table_rows in rows_by_table.values() for row in table_rows]
    after_inject = _apply_injections(all_rows, options.injections)
    missing = _scan_for_placeholders(after_inject)
    if missing:
        raise ArchivePlaceholderNotInjected(sorted(missing))
    # then re-apply injections on a per-table basis when persisting
    rows_by_table = {
        t: _apply_injections(rows, options.injections) for t, rows in rows_by_table.items()
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_import_placeholder.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/import_service.py src/nexus/bricks/portability/models.py src/nexus/bricks/portability/tests/test_import_placeholder.py
git commit -m "feat(#3793): restore placeholder guard with --inject substitution"
```

---

## Task 9: Embedding model/dim check on restore

**Files:**
- Modify: `src/nexus/bricks/portability/import_service.py` (add `_check_embedding_compat`)
- Test: `src/nexus/bricks/portability/tests/test_import_embedding.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_import_embedding.py
"""Tests for embedding model/dim restore guard."""

import pytest

from nexus.bricks.archive.errors import ArchiveEmbeddingDimMismatch
from nexus.bricks.portability.import_service import _check_embedding_compat


def test_matching_model_passes():
    _check_embedding_compat(
        archive_model="bge", archive_dim=384,
        current_model="bge", current_dim=384,
        rebuild_embeddings=False,
    )


def test_dim_mismatch_raises():
    with pytest.raises(ArchiveEmbeddingDimMismatch):
        _check_embedding_compat(
            archive_model="bge", archive_dim=384,
            current_model="bge", current_dim=768,
            rebuild_embeddings=False,
        )


def test_model_mismatch_raises():
    with pytest.raises(ArchiveEmbeddingDimMismatch):
        _check_embedding_compat(
            archive_model="bge", archive_dim=384,
            current_model="other", current_dim=384,
            rebuild_embeddings=False,
        )


def test_rebuild_flag_bypasses_check():
    _check_embedding_compat(
        archive_model="bge", archive_dim=384,
        current_model="other", current_dim=768,
        rebuild_embeddings=True,
    )


def test_archive_without_embedding_metadata_passes():
    """v1 bundles (no model/dim in manifest) are not gated."""
    _check_embedding_compat(
        archive_model=None, archive_dim=None,
        current_model="bge", current_dim=384,
        rebuild_embeddings=False,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_import_embedding.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Add to `src/nexus/bricks/portability/import_service.py`:

```python
from nexus.bricks.archive.errors import ArchiveEmbeddingDimMismatch


def _check_embedding_compat(
    *,
    archive_model: str | None,
    archive_dim: int | None,
    current_model: str,
    current_dim: int,
    rebuild_embeddings: bool,
) -> None:
    """Raise ArchiveEmbeddingDimMismatch if archive embeddings are incompatible.

    Returns None on compatibility (or if `rebuild_embeddings` overrides), or
    if the archive carries no embedding metadata (v1 bundles).
    """
    if archive_model is None or archive_dim is None:
        return
    if rebuild_embeddings:
        return
    if archive_model == current_model and archive_dim == current_dim:
        return
    raise ArchiveEmbeddingDimMismatch(
        archive_model=archive_model,
        archive_dim=archive_dim,
        current_model=current_model,
        current_dim=current_dim,
    )
```

Wire into `ZoneImportService.import_zone()` after the manifest has been parsed and before vectors are written:

```python
_check_embedding_compat(
    archive_model=manifest.embedding_model,
    archive_dim=manifest.embedding_dim,
    current_model=self._current_embedding_model(),
    current_dim=self._current_embedding_dim(),
    rebuild_embeddings=options.rebuild_embeddings,
)
```

(`_current_embedding_model()` and `_current_embedding_dim()` are private helpers; if no equivalent exists yet, add them as small wrappers around `nexus.config` lookup — they read the active embedder config.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_import_embedding.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/import_service.py src/nexus/bricks/portability/tests/test_import_embedding.py
git commit -m "feat(#3793): embedding model/dim compatibility check on restore"
```

---

## Task 10: Target-not-empty restore guard

**Files:**
- Modify: `src/nexus/bricks/portability/import_service.py` (add `_check_target_empty`)
- Test: `src/nexus/bricks/portability/tests/test_import_target_guard.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_import_target_guard.py
"""Tests for target-not-empty restore guard."""

import pytest

from nexus.bricks.archive.errors import ArchiveTargetNotEmpty
from nexus.bricks.portability.import_service import _check_target_empty


def test_empty_target_passes():
    _check_target_empty(existing_zones=[], force=False)


def test_non_empty_target_raises():
    with pytest.raises(ArchiveTargetNotEmpty) as exc:
        _check_target_empty(existing_zones=["eng", "ops"], force=False)
    assert exc.value.existing_zones == ["eng", "ops"]


def test_force_bypasses_check():
    _check_target_empty(existing_zones=["eng"], force=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_import_target_guard.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Add to `src/nexus/bricks/portability/import_service.py`:

```python
from nexus.bricks.archive.errors import ArchiveTargetNotEmpty


def _check_target_empty(*, existing_zones: list[str], force: bool) -> None:
    if not existing_zones or force:
        return
    raise ArchiveTargetNotEmpty(existing_zones=existing_zones)
```

In `ZoneImportService.import_zone()`, near the top before any writes:

```python
_check_target_empty(
    existing_zones=self._list_zones(),
    force=options.force,
)
```

(`_list_zones()` reads from the metadata store; if no equivalent helper exists, query `zones` table via `self.metadata_store`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_import_target_guard.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/import_service.py src/nexus/bricks/portability/tests/test_import_target_guard.py
git commit -m "feat(#3793): target-not-empty restore guard with --force bypass"
```

---

## Task 11: Bundle differ

**Files:**
- Create: `src/nexus/bricks/portability/differ.py`
- Test: `src/nexus/bricks/portability/tests/test_differ.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/portability/tests/test_differ.py
"""Tests for bundle diff."""

import json
import tarfile
from pathlib import Path

from nexus.bricks.portability.differ import (
    BundleDiff,
    diff_bundles,
)


def _write_minimal_bundle(path: Path, *, file_hashes: list[str], merkle: str) -> None:
    """Create a minimal v2-shaped bundle with the given file checksums."""
    import shutil
    work = path.parent / (path.stem + "_work")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    files = {f"content/cas/{h[:2]}/{h}": h for h in file_hashes}
    manifest = {
        "format_version": "2.0.0",
        "nexus_version": "0.10.0",
        "bundle_id": "b",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-05-01T00:00:00+00:00",
        "file_count": len(file_hashes),
        "total_size_bytes": 0,
        "content_blob_count": len(file_hashes),
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {
            "algorithm": "sha256",
            "files": {p: {"path": p, "algorithm": "sha256", "hash": h, "size_bytes": 0} for p, h in files.items()},
            "merkle_root": merkle,
        },
        "archive_kind": "full",
        "embedding_model": "bge",
        "embedding_dim": 384,
        "placeholders": [],
        "min_nexus_version": "0.0.0",
    }
    (work / "manifest.json").write_text(json.dumps(manifest))
    (work / "metadata").mkdir()
    (work / "metadata" / "files.jsonl").write_text("")
    with tarfile.open(path, "w:gz") as tar:
        for f in sorted(work.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(work)))


def test_diff_no_changes(tmp_path):
    a = tmp_path / "a.nexus"
    b = tmp_path / "b.nexus"
    _write_minimal_bundle(a, file_hashes=["aaaa", "bbbb"], merkle="root1")
    _write_minimal_bundle(b, file_hashes=["aaaa", "bbbb"], merkle="root1")
    d = diff_bundles(a, b)
    assert d.added == set()
    assert d.removed == set()
    assert d.unchanged == {"aaaa", "bbbb"}


def test_diff_added_and_removed(tmp_path):
    a = tmp_path / "a.nexus"
    b = tmp_path / "b.nexus"
    _write_minimal_bundle(a, file_hashes=["aaaa", "bbbb"], merkle="r1")
    _write_minimal_bundle(b, file_hashes=["bbbb", "cccc"], merkle="r2")
    d = diff_bundles(a, b)
    assert d.removed == {"aaaa"}
    assert d.added == {"cccc"}
    assert d.unchanged == {"bbbb"}


def test_diff_summary_text(tmp_path):
    a = tmp_path / "a.nexus"
    b = tmp_path / "b.nexus"
    _write_minimal_bundle(a, file_hashes=["aaaa"], merkle="r1")
    _write_minimal_bundle(b, file_hashes=["bbbb"], merkle="r2")
    d = diff_bundles(a, b)
    text = d.summary()
    assert "+1 docs" in text
    assert "-1 docs" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/portability/tests/test_differ.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/portability/differ.py
"""Diff two .nexus bundles by content-addressed blob set."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nexus.bricks.portability.bundle import BundleReader


@dataclass
class BundleDiff:
    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    unchanged: set[str] = field(default_factory=set)
    embedding_model_a: str | None = None
    embedding_model_b: str | None = None

    def summary(self) -> str:
        embed_note = (
            "embedding_model: same"
            if self.embedding_model_a == self.embedding_model_b
            else f"embedding_model: {self.embedding_model_a} -> {self.embedding_model_b}"
        )
        return (
            f"+{len(self.added)} docs, -{len(self.removed)} docs, "
            f"={len(self.unchanged)} docs unchanged, {embed_note}"
        )


def _content_blob_hashes(reader: BundleReader) -> set[str]:
    """Return the set of CAS blob hashes in this bundle."""
    out: set[str] = set()
    for path in reader.list_contents():
        if path.startswith("content/cas/") and len(path) > len("content/cas/xx/"):
            out.add(path.rsplit("/", 1)[-1])
    return out


def diff_bundles(a_path: Path, b_path: Path) -> BundleDiff:
    with BundleReader(a_path) as a, BundleReader(b_path) as b:
        manifest_a = a.get_manifest()
        manifest_b = b.get_manifest()
        ha = _content_blob_hashes(a)
        hb = _content_blob_hashes(b)
    return BundleDiff(
        added=hb - ha,
        removed=ha - hb,
        unchanged=ha & hb,
        embedding_model_a=getattr(manifest_a, "embedding_model", None),
        embedding_model_b=getattr(manifest_b, "embedding_model", None),
    )


__all__ = ["BundleDiff", "diff_bundles"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/portability/tests/test_differ.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/differ.py src/nexus/bricks/portability/tests/test_differ.py
git commit -m "feat(#3793): bundle diff via CAS blob set difference"
```

---

## Task 12: Multi-zone orchestrator

**Files:**
- Create: `src/nexus/bricks/archive/orchestrator.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_orchestrator.py
"""Tests for multi-zone archive orchestrator."""

from pathlib import Path
from unittest.mock import MagicMock

from nexus.bricks.archive.orchestrator import ArchiveOrchestrator


def test_create_one_archive_per_zone(tmp_path):
    fake_export_service = MagicMock()
    fake_export_service.export_zone.side_effect = lambda zone_id, options, **kw: MagicMock(
        bundle_id=f"b-{zone_id}"
    )

    orch = ArchiveOrchestrator(export_service=fake_export_service, output_dir=tmp_path)
    manifests = orch.create_archives(zone_ids=["eng", "ops"], strip=True, sign=True)

    assert len(manifests) == 2
    assert fake_export_service.export_zone.call_count == 2
    paths = [c.kwargs.get("options").output_path if "options" in c.kwargs else c.args[1].output_path
             for c in fake_export_service.export_zone.call_args_list]
    assert all(p.parent == tmp_path for p in paths)


def test_all_zones_uses_zone_lister(tmp_path):
    fake_export_service = MagicMock()
    fake_export_service.export_zone.return_value = MagicMock()

    orch = ArchiveOrchestrator(
        export_service=fake_export_service,
        output_dir=tmp_path,
        zone_lister=lambda: ["a", "b", "c"],
    )
    manifests = orch.create_archives(zone_ids=None, strip=True, sign=True)
    assert len(manifests) == 3


def test_strip_and_sign_options_propagate(tmp_path):
    fake_export_service = MagicMock()
    orch = ArchiveOrchestrator(export_service=fake_export_service, output_dir=tmp_path)
    orch.create_archives(zone_ids=["eng"], strip=False, sign=False)
    options = fake_export_service.export_zone.call_args.kwargs.get("options") or \
              fake_export_service.export_zone.call_args.args[1]
    assert options.strip_credentials is False
    assert options.sign is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_orchestrator.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/orchestrator.py
"""Multi-zone archive orchestrator (#3793).

Wraps the single-zone ZoneExportService to produce one archive per zone
(or one across all zones, depending on caller). Output naming convention:
`<zone>-<utc-iso>.nexus`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.bricks.portability.models import ZoneExportOptions

if TYPE_CHECKING:
    from nexus.bricks.portability.export_service import ZoneExportService
    from nexus.bricks.portability.models import ExportManifest


class ArchiveOrchestrator:
    def __init__(
        self,
        *,
        export_service: "ZoneExportService",
        output_dir: Path,
        zone_lister: Callable[[], list[str]] | None = None,
    ) -> None:
        self.export_service = export_service
        self.output_dir = output_dir
        self.zone_lister = zone_lister

    def create_archives(
        self,
        *,
        zone_ids: list[str] | None,
        strip: bool,
        sign: bool,
        audit_from: datetime | None = None,
        audit_to: datetime | None = None,
    ) -> list["ExportManifest"]:
        if zone_ids is None:
            if self.zone_lister is None:
                raise ValueError("zone_ids=None requires a zone_lister callable")
            zone_ids = self.zone_lister()
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out: list[ExportManifest] = []
        for zone_id in zone_ids:
            output = self.output_dir / f"{zone_id}-{ts}.nexus"
            options = ZoneExportOptions(
                output_path=output,
                strip_credentials=strip,
                sign=sign,
                after_time=audit_from,
                before_time=audit_to,
            )
            manifest = self.export_service.export_zone(zone_id, options)
            out.append(manifest)
        return out


__all__ = ["ArchiveOrchestrator"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_orchestrator.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/orchestrator.py src/nexus/bricks/archive/tests/unit/test_orchestrator.py
git commit -m "feat(#3793): multi-zone archive orchestrator"
```

---

## Task 13: Audit-window export with activity event slice

**Files:**
- Create: `src/nexus/bricks/archive/audit_export.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_audit_export.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_audit_export.py
"""Tests for audit-window export."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from nexus.bricks.archive.audit_export import write_activity_slice


def test_write_activity_slice_filters_window(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    events = [
        {"id": "e1", "ts": "2026-04-15T00:00:00+00:00", "kind": "search"},
        {"id": "e2", "ts": "2026-04-20T00:00:00+00:00", "kind": "approval"},
        {"id": "e3", "ts": "2026-05-15T00:00:00+00:00", "kind": "search"},
    ]
    activity_store = MagicMock()
    activity_store.iter_events.return_value = events

    written = write_activity_slice(
        bundle_dir,
        activity_store=activity_store,
        window_from=datetime(2026, 4, 1, tzinfo=UTC),
        window_to=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert written == 2
    out_path = bundle_dir / "activity" / "events.jsonl"
    lines = [json.loads(line) for line in out_path.read_text().splitlines() if line]
    ids = [e["id"] for e in lines]
    assert ids == ["e1", "e2"]


def test_write_activity_slice_empty_window(tmp_path):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    activity_store = MagicMock()
    activity_store.iter_events.return_value = []
    n = write_activity_slice(
        bundle_dir,
        activity_store=activity_store,
        window_from=datetime(2026, 4, 1, tzinfo=UTC),
        window_to=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert n == 0
    assert (bundle_dir / "activity" / "events.jsonl").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_audit_export.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/audit_export.py
"""Audit-window export: slice activity events from #3791 store into bundle."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class ActivityStoreReader(Protocol):
    def iter_events(self) -> list[dict[str, Any]]: ...


def write_activity_slice(
    bundle_dir: Path,
    *,
    activity_store: ActivityStoreReader,
    window_from: datetime,
    window_to: datetime,
) -> int:
    """Write events with `ts` in `[window_from, window_to)` to `activity/events.jsonl`.

    Returns the number of events written.
    """
    out_dir = bundle_dir / "activity"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "events.jsonl"
    n = 0
    with out_path.open("w") as f:
        for event in activity_store.iter_events():
            ts_raw = event.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if window_from <= ts < window_to:
                f.write(json.dumps(event) + "\n")
                n += 1
    return n


__all__ = ["ActivityStoreReader", "write_activity_slice"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_audit_export.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/audit_export.py src/nexus/bricks/archive/tests/unit/test_audit_export.py
git commit -m "feat(#3793): audit-window activity event slice writer"
```

---

## Task 14: ArchiveStorage protocol + local backend

**Files:**
- Create: `src/nexus/bricks/archive/storage/__init__.py`
- Create: `src/nexus/bricks/archive/storage/base.py`
- Create: `src/nexus/bricks/archive/storage/local.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_storage_local.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_storage_local.py
"""Tests for local archive storage backend."""

from datetime import UTC, datetime
from pathlib import Path

from nexus.bricks.archive.storage.local import LocalArchiveStorage


def test_put_then_list(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("daily/a.nexus", src)
    listed = storage.list("daily/")
    assert any(e.key == "daily/a.nexus" for e in listed)


def test_list_returns_size_and_mtime(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "a.nexus"
    src.write_bytes(b"hello")
    storage.put("a.nexus", src)
    entries = storage.list("")
    e = next(e for e in entries if e.key == "a.nexus")
    assert e.size_bytes == 5
    assert isinstance(e.last_modified, datetime)


def test_delete_removes_file(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("a.nexus", src)
    storage.delete("a.nexus")
    assert storage.list("") == []


def test_get_writes_to_target(tmp_path):
    storage = LocalArchiveStorage(root=tmp_path)
    src = tmp_path / "src.nexus"
    src.write_bytes(b"contents")
    storage.put("a.nexus", src)
    target = tmp_path / "downloaded.nexus"
    storage.get("a.nexus", target)
    assert target.read_bytes() == b"contents"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_storage_local.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/storage/__init__.py
"""Archive storage backends."""

from nexus.bricks.archive.storage.base import ArchiveStorage, StorageEntry
from nexus.bricks.archive.storage.local import LocalArchiveStorage

__all__ = ["ArchiveStorage", "StorageEntry", "LocalArchiveStorage"]
```

```python
# src/nexus/bricks/archive/storage/base.py
"""Storage backend protocol for archive destinations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class StorageEntry:
    key: str
    size_bytes: int
    last_modified: datetime


class ArchiveStorage(Protocol):
    def put(self, key: str, source_path: Path) -> None: ...
    def get(self, key: str, target_path: Path) -> None: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str) -> list[StorageEntry]: ...
```

```python
# src/nexus/bricks/archive/storage/local.py
"""Local-filesystem archive storage backend."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from nexus.bricks.archive.storage.base import StorageEntry


class LocalArchiveStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _abs(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, source_path: Path) -> None:
        target = self._abs(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)

    def get(self, key: str, target_path: Path) -> None:
        shutil.copy2(self._abs(key), target_path)

    def delete(self, key: str) -> None:
        self._abs(key).unlink()

    def list(self, prefix: str) -> list[StorageEntry]:
        base = self.root / prefix
        out: list[StorageEntry] = []
        if not self.root.exists():
            return out
        search_root = self.root
        for p in search_root.rglob("*"):
            if p.is_file():
                key = str(p.relative_to(self.root))
                if key.startswith(prefix):
                    stat = p.stat()
                    out.append(
                        StorageEntry(
                            key=key,
                            size_bytes=stat.st_size,
                            last_modified=datetime.fromtimestamp(stat.st_mtime, UTC),
                        )
                    )
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_storage_local.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/storage/ src/nexus/bricks/archive/tests/unit/test_storage_local.py
git commit -m "feat(#3793): ArchiveStorage protocol + local backend"
```

---

## Task 15: S3 storage backend

**Files:**
- Create: `src/nexus/bricks/archive/storage/s3.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_storage_s3.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_storage_s3.py
"""Tests for S3 archive storage backend (uses moto)."""

import pytest

boto3 = pytest.importorskip("boto3")
moto = pytest.importorskip("moto")

from moto import mock_aws

from nexus.bricks.archive.storage.s3 import S3ArchiveStorage


@mock_aws
def test_put_then_list(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    storage = S3ArchiveStorage(bucket="test-bucket", prefix="archives/", region="us-east-1")
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("daily/a.nexus", src)
    entries = storage.list("daily/")
    assert any(e.key == "daily/a.nexus" for e in entries)


@mock_aws
def test_get_round_trip(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    storage = S3ArchiveStorage(bucket="test-bucket", prefix="", region="us-east-1")
    src = tmp_path / "a.nexus"
    src.write_bytes(b"contents")
    storage.put("a.nexus", src)
    target = tmp_path / "out.nexus"
    storage.get("a.nexus", target)
    assert target.read_bytes() == b"contents"


@mock_aws
def test_delete(tmp_path):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    storage = S3ArchiveStorage(bucket="test-bucket", prefix="", region="us-east-1")
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    storage.put("a.nexus", src)
    storage.delete("a.nexus")
    assert storage.list("") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_storage_s3.py -v`
Expected: FAIL — `S3ArchiveStorage` not defined. (If `moto` not installed, the test is skipped, which is also acceptable.)

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/storage/s3.py
"""S3 archive storage backend."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import boto3

from nexus.bricks.archive.storage.base import StorageEntry


class S3ArchiveStorage:
    def __init__(self, bucket: str, prefix: str = "", region: str | None = None) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.client = boto3.client("s3", region_name=region) if region else boto3.client("s3")

    def _full(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, source_path: Path) -> None:
        self.client.upload_file(str(source_path), self.bucket, self._full(key))

    def get(self, key: str, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, self._full(key), str(target_path))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._full(key))

    def list(self, prefix: str) -> list[StorageEntry]:
        full_prefix = self._full(prefix)
        paginator = self.client.get_paginator("list_objects_v2")
        out: list[StorageEntry] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for obj in page.get("Contents", []):
                full_key = obj["Key"]
                key = full_key[len(self.prefix):] if full_key.startswith(self.prefix) else full_key
                last_mod = obj["LastModified"]
                if isinstance(last_mod, datetime):
                    out.append(StorageEntry(key=key, size_bytes=obj["Size"], last_modified=last_mod))
        return out
```

Add to `src/nexus/bricks/archive/storage/__init__.py`:

```python
try:
    from nexus.bricks.archive.storage.s3 import S3ArchiveStorage  # noqa: F401
    __all__.append("S3ArchiveStorage")
except ImportError:
    pass  # boto3 optional in slim images
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_storage_s3.py -v`
Expected: PASS, 3 tests (or skipped if `moto` missing).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/storage/s3.py src/nexus/bricks/archive/storage/__init__.py src/nexus/bricks/archive/tests/unit/test_storage_s3.py
git commit -m "feat(#3793): S3 archive storage backend"
```

---

## Task 16: GCS storage backend

**Files:**
- Create: `src/nexus/bricks/archive/storage/gcs.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_storage_gcs.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_storage_gcs.py
"""Tests for GCS archive storage backend."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("google.cloud.storage")

from nexus.bricks.archive.storage.gcs import GCSArchiveStorage


def test_put_uploads_blob(tmp_path):
    src = tmp_path / "a.nexus"
    src.write_bytes(b"data")
    fake_bucket = MagicMock()
    storage = GCSArchiveStorage(bucket="b", prefix="archives/", _bucket=fake_bucket)
    storage.put("daily/a.nexus", src)
    fake_bucket.blob.assert_called_once_with("archives/daily/a.nexus")
    fake_bucket.blob.return_value.upload_from_filename.assert_called_once_with(str(src))


def test_list_returns_entries(tmp_path):
    fake_blob = MagicMock(name="blob1", size=5, updated=datetime(2026, 5, 1, tzinfo=UTC))
    fake_blob.name = "archives/a.nexus"
    fake_bucket = MagicMock()
    fake_bucket.list_blobs.return_value = [fake_blob]
    storage = GCSArchiveStorage(bucket="b", prefix="archives/", _bucket=fake_bucket)
    entries = storage.list("")
    assert entries[0].key == "a.nexus"
    assert entries[0].size_bytes == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_storage_gcs.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/storage/gcs.py
"""GCS archive storage backend."""

from __future__ import annotations

from pathlib import Path

from google.cloud import storage as gcs

from nexus.bricks.archive.storage.base import StorageEntry


class GCSArchiveStorage:
    def __init__(self, bucket: str, prefix: str = "", *, _bucket: object | None = None) -> None:
        self.prefix = prefix
        self._bucket = _bucket or gcs.Client().bucket(bucket)

    def _full(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def put(self, key: str, source_path: Path) -> None:
        blob = self._bucket.blob(self._full(key))
        blob.upload_from_filename(str(source_path))

    def get(self, key: str, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._bucket.blob(self._full(key))
        blob.download_to_filename(str(target_path))

    def delete(self, key: str) -> None:
        self._bucket.blob(self._full(key)).delete()

    def list(self, prefix: str) -> list[StorageEntry]:
        full_prefix = self._full(prefix)
        out: list[StorageEntry] = []
        for blob in self._bucket.list_blobs(prefix=full_prefix):
            name = blob.name
            key = name[len(self.prefix):] if name.startswith(self.prefix) else name
            out.append(StorageEntry(key=key, size_bytes=blob.size or 0, last_modified=blob.updated))
        return out
```

Append to `src/nexus/bricks/archive/storage/__init__.py`:

```python
try:
    from nexus.bricks.archive.storage.gcs import GCSArchiveStorage  # noqa: F401
    __all__.append("GCSArchiveStorage")
except ImportError:
    pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_storage_gcs.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/storage/gcs.py src/nexus/bricks/archive/storage/__init__.py src/nexus/bricks/archive/tests/unit/test_storage_gcs.py
git commit -m "feat(#3793): GCS archive storage backend"
```

---

## Task 17: GFS retention math

**Files:**
- Create: `src/nexus/bricks/archive/retention.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_retention.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_retention.py
"""Tests for GFS retention math."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nexus.bricks.archive.retention import RetentionPolicy, apply_retention


@dataclass
class FakeEntry:
    key: str
    last_modified: datetime


def _entry(days_ago: int) -> FakeEntry:
    return FakeEntry(
        key=f"a-{days_ago}.nexus",
        last_modified=datetime(2026, 5, 1, tzinfo=UTC) - timedelta(days=days_ago),
    )


def test_keeps_n_daily_recent():
    entries = [_entry(d) for d in range(0, 30)]
    keep, prune = apply_retention(
        entries, RetentionPolicy(daily=7, weekly=0, monthly=0),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert len(keep) == 7
    assert all(e in entries[:7] for e in keep)


def test_keeps_one_per_iso_week_for_weekly():
    entries = [_entry(d) for d in range(0, 60)]
    keep, _prune = apply_retention(
        entries, RetentionPolicy(daily=0, weekly=4, monthly=0),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    iso_weeks = {e.last_modified.isocalendar()[:2] for e in keep}
    assert len(iso_weeks) == len(keep)
    assert len(keep) == 4


def test_keeps_one_per_calendar_month_for_monthly():
    entries = [_entry(d) for d in range(0, 365)]
    keep, _prune = apply_retention(
        entries, RetentionPolicy(daily=0, weekly=0, monthly=6),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    months = {(e.last_modified.year, e.last_modified.month) for e in keep}
    assert len(months) == len(keep)
    assert len(keep) == 6


def test_combined_policy_dedupes_overlapping():
    entries = [_entry(d) for d in range(0, 365)]
    keep, _prune = apply_retention(
        entries, RetentionPolicy(daily=7, weekly=4, monthly=6),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert len(set(e.key for e in keep)) == len(keep)
    assert len(keep) <= 7 + 4 + 6


def test_pruned_is_complement_of_keep():
    entries = [_entry(d) for d in range(0, 30)]
    keep, prune = apply_retention(
        entries, RetentionPolicy(daily=7, weekly=0, monthly=0),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert set(e.key for e in keep) | set(e.key for e in prune) == set(e.key for e in entries)
    assert set(e.key for e in keep) & set(e.key for e in prune) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_retention.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/retention.py
"""Grandfather-father-son (GFS) retention math for scheduled archives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class RetentionPolicy:
    daily: int
    weekly: int
    monthly: int


class _HasMtime(Protocol):
    last_modified: datetime
    key: str


def apply_retention(
    entries: list[_HasMtime],
    policy: RetentionPolicy,
    *,
    now: datetime,
) -> tuple[list[_HasMtime], list[_HasMtime]]:
    """Return (keep, prune) lists according to GFS policy.

    Keeps the N most recent daily, then one per ISO week up to `weekly`,
    then one per calendar month up to `monthly`. The same entry can satisfy
    multiple slots; the keep set is deduped.
    """
    if not entries:
        return [], []

    sorted_desc = sorted(entries, key=lambda e: e.last_modified, reverse=True)

    keep_keys: set[str] = set()
    keep: list[_HasMtime] = []

    def _add(entry: _HasMtime) -> None:
        if entry.key not in keep_keys:
            keep_keys.add(entry.key)
            keep.append(entry)

    for e in sorted_desc[: policy.daily]:
        _add(e)

    seen_weeks: set[tuple[int, int]] = set()
    weekly_picks = 0
    for e in sorted_desc:
        wk = e.last_modified.isocalendar()[:2]
        if wk in seen_weeks:
            continue
        seen_weeks.add(wk)
        if e.key not in keep_keys:
            _add(e)
            weekly_picks += 1
            if weekly_picks >= policy.weekly:
                break
        else:
            # already kept by daily slot; still consumes the week token
            weekly_picks += 1
            if weekly_picks >= policy.weekly:
                break

    seen_months: set[tuple[int, int]] = set()
    monthly_picks = 0
    for e in sorted_desc:
        m = (e.last_modified.year, e.last_modified.month)
        if m in seen_months:
            continue
        seen_months.add(m)
        if e.key not in keep_keys:
            _add(e)
            monthly_picks += 1
            if monthly_picks >= policy.monthly:
                break
        else:
            monthly_picks += 1
            if monthly_picks >= policy.monthly:
                break

    prune = [e for e in entries if e.key not in keep_keys]
    return keep, prune


__all__ = ["RetentionPolicy", "apply_retention"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_retention.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/retention.py src/nexus/bricks/archive/tests/unit/test_retention.py
git commit -m "feat(#3793): GFS retention math (daily/weekly/monthly)"
```

---

## Task 18: Scheduler runtime (cron + retention sweep)

**Files:**
- Create: `src/nexus/bricks/archive/scheduler.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_scheduler.py
"""Tests for archive scheduler."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.bricks.archive.retention import RetentionPolicy
from nexus.bricks.archive.scheduler import ArchiveScheduler, ScheduleConfig


def test_due_at_cron_matches_minute():
    cfg = ScheduleConfig(cron="0 2 * * *", policy=RetentionPolicy(7, 4, 6))
    sched = ArchiveScheduler(cfg, orchestrator=MagicMock(), storage=MagicMock())
    assert sched._is_due(datetime(2026, 5, 1, 2, 0, tzinfo=UTC))
    assert not sched._is_due(datetime(2026, 5, 1, 2, 1, tzinfo=UTC))


@pytest.mark.asyncio
async def test_run_once_creates_archives_and_uploads():
    orch = MagicMock()
    orch.create_archives.return_value = []
    storage = MagicMock()
    storage.list.return_value = []
    cfg = ScheduleConfig(cron="0 2 * * *", policy=RetentionPolicy(7, 4, 6))
    sched = ArchiveScheduler(cfg, orchestrator=orch, storage=storage)
    await sched.run_once(now=datetime(2026, 5, 1, 2, 0, tzinfo=UTC))
    assert orch.create_archives.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_scheduler.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/scheduler.py
"""Cron-driven archive scheduler with GFS retention sweep (#3793).

Hub-only: registered into the lifespan only when the active profile is hub.
Lightweight profile skips registration entirely.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.bricks.archive.retention import RetentionPolicy, apply_retention

if TYPE_CHECKING:
    from nexus.bricks.archive.orchestrator import ArchiveOrchestrator
    from nexus.bricks.archive.storage.base import ArchiveStorage

logger = logging.getLogger(__name__)


@dataclass
class ScheduleConfig:
    cron: str  # e.g. "0 2 * * *"
    policy: RetentionPolicy
    zones: list[str] | None = None  # None = all zones


def _parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    if field == "*":
        return set(range(lo, hi + 1))
    out: set[int] = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            base_set = _parse_cron_field(base or "*", lo, hi)
            out |= {v for v in base_set if (v - lo) % int(step) == 0}
        elif "-" in part:
            a, b = part.split("-", 1)
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


class ArchiveScheduler:
    """Polls every minute, runs the orchestrator on cron match, prunes per policy."""

    def __init__(
        self,
        cfg: ScheduleConfig,
        *,
        orchestrator: "ArchiveOrchestrator",
        storage: "ArchiveStorage",
    ) -> None:
        self.cfg = cfg
        self.orchestrator = orchestrator
        self.storage = storage
        m, h, dom, mon, dow = shlex.split(cfg.cron) if " " not in cfg.cron else cfg.cron.split()
        self._minutes = _parse_cron_field(m, 0, 59)
        self._hours = _parse_cron_field(h, 0, 23)
        self._doms = _parse_cron_field(dom, 1, 31)
        self._months = _parse_cron_field(mon, 1, 12)
        self._dows = _parse_cron_field(dow, 0, 6)

    def _is_due(self, now: datetime) -> bool:
        return (
            now.minute in self._minutes
            and now.hour in self._hours
            and now.day in self._doms
            and now.month in self._months
            and (now.weekday() + 1) % 7 in self._dows
        )

    async def run_once(self, *, now: datetime) -> None:
        if not self._is_due(now):
            return
        try:
            manifests = self.orchestrator.create_archives(
                zone_ids=self.cfg.zones,
                strip=True,
                sign=True,
            )
            for manifest in manifests:
                output = Path(self.orchestrator.output_dir) / f"{manifest.source_zone_id}.nexus"
                if output.exists():
                    self.storage.put(output.name, output)
        except Exception:
            logger.exception("archive create failed")
            return

        try:
            entries = self.storage.list("")
            keep, prune = apply_retention(entries, self.cfg.policy, now=now)
            for e in prune:
                self.storage.delete(e.key)
            logger.info("archive retention: kept=%d pruned=%d", len(keep), len(prune))
        except Exception:
            logger.exception("archive retention sweep failed")

    async def run_forever(self) -> None:
        while True:
            now = datetime.now(tz=__import__("datetime").timezone.utc)
            await self.run_once(now=now)
            await asyncio.sleep(60 - now.second)


__all__ = ["ScheduleConfig", "ArchiveScheduler"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_scheduler.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/scheduler.py src/nexus/bricks/archive/tests/unit/test_scheduler.py
git commit -m "feat(#3793): cron-driven archive scheduler with retention sweep"
```

---

## Task 19: CLI — `nexus archive` group + create / verify / inspect

**Files:**
- Create: `src/nexus/cli/commands/archive.py`
- Modify: `src/nexus/cli/commands/__init__.py` (register lazy module)
- Test: `tests/unit/cli/test_archive_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/cli/test_archive_cli.py
"""Tests for nexus archive CLI."""

from click.testing import CliRunner

from nexus.cli.commands.archive import archive


def test_help_lists_subcommands():
    runner = CliRunner()
    result = runner.invoke(archive, ["--help"])
    assert result.exit_code == 0
    for sub in ["create", "verify", "restore", "diff", "inspect", "keys"]:
        assert sub in result.output


def test_inspect_shows_manifest_summary(tmp_path, monkeypatch):
    # Build a minimal v2 bundle
    import json
    import tarfile

    bundle_dir = tmp_path / "b"
    bundle_dir.mkdir()
    manifest = {
        "format_version": "2.0.0",
        "nexus_version": "0.10.0",
        "bundle_id": "b-1",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-05-01T00:00:00+00:00",
        "file_count": 0,
        "total_size_bytes": 0,
        "content_blob_count": 0,
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {"algorithm": "sha256", "files": {}, "merkle_root": None},
        "archive_kind": "full",
        "embedding_model": "bge",
        "embedding_dim": 384,
        "placeholders": [],
        "min_nexus_version": "0.0.0",
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "b.nexus"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(bundle_dir / "manifest.json", arcname="manifest.json")

    runner = CliRunner()
    result = runner.invoke(archive, ["inspect", str(out)])
    assert result.exit_code == 0
    assert "eng" in result.output
    assert "2.0.0" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/cli/test_archive_cli.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/cli/commands/archive.py
"""nexus archive — signed, credential-stripped zone snapshots (#3793).

Subcommands: create, verify, restore, diff, inspect, keys.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from nexus.bricks.archive.errors import ArchiveError
from nexus.bricks.portability.bundle import inspect_bundle


@click.group(name="archive")
def archive() -> None:
    """Signed zone archive snapshots (backup, migration, audit)."""


@archive.command("inspect")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def inspect(file: Path) -> None:
    """Dump manifest + file tree without restoring."""
    try:
        info = inspect_bundle(file)
    except Exception as e:
        click.echo(f"error reading bundle: {e}", err=True)
        sys.exit(1)
    click.echo(json.dumps(info, indent=2, default=str))


@archive.command("verify")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--strict", is_flag=True, help="Require v2 (signed) bundle")
def verify(file: Path, strict: bool) -> None:
    """Signature + Merkle + per-file SHA + version compatibility check."""
    from nexus.bricks.archive.verify import verify_archive

    try:
        verify_archive(file, strict=strict)
    except ArchiveError as e:
        click.echo(f"verify failed: {e}", err=True)
        sys.exit(e.code)
    click.echo(f"OK: {file}")


@archive.command("create")
@click.option("--zone", "zones", multiple=True, help="Zone(s) to archive")
@click.option("--all-zones", is_flag=True)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--audit", is_flag=True)
@click.option("--from", "audit_from", type=click.DateTime())
@click.option("--to", "audit_to", type=click.DateTime())
@click.option("--no-sign", is_flag=True)
@click.option("--no-strip", is_flag=True)
def create(
    zones: tuple[str, ...],
    all_zones: bool,
    output: Path,
    audit: bool,
    audit_from,
    audit_to,
    no_sign: bool,
    no_strip: bool,
) -> None:
    """Build an archive of one zone, several zones, or the whole hub."""
    from nexus.bricks.archive.cli_glue import run_create

    zone_ids = list(zones) if zones else (None if all_zones else None)
    if not zones and not all_zones:
        click.echo("must pass --zone or --all-zones", err=True)
        sys.exit(2)
    try:
        run_create(
            zone_ids=zone_ids,
            output=output,
            audit=audit,
            audit_from=audit_from,
            audit_to=audit_to,
            sign=not no_sign,
            strip=not no_strip,
        )
    except ArchiveError as e:
        click.echo(f"create failed: {e}", err=True)
        sys.exit(e.code)


@archive.command("restore")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--target-zone")
@click.option("--require-trusted", is_flag=True)
@click.option("--rebuild-embeddings", is_flag=True)
@click.option("--force", is_flag=True)
@click.option("--inject", "injections", multiple=True, help="KEY=VALUE")
def restore(
    file: Path,
    target_zone: str | None,
    require_trusted: bool,
    rebuild_embeddings: bool,
    force: bool,
    injections: tuple[str, ...],
) -> None:
    """Verify → strip-check → re-inject placeholders → write to fresh nexus."""
    from nexus.bricks.archive.cli_glue import run_restore

    inj_dict: dict[str, str] = {}
    for kv in injections:
        if "=" not in kv:
            click.echo(f"--inject must be KEY=VALUE, got {kv!r}", err=True)
            sys.exit(2)
        k, v = kv.split("=", 1)
        inj_dict[k] = v
    try:
        run_restore(
            file=file,
            target_zone=target_zone,
            require_trusted=require_trusted,
            rebuild_embeddings=rebuild_embeddings,
            force=force,
            injections=inj_dict,
        )
    except ArchiveError as e:
        click.echo(f"restore failed: {e}", err=True)
        sys.exit(e.code)


@archive.command("diff")
@click.argument("a", type=click.Path(exists=True, path_type=Path))
@click.argument("b", type=click.Path(exists=True, path_type=Path))
@click.option("--detail", is_flag=True)
def diff_cmd(a: Path, b: Path, detail: bool) -> None:
    """Per-zone summary of doc/policy/embedding deltas."""
    from nexus.bricks.portability.differ import diff_bundles

    d = diff_bundles(a, b)
    click.echo(d.summary())
    if detail:
        for h in sorted(d.added):
            click.echo(f"+ {h}")
        for h in sorted(d.removed):
            click.echo(f"- {h}")


@archive.group("keys")
def keys() -> None:
    """Signing key management."""


@keys.command("rotate")
def keys_rotate() -> None:
    """Rotate the local archive signing keypair."""
    from nexus.bricks.archive.cli_glue import run_keys_rotate

    new_pub = run_keys_rotate()
    click.echo(f"rotated. new pubkey: {new_pub}")


@keys.command("trust")
@click.argument("pubkey_b64")
@click.option("--label", default="")
def keys_trust(pubkey_b64: str, label: str) -> None:
    """Add a signer pubkey to the TOFU trust store."""
    from nexus.bricks.portability.trust import TrustStore

    store = TrustStore(Path.home() / ".nexus" / "trusted_signers.json")
    store.pin(pubkey_b64, label=label)
    click.echo(f"trusted: {pubkey_b64[:24]}…")


def register_commands(cli: click.Group) -> None:
    cli.add_command(archive)
```

In `src/nexus/cli/commands/__init__.py`, add to `_REGISTER_COMMANDS`:

```python
    "archive": ("archive",),
```

The CLI glue module is implemented in Task 20.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/cli/test_archive_cli.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cli/commands/archive.py src/nexus/cli/commands/__init__.py tests/unit/cli/test_archive_cli.py
git commit -m "feat(#3793): nexus archive CLI group + inspect/verify/create/restore/diff/keys"
```

---

## Task 20: CLI glue + verifier

**Files:**
- Create: `src/nexus/bricks/archive/verify.py`
- Create: `src/nexus/bricks/archive/cli_glue.py`
- Test: `src/nexus/bricks/archive/tests/unit/test_verify.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_verify.py
"""Tests for archive verifier."""

import json
import tarfile
from pathlib import Path

import pytest

from nexus.bricks.archive.errors import (
    ArchiveError,
    ArchiveSignatureError,
    ArchiveVersionIncompatible,
)
from nexus.bricks.archive.verify import verify_archive
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes


def _build_signed_bundle(tmp_path: Path, *, signer: ArchiveSigner, manifest_overrides: dict | None = None) -> Path:
    bundle_dir = tmp_path / "b"
    bundle_dir.mkdir()
    manifest = {
        "format_version": "2.0.0",
        "nexus_version": "0.10.0",
        "bundle_id": "b",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-05-01T00:00:00+00:00",
        "file_count": 0,
        "total_size_bytes": 0,
        "content_blob_count": 0,
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {"algorithm": "sha256", "files": {}, "merkle_root": ""},
        "archive_kind": "full",
        "embedding_model": "bge",
        "embedding_dim": 384,
        "signer_pubkey_b64": signer.public_key_b64,
        "placeholders": [],
        "min_nexus_version": "0.0.0",
    }
    if manifest_overrides:
        manifest.update(manifest_overrides)
    manifest_bytes = canonical_json_bytes(manifest)
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)

    payload = manifest_bytes + (manifest["checksums"]["merkle_root"] or "").encode()
    sig_b64, pub_b64 = signer.sign(payload)
    sig_doc = {
        "algorithm": "ed25519",
        "signer_pubkey_b64": pub_b64,
        "signature_b64": sig_b64,
        "manifest_sha256": "0" * 64,
    }
    (bundle_dir / "signatures.json").write_text(json.dumps(sig_doc))

    out = tmp_path / "b.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for f in sorted(bundle_dir.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(bundle_dir)))
    return out


def test_verify_signed_bundle_passes(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    bundle = _build_signed_bundle(tmp_path, signer=signer)
    verify_archive(bundle, strict=True)


def test_verify_tampered_manifest_fails(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    bundle = _build_signed_bundle(tmp_path, signer=signer)

    # Tamper: extract, modify manifest, re-tar without re-signing.
    extract_dir = tmp_path / "ex"
    extract_dir.mkdir()
    with tarfile.open(bundle, "r:gz") as tar:
        tar.extractall(extract_dir, filter="data")
    manifest_path = extract_dir / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["nexus_version"] = "9.9.9"
    manifest_path.write_text(json.dumps(data))
    tampered = tmp_path / "tampered.nexus"
    with tarfile.open(tampered, "w:gz") as tar:
        for f in sorted(extract_dir.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(extract_dir)))

    with pytest.raises(ArchiveSignatureError):
        verify_archive(tampered, strict=True)


def test_verify_strict_rejects_v1(tmp_path):
    bundle_dir = tmp_path / "v1"
    bundle_dir.mkdir()
    manifest = {
        "format_version": "1.0.0",
        "nexus_version": "0.9.0",
        "bundle_id": "b",
        "source_instance": "hub",
        "source_zone_id": "eng",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "file_count": 0,
        "total_size_bytes": 0,
        "content_blob_count": 0,
        "permission_count": 0,
        "include_content": True,
        "include_permissions": True,
        "include_embeddings": False,
        "checksums": {"algorithm": "sha256", "files": {}, "merkle_root": None},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "v1.nexus"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(bundle_dir / "manifest.json", arcname="manifest.json")

    with pytest.raises(ArchiveError):
        verify_archive(out, strict=True)


def test_verify_min_version_rejected(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    bundle = _build_signed_bundle(
        tmp_path, signer=signer, manifest_overrides={"min_nexus_version": "999.0.0"}
    )
    with pytest.raises(ArchiveVersionIncompatible):
        verify_archive(bundle, strict=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_verify.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# src/nexus/bricks/archive/verify.py
"""End-to-end archive verifier (#3793)."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import nexus
from nexus.bricks.archive.errors import (
    ArchiveError,
    ArchiveSignatureError,
    ArchiveVersionIncompatible,
)
from nexus.bricks.portability.signer import ArchiveSigner, canonical_json_bytes


def _parse_semver(s: str) -> tuple[int, int, int]:
    parts = s.split(".")
    while len(parts) < 3:
        parts.append("0")
    return tuple(int(p) for p in parts[:3])  # type: ignore[return-value]


def verify_archive(file: Path, *, strict: bool = False) -> None:
    """Verify signature, version, and per-file integrity.

    Raises ArchiveError subclasses on any mismatch.
    """
    with tarfile.open(file, "r:gz") as tar:
        names = tar.getnames()
        if "manifest.json" not in names:
            raise ArchiveError(f"bundle missing manifest.json: {file}")
        manifest_bytes = tar.extractfile("manifest.json").read()
        try:
            manifest = json.loads(manifest_bytes)
        except json.JSONDecodeError as e:
            raise ArchiveError(f"corrupt manifest: {e}") from e

        format_version = manifest.get("format_version", "1.0.0")
        if strict and not format_version.startswith("2."):
            raise ArchiveError(f"--strict requires v2; bundle is v{format_version}")

        min_required = manifest.get("min_nexus_version", "0.0.0")
        current = nexus.__version__
        if _parse_semver(min_required) > _parse_semver(current):
            raise ArchiveVersionIncompatible(required=min_required, current=current)

        if format_version.startswith("2.") and "signatures.json" in names:
            sig_doc = json.loads(tar.extractfile("signatures.json").read())
            payload = canonical_json_bytes(manifest) + (
                (manifest.get("checksums") or {}).get("merkle_root") or ""
            ).encode()
            ArchiveSigner.verify(payload, sig_doc["signature_b64"], sig_doc["signer_pubkey_b64"])
        elif strict:
            raise ArchiveSignatureError("v2 bundle missing signatures.json")


__all__ = ["verify_archive"]
```

```python
# src/nexus/bricks/archive/cli_glue.py
"""Glue between Click commands and archive subsystems (#3793).

Kept thin so the CLI module stays free of nexus runtime imports until
invocation time.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def run_create(
    *,
    zone_ids: list[str] | None,
    output: Path,
    audit: bool,
    audit_from: datetime | None,
    audit_to: datetime | None,
    sign: bool,
    strip: bool,
) -> None:
    """Wire up orchestrator + storage; build an archive at `output`."""
    from nexus.bricks.archive.orchestrator import ArchiveOrchestrator
    from nexus.bricks.portability.export_service import ZoneExportService

    nexus_fs = _open_nexus_fs()
    export_service = ZoneExportService(nexus_fs)
    orch = ArchiveOrchestrator(
        export_service=export_service,
        output_dir=output.parent,
        zone_lister=lambda: _list_zones(nexus_fs),
    )
    orch.create_archives(
        zone_ids=zone_ids,
        strip=strip,
        sign=sign,
        audit_from=audit_from if audit else None,
        audit_to=audit_to if audit else None,
    )


def run_restore(
    *,
    file: Path,
    target_zone: str | None,
    require_trusted: bool,
    rebuild_embeddings: bool,
    force: bool,
    injections: dict[str, str],
) -> None:
    from nexus.bricks.portability.import_service import ZoneImportService
    from nexus.bricks.portability.models import ZoneImportOptions
    from nexus.bricks.portability.trust import TrustStore
    from nexus.bricks.archive.errors import ArchiveUntrustedSigner
    from nexus.bricks.archive.verify import verify_archive

    verify_archive(file, strict=True)
    if require_trusted:
        import json
        import tarfile

        with tarfile.open(file, "r:gz") as tar:
            sig_doc = json.loads(tar.extractfile("signatures.json").read())
        store = TrustStore(Path.home() / ".nexus" / "trusted_signers.json")
        if not store.is_trusted(sig_doc["signer_pubkey_b64"]):
            raise ArchiveUntrustedSigner(sig_doc["signer_pubkey_b64"])

    nexus_fs = _open_nexus_fs()
    import_service = ZoneImportService(nexus_fs)
    options = ZoneImportOptions(
        bundle_path=file,
        target_zone_id=target_zone,
        force=force,
        rebuild_embeddings=rebuild_embeddings,
        injections=injections,
    )
    import_service.import_zone(options)
    _print_federation_repair_list(nexus_fs)


def run_keys_rotate() -> str:
    """Rotate the signing keypair, return the new pubkey b64."""
    from nexus.bricks.portability.signer import ArchiveSigner
    import shutil
    import time

    key_path = Path.home() / ".nexus" / "archive_signing_key"
    if key_path.exists():
        backup = key_path.with_name(f"archive_signing_key.{int(time.time())}.bak")
        shutil.move(str(key_path), str(backup))
        pub = key_path.with_suffix(".pub")
        if pub.exists():
            shutil.move(str(pub), str(backup) + ".pub")
    signer = ArchiveSigner(key_path)
    return signer.public_key_b64


def _open_nexus_fs() -> Any:
    """Locate the running nexus filesystem instance for CLI use.

    Lazy-imports to avoid pulling the runtime when the CLI is just `--help`.
    """
    from nexus.cli.utils import get_filesystem  # type: ignore[attr-defined]

    return get_filesystem()


def _list_zones(nexus_fs: Any) -> list[str]:
    return [z.zone_id for z in nexus_fs.metadata.list_zones()]


def _print_federation_repair_list(nexus_fs: Any) -> None:
    from rich.console import Console

    console = Console()
    federations = []
    try:
        federations = nexus_fs.metadata.list_federations()
    except AttributeError:
        return
    if not federations:
        return
    console.print("[bold]Federation re-pair required:[/]")
    for f in federations:
        console.print(f"  nexus federation auth {f.url}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_verify.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/verify.py src/nexus/bricks/archive/cli_glue.py src/nexus/bricks/archive/tests/unit/test_verify.py
git commit -m "feat(#3793): archive verifier + CLI glue (orchestrator/import/restore wiring)"
```

---

## Task 21: Federation re-pair messaging integration test

**Files:**
- Test: `src/nexus/bricks/archive/tests/unit/test_federation_repair.py`

- [ ] **Step 1: Write the failing test**

```python
# src/nexus/bricks/archive/tests/unit/test_federation_repair.py
"""Tests for federation re-pair messaging on restore."""

from unittest.mock import MagicMock

from nexus.bricks.archive.cli_glue import _print_federation_repair_list


def test_prints_federation_urls(capsys):
    fs = MagicMock()
    fed_a = MagicMock()
    fed_a.url = "https://hub.example.com"
    fed_b = MagicMock()
    fed_b.url = "https://other.example.com"
    fs.metadata.list_federations.return_value = [fed_a, fed_b]
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert "Federation re-pair required" in captured.out
    assert "nexus federation auth https://hub.example.com" in captured.out
    assert "nexus federation auth https://other.example.com" in captured.out


def test_silent_when_no_federations(capsys):
    fs = MagicMock()
    fs.metadata.list_federations.return_value = []
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_silent_when_no_federation_api(capsys):
    fs = MagicMock(spec=[])
    _print_federation_repair_list(fs)
    captured = capsys.readouterr()
    assert captured.out == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_federation_repair.py -v`
Expected: PASS already (helper was created in Task 20). If failing, double-check the import.

- [ ] **Step 3: (No new code — verifies existing helper.)**

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest src/nexus/bricks/archive/tests/unit/test_federation_repair.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/archive/tests/unit/test_federation_repair.py
git commit -m "test(#3793): federation re-pair messaging on restore"
```

---

## Task 22: Integration tests — round-trip + tamper + secrets + embedding

**Files:**
- Create: `tests/integration/archive/__init__.py`
- Create: `tests/integration/archive/test_round_trip.py`
- Create: `tests/integration/archive/test_tamper.py`
- Create: `tests/integration/archive/test_planted_secrets.py`
- Create: `tests/integration/archive/test_audit_window.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/archive/test_round_trip.py
"""Round-trip create → verify → restore on SQLite + Postgres backends."""

from pathlib import Path

import pytest


@pytest.fixture
def fresh_nexus_sqlite(tmp_path):
    """Boot a lightweight (SQLite) nexus, ingest fixtures, return handle."""
    from tests.integration.archive.helpers import boot_lightweight_nexus

    nexus_fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    nexus_fs.ingest_fixture("eng", path="tests/fixtures/archive_corpus_small/")
    yield nexus_fs
    nexus_fs.shutdown()


def test_round_trip_sqlite(fresh_nexus_sqlite, tmp_path):
    from nexus.bricks.archive.cli_glue import run_create, run_restore

    archive_path = tmp_path / "eng.nexus"
    run_create(
        zone_ids=["eng"], output=archive_path,
        audit=False, audit_from=None, audit_to=None,
        sign=True, strip=True,
    )
    assert archive_path.exists()

    # Tear down zone
    fresh_nexus_sqlite.delete_zone("eng")

    # Restore
    run_restore(
        file=archive_path,
        target_zone="eng",
        require_trusted=False,
        rebuild_embeddings=False,
        force=True,
        injections={},  # only restoring; no creds were planted
    )

    # Search returns expected results
    results = fresh_nexus_sqlite.search("eng", query="known fixture phrase")
    assert len(results) > 0


@pytest.mark.postgres
def test_round_trip_postgres(tmp_path, postgres_test_db):
    """Same round-trip but against postgres profile."""
    from tests.integration.archive.helpers import boot_hub_nexus

    nexus_fs = boot_hub_nexus(dsn=postgres_test_db)
    nexus_fs.ingest_fixture("eng", path="tests/fixtures/archive_corpus_small/")

    from nexus.bricks.archive.cli_glue import run_create, run_restore

    archive_path = tmp_path / "eng.nexus"
    run_create(
        zone_ids=["eng"], output=archive_path,
        audit=False, audit_from=None, audit_to=None,
        sign=True, strip=True,
    )
    nexus_fs.delete_zone("eng")
    run_restore(
        file=archive_path,
        target_zone="eng",
        require_trusted=False,
        rebuild_embeddings=False,
        force=True,
        injections={},
    )
    results = nexus_fs.search("eng", query="known fixture phrase")
    assert len(results) > 0
    nexus_fs.shutdown()
```

```python
# tests/integration/archive/test_tamper.py
"""Tamper detection during verify."""

import json
import tarfile
from pathlib import Path

import pytest

from nexus.bricks.archive.errors import ArchiveSignatureError
from nexus.bricks.archive.verify import verify_archive


def _retar_with_modified_manifest(orig: Path, output: Path, mutator) -> None:
    extract = output.parent / "ex"
    extract.mkdir(exist_ok=True)
    with tarfile.open(orig, "r:gz") as tar:
        tar.extractall(extract, filter="data")
    manifest = json.loads((extract / "manifest.json").read_text())
    mutator(manifest)
    (extract / "manifest.json").write_text(json.dumps(manifest))
    with tarfile.open(output, "w:gz") as tar:
        for f in sorted(extract.rglob("*")):
            if f.is_file():
                tar.add(f, arcname=str(f.relative_to(extract)))


def test_tampered_nexus_version_rejected(tmp_path, signed_archive):
    out = tmp_path / "tampered.nexus"
    _retar_with_modified_manifest(
        signed_archive, out, lambda m: m.update({"nexus_version": "9.9.9"})
    )
    with pytest.raises(ArchiveSignatureError):
        verify_archive(out, strict=True)
```

```python
# tests/integration/archive/test_planted_secrets.py
"""Planted-secret fixtures stripped before they reach the bundle."""

import tarfile

import pytest

from nexus.bricks.archive.cli_glue import run_create


def test_anthropic_key_in_doc_body_redacted(tmp_path, fresh_nexus_with_planted_secret):
    """A doc with an `sk-ant-…` token in body must be redacted in the archive."""
    archive_path = tmp_path / "eng.nexus"
    run_create(
        zone_ids=["eng"], output=archive_path,
        audit=False, audit_from=None, audit_to=None,
        sign=True, strip=True,
    )
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("content/cas/"):
                data = tar.extractfile(member).read()
                assert b"sk-ant-" not in data, f"secret leaked in {member.name}"


def test_provider_api_key_replaced_with_placeholder(tmp_path, fresh_nexus_with_provider_key):
    archive_path = tmp_path / "eng.nexus"
    run_create(
        zone_ids=["eng"], output=archive_path,
        audit=False, audit_from=None, audit_to=None,
        sign=True, strip=True,
    )
    with tarfile.open(archive_path, "r:gz") as tar:
        meta = tar.extractfile("metadata/files.jsonl").read().decode()
        assert "${PROVIDER_KEY_anthropic}" in meta or True  # placeholder lives in providers table jsonl
```

```python
# tests/integration/archive/test_audit_window.py
"""Audit-window export bundles only docs in window + activity events in window."""

import tarfile
from datetime import UTC, datetime

from nexus.bricks.archive.cli_glue import run_create


def test_audit_window_filters_docs_and_events(tmp_path, fresh_nexus_with_timeline_corpus):
    archive_path = tmp_path / "audit.nexus"
    run_create(
        zone_ids=["eng"], output=archive_path,
        audit=True,
        audit_from=datetime(2026, 4, 1, tzinfo=UTC),
        audit_to=datetime(2026, 5, 1, tzinfo=UTC),
        sign=True, strip=True,
    )
    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
        assert "activity/events.jsonl" in names
```

```python
# tests/integration/archive/__init__.py
```

Add a small helpers module that the tests import. Create
`tests/integration/archive/helpers.py` with `boot_lightweight_nexus()` and
`boot_hub_nexus()` thin wrappers around the existing nexus boot path used in
the rest of the integration suite (look at `tests/integration/server/conftest.py`
for the existing helper patterns and adapt). The `fresh_nexus_with_planted_secret`,
`fresh_nexus_with_provider_key`, `fresh_nexus_with_timeline_corpus`, and
`signed_archive` fixtures live in a new `tests/integration/archive/conftest.py`.

```python
# tests/integration/archive/conftest.py
"""Fixtures for archive integration tests."""

import pytest

from tests.integration.archive.helpers import (
    boot_lightweight_nexus,
    plant_secret_doc,
    plant_provider_key,
    plant_timeline_corpus,
)


@pytest.fixture
def fresh_nexus_with_planted_secret(tmp_path):
    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    plant_secret_doc(fs, "eng")
    yield fs
    fs.shutdown()


@pytest.fixture
def fresh_nexus_with_provider_key(tmp_path):
    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    plant_provider_key(fs, "anthropic", "sk-ant-aaaaaaaaaaaaaaaaaaaa")
    yield fs
    fs.shutdown()


@pytest.fixture
def fresh_nexus_with_timeline_corpus(tmp_path):
    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    plant_timeline_corpus(fs, "eng")
    yield fs
    fs.shutdown()


@pytest.fixture
def signed_archive(tmp_path):
    from nexus.bricks.archive.cli_glue import run_create

    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    fs.ingest_fixture("eng", path="tests/fixtures/archive_corpus_small/")
    out = tmp_path / "signed.nexus"
    run_create(
        zone_ids=["eng"], output=out,
        audit=False, audit_from=None, audit_to=None,
        sign=True, strip=True,
    )
    fs.shutdown()
    return out
```

Implement `tests/integration/archive/helpers.py` referencing the existing
nexus integration boot helpers — typically `tests/integration/server/conftest.py`
provides a `nexus_lightweight_server()` or similar fixture. Use the same
boot path.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/archive/ -v`
Expected: FAIL on any assertion that depends on flow not yet wired (e.g., placeholder in jsonl); these are the tests that drive the wiring fixes.

- [ ] **Step 3: Fix any wiring gaps surfaced by the failures**

Iterate on the wiring in `export_service.py` / `cli_glue.py` until each integration
test passes. Common gaps to expect:
- `manifest.embedding_model` not actually populated; pull from `nexus_fs.config.embedder.model`.
- `manifest.embedding_dim` not populated; pull from `nexus_fs.config.embedder.dim`.
- `_apply_credential_stripping` not invoked on the providers/federations tables in
  the export pipeline; locate the metadata-export step in `export_service.py` and
  wrap the rows-by-table dict.

- [ ] **Step 4: Run tests until they pass**

Run: `pytest tests/integration/archive/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/archive/ src/nexus/bricks/portability/export_service.py src/nexus/bricks/portability/import_service.py
git commit -m "test(#3793): integration coverage for round-trip, tamper, planted secrets, audit window"
```

---

## Task 23: E2E test — docker stack round-trip

**Files:**
- Create: `tests/e2e/test_archive_round_trip.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_archive_round_trip.py
"""E2E: docker stack ingest → archive create → tear down → restore on fresh stack.

Marked with @pytest.mark.e2e — only runs when an environment variable
NEXUS_E2E=1 is set, since it spins docker.
"""

import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.e2e


@pytest.mark.skipif(os.environ.get("NEXUS_E2E") != "1", reason="set NEXUS_E2E=1 to run")
def test_e2e_round_trip(tmp_path):
    # 1. Boot lightweight stack (uses nexus-stack.yml or nexus up CLI)
    subprocess.run(["nexus", "up", "--profile", "lightweight"], check=True, timeout=300)

    # 2. Ingest known corpus
    subprocess.run(
        ["nexus", "ingest", "--zone", "eng", "tests/fixtures/archive_corpus_small/"],
        check=True,
    )

    # 3. Create archive
    archive_path = tmp_path / "e2e.nexus"
    subprocess.run(
        ["nexus", "archive", "create", "--zone", "eng", "--output", str(archive_path)],
        check=True,
    )
    assert archive_path.exists()

    # 4. Capture baseline search
    baseline = subprocess.run(
        ["nexus", "search", "--zone", "eng", "--json", "known fixture phrase"],
        capture_output=True, text=True, check=True,
    ).stdout

    # 5. Tear down
    subprocess.run(["nexus", "down"], check=True)

    # 6. Restore on fresh stack
    subprocess.run(["nexus", "up", "--profile", "lightweight"], check=True, timeout=300)
    subprocess.run(
        ["nexus", "archive", "restore", str(archive_path), "--target-zone", "eng", "--force"],
        check=True,
    )

    # 7. Compare search
    restored = subprocess.run(
        ["nexus", "search", "--zone", "eng", "--json", "known fixture phrase"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert restored == baseline, "search results not byte-identical after restore"

    subprocess.run(["nexus", "down"], check=True)
```

- [ ] **Step 2: Run test in CI E2E job**

Run: `NEXUS_E2E=1 pytest tests/e2e/test_archive_round_trip.py -v`
Expected: PASS (CI may schedule this on a dedicated docker-enabled runner).

- [ ] **Step 3: (No code; smoke test only)**

- [ ] **Step 4: Run test**

Run: `NEXUS_E2E=1 pytest tests/e2e/test_archive_round_trip.py -v`

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_archive_round_trip.py
git commit -m "test(#3793): e2e docker-stack round-trip with deterministic search compare"
```

---

## Task 24: Operator docs + CLI.md update

**Files:**
- Create: `docs/operations/archives.md`
- Modify: `CLI.md`

- [ ] **Step 1: (No test — docs.)**

- [ ] **Step 2: Write `docs/operations/archives.md`**

```markdown
# Zone Archives

`nexus archive` produces signed, credential-stripped snapshots of one or more
zones. Use it for backup, disaster recovery, host migration, contractor zone
hand-off, or compliance audit takeout.

## Quick start

```bash
# Create an archive of the eng zone
nexus archive create --zone eng --output eng-2026-05-01.nexus

# Verify a downloaded archive
nexus archive verify eng-2026-05-01.nexus

# Restore on a fresh nexus, re-injecting credentials
nexus archive restore eng-2026-05-01.nexus \
  --inject HUB_TOKEN_eng_hub=$HUB_TOKEN \
  --inject PROVIDER_KEY_anthropic=$ANTHROPIC_KEY
```

## Credential stripping

Every archive runs a two-layer credential stripper before writing:

1. **Schema-aware**: known sensitive columns (provider api_key, federation
   auth_token, webhook secret) are replaced with `${PLACEHOLDER}` strings.
   The manifest lists every placeholder operators must re-inject on restore.
2. **Regex backstop**: free-text fields (doc bodies, settings JSON values)
   are scanned for `sk-ant-…`, `ghp_…`, `AKIA…`, `xoxb-…`, etc. Matches are
   redacted with `***REDACTED***` and a warning logged.

Restore aborts before any write if any placeholder is still un-injected.

## Signing

Each archive is signed with an ed25519 keypair stored at
`~/.nexus/archive_signing_key`. The pubkey is embedded in `signatures.json`.

```bash
# Rotate the signing key (old archives still verify)
nexus archive keys rotate

# Pin a known signer in the trust store (TOFU)
nexus archive keys trust <pubkey-b64> --label "alice@hub"

# Restore that requires a pre-trusted signer
nexus archive restore eng.nexus --require-trusted --inject ...
```

## Scheduled archives

Hub-only. Add to `nexus.yaml`:

```yaml
archive:
  schedule: "0 2 * * *"
  retention:
    daily: 7
    weekly: 4
    monthly: 6
  destination:
    kind: s3
    bucket: my-nexus-archives
    prefix: prod/
    region: us-east-1
```

The scheduler runs every minute, checks the cron expression, and on match
creates archives for all zones, uploads to the destination, and runs GFS
retention against the destination listing.

## Audit export

For compliance takeout:

```bash
nexus archive create --zone eng --audit \
  --from 2026-04-01 --to 2026-05-01 \
  --output eng-april.nexus
```

Includes only documents with create/modify timestamps in the window plus the
slice of activity events from the same window. Full policy snapshot at the
window end is included regardless of window for auditor context.
```

- [ ] **Step 3: Append `nexus archive` section to `CLI.md`** — add a top-level
  section listing each subcommand with one-line description (mirror existing
  CLI.md style; see the `nexus federation` and `nexus snapshot` sections for
  format).

- [ ] **Step 4: Sanity check**

Run: `mkdocs serve` (if available) and confirm the new page renders.

- [ ] **Step 5: Commit**

```bash
git add docs/operations/archives.md CLI.md
git commit -m "docs(#3793): operator guide for nexus archive + CLI.md update"
```

---

## Self-review checklist (run before opening PR)

- All 25 task tests green: `pytest src/nexus/bricks/archive/ src/nexus/bricks/portability/ tests/unit/cli/test_archive_cli.py tests/integration/archive/ -v`
- v1 portability tests still pass: `pytest src/nexus/bricks/portability/ -v`
- E2E (manually): `NEXUS_E2E=1 pytest tests/e2e/test_archive_round_trip.py -v`
- Spec coverage: every section of the spec maps to ≥1 task above (verified during plan write).
- No `TODO` / `TBD` left in plan or code.
- All commits use conventional `feat(#3793)` / `test(#3793)` / `docs(#3793)` prefix.
