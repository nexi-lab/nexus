import pytest
from pydantic import ValidationError

from nexus.contracts.agent_native import NativeExecutionProfile
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.native_execution import verify_output
from nexus.services.product_agent_registry.store import digest


def profile():
    return NativeExecutionProfile(
        conversion_id="native-candidate",
        spec_digest="sha256:" + "a" * 64,
        definition_digest="sha256:" + "b" * 64,
        image_id="sha256:" + "c" * 64,
        runtime={
            "kind": "sudocode",
            "commit": "d" * 40,
            "executable_digest": "sha256:" + "e" * 64,
            "runner_digest": "sha256:" + "f" * 64,
            "platform": "linux/amd64",
        },
    )


def verify(answer="42", **changes):
    options = {
        "model_call_ids": ["call-a"],
        "trace": [
            {"node": "native-sudocode", "phase": "started"},
            {"node": "native-sudocode", "phase": "completed"},
        ],
        "execution_succeeded": True,
        "cleanup_confirmed": True,
        **changes,
    }
    return verify_output(profile(), answer, **options)


def test_native_verification_binds_runtime_and_rules_without_claiming_business_correctness():
    result = verify()
    assert result.verdict == "passed" and result.failures == []
    assert result.output_digest == digest(b"42")
    assert result.profile_digest == digest(canonical_bytes(profile().model_dump()))
    # Shared with Moss nativeExecution.node-test.ts: canonical Python/TS profile bytes.
    assert (
        result.profile_digest
        == "sha256:dc34ebe2be844fae07108f35e191bf1f98cf58575f907387ba25393f22da532c"
    )
    assert verify("a possibly incorrect answer").verdict == "passed"
    assert verify("中文🙂").output_digest == digest("中文🙂".encode())
    for field, value in [
        ("tool_policy", "allow"),
        ("memory_policy", "enabled"),
        ("task_contract_version", "task.sudo.dev/v1"),
    ]:
        with pytest.raises(ValidationError):
            NativeExecutionProfile.model_validate({**profile().model_dump(), field: value})


@pytest.mark.parametrize("answer", ["", " \t\r\n\v\f", "x\x00y", "中" * 42667, "\ud800"])
def test_native_verification_rejects_unsupported_text(answer):
    assert verify(answer).failures == ["output_text"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"model_call_ids": []}, "model_call_count"),
        ({"model_call_ids": ["a", "b"]}, "model_call_count"),
        ({"trace": []}, "native_trace"),
        ({"trace": [{"node": "native-sudocode", "phase": "completed"}]}, "native_trace"),
        ({"execution_succeeded": False}, "execution_cleanup"),
        ({"cleanup_confirmed": False}, "execution_cleanup"),
    ],
)
def test_native_verification_rejects_incomplete_execution(changes, reason):
    assert verify(**changes).failures == [reason]
