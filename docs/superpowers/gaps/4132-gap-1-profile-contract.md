# build(cli): `nexus profile contract` — print resolved deployment-profile contract

**Parent / Epic**: Parent: #4132 · Epic: #4121 · gaps.yaml id: `profile.contract_cli`

**Priority**: REQUIRED — issue #4132 cannot close while this gap is untracked.

## Missing user workflow

An operator wants to verify, without reading source code, which bricks, drivers, and auth mode the running hub actually has. The existing `nexus profile …` sub-commands manage *connection* profiles (kubectl-style URL/key/zone entries in `~/.nexus/config.yaml`), not the deployment profile (`DeploymentProfile`, set via `nexusd --profile` or `NEXUS_PROFILE`). There is currently no user-runnable command that prints the resolved deployment-profile contract.

This matters for the FULL profile in particular: the user-guide correctness assertion states that `DeploymentProfile.FULL` includes a specific set of bricks and drivers and excludes `federation`. Without a CLI surface, the operator must read `src/nexus/contracts/deployment_profile.py` directly to verify this.

## Proposed surface

```
nexus profile contract
```

or, as an alternative form:

```
nexus status --profile-contract
```

Uses the active connection (no extra arguments required).

## Request/response shape

**Input**: no positional arguments; reads active connection from `~/.nexus/config.yaml`.

**Output** (JSON, exit 0):

```json
{
  "deployment_profile": "full",
  "bricks": ["search", "pay", "llm", "skills", "sandbox", "..."],
  "drivers": ["s3", "gcs", "gdrive", "gmail", "slack", "x", "hn", "remote", "..."],
  "http_surface": ["/api/v2/health", "/api/v2/features", "/api/v2/..."],
  "grpc_required": true,
  "auth_mode": "database"
}
```

On error (hub unreachable): non-zero exit + human-readable message.

## Tests required before docs claim support

1. **Unit — serialization from `DeploymentProfile`**: given a `DeploymentProfile.FULL` instance, the serializer emits all expected brick/driver keys and excludes `federation`.
2. **CLI snapshot test**: `nexus profile contract` against a running fixture hub produces output matching the expected JSON schema.
3. **Parity test vs `/api/v2/features`**: the `bricks[]` list returned by `nexus profile contract` matches the feature flags reported by the features endpoint.

## Benchmark

Not performance-sensitive (control plane, called at most a handful of times per operator session). No latency gate required.

## Why required

The FULL profile user-guide correctness assertion (spec §Profile-contract assertions, point 1) depends on this being a user-runnable command. Until this surface exists, the guide cannot tell operators *how* to verify the contract — it can only describe it in prose, leaving the claim untestable from a user perspective. Issue #4132 is gated on this gap being tracked.

## Source anchors

- `src/nexus/contracts/deployment_profile.py` — `DeploymentProfile.FULL`
- `src/nexus/cli/commands/` — existing `profile` sub-commands
- `/api/v2/features` — HTTP endpoint for parity assertion

---

> Drafted by issue #4132 work; not yet filed. Filing requires maintainer approval.
