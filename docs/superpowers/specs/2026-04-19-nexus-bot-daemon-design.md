# `nexus-bot` daemon + connector consumption (issue #3804)

Phases D + E of epic #3788. PR 3 of 3. Builds the local daemon that syncs laptop-local CLI credentials into the multi-tenant Postgres store (#3802) through the envelope encryption layer (#3803 / #3809), and rewires connector reads so they continue to work whether the daemon is running or not.

## Scope

Vertical slice: ship one source (`~/.codex/auth.json`) end-to-end with one platform (macOS launchd), one join mode (bound-keypair), and a stub for RFC 8693 token exchange. Other sources, platforms, and the OIDC device-code join path land in follow-ups. The goal of this PR is to prove the full path from laptop source file → encrypted push → Postgres row → connector read, with a smallest-possible surface that can be reviewed.

Explicitly out of scope (tracked, not built):

- `gcloud`, `gh`, `gws` source adapters. Registry has placeholder stubs so follow-ups are local.
- Linux `systemd-user` unit. `installer.py` raises `NotImplementedError` on non-darwin with a clear message.
- Windows service packaging. Issue defers.
- RFC 8693 token-exchange implementation. Route + schema land as flag-gated `501 Not Implemented` so client code can be written against the contract.
- OIDC device-code join for humans. Bound-keypair only; second join mode lands later.
- Append-only `auth_profile_writes` audit log. Stamps on `auth_profiles` cover the acceptance criterion; append-only is a richer pattern deferred.
- JWT cache encrypted at rest via OS Keychain / Secret Service. MVP writes plaintext JWT at `~/.nexus/daemon/jwt.cache` mode `0600`.
- JWKS endpoint. Verification uses the `server_pubkey_pem` pinned at join time; rotation lives with the OIDC follow-up.
- PID-file freshness hint in the connector read path. Staleness during daemon downtime is a degradation, not a correctness bug. Deferred.

## Architecture

```
┌─────────────────────────────── LAPTOP ───────────────────────────────┐
│                                                                      │
│  ~/.codex/auth.json ──► [watchdog fsnotify] ──► nexus-bot daemon     │
│                                                  │                   │
│                                                  │ writes            │
│                                                  ▼                   │
│  ~/.nexus/auth_profiles.db  ◄── [SqliteAuthProfileStore]             │
│           ▲                                       │ pushes           │
│           │ reads                                 │ (JWT auth,       │
│  PathCLIBackend._resolve_from_external_cli        │  envelope        │
│  (unchanged — already reads from SQLite store)    │  encrypted)      │
│                                                   ▼                  │
└───────────────────────────────────────────────────┼──────────────────┘
                                                    │
                                      POST /v1/auth-profiles
                                      POST /v1/daemon/{enroll,refresh}
                                      POST /v1/auth/token-exchange (501)
                                                    │
┌───────────────────────────────────────────────────┼──────────────────┐
│                          SERVER (FastAPI)         ▼                  │
│                                                                      │
│  v1 routers  ──► JWT verify (ES256)  ──► PostgresAuthProfileStore    │
│                  + machine_id claim       (RLS by tenant_id)         │
│                                                                      │
│  tables: tenants, principals, principal_aliases, auth_profiles       │
│          (+ new: daemon_machines, daemon_enroll_tokens)              │
│          (+ new cols on auth_profiles: source_file_hash,             │
│           daemon_version, machine_id)                                │
└──────────────────────────────────────────────────────────────────────┘
```

Key moves:

- The daemon writes **through** the existing local `SqliteAuthProfileStore`. No new IPC, no new cache format. Connectors keep reading SQLite — the daemon just keeps it fresh from upstream source files.
- Every server write is envelope-encrypted client-side using the `EncryptionProvider` layer from #3809. The server never sees plaintext credentials.
- Server adds a new `/v1` router namespace. The existing `/api/v2` auth routes are untouched.
- SQLite remains authoritative when the daemon is off: the store is always the read path; staleness is the only degradation.
- Vertical slice = codex source only. Other adapters (`gcloud`, `gh`, `gws`) have placeholder registry entries that return "not-watched" — wired in follow-ups.

## Components

### Daemon side (`src/nexus/bricks/auth/daemon/`, new package)

| File | Purpose |
|---|---|
| `__init__.py` | Package exports. |
| `cli.py` | Click subgroup `nexus daemon {join,run,install,uninstall,status}`. Wired from top-level `cli_commands.py`. |
| `config.py` | `DaemonConfig` dataclass + TOML load/save for `~/.nexus/daemon.toml`. |
| `keystore.py` | Ed25519 keypair generate/load, file perms `0600` enforced, sign-request helper. |
| `jwt_client.py` | JWT fetch + renewal loop. Caches current JWT in memory + `~/.nexus/daemon/jwt.cache`. Schedules renewal at 75% of TTL. |
| `watcher.py` | `watchdog.Observer` on `~/.codex/auth.json`. Debounced 500 ms callback → `on_source_changed`. |
| `push.py` | `push_profile(source, content_bytes) → PushResult`: compute `source_file_hash`, skip if unchanged, envelope-encrypt via `EncryptionProvider`, write to local `SqliteAuthProfileStore`, `POST` to server with JWT, mark dirty on push failure. Background retry loop drains dirty rows. |
| `runner.py` | `DaemonRunner.run()`: orchestrates JWT client + watcher + push retry loop. Handles `SIGTERM` graceful shutdown. |
| `installer.py` | macOS: render launchd plist from template, `launchctl bootstrap gui/<uid>`. Uninstall reverses. Other platforms raise `NotImplementedError`. |

### Server side (`src/nexus/server/api/v1/`, new subtree)

| File | Purpose |
|---|---|
| `routers/daemon.py` | `POST /v1/daemon/enroll` (accepts enroll-token + machine pubkey, issues JWT, records `daemon_machines`), `POST /v1/daemon/refresh` (accepts machine-signed request, issues fresh JWT). |
| `routers/auth_profiles.py` | `POST /v1/auth-profiles` (JWT-auth, envelope payload → `PostgresAuthProfileStore.upsert_with_credential` with audit stamps). |
| `routers/token_exchange.py` | `POST /v1/auth/token-exchange` — feature-flagged `501` stub with documented request/response schema. |
| `jwt_signer.py` | ES256 key load + sign/verify helpers (PyJWT). Fixture-generated ephemeral key for tests. |
| `enroll_tokens.py` | HMAC-signed JTI format; single-use check against `daemon_enroll_tokens`. |

### Schema extensions (`postgres_profile_store.py::ensure_schema`, idempotent)

- `auth_profiles`: new columns `source_file_hash TEXT NULL`, `daemon_version TEXT NULL`, `machine_id UUID NULL`.
- New table `daemon_machines(id UUID PK, tenant_id, principal_id, pubkey BYTEA, daemon_version_last_seen TEXT, enrolled_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ NULL)` + RLS by `tenant_id`.
- New table `daemon_enroll_tokens(jti UUID PK, tenant_id, principal_id, issued_at TIMESTAMPTZ, expires_at TIMESTAMPTZ, used_at TIMESTAMPTZ NULL)` + RLS by `tenant_id`.

All migrations are `ALTER TABLE … ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` following the style already established in `postgres_profile_store.py:433-436`.

### Admin CLI (`src/nexus/bricks/auth/cli_commands.py`, add subcommand)

- `nexus auth enroll-token --tenant <t> --principal <p> --ttl 15m` → prints HMAC-signed token. Single-use; server records used tokens in `daemon_enroll_tokens`.

## Data flow

### A. One-time join

```
1. Admin:  nexus auth enroll-token --tenant acme --principal alice@acme.com --ttl 15m
            → prints ENROLL_TOKEN (HMAC-signed JTI, 15 min TTL,
              stored in daemon_enroll_tokens)

2. User:   nexus daemon join --server https://nexus.acme --enroll-token ENROLL_TOKEN
            ├─ generates Ed25519 keypair at ~/.nexus/daemon/machine.key (0600)
            ├─ POST /v1/daemon/enroll { enroll_token, pubkey_pem,
            │                           daemon_version, hostname }
            │    └─ server: verify enroll-token HMAC + not-expired + not-used
            │              → mark used_at, insert daemon_machines row
            │              → mint JWT (1h), return { machine_id, jwt,
            │                                        server_pubkey_pem }
            └─ writes ~/.nexus/daemon.toml { server_url, tenant_id, principal_id,
                                             machine_id, key_path, jwt_cache_path,
                                             server_pubkey_path }
```

### B. JWT renewal loop (background task)

```
every (jwt.exp - now) * 0.75 seconds:
    body = { machine_id, timestamp_utc }
    sig  = ed25519.sign(machine_privkey, canonical_json(body))
    POST /v1/daemon/refresh { body, sig }
        └─ server: load daemon_machines.pubkey by machine_id
                   → verify sig, check timestamp skew ±60s, check not-revoked
                   → mint fresh JWT, update last_seen_at
    cache JWT (memory + ~/.nexus/daemon/jwt.cache)
```

### C. Source → push

```
on_source_changed(path=~/.codex/auth.json):
    bytes    = read_file(path)
    new_hash = sha256(bytes)
    cached   = SqliteAuthProfileStore.get(source="codex")
    if cached and cached.source_file_hash == new_hash: return  # no-op dedupe

    envelope = EncryptionProvider.encrypt(bytes)  # reuses #3809
    profile  = AuthProfile(source="codex", envelope=envelope,
                           source_file_hash=new_hash,
                           daemon_version=__version__,
                           machine_id=config.machine_id,
                           updated_at=now())

    SqliteAuthProfileStore.upsert(profile, mark_dirty=True)  # local first (offline-safe)

    try:
        POST /v1/auth-profiles { profile_payload }  with Authorization: Bearer <jwt>
            └─ server: JWT verify → extract (tenant_id, principal_id, machine_id)
                       → envelope pass-through (server stores ciphertext,
                         never decrypts here)
                       → PostgresAuthProfileStore.upsert_with_credential(...)
                       → conflict-check (advisory — still writes):
                           if incoming.source_file_hash != server.current_hash
                              AND incoming.updated_at < server.updated_at:
                                  log structured warning "push_conflict_stale_write"
                       → upsert row (last-write wins on updated_at)
                       → 200 OK
        SqliteAuthProfileStore.clear_dirty(source="codex")
    except (network, 5xx):
        # stays dirty; drain loop retries with exponential backoff (1s→60s cap)
```

### D. Offline read (connector path)

Unchanged. `PathCLIBackend._resolve_from_external_cli` → `_external_sync_boot.resolve_token_for_provider` → `SqliteAuthProfileStore.get` → decrypt envelope locally → return token. Daemon absent ⇒ SQLite may be stale, not wrong.

### E. Startup drain

```
on DaemonRunner.start():
    dirty_rows = SqliteAuthProfileStore.list_dirty()
    for row in dirty_rows:
        enqueue_push(row)  # same push path as C, retry loop drains
```

## Error handling

### Classification

| Error class | Example | Policy |
|---|---|---|
| **Transient network** | conn refused, DNS fail, 5xx, timeout | Local write succeeds + `dirty=true`. Retry loop: exp backoff 1s→2s→4s→…→60s cap. Metric `daemon_push_retry_total`. |
| **Auth stale** | 401 on push (JWT expired despite renewal) | Force JWT refresh. If refresh also 401 → machine revoked; daemon enters degraded mode (local writes continue, push disabled until `nexus daemon join` re-run). No auto-retry on auth failure. |
| **Permanent rejection** | 400 (malformed envelope), 409 (DB constraint violation — NOT stale-write; stale-write is advisory and always writes) | Log structured error with full context; mark row `push_error=<reason>`; do not retry. `nexus daemon status` surfaces it. |
| **Local failure** | watcher exception, disk full, corrupt source file | Watcher logs + continues. Unparseable source file ⇒ skip push this event. Source-file-deleted ⇒ log, do not wipe SQLite row (credentials remain usable until explicit invalidate). |

### Cryptographic failures (fatal to the affected write, not the daemon)

- `EncryptionProvider.encrypt` failure → log, abort this push, continue watching. No partial writes.
- JWT signature verify failure on server → `401` to daemon; daemon treats as auth-stale.
- Ed25519 signature verify failure on `/v1/daemon/refresh` → `401 signature_invalid`. Daemon does not auto-retry (indicates clock skew or key mismatch); surfaces via `daemon status`.

### Enroll-token replay / tampering

- JTI already in `daemon_enroll_tokens.used_at` → `409 enroll_token_reused`.
- HMAC verify fail → `401 enroll_token_invalid`.
- Expired → `401 enroll_token_expired`.
- All three logged with source IP for audit.

### Clock skew

Refresh endpoint enforces `|server_now - request_timestamp| ≤ 60s`. On skew rejection, daemon logs `clock_skew_detected` + the delta; operator issue, no auto-recovery.

### Degraded mode (explicit)

- Daemon sets `status="degraded"` when repeated push failures exceed 10 min OR machine revoked.
- `nexus daemon status` prints current mode + last-success timestamp + dirty-row count.
- Exit code of `status`: `0` healthy, `1` degraded, `2` stopped.

### Shutdown

- `SIGTERM`: watcher stops immediately, one final drain pass (5 s budget), remaining dirty rows stay on disk for next start, JWT cache flushed. launchd `KeepAlive=true` restarts cleanly.

### Unsafe-by-design refusals

- `~/.codex/auth.json` absent on first run → daemon logs `source_absent` and runs idle. Not an error. First write appears on watcher event.
- `~/.nexus/daemon/machine.key` missing → daemon refuses to start; tells operator to run `nexus daemon join`. Does not regenerate (would silently break enrollment state).
- `~/.nexus/daemon.toml` corrupt → daemon refuses to start; no auto-recovery.

## Testing

### Unit — daemon (`src/nexus/bricks/auth/daemon/tests/`)

| File | Covers |
|---|---|
| `test_config.py` | `DaemonConfig` TOML round-trip, corrupt-file refusal, default paths. |
| `test_keystore.py` | Ed25519 generate/load, `0600` perms enforced, sign-verify round-trip, missing-key refusal. |
| `test_jwt_client.py` | Renewal scheduling at 75% TTL, cache read/write, `401` → forced refresh → `401` → degraded mode. Mock HTTP with `respx`. |
| `test_watcher.py` | fsnotify event → debounced callback fires once within 500 ms window. Uses `tmp_path` + real file writes + `watchdog` in polling mode for test determinism. |
| `test_push.py` | Hash-dedupe skips no-op, envelope roundtrip, dirty-on-fail + clear-on-success, retry backoff schedule. Mock `EncryptionProvider` + HTTP. |
| `test_runner.py` | `SIGTERM` graceful shutdown, startup-drain replays dirty rows, degraded-mode transitions. |
| `test_installer.py` | macOS plist snapshot; skipped on non-darwin via `pytest.mark.skipif`. |

### Unit — server (`src/nexus/server/api/v1/tests/`)

| File | Covers |
|---|---|
| `test_enroll_tokens.py` | HMAC sign/verify, single-use replay rejection, TTL, tamper detection. |
| `test_jwt_signer.py` | ES256 sign/verify, claims shape, clock-skew rejection on verify. |
| `test_daemon_router.py` | `/v1/daemon/enroll` happy path + each rejection reason; `/v1/daemon/refresh` signature verify + skew rejection + revoked-machine path. FastAPI `TestClient`. |
| `test_auth_profiles_router.py` | `/v1/auth-profiles` JWT auth gate, envelope pass-through, audit stamps populated, conflict-warning log. |
| `test_token_exchange_router.py` | Flag-off returns `501` with schema; flag-on placeholder exists (returns same `501` until implemented). |

### Integration (`tests/integration/auth/test_daemon_e2e.py`)

Requires live Postgres via existing `TEST_POSTGRES_URL` fixture from #3802.

1. **End-to-end happy path**: mint enroll-token via admin CLI → daemon join → verify `daemon_machines` row → write fake `~/.codex/auth.json` (into `tmp_path`, env-override) → observe push within 2 s → verify `auth_profiles` row with correct envelope + audit stamps → connector path (`SqliteAuthProfileStore.get`) returns the credential.
2. **Offline resilience**: start daemon → write source file → stop server (close httpx transport) → write source file again → verify local SQLite updated + `dirty=true` → restart server → verify retry loop drains within backoff window → verify Postgres matches.
3. **JWT renewal**: freeze clock, advance past 75% of TTL, verify renewal request signed correctly, verify new JWT in use on next push.
4. **Revocation**: set `daemon_machines.revoked_at` → next refresh returns `401` → daemon enters degraded mode → `status` exit code = `1`.
5. **Enroll-token replay**: use token once → second use returns `409` → daemon `join` fails cleanly.
6. **Hash dedupe**: write identical content twice → exactly one push observed.

### Security regression (`tests/integration/auth/test_daemon_security.py`)

- Server write path rejects payloads missing audit stamps.
- RLS blocks cross-tenant reads (daemon with tenant A's JWT cannot write to tenant B's row — verified via direct DB query bypassing app filter).
- `~/.nexus/daemon/machine.key` perms verified `0600` post-`daemon join`.

### Manual validation (documented, not run in CI)

- `nexus daemon install` on a real macOS box → `launchctl list | grep com.nexus` shows loaded → restart box → daemon auto-starts → write to `~/.codex/auth.json` → verify Postgres updated.

## Acceptance-criterion coverage

Mapping back to issue #3804:

| Criterion | Covered by |
|---|---|
| `nexus daemon join` interactive + non-interactive | Bound-keypair path; OIDC is the deferred second mode. |
| `nexus daemon run` loop: renews JWT before expiry, survives central restart | JWT renewal loop + offline resilience test. |
| Source-file change → visible in Postgres within `sync_ttl_seconds` | Watcher + push; integration test asserts within 2 s. |
| Offline mode: disconnect daemon, connector reads stay green against local cache | Data flow D + integration test 2. |
| Token exchange: server requests delegated credential | Route + schema stub behind flag; full implementation deferred. |
| Conflict-resolution rule documented and tested | Error-handling section; structured log + integration test. |
| `source_file_hash` + `daemon_version` + attested `machine_id` stamped on every central write | Schema extensions + `test_auth_profiles_router.py`. |

## Open questions (resolved during plan writing, not before)

1. **Existing deps**: is `watchdog` pinned? PyJWT and `cryptography` Ed25519 likely are (#3809 brought `cryptography`). Plan step verifies and adds a `pyproject.toml` bump if needed.
2. **Server `/v1` router registration**: locate the `include_router` call in `fastapi_server.py` and match the pattern.
3. **Test Postgres fixture location**: confirm the `pg_engine` fixture added by #3802 is importable from `tests/integration/auth/`.
4. **Daemon version string**: use `nexus.__version__` for MVP. If the daemon later ships independently, split the symbol.
5. **Plist template shape**: ship as package resource via `importlib.resources` (cleaner than an embedded triple-quoted string).
