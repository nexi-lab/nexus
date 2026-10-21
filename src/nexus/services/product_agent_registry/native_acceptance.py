"""Native admission binds real stored execution evidence and the current runtime rules."""

from sqlalchemy import inspect, select

from nexus.contracts.agent_evaluation import HostedEvaluationCase, HostedEvaluationRun
from nexus.contracts.agent_inventory import AcceptanceReport
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.native_execution import execution_profile, verify_output
from nexus.services.product_agent_registry.store import RegistryError, digest


def require_native_acceptance(inventory, con, zone, record, report):
    from nexus.services.product_agent_registry.hosted_evaluations import (
        MigrationComparison,
        cases,
        comparisons,
        runs,
    )
    from nexus.services.product_agent_registry.inventory import acceptances
    from nexus.services.product_agent_registry.native_conversions import (
        NativeConversions,
        conversions,
    )

    if report.native_binding is None or record.metadata.source_platform != "native":
        raise RegistryError("AGENT_NATIVE_CONVERSION_REQUIRED")
    if conversions.name not in inspect(con).get_table_names():
        raise RegistryError("NATIVE_SCHEMA_REQUIRED", 503)

    def document(table, identifier):
        value = con.execute(
            select(table.c.document).where(table.c.zone == zone, table.c.id == identifier)
        ).scalar_one_or_none()
        if value is None:
            raise RegistryError("AGENT_NATIVE_CONVERSION_REQUIRED")
        return value

    converted = NativeConversions._decode(
        document(conversions, report.native_binding.conversion_id)
    )
    profile = execution_profile(converted)
    profile_digest = digest(canonical_bytes(profile.model_dump()))
    if (
        converted.asset_id != record.id
        or converted.metadata_digest != record.metadata_digest
        or converted.spec_digest != report.spec_digest
        or converted.image.image_id != report.execution_revision
        or report.native_binding.profile_digest != profile_digest
        or report.expires_at > converted.source_acceptance.expires_at
        or not {"native", "tool"}.issubset(report.checks)
        or not {
            profile_digest,
            converted.spec_digest,
            converted.image.runtime.executable_digest,
            converted.image.runtime.runner_digest,
        }.issubset(report.evidence_refs)
    ):
        raise RegistryError("AGENT_NATIVE_EVIDENCE_MISMATCH")
    source = inventory._get(con, zone, converted.source_asset_id)
    accepted = (
        con.execute(
            select(acceptances).where(
                acceptances.c.zone == zone,
                acceptances.c.asset_id == source.id,
                acceptances.c.metadata_digest == converted.source_metadata_digest,
                acceptances.c.spec_digest == converted.source_spec_digest,
                acceptances.c.approved == 1,
            )
        )
        .mappings()
        .first()
    )
    if (
        source.status != "approved"
        or source.metadata_digest != converted.source_metadata_digest
        or accepted is None
        or accepted["issuer"] not in inventory.attestors
    ):
        raise RegistryError("NATIVE_REVIEWED_L2_SOURCE_REQUIRED")
    source_report = AcceptanceReport.model_validate_json(accepted["document"])
    if source_report != converted.source_acceptance or source_report.level != "L2":
        raise RegistryError("NATIVE_REVIEWED_L2_SOURCE_REQUIRED")
    inventory._validate_acceptance(source, source_report, con=con, zone=zone)

    comparison = MigrationComparison.model_validate_json(
        document(comparisons, report.evaluation_id)
    )
    candidate = HostedEvaluationRun.model_validate_json(document(runs, comparison.candidate_run_id))
    if (
        comparison.execution_kind != "native"
        or candidate.execution_kind != "native"
        or candidate.role != "hosted"
        or candidate.artifact_id != converted.id
        or candidate.spec_digest != converted.spec_digest
    ):
        raise RegistryError("AGENT_NATIVE_EVIDENCE_MISMATCH")
    rows = list(
        con.execute(
            select(cases.c.document).where(cases.c.zone == zone, cases.c.run_id == candidate.id)
        ).scalars()
    )
    if len(rows) != candidate.total_cases:
        raise RegistryError("AGENT_NATIVE_EVIDENCE_MISMATCH")
    for row in rows:
        case = HostedEvaluationCase.model_validate_json(row)
        verified = verify_output(
            profile,
            case.answer or "",
            case.model_call_ids,
            [frame.model_dump() for frame in case.trace],
            execution_succeeded=case.status == "succeeded",
            cleanup_confirmed=case.cleanup_confirmed and case.iam_cleanup_confirmed,
        )
        # Old completed records are checked from immutable facts, never rewritten
        # or automatically replayed to add a new verification field.
        if verified.verdict != "passed" or (
            case.native_verification is not None and case.native_verification != verified
        ):
            raise RegistryError("AGENT_NATIVE_VERIFICATION_REQUIRED")
