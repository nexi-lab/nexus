# Issue 4131 MCP Tool Profile Story Design

## Goal

Complete the MCP-capable agent story for sandbox-oriented tool profiles by tying the default MCP profile matrix to user documentation, enforcement tests, and the shared API/RPC surface coverage model.

## Recommended Approach

Use the existing `develop` surface-coverage model as the source of truth. Keep the existing product surface because `nexus mcp profile list/show/assign/inspect` already covers profile introspection and assignment. Do not open a missing-surface gap unless implementation discovers a required command or RPC is absent.

## Alternatives Considered

1. Update only `docs/guides/user-guide.md`.
   This is too narrow because issue 4131 requires tests, matrix coverage, and performance classification.

2. Add a new standalone MCP profile guide.
   This would be easier to read in isolation but would duplicate the shared surface model and violate the issue comment that says to build the shared understanding first.

3. Update tests, user guide, and `api-rpc-surface-coverage.yaml` together.
   This is the chosen path because it keeps the guide, contract checks, and rendered architecture map aligned.

## Scope

- Add tests that prove the default profiles `minimal`, `coding`, `search`, `execution`, and `full` have the expected inherited tool matrix.
- Add tests that exercise namespace filtering and `tools/call` denial behavior for each default profile.
- Add an architecture contract test for issue 4131 so MCP tool rows and guide content cannot regress.
- Update the shared coverage YAML rows for MCP file/edit/sandbox/workflow/admin tools that currently lack owner, examples, correctness tests, and performance classification.
- Update `docs/guides/user-guide.md` to tell the agent tool-use story by task and include CLI plus MCP JSON-RPC examples, expected allowed/denied/unavailable behavior, a correctness assertion, and performance classification.
- Render the HTML architecture map after YAML updates.

## Out Of Scope

- New MCP tools.
- Changes to the MCP JSON-RPC protocol server.
- New benchmark code unless existing benchmark/test evidence is missing for a hot path.

## Missing Surface Decision

No missing-surface issue is needed if the existing `nexus mcp profile list`, `show`, `assign`, and `inspect` commands satisfy profile discovery and assignment. If tests show one of those commands cannot support the documented workflow, the implementation must add a gap row and linked issue before claiming completion.
