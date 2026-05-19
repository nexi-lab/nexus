# build(remote): remote-connect preflight (`nexus doctor remote` / SDK preflight)

**Parent / Epic**: Parent: #4132 · Epic: #4121 · gaps.yaml id: `remote.connect_preflight`

**Priority**: REQUIRED — issue #4132 cannot close while this gap is untracked.

## Missing user workflow

A remote client that has HTTP reachable but gRPC blocked currently receives a deep stack trace rather than an actionable error. The remote SDK `connect()` call requires gRPC to be reachable (`NEXUS_GRPC_PORT`); the HTTP URL alone is insufficient. Without a preflight step, the operator has no way to distinguish "hub unreachable" from "gRPC port blocked by firewall" without reading the stack trace and guessing.

This is especially problematic in the FULL profile remote-client workflow documented by #4132, where the guide asserts: "Remote SDK with no reachable gRPC → explicit failure." That assertion is only meaningful if the failure is actionable.

## Proposed surface

**CLI form**:

```
nexus doctor remote --url <HUB_URL> --api-key <KEY>
```

or as a sub-command alias: `nexus doctor remote`.

**SDK form**:

```python
connect(config={"profile": "remote", "url": ..., "api_key": ...}, preflight=True)
```

The surface probes both HTTP and gRPC reachability before attempting a full connection, and returns a structured diagnosis.

## Request/response shape

**Input**:
- `--url <HUB_URL>` — base HTTP URL of the hub
- `--api-key <KEY>` (or `NEXUS_API_KEY` env var) — API key for the health probe

**Output on success** (exit 0):

```
HTTP  <url>/api/v2/health  OK (200)
gRPC  <host>:<port>         OK (reachable)
Preflight passed. Remote connection is ready.
```

**Output on failure** (non-zero exit):

```
HTTP  <url>/api/v2/health  OK (200)
gRPC  <host>:<port>         UNREACHABLE
Error: gRPC port <N> unreachable; set NEXUS_GRPC_PORT to the correct port and ensure the port is open in your firewall.
```

With `--json` flag, structured output:

```json
{
  "http_ok": true,
  "grpc_ok": false,
  "grpc_host": "hub.example.com",
  "grpc_port": 50051,
  "error": "gRPC port 50051 unreachable; set NEXUS_GRPC_PORT"
}
```

## Tests required before docs claim support

1. **Unit — probe logic with mocked sockets**: inject a mock where HTTP succeeds and gRPC times out; assert exit code non-zero and error message contains "gRPC port … unreachable; set NEXUS_GRPC_PORT".
2. **CLI failure-path test**: `nexus doctor remote --url <stub> --api-key <key>` with gRPC port closed → non-zero exit, actionable message in stderr/stdout.

## Benchmark

Not performance-sensitive (setup path, called once per new remote environment configuration). No latency gate required.

## Why required

The documented remote workflow for FULL (spec §Profile-contract assertions, point 5) states "Remote SDK with no reachable gRPC → explicit failure." That requirement is only satisfiable if there is a user-runnable command or SDK option that surfaces the failure clearly. Without this surface, the guide can document the error scenario but cannot tell the operator how to diagnose it. Issue #4132 is gated on this gap being tracked.

## Source anchors

- `src/nexus/daemon/main.py` — gRPC port configuration (`NEXUS_GRPC_PORT`)
- `docs/paths/daemon-and-remote.md` — stale remote-client doc (gRPC requirement currently omitted)
- Remote SDK `connect()` — current behaviour on gRPC failure (deep stack trace)
- `docs/architecture/api-rpc-surface-gaps.yaml` — entry `remote.connect_preflight`

---

> Drafted by issue #4132 work; not yet filed. Filing requires maintainer approval.
