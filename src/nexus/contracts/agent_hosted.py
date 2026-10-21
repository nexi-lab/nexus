"""Experimental hosted text profile. Importing an artifact does not attest L2."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from nexus.contracts.agent_inventory import Identifier, Label
from nexus.contracts.product_agent_registry import API, Digest, Stamp, WireModel


class HostedPrompt(WireModel):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(max_length=32000)]


class DifyTextPlan(WireModel):
    api_version: Literal[API] = API
    kind: Literal["DifyTextPlan"] = "DifyTextPlan"
    profile: Literal["dify-0.6.0-linear-text-v1"] = "dify-0.6.0-linear-text-v1"
    source_digest: Digest
    source_model: Annotated[str, Field(min_length=1, max_length=200)]
    source_provider: Annotated[str, Field(min_length=1, max_length=200)]
    source_plugin: Annotated[str, Field(min_length=1, max_length=250)]
    llm_node: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
    prompts: Annotated[list[HostedPrompt], Field(min_length=1, max_length=16)]
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    max_tokens: Annotated[int, Field(ge=1, le=4096)] | None = None
    # The supported service accepts independent text turns. Conversation memory,
    # files, tools and platform-specific plugin code are not executed by this profile.
    conversation_mode: Literal["independent_turns"] = "independent_turns"


class HostedImage(WireModel):
    """A preloaded reviewed image, never a mutable tag or build instructions."""

    api_version: Literal[API] = API
    kind: Literal["HostedImage"] = "HostedImage"
    profile: Literal["dify-0.6.0-linear-text-v1", "langgraph-stdio-v1"]
    image_id: Digest
    source_digest: Digest
    dependency_lock_digest: Digest
    protocol: Literal["agent-hosted-stdio/v1"] = "agent-hosted-stdio/v1"
    # A host-provisioned alias, resolved outside the container. No endpoint or key.
    model_resource: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,499}$")]
    model_name: Annotated[str, Field(min_length=1, max_length=200)]
    max_output_tokens: Annotated[int, Field(ge=1, le=4096)] = 128


class RegisterHostedArtifact(WireModel):
    api_version: Literal[API] = API
    kind: Literal["RegisterHostedArtifact"] = "RegisterHostedArtifact"
    request_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")]
    asset_id: Identifier
    metadata_digest: Digest
    image: HostedImage
    dify_source: Annotated[str, Field(min_length=1, max_length=256000)] | None = None
    initiated_by: Identifier

    @model_validator(mode="after")
    def source_profile(self):
        if (self.image.profile == "dify-0.6.0-linear-text-v1") != (self.dify_source is not None):
            raise ValueError("HOSTED_SOURCE_PROFILE_MISMATCH")
        return self


class HostedArtifact(WireModel):
    api_version: Literal[API] = API
    kind: Literal["HostedArtifact"] = "HostedArtifact"
    id: Identifier
    zone: Identifier
    asset_id: Identifier
    metadata_digest: Digest
    spec_digest: Digest
    # Digest binds these exact bytes; consumers must not reserialize floats.
    spec_payload_base64: Annotated[str, Field(min_length=1, max_length=750000)]
    image: HostedImage
    dify_plan: DifyTextPlan | None
    created_by: Label
    initiated_by: Identifier
    created_at: Stamp
    # Registration is not execution evidence, publication, or a grade assertion.
    evidence: Literal["registered_only"] = "registered_only"


class HostedArtifactPage(WireModel):
    kind: Literal["HostedArtifactPage"] = "HostedArtifactPage"
    items: list[HostedArtifact]
    next_cursor: Identifier | None
