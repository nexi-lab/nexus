"""Trusted stdio host for HostedDockerRunner; never loads Agent Python into the host.

The parent coordinates durable tasks and IAM. Only model inputs/results traverse
this pipe; the worker never receives an IAM or Router credential. SIGTERM/EOF
cancel the run and allow the existing runner to remove its exact container.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from contextlib import suppress

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexus.bricks.sandbox.hosted_agent import FRAME_LIMIT, HostedDockerRunner, HostedRuntimeError
from nexus.contracts.agent_hosted import HostedImage

PROTOCOL = "agent-hosted-supervisor/v1"


class SupervisorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact: HostedImage
    execution_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    runtime_authority_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    query: str = Field(min_length=1, max_length=16000)
    timeout_seconds: int = Field(ge=1, le=240)
    max_model_calls: int = Field(ge=1, le=32)
    max_output_tokens: int = Field(ge=1, le=4096)


class CleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact: HostedImage
    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    runtime_authority_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def decode(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid(_):
        raise ValueError

    try:
        if not data.endswith(b"\n") or len(data) > FRAME_LIMIT:
            raise ValueError
        value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid)
        if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
            raise ValueError
        return value
    except (ValueError, RecursionError):
        raise HostedRuntimeError("HOSTED_PARENT_FRAME_INVALID") from None


async def supervise(reader, emit, runner):
    try:
        frame = decode(await reader.readline())
        kind = frame.pop("kind", None)
        if kind == "identity":
            if set(frame) != {"protocol"}:
                raise HostedRuntimeError("HOSTED_PARENT_FRAME_INVALID")
            await emit(
                {"kind": "runtime_identity", "runtime_authority_id": await runner.identity()}
            )
            return
        if kind == "reconcile":
            frame.pop("protocol")
            cleanup = CleanupInput.model_validate(frame)
            if await runner.identity() != cleanup.runtime_authority_id:
                raise HostedRuntimeError("HOSTED_RUNTIME_AUTHORITY_MISMATCH")
            await runner.reconcile(cleanup.artifact, cleanup.execution_id)
            if await runner.identity() != cleanup.runtime_authority_id:
                raise HostedRuntimeError("HOSTED_RUNTIME_AUTHORITY_MISMATCH")
            await emit(
                {
                    "kind": "cleanup_confirmed",
                    "execution_id": cleanup.execution_id,
                    "runtime_authority_id": cleanup.runtime_authority_id,
                }
            )
            return
        if kind != "execute":
            raise HostedRuntimeError("HOSTED_PARENT_FRAME_INVALID")
        frame.pop("protocol")
        request = SupervisorInput.model_validate(frame)
    except (ValidationError, ValueError):
        raise HostedRuntimeError("HOSTED_PARENT_FRAME_INVALID") from None
    if request.execution_id is not None and request.runtime_authority_id != await runner.identity():
        raise HostedRuntimeError("HOSTED_RUNTIME_AUTHORITY_MISMATCH")
    sequence = 0

    async def model(body):
        nonlocal sequence
        sequence += 1
        await emit({"kind": "model_request", "sequence": sequence, **body})
        reply = decode(await reader.readline())
        if (
            set(reply) != {"protocol", "kind", "sequence", "answer"}
            or reply["kind"] != "model_response"
            or type(reply["sequence"]) is not int
            or reply["sequence"] != sequence
            or not isinstance(reply["answer"], str)
            or len(reply["answer"].encode()) > 128000
        ):
            raise HostedRuntimeError("HOSTED_PARENT_FRAME_INVALID")
        return reply["answer"]

    async def trace(value):
        await emit({"kind": "trace", **value})

    result = await runner.run(
        request.artifact,
        request.query,
        model,
        trace,
        timeout_seconds=request.timeout_seconds,
        max_model_calls=request.max_model_calls,
        max_output_tokens=request.max_output_tokens,
        **({"execution_id": request.execution_id} if request.execution_id is not None else {}),
    )
    await emit({"kind": "completed", **result})


async def main(docker: str):
    reader = asyncio.StreamReader(limit=FRAME_LIMIT)
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    running = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, running.cancel)

    async def emit(frame):
        data = json.dumps({"protocol": PROTOCOL, **frame}, ensure_ascii=False).encode() + b"\n"
        if len(data) > FRAME_LIMIT:
            raise HostedRuntimeError("HOSTED_FRAME_LIMIT")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    try:
        # Input and callback waits are bounded too, including a silent parent.
        async with asyncio.timeout(250):
            await supervise(reader, emit, HostedDockerRunner(docker))
        return 0
    except (Exception, asyncio.CancelledError) as error:
        code = str(error) if isinstance(error, HostedRuntimeError) else "HOSTED_SUPERVISOR_FAILED"
        with suppress(Exception):
            await emit({"kind": "failed", "code": code})
        return 1
    finally:
        transport.close()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="/usr/bin/docker")
    args = parser.parse_args()
    if not args.docker.startswith("/"):
        parser.error("--docker must be an absolute deployment-owned executable")
    sys.exit(asyncio.run(main(args.docker)))


if __name__ == "__main__":
    cli()
