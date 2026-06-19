"""Merge fresh extraction with committed YAML, preserving human-filled fields.

Idempotent: same inputs -> same output.

Human-owned fields preserved from existing YAML:
    summary (if non-empty in existing), usage_example, correctness_test,
    perf_class, perf_link, gap_issue, owning_issue

Extractor-owned fields refreshed from fresh:
    transports, module (if extractor reassigned). Default profile assignments
    refresh from the extractor; non-default profile statuses are human-owned.
"""

from __future__ import annotations

from dataclasses import replace

from scripts.surface_coverage.schema import (
    Operation,
    ProfileStatus,
    StaleRow,
    SurfaceCoverage,
)

_DEFAULT_PROFILES = dict.fromkeys(("lite", "sandbox", "full"), ProfileStatus.SUPPORTED)


def _all_missing_needed_profiles(op: Operation) -> bool:
    return bool(op.profiles) and all(
        status == ProfileStatus.MISSING_NEEDED for status in op.profiles.values()
    )


def _merge_op(existing: Operation, fresh: Operation) -> Operation:
    should_preserve_profiles = existing.profiles != _DEFAULT_PROFILES
    if _all_missing_needed_profiles(existing) and fresh.transports:
        should_preserve_profiles = False

    return replace(
        fresh,
        profiles=(existing.profiles if should_preserve_profiles else fresh.profiles),
        summary=existing.summary if existing.summary.strip() else fresh.summary,
        usage_example=existing.usage_example,
        correctness_test=existing.correctness_test,
        perf_class=existing.perf_class,
        perf_link=existing.perf_link,
        gap_issue=existing.gap_issue if existing.gap_issue is not None else fresh.gap_issue,
        owning_issue=existing.owning_issue
        if existing.owning_issue is not None
        else fresh.owning_issue,
    )


def merge_coverage(
    *,
    existing: SurfaceCoverage,
    fresh: SurfaceCoverage,
) -> SurfaceCoverage:
    existing_by_id = {op.id: op for op in existing.operations}
    fresh_by_id = {op.id: op for op in fresh.operations}

    merged_ops: list[Operation] = []
    # Drop pre-existing stale entries whose op has been rediscovered by the
    # fresh extractor — they're no longer stale.
    stale_rows: list[StaleRow] = [
        s for s in existing.stale_rows if s.operation_id not in fresh_by_id
    ]

    for op_id, fresh_op in fresh_by_id.items():
        if op_id in existing_by_id:
            merged_ops.append(_merge_op(existing_by_id[op_id], fresh_op))
        else:
            merged_ops.append(fresh_op)

    # preserve operations that disappeared from extractor (don't auto-delete)
    for op_id, existing_op in existing_by_id.items():
        if op_id not in fresh_by_id:
            merged_ops.append(existing_op)
            # Missing-needed wishlist rows are source-controlled by
            # api-rpc-surface-gaps.yaml and validated separately.
            if _all_missing_needed_profiles(existing_op) and not existing_op.transports:
                continue
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
