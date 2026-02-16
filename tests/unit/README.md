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

## Keeping the suite lean (~3 min target)

We do **not** enforce timeouts in CI (they cause flaky failures on busy runners). Instead:

- **Target**: Full unit suite completes in **under ~3 minutes** on a typical CI runner. Treat this as a team norm, not a hard limit.
- **Slow tests**: Mark genuinely slow tests with `@pytest.mark.slow` so they can be deselected when iterating (`-m "not slow"`). Profile with `pytest tests/unit -v --durations=20` to find the slowest tests.
- **If a test is slow**: Prefer making it faster (smaller data, better mocks, less iteration). If it can't be fast, move it to `tests/integration/` or `tests/e2e/`.
- **CI**: We rely on the default job timeout; avoid adding step- or per-test timeouts that fail on variable runner load.

## Rules

1. **No external dependencies** — no network, no Docker, no database servers. Use mocks and `tmp_path`.
2. **Fast by default** — keep the full suite under ~3 minutes so CI and local runs stay responsive.
3. **Kernel tests are mandatory** — never delete a kernel invariant test without replacing it.
4. **One assertion per concern** — test names should read as specifications.
5. **No feature creep** — adding a new feature module? Its tests go in `tests/integration/` or `tests/e2e/`, not here, unless it's a new kernel/service/storage component.
