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


@pytest.mark.parametrize(
    "response", [b"true false true", b"true true false", b"false true true", b"true true", b"1 1 1"]
)
@pytest.mark.asyncio
async def test_missing_kernel_limits_refuse_before_creating_worker(response):
    runner = HostedDockerRunner()
    commands = []

    async def inspect(_artifact):
        pass

    async def command(*args):
        commands.append(args)
        assert args[0] == "info"
        return response

    runner.inspect = inspect
    runner._command = command
    with pytest.raises(HostedRuntimeError, match="HOSTED_RESOURCE_LIMITS_UNAVAILABLE"):
        await runner.run(artifact(), "6 * 7", None, None, execution_id="e" * 32)
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_kernel_limit_support_is_checked_anew():
    runner = HostedDockerRunner()
    replies = iter([b"true true true\n", b"true false true\n"])

    async def command(*_args):
        return next(replies)

    runner._command = command
    await runner.require_resource_limits()
    with pytest.raises(HostedRuntimeError, match="HOSTED_RESOURCE_LIMITS_UNAVAILABLE"):
        await runner.require_resource_limits()


@pytest.mark.asyncio
async def test_unjoinable_cli_does_not_skip_container_cleanup(monkeypatch):
    from nexus.bricks.sandbox import hosted_agent

    actions = []

    class Input:
        def close(self):
            actions.append("stdin_closed")

    class Process:
        stdin = Input()
        returncode = None

        def kill(self):
            actions.append("kill")

        async def wait(self):
            raise TimeoutError

    class Runner(HostedDockerRunner):
        async def inspect(self, _artifact):
            pass

        async def require_resource_limits(self):
            pass

        async def _command(self, *args):
            assert args[0] == "create"
            return b"a" * 64

        async def reconcile(self, _artifact, execution_id):
            assert execution_id == "e" * 32
            actions.append("cleanup")

    async def spawn(*args, **kwargs):
        return Process()

    async def exchange(*args, **kwargs):
        raise HostedRuntimeError("HOSTED_MODEL_REFUSED")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(hosted_agent, "exchange", exchange)
    with pytest.raises(HostedRuntimeError, match="HOSTED_CLEANUP_UNCONFIRMED"):
        await Runner().run(artifact(), "6 * 7", None, None, execution_id="e" * 32)
    assert actions == ["stdin_closed", "kill", "cleanup"]


class DurableDocker(HostedDockerRunner):
    """Controlled daemon races; real daemon recovery is tested in the isolated lab."""

    def __init__(self):
        super().__init__()
        self.container = None
        self.removed = []
        self.late_create = False
        self.unavailable = False

    async def inspect(self, artifact):
        pass

    def record(self, execution_id, *, closed=False, image=None):
        return {
            "Id": ("c" if closed else "b") * 64,
            "Name": "/" + self.execution_name(execution_id),
            "Image": image or artifact().image_id,
            "State": {"Running": not closed, "Status": "created" if closed else "running"},
            "Config": {
                "Labels": {
                    "dev.sudo.agent.worker": "hosted-v1",
                    "dev.sudo.agent.execution": execution_id,
                    "dev.sudo.agent.closed": "true" if closed else "false",
                }
            },
        }

    async def _command(self, *args):
        if self.unavailable:
            raise HostedRuntimeError("HOSTED_RUNTIME_UNAVAILABLE")
        if args[0] == "ps":
            return self.container["Id"].encode() if self.container else b""
        if args[:2] == ("container", "inspect"):
            return json.dumps(self.container).encode()
        if args[0] == "rm":
            assert args[-1] == self.container["Id"]
            self.removed.append(args[-1])
            self.container = None
            return b""
        if args[0] == "create":
            execution_id = args[args.index("--name") + 1].removeprefix("sudo-hosted-task-")
            if self.late_create:
                self.late_create = False
                self.container = self.record(execution_id)
                raise HostedRuntimeError("HOSTED_RUNTIME_UNAVAILABLE")
            assert "--rm" not in args and args[args.index("--entrypoint") + 1] == "/bin/false"
            self.container = self.record(execution_id, closed=True)
            return self.container["Id"].encode()
        raise AssertionError(args)


@pytest.mark.asyncio
async def test_durable_cleanup_closes_survivor_and_fences_delayed_create():
    execution_id = "e" * 32
    for present, late in [(False, False), (True, False), (False, True)]:
        runner = DurableDocker()
        if present:
            runner.container = runner.record(execution_id)
        runner.late_create = late
        await runner.reconcile(artifact(), execution_id)
        assert runner.container["State"]["Status"] == "created"
        assert runner.container["Config"]["Labels"]["dev.sudo.agent.closed"] == "true"
        assert len(runner.removed) == int(present or late)
        # A second cleanup neither removes nor recreates the closed marker.
        marker = runner.container
        await runner.reconcile(artifact(), execution_id)
        assert runner.container is marker


@pytest.mark.asyncio
async def test_durable_cleanup_refuses_foreign_identity_and_daemon_uncertainty():
    runner = DurableDocker()
    execution_id = "e" * 32
    runner.container = runner.record(execution_id, image="sha256:" + "d" * 64)
    with pytest.raises(HostedRuntimeError):
        await runner.reconcile(artifact(), execution_id)
    assert runner.removed == []
    runner.unavailable = True
    with pytest.raises(HostedRuntimeError):
        await runner.reconcile(artifact(), execution_id)
    with pytest.raises(HostedRuntimeError):
        runner.create_command(artifact(), "../../foreign")


def test_durable_create_retains_strict_isolation_and_has_no_auto_remove():
    command = HostedDockerRunner().create_command(artifact(), "e" * 32)
    assert command[1] == "create" and "--rm" not in command
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--pull" in command
    assert command[-1] == artifact().image_id
