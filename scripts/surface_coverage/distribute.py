"""Build and apply the surface-coverage appendix to a subissue body.

Idempotent: replaces any existing appendix bounded by sentinel comments.
"""

from __future__ import annotations

import re

APPENDIX_BEGIN = "<!-- BEGIN surface-contract-appendix:4161 -->"
APPENDIX_END = "<!-- END surface-contract-appendix:4161 -->"

_APPENDIX_RE = re.compile(
    re.escape(APPENDIX_BEGIN) + r".*?" + re.escape(APPENDIX_END) + r"\n?",
    re.DOTALL,
)


def build_appendix(*, issue_number: int, owned_op_ids: list[str]) -> str:
    if owned_op_ids:
        owned_block = "\n".join(f"- `{op_id}`" for op_id in sorted(owned_op_ids))
    else:
        owned_block = (
            "_No operations assigned yet. Use the search box in the map to find "
            "surfaces this slice should own, then add `owning_issue: "
            f"{issue_number}` to those rows in api-rpc-surface-coverage.yaml._"
        )
    return f"""{APPENDIX_BEGIN}

## Surface coverage contract (added by #4161)

This story slice contributes rows to the shared surface map:

- Map: `docs/surface-coverage/api-rpc-surface-coverage.html`
- Data: `docs/surface-coverage/api-rpc-surface-coverage.yaml`
- Contract: `docs/surface-coverage/api-rpc-surface-contract.md`

### Owned surfaces (filter map by `owner: #{issue_number}`)

{owned_block}

### Acceptance-criteria delta

- [ ] Every owned row has `summary` and `usage_example` filled.
- [ ] Every owned row has `correctness_test` linking to a test `file:line`.
- [ ] Every owned row has `perf_class` set (`hot|setup|control|not_perf_sensitive`)
      and `perf_link` (benchmark path or rationale).
- [ ] Every owned row has `profiles.{{lite,sandbox,full}}` set.
- [ ] Any missing-needed surface has a build gap issue opened and linked via `gap_issue`.
- [ ] Re-run `scripts/gen_api_surface_coverage.py`; commit YAML; render HTML.

{APPENDIX_END}
"""


def apply_appendix(body: str, appendix: str) -> str:
    """Return `body` with appendix appended (or replaced if already present)."""
    stripped = _APPENDIX_RE.sub("", body).rstrip() + "\n\n"
    return stripped + appendix
