"""Durable evaluation records, independent of publication and generic Task ownership.

Only the configured evaluator can reserve/complete cases. A started case is never
reclaimed: lost acknowledgement or process death requires explicit reconciliation
and preserves an unknown result. Raw lease secrets are returned once and hashed.
"""

import asyncio
import hmac
import secrets
from uuid import uuid4

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from nexus.contracts.agent_evaluation import (
    CompleteHostedEvaluationCase,
    CreateMigrationPolicy,
    EvaluationCaseLease,
    HostedEvaluationCase,
    HostedEvaluationCasePage,
    HostedEvaluationClaim,
    HostedEvaluationPage,
    HostedEvaluationRun,
    MigrationComparison,
    MigrationPolicy,
    MigrationPolicyPage,
    ObserveSourceEvaluation,
    StartHostedEvaluation,
)
from nexus.contracts.agent_execution import ExecutionGrant, IssueExecutionGrant
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.execution import grants
from nexus.services.product_agent_registry.store import RegistryError, digest

metadata = MetaData()
runs = Table(
    "product_agent_hosted_evaluations",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("asset_id", String(150), nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("request_digest", String(71), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "request_id"),
)
cases = Table(
    "product_agent_hosted_evaluation_cases",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("run_id", String(150), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("status", String(20), nullable=False),
    Column("document", Text, nullable=False),
    Column("lease_hash", String(71)),
    Column("claimed_by", String(255)),
    Column("completion_digest", String(71)),
    UniqueConstraint("zone", "run_id", "ordinal"),
)
policies = Table(
    "product_agent_migration_policies",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("asset_id", String(150), nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("request_digest", String(71), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "request_id"),
)
comparisons = Table(
    "product_agent_migration_comparisons",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("candidate_run_id", String(150), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "candidate_run_id"),
)


def initialize_hosted_evaluations_schema(engine):
    metadata.create_all(engine)


def require_migration_comparison(con, zone, record, report):
    """Admission reads owner-computed evidence; attestor scores are not authority."""
    from nexus.contracts.agent_hosted import HostedArtifact
    from nexus.services.product_agent_registry.hosted_artifacts import artifacts

    if not set(metadata.tables).issubset(inspect(con).get_table_names()):
        raise RegistryError("EVALUATION_RUN_SCHEMA_REQUIRED", 503)
    document = con.execute(
        select(comparisons.c.document).where(
            comparisons.c.zone == zone,
            comparisons.c.id == report.evaluation_id,
        )
    ).scalar_one_or_none()
    if document is None:
        raise RegistryError("AGENT_MIGRATION_COMPARISON_REQUIRED")
    comparison = MigrationComparison.model_validate_json(document)
    policy_document = con.execute(
        select(policies.c.document).where(
            policies.c.zone == zone,
            policies.c.id == comparison.policy_id,
        )
    ).scalar_one_or_none()
    if policy_document is None:
        raise RegistryError("AGENT_MIGRATION_COMPARISON_REQUIRED")
    policy = MigrationPolicy.model_validate_json(policy_document)
    if comparison.execution_kind != policy.execution_kind or (
        comparison.execution_kind == "native"
    ) != (report.level == "L3"):
        raise RegistryError("AGENT_MIGRATION_COMPARISON_MISMATCH")
    if comparison.execution_kind == "native":
        from nexus.services.product_agent_registry.native_conversions import (
            NativeConversions,
            conversions,
        )

        artifact_table = conversions
    else:
        artifact_table = artifacts
    artifact_document = con.execute(
        select(artifact_table.c.document).where(
            artifact_table.c.zone == zone,
            artifact_table.c.id == policy.artifact_id,
        )
    ).scalar_one_or_none()
    if artifact_document is None:
        raise RegistryError("AGENT_MIGRATION_COMPARISON_REQUIRED")
    artifact = (
        NativeConversions._decode(artifact_document)
        if comparison.execution_kind == "native"
        else HostedArtifact.model_validate_json(artifact_document)
    )
    if (
        not comparison.passed
        or comparison.asset_id != record.id
        or comparison.metadata_digest != record.metadata_digest
        or comparison.spec_digest != report.spec_digest
        or comparison.dataset_digest != report.dataset_digest
        or comparison.rubric_digest != report.rubric_digest
        or artifact.asset_id != record.id
        or artifact.metadata_digest != record.metadata_digest
        or artifact.spec_digest != report.spec_digest
        or artifact.image.image_id != report.execution_revision
        or report.before_score != comparison.baseline_passed / comparison.total_cases
        or report.after_score != comparison.candidate_passed / comparison.total_cases
        or report.allowed_drop != comparison.maximum_drop_bps / 10000
        or digest(canonical_bytes(comparison.model_dump())) not in report.evidence_refs
    ):
        raise RegistryError("AGENT_MIGRATION_COMPARISON_MISMATCH")


class HostedEvaluations:
    def __init__(self, registry, execution):
        self.registry, self.execution, self.engine = registry, execution, registry.engine

    def _authorize(self, actor):
        self.execution._policy(actor, "evaluators")
        self.execution._policy(actor, "gateways")
        if not set(metadata.tables).issubset(inspect(self.engine).get_table_names()):
            raise RegistryError("EVALUATION_RUN_SCHEMA_REQUIRED", 503)

    def _load(self, con, actor, run_id):
        document = con.execute(
            select(runs.c.document).where(runs.c.zone == actor.zone, runs.c.id == run_id)
        ).scalar_one_or_none()
        if document is None:
            raise RegistryError("EVALUATION_RUN_NOT_FOUND", 404)
        return HostedEvaluationRun.model_validate_json(document)

    def _admit(self, con, actor, run):
        asset = self.registry.inventory._get(con, actor.zone, run.asset_id, lock=True)
        if (
            asset.status != "approved"
            or not asset.metadata.owner_user_id
            or asset.metadata_digest != run.metadata_digest
            or asset.metadata.hosting != "internal_hosted"
        ):
            raise RegistryError("EVALUATION_ASSET_NOT_ADMITTED")

    def _artifact(self, actor, request):
        return (
            self.registry.native.get(actor, request.artifact_id)
            if request.execution_kind == "native"
            else self.registry.hosted.get(actor, request.artifact_id)
        )

    def start(self, actor, command: StartHostedEvaluation):
        self._authorize(actor)
        artifact = self._artifact(actor, command)
        dataset = self.registry.evaluation.get(actor, command.dataset_id)
        if (
            artifact.asset_id != command.asset_id
            or dataset.asset_id != command.asset_id
            or artifact.metadata_digest != command.metadata_digest
            or dataset.metadata_digest != command.metadata_digest
        ):
            raise RegistryError("EVALUATION_INPUT_MISMATCH", 400)
        source = command.source_runtime
        if source and command.execution_kind == "native":
            # The source is the exact reviewed L2 artifact recorded by the owner,
            # evaluated under the target's own permissions, not inherited grants.
            if (
                source.profile != "registry-hosted-artifact-v1"
                or source.source_digest != artifact.image.source_digest
                or source.model_name != artifact.image.model_name
                or source.runtime_digest != artifact.source_spec_digest
                or source.revision_strength != "local_source_digest"
            ):
                raise RegistryError("EVALUATION_SOURCE_MISMATCH", 400)
        elif source and (
            source.source_digest != artifact.image.source_digest
            or source.model_name != artifact.image.model_name
            or (source.profile, source.revision_strength)
            != (
                ("dify-service-api-v1", "observed_source_snapshot")
                if artifact.image.profile.startswith("dify-")
                else ("langgraph-source-v1", "local_source_digest")
            )
        ):
            raise RegistryError("EVALUATION_SOURCE_MISMATCH", 400)
        policy = self.execution._policy(actor, "evaluators")
        if command.max_model_calls > policy.max_model_calls or not any(
            p.operation == "model.invoke" and p.resource == artifact.image.model_resource
            for p in policy.evaluation_permissions
        ):
            raise RegistryError("EXECUTION_SCOPE_DENIED", 403)
        payload = command.model_dump()
        if command.role == "hosted" and source is None and command.comparison_policy_id is None:
            # Preserve pre-profile request fingerprints across additive upgrades.
            for field in ("role", "source_runtime", "comparison_policy_id"):
                payload.pop(field)
        fingerprint = digest(canonical_bytes({"actor": actor.subject, "command": payload}))

        def previous(con):
            return (
                con.execute(
                    select(runs).where(
                        runs.c.zone == actor.zone, runs.c.request_id == command.request_id
                    )
                )
                .mappings()
                .first()
            )

        def replay(row):
            if row["request_digest"] != fingerprint:
                raise RegistryError("EVALUATION_IDEMPOTENCY_CONFLICT")
            return row["id"]

        try:
            with self.registry.inventory._transaction() as con:
                row = previous(con)
                if row:
                    run_id = replay(row)
                else:
                    count = len(dataset.dataset.cases)
                    record = HostedEvaluationRun(
                        id=str(uuid4()),
                        zone=actor.zone,
                        asset_id=command.asset_id,
                        metadata_digest=command.metadata_digest,
                        artifact_id=artifact.id,
                        spec_digest=artifact.spec_digest,
                        dataset_id=dataset.id,
                        dataset_digest=dataset.dataset_digest,
                        rubric_digest=dataset.rubric_digest,
                        initiated_by=command.initiated_by,
                        created_by=actor.subject,
                        created_at=self.registry.clock(),
                        timeout_seconds=command.timeout_seconds,
                        max_model_calls=command.max_model_calls,
                        total_cases=count,
                        queued_cases=count,
                        running_cases=0,
                        finished_cases=0,
                        passed_cases=0,
                        status="queued",
                        role=command.role,
                        execution_kind=command.execution_kind,
                        source_runtime=source,
                        comparison_policy_id=command.comparison_policy_id,
                    )
                    self._admit(con, actor, record)
                    if command.comparison_policy_id:
                        policy = self._policy_record(con, actor, command.comparison_policy_id)
                        if record.role != "hosted" or any(
                            getattr(record, key) != getattr(policy, key)
                            for key in (
                                "asset_id",
                                "metadata_digest",
                                "artifact_id",
                                "spec_digest",
                                "dataset_id",
                                "dataset_digest",
                                "rubric_digest",
                                "total_cases",
                                "execution_kind",
                            )
                        ):
                            raise RegistryError("EVALUATION_POLICY_MISMATCH")
                    # Asset lock makes the bounded admission atomic across coordinators.
                    active = con.execute(
                        select(func.count())
                        .select_from(
                            cases.join(
                                runs, (cases.c.zone == runs.c.zone) & (cases.c.run_id == runs.c.id)
                            )
                        )
                        .where(
                            runs.c.zone == actor.zone,
                            runs.c.asset_id == record.asset_id,
                            cases.c.status.in_(["queued", "running"]),
                        )
                    ).scalar_one()
                    if active + count > 200:
                        raise RegistryError("EVALUATION_CAPACITY_LIMIT", 429)
                    con.execute(
                        insert(runs).values(
                            zone=actor.zone,
                            id=record.id,
                            asset_id=record.asset_id,
                            request_id=command.request_id,
                            request_digest=fingerprint,
                            document=record.model_dump_json(),
                        )
                    )
                    for ordinal, sample in enumerate(dataset.dataset.cases):
                        case = HostedEvaluationCase(
                            id=str(uuid4()), run_id=record.id, sample_id=sample.id, status="queued"
                        )
                        con.execute(
                            insert(cases).values(
                                zone=actor.zone,
                                id=case.id,
                                run_id=record.id,
                                ordinal=ordinal,
                                status=case.status,
                                document=case.model_dump_json(),
                            )
                        )
                    run_id = record.id
        except IntegrityError:
            with self.engine.connect() as con:
                row = previous(con)
                if not row:
                    raise RegistryError("EVALUATION_WRITE_CONFLICT") from None
                run_id = replay(row)
        return self.get(actor, run_id)

    def _present(self, con, actor, record):
        documents = (
            con.execute(
                select(cases.c.document).where(
                    cases.c.zone == actor.zone, cases.c.run_id == record.id
                )
            )
            .scalars()
            .all()
        )
        values = [HostedEvaluationCase.model_validate_json(doc) for doc in documents]
        queued = sum(case.status == "queued" for case in values)
        running = sum(case.status == "running" for case in values)
        return record.model_copy(
            update={
                "queued_cases": queued,
                "running_cases": running,
                "finished_cases": len(values) - queued - running,
                "passed_cases": sum(case.passed for case in values),
                "status": "finished"
                if not queued and not running
                else "queued"
                if queued == len(values)
                else "running",
            }
        )

    def get(self, actor, run_id):
        self._authorize(actor)
        with self.engine.connect() as con:
            return self._present(con, actor, self._load(con, actor, run_id))

    def list(self, actor, asset_id, after=""):
        self._authorize(actor)
        self.registry.inventory.get(actor, asset_id)
        with self.engine.connect() as con:
            documents = (
                con.execute(
                    select(runs.c.document)
                    .where(
                        runs.c.zone == actor.zone, runs.c.asset_id == asset_id, runs.c.id > after
                    )
                    .order_by(runs.c.id)
                    .limit(6)
                )
                .scalars()
                .all()
            )
            records = [
                self._present(con, actor, HostedEvaluationRun.model_validate_json(doc))
                for doc in documents
            ]
        return HostedEvaluationPage(
            items=records[:5], next_cursor=records[4].id if len(records) > 5 else None
        )

    def list_cases(self, actor, run_id, after=""):
        self._authorize(actor)
        with self.engine.connect() as con:
            self._load(con, actor, run_id)
            documents = (
                con.execute(
                    select(cases.c.document)
                    .where(cases.c.zone == actor.zone, cases.c.run_id == run_id, cases.c.id > after)
                    .order_by(cases.c.id)
                    .limit(11)
                )
                .scalars()
                .all()
            )
        records = [HostedEvaluationCase.model_validate_json(doc) for doc in documents]
        return HostedEvaluationCasePage(
            items=records[:10], next_cursor=records[9].id if len(records) > 10 else None
        )

    def claim(self, actor, run_id):
        self._authorize(actor)
        with self.registry.inventory._transaction() as con:
            run = self._load(con, actor, run_id)
            self._admit(con, actor, run)  # locks asset, serializing claims for this run
            unresolved = (
                con.execute(
                    select(cases.c.document).where(
                        cases.c.zone == actor.zone,
                        cases.c.run_id == run_id,
                        cases.c.status.in_(["failed", "unknown"]),
                    )
                )
                .scalars()
                .all()
            )
            if any(
                not HostedEvaluationCase.model_validate_json(doc).cleanup_confirmed
                or not HostedEvaluationCase.model_validate_json(doc).iam_cleanup_confirmed
                for doc in unresolved
            ):
                raise RegistryError("EVALUATION_CLEANUP_UNCONFIRMED")
            running = con.execute(
                select(cases.c.id).where(
                    cases.c.zone == actor.zone,
                    cases.c.run_id == run_id,
                    cases.c.status == "running",
                )
            ).first()
            if running:
                raise RegistryError("EVALUATION_CASE_IN_PROGRESS")
            row = (
                con.execute(
                    select(cases)
                    .where(
                        cases.c.zone == actor.zone,
                        cases.c.run_id == run_id,
                        cases.c.status == "queued",
                    )
                    .order_by(cases.c.ordinal)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            case = HostedEvaluationCase.model_validate_json(row["document"])
            now, lease = self.registry.clock(), secrets.token_urlsafe(32)
            case = case.model_copy(
                update={
                    "status": "running",
                    "started_at": now,
                    "deadline_at": now + (run.timeout_seconds + 20) * 1000,
                }
            )
            con.execute(
                update(cases)
                .where(cases.c.zone == actor.zone, cases.c.id == case.id)
                .values(
                    status=case.status,
                    document=case.model_dump_json(),
                    claimed_by=actor.subject,
                    lease_hash=digest(lease.encode()),
                )
            )
            return HostedEvaluationClaim(case=case, lease_token=lease)

    def _case(self, con, actor, run_id, case_id, lease):
        row = (
            con.execute(
                select(cases).where(
                    cases.c.zone == actor.zone, cases.c.run_id == run_id, cases.c.id == case_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RegistryError("EVALUATION_CASE_NOT_FOUND", 404)
        if (
            row["claimed_by"] != actor.subject
            or not row["lease_hash"]
            or not hmac.compare_digest(row["lease_hash"], digest(lease.encode()))
        ):
            raise RegistryError("EVALUATION_CLAIM_DENIED", 403)
        return row, HostedEvaluationCase.model_validate_json(row["document"])

    def _grant(self, actor, run, case, grant_id):
        grant = self.execution.get(actor, grant_id)
        request = grant.request
        if (
            grant.issuer != actor.subject
            or request.asset_id != run.asset_id
            or request.task_id != case.id
            or request.spec_digest != run.spec_digest
            or request.dataset_digest != run.dataset_digest
            or request.metadata_digest != run.metadata_digest
            or request.mode != "evaluation"
            or request.initiated_by != run.initiated_by
            or request.max_model_calls != run.max_model_calls
        ):
            raise RegistryError("EVALUATION_GRANT_MISMATCH", 403)
        return grant

    def _reserved_grant(self, actor, case_id):
        with self.engine.connect() as con:
            doc = con.execute(
                select(grants.c.document).where(
                    grants.c.zone == actor.zone,
                    grants.c.issuer == actor.subject,
                    grants.c.request_id == "evaluation-" + case_id,
                )
            ).scalar_one_or_none()
        return ExecutionGrant.model_validate_json(doc) if doc else None

    async def issue_grant(self, actor, run_id, command: EvaluationCaseLease):
        self._authorize(actor)
        with self.engine.connect() as con:
            run = self._load(con, actor, run_id)
            _, case = self._case(con, actor, run_id, command.case_id, command.lease_token)
        if case.status != "running" or case.deadline_at <= self.registry.clock():
            raise RegistryError("EVALUATION_CASE_EXPIRED")
        artifact = self._artifact(actor, run)
        # Deterministic request recovers an IAM grant even if the response/binding
        # was lost. IAM never returns its key twice; a lost key cannot replay work.
        request = IssueExecutionGrant(
            request_id="evaluation-" + case.id,
            asset_id=run.asset_id,
            metadata_digest=run.metadata_digest,
            spec_digest=run.spec_digest,
            task_id=case.id,
            initiated_by=run.initiated_by,
            mode="evaluation",
            dataset_digest=run.dataset_digest,
            max_model_calls=run.max_model_calls,
            ttl_seconds=run.timeout_seconds + 10,
            permissions=[{"operation": "model.invoke", "resource": artifact.image.model_resource}],
        )
        result = await self.execution.issue(actor, request)
        try:
            with self.registry.inventory._transaction() as con:
                self._admit(con, actor, run)
                _, current = self._case(con, actor, run_id, command.case_id, command.lease_token)
                if current.status != "running" or current.deadline_at <= self.registry.clock():
                    raise RegistryError("EVALUATION_CASE_EXPIRED")
                current = current.model_copy(update={"grant_id": result.grant.id})
                con.execute(
                    update(cases)
                    .where(cases.c.zone == actor.zone, cases.c.id == current.id)
                    .values(document=current.model_dump_json())
                )
        except Exception:
            await asyncio.to_thread(self.execution.revoke, actor, result.grant.id)
            raise
        return result

    def _cleanup_grant(self, actor, run, current):
        # Also finds an issuance that crashed before binding its grant ID.
        grant = self._reserved_grant(actor, current.id)
        if grant is None:
            return None
        self._grant(actor, run, current, grant.id)
        return self.execution.revoke(actor, grant.id)

    def observe_source(self, actor, run_id, command: ObserveSourceEvaluation):
        self._authorize(actor)
        with self.registry.inventory._transaction() as con:
            run = self._load(con, actor, run_id)
            self.registry.inventory._get(con, actor.zone, run.asset_id, lock=True)
            _, current = self._case(con, actor, run_id, command.case_id, command.lease_token)
            observed = command.source_execution
            if (
                run.role != "source_baseline"
                or run.execution_kind == "native"
                or run.source_runtime is None
                or observed.profile != run.source_runtime.profile
                or observed.request_id != current.id
            ):
                raise RegistryError("EVALUATION_SOURCE_MISMATCH", 400)
            if current.status != "running" or current.deadline_at <= self.registry.clock():
                raise RegistryError("EVALUATION_CASE_EXPIRED")
            if current.source_execution:
                prior = current.source_execution.model_dump(exclude_none=True)
                incoming = observed.model_dump(exclude_none=True)
                if any(incoming.get(key) != value for key, value in prior.items()):
                    raise RegistryError("EVALUATION_SOURCE_REFERENCE_CHANGED")
            current = current.model_copy(update={"source_execution": observed})
            con.execute(
                update(cases)
                .where(cases.c.zone == actor.zone, cases.c.id == current.id)
                .values(document=current.model_dump_json())
            )
            return current

    def complete(self, actor, run_id, command: CompleteHostedEvaluationCase):
        self._authorize(actor)
        fingerprint = digest(
            canonical_bytes(
                {"actor": actor.subject, "command": command.model_dump(exclude={"lease_token"})}
            )
        )
        with self.engine.connect() as con:
            run = self._load(con, actor, run_id)
        dataset = self.registry.evaluation.get(actor, run.dataset_id)
        with self.engine.connect() as con:
            row, prior = self._case(con, actor, run_id, command.case_id, command.lease_token)
            if prior.status != "running":
                if row["completion_digest"] == fingerprint:
                    return prior
                raise RegistryError("EVALUATION_CASE_CONFLICT")
        grant = self._cleanup_grant(actor, run, prior)
        iam_clean = grant is None or not grant.cleanup_pending
        if command.status == "succeeded" and not iam_clean:
            raise RegistryError("EVALUATION_IAM_CLEANUP_PENDING", 503)
        native_verification = None
        if (
            run.execution_kind == "native"
            and run.role == "hosted"
            and command.status == "succeeded"
        ):
            from nexus.services.product_agent_registry.native_execution import (
                execution_profile,
                verify_output,
            )

            conversion = self.registry.native.get(actor, run.artifact_id)
            native_verification = verify_output(
                execution_profile(conversion),
                command.answer,
                command.model_call_ids,
                [frame.model_dump() for frame in command.trace],
                execution_succeeded=True,
                cleanup_confirmed=command.cleanup_confirmed,
            )
            if native_verification.verdict != "passed":
                raise RegistryError("NATIVE_OUTPUT_VERIFICATION_FAILED", 400)
        with self.registry.inventory._transaction() as con:
            # Serialize completion/reconciliation with claim, without requiring a
            # still-enabled asset to record an already-performed operation.
            self.registry.inventory._get(con, actor.zone, run.asset_id, lock=True)
            row, current = self._case(con, actor, run_id, command.case_id, command.lease_token)
            if current.status != "running":
                if row["completion_digest"] == fingerprint:
                    return current
                raise RegistryError("EVALUATION_CASE_CONFLICT")
            if current.deadline_at <= self.registry.clock():
                raise RegistryError("EVALUATION_CASE_EXPIRED")
            if (command.status == "succeeded" or command.model_call_ids) and not grant:
                raise RegistryError("EVALUATION_GRANT_REQUIRED", 400)
            if (
                command.status == "succeeded"
                and run.execution_kind == "native"
                and len(command.model_call_ids) != 1
            ):
                raise RegistryError("NATIVE_MODEL_EVIDENCE_REQUIRED", 400)
            if (
                command.status == "succeeded"
                and run.role == "source_baseline"
                and run.execution_kind == "hosted"
            ):
                source = current.source_execution
                if source is None or (
                    source.profile == "dify-service-api-v1"
                    and not (source.workflow_run_id and source.message_id)
                ):
                    raise RegistryError("EVALUATION_SOURCE_EVIDENCE_REQUIRED", 400)
            if len(command.model_call_ids) > run.max_model_calls:
                raise RegistryError("EVALUATION_RESULT_INVALID", 400)
            sample = next(
                sample for sample in dataset.dataset.cases if sample.id == current.sample_id
            )
            passed = command.status == "succeeded" and command.answer.strip(
                " \t\n\r\v\f"
            ) == sample.expected.strip(" \t\n\r\v\f")
            current = current.model_copy(
                update={
                    **command.model_dump(exclude={"lease_token", "case_id"}),
                    "trace": command.trace,
                    "grant_id": grant.id if grant else current.grant_id,
                    "iam_cleanup_confirmed": iam_clean,
                    "trace_digest": digest(
                        canonical_bytes([frame.model_dump() for frame in command.trace])
                    ),
                    "passed": passed,
                    "native_verification": native_verification,
                    "completed_at": self.registry.clock(),
                }
            )
            con.execute(
                update(cases)
                .where(cases.c.zone == actor.zone, cases.c.id == current.id)
                .values(
                    status=current.status,
                    document=current.model_dump_json(),
                    completion_digest=fingerprint,
                )
            )
            return current

    def reconcile(self, actor, run_id):
        """Explicitly close expired cases as unknown. Never executes or claims them again."""
        self._authorize(actor)
        with self.registry.inventory._transaction() as con:
            run = self._load(con, actor, run_id)
            self.registry.inventory._get(con, actor.zone, run.asset_id, lock=True)
            documents = (
                con.execute(
                    select(cases.c.document).where(
                        cases.c.zone == actor.zone,
                        cases.c.run_id == run_id,
                        cases.c.status == "running",
                    )
                )
                .scalars()
                .all()
            )
            for document in documents:
                current = HostedEvaluationCase.model_validate_json(document)
                if current.deadline_at > self.registry.clock():
                    continue
                current = current.model_copy(
                    update={
                        "status": "unknown",
                        "error_code": "EVALUATION_CASE_INTERRUPTED",
                        "completed_at": self.registry.clock(),
                        "passed": False,
                        "cleanup_confirmed": False,
                    }
                )
                con.execute(
                    update(cases)
                    .where(cases.c.zone == actor.zone, cases.c.id == current.id)
                    .values(status=current.status, document=current.model_dump_json())
                )
        with self.engine.connect() as con:
            pending = (
                con.execute(
                    select(cases.c.document).where(
                        cases.c.zone == actor.zone,
                        cases.c.run_id == run_id,
                        cases.c.status.in_(["unknown", "failed"]),
                    )
                )
                .scalars()
                .all()
            )
        for document in pending:
            current = HostedEvaluationCase.model_validate_json(document)
            grant = self._cleanup_grant(actor, run, current)
            with self.registry.inventory._transaction() as con:
                self.registry.inventory._get(con, actor.zone, run.asset_id, lock=True)
                current = current.model_copy(
                    update={
                        "grant_id": grant.id if grant else current.grant_id,
                        "iam_cleanup_confirmed": grant is None or not grant.cleanup_pending,
                    }
                )
                con.execute(
                    update(cases)
                    .where(cases.c.zone == actor.zone, cases.c.id == current.id)
                    .values(document=current.model_dump_json())
                )
        return self.get(actor, run_id)

    def stop(self, actor, run_id):
        """Stop further admission and revoke IAM, without claiming Docker cancellation."""
        self._authorize(actor)
        with self.registry.inventory._transaction() as con:
            run = self._load(con, actor, run_id)
            self.registry.inventory._get(con, actor.zone, run.asset_id, lock=True)
            documents = (
                con.execute(
                    select(cases.c.document).where(
                        cases.c.zone == actor.zone,
                        cases.c.run_id == run_id,
                        cases.c.status.in_(["queued", "running"]),
                    )
                )
                .scalars()
                .all()
            )
            for document in documents:
                current = HostedEvaluationCase.model_validate_json(document)
                queued = current.status == "queued"
                current = current.model_copy(
                    update={
                        "status": "failed" if queued else "unknown",
                        "error_code": "EVALUATION_STOPPED",
                        "completed_at": self.registry.clock(),
                        "passed": False,
                        "cleanup_confirmed": queued,
                        "iam_cleanup_confirmed": queued,
                    }
                )
                con.execute(
                    update(cases)
                    .where(cases.c.zone == actor.zone, cases.c.id == current.id)
                    .values(status=current.status, document=current.model_dump_json())
                )
        return self.reconcile(actor, run_id)

    def _policy_record(self, con, actor, policy_id):
        document = con.execute(
            select(policies.c.document).where(
                policies.c.zone == actor.zone,
                policies.c.id == policy_id,
            )
        ).scalar_one_or_none()
        if document is None:
            raise RegistryError("EVALUATION_POLICY_NOT_FOUND", 404)
        return MigrationPolicy.model_validate_json(document)

    def get_policy(self, actor, policy_id):
        self._authorize(actor)
        with self.engine.connect() as con:
            return self._policy_record(con, actor, policy_id)

    def list_policies(self, actor, asset_id, after=""):
        self._authorize(actor)
        self.registry.inventory.get(actor, asset_id)
        with self.engine.connect() as con:
            documents = (
                con.execute(
                    select(policies.c.document)
                    .where(
                        policies.c.zone == actor.zone,
                        policies.c.asset_id == asset_id,
                        policies.c.id > after,
                    )
                    .order_by(policies.c.id)
                    .limit(21)
                )
                .scalars()
                .all()
            )
        records = [MigrationPolicy.model_validate_json(doc) for doc in documents]
        return MigrationPolicyPage(
            items=records[:20], next_cursor=records[19].id if len(records) > 20 else None
        )

    def _settled(self, con, actor, run):
        documents = (
            con.execute(
                select(cases.c.document).where(
                    cases.c.zone == actor.zone,
                    cases.c.run_id == run.id,
                )
            )
            .scalars()
            .all()
        )
        values = [HostedEvaluationCase.model_validate_json(doc) for doc in documents]
        if len(values) != run.total_cases or any(
            row.status not in ("succeeded", "failed")
            or not row.cleanup_confirmed
            or not row.iam_cleanup_confirmed
            for row in values
        ):
            raise RegistryError("EVALUATION_NOT_SETTLED")
        # Never trust cached counters or evaluator-supplied aggregate scores.
        return sum(row.passed for row in values)

    def create_policy(self, actor, command: CreateMigrationPolicy):
        self._authorize(actor)
        fingerprint = digest(
            canonical_bytes({"actor": actor.subject, "command": command.model_dump()})
        )
        with self.registry.inventory._transaction() as con:
            baseline = self._load(con, actor, command.baseline_run_id)
            self._admit(con, actor, baseline)  # locks the asset before idempotency and facts
            previous = (
                con.execute(
                    select(policies).where(
                        policies.c.zone == actor.zone,
                        policies.c.request_id == command.request_id,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["request_digest"] != fingerprint:
                    raise RegistryError("EVALUATION_IDEMPOTENCY_CONFLICT")
                return MigrationPolicy.model_validate_json(previous["document"])
            if baseline.role != "source_baseline" or baseline.source_runtime is None:
                raise RegistryError("EVALUATION_SOURCE_BASELINE_REQUIRED")
            passed = self._settled(con, actor, baseline)
            policy = MigrationPolicy(
                id=str(uuid4()),
                zone=actor.zone,
                **{
                    key: getattr(baseline, key)
                    for key in (
                        "asset_id",
                        "metadata_digest",
                        "artifact_id",
                        "spec_digest",
                        "dataset_id",
                        "dataset_digest",
                        "rubric_digest",
                        "total_cases",
                        "source_runtime",
                        "execution_kind",
                    )
                },
                baseline_run_id=baseline.id,
                baseline_passed=passed,
                minimum_pass_bps=command.minimum_pass_bps,
                maximum_drop_bps=command.maximum_drop_bps,
                initiated_by=command.initiated_by,
                created_by=actor.subject,
                created_at=self.registry.clock(),
            )
            con.execute(
                insert(policies).values(
                    zone=actor.zone,
                    id=policy.id,
                    asset_id=policy.asset_id,
                    request_id=command.request_id,
                    request_digest=fingerprint,
                    document=policy.model_dump_json(),
                )
            )
            return policy

    def compare(self, actor, candidate_run_id):
        self._authorize(actor)
        with self.registry.inventory._transaction() as con:
            candidate = self._load(con, actor, candidate_run_id)
            self._admit(con, actor, candidate)
            previous = con.execute(
                select(comparisons.c.document).where(
                    comparisons.c.zone == actor.zone,
                    comparisons.c.candidate_run_id == candidate_run_id,
                )
            ).scalar_one_or_none()
            if previous:
                return MigrationComparison.model_validate_json(previous)
            if candidate.role != "hosted" or candidate.comparison_policy_id is None:
                raise RegistryError("EVALUATION_POLICY_REQUIRED")
            policy = self._policy_record(con, actor, candidate.comparison_policy_id)
            if candidate.created_at < policy.created_at or any(
                getattr(candidate, key) != getattr(policy, key)
                for key in (
                    "asset_id",
                    "metadata_digest",
                    "artifact_id",
                    "spec_digest",
                    "dataset_id",
                    "dataset_digest",
                    "rubric_digest",
                    "total_cases",
                    "execution_kind",
                )
            ):
                raise RegistryError("EVALUATION_POLICY_MISMATCH")
            before = self._settled(con, actor, self._load(con, actor, policy.baseline_run_id))
            after = self._settled(con, actor, candidate)
            if before != policy.baseline_passed:
                raise RegistryError("EVALUATION_POLICY_MISMATCH")
            total = candidate.total_cases
            comparison = MigrationComparison(
                id=str(uuid4()),
                zone=actor.zone,
                policy_id=policy.id,
                baseline_run_id=policy.baseline_run_id,
                candidate_run_id=candidate.id,
                **{
                    key: getattr(candidate, key)
                    for key in (
                        "asset_id",
                        "metadata_digest",
                        "spec_digest",
                        "dataset_digest",
                        "rubric_digest",
                        "execution_kind",
                    )
                },
                total_cases=total,
                baseline_passed=before,
                candidate_passed=after,
                minimum_pass_bps=policy.minimum_pass_bps,
                maximum_drop_bps=policy.maximum_drop_bps,
                passed=(
                    after * 10000 >= policy.minimum_pass_bps * total
                    and (before - after) * 10000 <= policy.maximum_drop_bps * total
                ),
                created_by=actor.subject,
                created_at=self.registry.clock(),
            )
            con.execute(
                insert(comparisons).values(
                    zone=actor.zone,
                    id=comparison.id,
                    candidate_run_id=candidate.id,
                    document=comparison.model_dump_json(),
                )
            )
            return comparison

    def get_comparison(self, actor, comparison_id):
        self._authorize(actor)
        with self.engine.connect() as con:
            document = con.execute(
                select(comparisons.c.document).where(
                    comparisons.c.zone == actor.zone,
                    comparisons.c.id == comparison_id,
                )
            ).scalar_one_or_none()
        if document is None:
            raise RegistryError("EVALUATION_COMPARISON_NOT_FOUND", 404)
        return MigrationComparison.model_validate_json(document)
