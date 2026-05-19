# build(cli): add `deployment_profile` + `auth_mode` to `nexus status --json`

**Parent / Epic**: Parent: #4132 · Epic: #4121 · gaps.yaml id: `status.auth_profile_detail`

**Priority**: RECOMMENDED — not a hard blocker for #4132 closure, but improves operator experience.

## Missing user workflow

An operator running `nexus status` cannot confirm which deployment profile the hub is running under, nor which auth mode is active. The current `nexus status --json` output does not include `deployment_profile` or `auth_mode` keys. This forces the operator to cross-reference the daemon startup banner or environment variables manually.

For example, after starting a FULL-profile hub with `--auth-type database`, there is no way to confirm both facts from a single `nexus status --json` call.

## Proposed surface

No new sub-command required. Extend the existing `nexus status --json` output with two additive keys:

```
nexus status --json
```

This is a purely additive, backward-compatible change to the existing command.

## Request/response shape

**Input**: existing command, no new flags required.

**Output** — existing keys unchanged; the following keys are added:

```json
{
  "...existing keys...": "...",
  "deployment_profile": "full",
  "auth_mode": "database"
}
```

`auth_mode` values: `"static"` (API-key from env/file) or `"database"` (`--auth-type database` / `DatabaseAPIKeyAuth`).

`deployment_profile` values: any valid `DeploymentProfile` name (`"sandbox"`, `"lite"`, `"full"`, etc.).

## Tests required before docs claim support

1. **`nexus status --json` schema test**: assert that the response includes `deployment_profile` and `auth_mode` keys with the correct values for the active fixture hub.
2. **Parity with daemon banner**: the `deployment_profile` value in `nexus status --json` matches the value logged in the daemon startup banner.

## Benchmark

Not performance-sensitive (control plane, status check). No latency gate required.

## Source anchors

- `src/nexus/daemon/main.py` — daemon startup banner (source of truth for profile/auth at boot)
- `src/nexus/cli/commands/stack.py` — existing `nexus status` implementation
- `src/nexus/contracts/deployment_profile.py` — `DeploymentProfile` enum
- `docs/architecture/api-rpc-surface-gaps.yaml` — entry `status.auth_profile_detail`

---

> Drafted by issue #4132 work; not yet filed. Filing requires maintainer approval.
