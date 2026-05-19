# Shared Daemon

Nexus = filesystem/context plane.

Choose this path when Nexus needs to run as a service instead of as an in-process library.

## What Changes

- You start `nexusd` as the server process.
- Remote SDK clients use the `remote` profile.
- The remote SDK path depends on gRPC as well as the HTTP server URL.

## Server

Run the daemon directly (supported copy-paste workflow):

```bash
export NEXUS_GRPC_PORT=2126
nexusd --profile full --host 127.0.0.1 --port 2026 \
  --data-dir ./nexus-data --auth-type static --api-key dev-key
```

The managed stack (FULL profile: PostgreSQL + Dragonfly + the Nexus
server; Zoekt is optional and separately run) is `nexus init --preset
shared` then `nexus up`, then `eval $(nexus env)`.

> **Known issue (Bug B, tracked):** do **not** chain these with `&&` —
> `nexus up --preset shared` currently exits non-zero because its
> health gate waits on a `zoekt` service the preset does not start, so
> a `&&`-chained `eval $(nexus env)` would never run. **The hub itself
> boots and serves correctly**; only the `nexus up` wrapper's exit
> status is wrong. Prefer the direct `nexusd` command above until the
> out-of-scope `nexus up` fix lands. See
> [FULL deployment profile](../deployment/full-profile.md) ("Known
> issue").

> `minimal` is not a deployment profile. Valid profiles: `embedded`,
> `lite`, `sandbox`, `full`, `cloud`, `cluster`, `remote` (and `remote`
> cannot run as a daemon). See
> [FULL deployment profile](../deployment/full-profile.md).

## Client

```python
from nexus.sdk import connect

nx = connect(
    config={
        "profile": "remote",
        "url": "http://127.0.0.1:2026",
        "api_key": "dev-key",
    }
)
```

Set `NEXUS_GRPC_PORT` in the client environment if the server is not using the default gRPC port expected by the SDK.

## Trust Notes

- The HTTP port alone is not enough for the current remote SDK path.
- Documented remote access should always mention the gRPC requirement explicitly.
- If you only need local development, start with the [Local SDK path](embedded-sdk.md).
