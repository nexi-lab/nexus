"""Experimental native conversion materials; no object here grants L3 admission.

Native images remain distinct from hosted platform images. The source commit is
a provenance claim which an independent attestor must verify against the build.
"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from nexus.contracts.agent_hosted import DifyTextPlan
from nexus.contracts.agent_inventory import AcceptanceReport, Identifier, Label
from nexus.contracts.product_agent_registry import API, Digest, Stamp, WireModel

NATIVE_PROFILE = "sudocode-dify-independent-text-v1alpha1"


class NativeRuntime(WireModel):
    kind: Literal["sudocode"] = "sudocode"
    commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    executable_digest: Digest
    runner_digest: Digest
    platform: Literal["linux/amd64", "linux/arm64"]


class NativeDefinition(WireModel):
    profile: Literal[NATIVE_PROFILE] = NATIVE_PROFILE
    plan: DifyTextPlan
    tools: Annotated[list[str], Field(max_length=0)] = Field(default_factory=list)
    memory: Literal["disabled"] = "disabled"
    input: Literal["literal_text"] = "literal_text"
    max_model_calls: Literal[1] = 1

    @model_validator(mode="after")
    def supported_source(self):
        if (
            len(self.plan.prompts) != 1
            or self.plan.prompts[0].role != "system"
            or not self.plan.prompts[0].content.strip()
        ):
            raise ValueError("NATIVE_SOURCE_PROMPT_PROFILE_UNSUPPORTED")
        return self


class NativeImage(WireModel):
    """A fixed native runtime candidate, not a renamed hosted artifact."""

    api_version: Literal[API] = API
    kind: Literal["NativeImage"] = "NativeImage"
    profile: Literal[NATIVE_PROFILE] = NATIVE_PROFILE
    image_id: Digest
    source_digest: Digest
    dependency_lock_digest: Digest
    protocol: Literal["agent-hosted-stdio/v1"] = "agent-hosted-stdio/v1"
    model_resource: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,499}$")]
    model_name: Annotated[str, Field(min_length=1, max_length=200)]
    max_output_tokens: Annotated[int, Field(ge=1, le=4096)] = 128
    definition_digest: Digest
    runtime: NativeRuntime


class RegisterNativeConversion(WireModel):
    api_version: Literal[API] = API
    kind: Literal["RegisterNativeConversion"] = "RegisterNativeConversion"
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    source_artifact_id: Identifier
    asset_id: Identifier
    metadata_digest: Digest
    image: NativeImage
    initiated_by: Identifier


class NativeConversion(WireModel):
    api_version: Literal[API] = API
    kind: Literal["NativeConversion"] = "NativeConversion"
    id: Identifier
    zone: Identifier
    asset_id: Identifier
    metadata_digest: Digest
    source_asset_id: Identifier
    source_artifact_id: Identifier
    source_metadata_digest: Digest
    source_spec_digest: Digest
    source_acceptance: AcceptanceReport
    source_acceptance_issuer: Label
    source_acceptance_digest: Digest
    image: NativeImage
    definition: NativeDefinition
    spec_digest: Digest
    spec_payload_base64: Annotated[str, Field(min_length=1, max_length=750000)]
    created_by: Label
    initiated_by: Identifier
    created_at: Stamp
    evidence: Literal["registered_only"] = "registered_only"


class NativeConversionPage(WireModel):
    kind: Literal["NativeConversionPage"] = "NativeConversionPage"
    items: list[NativeConversion]
    next_cursor: Identifier | None
