# Issue #3872 - Remote Hub Admin CLI Design

**Date**: 2026-05-04
**Issue**: [#3872](https://github.com/nexi-lab/nexus/issues/3872) - feat: remote admin CLI for hub mode
**Follow-up to**: [#3784](https://github.com/nexi-lab/nexus/issues/3784) - hub mode

## Context

The current `nexus hub token` CLI is a local operator tool. It uses
`NEXUS_DATABASE_URL` through `src/nexus/cli/commands/_hub_common.py` and
therefore must run on the hub host or inside the hub container. Issue #3872
adds workstation administration:

```bash
nexus hub token create --remote https://nexus.example.com --admin-token sk-...
nexus hub token list --remote https://nexus.example.com --admin-token sk-...
nexus hub token revoke <id-or-name> --remote https://nexus.example.com --admin-token sk-...
nexus hub status --remote https://nexus.example.com --admin-token sk-...
```

The hub deployment already exposes MCP HTTP at `/mcp`, and the MCP middleware
already extracts bearer tokens into the request context. Existing server-side
admin RPC handlers (`admin_create_key`, `admin_list_keys`, `admin_revoke_key`)
already require `is_admin`, but they are gRPC/internal RPC surfaces, not the
public MCP endpoint requested by this issue.

## Decision

Implement remote hub administration over the existing MCP HTTP endpoint. The
CLI's `--remote` flag targets the hub MCP endpoint, and `--admin-token` is sent
as a bearer token. The MCP server exposes admin tools that wrap the same token
operations as the local CLI path. Each admin MCP tool resolves the caller from
the request token and refuses non-admin callers with a 403-equivalent MCP tool
error.

This keeps day-to-day administration on the same public hub URL used by agents,
while preserving the bootstrap model: the first admin token is still created
locally, then copied to the workstation.

## Non-goals

- Do not expose the gRPC port or require workstation gRPC access.
- Do not replace the existing local `NEXUS_DATABASE_URL` path.
- Do not add token storage on the workstation.
- Do not add multi-zone token features beyond the behavior already present from
  issue #3871.
- Do not add a broader management API beyond create, list, revoke, and status.

## Architecture

### Shared Hub Operations

Add a focused helper module for hub token operations, for example
`src/nexus/cli/commands/_hub_token_ops.py`. It owns the DB-backed behavior
currently embedded in `hub.py`:

- parse zone CSV and permissions
- validate duplicate names and active zones
- create tokens through `nexus.storage.api_key_ops.create_api_key`
- list tokens with primary zone and zone allow-list
- resolve and revoke tokens by key ID prefix or name
- build the status payload from Postgres and Redis

The local CLI and MCP admin tools both call these helpers. The helpers receive a
SQLAlchemy session factory and return structured dictionaries. `hub.py` remains
responsible for Click options and human output.

### MCP Admin Tools

Add admin-only MCP tools inside `src/nexus/bricks/mcp/server.py`, or in a small
helper imported by that module if the local file size becomes too large:

- `nexus_hub_token_create`
- `nexus_hub_token_list`
- `nexus_hub_token_revoke`
- `nexus_hub_status`

Each tool:

1. Reads the per-request API key from the existing MCP context variable.
2. Authenticates it with the existing `auth_provider`.
3. Requires `authenticated=True` and `is_admin=True`.
4. Resolves a DB session factory from the database auth provider.
5. Calls the shared hub operation helper.
6. Returns JSON strings so the CLI can parse a stable machine contract.

If no auth provider or DB session factory is available, the tools return a
configuration error. If authentication fails or the token is non-admin, they
return a permission error. Non-admin tokens must not reach the DB operation.

### Remote CLI Client

Add `--remote` and `--admin-token` to:

- `nexus hub token create`
- `nexus hub token list`
- `nexus hub token revoke`
- `nexus hub status`

When `--remote` is absent, behavior stays unchanged and uses local DB access.
When `--remote` is present:

- `--admin-token` is required unless `NEXUS_HUB_ADMIN_TOKEN` is set.
- `https://host` normalizes to `https://host/mcp`.
- `https://host/mcp` is used unchanged.
- The CLI performs one MCP initialize handshake, sends
  `notifications/initialized`, then calls the relevant admin tool with
  `tools/call`.
- The CLI reuses existing local output formatting so remote `list` and local
  `list` look the same.

`--admin-token` is accepted as requested by the issue. The environment variable
exists to keep tokens out of shell history for operators who prefer that.

## Data Flow

Create:

```text
CLI --remote/--admin-token
  -> MCP HTTP /mcp Authorization: Bearer <admin-token>
  -> tool nexus_hub_token_create
  -> authenticate request token via auth_provider
  -> require is_admin
  -> shared create helper
  -> api_keys and api_key_zones rows
  -> JSON result with key_id and one-time raw token
  -> CLI prints same output as local create
```

List, revoke, and status follow the same path, replacing the helper operation.

## Error Handling

- Missing `--admin-token` with `--remote`: Click error.
- Bad remote URL or HTTP failure: Click error with remote URL and status.
- MCP tool error: Click error with the tool error message.
- Non-admin token: MCP tool returns permission error; CLI exits non-zero.
- Missing DB-backed auth provider on the MCP server: configuration error.
- Ambiguous revoke target: preserve local behavior and exit code semantics as
  closely as Click allows.

## Testing

Unit tests first:

- Remote CLI builds the correct MCP tool call for create, list, revoke, and
  status.
- `--remote` normalizes host URLs to `/mcp`.
- `--remote` requires `--admin-token` or `NEXUS_HUB_ADMIN_TOKEN`.
- MCP admin helpers reject non-admin auth results before invoking operations.
- MCP admin helpers call the shared operation with an admin auth result.
- Local hub token tests continue passing against the shared helper extraction.

Integration/E2E:

- Start a fresh local stack with `nexus up --build`.
- Bootstrap one admin token locally.
- `nexus hub token list --remote http://localhost:<mcp-port> --admin-token <admin>`
  returns the same token rows as local list.
- A non-admin token calling remote admin receives a permission failure.
- `--remote` works for create, list, revoke, and status.

## Acceptance Mapping

- `nexus hub token list --remote https://... --admin-token sk-...` returns the
  same output as local list: shared operation helper plus reused CLI formatter.
- Non-admin tokens receive 403 from the admin RPC: MCP admin tools require
  `is_admin` before calling hub operations.
- `--remote` works for create, list, revoke, status: each command receives a
  remote branch and maps to an MCP admin tool.
