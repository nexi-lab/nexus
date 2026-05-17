"""Merge fresh extraction with committed YAML, preserving human-filled fields.

Idempotent: same inputs -> same output.

Human-owned fields preserved from existing YAML:
    summary (if non-empty in existing), usage_example, correctness_test,
    perf_class, perf_link, gap_issue, owning_issue

Extractor-owned fields refreshed from fresh:
    transports, module (if extractor reassigned), profiles (extractor seeds;
    humans override via overrides YAML applied separately).
"""

from __future__ import annotations

from dataclasses import replace

from scripts.surface_coverage.schema import (
    Operation,
    StaleRow,
    SurfaceCoverage,
)


def _merge_op(existing: Operation, fresh: Operation) -> Operation:
    return replace(
        fresh,
        summary=existing.summary if existing.summary.strip() else fresh.summary,
        usage_example=existing.usage_example,
        correctness_test=existing.correctness_test,
        perf_class=existing.perf_class,
        perf_link=existing.perf_link,
        gap_issue=existing.gap_issue,
        owning_issue=existing.owning_issue,
    )


def merge_coverage(
    *,
    existing: SurfaceCoverage,
    fresh: SurfaceCoverage,
) -> SurfaceCoverage:
    existing_by_id = {op.id: op for op in existing.operations}
    fresh_by_id = {op.id: op for op in fresh.operations}

    merged_ops: list[Operation] = []
    stale_rows: list[StaleRow] = list(existing.stale_rows)

    for op_id, fresh_op in fresh_by_id.items():
        if op_id in existing_by_id:
            merged_ops.append(_merge_op(existing_by_id[op_id], fresh_op))
        else:
            merged_ops.append(fresh_op)

    # preserve operations that disappeared from extractor (don't auto-delete)
    for op_id, existing_op in existing_by_id.items():
        if op_id not in fresh_by_id:
            merged_ops.append(existing_op)
            if not any(s.operation_id == op_id for s in stale_rows):
                stale_rows.append(
                    StaleRow(
                        operation_id=op_id,
                        reason="present in committed YAML but not detected by extractor",
                    )
                )

    # sort for deterministic output
    merged_ops.sort(key=lambda o: o.id)

    return SurfaceCoverage(
        schema_version=fresh.schema_version,
        modules=sorted(fresh.modules, key=lambda m: m.id),
        operations=merged_ops,
        parity_warnings=list(fresh.parity_warnings),
        unmapped_surfaces=list(fresh.unmapped_surfaces),
        stale_rows=stale_rows,
    )
