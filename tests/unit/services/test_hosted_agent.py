"""Real local pipe workers test protocol bounds, not Docker/L2 qualification."""

import asyncio
import json
import sys
from contextlib import suppress

import pytest

from nexus.bricks.sandbox.hosted_agent import (
    PROTOCOL,
    HostedDockerRunner,
    HostedRuntimeError,
    decode_frame,
    exchange,
)
from nexus.contracts.agent_hosted import HostedImage


def artifact():
    return HostedImage(
        profile="langgraph-stdio-v1",
        image_id="sha256:" + "a" * 64,
        source_digest="sha256:" + "b" * 64,
        dependency_lock_digest="sha256:" + "c" * 64,
        model_resource="evaluation-model",
        model_name="example-model",
    )


def model_frame(**overrides):
    return {
        "protocol": PROTOCOL,
        "kind": "model_request",
        "sequence": 1,
        "messages": [{"role": "user", "content": "6 * 7"}],
        "max_tokens": 10,
        **overrides,
    }


def final(**overrides):
    return {
        "protocol": PROTOCOL,
        "kind": "completed",
        "answer": "42",
        "model_calls": 1,
        **overrides,
    }


async def worker(frames, exit_code=0):
    # A genuine subprocess. The frame script cannot receive any host credentials.
    program = "import json,sys\njson.loads(sys.stdin.readline())\n"
    for frame in frames:
        program += f"print({json.dumps(json.dumps(frame))},flush=True)\n"
        if frame.get("kind") == "model_request":
            program += "sys.stdin.readline()\n"
    program += f"sys.exit({exit_code})\n"
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        program,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=256000,
    )


async def exercise(frames, calls, traces=None, *, limit=2, exit_code=0):
    proc = await worker(frames, exit_code)

    async def model(body):
        calls.append(body)
        return "42"

    async def trace(frame):
        if traces is not None:
            traces.append(frame)

    try:
        async with asyncio.timeout(5):
            return await exchange(proc, {"query": "6 * 7"}, model, trace, max_model_calls=limit)
    finally:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        await proc.wait()


@pytest.mark.asyncio
async def test_real_worker_exchange_separates_model_and_worker_trace():
    calls, traces = [], []
    result = await exercise(
        [
            {"protocol": PROTOCOL, "kind": "trace", "node": "generate", "phase": "started"},
            model_frame(),
            final(),
        ],
        calls,
        traces,
    )
    assert result == {"answer": "42", "model_calls": 1}
    assert len(calls) == 1 and calls[0]["messages"][0]["content"] == "6 * 7"
    assert traces[0]["origin"] == "worker_reported"


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "https://attacker.invalid"},
        {"token": "fake"},
        {"model": "other"},
        {"sequence": 2},
        {"sequence": True},
        {"max_tokens": 99999},
        {"temperature": True},
        {"messages": [{"role": "user", "content": "q", "tool_calls": []}]},
    ],
)
@pytest.mark.asyncio
async def test_untrusted_worker_cannot_select_destination_identity_or_exceed_budget(change):
    calls = []
    with pytest.raises(HostedRuntimeError):
        await exercise([model_frame(**change), final()], calls)
    assert calls == []


@pytest.mark.asyncio
async def test_second_call_is_denied_before_callback_when_cap_exhausted():
    calls = []
    with pytest.raises(HostedRuntimeError, match="HOSTED_CALL_LIMIT"):
        await exercise(
            [model_frame(), model_frame(sequence=2), final(model_calls=2)], calls, limit=1
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "frames,exit_code",
    [
        ([model_frame()], 0),
        ([model_frame(), final()], 1),
        ([model_frame(), final(), final()], 0),
        ([model_frame(), final(model_calls=2)], 0),
        ([{"protocol": PROTOCOL, "kind": "tool_request", "name": "delete"}], 0),
        ([{"protocol": PROTOCOL, "kind": "failed", "error": "sensitive detail"}], 1),
    ],
)
@pytest.mark.asyncio
async def test_success_requires_clean_complete_protocol(frames, exit_code):
    with pytest.raises(HostedRuntimeError) as err:
        await exercise(frames, [], exit_code=exit_code)
    assert "sensitive" not in str(err.value)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}\n",
        b'{"protocol":"agent-hosted-stdio/v1","kind":"a","kind":"b"}\n',
        b"x" * 256001,
        b"\xff\n",
    ],
)
def test_frames_reject_ambiguous_or_invalid_encoding(raw):
    with pytest.raises(HostedRuntimeError):
        decode_frame(raw)


def test_container_command_uses_existing_strict_profile_without_host_mounts():
    cmd = HostedDockerRunner().command(artifact(), "sudo-hosted-test")

    def option(key):
        return cmd[cmd.index(key) + 1]

    assert option("--network") == "none"
    assert option("--user") == "65532:65532"
    assert option("--cap-drop") == "ALL"
    assert option("--security-opt") == "no-new-privileges:true"
    assert option("--pull") == "never"
    assert option("--memory-swap") == option("--memory")
    assert "--read-only" in cmd
    assert not {"--volume", "--mount", "--privileged", "--publish", "--device"}.intersection(cmd)


@pytest.mark.asyncio
async def test_unreviewed_image_never_runs():
    runner = HostedDockerRunner()

    async def inspect(*args):
        return json.dumps({"Labels": {"dev.sudo.agent.protocol": PROTOCOL}}).encode()

    runner._command = inspect
    with pytest.raises(HostedRuntimeError, match="HOSTED_IMAGE_MISMATCH"):
        await runner.inspect(artifact())
