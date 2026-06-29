# PR #4454 Live Qualification Harness Design

## Context

PR #4454 passed a disposable `nexus up --build` qualification run, the full
build/performance E2E, and the enhanced ReBAC permissions demo. The live run
also exposed three false-confidence or misleading-output gaps in the host-side
validation scripts:

1. `scripts/test_build_perf_e2e.py` reports that its fuzzy edit restored
   `plan.md`, but the replacement drops the Markdown list marker.
2. `permissions_demo_enhanced.sh` documents owners as unable to read and does
   not fail when owner read is denied, while the current file namespace grants
   owners read, write, and execute.
3. The permissions demo asks `rebac explain` about a child file without a
   ReBAC parent tuple, prints a red denial, and then reports the parent-based
   real-I/O test as successful. The explanation should target and assert the
   parent resource whose group permission is under test.

## Goal

Make the two live qualification scripts fail when these expectations regress
and keep their output consistent with the behavior they claim to validate.

## Scope

Modify only:

- `scripts/test_build_perf_e2e.py`
- `permissions_demo_enhanced.sh`
- focused tests under `tests/unit/scripts/`

No Nexus server, ReBAC engine, API, schema, dependency, image, or Koodle code
changes are included. The live server behavior already produced the correct
authorization and mutation results.

## Design

### Exact fuzzy-edit restoration

The performance E2E will preserve the complete Markdown line, including the
leading list marker, when performing its intentionally misspelled fuzzy
restore. It will read the file after the restore and add an explicit check that
the original line is present and the temporary edited line is absent. A small
pure predicate will make this requirement directly unit-testable without a
running stack.

### Current owner semantics

The permissions demo will describe an owner as having read, write, and execute,
matching `DEFAULT_FILE_NAMESPACE`. Its owner assertion will require all three
decisions to be granted. A denial of any one permission will increment the
existing failure counter and make the script exit nonzero.

### Actionable group explanation

The group-composition section will run `rebac explain` on the parent directory
that has the userset permission. The command output will be captured and
required to contain `GRANTED`; a denial or command failure will increment the
existing failure counter. Child-path inheritance remains covered separately by
the real file-I/O assertion.

## Testing Strategy

Follow red-green TDD for each behavior:

1. Add a focused Python unit test proving that a missing Markdown list marker
   is not accepted as an exact restore; run it and observe the expected failure.
2. Add static shell-script guards requiring owner read/write/execute and a
   gated parent-resource explanation; run them and observe the expected
   failures.
3. Implement the smallest script changes that satisfy the tests.
4. Run the focused unit tests and formatting/lint checks.
5. Reuse the disposable locally built PR stack, reset/reseed the demo corpus,
   and rerun both requested scripts. Require zero failed checks, strict ReBAC
   median latency below 300 ms, and no misleading red denial in the positive
   explanation section.
6. Run `git diff --check` and confirm the branch contains only the approved
   harness/spec changes beyond the existing PR.

## Operational Safety

All live reruns stay on the disposable local stack and database at ports
`50780-50784`. No Railway, Vercel, Supabase, Koodle production, or shared Nexus
database is used. The permissions demo remains restricted to the disposable
database because its cleanup performs broad ReBAC tuple deletion.

## Acceptance Criteria

- The fuzzy restore leaves `plan.md` exactly formatted and is verified by a
  post-restore read.
- Owner read, write, and execute are all mandatory in the permissions demo.
- The positive group explanation is `GRANTED` and failure-counted otherwise.
- Focused unit tests pass after demonstrating the expected red state first.
- Both live scripts pass again against the locally built PR image.
- No production environment or dependency version is changed.
