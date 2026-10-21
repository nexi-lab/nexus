"""Strict single-run container adapter over the existing sandbox security profile.

The caller owns durable task admission and IAM checks at each model/tool dispatch.
This adapter never receives model credentials and never retries a process. Images
must already exist on this worker and match a reviewed content-addressed descriptor.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from uuid import uuid4

from nexus.bricks.sandbox.security_profile import SandboxSecurityProfile
from nexus.contracts.agent_hosted import HostedImage

PROTOCOL = "agent-hosted-stdio/v1"
FRAME_LIMIT = 256000


class HostedRuntimeError(ValueError):
    """Stable code only; stderr, prompts and keys never enter public errors."""


def decode_frame(data: bytes) -> dict:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError
            out[key] = value
        return out

    if not data.endswith(b"\n") or len(data) > FRAME_LIMIT:
        raise HostedRuntimeError("HOSTED_FRAME_LIMIT")
    try:

        def invalid_constant(_):
            raise ValueError

        value = json.loads(data, object_pairs_hook=unique, parse_constant=invalid_constant)
        if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise HostedRuntimeError("HOSTED_FRAME_INVALID") from None


def validate_model_request(frame: dict, sequence: int, max_output_tokens: int) -> dict:
    if (
        set(frame) - {"protocol", "kind", "sequence", "messages", "temperature", "max_tokens"}
        or type(frame.get("sequence")) is not int
        or frame["sequence"] != sequence
        or type(frame.get("max_tokens")) is not int
        or not 1 <= frame["max_tokens"] <= max_output_tokens
    ):
        raise HostedRuntimeError("HOSTED_MODEL_REQUEST_INVALID")
    messages = frame.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise HostedRuntimeError("HOSTED_MODEL_REQUEST_INVALID")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in ("system", "user", "assistant")
            or not isinstance(message["content"], str)
            or len(message["content"]) > 32000
        ):
            raise HostedRuntimeError("HOSTED_MODEL_REQUEST_INVALID")
    result = {"messages": messages, "max_tokens": frame["max_tokens"]}
    if "temperature" in frame:
        value = frame["temperature"]
        if type(value) not in (int, float) or not 0 <= value <= 2:
            raise HostedRuntimeError("HOSTED_MODEL_REQUEST_INVALID")
        result["temperature"] = value
    return result


async def exchange(
    process,
    request: dict,
    invoke_model: Callable[[dict], Awaitable[str]],
    on_trace: Callable[[dict], Awaitable[None]],
    *,
    max_model_calls=8,
    max_output_tokens=4096,
):
    """Bounded stdio exchange. A complete frame plus clean process exit is required."""
    data = json.dumps({"protocol": PROTOCOL, **request}, ensure_ascii=False).encode() + b"\n"
    if len(data) > FRAME_LIMIT:
        raise HostedRuntimeError("HOSTED_INPUT_LIMIT")
    process.stdin.write(data)
    await process.stdin.drain()
    sequence, count, final = 0, 0, None
    while True:
        try:
            data = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError):
            raise HostedRuntimeError("HOSTED_FRAME_LIMIT") from None
        if not data:
            break
        frame = decode_frame(data)
        count += 1
        if count > 256 or final is not None:
            raise HostedRuntimeError("HOSTED_FRAME_ORDER")
        kind = frame.get("kind")
        if kind == "model_request":
            sequence += 1
            if sequence > max_model_calls:
                raise HostedRuntimeError("HOSTED_CALL_LIMIT")
            body = validate_model_request(frame, sequence, max_output_tokens)
            answer = await invoke_model(body)
            if not isinstance(answer, str) or len(answer.encode()) > 128000:
                raise HostedRuntimeError("HOSTED_MODEL_REPLY_INVALID")
            process.stdin.write(
                json.dumps(
                    {
                        "protocol": PROTOCOL,
                        "kind": "model_response",
                        "sequence": sequence,
                        "answer": answer,
                    },
                    ensure_ascii=False,
                ).encode()
                + b"\n"
            )
            await process.stdin.drain()
        elif kind == "trace":
            if (
                set(frame) != {"protocol", "kind", "node", "phase"}
                or not isinstance(frame["node"], str)
                or not 1 <= len(frame["node"]) <= 100
                or frame["phase"] not in ("started", "completed")
            ):
                raise HostedRuntimeError("HOSTED_TRACE_INVALID")
            await on_trace(
                {"node": frame["node"], "phase": frame["phase"], "origin": "worker_reported"}
            )
        elif kind == "completed":
            if (
                set(frame) != {"protocol", "kind", "answer", "model_calls"}
                or not isinstance(frame["answer"], str)
                or len(frame["answer"].encode()) > 128000
                or type(frame["model_calls"]) is not int
                or frame["model_calls"] != sequence
            ):
                raise HostedRuntimeError("HOSTED_RESULT_INVALID")
            final = frame
            process.stdin.close()
        else:
            raise HostedRuntimeError(
                "HOSTED_WORKER_FAILED" if kind == "failed" else "HOSTED_FRAME_KIND"
            )
    if await process.wait() != 0 or final is None:
        raise HostedRuntimeError("HOSTED_RESULT_UNCONFIRMED")
    return {"answer": final["answer"], "model_calls": sequence}


class HostedDockerRunner:
    def __init__(self, docker: str = "/usr/bin/docker"):
        # Deployment selects a local binary; task data never selects a command/context.
        self.docker = docker

    async def _command(self, *args):
        proc = await asyncio.create_subprocess_exec(
            self.docker,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=FRAME_LIMIT,
        )
        try:
            async with asyncio.timeout(10):
                result = await proc.stdout.read(FRAME_LIMIT + 1)
                if len(result) > FRAME_LIMIT or await proc.wait() != 0:
                    raise HostedRuntimeError("HOSTED_RUNTIME_UNAVAILABLE")
                return result
        except TimeoutError:
            raise HostedRuntimeError("HOSTED_RUNTIME_UNAVAILABLE") from None
        finally:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def inspect(self, artifact: HostedImage):
        output = await self._command(
            "image", "inspect", artifact.image_id, "--format", "{{json .Config}}"
        )
        try:
            config = json.loads(output)
            labels = config.get("Labels") or {}
            if (
                config.get("Volumes")
                or labels.get("dev.sudo.agent.protocol") != PROTOCOL
                or labels.get("dev.sudo.agent.source") != artifact.source_digest
                or labels.get("dev.sudo.agent.dependencies") != artifact.dependency_lock_digest
                or labels.get("dev.sudo.agent.profile") != artifact.profile
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise HostedRuntimeError("HOSTED_IMAGE_MISMATCH") from None

    def command(self, artifact: HostedImage, name: str) -> list[str]:
        profile = SandboxSecurityProfile.strict()
        return [
            self.docker,
            "run",
            "--rm",
            "-i",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            "dev.sudo.agent.worker=hosted-v1",
            "--network",
            profile.network_mode,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "65532:65532",
            "--memory",
            profile.memory_limit,
            "--memory-swap",
            profile.memory_limit,
            "--cpus",
            str(profile.cpu_limit),
            "--pids-limit",
            str(profile.pids_limit),
            "--tmpfs",
            "/tmp:size=10m,noexec,nosuid,nodev",
            "--no-healthcheck",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONUNBUFFERED=1",
            artifact.image_id,
        ]

    async def run(
        self,
        artifact: HostedImage,
        query: str,
        invoke_model,
        on_trace,
        *,
        timeout_seconds=120,
        max_model_calls=8,
        max_output_tokens=4096,
    ):
        if not isinstance(query, str) or not 1 <= len(query) <= 16000:
            raise HostedRuntimeError("HOSTED_INPUT_INVALID")
        if (
            type(timeout_seconds) is not int
            or type(max_model_calls) is not int
            or type(max_output_tokens) is not int
            or not 1 <= timeout_seconds <= 300
            or not 1 <= max_model_calls <= 32
            or not 1 <= max_output_tokens <= 4096
        ):
            raise HostedRuntimeError("HOSTED_LIMIT_INVALID")
        await self.inspect(artifact)
        name = "sudo-hosted-" + uuid4().hex
        proc = await asyncio.create_subprocess_exec(
            *self.command(artifact, name),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=FRAME_LIMIT,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                return await exchange(
                    proc,
                    {"profile": artifact.profile, "query": query},
                    invoke_model,
                    on_trace,
                    max_model_calls=max_model_calls,
                    max_output_tokens=min(max_output_tokens, artifact.max_output_tokens),
                )
        except TimeoutError:
            raise HostedRuntimeError("HOSTED_RESULT_UNCONFIRMED") from None
        finally:
            # Killing only the Docker CLI does not reliably stop the container.
            # Always remove this exact generated name; never sweep other workers.
            cleanup_failed = False
            try:
                await self._command("rm", "-f", name)
            except HostedRuntimeError:
                # --rm may already have removed it. Verify absence explicitly.
                try:
                    remaining = await self._command("ps", "-aq", "--filter", f"name=^/{name}$")
                    cleanup_failed = bool(remaining.strip())
                except HostedRuntimeError:
                    cleanup_failed = True
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
            if cleanup_failed:
                raise HostedRuntimeError("HOSTED_CLEANUP_UNCONFIRMED")
