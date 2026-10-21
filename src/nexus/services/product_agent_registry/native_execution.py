"""Native profile binding and deterministic per-execution verification.

The six bindings use existing Task/Event alpha ownership and the immutable
SudoCode definition/runtime. They do not claim stable Context/Memory/Verify APIs.
An output-shape pass does not prove that the model's answer is factually correct.
"""

from nexus.contracts.agent_native import (
    NativeConversion,
    NativeExecutionProfile,
    NativeOutputVerification,
)
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.store import digest


def execution_profile(conversion: NativeConversion) -> NativeExecutionProfile:
    return NativeExecutionProfile(
        conversion_id=conversion.id,
        spec_digest=conversion.spec_digest,
        definition_digest=conversion.image.definition_digest,
        image_id=conversion.image.image_id,
        runtime=conversion.image.runtime,
    )


def verify_output(
    profile: NativeExecutionProfile,
    answer: str,
    model_call_ids: list[str],
    trace: list[dict],
    *,
    execution_succeeded: bool,
    cleanup_confirmed: bool,
) -> NativeOutputVerification:
    # Use explicit ASCII whitespace so the TypeScript consumer has identical
    # semantics. UTF-8 is strict; malformed surrogate output is never accepted.
    encoded = "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in answer
    ).encode("utf-8")
    failures = []
    if (
        not answer.strip(" \t\r\n\v\f")
        or len(encoded) > 128000
        or "\x00" in answer
        or any(0xD800 <= ord(char) <= 0xDFFF for char in answer)
    ):
        failures.append("output_text")
    if len(model_call_ids) != 1:
        failures.append("model_call_count")
    if [(row.get("node"), row.get("phase")) for row in trace] != [
        ("native-sudocode", "started"),
        ("native-sudocode", "completed"),
    ]:
        failures.append("native_trace")
    if not execution_succeeded or not cleanup_confirmed:
        failures.append("execution_cleanup")
    return NativeOutputVerification(
        profile_digest=digest(canonical_bytes(profile.model_dump())),
        output_digest=digest(encoded),
        verdict="rejected" if failures else "passed",
        failures=failures,
    )
