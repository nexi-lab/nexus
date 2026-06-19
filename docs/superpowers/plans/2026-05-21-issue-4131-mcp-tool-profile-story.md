# Issue 4131 MCP Tool Profile Story Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the MCP tool profile agent story with tests, user docs, and shared surface coverage.

**Architecture:** Keep `src/nexus/config/tool_profiles.yaml` as the profile source, `ToolNamespaceMiddleware` as the enforcement boundary, and `docs/architecture/api-rpc-surface-coverage.yaml` as the shared external-surface model. The guide explains the task workflow; tests lock profile inheritance, per-profile filtering, and contract coverage.

**Tech Stack:** Python, pytest, Click CLI tests, FastMCP test doubles, YAML surface coverage schema, generated static HTML.

---

### Task 1: Profile Matrix Tests

**Files:**
- Modify: `tests/unit/bricks/mcp/test_tool_profiles.py`

- [ ] **Step 1: Write the failing test**

Add a test that loads `src/nexus/config/tool_profiles.yaml` and asserts the inherited matrix for `minimal`, `coding`, `search`, `execution`, and `full`, including sandbox execution tools and discovery tools.

- [ ] **Step 2: Verify red**

Run `uv run pytest tests/unit/bricks/mcp/test_tool_profiles.py::TestDefaultConfigValidity::test_default_profiles_match_expected_tool_matrix -q`.
Expected before implementation: failure because the test does not exist.

- [ ] **Step 3: Implement minimal test support**

Use the existing `load_profiles()` API and no production code unless the test exposes a real config mismatch.

- [ ] **Step 4: Verify green**

Run the same pytest command and confirm it passes.

### Task 2: Enforcement Tests

**Files:**
- Modify: `tests/unit/bricks/mcp/test_tool_namespace_middleware.py`

- [ ] **Step 1: Write the failing test**

Add a parameterized test that grants each default profile to a subject, lists representative tools, verifies visible tools, and verifies the next-tier or out-of-profile tool returns `not found` from `tools/call`.

- [ ] **Step 2: Verify red**

Run `uv run pytest tests/unit/bricks/mcp/test_tool_namespace_middleware.py::TestDefaultToolProfileEnforcement -q`.
Expected before implementation: failure because the test class does not exist.

- [ ] **Step 3: Implement minimal test support**

Reuse `MemoryToolGrantReBAC`, `ToolNamespaceMiddleware`, and existing fake context/tool helpers.

- [ ] **Step 4: Verify green**

Run the same pytest command and confirm it passes.

### Task 3: Architecture Contract Test

**Files:**
- Create: `tests/architecture/test_issue_4131_mcp_tool_profile_story.py`

- [ ] **Step 1: Write the failing test**

Assert all issue 4131 MCP rows have summary, usage example containing both MCP and CLI context where applicable, correctness test links, performance class/link, and owning issue. Assert the user guide includes the profile matrix, profile CLI commands, JSON-RPC `tools/list` and `tools/call` examples, correctness assertion, performance classification, and links to file/search/ReBAC stories.

- [ ] **Step 2: Verify red**

Run `uv run pytest tests/architecture/test_issue_4131_mcp_tool_profile_story.py -q`.
Expected before docs/YAML implementation: failure on missing rows or guide phrases.

- [ ] **Step 3: Update data/docs**

Populate the relevant MCP rows in `docs/architecture/api-rpc-surface-coverage.yaml`, update `docs/guides/user-guide.md`, and regenerate `docs/architecture/api-rpc-surface-coverage.html`.

- [ ] **Step 4: Verify green**

Run the same pytest command and confirm it passes.

### Task 4: Full Verification

**Files:**
- Modified docs/tests/YAML/HTML from prior tasks.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
uv run pytest \
  tests/unit/bricks/mcp/test_tool_profiles.py \
  tests/unit/bricks/mcp/test_tool_namespace_middleware.py::TestDefaultToolProfileEnforcement \
  tests/architecture/test_issue_4131_mcp_tool_profile_story.py \
  tests/architecture/test_issue_4128_sandbox_rebac_mcp_boundaries.py \
  tests/architecture/test_issue_4136_full_mcp_mount_oauth_surface.py \
  -q
```

- [ ] **Step 2: Run generator/render checks**

Run:

```bash
uv run python scripts/gen_api_surface_coverage.py
uv run python scripts/render_api_surface_coverage.py
```

- [ ] **Step 3: Inspect diff**

Run `git diff -- docs/guides/user-guide.md docs/architecture tests/unit/bricks/mcp docs/superpowers` and verify the diff is scoped to issue 4131.
