# Unit Tests

## Philosophy

Unit tests cover the **trusted computing base** — the layers whose failure crashes the entire system. Feature modules that can fail gracefully are tested at integration/e2e level instead.

### What we test (priority order)

| Tier | Directory | What |
|------|-----------|------|
| **Kernel** | `core/` | NexusFS, VFS, mounts, namespaces, permissions, ReBaC, kernel invariants |
| **System services** | `services/` | Event bus, agent registry, protocol contracts, permission services |
| **Storage** | `backends/`, `storage/` | Backend contracts, CAS, record store, query builder, write path |
| **Protocols** | `ipc/`, `mcp/`, `server/test_rpc_*` | IPC envelope/delivery, MCP server/tools, RPC parity/protocol |

### What we don't unit-test

Feature modules (search, skills, pay, connectors, LLM, workflows, sandbox, etc.) — these are self-contained and covered by integration and e2e tests.

## Time Budget

- **Per-test**: 60s max (`pytest-timeout`)
- **Entire suite**: 3 minutes max (enforced in `conftest.py` and CI)

If a test approaches these limits, it belongs in `tests/integration/`, not here.

## Rules

1. **No external dependencies** — no network, no Docker, no database servers. Use mocks and `tmp_path`.
2. **Fast by default** — the full suite must finish in under 3 minutes on CI.
3. **Kernel tests are mandatory** — never delete a kernel invariant test without replacing it.
4. **One assertion per concern** — test names should read as specifications.
5. **No feature creep** — adding a new feature module? Its tests go in `tests/integration/` or `tests/e2e/`, not here, unless it's a new kernel/service/storage component.
