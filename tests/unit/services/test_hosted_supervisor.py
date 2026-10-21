"""Supervisor pipe contract checks; Docker containment is exercised separately."""

import asyncio
import json

import pytest

from nexus.bricks.sandbox.hosted_agent import HostedRuntimeError
from nexus.bricks.sandbox.hosted_supervisor import PROTOCOL, decode, supervise


def request(**changes):
    return {
        "protocol": PROTOCOL,
        "kind": "execute",
        "artifact": {
            "profile": "langgraph-stdio-v1",
            "image_id": "sha256:" + "a" * 64,
            "source_digest": "sha256:" + "b" * 64,
            "dependency_lock_digest": "sha256:" + "c" * 64,
            "model_resource": "evaluation-model",
            "model_name": "example-model",
        },
        "query": "6 * 7",
        "timeout_seconds": 60,
        "max_model_calls": 1,
        "max_output_tokens": 32,
        **changes,
    }


def reader(*frames):
    stream = asyncio.StreamReader(limit=256000)
    for frame in frames:
        stream.feed_data(json.dumps(frame).encode() + b"\n")
    stream.feed_eof()
    return stream


class Runner:
    called = False

    async def run(self, artifact, query, model, trace, **limits):
        self.called = True
        assert artifact.model_resource == "evaluation-model"
        assert query == "6 * 7" and limits["max_model_calls"] == 1
        await trace({"node": "model", "phase": "started", "origin": "worker_reported"})
        answer = await model({"messages": [{"role": "user", "content": query}], "max_tokens": 32})
        return {"answer": answer, "model_calls": 1}


@pytest.mark.asyncio
async def test_parent_model_roundtrip_and_worker_trace_remain_distinct():
    frames = []

    async def emit(frame):
        frames.append(frame)

    await supervise(
        reader(
            request(),
            {"protocol": PROTOCOL, "kind": "model_response", "sequence": 1, "answer": "42"},
        ),
        emit,
        Runner(),
    )
    assert frames[0]["origin"] == "worker_reported"
    assert frames[1]["kind"] == "model_request" and frames[1]["sequence"] == 1
    assert frames[2] == {"kind": "completed", "answer": "42", "model_calls": 1}


@pytest.mark.parametrize(
    "change",
    [{"docker": "/tmp/evil"}, {"timeout_seconds": 999}, {"max_model_calls": True}, {"query": ""}],
)
@pytest.mark.asyncio
async def test_invalid_input_never_starts_worker(change):
    runner = Runner()
    with pytest.raises(HostedRuntimeError):
        await supervise(reader(request(**change)), None, runner)
    assert not runner.called


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {"sequence": 2, "answer": "42"},
        {"sequence": True, "answer": "42"},
        {"sequence": 1, "answer": "42", "token": "forged"},
    ],
)
@pytest.mark.asyncio
async def test_eof_wrong_sequence_and_credential_fields_fail_closed(reply):
    frames = []

    async def emit(frame):
        frames.append(frame)

    replies = [] if reply is None else [{"protocol": PROTOCOL, "kind": "model_response", **reply}]
    with pytest.raises(HostedRuntimeError):
        await supervise(reader(request(), *replies), emit, Runner())
    assert all(frame["kind"] != "completed" for frame in frames)


def test_duplicate_keys_nonfinite_and_oversized_frames_are_rejected():
    for raw in [
        b'{"protocol":"agent-hosted-supervisor/v1","query":1,"query":2}\n',
        b'{"protocol":"agent-hosted-supervisor/v1","query":NaN}\n',
        b"x" * 256001 + b"\n",
    ]:
        with pytest.raises(HostedRuntimeError):
            decode(raw)
