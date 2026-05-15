# Portability Redacted-Fields Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a typed redaction contract for `bricks/portability/` derived from each backend's existing `ConnectionArg.secret=True` flag, plus mount-config export/import with forced credential re-injection on import.

**Architecture:** Three new modules (`redaction.py`, `mount_export.py`, `mount_import.py`) sit alongside existing portability code. They reuse `PlaceholderRef` so the manifest's placeholder list stays uniform across DB rows and mount configs. `BUNDLE_FORMAT_VERSION` bumps `"2.0.0"` → `"3.0.0"`; new `manifest-v3.json` schema is added next to existing `manifest-v1.json`. `ConnectorRegistry.get_info(...).connector_class.CONNECTION_ARGS` is the single source of truth for which fields are secrets.

**Tech Stack:** Python 3.11+, pytest, dataclasses, JSON schema, ed25519 signing infra (existing).

**Spec:** `docs/superpowers/specs/2026-05-09-portability-redacted-fields-design.md`

---

## File Structure

**New files:**
- `src/nexus/bricks/portability/redaction.py` — derive redaction set, secret-shape regex audit, redact one config dict
- `src/nexus/bricks/portability/mount_export.py` — collect mount configs, run redaction, write `mounts.jsonl`
- `src/nexus/bricks/portability/mount_import.py` — read mounts, validate overrides, materialize, restore
- `src/nexus/bricks/portability/schemas/manifest-v3.json` — schema for new bundle version
- `src/nexus/bricks/portability/tests/test_redaction_audit.py` — CI gate audit
- `src/nexus/bricks/portability/tests/test_redaction_unit.py` — redaction unit
- `src/nexus/bricks/portability/tests/test_mount_export.py` — export unit
- `src/nexus/bricks/portability/tests/test_mount_import.py` — import unit
- `src/nexus/bricks/portability/tests/test_manifest_v3.py` — version + cross-version
- `src/nexus/bricks/portability/tests/test_roundtrip_with_mounts.py` — integration roundtrip

**Modified files:**
- `src/nexus/bricks/portability/models.py` — bump `BUNDLE_FORMAT_VERSION`, add `MountRecord`, add `mount_count`, new errors, options additions
- `src/nexus/bricks/portability/export_service.py` — wire `mount_export` when `options.include_mounts=True`
- `src/nexus/bricks/portability/import_service.py` — wire `mount_import`; validate before any side effect
- `src/nexus/bricks/portability/__init__.py` — public API exports

---

## Task 1: Add new errors and `MountRecord` dataclass to models.py

**Files:**
- Modify: `src/nexus/bricks/portability/models.py:50` (constant), `:1190` (BUNDLE_PATHS); append errors and `MountRecord` near end of module
- Test: `src/nexus/bricks/portability/tests/test_mount_record.py`

- [ ] **Step 1: Write failing test for MountRecord round-trip**

Create `src/nexus/bricks/portability/tests/test_mount_record.py`:

```python
"""Tests for MountRecord dataclass."""

from nexus.bricks.portability.models import MountRecord


def test_mount_record_round_trip_dict():
    rec = MountRecord(
        mount_id="m-1",
        mount_point="/personal/alice",
        backend_type="path_s3",
        backend_config={"bucket_name": "acme", "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}"},
        owner_user_id="alice",
        zone_id="acme",
        description=None,
    )
    d = rec.to_dict()
    rec2 = MountRecord.from_dict(d)
    assert rec2 == rec


def test_mount_record_handles_none_zone_and_owner():
    rec = MountRecord(
        mount_id="m-2",
        mount_point="/team/x",
        backend_type="path_local",
        backend_config={"root": "/data"},
        owner_user_id=None,
        zone_id=None,
        description="team mount",
    )
    rec2 = MountRecord.from_dict(rec.to_dict())
    assert rec2 == rec
```

- [ ] **Step 2: Write failing test for new errors**

Append to the same file:

```python
import pytest

from nexus.bricks.portability.models import (
    MissingCredentialsError,
    SensitiveFieldNotDeclaredError,
)


def test_sensitive_field_not_declared_error_carries_payload():
    err = SensitiveFieldNotDeclaredError(backend_type="path_s3", fields=["my_secret"])
    assert err.backend_type == "path_s3"
    assert err.fields == ["my_secret"]
    assert "path_s3" in str(err)
    assert "my_secret" in str(err)


def test_missing_credentials_error_lists_all_gaps():
    err = MissingCredentialsError(missing={"m-1": ["a", "b"], "m-2": ["c"]})
    msg = str(err)
    assert "m-1" in msg and "a" in msg and "b" in msg
    assert "m-2" in msg and "c" in msg


def test_missing_credentials_error_is_value_error():
    with pytest.raises(ValueError):
        raise MissingCredentialsError(missing={"m-1": ["a"]})
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_record.py -v
```

Expected: All tests FAIL with `ImportError: cannot import name 'MountRecord' from nexus.bricks.portability.models` (or similar).

- [ ] **Step 4: Implement MountRecord and errors in models.py**

Find the end of `models.py` (after `BUNDLE_PATHS`). Append:

```python
# =============================================================================
# Mount portability (Issue #4083)
# =============================================================================


@dataclass(frozen=True)
class MountRecord:
    """One line of mounts.jsonl. Mirrors MountManager's persisted shape with
    secrets replaced by ${MOUNT_<id>_<FIELD>} placeholders on export."""

    mount_id: str
    mount_point: str
    backend_type: str
    backend_config: dict[str, Any]
    owner_user_id: str | None = None
    zone_id: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mount_id": self.mount_id,
            "mount_point": self.mount_point,
            "backend_type": self.backend_type,
            "backend_config": self.backend_config,
            "owner_user_id": self.owner_user_id,
            "zone_id": self.zone_id,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MountRecord":
        return cls(
            mount_id=data["mount_id"],
            mount_point=data["mount_point"],
            backend_type=data["backend_type"],
            backend_config=data["backend_config"],
            owner_user_id=data.get("owner_user_id"),
            zone_id=data.get("zone_id"),
            description=data.get("description"),
        )


class SensitiveFieldNotDeclaredError(ValueError):
    """Export-time. Backend has CONNECTION_ARGS keys whose names match the
    secret-shape heuristic but aren't marked secret=True."""

    def __init__(self, backend_type: str, fields: list[str]) -> None:
        self.backend_type = backend_type
        self.fields = list(fields)
        super().__init__(
            f"Backend {backend_type!r} has secret-shaped fields not marked secret=True: "
            f"{self.fields}. Mark them secret=True in CONNECTION_ARGS or rename them."
        )


class MissingCredentialsError(ValueError):
    """Import-time. Bundle has redacted mount fields with no override supplied.
    Reports every gap in one error so operators see the full set at once."""

    def __init__(self, missing: dict[str, list[str]]) -> None:
        self.missing = {k: list(v) for k, v in missing.items()}
        lines = [f"  {mid}: {fields}" for mid, fields in sorted(self.missing.items())]
        super().__init__(
            "Mount imports require credential overrides for:\n" + "\n".join(lines)
        )
```

Update `BUNDLE_PATHS` (find at `models.py:1190`):

```python
BUNDLE_PATHS = {
    # ... existing entries unchanged ...
    "mounts": "mounts.jsonl",  # NEW
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_record.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/portability/models.py \
        src/nexus/bricks/portability/tests/test_mount_record.py
git commit -m "feat(portability): add MountRecord and credential-contract errors

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 2: Bump BUNDLE_FORMAT_VERSION and add `mount_count` to ExportManifest

**Files:**
- Modify: `src/nexus/bricks/portability/models.py:50,55` (constants), `:523-720` (`ExportManifest` dataclass + `to_dict`/`from_dict`)
- Test: `src/nexus/bricks/portability/tests/test_manifest_v3.py`

- [ ] **Step 1: Write failing test**

Create `src/nexus/bricks/portability/tests/test_manifest_v3.py`:

```python
"""Tests for v3 bundle manifest additions."""

from nexus.bricks.portability.models import (
    BUNDLE_FORMAT_VERSION,
    MANIFEST_SCHEMA_URL,
    ExportManifest,
)


def test_format_version_is_v3():
    assert BUNDLE_FORMAT_VERSION == "3.0.0"


def test_manifest_schema_url_is_v3():
    assert MANIFEST_SCHEMA_URL == "https://nexus.io/schemas/manifest-v3.json"


def test_manifest_includes_mount_count():
    m = ExportManifest(source_zone_id="z1")
    m.mount_count = 5
    d = m.to_dict()
    assert d["statistics"]["mount_count"] == 5


def test_manifest_default_mount_count_is_zero():
    m = ExportManifest(source_zone_id="z1")
    assert m.mount_count == 0
    assert m.to_dict()["statistics"]["mount_count"] == 0


def test_manifest_round_trip_preserves_mount_count():
    m = ExportManifest(source_zone_id="z1")
    m.mount_count = 7
    d = m.to_dict()
    m2 = ExportManifest.from_dict(d)
    assert m2.mount_count == 7


def test_v1_bundle_loads_with_default_mount_count():
    """A v1/v2 manifest dict (no mount_count) must still load."""
    legacy_dict = {
        "format_version": "2.0.0",
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {
            "file_count": 0,
            "total_size_bytes": 0,
            "content_blob_count": 0,
            "permission_count": 0,
            "embedding_count": 0,
        },
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    m = ExportManifest.from_dict(legacy_dict)
    assert m.mount_count == 0
    assert m.format_version == "2.0.0"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest src/nexus/bricks/portability/tests/test_manifest_v3.py -v
```

Expected: FAIL — version is `"2.0.0"`, no `mount_count` attribute.

- [ ] **Step 3: Bump version constants**

Edit `src/nexus/bricks/portability/models.py` near line 50:

```python
BUNDLE_FORMAT_VERSION = "3.0.0"
```

Near line 55:

```python
MANIFEST_SCHEMA_URL = "https://nexus.io/schemas/manifest-v3.json"
```

- [ ] **Step 4: Add `mount_count` field and serialize/deserialize**

In `models.py`, locate `ExportManifest` (line 523). Add field next to other v2 additions (after `placeholders: list[PlaceholderRef]`, around line 596):

```python
    # v3 additions (Issue #4083)
    mount_count: int = 0
```

In `to_dict`, find the `"statistics"` block (around line 617) and add `mount_count`:

```python
            "statistics": {
                "file_count": self.file_count,
                "total_size_bytes": self.total_size_bytes,
                "content_blob_count": self.content_blob_count,
                "permission_count": self.permission_count,
                "embedding_count": self.embedding_count,
                "mount_count": self.mount_count,  # NEW
            },
```

Also update the `"$schema"` literal at the top of `to_dict`:

```python
            "$schema": "https://nexus.io/schemas/manifest-v3.json",
```

In `from_dict` (around line 720), find the statistics-extraction block and add a default-tolerant lookup:

```python
            mount_count=stats.get("mount_count", 0),
```

(Locate the existing `stats = data.get("statistics", {})` line; if absent, replace direct lookups with `.get(...)` defaults.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_manifest_v3.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 6: Run the existing portability test suite to confirm no regressions**

```bash
pytest src/nexus/bricks/portability/tests/ -v
```

Expected: ALL existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/bricks/portability/models.py \
        src/nexus/bricks/portability/tests/test_manifest_v3.py
git commit -m "feat(portability): bump bundle format to v3.0.0 with mount_count

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 3: `manifest-v3.json` schema file

**Files:**
- Create: `src/nexus/bricks/portability/schemas/manifest-v3.json`
- Test: extend `src/nexus/bricks/portability/tests/test_manifest_v3.py`

- [ ] **Step 1: Write failing test for schema validation**

Append to `test_manifest_v3.py`:

```python
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

SCHEMA_PATH = (
    Path(__file__).parent.parent / "schemas" / "manifest-v3.json"
)


def test_v3_schema_file_exists_and_is_valid_json():
    text = SCHEMA_PATH.read_text()
    data = json.loads(text)
    assert data["$id"].endswith("manifest-v3.json")


def test_v3_manifest_validates_against_schema():
    schema = json.loads(SCHEMA_PATH.read_text())
    m = ExportManifest(source_zone_id="z1")
    m.mount_count = 3
    jsonschema.validate(m.to_dict(), schema)


def test_v3_schema_rejects_unknown_root_field():
    schema = json.loads(SCHEMA_PATH.read_text())
    bad = ExportManifest(source_zone_id="z1").to_dict()
    bad["totally_unknown_field"] = "bogus"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest src/nexus/bricks/portability/tests/test_manifest_v3.py::test_v3_schema_file_exists_and_is_valid_json -v
```

Expected: FAIL — file does not exist.

- [ ] **Step 3: Copy v1 schema to v3, update `$id` and add `mount_count`**

```bash
cp src/nexus/bricks/portability/schemas/manifest-v1.json \
   src/nexus/bricks/portability/schemas/manifest-v3.json
```

Edit `src/nexus/bricks/portability/schemas/manifest-v3.json`:

1. Change `$id` to `"https://nexus.io/schemas/manifest-v3.json"`.
2. Update the `format_version.examples` to `["3.0.0"]`.
3. Inside `properties.$schema.const`, change to `"https://nexus.io/schemas/manifest-v3.json"`.
4. Inside `properties.statistics.properties`, add:
   ```json
   "mount_count": {
     "type": "integer",
     "description": "Number of mount records in mounts.jsonl",
     "minimum": 0,
     "default": 0
   }
   ```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_manifest_v3.py -v
```

Expected: ALL tests in this file PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/schemas/manifest-v3.json \
        src/nexus/bricks/portability/tests/test_manifest_v3.py
git commit -m "feat(portability): add manifest-v3.json schema with mount_count

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 4: `redaction.py` — declared_secret_fields, audit_backend, redact_config

**Files:**
- Create: `src/nexus/bricks/portability/redaction.py`
- Test: `src/nexus/bricks/portability/tests/test_redaction_unit.py`

- [ ] **Step 1: Write failing tests**

Create `src/nexus/bricks/portability/tests/test_redaction_unit.py`:

```python
"""Unit tests for redaction.py."""

import pytest

from nexus.bricks.portability.models import (
    PlaceholderRef,
    SensitiveFieldNotDeclaredError,
)
from nexus.bricks.portability.redaction import (
    SECRET_SHAPED,
    audit_backend,
    declared_secret_fields,
    redact_config,
)


def test_secret_shape_regex_matches_obvious_names():
    for name in ("api_key", "secret_access_key", "session_token", "password", "credential"):
        assert SECRET_SHAPED.search(name), name


def test_secret_shape_regex_ignores_benign_names():
    for name in ("bucket_name", "region", "prefix", "path"):
        assert not SECRET_SHAPED.search(name), name


def test_declared_secret_fields_for_path_s3():
    fields = declared_secret_fields("path_s3")
    assert "access_key_id" in fields
    assert "secret_access_key" in fields
    assert "session_token" in fields
    assert "bucket_name" not in fields


def test_audit_backend_passes_for_path_s3():
    """All secret-shaped names in path_s3 are already marked secret=True."""
    assert audit_backend("path_s3") == []


def test_redact_config_replaces_only_declared_secrets():
    config = {
        "bucket_name": "acme",
        "access_key_id": "AKIA1234",
        "secret_access_key": "wJalr...",
    }
    redacted, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["bucket_name"] == "acme"
    assert redacted["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"
    assert redacted["secret_access_key"] == "${MOUNT_m-1_SECRET_ACCESS_KEY}"
    assert {p.name for p in placeholders} == {
        "MOUNT_m-1_ACCESS_KEY_ID",
        "MOUNT_m-1_SECRET_ACCESS_KEY",
    }
    assert all(isinstance(p, PlaceholderRef) for p in placeholders)


def test_redact_config_skips_none_values():
    config = {"bucket_name": "acme", "access_key_id": None}
    redacted, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["access_key_id"] is None
    assert placeholders == []


def test_redact_config_idempotent_on_already_redacted():
    config = {"bucket_name": "acme", "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}"}
    redacted, _ = redact_config("path_s3", config, mount_id="m-1")
    assert redacted["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"


def test_redact_config_audit_failure_raises():
    """If a backend has a secret-shaped name not marked secret=True, raise."""
    from unittest.mock import patch

    fake_args = {
        "bucket": __import__("nexus.extensions.types", fromlist=["ConnectionArg"]).ConnectionArg(
            type=__import__("nexus.extensions.types", fromlist=["ArgType"]).ArgType.STRING,
            description="ok",
        ),
        "my_token": __import__("nexus.extensions.types", fromlist=["ConnectionArg"]).ConnectionArg(
            type=__import__("nexus.extensions.types", fromlist=["ArgType"]).ArgType.STRING,
            description="should be secret but isn't",
            secret=False,
        ),
    }

    class FakeBackend:
        CONNECTION_ARGS = fake_args

    with patch("nexus.bricks.portability.redaction._get_connection_args", return_value=fake_args):
        with pytest.raises(SensitiveFieldNotDeclaredError) as exc:
            redact_config("fake", {"bucket": "x", "my_token": "y"}, mount_id="m-1")
        assert "my_token" in exc.value.fields


def test_placeholder_field_dotted_path_is_predictable():
    config = {"access_key_id": "AKIA"}
    _, placeholders = redact_config("path_s3", config, mount_id="m-1")
    assert placeholders[0].field == "mounts.m-1.access_key_id"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest src/nexus/bricks/portability/tests/test_redaction_unit.py -v
```

Expected: FAIL — `redaction` module does not exist.

- [ ] **Step 3: Implement `redaction.py`**

Create `src/nexus/bricks/portability/redaction.py`:

```python
"""Typed redaction contract for mount-config exports (Issue #4083).

Single source of truth: each connector's CONNECTION_ARGS already declares
`secret: bool` per argument. This module derives the redaction set from
that declaration, and runs a heuristic audit that hard-fails export when a
secret-shaped argument name (key/secret/token/password/cred) isn't marked
secret=True.

Why a separate module: the redaction policy is the contract surface that
both export and import depend on. Keeping it focused (one file, three
public functions) means the audit test, the export pipeline, and any
future tooling all see the same answer to "is this field a secret?".
"""

from __future__ import annotations

import re
from typing import Any

from nexus.bricks.portability.models import (
    PlaceholderRef,
    SensitiveFieldNotDeclaredError,
)

SECRET_SHAPED = re.compile(r"(?i)(key|secret|token|password|cred)")
"""Heuristic regex for argument names that should be marked secret=True.

Audit fails if a CONNECTION_ARGS key matches this regex but has secret=False.
The check is deliberately strict — every false positive is a forcing function
to either rename the field or mark it secret=True. No allowlist exists today;
add one only if real friction emerges (see spec for `audit_safe` follow-up)."""


def _get_connection_args(backend_type: str) -> dict[str, Any]:
    """Return the CONNECTION_ARGS dict for `backend_type`, or {} if unavailable.

    Returns {} for placeholder registry entries whose connector module failed
    to import (extra not installed) — those are skipped by callers, not
    treated as audit failures.
    """
    from nexus.backends.base.registry import ConnectorRegistry

    info = ConnectorRegistry.get_info(backend_type)
    cls = info.connector_class
    if cls is None:
        return {}
    return getattr(cls, "CONNECTION_ARGS", {}) or {}


def declared_secret_fields(backend_type: str) -> frozenset[str]:
    """Return the set of CONNECTION_ARGS keys with secret=True for a backend."""
    args = _get_connection_args(backend_type)
    return frozenset(name for name, arg in args.items() if getattr(arg, "secret", False))


def audit_backend(backend_type: str) -> list[str]:
    """Return CONNECTION_ARGS keys that look secret-shaped but aren't marked secret=True.

    Empty list = backend passes audit. Non-empty = export must abort.
    Returns [] for backends whose connector class hasn't loaded (no audit possible
    until the optional extra is installed; the audit test skips those entries).
    """
    args = _get_connection_args(backend_type)
    return [
        name
        for name, arg in args.items()
        if SECRET_SHAPED.search(name) and not getattr(arg, "secret", False)
    ]


def redact_config(
    backend_type: str,
    config: dict[str, Any],
    *,
    mount_id: str,
) -> tuple[dict[str, Any], list[PlaceholderRef]]:
    """Strip declared secret fields from a mount's backend_config.

    Args:
        backend_type: Connector registry key (e.g., "path_s3").
        config: The mount's backend_config dict.
        mount_id: Used to namespace placeholders (uniqueness across bundle).

    Returns:
        (redacted_config, placeholders). The redacted dict is a shallow copy with
        secret fields replaced by `${MOUNT_<id>_<FIELD_UPPER>}`. None values are
        skipped (no placeholder generated for a field that's None).

    Raises:
        SensitiveFieldNotDeclaredError: if audit_backend(backend_type) is non-empty.
    """
    leaks = audit_backend(backend_type)
    if leaks:
        raise SensitiveFieldNotDeclaredError(backend_type=backend_type, fields=leaks)

    secrets = declared_secret_fields(backend_type)
    out = dict(config)
    placeholders: list[PlaceholderRef] = []

    for field_name in secrets & out.keys():
        value = out[field_name]
        if value is None:
            continue
        ph_name = f"MOUNT_{mount_id}_{field_name.upper()}"
        placeholder_string = f"${{{ph_name}}}"
        if value == placeholder_string:
            continue
        out[field_name] = placeholder_string
        placeholders.append(
            PlaceholderRef(name=ph_name, field=f"mounts.{mount_id}.{field_name}")
        )

    return out, placeholders


__all__ = [
    "SECRET_SHAPED",
    "declared_secret_fields",
    "audit_backend",
    "redact_config",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_redaction_unit.py -v
```

Expected: 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/redaction.py \
        src/nexus/bricks/portability/tests/test_redaction_unit.py
git commit -m "feat(portability): add redaction module deriving secrets from CONNECTION_ARGS

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 5: CI audit test over CONNECTOR_MANIFEST

**Files:**
- Test: `src/nexus/bricks/portability/tests/test_redaction_audit.py`

- [ ] **Step 1: Write the audit test**

Create `src/nexus/bricks/portability/tests/test_redaction_audit.py`:

```python
"""CI gate: every registered connector must pass the secret-shape audit.

Adding a backend whose CONNECTION_ARGS has a key like 'token_xyz' or
'auth_secret' without marking it secret=True must fail this test.
"""

import pytest

from nexus.backends._manifest import CONNECTOR_MANIFEST
from nexus.bricks.portability.redaction import (
    SECRET_SHAPED,
    _get_connection_args,
    audit_backend,
)


@pytest.mark.parametrize("entry", CONNECTOR_MANIFEST, ids=lambda e: e.name)
def test_connector_passes_secret_shape_audit(entry):
    """Every CONNECTION_ARGS key matching SECRET_SHAPED must be marked secret=True."""
    args = _get_connection_args(entry.name)
    if not args:
        pytest.skip(f"{entry.name}: connector class not loaded (optional extra not installed)")
    leaks = audit_backend(entry.name)
    assert not leaks, (
        f"{entry.name}: argument names {leaks} match secret-shape regex "
        f"({SECRET_SHAPED.pattern}) but are not marked secret=True. "
        f"Either rename them or set secret=True in CONNECTION_ARGS."
    )
```

- [ ] **Step 2: Run the audit test**

```bash
pytest src/nexus/bricks/portability/tests/test_redaction_audit.py -v
```

Expected: ALL connectors PASS or SKIP (skipped = optional extra absent in dev environment). If a connector FAILs, the failure message names exactly which fields need to be fixed in that connector's CONNECTION_ARGS — fix those before continuing the plan.

- [ ] **Step 3: If any connector fails the audit, fix it in a separate commit**

For each failing connector, edit its CONNECTION_ARGS to set `secret=True` on the offending fields. Re-run the audit test. Commit each connector fix as its own commit:

```bash
git add src/nexus/backends/<path>/connector.py
git commit -m "fix(<connector>): mark <field> as secret=True for portability audit

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

(If no connectors fail, skip this step.)

- [ ] **Step 4: Commit the audit test**

```bash
git add src/nexus/bricks/portability/tests/test_redaction_audit.py
git commit -m "test(portability): CI gate - every connector passes secret-shape audit

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 6: Add export/import options for mounts

**Files:**
- Modify: `src/nexus/bricks/portability/models.py:308` (`ZoneExportOptions`), `:393` (`ZoneImportOptions`)
- Test: `src/nexus/bricks/portability/tests/test_mount_options.py`

- [ ] **Step 1: Write failing test**

Create `src/nexus/bricks/portability/tests/test_mount_options.py`:

```python
"""Tests for new mount-portability options."""

from pathlib import Path

from nexus.bricks.portability.models import ZoneExportOptions, ZoneImportOptions


def test_export_options_default_include_mounts_false():
    o = ZoneExportOptions(output_path=Path("/tmp/x.nexus"))
    assert o.include_mounts is False


def test_export_options_accepts_include_mounts_true():
    o = ZoneExportOptions(output_path=Path("/tmp/x.nexus"), include_mounts=True)
    assert o.include_mounts is True


def test_import_options_default_mount_overrides_none():
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"))
    assert o.mount_overrides is None


def test_import_options_default_restore_mounts_true():
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"))
    assert o.restore_mounts is True


def test_import_options_accepts_mount_overrides():
    overrides = {"m-1": {"access_key_id": "AKIA"}}
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"), mount_overrides=overrides)
    assert o.mount_overrides == overrides


def test_import_options_accepts_restore_mounts_false():
    o = ZoneImportOptions(bundle_path=Path("/tmp/x.nexus"), restore_mounts=False)
    assert o.restore_mounts is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_options.py -v
```

Expected: FAIL — fields don't exist.

- [ ] **Step 3: Add fields to options dataclasses**

In `models.py`, locate `ZoneExportOptions` (line 308). Add field with the other v2-style flags (look for `include_embeddings` or `strip_credentials`):

```python
    include_mounts: bool = False  # Issue #4083 — opt-in mount config export
```

In `ZoneImportOptions` (line 393). Append after `force` field (around line 448):

```python
    # Mount portability (Issue #4083)
    restore_mounts: bool = True
    mount_overrides: dict[str, dict[str, str]] | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_options.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/models.py \
        src/nexus/bricks/portability/tests/test_mount_options.py
git commit -m "feat(portability): add include_mounts / mount_overrides / restore_mounts

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 7: `mount_export.py` — collect mounts and write redacted `mounts.jsonl`

**Files:**
- Create: `src/nexus/bricks/portability/mount_export.py`
- Test: `src/nexus/bricks/portability/tests/test_mount_export.py`

- [ ] **Step 1: Write failing tests**

Create `src/nexus/bricks/portability/tests/test_mount_export.py`:

```python
"""Tests for mount_export.py."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.models import (
    PlaceholderRef,
    SensitiveFieldNotDeclaredError,
)
from nexus.bricks.portability.mount_export import (
    collect_mounts,
    redact_and_write,
)


@pytest.fixture
def s3_mount_dict():
    return {
        "mount_id": "m-1",
        "mount_point": "/personal/alice",
        "backend_type": "path_s3",
        "backend_config": {
            "bucket_name": "acme",
            "access_key_id": "AKIA1234",
            "secret_access_key": "wJalr...",
        },
        "owner_user_id": "alice",
        "zone_id": "acme",
        "description": None,
    }


def test_collect_mounts_calls_list_mounts_with_zone_filter(s3_mount_dict):
    mgr = MagicMock()
    mgr.list_mounts.return_value = [s3_mount_dict]
    out = collect_mounts(mgr, zone_id="acme")
    mgr.list_mounts.assert_called_once_with(zone_id="acme")
    assert out == [s3_mount_dict]


def test_collect_mounts_no_zone_filter():
    mgr = MagicMock()
    mgr.list_mounts.return_value = []
    collect_mounts(mgr, zone_id=None)
    mgr.list_mounts.assert_called_once_with(zone_id=None)


def test_redact_and_write_redacts_secrets_and_returns_placeholders(tmp_path, s3_mount_dict):
    out_path = tmp_path / "mounts.jsonl"
    placeholders = redact_and_write([s3_mount_dict], out_path=out_path)

    assert out_path.exists()
    line = out_path.read_text().strip()
    record = json.loads(line)
    assert record["backend_config"]["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"
    assert record["backend_config"]["secret_access_key"] == "${MOUNT_m-1_SECRET_ACCESS_KEY}"
    assert record["backend_config"]["bucket_name"] == "acme"  # not redacted

    assert {p.name for p in placeholders} == {
        "MOUNT_m-1_ACCESS_KEY_ID",
        "MOUNT_m-1_SECRET_ACCESS_KEY",
    }
    assert all(isinstance(p, PlaceholderRef) for p in placeholders)


def test_redact_and_write_sorts_lines_by_mount_id(tmp_path):
    mounts = [
        {"mount_id": "m-z", "mount_point": "/z", "backend_type": "path_local",
         "backend_config": {}, "owner_user_id": None, "zone_id": None, "description": None},
        {"mount_id": "m-a", "mount_point": "/a", "backend_type": "path_local",
         "backend_config": {}, "owner_user_id": None, "zone_id": None, "description": None},
    ]
    out_path = tmp_path / "mounts.jsonl"
    redact_and_write(mounts, out_path=out_path)
    lines = out_path.read_text().strip().split("\n")
    assert json.loads(lines[0])["mount_id"] == "m-a"
    assert json.loads(lines[1])["mount_id"] == "m-z"


def test_redact_and_write_byte_stable_across_runs(tmp_path, s3_mount_dict):
    out1 = tmp_path / "a.jsonl"
    out2 = tmp_path / "b.jsonl"
    redact_and_write([s3_mount_dict], out_path=out1)
    redact_and_write([s3_mount_dict], out_path=out2)
    assert out1.read_bytes() == out2.read_bytes()


def test_redact_and_write_audit_failure_raises(tmp_path):
    """If a mount references a backend whose CONNECTION_ARGS audit fails, raise."""
    from unittest.mock import patch

    bad_mount = {
        "mount_id": "m-1", "mount_point": "/x", "backend_type": "path_s3",
        "backend_config": {"my_token": "x"},
        "owner_user_id": None, "zone_id": None, "description": None,
    }
    with patch(
        "nexus.bricks.portability.redaction.audit_backend",
        return_value=["my_token"],
    ):
        with pytest.raises(SensitiveFieldNotDeclaredError):
            redact_and_write([bad_mount], out_path=tmp_path / "x.jsonl")


def test_redact_and_write_empty_list_writes_empty_file(tmp_path):
    out_path = tmp_path / "mounts.jsonl"
    placeholders = redact_and_write([], out_path=out_path)
    assert out_path.exists()
    assert out_path.read_text() == ""
    assert placeholders == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_export.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `mount_export.py`**

Create `src/nexus/bricks/portability/mount_export.py`:

```python
"""Mount-config export for .nexus bundles (Issue #4083).

Pulls mount configurations from MountManager, runs each through the redaction
contract (declared via ConnectionArg.secret=True), and writes a sorted JSONL
file alongside the rest of the bundle. Audit failures abort the export with
SensitiveFieldNotDeclaredError before any bytes are written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.bricks.portability.models import MountRecord, PlaceholderRef
from nexus.bricks.portability.redaction import redact_config

if TYPE_CHECKING:
    from nexus.bricks.mount.mount_manager import MountManager


def collect_mounts(
    mount_manager: "MountManager",
    *,
    zone_id: str | None,
) -> list[dict[str, Any]]:
    """Return raw mount-config dicts from MountManager (zone-filtered if zone_id set).

    Output dicts have keys: mount_id, mount_point, backend_type, backend_config,
    owner_user_id, zone_id, description.
    """
    return mount_manager.list_mounts(zone_id=zone_id)


def redact_and_write(
    mounts: list[dict[str, Any]],
    *,
    out_path: Path,
) -> list[PlaceholderRef]:
    """Per-mount: run redaction, write JSONL line; return aggregated placeholders.

    Lines are sorted by mount_id for byte-stable output. Each line is canonical
    JSON (sort_keys=True, no extra whitespace). On audit failure for any mount,
    raises SensitiveFieldNotDeclaredError before writing anything.
    """
    sorted_mounts = sorted(mounts, key=lambda m: m["mount_id"])

    # Phase 1: redact in memory, surface audit failures before any I/O.
    redacted_records: list[MountRecord] = []
    placeholders: list[PlaceholderRef] = []
    for raw in sorted_mounts:
        redacted_config, mount_phs = redact_config(
            backend_type=raw["backend_type"],
            config=raw.get("backend_config", {}) or {},
            mount_id=raw["mount_id"],
        )
        record = MountRecord(
            mount_id=raw["mount_id"],
            mount_point=raw["mount_point"],
            backend_type=raw["backend_type"],
            backend_config=redacted_config,
            owner_user_id=raw.get("owner_user_id"),
            zone_id=raw.get("zone_id"),
            description=raw.get("description"),
        )
        redacted_records.append(record)
        placeholders.extend(mount_phs)

    # Phase 2: write JSONL
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        for record in redacted_records:
            fh.write(json.dumps(record.to_dict(), sort_keys=True))
            fh.write("\n")
    if not redacted_records:
        out_path.write_text("")  # touch empty file

    return placeholders


__all__ = ["collect_mounts", "redact_and_write"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_export.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/mount_export.py \
        src/nexus/bricks/portability/tests/test_mount_export.py
git commit -m "feat(portability): mount_export module with redacted JSONL output

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 8: `mount_import.py` — read, validate, materialize, restore

**Files:**
- Create: `src/nexus/bricks/portability/mount_import.py`
- Test: `src/nexus/bricks/portability/tests/test_mount_import.py`

- [ ] **Step 1: Write failing tests**

Create `src/nexus/bricks/portability/tests/test_mount_import.py`:

```python
"""Tests for mount_import.py."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.models import (
    ConflictMode,
    MissingCredentialsError,
    MountRecord,
)
from nexus.bricks.portability.mount_import import (
    import_mounts,
    materialize,
    read_mounts,
    validate_overrides,
)


@pytest.fixture
def redacted_record():
    return MountRecord(
        mount_id="m-1",
        mount_point="/personal/alice",
        backend_type="path_s3",
        backend_config={
            "bucket_name": "acme",
            "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}",
            "secret_access_key": "${MOUNT_m-1_SECRET_ACCESS_KEY}",
        },
        owner_user_id="alice",
        zone_id="acme",
        description=None,
    )


def test_read_mounts_absent_file_returns_empty(tmp_path):
    assert read_mounts(tmp_path) == []


def test_read_mounts_parses_jsonl(tmp_path, redacted_record):
    p = tmp_path / "mounts.jsonl"
    p.write_text(json.dumps(redacted_record.to_dict()) + "\n")
    out = read_mounts(tmp_path)
    assert len(out) == 1
    assert out[0] == redacted_record


def test_read_mounts_skips_blank_lines(tmp_path, redacted_record):
    p = tmp_path / "mounts.jsonl"
    p.write_text(json.dumps(redacted_record.to_dict()) + "\n\n")
    assert len(read_mounts(tmp_path)) == 1


def test_validate_overrides_no_redacted_fields_passes():
    rec = MountRecord(
        mount_id="m-1", mount_point="/x", backend_type="path_local",
        backend_config={"root": "/data"},
    )
    validate_overrides([rec], overrides=None)  # no raise


def test_validate_overrides_missing_raises_with_all_gaps(redacted_record):
    rec2 = MountRecord(
        mount_id="m-2", mount_point="/y", backend_type="path_s3",
        backend_config={"access_key_id": "${MOUNT_m-2_ACCESS_KEY_ID}"},
    )
    with pytest.raises(MissingCredentialsError) as exc:
        validate_overrides([redacted_record, rec2], overrides=None)
    missing = exc.value.missing
    assert set(missing.keys()) == {"m-1", "m-2"}
    assert "access_key_id" in missing["m-1"]
    assert "secret_access_key" in missing["m-1"]
    assert "access_key_id" in missing["m-2"]


def test_validate_overrides_partial_provided_still_raises(redacted_record):
    overrides = {"m-1": {"access_key_id": "AKIA"}}  # missing secret_access_key
    with pytest.raises(MissingCredentialsError) as exc:
        validate_overrides([redacted_record], overrides=overrides)
    assert exc.value.missing == {"m-1": ["secret_access_key"]}


def test_validate_overrides_full_passes(redacted_record):
    overrides = {"m-1": {"access_key_id": "AKIA", "secret_access_key": "wJalr"}}
    validate_overrides([redacted_record], overrides=overrides)  # no raise


def test_materialize_substitutes_placeholders(redacted_record):
    overrides = {"access_key_id": "AKIA", "secret_access_key": "wJalr"}
    out = materialize(redacted_record, overrides)
    assert out["access_key_id"] == "AKIA"
    assert out["secret_access_key"] == "wJalr"
    assert out["bucket_name"] == "acme"


def test_import_mounts_calls_save_mount_per_record(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = None  # no conflict
    overrides = {"m-1": {"access_key_id": "AKIA", "secret_access_key": "wJalr"}}
    errors = import_mounts(
        mounts=[redacted_record],
        overrides=overrides,
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.SKIP,
    )
    assert errors == []
    assert mgr.save_mount.call_count == 1
    kwargs = mgr.save_mount.call_args.kwargs
    assert kwargs["mount_point"] == "/personal/alice"
    assert kwargs["backend_type"] == "path_s3"
    assert kwargs["backend_config"]["access_key_id"] == "AKIA"


def test_import_mounts_skip_existing_records_info(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = {"mount_point": "/personal/alice"}  # already there
    errors = import_mounts(
        mounts=[redacted_record],
        overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "w"}},
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.SKIP,
    )
    assert mgr.save_mount.call_count == 0
    assert len(errors) == 1
    assert "alice" in errors[0].message


def test_import_mounts_overwrite_calls_update(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = {"mount_point": "/personal/alice"}
    import_mounts(
        mounts=[redacted_record],
        overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "w"}},
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.OVERWRITE,
    )
    mgr.update_mount.assert_called_once()
    assert mgr.save_mount.call_count == 0


def test_import_mounts_zone_remap_applied(redacted_record):
    mgr = MagicMock()
    mgr.get_mount.return_value = None
    import_mounts(
        mounts=[redacted_record],
        overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "w"}},
        mount_manager=mgr,
        target_zone_id="new-zone",
        conflict_mode=ConflictMode.SKIP,
    )
    assert mgr.save_mount.call_args.kwargs["zone_id"] == "new-zone"


def test_import_mounts_orders_by_path_depth(redacted_record):
    """Parents must restore before children to avoid ordering bugs."""
    mgr = MagicMock()
    mgr.get_mount.return_value = None
    deep = MountRecord(
        mount_id="m-deep", mount_point="/personal/alice/sub", backend_type="path_local",
        backend_config={"root": "/x"},
    )
    shallow = MountRecord(
        mount_id="m-shallow", mount_point="/personal", backend_type="path_local",
        backend_config={"root": "/y"},
    )
    import_mounts(
        mounts=[deep, shallow],
        overrides={},
        mount_manager=mgr,
        target_zone_id=None,
        conflict_mode=ConflictMode.SKIP,
    )
    saved_paths = [c.kwargs["mount_point"] for c in mgr.save_mount.call_args_list]
    assert saved_paths.index("/personal") < saved_paths.index("/personal/alice/sub")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_import.py -v
```

Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `mount_import.py`**

Create `src/nexus/bricks/portability/mount_import.py`:

```python
"""Mount-config import for .nexus bundles (Issue #4083).

Reads mounts.jsonl, validates that all redacted fields have overrides supplied,
re-injects values, and restores via MountManager. Validation runs *before* any
backend init or persistence — operators see every credential gap in one error.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.bricks.portability.models import (
    ConflictMode,
    ImportError,
    MissingCredentialsError,
    MountRecord,
)

if TYPE_CHECKING:
    from nexus.bricks.mount.mount_manager import MountManager


_PLACEHOLDER_RE = re.compile(r"^\$\{MOUNT_(?P<id>[^_]+(?:_[^_]+)*)_(?P<field>[A-Z0-9_]+)\}$")
"""Matches ${MOUNT_<id>_<FIELD>} where <id> may contain underscores and <field>
is the upper-cased CONNECTION_ARGS key."""


def read_mounts(bundle_dir: Path) -> list[MountRecord]:
    """Parse bundle_dir/mounts.jsonl. Returns [] if the file is absent (v1/v2 bundle)."""
    path = bundle_dir / "mounts.jsonl"
    if not path.exists():
        return []
    out: list[MountRecord] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(MountRecord.from_dict(json.loads(line)))
    return out


def _redacted_fields(record: MountRecord) -> list[str]:
    """Return the list of CONNECTION_ARGS keys whose value is a placeholder string."""
    return [
        name
        for name, value in record.backend_config.items()
        if isinstance(value, str) and _PLACEHOLDER_RE.match(value)
    ]


def validate_overrides(
    mounts: list[MountRecord],
    overrides: dict[str, dict[str, str]] | None,
) -> None:
    """Walk every redacted field; raise MissingCredentialsError listing all gaps.

    Pure function: no side effects. Runs before any backend init.
    """
    overrides = overrides or {}
    missing: dict[str, list[str]] = {}
    for record in mounts:
        provided = overrides.get(record.mount_id, {}) or {}
        gaps = [f for f in _redacted_fields(record) if f not in provided]
        if gaps:
            missing[record.mount_id] = sorted(gaps)
    if missing:
        raise MissingCredentialsError(missing=missing)


def materialize(
    mount_record: MountRecord,
    overrides_for_mount: dict[str, str],
) -> dict[str, Any]:
    """Substitute ${MOUNT_<id>_<FIELD>} placeholders. Returns the final backend_config."""
    out = dict(mount_record.backend_config)
    for field_name, value in list(out.items()):
        if isinstance(value, str) and _PLACEHOLDER_RE.match(value):
            if field_name in overrides_for_mount:
                out[field_name] = overrides_for_mount[field_name]
    return out


def import_mounts(
    mounts: list[MountRecord],
    overrides: dict[str, dict[str, str]],
    mount_manager: "MountManager",
    *,
    target_zone_id: str | None,
    conflict_mode: ConflictMode,
) -> list[ImportError]:
    """Per mount: materialize + save_mount/update_mount per conflict_mode.

    Mounts are sorted by mount_point depth (shallowest first) so parent paths
    restore before any nested children that may depend on them.

    Returns per-mount errors; does not raise except on programmer bugs.
    """
    errors: list[ImportError] = []
    overrides = overrides or {}
    sorted_mounts = sorted(mounts, key=lambda m: len(m.mount_point.split("/")))

    for record in sorted_mounts:
        mount_overrides = overrides.get(record.mount_id, {}) or {}
        backend_config = materialize(record, mount_overrides)
        zone_id = target_zone_id if target_zone_id is not None else record.zone_id

        existing = mount_manager.get_mount(record.mount_point)
        if existing is not None:
            if conflict_mode == ConflictMode.SKIP:
                errors.append(
                    ImportError(
                        message=f"mount {record.mount_point!r} already exists; skipped",
                        severity="info",
                    )
                )
                continue
            if conflict_mode == ConflictMode.FAIL:
                errors.append(
                    ImportError(
                        message=f"mount {record.mount_point!r} already exists",
                        severity="error",
                    )
                )
                continue
            if conflict_mode == ConflictMode.OVERWRITE:
                mount_manager.update_mount(
                    mount_point=record.mount_point,
                    backend_config=backend_config,
                    description=record.description,
                )
                continue

        try:
            mount_manager.save_mount(
                mount_point=record.mount_point,
                backend_type=record.backend_type,
                backend_config=backend_config,
                owner_user_id=record.owner_user_id,
                zone_id=zone_id,
                description=record.description,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                ImportError(
                    message=f"failed to restore mount {record.mount_point!r}: {exc}",
                    severity="error",
                )
            )

    return errors


__all__ = [
    "read_mounts",
    "validate_overrides",
    "materialize",
    "import_mounts",
]
```

**Note**: This task assumes `ImportError` (from `models.py`) accepts `message` and `severity` kwargs. If its current signature differs, adjust the calls — check `models.py:67` (or the existing `ImportError` class definition) before running tests.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_mount_import.py -v
```

Expected: 13 tests PASS. If `ImportError` signature mismatch, fix the calls in `mount_import.py` and re-run.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/portability/mount_import.py \
        src/nexus/bricks/portability/tests/test_mount_import.py
git commit -m "feat(portability): mount_import with validate-then-materialize-then-restore

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 9: Wire `mount_export` into `export_service.py`

**Files:**
- Modify: `src/nexus/bricks/portability/export_service.py:105` (`__init__`), at the end of the export flow (after `_apply_credential_stripping`, around line 234)

- [ ] **Step 1: Read the current `export_service.py:200-260` to confirm the splice point**

```bash
sed -n '200,260p' src/nexus/bricks/portability/export_service.py
```

Confirm the credential stripping block ends with `manifest.placeholders = list(placeholders)`. Splice point: immediately after that block.

- [ ] **Step 2: Write failing integration test**

Create `src/nexus/bricks/portability/tests/test_export_service_mounts.py`:

```python
"""Wiring tests for mount export integration."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.export_service import ZoneExportService
from nexus.bricks.portability.models import ZoneExportOptions


def test_include_mounts_true_without_mount_manager_raises(tmp_path):
    fs = MagicMock()
    service = ZoneExportService(fs)
    options = ZoneExportOptions(
        output_path=tmp_path / "x.nexus",
        include_mounts=True,
    )
    # Note: actual export call may need other args; this test verifies the
    # validation runs early. Adjust to match service signature once you read
    # the existing export entrypoint.
    with pytest.raises(ValueError, match="mount_manager"):
        # Synchronous if export_zone is async, wrap with anyio.run or asyncio.run
        # For this test, we expect the constructor or first arg validation
        # to trip — adjust as needed based on existing code shape.
        import asyncio
        asyncio.run(service.export_zone("z1", options))
```

(If `export_zone` is sync, drop the `asyncio.run` wrapper. Read `export_service.py:117` to determine signature.)

- [ ] **Step 3: Run test to verify it fails**

```bash
pytest src/nexus/bricks/portability/tests/test_export_service_mounts.py -v
```

Expected: FAIL — `ValueError` not raised (validation not yet wired).

- [ ] **Step 4: Modify `ZoneExportService.__init__` to accept optional `mount_manager`**

Edit `src/nexus/bricks/portability/export_service.py`. Find the `__init__` signature (around line 130). Add an optional kwarg:

```python
def __init__(
    self,
    nexus_fs: "PortabilityFSProtocol",
    *,
    mount_manager: "MountManager | None" = None,
) -> None:
    self._fs = nexus_fs
    self._mount_manager = mount_manager
```

Add a TYPE_CHECKING import near the top:

```python
if TYPE_CHECKING:
    from nexus.bricks.mount.mount_manager import MountManager
    from nexus.contracts.portability_types import PortabilityFSProtocol
```

- [ ] **Step 5: Add early validation in `export_zone`**

Find the start of the export flow inside `export_zone` (the method body around line 140). Add at the very top:

```python
if options.include_mounts and self._mount_manager is None:
    raise ValueError(
        "ZoneExportOptions.include_mounts=True requires "
        "ZoneExportService(mount_manager=...)"
    )
```

- [ ] **Step 6: Add the mount-export call after credential stripping**

After the existing `_apply_credential_stripping` block (around line 234, where `manifest.placeholders = list(placeholders)` is set), add:

```python
# --- Mount-config export (v3+, Issue #4083) ---
if options.include_mounts:
    from nexus.bricks.portability.mount_export import (
        collect_mounts,
        redact_and_write,
    )
    mounts_path = bundle_dir / "mounts.jsonl"
    raw_mounts = collect_mounts(self._mount_manager, zone_id=zone_id)
    mount_phs = redact_and_write(raw_mounts, out_path=mounts_path)
    manifest.placeholders = list(manifest.placeholders) + list(mount_phs)
    manifest.mount_count = len(raw_mounts)
    logger.info(
        "Mount export: %d mounts, %d placeholders", len(raw_mounts), len(mount_phs)
    )
```

- [ ] **Step 7: Run wiring test to verify it passes**

```bash
pytest src/nexus/bricks/portability/tests/test_export_service_mounts.py -v
```

Expected: PASS.

- [ ] **Step 8: Run the full portability suite to confirm no regressions**

```bash
pytest src/nexus/bricks/portability/tests/ -v
```

Expected: ALL pass.

- [ ] **Step 9: Commit**

```bash
git add src/nexus/bricks/portability/export_service.py \
        src/nexus/bricks/portability/tests/test_export_service_mounts.py
git commit -m "feat(portability): wire mount_export into ZoneExportService

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 10: Wire `mount_import` into `import_service.py`

**Files:**
- Modify: `src/nexus/bricks/portability/import_service.py` (`__init__` + `import_zone` early validation + restore section)

- [ ] **Step 1: Read the current import flow**

```bash
sed -n '1,80p' src/nexus/bricks/portability/import_service.py
grep -n "def __init__\|def import_zone\|class ZoneImportService" src/nexus/bricks/portability/import_service.py | head
```

Identify the `__init__` and main entrypoint methods.

- [ ] **Step 2: Write failing integration test**

Create `src/nexus/bricks/portability/tests/test_import_service_mounts.py`:

```python
"""Wiring tests for mount import integration."""

import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.import_service import ZoneImportService
from nexus.bricks.portability.models import (
    BUNDLE_FORMAT_VERSION,
    MissingCredentialsError,
    ZoneImportOptions,
)


def _build_bundle_with_mount(tmp_path: Path) -> Path:
    """Create a minimal v3 bundle containing mounts.jsonl."""
    bundle_dir = tmp_path / "src"
    bundle_dir.mkdir()
    manifest = {
        "$schema": "https://nexus.io/schemas/manifest-v3.json",
        "format_version": BUNDLE_FORMAT_VERSION,
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {
            "file_count": 0, "total_size_bytes": 0, "content_blob_count": 0,
            "permission_count": 0, "embedding_count": 0, "mount_count": 1,
        },
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    (bundle_dir / "mounts.jsonl").write_text(json.dumps({
        "mount_id": "m-1", "mount_point": "/x", "backend_type": "path_s3",
        "backend_config": {
            "bucket_name": "acme",
            "access_key_id": "${MOUNT_m-1_ACCESS_KEY_ID}",
            "secret_access_key": "${MOUNT_m-1_SECRET_ACCESS_KEY}",
        },
        "owner_user_id": "alice", "zone_id": "z1", "description": None,
    }) + "\n")

    out = tmp_path / "bundle.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for p in bundle_dir.rglob("*"):
            tar.add(p, arcname=p.relative_to(bundle_dir))
    return out


@pytest.mark.asyncio
async def test_import_without_overrides_raises_before_side_effects(tmp_path):
    bundle = _build_bundle_with_mount(tmp_path)
    fs = MagicMock()
    mgr = MagicMock()
    service = ZoneImportService(fs, mount_manager=mgr)

    options = ZoneImportOptions(bundle_path=bundle)
    with pytest.raises(MissingCredentialsError) as exc:
        await service.import_zone(options)
    assert "m-1" in exc.value.missing
    assert mgr.save_mount.call_count == 0
    assert mgr.update_mount.call_count == 0


@pytest.mark.asyncio
async def test_import_with_overrides_calls_save_mount(tmp_path):
    bundle = _build_bundle_with_mount(tmp_path)
    fs = MagicMock()
    mgr = MagicMock()
    mgr.get_mount.return_value = None
    service = ZoneImportService(fs, mount_manager=mgr)

    options = ZoneImportOptions(
        bundle_path=bundle,
        mount_overrides={"m-1": {"access_key_id": "AKIA", "secret_access_key": "wJalr"}},
    )
    await service.import_zone(options)
    assert mgr.save_mount.call_count == 1
    cfg = mgr.save_mount.call_args.kwargs["backend_config"]
    assert cfg["access_key_id"] == "AKIA"
    assert cfg["secret_access_key"] == "wJalr"


@pytest.mark.asyncio
async def test_import_restore_mounts_false_skips_mount_step(tmp_path):
    bundle = _build_bundle_with_mount(tmp_path)
    fs = MagicMock()
    mgr = MagicMock()
    service = ZoneImportService(fs, mount_manager=mgr)
    options = ZoneImportOptions(bundle_path=bundle, restore_mounts=False)
    await service.import_zone(options)
    assert mgr.save_mount.call_count == 0


@pytest.mark.asyncio
async def test_import_v2_bundle_no_mounts_jsonl_does_nothing(tmp_path):
    """v2 bundle (no mounts.jsonl) imports cleanly."""
    bundle_dir = tmp_path / "src"
    bundle_dir.mkdir()
    manifest = {
        "$schema": "https://nexus.io/schemas/manifest-v1.json",
        "format_version": "2.0.0",
        "bundle_id": "550e8400-e29b-41d4-a716-446655440000",
        "source_zone_id": "z1",
        "export_timestamp": "2026-01-01T00:00:00+00:00",
        "statistics": {"file_count": 0, "total_size_bytes": 0,
                       "content_blob_count": 0, "permission_count": 0,
                       "embedding_count": 0},
        "options": {"include_content": True, "include_permissions": True},
        "checksums": {"algorithm": "sha256", "files": {}},
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / "bundle.nexus"
    with tarfile.open(out, "w:gz") as tar:
        for p in bundle_dir.rglob("*"):
            tar.add(p, arcname=p.relative_to(bundle_dir))

    fs = MagicMock()
    mgr = MagicMock()
    service = ZoneImportService(fs, mount_manager=mgr)
    await service.import_zone(ZoneImportOptions(bundle_path=out))
    assert mgr.save_mount.call_count == 0
```

(If the existing import service is synchronous, drop `pytest.mark.asyncio` and the `await`s. Confirm by reading the entrypoint signature.)

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest src/nexus/bricks/portability/tests/test_import_service_mounts.py -v
```

Expected: FAIL — wiring not yet present.

- [ ] **Step 4: Modify `ZoneImportService.__init__` to accept optional `mount_manager`**

Edit `src/nexus/bricks/portability/import_service.py`. Add optional kwarg to the existing `__init__`:

```python
def __init__(
    self,
    nexus_fs: "PortabilityFSProtocol",
    *,
    mount_manager: "MountManager | None" = None,
) -> None:
    self._fs = nexus_fs
    self._mount_manager = mount_manager
```

Add the TYPE_CHECKING import:

```python
if TYPE_CHECKING:
    from nexus.bricks.mount.mount_manager import MountManager
    # ... existing imports ...
```

- [ ] **Step 5: Add validation + restore at the right points in `import_zone`**

Locate `import_zone`. After the bundle is extracted to a `bundle_dir` (search for the existing `tarfile.open(...).extractall` call), insert mount-validation **before any other restore step**:

```python
# --- Mount validation (Issue #4083) — BEFORE any side effect ---
if options.restore_mounts:
    from nexus.bricks.portability.mount_import import (
        read_mounts,
        validate_overrides,
    )
    _mount_records = read_mounts(bundle_dir)
    if _mount_records:
        validate_overrides(_mount_records, options.mount_overrides)
        if self._mount_manager is None:
            raise ValueError(
                "Bundle contains mounts.jsonl but no MountManager supplied. "
                "Pass ZoneImportService(mount_manager=...) or set restore_mounts=False."
            )
else:
    _mount_records = []
```

Then, after the existing files/permissions/embeddings restore (find the end of `import_zone`'s main flow, just before `return result`), add:

```python
# --- Mount restore (Issue #4083) ---
if _mount_records and options.restore_mounts:
    from nexus.bricks.portability.mount_import import import_mounts
    mount_errors = import_mounts(
        mounts=_mount_records,
        overrides=options.mount_overrides or {},
        mount_manager=self._mount_manager,
        target_zone_id=options.target_zone_id,
        conflict_mode=options.conflict_mode,
    )
    result.errors.extend(mount_errors)
```

- [ ] **Step 6: Run wiring tests to verify they pass**

```bash
pytest src/nexus/bricks/portability/tests/test_import_service_mounts.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 7: Run full portability suite**

```bash
pytest src/nexus/bricks/portability/tests/ -v
```

Expected: ALL pass.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/bricks/portability/import_service.py \
        src/nexus/bricks/portability/tests/test_import_service_mounts.py
git commit -m "feat(portability): wire mount_import into ZoneImportService

Validate overrides before any side effect; restore mounts after files/perms.

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 11: Public API exports in `__init__.py`

**Files:**
- Modify: `src/nexus/bricks/portability/__init__.py:38-112`

- [ ] **Step 1: Add new imports and `__all__` entries**

Edit `src/nexus/bricks/portability/__init__.py`. Add to the existing `from nexus.bricks.portability.models import (...)` block (line 51):

```python
    MissingCredentialsError,
    MountRecord,
    SensitiveFieldNotDeclaredError,
```

After the existing service imports, add:

```python
from nexus.bricks.portability.mount_export import (
    collect_mounts,
    redact_and_write,
)
from nexus.bricks.portability.mount_import import (
    import_mounts,
    materialize,
    read_mounts,
    validate_overrides,
)
from nexus.bricks.portability.redaction import (
    audit_backend,
    declared_secret_fields,
    redact_config,
)
```

Append to `__all__` (line 75):

```python
    # Mount portability (Issue #4083)
    "MountRecord",
    "MissingCredentialsError",
    "SensitiveFieldNotDeclaredError",
    "audit_backend",
    "declared_secret_fields",
    "redact_config",
    "collect_mounts",
    "redact_and_write",
    "read_mounts",
    "validate_overrides",
    "materialize",
    "import_mounts",
```

- [ ] **Step 2: Verify the import surface**

```bash
python -c "from nexus.bricks.portability import MountRecord, MissingCredentialsError, audit_backend, redact_config, read_mounts, validate_overrides, import_mounts; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Run the suite to verify nothing broke**

```bash
pytest src/nexus/bricks/portability/tests/ -v
```

Expected: ALL pass.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/portability/__init__.py
git commit -m "feat(portability): export mount-portability public API

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 12: End-to-end roundtrip integration test

**Files:**
- Test: `src/nexus/bricks/portability/tests/test_roundtrip_with_mounts.py`

- [ ] **Step 1: Write the integration test**

Create `src/nexus/bricks/portability/tests/test_roundtrip_with_mounts.py`:

```python
"""End-to-end roundtrip: export bundle with mounts → import → mounts restored.

Black-box test against real ZoneExportService + ZoneImportService. Uses
in-memory MountManager test doubles to avoid VFS plumbing for the kernel.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.bricks.portability.export_service import ZoneExportService
from nexus.bricks.portability.import_service import ZoneImportService
from nexus.bricks.portability.models import (
    MissingCredentialsError,
    ZoneExportOptions,
    ZoneImportOptions,
)


def _make_mount_manager_with(mounts: list[dict]) -> MagicMock:
    """Build a MountManager test double that returns `mounts` from list_mounts."""
    mgr = MagicMock()
    mgr.list_mounts.return_value = mounts
    mgr.get_mount.return_value = None  # no conflict
    return mgr


@pytest.mark.asyncio
async def test_roundtrip_export_then_import_with_overrides(tmp_path):
    src_mgr = _make_mount_manager_with([
        {
            "mount_id": "m-1", "mount_point": "/personal/alice",
            "backend_type": "path_s3",
            "backend_config": {
                "bucket_name": "acme",
                "access_key_id": "AKIA-LIVE-KEY",
                "secret_access_key": "wJalr-LIVE-SECRET",
            },
            "owner_user_id": "alice", "zone_id": "z1", "description": None,
        },
    ])

    fs = MagicMock()
    out = tmp_path / "bundle.nexus"
    exporter = ZoneExportService(fs, mount_manager=src_mgr)
    await exporter.export_zone(
        "z1",
        ZoneExportOptions(output_path=out, include_mounts=True),
    )
    assert out.exists()

    # Verify export stripped secrets — read raw mounts.jsonl back out of the tar.
    import json
    import tarfile
    with tarfile.open(out, "r:gz") as tar:
        member = tar.getmember("mounts.jsonl")
        f = tar.extractfile(member)
        assert f is not None
        record = json.loads(f.read().decode())
    assert record["backend_config"]["access_key_id"] == "${MOUNT_m-1_ACCESS_KEY_ID}"
    assert "AKIA-LIVE-KEY" not in f.read() if hasattr(f, 'read') else True

    # Import without overrides → MissingCredentialsError
    dst_mgr = MagicMock()
    dst_mgr.get_mount.return_value = None
    importer = ZoneImportService(fs, mount_manager=dst_mgr)
    with pytest.raises(MissingCredentialsError):
        await importer.import_zone(ZoneImportOptions(bundle_path=out))
    assert dst_mgr.save_mount.call_count == 0

    # Import with full overrides → save_mount called with concrete values
    await importer.import_zone(ZoneImportOptions(
        bundle_path=out,
        mount_overrides={"m-1": {
            "access_key_id": "AKIA-NEW-KEY",
            "secret_access_key": "wJalr-NEW-SECRET",
        }},
    ))
    assert dst_mgr.save_mount.call_count == 1
    saved_cfg = dst_mgr.save_mount.call_args.kwargs["backend_config"]
    assert saved_cfg["access_key_id"] == "AKIA-NEW-KEY"
    assert saved_cfg["secret_access_key"] == "wJalr-NEW-SECRET"
    assert saved_cfg["bucket_name"] == "acme"  # non-secret survives roundtrip
```

- [ ] **Step 2: Run the test**

```bash
pytest src/nexus/bricks/portability/tests/test_roundtrip_with_mounts.py -v
```

Expected: PASS. If it fails, the failure pinpoints which leg of the roundtrip is broken — fix and re-run.

- [ ] **Step 3: Run the full suite once more**

```bash
pytest src/nexus/bricks/portability/tests/ -v
```

Expected: ALL pass.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/portability/tests/test_roundtrip_with_mounts.py
git commit -m "test(portability): roundtrip integration with mount export+import

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Task 13: Documentation page

**Files:**
- Modify or Create: `docs/portability.md` (modify if exists, otherwise create at this path)

- [ ] **Step 1: Check whether `docs/portability.md` already exists**

```bash
ls docs/portability.md 2>/dev/null || echo "missing"
```

- [ ] **Step 2: Add or update the security/redaction section**

If creating the file fresh:

```markdown
# Zone Data Portability

Nexus zone bundles (`.nexus` archives) carry files, permissions, embeddings, and
— as of bundle format `3.0.0` — mount configurations. This document covers the
export/import flow, with particular attention to credential handling.

## Mount-config redaction (Issue #4083)

Mount configurations contain backend credentials: S3 access keys, OAuth tokens,
KMS material, etc. Bundles **must never** contain live credential values, so
the export pipeline strips them and the import pipeline refuses to proceed
without explicit re-injection.

### What gets stripped

For each connector, the redaction set is derived from `CONNECTION_ARGS`: every
argument whose `secret=True` flag is set has its value replaced by a
`${MOUNT_<id>_<FIELD>}` placeholder. This is the **only** source of truth — no
parallel list to maintain.

### Heuristic guard

On export, a heuristic regex (`(?i)(key|secret|token|password|cred)`) scans
every `CONNECTION_ARGS` key. If any matching name is **not** marked
`secret=True`, the export aborts with `SensitiveFieldNotDeclaredError`. This
prevents a forgotten flag from silently leaking credentials.

### Forced re-injection on import

Imports require an `overrides` dict per mount:

\```python
from nexus.bricks.portability import import_zone_bundle, ZoneImportOptions

await import_zone_bundle(ZoneImportOptions(
    bundle_path="zone.nexus",
    mount_overrides={
        "m-1": {
            "access_key_id": "AKIA...",
            "secret_access_key": "wJalr...",
        },
    },
))
\```

Without `mount_overrides` for a redacted field, the import raises
`MissingCredentialsError` listing **every** missing credential in one message
— operators see the full set up front, before any backend is initialized.

### Skipping mount restore

For dry-run inspections or destination instances that don't need the mounts:

\```python
await import_zone_bundle(ZoneImportOptions(
    bundle_path="zone.nexus",
    restore_mounts=False,
))
\```

### Bundle format compatibility

- v1/v2 bundles (no `mounts.jsonl`) import unchanged on the new code path.
- v3 bundles read by older Nexus instances fail loudly at schema validation
  — `additionalProperties: false` in `manifest-v1.json` rejects the new
  `mount_count` field. The failure is explicit, not silent corruption.
```

If the file already exists, append the "Mount-config redaction (Issue #4083)" section after the existing intro. Adjust formatting to match existing house style.

- [ ] **Step 3: Verify Markdown renders**

```bash
# If mkdocs is configured:
mkdocs build --strict 2>&1 | head -20
```

- [ ] **Step 4: Commit**

```bash
git add docs/portability.md
git commit -m "docs(portability): document redaction contract and override flow

Issue: https://github.com/nexi-lab/nexus/issues/4083"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Implementing task |
|---|---|
| `redaction.py` contract (declared_secret_fields, audit_backend, redact_config) | Task 4 |
| `mount_export.py` contract | Task 7 |
| `mount_import.py` contract | Task 8 |
| Errors (`SensitiveFieldNotDeclaredError`, `MissingCredentialsError`) | Task 1 |
| `MountRecord` dataclass | Task 1 |
| Bundle format version bump 2.0.0 → 3.0.0 | Task 2 |
| `manifest-v3.json` schema | Task 3 |
| CI audit test over `CONNECTOR_MANIFEST` | Task 5 |
| `ZoneExportOptions.include_mounts` | Task 6 |
| `ZoneImportOptions.mount_overrides` + `restore_mounts` | Task 6 |
| Wire export pipeline | Task 9 |
| Wire import pipeline | Task 10 |
| Public API exports | Task 11 |
| Conflict semantics (SKIP/OVERWRITE/FAIL) | Task 8 |
| Zone remap on import | Task 8 (test_import_mounts_zone_remap_applied) |
| Mount restore order (parents first) | Task 8 (test_import_mounts_orders_by_path_depth) |
| Roundtrip integration | Task 12 |
| Doc page | Task 13 |

All spec requirements have an implementing task.

**Type-consistency check:**

- `MountRecord` field names (`mount_id`, `mount_point`, `backend_type`, `backend_config`, `owner_user_id`, `zone_id`, `description`) consistent across Tasks 1, 7, 8, 12.
- Placeholder format `${MOUNT_<id>_<FIELD_UPPER>}` consistent across redaction (T4), import (T8 regex `_PLACEHOLDER_RE`), tests (T7, T8, T12).
- `redact_config` returns `(dict, list[PlaceholderRef])` — used as such in T7's `redact_and_write`.
- `validate_overrides(mounts, overrides)` signature consistent across T8 implementation and T10 wiring.

**Placeholder scan:** No "TBD"/"TODO"/"add appropriate" found. Each step has runnable code or a concrete command.

**Notes for the implementer:**

- Task 8 includes a guard note: confirm the existing `ImportError` constructor signature in `models.py` before running tests; adjust `mount_import.py` calls if `severity` isn't a kwarg.
- Tasks 9 and 10 reference line numbers from the current source — use `grep` first to verify the splice point hasn't shifted under you.
- The wiring tests in Tasks 9, 10, 12 assume `export_zone`/`import_zone` are async; flip to sync if the existing service is synchronous (drop `@pytest.mark.asyncio` and `await`).
