# PR #4454 Live Qualification Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the two live PR #4454 qualification scripts fail on incomplete document restoration, stale owner semantics, or misleading group-permission explanations.

**Architecture:** Keep all changes in the host-side validation layer. Add one pure Python predicate for exact edit restoration and extend the existing static shell-script guards so the Bash demo remains testable without a server; then confirm the same behavior against the disposable locally built Nexus stack.

**Tech Stack:** Python 3.14, pytest, Bash, Nexus CLI, Docker Compose

---

### Task 1: Require exact fuzzy-edit restoration

**Files:**
- Create: `tests/unit/scripts/test_build_perf_e2e.py`
- Modify: `scripts/test_build_perf_e2e.py:32-40`
- Modify: `scripts/test_build_perf_e2e.py:287-335`

- [ ] **Step 1: Write the failing unit test**

Create `tests/unit/scripts/test_build_perf_e2e.py`:

```python
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "test_build_perf_e2e.py"
SPEC = importlib.util.spec_from_file_location("test_build_perf_e2e_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_plan_auth_restore_requires_the_complete_original_line() -> None:
    assert MODULE._plan_auth_line_restored("# Plan\n- Configure authentication\n")
    assert not MODULE._plan_auth_line_restored("# Plan\nConfigure authentication\n")
    assert not MODULE._plan_auth_line_restored(
        "# Plan\n- Configure authentication\n- Configure auth (test-edit)\n"
    )
```

- [ ] **Step 2: Run the test and verify the red state**

Run:

```bash
uv run --no-sync pytest tests/unit/scripts/test_build_perf_e2e.py -q
```

Expected: FAIL because `scripts/test_build_perf_e2e.py` has no `_plan_auth_line_restored` function.

- [ ] **Step 3: Add the minimal predicate and exact restore check**

Add next to the other script constants:

```python
PLAN_AUTH_ORIGINAL = "- Configure authentication"
PLAN_AUTH_EDITED = "- Configure auth (test-edit)"


def _plan_auth_line_restored(text: str) -> bool:
    lines = text.splitlines()
    return PLAN_AUTH_ORIGINAL in lines and PLAN_AUTH_EDITED not in lines
```

Change the fuzzy edit to preserve the full Markdown line:

```python
"edits": [["- Confgiure auth (test-edt)", PLAN_AUTH_ORIGINAL]],
```

Immediately after the existing `edit (fuzzy restore)` check, read the file and add a separate correctness check:

```python
step("RPC sys_read verifying fuzzy restore is exact")
content = t.call_rpc("sys_read", {"path": "/workspace/demo/plan.md"})
text = content.decode() if isinstance(content, bytes) else str(content)
check(
    "fuzzy restore preserved original line",
    _plan_auth_line_restored(text),
    f"original_line={PLAN_AUTH_ORIGINAL in text.splitlines()}, "
    f"edited_line={PLAN_AUTH_EDITED in text.splitlines()}",
)
```

- [ ] **Step 4: Run the focused test and verify green**

Run:

```bash
uv run --no-sync pytest tests/unit/scripts/test_build_perf_e2e.py -q
```

Expected: `1 passed`.

- [ ] **Step 5: Commit the exact-restore guard**

```bash
git add scripts/test_build_perf_e2e.py tests/unit/scripts/test_build_perf_e2e.py
git commit -m "test(e2e): require exact fuzzy edit restoration"
```

### Task 2: Align owner expectations with the file namespace

**Files:**
- Modify: `tests/unit/scripts/test_permissions_demo_enhanced.py:46`
- Modify: `permissions_demo_enhanced.sh:467-516`

- [ ] **Step 1: Add a failing static guard**

Append to `tests/unit/scripts/test_permissions_demo_enhanced.py`:

```python
def test_owner_role_requires_read_write_and_execute() -> None:
    text = SCRIPT.read_text()
    owner_block = text[
        text.index('print_subsection "1.1 Understanding Permission Roles"') : text.index(
            'print_test "Verify bob (editor)'
        )
    ]

    assert "OWNER:  read ✓  write ✓  execute ✓" in owner_block
    assert "owners need editor/viewer role for read" not in owner_block
    assert "ALICE_READ=$(nexus rebac check user alice read file" in owner_block
    assert (
        'if echo "$ALICE_READ" | grep -q "GRANTED" '
        '&& echo "$ALICE_WRITE" | grep -q "GRANTED" '
        '&& echo "$ALICE_EXEC" | grep -q "GRANTED"; then'
    ) in owner_block
```

- [ ] **Step 2: Run the test and verify the red state**

```bash
uv run --no-sync pytest \
  tests/unit/scripts/test_permissions_demo_enhanced.py::test_owner_role_requires_read_write_and_execute \
  -q
```

Expected: FAIL because the demo still says owner read is denied and does not include read in its success condition.

- [ ] **Step 3: Update the owner narrative and assertion**

Replace the owner description with:

```bash
echo "    OWNER:  read ✓  write ✓  execute ✓  (full control)"
```

Remove the sentence that owners need an editor/viewer role. Read all three decisions before the condition:

```bash
ALICE_READ=$(nexus rebac check user alice read file $DEMO_BASE/test-file.txt 2>&1)
ALICE_WRITE=$(nexus rebac check user alice write file $DEMO_BASE/test-file.txt 2>&1)
ALICE_EXEC=$(nexus rebac check user alice execute file $DEMO_BASE/test-file.txt 2>&1)
```

Require all three grants and include all three observed decisions in the failure:

```bash
if echo "$ALICE_READ" | grep -q "GRANTED" && echo "$ALICE_WRITE" | grep -q "GRANTED" && echo "$ALICE_EXEC" | grep -q "GRANTED"; then
    print_success "✅ Owner has read + write + execute"
else
    print_error "Owner permissions incorrect! read=$(echo "$ALICE_READ"|grep -oE 'GRANTED|DENIED'|head -1) write=$(echo "$ALICE_WRITE"|grep -oE 'GRANTED|DENIED'|head -1) execute=$(echo "$ALICE_EXEC"|grep -oE 'GRANTED|DENIED'|head -1)"
fi
```

- [ ] **Step 4: Run the focused test and Bash syntax check**

```bash
uv run --no-sync pytest \
  tests/unit/scripts/test_permissions_demo_enhanced.py::test_owner_role_requires_read_write_and_execute \
  -q
bash -n permissions_demo_enhanced.sh
```

Expected: `1 passed`; Bash exits `0`.

- [ ] **Step 5: Commit the owner semantics guard**

```bash
git add permissions_demo_enhanced.sh tests/unit/scripts/test_permissions_demo_enhanced.py
git commit -m "test(rebac): require current owner semantics"
```

### Task 3: Make the group explanation actionable

**Files:**
- Modify: `tests/unit/scripts/test_permissions_demo_enhanced.py:60`
- Modify: `permissions_demo_enhanced.sh:612-624`

- [ ] **Step 1: Add a failing explanation guard**

Append to `tests/unit/scripts/test_permissions_demo_enhanced.py`:

```python
def test_group_explanation_targets_and_requires_the_granted_parent() -> None:
    text = SCRIPT.read_text()
    explain_block = text[
        text.index('log_step "rebac check user bob write file $DEMO_BASE') : text.index(
            'print_subsection "2.4 PROVE group composition'
        )
    ]

    assert (
        "EXPLAIN_OUT=$(nexus rebac explain user bob write file $DEMO_BASE 2>&1) "
        "&& EXPLAIN_RC=0 || EXPLAIN_RC=$?"
    ) in explain_block
    assert ('[ "$EXPLAIN_RC" -eq 0 ] && echo "$EXPLAIN_OUT" | grep -q "GRANTED"') in explain_block
    assert "Group explanation failed" in explain_block
    assert "$DEMO_BASE/team-file.txt" not in explain_block
```

- [ ] **Step 2: Run the test and verify the red state**

```bash
uv run --no-sync pytest \
  tests/unit/scripts/test_permissions_demo_enhanced.py::test_group_explanation_targets_and_requires_the_granted_parent \
  -q
```

Expected: FAIL because the demo explains the child path, discards failure, and never checks `GRANTED`.

- [ ] **Step 3: Capture and assert the parent explanation**

Replace the unguarded explanation command with:

```bash
log_step "rebac explain bob write on $DEMO_BASE"
EXPLAIN_OUT=$(nexus rebac explain user bob write file $DEMO_BASE 2>&1) && EXPLAIN_RC=0 || EXPLAIN_RC=$?
echo "$EXPLAIN_OUT" | head -5
if [ "$EXPLAIN_RC" -eq 0 ] && echo "$EXPLAIN_OUT" | grep -q "GRANTED"; then
    print_success "✅ Group permission explanation is granted"
else
    print_error "Group explanation failed (exit=$EXPLAIN_RC): $(echo "$EXPLAIN_OUT" | head -3)"
fi
```

- [ ] **Step 4: Run the complete focused unit suite**

```bash
uv run --no-sync pytest \
  tests/unit/scripts/test_build_perf_e2e.py \
  tests/unit/scripts/test_permissions_demo_enhanced.py \
  -q
bash -n permissions_demo_enhanced.sh
uv run --no-sync ruff check \
  scripts/test_build_perf_e2e.py \
  tests/unit/scripts/test_build_perf_e2e.py \
  tests/unit/scripts/test_permissions_demo_enhanced.py
```

Expected: all focused tests pass; Bash and Ruff exit `0`.

- [ ] **Step 5: Commit the explanation guard**

```bash
git add permissions_demo_enhanced.sh tests/unit/scripts/test_permissions_demo_enhanced.py
git commit -m "test(rebac): require granted group explanation"
```

### Task 4: Re-run both requested scripts against the built PR image

**Files:**
- Read: `/tmp/nexus-pr4454-e2e.dWZeQ2/nexus.yaml`
- Read: `/tmp/nexus-pr4454-e2e.dWZeQ2/nexus-data/.state.json`
- Produce: `/tmp/nexus-pr4454-e2e.dWZeQ2/build-perf-e2e-fixed.log`
- Produce: `/tmp/nexus-pr4454-e2e.dWZeQ2/permissions-demo-fixed.log`

- [ ] **Step 1: Confirm the disposable stack and reset its demo data**

Run from `/tmp/nexus-pr4454-e2e.dWZeQ2`:

```bash
REPO=/Users/tafeng/.codex/worktrees/21d7/nexus-upstream-gaps
CLI="$REPO/.venv/bin/nexus"
unset NEXUS_URL NEXUS_API_KEY NEXUS_GRPC_HOST NEXUS_GRPC_PORT \
  NEXUS_APPROVALS_GRPC_PORT NEXUS_DATABASE_URL DATABASE_URL NEXUS_PROFILE \
  NEXUS_IMAGE_REF NEXUS_IMAGE_TAG NEXUS_DATA_DIR
eval "$("$CLI" env)"
"$CLI" status --json
"$CLI" demo init --reset
```

Expected: status identifies the healthy local server and reseeding exits `0`.

- [ ] **Step 2: Run the corrected performance/correctness E2E**

Run without printing either key:

```bash
PY="$REPO/.venv/bin/python"
export NEXUS_DEMO_USER_KEY="$("$PY" -c \
  'import json; print(json.load(open("nexus-data/.demo-manifest.json"))["identity_keys"]["demo_user"]["api_key"])')"
export NEXUS_CLI="$CLI"
"$PY" "$REPO/scripts/test_build_perf_e2e.py" \
  > build-perf-e2e-fixed.log 2>&1
grep -E '^RESULTS:|RPC latency|HERB hit rate' build-perf-e2e-fixed.log
```

Expected: `RESULTS: 26 passed, 0 failed`, HERB at least `7/8`, and RPC p50 below `50 ms`.

- [ ] **Step 3: Reset again and run the corrected strict permissions demo**

```bash
"$CLI" demo init --reset
eval "$("$CLI" env)"
export NEXUS_DATABASE_URL="$DATABASE_URL"
export PATH="$REPO/.venv/bin:$PATH"
KEEP=0 \
NEXUS_DEMO_PYTHON_BIN="$REPO/.venv/bin/python" \
NEXUS_DEMO_STRICT_PERF=1 \
NEXUS_DEMO_LATENCY_MEDIAN_MS_MAX=300 \
"$REPO/permissions_demo_enhanced.sh" \
  > permissions-demo-fixed.log 2>&1
```

Expected: exit `0`, final `All tests passed!`, all five benchmark rows say `PASS`, and median latency is below `300 ms`.

- [ ] **Step 4: Verify the corrected positive-path output**

```bash
python3 - <<'PY'
from pathlib import Path
import re

text = Path("permissions-demo-fixed.log").read_text(errors="replace")
text = re.sub(r"\x1b\[[0-9;]*m", "", text)
assert "Owner has read + write + execute" in text
assert "Group permission explanation is granted" in text
assert "Group explanation failed" not in text
assert "All tests passed!" in text
PY
```

Expected: exit `0`.

### Task 5: Final verification and PR update

**Files:**
- Verify: all files changed since `origin/develop`

- [ ] **Step 1: Run repository hygiene checks**

```bash
git diff --check origin/develop...HEAD
git status --short --branch
```

Expected: no whitespace errors; only intended uncommitted plan state, if any.

- [ ] **Step 2: Commit the implementation plan if still uncommitted**

```bash
git add docs/superpowers/plans/2026-06-29-pr4454-live-qualification-harness.md
git commit -m "docs: plan live qualification harness fixes"
```

- [ ] **Step 3: Re-run the focused tests from the final committed tree**

```bash
uv run --no-sync pytest \
  tests/unit/scripts/test_build_perf_e2e.py \
  tests/unit/scripts/test_permissions_demo_enhanced.py \
  -q
bash -n permissions_demo_enhanced.sh
uv run --no-sync ruff check \
  scripts/test_build_perf_e2e.py \
  tests/unit/scripts/test_build_perf_e2e.py \
  tests/unit/scripts/test_permissions_demo_enhanced.py
git diff --check origin/develop...HEAD
```

Expected: every command exits `0`.

- [ ] **Step 4: Push the branch and confirm PR state**

```bash
git push origin codex/fix-edge-qualification-gaps
gh pr checks 4454 --repo nexi-lab/nexus --watch
gh pr view 4454 --repo nexi-lab/nexus \
  --json isDraft,state,mergeable,mergeStateStatus,reviewDecision,headRefOid
```

Expected: push succeeds, required checks pass, the PR head matches local `HEAD`, and the only remaining merge gate is required review if `reviewDecision` remains `REVIEW_REQUIRED`.

- [ ] **Step 5: Stop the disposable stack after all reruns**

Run from `/tmp/nexus-pr4454-e2e.dWZeQ2`:

```bash
REPO=/Users/tafeng/.codex/worktrees/21d7/nexus-upstream-gaps
"$REPO/.venv/bin/nexus" down
```

Expected: the `nexus-c26ab29d` containers and network stop without touching unrelated Compose projects or production services.
