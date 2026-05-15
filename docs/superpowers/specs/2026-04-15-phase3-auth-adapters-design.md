# Phase 3: gcloud/gh/gws/codex Sync Adapters + gws_* Connector Migration

**Issue:** #3740
**Epic:** #3722
**Blocked by:** #3739 (Phase 2 — landed)
**Date:** 2026-04-15

---

## Overview

Phase 3 adds four external-CLI sync adapters and migrates the gws_* connector family to resolve auth through the unified profile store. Deletes the gws-CLI probe workarounds in `unified_service.py`.

## Architecture

### Inheritance

All four adapters inherit from Phase 2 bases (`FileAdapter` or `SubprocessAdapter`), which in turn inherit from `ExternalCliSyncAdapter`. The existing `ExternalCliBackend` delegates `resolve()` calls to the appropriate adapter via `AdapterRegistry.get_adapter()`.

```
ExternalCliSyncAdapter (base.py)
├── FileAdapter (file_adapter.py)
│   ├── AwsCliSyncAdapter     ← Phase 2
│   ├── GcloudSyncAdapter     ← Phase 3
│   └── CodexSyncAdapter      ← Phase 3
├── SubprocessAdapter (subprocess_adapter.py)
│   └── GwsCliSyncAdapter     ← Phase 3
└── GhCliSyncAdapter          ← Phase 3 (composes both strategies)
```

### Data flow

```
Adapter.sync() → SyncedProfile[] → AdapterRegistry._upsert_sync_results()
  → AuthProfile (backend="external-cli") → SqliteAuthProfileStore.upsert()

PathCLIBackend._get_user_token(context)
  → CredentialPoolRegistry.get(provider).select() → AuthProfile
  → ExternalCliBackend.resolve(backend_key) → ResolvedCredential
  → access_token injected via env var (GWS_ACCESS_TOKEN, GH_TOKEN, etc.)
  → fallback: TokenManager.get_credentials() (existing path)
```

---

## 1. Sync Adapters

### 1a. GcloudSyncAdapter — FileAdapter

**File:** `src/nexus/bricks/auth/external_sync/gcloud_sync.py`

| Field | Value |
|-------|-------|
| `adapter_name` | `"gcloud"` |
| `provider` | `"gcs"` |
| `backend_key` format | `"gcloud/{account_email}"` |
| `source` | `"gcloud"` |

**`paths()`:**
1. `~/.config/gcloud/application_default_credentials.json` — ADC file (primary)
2. `~/.config/gcloud/properties` — active config INI with `[core] account=`

Respects `CLOUDSDK_CONFIG` env var override for the `~/.config/gcloud/` base directory.

**`parse_file()`:**
- ADC JSON: extract `client_email` (service account) or `client_id` + `type` (authorized_user). For `authorized_user` type, the account identifier comes from the properties file's `[core] account`.
- Properties INI: extract `[core] account` for active account email.
- Combines both: ADC provides credential type, properties provides active account identity.

**`resolve_credential()`:**
- Re-read ADC JSON.
- `authorized_user` type: ADC files for this type contain `client_id`, `client_secret`, and `refresh_token` (no `access_token`). Return `ResolvedCredential(kind="bearer_token", access_token=None, metadata={"client_id": ..., "client_secret": ..., "refresh_token": ...})`. The caller (or a future token-refresh layer) uses these to mint a fresh access token. Phase 3 scope: discover and expose the credential; token refresh is Phase 4.
- `service_account` type: return `ResolvedCredential(kind="api_key", api_key=private_key, metadata={"client_email": ...})`.

**No metadata server hit** — ADC file is the source of truth. Offline-safe.

### 1b. GhCliSyncAdapter — Dual-mode (SubprocessAdapter + FileAdapter fallback)

**File:** `src/nexus/bricks/auth/external_sync/gh_sync.py`

| Field | Value |
|-------|-------|
| `adapter_name` | `"gh-cli"` |
| `provider` | `"github"` |
| `backend_key` format | `"gh-cli/{username}"` |
| `source` | `"gh-cli"` |

Subclasses `ExternalCliSyncAdapter` directly and composes both strategies internally — does NOT subclass `SubprocessAdapter` or `FileAdapter`.

**`detect()`:**
- `shutil.which("gh")` → True → subprocess mode
- Else: check `~/.config/gh/hosts.yml` exists → True → file mode
- Else: False

**`sync()` — subprocess mode:**
- Run: `gh auth status --show-token` (outputs to stderr, tokens in stdout in newer versions)
- Parse: extract username, token, scopes per host (github.com, enterprise hosts)
- One `SyncedProfile` per `(host, username)` pair

**`sync()` — file fallback mode:**
- Read `~/.config/gh/hosts.yml`
- v2.40 format: `github.com:\n  oauth_token: gho_xxx\n  user: username`
- v2.50 format: `github.com:\n  users:\n    username:\n      oauth_token: gho_xxx`
- Parse both formats, one `SyncedProfile` per `(host, username)` pair

**`resolve_credential()`:**
- Same dual-mode: prefer subprocess (`gh auth token -h {host}`), fallback to file parse
- Return `ResolvedCredential(kind="bearer_token", access_token=token)`

Respects `GH_CONFIG_DIR` env var override for `~/.config/gh/`.

### 1c. GwsCliSyncAdapter — SubprocessAdapter

**File:** `src/nexus/bricks/auth/external_sync/gws_sync.py`

| Field | Value |
|-------|-------|
| `adapter_name` | `"gws-cli"` |
| `binary_name` | `"gws"` |
| `provider` | `"google"` |
| `backend_key` format | `"gws-cli/{email}"` |
| `source` | `"gws-cli"` |

**`get_status_args()`:** `("auth", "status", "--format=json")`

Note: the exact command surface must be validated against a real gws install. If `gws auth status` doesn't exist, the fallback is `gws gmail users getProfile --params '{"userId":"me"}' --format json` (the same command the deleted probe used).

**`parse_output()`:**
- JSON parse stdout
- Extract account email(s) from the response
- One `SyncedProfile` per connected account

**`resolve_credential()`:**
- Run: `gws auth token --format=json` (or equivalent)
- Extract `access_token` from JSON output
- Return `ResolvedCredential(kind="bearer_token", access_token=...)`

### 1d. CodexSyncAdapter — FileAdapter

**File:** `src/nexus/bricks/auth/external_sync/codex_sync.py`

| Field | Value |
|-------|-------|
| `adapter_name` | `"codex"` |
| `provider` | `"codex"` |
| `backend_key` format | `"codex/{profile_name}"` |
| `source` | `"codex"` |

**`paths()`:**
1. `~/.codex/credentials.json` — primary credential file
2. `~/.codex/config.json` — fallback/alternate config

Respects `CODEX_CONFIG_DIR` env var override.

**`parse_file()`:**
- JSON parse → expect object with profile entries (keyed by profile name)
- Each entry has `api_key` or `token` field
- One `SyncedProfile` per profile entry

**`resolve_credential()`:**
- Re-read credential file
- Extract the specific profile's credential
- Return `ResolvedCredential(kind="api_key", api_key=...)` or `kind="bearer_token"` depending on field present

### Registration

All four adapters added to `external_sync/__init__.py` exports:

```python
from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter
from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter
from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
from nexus.bricks.auth.external_sync.codex_sync import CodexSyncAdapter
```

And instantiated in whatever application startup code creates the `AdapterRegistry` (alongside the existing `AwsCliSyncAdapter`).

---

## 2. gws_* Connector Migration

### PathCLIBackend changes

**New class attribute:**

```python
class PathCLIBackend(...):
    AUTH_SOURCE: str | None = None  # e.g., "gws-cli", "gh-cli", "gcloud"
```

**New optional constructor parameter:**

```python
def __init__(
    self,
    config: CLIConnectorConfig | None = None,
    token_manager_db: str | None = None,
    credential_pool_registry: CredentialPoolRegistry | None = None,  # NEW
    **kwargs: Any,
) -> None:
    self._credential_pool_registry = credential_pool_registry
    # ... existing init
```

**Modified `_get_user_token()`:**

Two-phase resolution:

```python
def _get_user_token(self, context: "OperationContext | None" = None) -> str | None:
    # Phase 1: External CLI credential (new)
    if self.AUTH_SOURCE and self._credential_pool_registry:
        token = self._resolve_from_external_cli(context)
        if token:
            return token

    # Phase 2: TokenManager (existing, unchanged)
    if self._token_manager is None:
        return None
    # ... existing TokenManager logic
```

**New private method `_resolve_from_external_cli()`:**

```python
def _resolve_from_external_cli(self, context: ...) -> str | None:
    provider = "google"  # default
    if self._config and self._config.auth:
        provider = self._config.auth.provider

    try:
        pool = self._credential_pool_registry.get(provider)
        profile = pool.select_sync()  # sync — _get_user_token is sync
        cred = self._external_cli_backend.resolve_sync(profile.backend_key)
        return cred.access_token or cred.api_key
    except NoAvailableCredentialError:
        return None
    except Exception:
        logger.debug("External CLI credential resolution failed", exc_info=True)
        return None
```

**Sync resolution:** `_get_user_token()` is sync (called from sync `write_content`/`read_content` paths). `ExternalCliBackend.resolve()` is async. Solution: add `resolve_credential_sync()` to each adapter (synchronous file I/O via `Path.read_text()` or `subprocess.run()`), and `ExternalCliBackend.resolve_sync()` that delegates to it. FileAdapters are naturally sync-safe. SubprocessAdapters use `subprocess.run()` (same as `PathCLIBackend._execute_cli` already does).

The `_external_cli_backend` is injected alongside `credential_pool_registry` at construction time.

### Per-connector changes

Each gws connector adds one attribute:

```python
@register_connector("gws_gmail", category="cli", service_name="gws")
class GmailConnector(PathCLIBackend):
    AUTH_SOURCE = "gws-cli"
    # ... rest unchanged

@register_connector("gws_drive", category="cli", service_name="gws")
class DriveConnector(PathCLIBackend):
    AUTH_SOURCE = "gws-cli"
    # ... rest unchanged
```

Same for: `gws_docs`, `gws_sheets`, `gws_calendar`, `gws_chat`.

---

## 3. unified_service.py Cleanup

### Deleted methods

1. **`_detect_google_workspace_cli_native()`** (lines 754-807) — hardcoded gws subprocess probe
2. **`_detect_oauth_native()`** (lines 744-752) — thin wrapper, sole caller of above

### Modified code

**Auth-list loop (lines 419-449):**

Replace:
```python
cached_native = await self._detect_google_workspace_cli_native()
native = cached_native if isinstance(cached_native, dict) else None
```

With:
```python
# Query profile store for gws-cli-synced profiles
gws_profiles = [
    p for p in self._profile_store.list(provider="google")
    if p.backend == "external-cli" and p.backend_key.startswith("gws-cli/")
]
native_available = len(gws_profiles) > 0
```

The `AuthSummary` entries are then built from profile store data instead of ad-hoc probe results. Same UX (shows "gws-cli available for user@example.com"), different data source.

### `_probe_google_workspace_targets()` — kept

This method checks service reachability (can gmail/drive/calendar actually be accessed). It's a separate concern from auth discovery. It stays, but its input changes from the native probe dict to profile store data.

### Bug #3713 failure routing

Failure classification now flows through the adapter framework:

| Failure | `AuthProfileFailureReason` | Fix hint |
|---------|---------------------------|----------|
| gws binary not found | `UPSTREAM_CLI_MISSING` | `"Install gws CLI: ..."` |
| Token revoked/expired | `AUTH_PERMANENT` | `"Run: gws auth login"` |
| Missing OAuth scopes | `SCOPE_INSUFFICIENT` | `"Run: gws auth login --scopes=..."` |
| Network timeout | `TIMEOUT` | `"Check network connectivity"` |
| Clock skew | `CLOCK_SKEW` | `"Sync system clock"` |

Fix hints stored as a `_FIX_HINTS: dict[AuthProfileFailureReason, str]` class attribute on `GwsCliSyncAdapter`. No separate classifier file — the adapter knows its own CLI's remediation commands.

---

## 4. Tests

### 4a. Fixture files

New in `src/nexus/bricks/auth/tests/fixtures/external_cli_output/`:

| Fixture | Description |
|---------|-------------|
| `gcloud_adc_v456.json` | Standard ADC: authorized_user type |
| `gcloud_adc_service_account.json` | Service account key variant |
| `gcloud_properties_v456.ini` | `[core] account=user@example.com` |
| `gh_hosts_v2.40.yml` | Flat `oauth_token` format |
| `gh_hosts_v2.50.yml` | Nested `users:` format |
| `gh_auth_status_v2.40.txt` | `gh auth status --show-token` stdout (v2.40) |
| `gh_auth_status_v2.50.txt` | Same, v2.50 format |
| `gws_status_v1.json` | `gws auth status --format=json` output |
| `gws_status_v2.json` | Future format variant |
| `codex_credentials_v1.json` | Standard codex credentials |
| `codex_credentials_empty.json` | Empty/minimal edge case |

### 4b. Per-adapter parser tests

One test file per adapter (`test_gcloud_sync.py`, `test_gh_sync.py`, `test_gws_sync.py`, `test_codex_sync.py`). Each follows `test_aws_sync.py` structure:

- **Parse tests:** parse each fixture version, verify `account_identifier`, `backend_key`, `provider`, `source`
- **Empty/malformed:** empty file → empty profiles; garbage → parse error in `SyncResult.error`
- **backend_key format:** verify `"{adapter_name}/{identifier}"` pattern
- **provider/source values:** verify correct constants

### 4c. Error condition tests

Per adapter:
- Missing binary → `SyncResult(error="gws: binary not found on PATH")`
- Missing file → degraded `SyncResult` with error message
- Unreadable permissions → permission error captured
- Malformed content → parse error captured
- Empty file → empty profiles, no crash

### 4d. `no_network` fixture tests

```python
@pytest.fixture
def no_network(monkeypatch):
    """Block all network I/O."""
    import socket
    def _blocked(*args, **kwargs):
        raise OSError("Network blocked by test fixture")
    monkeypatch.setattr(socket, "socket", _blocked)
```

Each adapter tested under `no_network`. SubprocessAdapters must return degraded result within 2s. FileAdapters are inherently offline-safe.

### 4e. Nightly real-binary e2e

```python
@pytest.mark.skipunless(
    os.environ.get("TEST_WITH_REAL_GCLOUD_CLI"), reason="opt-in real binary"
)
async def test_gcloud_real_binary_sync(tmp_path):
    """Run actual gcloud in temp HOME, validate parseable output."""
```

One test class per adapter. Opt-in via `TEST_WITH_REAL_GCLOUD_CLI=1`, `TEST_WITH_REAL_GH_CLI=1`, `TEST_WITH_REAL_GWS_CLI=1`. Runs actual binaries in temp `HOME`. Validates `sync()` output is parseable and `resolve_credential()` returns a credential.

### 4f. Connector integration test

```python
class TestGwsGmailConnectorAuth:
    async def test_resolves_through_external_cli_backend(self):
        """gws_gmail with a gws-synced profile resolves via ExternalCliBackend."""
        # Setup: profile store with gws-cli/user@example.com profile
        # Setup: mock ExternalCliBackend.resolve() → access_token
        # Setup: GmailConnector with AUTH_SOURCE="gws-cli" + credential_pool_registry
        # Act: connector._get_user_token(context)
        # Assert: token came from ExternalCliBackend, not TokenManager
        # Assert: zero calls to TokenManager.get_credentials
```

### 4g. Regression tests for #3713

```python
class TestBug3713FailureClassification:
    @pytest.mark.parametrize("scenario,expected_reason,expected_hint_substr", [
        ("missing_binary", AuthProfileFailureReason.UPSTREAM_CLI_MISSING, "Install gws"),
        ("revoked_token", AuthProfileFailureReason.AUTH_PERMANENT, "gws auth login"),
        ("missing_scopes", AuthProfileFailureReason.SCOPE_INSUFFICIENT, "scopes"),
    ])
    async def test_failure_classified_correctly(self, scenario, expected_reason, expected_hint_substr):
        ...
```

### 4h. Concurrency test

```python
async def test_concurrent_profile_store_select():
    """10 coroutines × 5 providers, no deadlock, no torn reads."""
    store = InMemoryAuthProfileStore()
    # Pre-populate with 2 profiles per provider
    registry = CredentialPoolRegistry(store=store)

    async def hammer(provider: str):
        pool = registry.get(provider)
        for _ in range(50):
            profile = await pool.select()
            assert profile.provider == provider

    await asyncio.wait_for(
        asyncio.gather(*[hammer(p) for p in ["google", "github", "s3", "codex", "gcs"]
                         for _ in range(2)]),
        timeout=5.0,  # deadlock detector
    )
```

---

## Non-goals (explicitly excluded)

- `nexus auth doctor` overhaul (Phase 4)
- CLI unification between `auth_cli.py` and `_auth_cli.py` (Phase 4)
- `--finalize` migration command (Phase 4)
- PostgresAuthProfileStore (sister epic)

## Files touched

### New files
- `src/nexus/bricks/auth/external_sync/gcloud_sync.py`
- `src/nexus/bricks/auth/external_sync/gh_sync.py`
- `src/nexus/bricks/auth/external_sync/gws_sync.py`
- `src/nexus/bricks/auth/external_sync/codex_sync.py`
- `src/nexus/bricks/auth/tests/test_gcloud_sync.py`
- `src/nexus/bricks/auth/tests/test_gh_sync.py`
- `src/nexus/bricks/auth/tests/test_gws_sync.py`
- `src/nexus/bricks/auth/tests/test_codex_sync.py`
- `src/nexus/bricks/auth/tests/test_connector_auth_migration.py`
- `src/nexus/bricks/auth/tests/test_bug3713_regression.py`
- `src/nexus/bricks/auth/tests/test_concurrent_select.py`
- 11 fixture files in `tests/fixtures/external_cli_output/`

### Modified files
- `src/nexus/bricks/auth/external_sync/__init__.py` — add exports
- `src/nexus/backends/connectors/cli/base.py` — `AUTH_SOURCE`, `credential_pool_registry`, modified `_get_user_token()`
- `src/nexus/backends/connectors/gws/connector.py` — add `AUTH_SOURCE = "gws-cli"` to each connector
- `src/nexus/bricks/auth/unified_service.py` — delete probe methods, update auth-list loop
- `src/nexus/bricks/auth/external_sync/external_cli_backend.py` — add `resolve_sync()`
- `src/nexus/bricks/auth/external_sync/base.py` — add `resolve_credential_sync()` to ABC
