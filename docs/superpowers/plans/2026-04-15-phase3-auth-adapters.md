# Phase 3: Auth Sync Adapters + gws Connector Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add gcloud/gh/gws/codex sync adapters, migrate gws_* connectors to resolve auth through the unified profile store, and delete legacy gws-CLI probe workarounds.

**Architecture:** Four thin adapter classes (~30-60 LOC each) inherit from Phase 2 bases (FileAdapter/SubprocessAdapter). PathCLIBackend gains an AUTH_SOURCE class attribute and a two-phase token resolution that checks ExternalCliBackend before falling back to TokenManager. The gws-CLI probe in unified_service.py is replaced by profile-store queries.

**Tech Stack:** Python 3.11+, pytest, asyncio, configparser, json, yaml, subprocess

**Spec:** `docs/superpowers/specs/2026-04-15-phase3-auth-adapters-design.md`

---

## File Map

### New files
| File | Responsibility |
|------|---------------|
| `src/nexus/bricks/auth/external_sync/gcloud_sync.py` | GcloudSyncAdapter — FileAdapter for ADC + properties |
| `src/nexus/bricks/auth/external_sync/gh_sync.py` | GhCliSyncAdapter — dual-mode subprocess + file fallback |
| `src/nexus/bricks/auth/external_sync/gws_sync.py` | GwsCliSyncAdapter — SubprocessAdapter for gws CLI |
| `src/nexus/bricks/auth/external_sync/codex_sync.py` | CodexSyncAdapter — FileAdapter for ~/.codex/ |
| `src/nexus/bricks/auth/tests/test_gcloud_sync.py` | Tests for GcloudSyncAdapter |
| `src/nexus/bricks/auth/tests/test_gh_sync.py` | Tests for GhCliSyncAdapter |
| `src/nexus/bricks/auth/tests/test_gws_sync.py` | Tests for GwsCliSyncAdapter |
| `src/nexus/bricks/auth/tests/test_codex_sync.py` | Tests for CodexSyncAdapter |
| `src/nexus/bricks/auth/tests/test_connector_auth_migration.py` | Connector integration + concurrency + regression |
| 11 fixture files in `src/nexus/bricks/auth/tests/fixtures/external_cli_output/` | CLI output samples |

### Modified files
| File | Change |
|------|--------|
| `src/nexus/bricks/auth/external_sync/base.py` | Add `resolve_credential_sync()` to ABC |
| `src/nexus/bricks/auth/external_sync/external_cli_backend.py` | Add `resolve_sync()` |
| `src/nexus/bricks/auth/external_sync/aws_sync.py` | Add `resolve_credential_sync()` (mirrors async version) |
| `src/nexus/bricks/auth/external_sync/__init__.py` | Export new adapters |
| `src/nexus/backends/connectors/cli/base.py` | AUTH_SOURCE, credential_pool_registry, two-phase _get_user_token |
| `src/nexus/backends/connectors/gws/connector.py` | Add AUTH_SOURCE = "gws-cli" to 6 connectors |
| `src/nexus/bricks/auth/unified_service.py` | Delete probe methods, update auth-list loop |

---

## Task 1: Test Fixtures

**Files:**
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_adc_v456.json`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_adc_service_account.json`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_properties_v456.ini`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_hosts_v2.40.yml`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_hosts_v2.50.yml`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_auth_status_v2.40.txt`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_auth_status_v2.50.txt`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gws_status_v1.json`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gws_status_v2.json`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/codex_credentials_v1.json`
- Create: `src/nexus/bricks/auth/tests/fixtures/external_cli_output/codex_credentials_empty.json`

- [ ] **Step 1: Create gcloud ADC fixture (authorized_user)**

```json
{
  "client_id": "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com",
  "client_secret": "d-FL95Q19q7MQmFpd7hHD0Ty",
  "refresh_token": "1//0dx4s-EXAMPLETOKEN_NOT_REAL",
  "type": "authorized_user"
}
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_adc_v456.json`.

- [ ] **Step 2: Create gcloud ADC fixture (service_account)**

```json
{
  "type": "service_account",
  "project_id": "my-project-456",
  "private_key_id": "key123abc",
  "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWe\n-----END RSA PRIVATE KEY-----\n",
  "client_email": "my-sa@my-project-456.iam.gserviceaccount.com",
  "client_id": "123456789012345678901",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token"
}
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_adc_service_account.json`.

- [ ] **Step 3: Create gcloud properties fixture**

```ini
[core]
account = user@example.com
project = my-project-456

[compute]
region = us-central1
zone = us-central1-a
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_properties_v456.ini`.

- [ ] **Step 4: Create gh hosts v2.40 fixture**

```yaml
github.com:
  oauth_token: gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx40
  user: testuser
  git_protocol: https
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_hosts_v2.40.yml`.

- [ ] **Step 5: Create gh hosts v2.50 fixture**

```yaml
github.com:
  users:
    testuser:
      oauth_token: gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx50
    workuser:
      oauth_token: gho_yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy50
  git_protocol: https
enterprise.corp.com:
  users:
    corpuser:
      oauth_token: gho_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz50
  git_protocol: https
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_hosts_v2.50.yml`.

- [ ] **Step 6: Create gh auth status v2.40 fixture**

```
github.com
  ✓ Logged in to github.com as testuser (oauth_token)
  ✓ Git operations for github.com configured to use https protocol.
  ✓ Token: gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx40
  ✓ Token scopes: gist, read:org, repo, workflow
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_auth_status_v2.40.txt`.

- [ ] **Step 7: Create gh auth status v2.50 fixture**

```
github.com
  ✓ Logged in to github.com account testuser (oauth_token)
  - Active account: true
  - Git operations for github.com configured to use https protocol.
  - Token: gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx50
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
enterprise.corp.com
  ✓ Logged in to enterprise.corp.com account corpuser (oauth_token)
  - Active account: true
  - Git operations for enterprise.corp.com configured to use https protocol.
  - Token: gho_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz50
  - Token scopes: 'repo', 'read:org'
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_auth_status_v2.50.txt`.

- [ ] **Step 8: Create gws status v1 fixture**

```json
{
  "accounts": [
    {
      "email": "user@example.com",
      "active": true,
      "scopes": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/drive"
      ]
    }
  ]
}
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gws_status_v1.json`.

- [ ] **Step 9: Create gws status v2 fixture**

```json
{
  "version": 2,
  "accounts": [
    {
      "email": "user@example.com",
      "active": true,
      "scopes": [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/calendar"
      ],
      "expires_at": "2026-04-15T12:00:00Z"
    },
    {
      "email": "admin@corp.com",
      "active": false,
      "scopes": [
        "https://www.googleapis.com/auth/gmail.readonly"
      ],
      "expires_at": "2026-04-14T08:00:00Z"
    }
  ]
}
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/gws_status_v2.json`.

- [ ] **Step 10: Create codex credentials v1 fixture**

```json
{
  "default": {
    "api_key": "sk-codex-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "endpoint": "https://api.codex.example.com/v1"
  },
  "staging": {
    "token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.staging-example",
    "endpoint": "https://staging.codex.example.com/v1"
  }
}
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/codex_credentials_v1.json`.

- [ ] **Step 11: Create codex credentials empty fixture**

```json
{}
```

Write to `src/nexus/bricks/auth/tests/fixtures/external_cli_output/codex_credentials_empty.json`.

- [ ] **Step 12: Commit fixtures**

```bash
git add src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_*.json \
        src/nexus/bricks/auth/tests/fixtures/external_cli_output/gcloud_*.ini \
        src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_*.yml \
        src/nexus/bricks/auth/tests/fixtures/external_cli_output/gh_*.txt \
        src/nexus/bricks/auth/tests/fixtures/external_cli_output/gws_*.json \
        src/nexus/bricks/auth/tests/fixtures/external_cli_output/codex_*.json
git commit -m "test: add CLI output fixtures for gcloud/gh/gws/codex adapters (#3740)"
```

---

## Task 2: resolve_credential_sync() Base Infrastructure

**Files:**
- Modify: `src/nexus/bricks/auth/external_sync/base.py:46-75`
- Modify: `src/nexus/bricks/auth/external_sync/external_cli_backend.py:21-69`
- Modify: `src/nexus/bricks/auth/external_sync/aws_sync.py:59-93`

- [ ] **Step 1: Write test for resolve_sync on ExternalCliBackend**

Create `src/nexus/bricks/auth/tests/test_resolve_sync.py`:

```python
"""Tests for synchronous credential resolution path."""

from __future__ import annotations

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError, ResolvedCredential
from nexus.bricks.auth.external_sync.base import ExternalCliSyncAdapter, SyncResult
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
from nexus.bricks.auth.external_sync.registry import AdapterRegistry
from nexus.bricks.auth.profile import InMemoryAuthProfileStore


class _StubAdapter(ExternalCliSyncAdapter):
    adapter_name = "stub"

    async def sync(self) -> SyncResult:
        return SyncResult(adapter_name="stub")

    async def detect(self) -> bool:
        return True

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="bearer_token", access_token="async-tok")

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return ResolvedCredential(kind="bearer_token", access_token="sync-tok")


class TestResolveSyncBackend:
    def test_resolve_sync_delegates_to_adapter(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry([_StubAdapter()], store)
        backend = ExternalCliBackend(registry)

        cred = backend.resolve_sync("stub/my-account")

        assert cred.kind == "bearer_token"
        assert cred.access_token == "sync-tok"

    def test_resolve_sync_unknown_adapter_raises(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry([], store)
        backend = ExternalCliBackend(registry)

        with pytest.raises(CredentialResolutionError, match="no adapter"):
            backend.resolve_sync("unknown/account")

    def test_resolve_sync_bad_key_format_raises(self) -> None:
        store = InMemoryAuthProfileStore()
        registry = AdapterRegistry([_StubAdapter()], store)
        backend = ExternalCliBackend(registry)

        with pytest.raises(CredentialResolutionError, match="expected"):
            backend.resolve_sync("no-slash")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_resolve_sync.py -v`
Expected: FAIL — `resolve_credential_sync` not in ABC, `resolve_sync` not on ExternalCliBackend.

- [ ] **Step 3: Add resolve_credential_sync to ExternalCliSyncAdapter ABC**

In `src/nexus/bricks/auth/external_sync/base.py`, add after the existing `resolve_credential` method (line 74):

```python
    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        """Synchronous variant of resolve_credential().

        Default: raises NotImplementedError. Adapters that support sync
        resolution (FileAdapter subclasses, SubprocessAdapter with
        subprocess.run) override this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement resolve_credential_sync"
        )
```

Also add `ResolvedCredential` to the imports (move from TYPE_CHECKING to runtime):

Replace the existing import block:
```python
if TYPE_CHECKING:
    from nexus.bricks.auth.credential_backend import ResolvedCredential
```

With:
```python
from nexus.bricks.auth.credential_backend import ResolvedCredential
```

- [ ] **Step 4: Add resolve_sync to ExternalCliBackend**

In `src/nexus/bricks/auth/external_sync/external_cli_backend.py`, add after the existing `resolve` method (after line 41):

```python
    def resolve_sync(self, backend_key: str) -> ResolvedCredential:
        """Synchronous variant of resolve() for sync calling contexts."""
        adapter_name, _ = self._parse_key(backend_key)
        adapter = self._registry.get_adapter(adapter_name)
        if adapter is None:
            raise CredentialResolutionError(
                self._NAME, backend_key, f"no adapter registered for '{adapter_name}'"
            )
        return adapter.resolve_credential_sync(backend_key)
```

- [ ] **Step 5: Add resolve_credential_sync to AwsCliSyncAdapter**

In `src/nexus/bricks/auth/external_sync/aws_sync.py`, add after the existing `resolve_credential` method (after line 93):

```python
    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        """Synchronous resolve — re-reads config files (same logic as async)."""
        _, profile_name = backend_key.split("/", 1)

        for path in self.paths():
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue

            parser = configparser.ConfigParser()
            parser.read_string(content)

            for section in [profile_name, f"profile {profile_name}"]:
                if parser.has_section(section) and parser.has_option(section, "aws_access_key_id"):
                    return ResolvedCredential(
                        kind="api_key",
                        api_key=parser.get(section, "aws_access_key_id"),
                        metadata={
                            "secret_access_key": parser.get(
                                section, "aws_secret_access_key", fallback=""
                            ),
                            "session_token": parser.get(section, "aws_session_token", fallback=""),
                            "region": parser.get(section, "region", fallback=""),
                        },
                    )

        from nexus.bricks.auth.credential_backend import CredentialResolutionError

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"AWS profile '{profile_name}' not found in config files",
        )
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_resolve_sync.py -v`
Expected: 3 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/base.py \
        src/nexus/bricks/auth/external_sync/external_cli_backend.py \
        src/nexus/bricks/auth/external_sync/aws_sync.py \
        src/nexus/bricks/auth/tests/test_resolve_sync.py
git commit -m "feat(auth): add resolve_credential_sync() for sync calling contexts (#3740)"
```

---

## Task 3: GcloudSyncAdapter + Tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/gcloud_sync.py`
- Create: `src/nexus/bricks/auth/tests/test_gcloud_sync.py`

- [ ] **Step 1: Write failing tests**

Create `src/nexus/bricks/auth/tests/test_gcloud_sync.py`:

```python
"""Tests for GcloudSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError
from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_ADC_V456 = _FIXTURE_DIR / "gcloud_adc_v456.json"
_ADC_SA = _FIXTURE_DIR / "gcloud_adc_service_account.json"
_PROPS_V456 = _FIXTURE_DIR / "gcloud_properties_v456.ini"


@pytest.fixture()
def adapter() -> GcloudSyncAdapter:
    return GcloudSyncAdapter()


class TestGcloudParseAdc:
    """Test parse_file against gcloud ADC fixtures."""

    def test_parse_authorized_user_returns_one_profile(
        self, adapter: GcloudSyncAdapter
    ) -> None:
        content = _ADC_V456.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_ADC_V456, content)
        # authorized_user ADC has no account email — profile comes from properties file
        # ADC alone produces a profile with account_identifier="unknown"
        assert len(profiles) == 1
        assert profiles[0].provider == "gcs"
        assert profiles[0].source == "gcloud"

    def test_parse_service_account_extracts_email(
        self, adapter: GcloudSyncAdapter
    ) -> None:
        content = _ADC_SA.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_ADC_SA, content)

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "my-sa@my-project-456.iam.gserviceaccount.com"
        assert profiles[0].backend_key == "gcloud/my-sa@my-project-456.iam.gserviceaccount.com"
        assert profiles[0].provider == "gcs"

    def test_parse_empty_returns_empty(self, adapter: GcloudSyncAdapter) -> None:
        profiles = adapter.parse_file(Path("/dev/null"), "")
        assert profiles == []

    def test_parse_malformed_json_raises(self, adapter: GcloudSyncAdapter) -> None:
        with pytest.raises(Exception):
            adapter.parse_file(Path("bad.json"), "not json at all {{{")


class TestGcloudParseProperties:
    """Test parse_file against gcloud properties fixture."""

    def test_parse_properties_extracts_account(
        self, adapter: GcloudSyncAdapter
    ) -> None:
        content = _PROPS_V456.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_PROPS_V456, content)

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "user@example.com"
        assert profiles[0].backend_key == "gcloud/user@example.com"
        assert profiles[0].provider == "gcs"

    def test_parse_properties_no_account_returns_empty(
        self, adapter: GcloudSyncAdapter
    ) -> None:
        profiles = adapter.parse_file(Path("p.ini"), "[compute]\nregion = us-central1\n")
        assert profiles == []


class TestGcloudPaths:
    def test_defaults_to_home_gcloud(
        self, adapter: GcloudSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLOUDSDK_CONFIG", raising=False)
        paths = adapter.paths()
        assert len(paths) == 2
        assert "application_default_credentials.json" in str(paths[0])
        assert "properties" in str(paths[1])

    def test_cloudsdk_config_override(
        self, adapter: GcloudSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", "/custom/gcloud")
        paths = adapter.paths()
        assert paths[0] == Path("/custom/gcloud/application_default_credentials.json")
        assert paths[1] == Path("/custom/gcloud/properties")


class TestGcloudSync:
    async def test_sync_discovers_service_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        props = tmp_path / "properties"
        shutil.copy(_ADC_SA, adc)
        shutil.copy(_PROPS_V456, props)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        result = await adapter.sync()

        assert result.error is None
        assert len(result.profiles) >= 1
        emails = {p.account_identifier for p in result.profiles}
        assert "my-sa@my-project-456.iam.gserviceaccount.com" in emails

    async def test_sync_deduplicates_by_backend_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        props = tmp_path / "properties"
        shutil.copy(_ADC_SA, adc)
        shutil.copy(_PROPS_V456, props)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        result = await adapter.sync()

        keys = [p.backend_key for p in result.profiles]
        assert len(keys) == len(set(keys)), "Duplicate backend_key found"

    async def test_sync_missing_files_returns_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nonexistent"))
        adapter = GcloudSyncAdapter()
        result = await adapter.sync()
        assert result.error is not None
        assert result.profiles == []

    async def test_detect_true_when_adc_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_V456, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))
        adapter = GcloudSyncAdapter()
        assert await adapter.detect() is True

    async def test_detect_false_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nope"))
        adapter = GcloudSyncAdapter()
        assert await adapter.detect() is False


class TestGcloudResolveCredential:
    async def test_resolve_service_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_SA, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        cred = await adapter.resolve_credential(
            "gcloud/my-sa@my-project-456.iam.gserviceaccount.com"
        )
        assert cred.kind == "api_key"
        assert "BEGIN RSA PRIVATE KEY" in (cred.api_key or "")
        assert cred.metadata["client_email"] == "my-sa@my-project-456.iam.gserviceaccount.com"

    async def test_resolve_authorized_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_V456, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        cred = await adapter.resolve_credential("gcloud/user@example.com")
        assert cred.kind == "bearer_token"
        assert cred.access_token is None  # ADC has no access_token for authorized_user
        assert "refresh_token" in cred.metadata

    async def test_resolve_missing_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nope"))
        adapter = GcloudSyncAdapter()
        with pytest.raises(CredentialResolutionError):
            await adapter.resolve_credential("gcloud/nobody@example.com")

    def test_resolve_sync_service_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adc = tmp_path / "application_default_credentials.json"
        shutil.copy(_ADC_SA, adc)
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path))

        adapter = GcloudSyncAdapter()
        cred = adapter.resolve_credential_sync(
            "gcloud/my-sa@my-project-456.iam.gserviceaccount.com"
        )
        assert cred.kind == "api_key"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_gcloud_sync.py -v`
Expected: FAIL — `gcloud_sync` module does not exist.

- [ ] **Step 3: Implement GcloudSyncAdapter**

Create `src/nexus/bricks/auth/external_sync/gcloud_sync.py`:

```python
"""Gcloud sync adapter — discovers credentials from ADC + properties files.

FileAdapter subclass. Reads ~/.config/gcloud/application_default_credentials.json
and ~/.config/gcloud/properties. Does NOT hit the gcloud metadata server —
the ADC file is the source of truth (offline-safe).
"""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter


class GcloudSyncAdapter(FileAdapter):
    """Discovers gcloud credentials from ADC + active config."""

    adapter_name = "gcloud"

    def _config_dir(self) -> Path:
        return Path(
            os.environ.get("CLOUDSDK_CONFIG", "~/.config/gcloud")
        ).expanduser()

    def paths(self) -> list[Path]:
        base = self._config_dir()
        return [
            base / "application_default_credentials.json",
            base / "properties",
        ]

    def parse_file(self, path: Path, content: str) -> list[SyncedProfile]:
        if not content.strip():
            return []

        name = path.name
        if name == "application_default_credentials.json":
            return self._parse_adc(content)
        if name == "properties":
            return self._parse_properties(content)
        return []

    def _parse_adc(self, content: str) -> list[SyncedProfile]:
        data = json.loads(content)
        cred_type = data.get("type", "")

        if cred_type == "service_account":
            email = data.get("client_email", "")
            if not email:
                return []
            return [
                SyncedProfile(
                    provider="gcs",
                    account_identifier=email,
                    backend_key=f"gcloud/{email}",
                    source="gcloud",
                )
            ]

        if cred_type == "authorized_user":
            # authorized_user ADC doesn't contain account email.
            # Use "unknown" — the properties file provides the real account.
            return [
                SyncedProfile(
                    provider="gcs",
                    account_identifier="unknown",
                    backend_key="gcloud/unknown",
                    source="gcloud",
                )
            ]

        return []

    def _parse_properties(self, content: str) -> list[SyncedProfile]:
        parser = configparser.ConfigParser()
        parser.read_string(content)

        if not parser.has_option("core", "account"):
            return []

        account = parser.get("core", "account").strip()
        if not account:
            return []

        return [
            SyncedProfile(
                provider="gcs",
                account_identifier=account,
                backend_key=f"gcloud/{account}",
                source="gcloud",
            )
        ]

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Shared resolve logic — re-read ADC JSON and extract credential."""
        adc_path = self._config_dir() / "application_default_credentials.json"

        try:
            content = adc_path.read_text(encoding="utf-8")
            data = json.loads(content)
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, f"Cannot read ADC: {exc}"
            ) from exc

        cred_type = data.get("type", "")

        if cred_type == "service_account":
            return ResolvedCredential(
                kind="api_key",
                api_key=data.get("private_key", ""),
                metadata={"client_email": data.get("client_email", "")},
            )

        if cred_type == "authorized_user":
            return ResolvedCredential(
                kind="bearer_token",
                access_token=None,
                metadata={
                    "client_id": data.get("client_id", ""),
                    "client_secret": data.get("client_secret", ""),
                    "refresh_token": data.get("refresh_token", ""),
                },
            )

        raise CredentialResolutionError(
            "external-cli", backend_key, f"Unknown ADC type: {cred_type!r}"
        )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_gcloud_sync.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/gcloud_sync.py \
        src/nexus/bricks/auth/tests/test_gcloud_sync.py
git commit -m "feat(auth): add GcloudSyncAdapter — FileAdapter for ADC + properties (#3740)"
```

---

## Task 4: GhCliSyncAdapter + Tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/gh_sync.py`
- Create: `src/nexus/bricks/auth/tests/test_gh_sync.py`

- [ ] **Step 1: Write failing tests**

Create `src/nexus/bricks/auth/tests/test_gh_sync.py`:

```python
"""Tests for GhCliSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError
from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_HOSTS_V240 = _FIXTURE_DIR / "gh_hosts_v2.40.yml"
_HOSTS_V250 = _FIXTURE_DIR / "gh_hosts_v2.50.yml"
_STATUS_V240 = _FIXTURE_DIR / "gh_auth_status_v2.40.txt"
_STATUS_V250 = _FIXTURE_DIR / "gh_auth_status_v2.50.txt"


@pytest.fixture()
def adapter() -> GhCliSyncAdapter:
    return GhCliSyncAdapter()


class TestGhParseHosts:
    """Test _parse_hosts_file against both hosts.yml formats."""

    def test_parse_v240_flat_format(self, adapter: GhCliSyncAdapter) -> None:
        content = _HOSTS_V240.read_text(encoding="utf-8")
        profiles = adapter.parse_hosts_file(content)

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "testuser"
        assert profiles[0].backend_key == "gh-cli/github.com/testuser"
        assert profiles[0].provider == "github"
        assert profiles[0].source == "gh-cli"

    def test_parse_v250_nested_format(self, adapter: GhCliSyncAdapter) -> None:
        content = _HOSTS_V250.read_text(encoding="utf-8")
        profiles = adapter.parse_hosts_file(content)

        names = {p.account_identifier for p in profiles}
        assert "testuser" in names
        assert "workuser" in names
        assert "corpuser" in names
        assert len(profiles) == 3

    def test_parse_v250_enterprise_host(self, adapter: GhCliSyncAdapter) -> None:
        content = _HOSTS_V250.read_text(encoding="utf-8")
        profiles = adapter.parse_hosts_file(content)

        corp = [p for p in profiles if p.account_identifier == "corpuser"]
        assert len(corp) == 1
        assert corp[0].backend_key == "gh-cli/enterprise.corp.com/corpuser"

    def test_parse_empty_returns_empty(self, adapter: GhCliSyncAdapter) -> None:
        profiles = adapter.parse_hosts_file("")
        assert profiles == []


class TestGhParseAuthStatus:
    """Test _parse_status_output against gh auth status --show-token output."""

    def test_parse_v240_single_host(self, adapter: GhCliSyncAdapter) -> None:
        content = _STATUS_V240.read_text(encoding="utf-8")
        profiles = adapter.parse_status_output(content)

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "testuser"
        assert profiles[0].backend_key == "gh-cli/github.com/testuser"

    def test_parse_v250_multiple_hosts(self, adapter: GhCliSyncAdapter) -> None:
        content = _STATUS_V250.read_text(encoding="utf-8")
        profiles = adapter.parse_status_output(content)

        assert len(profiles) == 2
        names = {p.account_identifier for p in profiles}
        assert "testuser" in names
        assert "corpuser" in names


class TestGhPaths:
    def test_default_config_dir(
        self, adapter: GhCliSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
        config_dir = adapter._config_dir()
        assert str(config_dir).endswith(".config/gh")

    def test_gh_config_dir_override(
        self, adapter: GhCliSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_CONFIG_DIR", "/custom/gh")
        assert adapter._config_dir() == Path("/custom/gh")


class TestGhSync:
    async def test_sync_file_fallback_discovers_profiles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When gh binary is missing, falls back to hosts.yml."""
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V250, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            result = await adapter.sync()

        assert result.error is None
        assert len(result.profiles) == 3

    async def test_sync_missing_binary_and_file_returns_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "nope"))
        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            result = await adapter.sync()
        assert result.error is not None
        assert result.profiles == []

    async def test_detect_true_with_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("shutil.which", return_value="/usr/bin/gh"):
            adapter = GhCliSyncAdapter()
            assert await adapter.detect() is True

    async def test_detect_true_with_hosts_file_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V240, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            assert await adapter.detect() is True

    async def test_detect_false_nothing_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "nope"))
        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            assert await adapter.detect() is False


class TestGhResolveCredential:
    async def test_resolve_from_hosts_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V250, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            cred = await adapter.resolve_credential("gh-cli/github.com/testuser")

        assert cred.kind == "bearer_token"
        assert cred.access_token == "gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx50"

    async def test_resolve_missing_user_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V250, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            with pytest.raises(CredentialResolutionError):
                await adapter.resolve_credential("gh-cli/github.com/nobody")

    def test_resolve_sync_from_hosts_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        shutil.copy(_HOSTS_V240, config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            cred = adapter.resolve_credential_sync("gh-cli/github.com/testuser")

        assert cred.access_token == "gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx40"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_gh_sync.py -v`
Expected: FAIL — `gh_sync` module does not exist.

- [ ] **Step 3: Implement GhCliSyncAdapter**

Create `src/nexus/bricks/auth/external_sync/gh_sync.py`:

```python
"""GitHub CLI sync adapter — dual-mode subprocess + file fallback.

Composes both SubprocessAdapter and FileAdapter strategies internally.
Primary: ``gh auth status --show-token``. Fallback: parse
``~/.config/gh/hosts.yml`` when the binary isn't on PATH.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import yaml

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import (
    ExternalCliSyncAdapter,
    SyncedProfile,
    SyncResult,
)

logger = logging.getLogger(__name__)


class GhCliSyncAdapter(ExternalCliSyncAdapter):
    """Discovers GitHub CLI credentials via subprocess or hosts.yml fallback."""

    adapter_name = "gh-cli"
    sync_ttl_seconds: float = 300.0  # subprocess = expensive

    def _config_dir(self) -> Path:
        return Path(
            os.environ.get("GH_CONFIG_DIR", "~/.config/gh")
        ).expanduser()

    def _hosts_path(self) -> Path:
        return self._config_dir() / "hosts.yml"

    def _has_binary(self) -> bool:
        return shutil.which("gh") is not None

    async def detect(self) -> bool:
        if self._has_binary():
            return True
        try:
            p = self._hosts_path()
            return p.exists() and p.is_file()
        except OSError:
            return False

    async def sync(self) -> SyncResult:
        if self._has_binary():
            return await self._sync_subprocess()
        return self._sync_file()

    async def _sync_subprocess(self) -> SyncResult:
        """Run gh auth status --show-token and parse output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "auth", "status", "--show-token",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=5.0,
            )
        except TimeoutError:
            return SyncResult(adapter_name=self.adapter_name, error="gh: timeout after 5s")
        except FileNotFoundError:
            return SyncResult(adapter_name=self.adapter_name, error="gh: binary not found")

        # gh auth status prints to stderr in older versions, stdout in newer
        output = stdout_bytes.decode("utf-8", errors="replace")
        if not output.strip():
            output = stderr_bytes.decode("utf-8", errors="replace")

        if not output.strip():
            return SyncResult(
                adapter_name=self.adapter_name,
                error="gh auth status returned empty output",
            )

        try:
            profiles = self.parse_status_output(output)
        except Exception as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gh: parse error: {exc}")

        return SyncResult(adapter_name=self.adapter_name, profiles=profiles)

    def _sync_file(self) -> SyncResult:
        """Parse hosts.yml as fallback when gh binary is not available."""
        hosts_path = self._hosts_path()
        try:
            content = hosts_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"gh: {hosts_path} not found and binary not on PATH",
            )
        except OSError as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gh: {exc}")

        if not content.strip():
            return SyncResult(adapter_name=self.adapter_name, error="gh: hosts.yml is empty")

        try:
            profiles = self.parse_hosts_file(content)
        except Exception as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gh: parse error: {exc}")

        return SyncResult(adapter_name=self.adapter_name, profiles=profiles)

    def parse_hosts_file(self, content: str) -> list[SyncedProfile]:
        """Parse ~/.config/gh/hosts.yml into profiles.

        Supports v2.40 (flat) and v2.50 (nested users) formats.
        """
        if not content.strip():
            return []

        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return []

        profiles: list[SyncedProfile] = []
        for host, host_data in data.items():
            if not isinstance(host_data, dict):
                continue

            # v2.50: nested users dict
            users = host_data.get("users")
            if isinstance(users, dict):
                for username, user_data in users.items():
                    if isinstance(user_data, dict) and user_data.get("oauth_token"):
                        profiles.append(
                            SyncedProfile(
                                provider="github",
                                account_identifier=username,
                                backend_key=f"gh-cli/{host}/{username}",
                                source="gh-cli",
                            )
                        )
            # v2.40: flat oauth_token + user
            elif host_data.get("oauth_token") and host_data.get("user"):
                profiles.append(
                    SyncedProfile(
                        provider="github",
                        account_identifier=host_data["user"],
                        backend_key=f"gh-cli/{host}/{host_data['user']}",
                        source="gh-cli",
                    )
                )

        return profiles

    def parse_status_output(self, output: str) -> list[SyncedProfile]:
        """Parse ``gh auth status --show-token`` text output."""
        profiles: list[SyncedProfile] = []
        current_host: str | None = None

        for line in output.splitlines():
            stripped = line.strip()
            # Host line: no leading whitespace, ends with domain
            if not line.startswith(" ") and not line.startswith("\t") and stripped:
                current_host = stripped.rstrip(":")
                continue

            if current_host is None:
                continue

            # Match: "Logged in to <host> as <user>" or "account <user>"
            m = re.search(r"Logged in to \S+ (?:as|account) (\S+)", stripped)
            if m:
                username = m.group(1).strip("()")
                profiles.append(
                    SyncedProfile(
                        provider="github",
                        account_identifier=username,
                        backend_key=f"gh-cli/{current_host}/{username}",
                        source="gh-cli",
                    )
                )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Resolve token from hosts.yml (sync-safe file read)."""
        parts = backend_key.split("/", 2)
        if len(parts) < 3:
            raise CredentialResolutionError(
                "external-cli", backend_key,
                f"expected 'gh-cli/host/user', got {backend_key!r}",
            )
        _, host, username = parts

        hosts_path = self._hosts_path()
        try:
            content = hosts_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
        except Exception as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, f"Cannot read hosts.yml: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise CredentialResolutionError(
                "external-cli", backend_key, "hosts.yml is not a valid YAML mapping"
            )

        host_data = data.get(host)
        if not isinstance(host_data, dict):
            raise CredentialResolutionError(
                "external-cli", backend_key, f"Host '{host}' not found in hosts.yml"
            )

        # v2.50 nested
        users = host_data.get("users")
        if isinstance(users, dict):
            user_data = users.get(username)
            if isinstance(user_data, dict) and user_data.get("oauth_token"):
                return ResolvedCredential(
                    kind="bearer_token",
                    access_token=user_data["oauth_token"],
                )

        # v2.40 flat
        if host_data.get("user") == username and host_data.get("oauth_token"):
            return ResolvedCredential(
                kind="bearer_token",
                access_token=host_data["oauth_token"],
            )

        raise CredentialResolutionError(
            "external-cli", backend_key,
            f"User '{username}' not found for host '{host}' in hosts.yml",
        )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_gh_sync.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/gh_sync.py \
        src/nexus/bricks/auth/tests/test_gh_sync.py
git commit -m "feat(auth): add GhCliSyncAdapter — dual subprocess + file fallback (#3740)"
```

---

## Task 5: GwsCliSyncAdapter + Tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/gws_sync.py`
- Create: `src/nexus/bricks/auth/tests/test_gws_sync.py`

- [ ] **Step 1: Write failing tests**

Create `src/nexus/bricks/auth/tests/test_gws_sync.py`:

```python
"""Tests for GwsCliSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
from nexus.bricks.auth.profile import AuthProfileFailureReason

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_STATUS_V1 = _FIXTURE_DIR / "gws_status_v1.json"
_STATUS_V2 = _FIXTURE_DIR / "gws_status_v2.json"


@pytest.fixture()
def adapter() -> GwsCliSyncAdapter:
    return GwsCliSyncAdapter()


class TestGwsParseOutput:
    """Test parse_output against gws status JSON fixtures."""

    def test_parse_v1_single_account(self, adapter: GwsCliSyncAdapter) -> None:
        content = _STATUS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "user@example.com"
        assert profiles[0].backend_key == "gws-cli/user@example.com"
        assert profiles[0].provider == "google"
        assert profiles[0].source == "gws-cli"

    def test_parse_v2_multiple_accounts(self, adapter: GwsCliSyncAdapter) -> None:
        content = _STATUS_V2.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")

        assert len(profiles) == 2
        emails = {p.account_identifier for p in profiles}
        assert "user@example.com" in emails
        assert "admin@corp.com" in emails

    def test_parse_empty_returns_empty(self, adapter: GwsCliSyncAdapter) -> None:
        profiles = adapter.parse_output("{}", "")
        assert profiles == []

    def test_parse_malformed_raises(self, adapter: GwsCliSyncAdapter) -> None:
        with pytest.raises(Exception):
            adapter.parse_output("not json {{{", "")

    def test_backend_key_format(self, adapter: GwsCliSyncAdapter) -> None:
        content = _STATUS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")
        for p in profiles:
            assert p.backend_key.startswith("gws-cli/")


class TestGwsSync:
    async def test_sync_binary_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("shutil.which", return_value=None):
            adapter = GwsCliSyncAdapter()
            result = await adapter.sync()
        assert result.error is not None
        assert "not found" in result.error

    async def test_detect_true_with_binary(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/gws"):
            adapter = GwsCliSyncAdapter()
            assert await adapter.detect() is True

    async def test_detect_false_without_binary(self) -> None:
        with patch("shutil.which", return_value=None):
            adapter = GwsCliSyncAdapter()
            assert await adapter.detect() is False


class TestGwsFixHints:
    def test_fix_hints_defined(self) -> None:
        adapter = GwsCliSyncAdapter()
        hints = adapter.FIX_HINTS
        assert AuthProfileFailureReason.UPSTREAM_CLI_MISSING in hints
        assert AuthProfileFailureReason.AUTH_PERMANENT in hints
        assert AuthProfileFailureReason.SCOPE_INSUFFICIENT in hints

    def test_missing_binary_hint(self) -> None:
        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.UPSTREAM_CLI_MISSING]
        assert "gws" in hint.lower() or "install" in hint.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_gws_sync.py -v`
Expected: FAIL — `gws_sync` module does not exist.

- [ ] **Step 3: Implement GwsCliSyncAdapter**

Create `src/nexus/bricks/auth/external_sync/gws_sync.py`:

```python
"""GWS CLI sync adapter — discovers Google Workspace accounts via gws binary.

SubprocessAdapter subclass. Runs ``gws auth status --format=json`` to discover
connected accounts. Falls back to the legacy ``gws gmail users getProfile``
command if ``auth status`` is not available.
"""

from __future__ import annotations

import json
import subprocess

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.subprocess_adapter import SubprocessAdapter
from nexus.bricks.auth.profile import AuthProfileFailureReason


class GwsCliSyncAdapter(SubprocessAdapter):
    """Discovers Google Workspace CLI credentials via subprocess."""

    adapter_name = "gws-cli"
    binary_name = "gws"

    FIX_HINTS: dict[AuthProfileFailureReason, str] = {
        AuthProfileFailureReason.UPSTREAM_CLI_MISSING: (
            "Install the gws CLI and run: gws auth login"
        ),
        AuthProfileFailureReason.AUTH_PERMANENT: "Run: gws auth login",
        AuthProfileFailureReason.SCOPE_INSUFFICIENT: (
            "Run: gws auth login --scopes=<required_scopes>"
        ),
        AuthProfileFailureReason.SESSION_EXPIRED: "Run: gws auth login",
        AuthProfileFailureReason.TIMEOUT: "Check network connectivity to Google APIs",
        AuthProfileFailureReason.CLOCK_SKEW: "Sync system clock (e.g. ntpdate pool.ntp.org)",
    }

    def get_status_args(self) -> tuple[str, ...]:
        return ("auth", "status", "--format=json")

    async def sync(self) -> SyncResult:
        """Try ``auth status`` first; fall back to legacy getProfile command."""
        result = await super().sync()
        if result.error is None:
            return result

        # Fallback: legacy command (same as the deleted unified_service probe)
        return await self._sync_legacy_probe()

    async def _sync_legacy_probe(self) -> SyncResult:
        """Fallback: gws gmail users getProfile --format json."""
        import asyncio
        import shutil as _shutil

        binary_path = _shutil.which(self.binary_name)
        if binary_path is None:
            return SyncResult(
                adapter_name=self.adapter_name,
                error=f"{self.binary_name}: binary not found on PATH",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                binary_path, "gmail", "users", "getProfile",
                "--params", '{"userId":"me"}', "--format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except TimeoutError:
            return SyncResult(adapter_name=self.adapter_name, error="gws: timeout")
        except FileNotFoundError:
            return SyncResult(adapter_name=self.adapter_name, error="gws: binary not found")

        if proc.returncode != 0:
            return SyncResult(adapter_name=self.adapter_name, error="gws: legacy probe failed")

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not stdout:
            return SyncResult(adapter_name=self.adapter_name, error="gws: empty output")

        try:
            start = stdout.find("{")
            payload = stdout[start:] if start >= 0 else stdout
            data = json.loads(payload)
            email = str(data.get("emailAddress", "")).strip()
        except Exception as exc:
            return SyncResult(adapter_name=self.adapter_name, error=f"gws: parse error: {exc}")

        if not email:
            return SyncResult(adapter_name=self.adapter_name, error="gws: no email in response")

        return SyncResult(
            adapter_name=self.adapter_name,
            profiles=[
                SyncedProfile(
                    provider="google",
                    account_identifier=email,
                    backend_key=f"gws-cli/{email}",
                    source="gws-cli",
                )
            ],
        )

    def parse_output(self, stdout: str, stderr: str) -> list[SyncedProfile]:
        if not stdout.strip():
            return []

        data = json.loads(stdout)
        accounts = data.get("accounts", [])
        if not isinstance(accounts, list):
            return []

        profiles: list[SyncedProfile] = []
        for acct in accounts:
            if not isinstance(acct, dict):
                continue
            email = acct.get("email", "").strip()
            if not email:
                continue
            profiles.append(
                SyncedProfile(
                    provider="google",
                    account_identifier=email,
                    backend_key=f"gws-cli/{email}",
                    source="gws-cli",
                )
            )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Run gws auth token to get a fresh access token (sync subprocess)."""
        import shutil

        binary_path = shutil.which(self.binary_name)
        if binary_path is None:
            raise CredentialResolutionError(
                "external-cli", backend_key,
                f"{self.binary_name}: binary not found on PATH",
            )

        try:
            proc = subprocess.run(
                [binary_path, "auth", "token", "--format=json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, "gws auth token: timeout"
            ) from exc

        if proc.returncode != 0:
            error_detail = proc.stderr.strip() or f"exit code {proc.returncode}"
            raise CredentialResolutionError(
                "external-cli", backend_key, f"gws auth token: {error_detail}"
            )

        try:
            data = json.loads(proc.stdout)
            access_token = data.get("access_token", "").strip()
        except (json.JSONDecodeError, AttributeError) as exc:
            raise CredentialResolutionError(
                "external-cli", backend_key, f"gws auth token: parse error: {exc}"
            ) from exc

        if not access_token:
            raise CredentialResolutionError(
                "external-cli", backend_key, "gws auth token: empty access_token"
            )

        return ResolvedCredential(kind="bearer_token", access_token=access_token)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_gws_sync.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/gws_sync.py \
        src/nexus/bricks/auth/tests/test_gws_sync.py
git commit -m "feat(auth): add GwsCliSyncAdapter — SubprocessAdapter for gws CLI (#3740)"
```

---

## Task 6: CodexSyncAdapter + Tests

**Files:**
- Create: `src/nexus/bricks/auth/external_sync/codex_sync.py`
- Create: `src/nexus/bricks/auth/tests/test_codex_sync.py`

- [ ] **Step 1: Write failing tests**

Create `src/nexus/bricks/auth/tests/test_codex_sync.py`:

```python
"""Tests for CodexSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nexus.bricks.auth.credential_backend import CredentialResolutionError
from nexus.bricks.auth.external_sync.codex_sync import CodexSyncAdapter

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_CREDS_V1 = _FIXTURE_DIR / "codex_credentials_v1.json"
_CREDS_EMPTY = _FIXTURE_DIR / "codex_credentials_empty.json"


@pytest.fixture()
def adapter() -> CodexSyncAdapter:
    return CodexSyncAdapter()


class TestCodexParse:
    def test_parse_v1_discovers_two_profiles(self, adapter: CodexSyncAdapter) -> None:
        content = _CREDS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_CREDS_V1, content)

        assert len(profiles) == 2
        names = {p.account_identifier for p in profiles}
        assert "default" in names
        assert "staging" in names

    def test_parse_v1_backend_key_format(self, adapter: CodexSyncAdapter) -> None:
        content = _CREDS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_CREDS_V1, content)

        for p in profiles:
            assert p.backend_key.startswith("codex/")
            assert p.provider == "codex"
            assert p.source == "codex"

    def test_parse_empty_returns_empty(self, adapter: CodexSyncAdapter) -> None:
        content = _CREDS_EMPTY.read_text(encoding="utf-8")
        profiles = adapter.parse_file(_CREDS_EMPTY, content)
        assert profiles == []

    def test_parse_malformed_raises(self, adapter: CodexSyncAdapter) -> None:
        with pytest.raises(Exception):
            adapter.parse_file(Path("bad.json"), "not json")


class TestCodexPaths:
    def test_defaults_to_home_codex(
        self, adapter: CodexSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CODEX_CONFIG_DIR", raising=False)
        paths = adapter.paths()
        assert len(paths) == 2
        assert "credentials.json" in str(paths[0])
        assert "config.json" in str(paths[1])

    def test_env_override(
        self, adapter: CodexSyncAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_CONFIG_DIR", "/custom/codex")
        paths = adapter.paths()
        assert paths[0] == Path("/custom/codex/credentials.json")


class TestCodexSync:
    async def test_sync_discovers_from_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        result = await adapter.sync()

        assert result.error is None
        assert len(result.profiles) == 2

    async def test_sync_empty_file_returns_no_profiles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_EMPTY, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        result = await adapter.sync()

        assert result.profiles == []

    async def test_sync_missing_files_returns_degraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "nope"))
        adapter = CodexSyncAdapter()
        result = await adapter.sync()
        assert result.error is not None

    async def test_detect_true_when_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))
        adapter = CodexSyncAdapter()
        assert await adapter.detect() is True

    async def test_detect_false_when_no_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "nope"))
        adapter = CodexSyncAdapter()
        assert await adapter.detect() is False


class TestCodexResolveCredential:
    async def test_resolve_api_key_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = await adapter.resolve_credential("codex/default")
        assert cred.kind == "api_key"
        assert cred.api_key == "sk-codex-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    async def test_resolve_token_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = await adapter.resolve_credential("codex/staging")
        assert cred.kind == "bearer_token"
        assert cred.access_token is not None

    async def test_resolve_missing_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        with pytest.raises(CredentialResolutionError, match="nonexistent"):
            await adapter.resolve_credential("codex/nonexistent")

    def test_resolve_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cred_file = tmp_path / "credentials.json"
        shutil.copy(_CREDS_V1, cred_file)
        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path))

        adapter = CodexSyncAdapter()
        cred = adapter.resolve_credential_sync("codex/default")
        assert cred.kind == "api_key"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_codex_sync.py -v`
Expected: FAIL — `codex_sync` module does not exist.

- [ ] **Step 3: Implement CodexSyncAdapter**

Create `src/nexus/bricks/auth/external_sync/codex_sync.py`:

```python
"""Codex sync adapter — discovers credentials from ~/.codex/ config files.

FileAdapter subclass. Reads ~/.codex/credentials.json (primary) and
~/.codex/config.json (fallback).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from nexus.bricks.auth.credential_backend import (
    CredentialResolutionError,
    ResolvedCredential,
)
from nexus.bricks.auth.external_sync.base import SyncedProfile
from nexus.bricks.auth.external_sync.file_adapter import FileAdapter


class CodexSyncAdapter(FileAdapter):
    """Discovers Codex credentials from config files."""

    adapter_name = "codex"

    def _config_dir(self) -> Path:
        return Path(
            os.environ.get("CODEX_CONFIG_DIR", "~/.codex")
        ).expanduser()

    def paths(self) -> list[Path]:
        base = self._config_dir()
        return [
            base / "credentials.json",
            base / "config.json",
        ]

    def parse_file(self, _path: Path, content: str) -> list[SyncedProfile]:
        if not content.strip():
            return []

        data = json.loads(content)
        if not isinstance(data, dict):
            return []

        profiles: list[SyncedProfile] = []
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue
            if not (entry.get("api_key") or entry.get("token")):
                continue
            profiles.append(
                SyncedProfile(
                    provider="codex",
                    account_identifier=name,
                    backend_key=f"codex/{name}",
                    source="codex",
                )
            )

        return profiles

    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        return self._resolve_impl(backend_key)

    def _resolve_impl(self, backend_key: str) -> ResolvedCredential:
        """Re-read credential file and extract one profile's credential."""
        _, profile_name = backend_key.split("/", 1)

        for path in self.paths():
            try:
                content = path.read_text(encoding="utf-8")
                data = json.loads(content)
            except (OSError, json.JSONDecodeError):
                continue

            if not isinstance(data, dict):
                continue

            entry = data.get(profile_name)
            if not isinstance(entry, dict):
                continue

            if entry.get("api_key"):
                return ResolvedCredential(
                    kind="api_key",
                    api_key=entry["api_key"],
                    metadata={k: v for k, v in entry.items() if k != "api_key"},
                )
            if entry.get("token"):
                return ResolvedCredential(
                    kind="bearer_token",
                    access_token=entry["token"],
                    metadata={k: v for k, v in entry.items() if k != "token"},
                )

        raise CredentialResolutionError(
            "external-cli",
            backend_key,
            f"Codex profile '{profile_name}' not found in config files",
        )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_codex_sync.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/codex_sync.py \
        src/nexus/bricks/auth/tests/test_codex_sync.py
git commit -m "feat(auth): add CodexSyncAdapter — FileAdapter for ~/.codex/ (#3740)"
```

---

## Task 7: Register Adapters in __init__.py

**Files:**
- Modify: `src/nexus/bricks/auth/external_sync/__init__.py`

- [ ] **Step 1: Update __init__.py exports**

In `src/nexus/bricks/auth/external_sync/__init__.py`, add imports and exports for all four new adapters:

Add after the existing `AwsCliSyncAdapter` import:
```python
from nexus.bricks.auth.external_sync.codex_sync import CodexSyncAdapter
from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter
from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter
from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
```

Update `__all__` to include:
```python
__all__ = [
    "AdapterRegistry",
    "AwsCliSyncAdapter",
    "CircuitBreaker",
    "CodexSyncAdapter",
    "ExternalCliBackend",
    "ExternalCliSyncAdapter",
    "FileAdapter",
    "GcloudSyncAdapter",
    "GhCliSyncAdapter",
    "GwsCliSyncAdapter",
    "SubprocessAdapter",
    "SyncedProfile",
    "SyncResult",
]
```

- [ ] **Step 2: Verify imports work**

Run: `python -c "from nexus.bricks.auth.external_sync import GcloudSyncAdapter, GhCliSyncAdapter, GwsCliSyncAdapter, CodexSyncAdapter; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Run all adapter tests to confirm nothing broke**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_gcloud_sync.py src/nexus/bricks/auth/tests/test_gh_sync.py src/nexus/bricks/auth/tests/test_gws_sync.py src/nexus/bricks/auth/tests/test_codex_sync.py src/nexus/bricks/auth/tests/test_aws_sync.py -v`
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/bricks/auth/external_sync/__init__.py
git commit -m "feat(auth): register gcloud/gh/gws/codex adapters in external_sync (#3740)"
```

---

## Task 8: PathCLIBackend AUTH_SOURCE + Two-Phase Token Resolution

**Files:**
- Modify: `src/nexus/backends/connectors/cli/base.py:61-271`
- Create: `src/nexus/bricks/auth/tests/test_connector_auth_migration.py`

- [ ] **Step 1: Write failing test for two-phase resolution**

Create `src/nexus/bricks/auth/tests/test_connector_auth_migration.py`:

```python
"""Tests for PathCLIBackend AUTH_SOURCE integration + connector migration."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.credential_pool import CredentialPoolRegistry, NoAvailableCredentialError
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
from nexus.bricks.auth.profile import AuthProfile, InMemoryAuthProfileStore, ProfileUsageStats


class TestPathCLIBackendAuthSource:
    """Test two-phase token resolution via AUTH_SOURCE."""

    def test_external_cli_takes_priority_over_token_manager(self) -> None:
        """When AUTH_SOURCE is set and external credential exists, use it."""
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="google/user@example.com",
                provider="google",
                account_identifier="user@example.com",
                backend="external-cli",
                backend_key="gws-cli/user@example.com",
                usage_stats=ProfileUsageStats(),
            )
        )
        pool_registry = CredentialPoolRegistry(store=store)

        mock_backend = MagicMock(spec=ExternalCliBackend)
        mock_backend.resolve_sync.return_value = ResolvedCredential(
            kind="bearer_token", access_token="external-token-123"
        )

        # Import here to avoid circular import issues
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector(
            credential_pool_registry=pool_registry,
            external_cli_backend=mock_backend,
        )

        token = connector._get_user_token(context=None)
        assert token == "external-token-123"
        mock_backend.resolve_sync.assert_called_once_with("gws-cli/user@example.com")

    def test_falls_back_to_token_manager_when_no_external_profiles(self) -> None:
        """When no external profiles, fall back to TokenManager."""
        store = InMemoryAuthProfileStore()  # empty — no profiles
        pool_registry = CredentialPoolRegistry(store=store)

        mock_backend = MagicMock(spec=ExternalCliBackend)

        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector(
            credential_pool_registry=pool_registry,
            external_cli_backend=mock_backend,
        )

        # No TokenManager either → returns None
        token = connector._get_user_token(context=None)
        assert token is None

    def test_no_auth_source_skips_external_cli(self) -> None:
        """When AUTH_SOURCE is None, don't try external CLI."""
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="google/user@example.com",
                provider="google",
                account_identifier="user@example.com",
                backend="external-cli",
                backend_key="gws-cli/user@example.com",
                usage_stats=ProfileUsageStats(),
            )
        )
        pool_registry = CredentialPoolRegistry(store=store)
        mock_backend = MagicMock(spec=ExternalCliBackend)

        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            # AUTH_SOURCE not set — default None

        connector = _TestConnector(
            credential_pool_registry=pool_registry,
            external_cli_backend=mock_backend,
        )

        token = connector._get_user_token(context=None)
        assert token is None  # No TokenManager, no AUTH_SOURCE
        mock_backend.resolve_sync.assert_not_called()


class TestConcurrentSelect:
    """Concurrency test: 10 coroutines × 5 providers, no deadlock."""

    async def test_concurrent_select_no_deadlock(self) -> None:
        store = InMemoryAuthProfileStore()
        providers = ["google", "github", "s3", "codex", "gcs"]

        for provider in providers:
            for i in range(2):
                store.upsert(
                    AuthProfile(
                        id=f"{provider}/acct{i}@example.com",
                        provider=provider,
                        account_identifier=f"acct{i}@example.com",
                        backend="external-cli",
                        backend_key=f"test/{provider}/acct{i}",
                        usage_stats=ProfileUsageStats(),
                    )
                )

        registry = CredentialPoolRegistry(store=store)

        async def hammer(provider: str) -> None:
            pool = registry.get(provider)
            for _ in range(50):
                profile = await pool.select()
                assert profile.provider == provider

        await asyncio.wait_for(
            asyncio.gather(
                *[hammer(p) for p in providers for _ in range(2)]
            ),
            timeout=5.0,
        )


class TestBug3713Regression:
    """Regression tests: #3713 failure reasons classified correctly."""

    def test_missing_binary_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.UPSTREAM_CLI_MISSING]
        assert "install" in hint.lower() or "gws" in hint.lower()

    def test_revoked_token_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.AUTH_PERMANENT]
        assert "login" in hint.lower()

    def test_scope_insufficient_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.SCOPE_INSUFFICIENT]
        assert "scope" in hint.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_connector_auth_migration.py -v`
Expected: FAIL — PathCLIBackend doesn't have AUTH_SOURCE or credential_pool_registry.

- [ ] **Step 3: Add AUTH_SOURCE and credential_pool_registry to PathCLIBackend**

In `src/nexus/backends/connectors/cli/base.py`, add the class attribute after `CLI_SERVICE` (around line 64):

```python
    AUTH_SOURCE: str | None = None  # e.g., "gws-cli", "gh-cli", "gcloud"
```

Modify `__init__` to accept new parameters. After the existing `token_manager_db: str | None = None` parameter, add:

```python
    credential_pool_registry: "CredentialPoolRegistry | None" = None,
    external_cli_backend: "ExternalCliBackend | None" = None,
```

In the `__init__` body, store them:

```python
        self._credential_pool_registry = credential_pool_registry
        self._external_cli_backend = external_cli_backend
```

Add the TYPE_CHECKING imports at the top of the file:

```python
if TYPE_CHECKING:
    from nexus.bricks.auth.credential_pool import CredentialPoolRegistry
    from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
```

- [ ] **Step 4: Modify _get_user_token for two-phase resolution**

In `src/nexus/backends/connectors/cli/base.py`, replace the `_get_user_token` method (lines 237-271) with:

```python
    def _get_user_token(self, context: "OperationContext | None" = None) -> str | None:
        """Resolve auth token — external CLI first, then TokenManager fallback.

        Phase 1 (new): If AUTH_SOURCE is set and a CredentialPoolRegistry is
        available, try to select a profile and resolve via ExternalCliBackend.
        Phase 2 (existing): Fall back to TokenManager.get_credentials().
        """
        # Phase 1: External CLI credential
        if self.AUTH_SOURCE and self._credential_pool_registry and self._external_cli_backend:
            token = self._resolve_from_external_cli()
            if token:
                return token

        # Phase 2: TokenManager (existing behavior)
        if self._token_manager is None:
            return None
        if context is None:
            return None

        try:
            user_email = getattr(context, "user_id", None)
            zone_id = getattr(context, "zone_id", None)
            if not user_email:
                return None

            provider = "google"  # Default; subclasses override
            if self._config and self._config.auth:
                provider = self._config.auth.provider

            credentials = self._token_manager.get_credentials(
                user_email=user_email,
                provider=provider,
                zone_id=zone_id,
            )
            if credentials:
                return str(credentials.get("access_token", ""))
        except Exception:
            logger.debug("Token resolution failed for %s", context.user_id, exc_info=True)

        return None

    def _resolve_from_external_cli(self) -> str | None:
        """Try to resolve a token from the external CLI credential pool."""
        provider = "google"  # default
        if self._config and self._config.auth:
            provider = self._config.auth.provider

        try:
            pool = self._credential_pool_registry.get(provider)
            profile = pool.select_sync()
            cred = self._external_cli_backend.resolve_sync(profile.backend_key)
            return cred.access_token or cred.api_key
        except Exception:
            logger.debug("External CLI credential resolution failed", exc_info=True)
            return None
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_connector_auth_migration.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Run existing PathCLIBackend tests to confirm no regression**

Run: `python -m pytest src/nexus/bricks/auth/tests/ -v -k "not nightly"`
Expected: All existing tests still PASS.

- [ ] **Step 7: Commit**

```bash
git add src/nexus/backends/connectors/cli/base.py \
        src/nexus/bricks/auth/tests/test_connector_auth_migration.py
git commit -m "feat(auth): PathCLIBackend two-phase token resolution with AUTH_SOURCE (#3740)"
```

---

## Task 9: gws_* Connector AUTH_SOURCE Declarations

**Files:**
- Modify: `src/nexus/backends/connectors/gws/connector.py` (6 classes)

- [ ] **Step 1: Add AUTH_SOURCE to all 6 gws connectors**

In `src/nexus/backends/connectors/gws/connector.py`, add `AUTH_SOURCE = "gws-cli"` to each connector class, right after the class definition line:

**SheetsConnector** (line 91): add `AUTH_SOURCE = "gws-cli"` as the first line inside the class.

**DocsConnector** (line 176): add `AUTH_SOURCE = "gws-cli"` as the first line inside the class.

**ChatConnector** (line 359): add `AUTH_SOURCE = "gws-cli"` as the first line inside the class.

**DriveConnector** (line 447): add `AUTH_SOURCE = "gws-cli"` as the first line inside the class.

**GmailConnector** (line 563): add `AUTH_SOURCE = "gws-cli"` as the first line inside the class.

**CalendarConnector** (line 1217): add `AUTH_SOURCE = "gws-cli"` as the first line inside the class.

- [ ] **Step 2: Verify with a quick import check**

Run: `python -c "from nexus.backends.connectors.gws.connector import GmailConnector; assert GmailConnector.AUTH_SOURCE == 'gws-cli'; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/nexus/backends/connectors/gws/connector.py
git commit -m "feat(auth): declare AUTH_SOURCE='gws-cli' on all gws_* connectors (#3740)"
```

---

## Task 10: unified_service.py Cleanup

**Files:**
- Modify: `src/nexus/bricks/auth/unified_service.py`

- [ ] **Step 1: Delete _detect_google_workspace_cli_native method**

Delete lines 754-807 (the `_detect_google_workspace_cli_native` method).

- [ ] **Step 2: Delete _detect_oauth_native method**

Delete lines 744-752 (the `_detect_oauth_native` method).

- [ ] **Step 3: Replace cached_native logic in auth-list loop**

Replace the `cached_native` block (lines 421-428):

```python
            cached_native: dict[str, str] | None | object = _UNSET
```

...through:

```python
                    if cached_native is _UNSET:
                        cached_native = await self._detect_google_workspace_cli_native()
                    native = cached_native if isinstance(cached_native, dict) else None
```

With:

```python
            # Check profile store for gws-cli-synced profiles (Phase 3, #3740)
            _gws_profiles: list | None = None
            _gws_native: dict[str, str] | None = None
```

And where `native` was used from `cached_native`, replace with profile-store lookup:

```python
                if service in _GOOGLE_OAUTH_SERVICES:
                    if _gws_profiles is None and self._profile_store is not None:
                        _gws_profiles = [
                            p
                            for p in self._profile_store.list(provider="google")
                            if p.backend == "external-cli"
                            and p.backend_key.startswith("gws-cli/")
                        ]
                    if _gws_profiles:
                        _gws_native = {
                            "source": "gws-cli",
                            "email": _gws_profiles[0].account_identifier,
                            "message": (
                                f"gws CLI profile available for "
                                f"{_gws_profiles[0].account_identifier}."
                            ),
                        }
                    native = _gws_native
                else:
                    native = None
```

- [ ] **Step 4: Update the test_auth call at line 610**

Replace the line:
```python
        native = await self._detect_oauth_native(service, user_email=user_email)
```

With:
```python
        native = None
        if service in _GOOGLE_OAUTH_SERVICES and self._profile_store is not None:
            gws_profiles = [
                p
                for p in self._profile_store.list(provider="google")
                if p.backend == "external-cli"
                and p.backend_key.startswith("gws-cli/")
            ]
            if gws_profiles:
                email = gws_profiles[0].account_identifier
                if user_email is None or user_email == email:
                    native = {
                        "source": "gws-cli",
                        "email": email,
                        "message": f"gws CLI profile available for {email}.",
                    }
```

- [ ] **Step 5: Remove unused shutil import if no longer needed**

Check if `shutil` is used elsewhere in unified_service.py. If the only usage was `shutil.which("gws")` in the deleted method, remove it from imports.

- [ ] **Step 6: Run existing unified_service tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/ -v -k "unified_service or auth_service"`
Expected: All PASS (or skip if no existing tests — the cleanup should not break anything).

- [ ] **Step 7: Commit**

```bash
git add src/nexus/bricks/auth/unified_service.py
git commit -m "fix(auth): replace gws-CLI probe with profile-store lookup (#3740, #3713)"
```

---

## Task 11: No-Network + Nightly Real-Binary Tests

**Files:**
- Modify: `src/nexus/bricks/auth/tests/test_connector_auth_migration.py` (append)

- [ ] **Step 1: Add no_network fixture and tests**

Append to `src/nexus/bricks/auth/tests/test_connector_auth_migration.py`:

```python
import os
import socket as _socket_module


@pytest.fixture()
def no_network(monkeypatch: pytest.MonkeyPatch):
    """Block all network I/O."""
    _real_socket = _socket_module.socket

    def _blocked(*args, **kwargs):
        raise OSError("Network blocked by test fixture")

    monkeypatch.setattr(_socket_module, "socket", _blocked)
    yield
    # monkeypatch auto-restores


class TestNoNetwork:
    """Adapters must return degraded results without network access."""

    async def test_gcloud_offline_safe(self, tmp_path, monkeypatch, no_network) -> None:
        from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nope"))
        adapter = GcloudSyncAdapter()
        result = await adapter.sync()
        # FileAdapter: returns degraded with error, doesn't hang
        assert result.error is not None or result.profiles == []

    async def test_codex_offline_safe(self, tmp_path, monkeypatch, no_network) -> None:
        from nexus.bricks.auth.external_sync.codex_sync import CodexSyncAdapter

        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "nope"))
        adapter = CodexSyncAdapter()
        result = await adapter.sync()
        assert result.error is not None or result.profiles == []

    async def test_gh_file_fallback_offline(self, tmp_path, monkeypatch, no_network) -> None:
        import shutil

        from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        _FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
        shutil.copy(_FIXTURE_DIR / "gh_hosts_v2.40.yml", config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            result = await adapter.sync()
        # File fallback works offline
        assert result.error is None
        assert len(result.profiles) == 1

    async def test_gws_no_binary_returns_fast(self, monkeypatch, no_network) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter

        with patch("shutil.which", return_value=None):
            adapter = GwsCliSyncAdapter()
            result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is not None


# ---------------------------------------------------------------------------
# Nightly real-binary tests (opt-in)
# ---------------------------------------------------------------------------


@pytest.mark.skipunless(
    os.environ.get("TEST_WITH_REAL_GCLOUD_CLI"), reason="opt-in: set TEST_WITH_REAL_GCLOUD_CLI=1"
)
class TestRealGcloudBinary:
    async def test_gcloud_real_sync(self, tmp_path, monkeypatch) -> None:
        from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

        adapter = GcloudSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gcloud not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gcloud profile"


@pytest.mark.skipunless(
    os.environ.get("TEST_WITH_REAL_GH_CLI"), reason="opt-in: set TEST_WITH_REAL_GH_CLI=1"
)
class TestRealGhBinary:
    async def test_gh_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

        adapter = GhCliSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gh not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gh profile"


@pytest.mark.skipunless(
    os.environ.get("TEST_WITH_REAL_GWS_CLI"), reason="opt-in: set TEST_WITH_REAL_GWS_CLI=1"
)
class TestRealGwsBinary:
    async def test_gws_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter

        adapter = GwsCliSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gws not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gws profile"
```

- [ ] **Step 2: Run the no-network tests**

Run: `python -m pytest src/nexus/bricks/auth/tests/test_connector_auth_migration.py::TestNoNetwork -v`
Expected: All PASS.

- [ ] **Step 3: Commit**

```bash
git add src/nexus/bricks/auth/tests/test_connector_auth_migration.py
git commit -m "test(auth): add no-network + nightly real-binary e2e tests (#3740)"
```

---

## Task 12: Final Integration Check

- [ ] **Step 1: Run the full auth test suite**

Run: `python -m pytest src/nexus/bricks/auth/tests/ -v --timeout=30 -k "not nightly and not Real"`
Expected: All tests PASS.

- [ ] **Step 2: Run mypy type check**

Run: `python -m mypy src/nexus/bricks/auth/external_sync/ --ignore-missing-imports`
Expected: No errors (or only pre-existing ones).

- [ ] **Step 3: Run ruff lint**

Run: `python -m ruff check src/nexus/bricks/auth/external_sync/ src/nexus/backends/connectors/cli/base.py src/nexus/backends/connectors/gws/connector.py`
Expected: No new violations.

- [ ] **Step 4: Final commit if any cleanup needed**

Only if steps 2-3 required fixes:
```bash
git add -u
git commit -m "fix(auth): address lint/type issues from Phase 3 adapters (#3740)"
```
