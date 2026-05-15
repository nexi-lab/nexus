# External-CLI Sync Framework + AWS Adapter (Phase 2 of #3722)

Issue: #3739
Epic: #3722
Blocked by: #3738 (closed)

## Overview

Phase 2 lands the external-CLI sync framework, the first concrete adapter (aws-cli), and cuts `nexus-fs auth list` over to read from the unified profile store. Two distinct responsibilities are separated into two class hierarchies:

1. **Sync adapters** — discover which accounts exist in external CLIs, upsert routing metadata into `AuthProfileStore`
2. **Credential backend** — `ExternalCliBackend` implements `CredentialBackend`, resolves fresh credentials on demand by re-reading external sources

This matches Phase 1's decision 1A: AuthProfile = routing metadata only, credential lives in pluggable backend.

## Module Layout

```
src/nexus/bricks/auth/external_sync/
├── __init__.py              # re-exports public API
├── base.py                  # ExternalCliSyncAdapter ABC + SyncedProfile + SyncResult
├── subprocess_adapter.py    # SubprocessAdapter(ExternalCliSyncAdapter)
├── file_adapter.py          # FileAdapter(ExternalCliSyncAdapter)
├── aws_sync.py              # AwsCliSyncAdapter(FileAdapter) — ~40 LOC
├── external_cli_backend.py  # ExternalCliBackend(CredentialBackend) — ~80 LOC
└── registry.py              # AdapterRegistry — startup, background loop, circuit breaker
```

## Adapter ABC Hierarchy

### `base.py` — `ExternalCliSyncAdapter`

```python
@dataclass(frozen=True, slots=True)
class SyncedProfile:
    """One discovered account from an external CLI."""
    provider: str              # e.g. "s3"
    account_identifier: str    # e.g. "default", "work-prod"
    backend_key: str           # opaque key for ExternalCliBackend.resolve()
    source: str                # e.g. "aws-cli"

@dataclass
class SyncResult:
    """Output of a single adapter sync."""
    adapter_name: str
    profiles: list[SyncedProfile]
    error: str | None = None   # non-None means degraded

class ExternalCliSyncAdapter(ABC):
    # Class-level configurables (override in subclass)
    adapter_name: str                    # e.g. "aws-cli"
    sync_ttl_seconds: float = 60.0       # default for FileAdapter
    failure_threshold: int = 3           # circuit breaker trips after N consecutive failures
    reset_timeout_seconds: float = 60.0  # half-open probe after this duration

    @abstractmethod
    async def sync(self) -> SyncResult: ...

    @abstractmethod
    async def detect(self) -> bool: ...

    @abstractmethod
    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        """Fresh-read a credential for the given backend_key.

        Called by ExternalCliBackend.resolve(). Re-reads the source
        (file or subprocess) and extracts the actual secret for one profile.
        """
        ...
```

### `subprocess_adapter.py` — `SubprocessAdapter`

Concrete base for CLIs that need `asyncio.create_subprocess_exec`. Subclasses declare descriptors only.

```python
class SubprocessAdapter(ExternalCliSyncAdapter):
    binary_name: str               # e.g. "gcloud"
    status_args: tuple[str, ...]   # e.g. ("auth", "list", "--format=json")
    sync_ttl_seconds: float = 300.0  # subprocess = expensive, longer TTL
```

Base class handles:
- `detect()` — `shutil.which(binary_name)`
- `sync()` — `asyncio.create_subprocess_exec`, `asyncio.wait_for(5.0)`, stderr capture
- Failure classification into `AuthProfileFailureReason` (UPSTREAM_CLI_MISSING, TIMEOUT, AUTH, UNKNOWN)
- Exponential backoff retry: 3 attempts with 1s/2s/4s delays
- Subclass implements only `parse_output(stdout, stderr) -> list[SyncedProfile]`

### `file_adapter.py` — `FileAdapter`

Concrete base for CLIs with parseable config files.

```python
class FileAdapter(ExternalCliSyncAdapter):
    sync_ttl_seconds: float = 60.0  # file read = cheap, shorter TTL
```

Base class handles:
- `detect()` — any path from `paths()` exists
- `sync()` — read files in priority order, call `parse_file()`, aggregate results
- Error handling: missing file, unreadable perms, empty file, symlink loop — all classified as degraded (not exceptions)
- Subclass implements only `paths() -> list[Path]` and `parse_file(path, content) -> list[SyncedProfile]`

## AWS Adapter

### `aws_sync.py` — `AwsCliSyncAdapter(FileAdapter)`

~40 LOC descriptor-only subclass.

```python
class AwsCliSyncAdapter(FileAdapter):
    adapter_name = "aws-cli"
```

Behaviors:
- `paths()` returns `~/.aws/credentials` and `~/.aws/config` (respects `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE` env vars)
- `parse_file()` uses `configparser` on INI format
  - `~/.aws/credentials`: section names are profile names directly
  - `~/.aws/config`: section names are `profile <name>` (except `[default]`)
  - Returns one `SyncedProfile` per section containing `aws_access_key_id`
  - `backend_key` format: `"aws-cli/{profile_name}"`
- Merges profiles from both files (credentials takes precedence on conflict)
- Respects `AWS_PROFILE` env var for marking active profile
- Tolerates unknown INI keys (forward-compatible)
- Sections missing `aws_access_key_id` are skipped silently

## ExternalCliBackend

### `external_cli_backend.py` — `ExternalCliBackend(CredentialBackend)`

~80 LOC. Implements the `CredentialBackend` protocol from `credential_backend.py`.

```python
class ExternalCliBackend:
    _NAME = "external-cli"

    def __init__(self, registry: AdapterRegistry) -> None: ...

    async def resolve(self, backend_key: str) -> ResolvedCredential:
        # Parse: "aws-cli/default" → adapter_name="aws-cli", profile="default"
        # Get adapter from registry
        # FileAdapter: re-read + re-parse the file, extract credential for this profile
        # SubprocessAdapter: re-run the command, extract credential
        # Returns ResolvedCredential(kind="api_key", api_key=access_key, metadata={"secret_key": ...})

    async def health_check(self, backend_key: str) -> BackendHealth:
        # Non-destructive: check file/binary exists and profile section present
```

Key property: `resolve()` does a **fresh read** every call. No caching. The external credential file could change between calls (user runs `aws configure`, SSO token refresh). Matches the AWS `credential_process` contract.

Note: both `sync()` (on the adapter) and `resolve()` (on the backend) read the same files, but for different purposes. `sync()` extracts profile metadata (which accounts exist). `resolve()` extracts the actual credential (access key + secret key) for a specific profile. The adapter's `parse_file()` returns `SyncedProfile` (metadata); the backend's `resolve()` returns `ResolvedCredential` (secret). The adapter needs a second method — `resolve_credential(backend_key) -> ResolvedCredential` — that the backend delegates to. This keeps the fresh-read logic in the adapter (which knows the file format) while the backend stays format-agnostic.

## Registry

### `registry.py` — `AdapterRegistry`

~200 LOC. Manages adapter lifecycle, startup sync, and background refresh.

#### Circuit Breaker

```python
@dataclass
class CircuitBreaker:
    failure_count: int = 0
    tripped_at: float | None = None        # time.monotonic() timestamp
    failure_threshold: int = 3
    reset_timeout_seconds: float = 60.0

    @property
    def is_tripped(self) -> bool: ...      # tripped and not past reset_timeout

    @property
    def is_half_open(self) -> bool: ...    # tripped but past reset_timeout (allow probe)

    def record_success(self) -> None: ...  # reset to clean state
    def record_failure(self) -> None: ...  # increment, trip if threshold reached
```

Per-adapter circuit breaker. Configurable via adapter's `failure_threshold` and `reset_timeout_seconds` class attributes.

#### Startup (decision 15A)

```python
async def startup(self) -> dict[str, SyncResult]:
```

- All adapters run `detect()` + `sync()` concurrently via `asyncio.gather(return_exceptions=True)`
- Wrapped in `asyncio.wait_for(startup_timeout)` (default 3.0s)
- Adapters that miss the timeout get `SyncResult(error="timeout")`, marked "not yet synced"
- Successful results upserted into `AuthProfileStore` immediately
- Startup is bounded by the slowest adapter, not the sum

#### Background Refresh Loop

```python
async def run_refresh_loop(self) -> None:
```

Runs forever (cancel via `task.cancel()`). Every 30s tick:
1. Iterate registered adapters
2. Skip if circuit breaker is tripped (not half-open)
3. Skip if `last_synced_at + sync_ttl_seconds > now` (not stale)
4. Run `sync()`, upsert results into `AuthProfileStore`
5. On success: circuit breaker `record_success()`
6. On failure: circuit breaker `record_failure()`
7. If half-open probe fails: re-trip

#### Store Integration

`_upsert_sync_results()` maps `SyncedProfile` → `AuthProfile`:
- `backend = "external-cli"`
- `backend_key = "{adapter_name}/{account_identifier}"`
- `last_synced_at = now`
- `sync_ttl_seconds = adapter.sync_ttl_seconds`

Profiles in the store but NOT in the latest sync result are **kept** (not deleted). Prevents flapping when a config file is temporarily being edited. Stale profiles age out via `sync_ttl_seconds` expiry.

## `nexus-fs auth list` Cutover

Dual-read strategy: profile store primary, old `UnifiedAuthService` fallback.

```python
@auth.command("list")
def list_auth(output_opts: OutputOptions) -> None:
    profiles = _try_profile_store_list()

    if profiles is not None:
        # New table: Provider | Account | Source | Status | Last used
        ...
        return

    # Fallback: old UnifiedAuthService.list_summaries() path
    service = _build_auth_service()
    summaries = asyncio.run(service.list_summaries())
    # ... existing rendering unchanged ...
```

`_try_profile_store_list()`:
- Instantiates `SqliteAuthProfileStore` with standard DB path
- Returns `store.list()` if DB exists and has rows
- Returns `None` on any of: DB file doesn't exist, `store.list()` returns empty list, `ImportError` for `nexus.bricks.auth` (slim wheel), or any other exception → triggers fallback
- Lazy imports for slim `nexus-fs` compatibility

New table format:
```
Provider       Account                   Source       Status     Last used
s3             default                   aws-cli      ok         14m ago
s3             work-prod                 aws-cli      cooldown   rate_limit · 43m left
openai         team                      nexus        ok         2m ago
```

Status values: `ok`, `cooldown · {reason} · {time_left}`, `expired`, `disabled`, `not yet synced`

## S3 Backend Routing

Dual-path in `_backend_factory.py`:

```python
if spec.scheme == "s3":
    profile = _try_profile_store_select(provider="s3")
    if profile is not None:
        backend = _build_external_cli_backend()
        cred = asyncio.run(backend.resolve(profile.backend_key))
        return PathS3Backend(
            bucket_name=spec.authority,
            prefix=...,
            aws_access_key_id=cred.api_key,
            aws_secret_access_key=cred.metadata.get("secret_key"),
        )

    # Fallback: old discover_credentials() path
    discover_credentials(spec.scheme)
    return PathS3Backend(bucket_name=spec.authority, prefix=...)
```

- Old path untouched, wrapped in `else` branch
- No behavior change for users without populated profile store
- `PathS3Backend` may need minor change to accept explicit credential kwargs
- Old path removed in Phase 3/4

## Test Plan

### Base class tests via synthetic adapters

**SubprocessAdapter:**
- Hang timeout: subprocess sleeps 30s, `wait_for(5.0)` fires → degraded
- Stderr capture: subprocess writes stderr → captured in `SyncResult.error`
- Failure classification for each applicable `AuthProfileFailureReason`
- Exponential backoff: mock fails 2x then succeeds → 3 calls, 1s/2s gaps
- Circuit breaker trip: fail `failure_threshold` times → next `sync()` short-circuits

**FileAdapter:**
- Missing file → degraded, not exception
- Unreadable permissions → degraded with error message
- Empty file → empty profiles (not error)
- Malformed content → degraded with parse error
- Symlink loop → degraded (OSError caught)

### AWS adapter tests

**Fixtures:**
- `tests/fixtures/external_cli_output/aws_credentials_v2.15.ini` — standard format, multiple profiles
- `tests/fixtures/external_cli_output/aws_credentials_v2.16.ini` — newer format, SSO entries, session tokens

**Tests:**
- Parse every fixture → correct profiles extracted
- Merge credentials + config (credentials wins)
- `AWS_PROFILE` env var respected
- Unknown INI keys tolerated
- Section missing `aws_access_key_id` → skipped

### Registry integration tests

- **Startup timeout:** 5 mock adapters, 4 return ~50ms, 1 hangs 30s → gather returns 4 ok + 1 degraded within ~3.1s
- **Background loop:** mock adapter with 60s TTL, 30s tick → sync called only after TTL expiry
- **Circuit breaker:** fail N times → tripped → skip for reset_timeout → half-open → success resets

### Concurrency test

Two coroutines hit `profile_store.list(provider="s3")` while background loop upserts → no torn reads, no double-refresh. Uses `asyncio.Event` barriers.

### Offline safety (decision 12A)

`no_network` fixture on all external_sync tests. Every adapter returns degraded-but-useful within 2s with network blocked.

### Nightly real-binary e2e (decision 10A)

Gated by `TEST_WITH_REAL_AWS_CLI=1`:
- Temp HOME, `aws configure` with dummy creds
- Full stack: `AwsCliSyncAdapter` → `AdapterRegistry` → startup → `nexus-fs auth list`
- Assert AWS profile shows with `Source: aws-cli`, no auth-connect ceremony
- Skipped in default CI

## Non-goals (deferred)

- gcloud/gh/gws/codex adapters (Phase 3)
- `nexus auth list` cutover (Phase 4, with CLI unification)
- `auth doctor` overhaul (Phase 4)
- `--finalize` migration command (Phase 4)
- Deletion of old `discover_credentials()` path (Phase 3/4)
