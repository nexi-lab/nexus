# #4126 — Sandbox no longer bootstraps Raft federation (FIXED in this PR)

## The defect

`nexusd --profile sandbox` bootstrapped Raft federation and bound a gRPC
server on `0.0.0.0:2126` **even with `NEXUS_HOSTNAME` unset** — violating
the sandbox profile contract ("lightweight / no federation / no external
services"). Uncovered by the real-subprocess smoke test
`tests/integration/test_sandbox_boot_smoke.py` (#4126): the daemon boot log
contained `federation bootstrap complete`, `Starting Raft gRPC server`,
`ZoneManager node`, and `:2126`.

## Root cause

`rust/raft/src/distributed_coordinator.rs::install()` is the single
per-process chokepoint, called from the cdylib boot path
(`install_federation_wiring_py`, `rust/raft/src/pyo3_bindings.rs:866`). It
**unconditionally**:

1. `RaftDistributedCoordinator::new()`
2. `kernel.set_distributed_coordinator(real)` (replacing the kernel's
   default `NoopDistributedCoordinator`)
3. `coordinator.init_from_env(kernel)` — which derives a hostname from
   `NEXUS_HOSTNAME` **or falls back to the system `hostname`**, then binds
   the Raft gRPC server on `0.0.0.0:2126` and logs "federation bootstrap
   complete".

So even with `NEXUS_HOSTNAME` unset, `init_from_env`'s system-`hostname`
fallback still produced a hostname and started federation. The in-process
test `tests/integration/test_sandbox_boot.py::test_sandbox_boot_never_starts_federation`
(#3778) assumed "`NEXUS_HOSTNAME` unset → no-op", which was wrong on the
real daemon path.

## The fix (minimal, single-chokepoint, default-unset = byte-identical)

**Rust** — `rust/raft/src/distributed_coordinator.rs`, top of
`pub fn install(kernel: &Arc<Kernel>)`: an explicit kill-switch that
early-returns `Ok(())` when `NEXUS_FEDERATION_DISABLED` is `1`/`true`,
*before* `RaftDistributedCoordinator::new()`. The real coordinator is never
installed and `init_from_env` never runs → no ZoneManager, no `:2126`, no
bootstrap; the kernel keeps its default `NoopDistributedCoordinator`. When
the var is unset (cluster/full/default) the branch is skipped — behavior is
byte-identical to before.

**Python** — `src/nexus/daemon/main.py` (before `nexus.connect(...)`):

```python
if deployment_profile == "sandbox" and not any(
    os.environ.get(v)
    for v in ("NEXUS_PEERS", "NEXUS_HOSTNAME", "NEXUS_BOOTSTRAP_NEW")
):
    os.environ.setdefault("NEXUS_FEDERATION_DISABLED", "1")
```

Scoped **strictly** to `deployment_profile == "sandbox"`. `setdefault` plus
the `NEXUS_PEERS/HOSTNAME/BOOTSTRAP_NEW` guard preserve any deliberate
operator override (someone explicitly wanting zone federation in sandbox).

## Zero-regression argument (cluster / full)

- `NEXUS_FEDERATION_DISABLED` is **set in exactly one place** —
  `src/nexus/daemon/main.py`, gated on `deployment_profile == "sandbox"`.
  Verified by repo-wide grep: no other set site in `src/` or `rust/`.
- cluster / full / lite / embedded never set it → `install()` takes the
  unchanged default path → byte-identical behavior.
- The only reader is `distributed_coordinator.rs::install()`; the guard is
  a pure early-return that does nothing when the var is unset.

## `--hub-url` hub federation is unaffected

Sandbox `--hub-url` hub federation uses a **separate** path
(`SandboxBootstrapper`, `src/nexus/daemon/main.py:~427`), NOT the Raft
distributed coordinator (#4130). Disabling the local Raft coordinator does
not touch it. The Python guard also excludes `NEXUS_HOSTNAME`/`NEXUS_PEERS`
so a deliberate operator opt-in to zone federation is preserved.

## Regression guard

`tests/integration/test_sandbox_boot_smoke.py::test_sandbox_does_not_bootstrap_federation_or_raft`
asserts the daemon boot log contains none of `federation bootstrap
complete` / `Starting Raft gRPC server` / `ZoneManager node` / `:2126`, and
that the daemon is still healthy (`/health` 200). Demonstrated to FAIL
pre-fix (markers present when the guard is neutralized via
`NEXUS_FEDERATION_DISABLED=0`) and PASS post-fix.
