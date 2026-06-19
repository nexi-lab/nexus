# #4148 resolution — typed VFS gRPC `Ping` in sandbox (evidence)

**Prepared for a human to post on #4148. Do not auto-post.**

## Summary / recommendation

Close #4148 as **not reproducible in the sandbox profile**, or reclassify it
as a **cluster-only feature request**. The reported "sandbox typed VFS gRPC
`Ping` returns UNAUTHENTICATED" does not occur because **the sandbox profile
never binds a typed VFS gRPC server at all** — there is nothing to return
`UNAUTHENTICATED` (or anything else). This is an architectural property, not
an auth bug.

## Reproduction

```bash
# Boot a real sandbox daemon (isolated HOME + data dir), free HTTP port P.
python -m nexus.daemon.main --profile sandbox \
  --workspace /tmp/ws --host 127.0.0.1 --port P \
  --data-dir /tmp/data

# Once ~/.nexus/nexusd.ready exists, the typed VFS gRPC would (per #4148)
# be on http_port + 2:
grpcurl -plaintext 127.0.0.1:$((P+2)) nexus.grpc.vfs.NexusVFSService/Ping
```

## Observed result

- HTTP surface is healthy: `/health` → 200, `/api/v2/features` →
  `profile=sandbox`.
- The typed VFS gRPC port (`http_port + 2`, e.g. 43102 for HTTP 43100) is
  **connection-refused for the daemon's entire lifetime** (~20 s observed and
  beyond) while the daemon stays healthy.
- Historically the boot log showed a **Raft/federation gRPC on the fixed
  port `:2126`** — a different surface, not VFS, not at `http_port + 2`, and
  with no `Ping`. As of #4126 the sandbox profile no longer starts that
  Raft listener at all (profile-gated via `NEXUS_FEDERATION_DISABLED`; see
  `sandbox-federation-fix.md`). Either way, no VFS gRPC is bound.
- No VFS gRPC server is ever bound, so there is no servicer to return
  `UNAUTHENTICATED`.

## Code root cause

- The typed VFS gRPC service `NexusVfsService` (serves `Ping`/`Read`/`Write`)
  is defined in `rust/transport/src/grpc.rs`.
- Its server has **exactly one spawn call site in the entire repository**:
  `rust/profiles/cluster/src/main.rs:422` — the **cluster** profile binary.
  No call site exists anywhere in the sandbox path (`rust/`, `src/nexus/`).
- The only Python gRPC server is the env-gated approvals brick
  (`src/nexus/server/lifespan/approvals.py:317`), which is **not part of the
  sandbox profile** and has **no `Ping`**.
- The sandbox profile formerly started a Raft/federation gRPC
  (`rust/raft/src/transport/server.rs`) on the fixed port `:2126`; this is
  removed by #4126 (kill-switch in `distributed_coordinator.rs::install()`).
  Neither before nor after #4126 is any VFS gRPC bound in sandbox.

Conclusion: the typed VFS gRPC `Ping` is **unavailable in the sandbox
profile by architecture (cluster-profile-only)**. #4148's UNAUTHENTICATED
scenario cannot reproduce there because no VFS gRPC server is bound.

## Recommendation

1. Close #4148 as not-reproducible in sandbox, **or** reclassify it as a
   cluster-only feature request ("expose typed VFS gRPC under the sandbox
   profile") — a product decision, not a bug fix.
2. The contract is now locked by a real (non-skipped) regression test:
   `tests/integration/test_sandbox_boot_smoke.py::test_sandbox_does_not_bind_typed_vfs_grpc`
   asserts the sandbox VFS gRPC port never becomes ready and a `Ping`
   attempt fails `UNAVAILABLE`. If a future change makes sandbox bind the
   typed VFS gRPC server, that test fails — the intended regression signal.
