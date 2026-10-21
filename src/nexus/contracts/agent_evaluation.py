"""Versioned, immutable synthetic-text migration datasets. No automatic L2 attestation."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from nexus.contracts.agent_inventory import Identifier, Label
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
