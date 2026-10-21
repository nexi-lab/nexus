"""Versioned, immutable synthetic-text migration datasets. No automatic L2 attestation."""

from typing import Annotated, Literal

from pydantic import Field, model_serializer, model_validator

from nexus.contracts.agent_inventory import Identifier, Label
from nexus.contracts.agent_native import NativeOutputVerification
from nexus.contracts.product_agent_registry import API, Digest, Stamp, WireModel


class EvaluationCase(WireModel):
    id: Identifier
    query: Annotated[str, Field(min_length=1, max_length=16000)]
    expected: Annotated[str, Field(min_length=1, max_length=32000)]


class EvaluationDatasetInput(WireModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    version: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
    input_scope: Literal["synthetic_text"] = "synthetic_text"
    rubric: Literal["exact-strip-v1"] = "exact-strip-v1"
    cases: Annotated[list[EvaluationCase], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("EVALUATION_DUPLICATE_CASE")
        return self


class RegisterEvaluationDataset(WireModel):
    api_version: Literal[API] = API
    kind: Literal["RegisterEvaluationDataset"] = "RegisterEvaluationDataset"
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    asset_id: Identifier
    metadata_digest: Digest
    dataset: EvaluationDatasetInput
    initiated_by: Identifier


class EvaluationDataset(WireModel):
    api_version: Literal[API] = API
    kind: Literal["EvaluationDataset"] = "EvaluationDataset"
    id: Identifier
    zone: Identifier
    asset_id: Identifier
    metadata_digest: Digest
    dataset_digest: Digest
    rubric_digest: Digest
    dataset: EvaluationDatasetInput
    created_by: Label
    initiated_by: Identifier
    created_at: Stamp


class EvaluationDatasetPage(WireModel):
    kind: Literal["EvaluationDatasetPage"] = "EvaluationDatasetPage"
    items: list[EvaluationDataset]
    next_cursor: Identifier | None


class SourceEvaluationRuntime(WireModel):
    profile: Literal["dify-service-api-v1", "langgraph-source-v1", "registry-hosted-artifact-v1"]
    source_digest: Digest
    runtime_digest: Digest
    model_name: Annotated[str, Field(min_length=1, max_length=200)]
    # A Dify export is an observation of the source, not a remote immutable version.
    revision_strength: Literal["observed_source_snapshot", "local_source_digest"]


class EvaluationExecution(WireModel):
    execution_kind: Literal["hosted", "native"] = "hosted"

    @model_serializer(mode="wrap")
    def preserve_hosted_wire(self, handler):
        # Existing evidence hashes and request fingerprints must remain valid.
        # This discriminator is emitted only for the new native execution path.
        value = handler(self)
        if self.execution_kind == "hosted":
            value.pop("execution_kind", None)
        return value


class StartHostedEvaluation(EvaluationExecution):
    api_version: Literal[API] = API
    kind: Literal["StartHostedEvaluation"] = "StartHostedEvaluation"
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    asset_id: Identifier
    metadata_digest: Digest
    artifact_id: Identifier
    dataset_id: Identifier
    initiated_by: Identifier
    timeout_seconds: Annotated[int, Field(ge=1, le=240)] = 120
    max_model_calls: Annotated[int, Field(ge=1, le=32)] = 1
    role: Literal["hosted", "source_baseline"] = "hosted"
    source_runtime: SourceEvaluationRuntime | None = None
    comparison_policy_id: Identifier | None = None

    @model_validator(mode="after")
    def execution_role(self):
        if (self.role == "source_baseline") != (self.source_runtime is not None):
            raise ValueError("EVALUATION_SOURCE_RUNTIME_REQUIRED")
        if self.role == "source_baseline" and self.comparison_policy_id is not None:
            raise ValueError("EVALUATION_POLICY_MISMATCH")
        if self.source_runtime is not None and (
            (self.source_runtime.profile == "registry-hosted-artifact-v1")
            != (self.execution_kind == "native")
        ):
            raise ValueError("EVALUATION_SOURCE_RUNTIME_MISMATCH")
        if self.execution_kind == "native" and self.max_model_calls != 1:
            raise ValueError("NATIVE_SINGLE_MODEL_CALL_REQUIRED")
        return self


class HostedEvaluationRun(EvaluationExecution):
    api_version: Literal[API] = API
    kind: Literal["HostedEvaluationRun"] = "HostedEvaluationRun"
    id: Identifier
    zone: Identifier
    asset_id: Identifier
    metadata_digest: Digest
    artifact_id: Identifier
    spec_digest: Digest
    dataset_id: Identifier
    dataset_digest: Digest
    rubric_digest: Digest
    initiated_by: Identifier
    created_by: Label
    created_at: Stamp
    timeout_seconds: Annotated[int, Field(ge=1, le=240)]
    max_model_calls: Annotated[int, Field(ge=1, le=32)]
    total_cases: Annotated[int, Field(ge=1, le=100)]
    queued_cases: Annotated[int, Field(ge=0, le=100)]
    running_cases: Annotated[int, Field(ge=0, le=100)]
    finished_cases: Annotated[int, Field(ge=0, le=100)]
    passed_cases: Annotated[int, Field(ge=0, le=100)]
    # Counts are authoritative; callers must not grade only successful responses.
    status: Literal["queued", "running", "finished"]
    evidence: Literal["evaluation_only"] = "evaluation_only"
    role: Literal["hosted", "source_baseline"] = "hosted"
    source_runtime: SourceEvaluationRuntime | None = None
    comparison_policy_id: Identifier | None = None


class CreateMigrationPolicy(WireModel):
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    baseline_run_id: Identifier
    minimum_pass_bps: Annotated[int, Field(ge=0, le=10000)]
    maximum_drop_bps: Annotated[int, Field(ge=0, le=10000)]
    initiated_by: Identifier


class MigrationPolicy(EvaluationExecution):
    kind: Literal["MigrationPolicy"] = "MigrationPolicy"
    id: Identifier
    zone: Identifier
    asset_id: Identifier
    metadata_digest: Digest
    artifact_id: Identifier
    spec_digest: Digest
    dataset_id: Identifier
    dataset_digest: Digest
    rubric_digest: Digest
    baseline_run_id: Identifier
    baseline_passed: Annotated[int, Field(ge=0, le=100)]
    total_cases: Annotated[int, Field(ge=1, le=100)]
    minimum_pass_bps: Annotated[int, Field(ge=0, le=10000)]
    maximum_drop_bps: Annotated[int, Field(ge=0, le=10000)]
    source_runtime: SourceEvaluationRuntime
    initiated_by: Identifier
    created_by: Label
    created_at: Stamp


class MigrationPolicyPage(WireModel):
    kind: Literal["MigrationPolicyPage"] = "MigrationPolicyPage"
    items: list[MigrationPolicy]
    next_cursor: Identifier | None


class MigrationComparison(EvaluationExecution):
    kind: Literal["MigrationComparison"] = "MigrationComparison"
    id: Identifier
    zone: Identifier
    asset_id: Identifier
    policy_id: Identifier
    baseline_run_id: Identifier
    candidate_run_id: Identifier
    metadata_digest: Digest
    spec_digest: Digest
    dataset_digest: Digest
    rubric_digest: Digest
    total_cases: Annotated[int, Field(ge=1, le=100)]
    baseline_passed: Annotated[int, Field(ge=0, le=100)]
    candidate_passed: Annotated[int, Field(ge=0, le=100)]
    minimum_pass_bps: Annotated[int, Field(ge=0, le=10000)]
    maximum_drop_bps: Annotated[int, Field(ge=0, le=10000)]
    passed: bool
    created_by: Label
    created_at: Stamp


class EvaluationTrace(WireModel):
    node: Annotated[str, Field(min_length=1, max_length=100)]
    phase: Literal["started", "completed"]
    origin: Literal["worker_reported"] = "worker_reported"


class SourceEvaluationExecution(WireModel):
    """Source-reported correlation only; never a trusted model bill or version pin."""

    profile: Literal["dify-service-api-v1", "langgraph-source-v1"]
    request_id: Identifier
    task_id: Identifier | None = None
    workflow_run_id: Identifier | None = None
    conversation_id: Identifier | None = None
    message_id: Identifier | None = None
    origin: Literal["source_reported"] = "source_reported"


class HostedEvaluationCase(WireModel):
    api_version: Literal[API] = API
    kind: Literal["HostedEvaluationCase"] = "HostedEvaluationCase"
    id: Identifier
    run_id: Identifier
    sample_id: Identifier
    status: Literal["queued", "running", "succeeded", "failed", "unknown"]
    started_at: Stamp | None = None
    deadline_at: Stamp | None = None
    completed_at: Stamp | None = None
    answer: Annotated[str, Field(max_length=128000)] | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")] | None = None
    passed: bool = False
    model_call_ids: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)
    grant_id: Identifier | None = None
    trace: Annotated[list[EvaluationTrace], Field(max_length=256)] = Field(default_factory=list)
    trace_digest: Digest | None = None
    cleanup_confirmed: bool = False
    iam_cleanup_confirmed: bool = False
    source_execution: SourceEvaluationExecution | None = None
    model_evidence: Literal["evaluator_reported"] = "evaluator_reported"
    native_verification: NativeOutputVerification | None = None

    @model_serializer(mode="wrap")
    def preserve_previous_evidence(self, handler):
        value = handler(self)
        if self.native_verification is None:
            value.pop("native_verification", None)
        return value


class HostedEvaluationPage(WireModel):
    kind: Literal["HostedEvaluationPage"] = "HostedEvaluationPage"
    items: list[HostedEvaluationRun]
    next_cursor: Identifier | None


class HostedEvaluationCasePage(WireModel):
    kind: Literal["HostedEvaluationCasePage"] = "HostedEvaluationCasePage"
    items: list[HostedEvaluationCase]
    next_cursor: Identifier | None


class HostedEvaluationClaim(WireModel):
    kind: Literal["HostedEvaluationClaim"] = "HostedEvaluationClaim"
    case: HostedEvaluationCase
    # Returned once, only to the configured evaluator; never sent to a browser.
    lease_token: Annotated[str, Field(min_length=32, max_length=128, repr=False)]


class EvaluationCaseLease(WireModel):
    case_id: Identifier
    lease_token: Annotated[str, Field(min_length=32, max_length=128, repr=False)]


class ObserveSourceEvaluation(EvaluationCaseLease):
    source_execution: SourceEvaluationExecution


class CompleteHostedEvaluationCase(WireModel):
    case_id: Identifier
    lease_token: Annotated[str, Field(min_length=32, max_length=128, repr=False)]
    status: Literal["succeeded", "failed", "unknown"]
    answer: Annotated[str, Field(max_length=128000)] | None = None
    error_code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")] | None = None
    model_call_ids: Annotated[list[Identifier], Field(max_length=32)] = Field(default_factory=list)
    trace: Annotated[list[EvaluationTrace], Field(max_length=256)] = Field(default_factory=list)
    cleanup_confirmed: bool = False

    @model_validator(mode="after")
    def result_consistency(self):
        if self.status == "succeeded" and (
            self.answer is None
            or self.error_code is not None
            or not self.cleanup_confirmed
            or not self.trace
        ):
            raise ValueError("EVALUATION_RESULT_INVALID")
        if self.status != "succeeded" and (self.answer is not None or self.error_code is None):
            raise ValueError("EVALUATION_RESULT_INVALID")
        if len(set(self.model_call_ids)) != len(self.model_call_ids):
            raise ValueError("EVALUATION_RESULT_INVALID")
        return self
