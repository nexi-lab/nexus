import json

import pytest
from pydantic import ValidationError

from nexus.bricks.sandbox.hosted_agent import HostedDockerRunner, HostedRuntimeError
from nexus.bricks.sandbox.hosted_supervisor import CleanupInput, SupervisorInput
from nexus.contracts.agent_native import NativeImage, NativeRuntime


def native_image():
    return NativeImage(
        image_id="sha256:" + "a" * 64,
        source_digest="sha256:" + "b" * 64,
        dependency_lock_digest="sha256:" + "c" * 64,
        definition_digest="sha256:" + "d" * 64,
        model_resource="model:lab",
        model_name="model",
        runtime=NativeRuntime(
            commit="a" * 40,
            executable_digest="sha256:" + "e" * 64,
            runner_digest="sha256:" + "f" * 64,
            platform="linux/amd64",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        None,
        "native-definition",
        "native-runtime",
        "native-runner",
        "native-commit",
        "native-platform",
    ],
)
async def test_fixed_image_must_match_all_native_material_labels(monkeypatch, changed):
    image = native_image()
    labels = {
        "protocol": image.protocol,
        "profile": image.profile,
        "source": image.source_digest,
        "dependencies": image.dependency_lock_digest,
        "native-definition": image.definition_digest,
        "native-runtime": image.runtime.executable_digest,
        "native-runner": image.runtime.runner_digest,
        "native-commit": image.runtime.commit,
        "native-platform": image.runtime.platform,
    }
    if changed:
        labels[changed] = "other"
    runner = HostedDockerRunner()

    async def command(*args):
        assert image.image_id in args
        return json.dumps(
            {"Labels": {"dev.sudo.agent." + k: v for k, v in labels.items()}}
        ).encode()

    monkeypatch.setattr(runner, "_command", command)
    if changed:
        with pytest.raises(HostedRuntimeError, match="HOSTED_IMAGE_MISMATCH"):
            await runner.inspect(image)
    else:
        await runner.inspect(image)


def test_native_uses_same_strict_runtime_and_no_task_selected_command():
    image = native_image()
    command = HostedDockerRunner("/operator/docker").command(image, "controlled-name")
    assert command[-1] == image.image_id
    for pair in [
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--user", "65532:65532"),
        ("--pull", "never"),
    ]:
        index = command.index(pair[0])
        assert command[index + 1] == pair[1]
    assert "--read-only" in command
    assert not any("key" in value.lower() for value in command)


def test_supervisor_accepts_native_material_without_increasing_call_budget():
    image = native_image()
    request = {
        "artifact": image.model_dump(),
        "query": "6*7",
        "timeout_seconds": 60,
        "max_model_calls": 1,
        "max_output_tokens": 128,
    }
    assert isinstance(SupervisorInput.model_validate(request).artifact, NativeImage)
    with pytest.raises(ValidationError):
        SupervisorInput.model_validate({**request, "max_model_calls": 2})
    cleanup = CleanupInput.model_validate(
        {
            "artifact": image.model_dump(),
            "execution_id": "a" * 32,
            "runtime_authority_id": "sha256:" + "b" * 64,
        }
    )
    assert isinstance(cleanup.artifact, NativeImage)
