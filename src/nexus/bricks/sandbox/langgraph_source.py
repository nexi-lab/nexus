"""Reviewed single-file original LangGraph runtime for synthetic migration baselines.

This executes the original source in a separate deployment-provisioned Python process,
not the imported container. It is NOT a sandbox or an L2 runtime. Never configure this
entry point with an unreviewed user upload or a production-data workload.
"""

import argparse
import hashlib
import importlib.metadata
import json
import sys
import types
from pathlib import Path

PROTOCOL = "agent-source-stdio/v1"
LIMIT = 256000


def snapshot(project):
    if not project.is_absolute() or not project.is_file():
        raise ValueError("EVALUATION_SOURCE_RUNTIME_INVALID")
    source = project.read_bytes()
    if not source or len(source) > LIMIT:
        raise ValueError("EVALUATION_SOURCE_RUNTIME_INVALID")
    versions = sorted(
        (dist.metadata["Name"].lower(), dist.version)
        for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    )
    if importlib.metadata.version("langgraph") != "1.0.5":
        raise ValueError("EVALUATION_SOURCE_RUNTIME_INVALID")
    return source, {
        "source_digest": "sha256:" + hashlib.sha256(source).hexdigest(),
        "python": sys.version,
        "distributions": versions,
        "entry_digest": "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def read():
    raw = sys.stdin.buffer.readline(LIMIT + 1)
    if not raw.endswith(b"\n") or len(raw) > LIMIT:
        raise ValueError("EVALUATION_SOURCE_EXECUTION_FAILED")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ValueError("EVALUATION_SOURCE_EXECUTION_FAILED")
    return value


def send(frame):
    raw = json.dumps({"protocol": PROTOCOL, **frame}, ensure_ascii=False).encode() + b"\n"
    if len(raw) > LIMIT:
        raise ValueError("EVALUATION_SOURCE_EXECUTION_FAILED")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--inspect", action="store_true")
    args = parser.parse_args()
    source, observed = snapshot(args.project)
    if args.inspect:
        print(json.dumps(observed, separators=(",", ":")))
        return
    request = read()
    # Compare JSON forms so the wire lists and in-process tuples have identical meaning.
    if json.loads(json.dumps(observed)) != request.get("expected_snapshot"):
        raise ValueError("EVALUATION_SOURCE_CHANGED")
    if request.get("kind") != "execute" or not isinstance(request.get("query"), str):
        raise ValueError("EVALUATION_SOURCE_EXECUTION_FAILED")
    limit = request.get("max_model_calls")
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("HOSTED_CALL_LIMIT")
    sequence = 0

    def model(messages, **options):
        nonlocal sequence
        sequence += 1
        if sequence > limit or set(options) - {"max_tokens", "temperature"}:
            raise ValueError("HOSTED_CALL_LIMIT")
        send({"kind": "model_request", "sequence": sequence, "messages": messages, **options})
        reply = read()
        if (
            set(reply) != {"protocol", "kind", "sequence", "answer"}
            or reply["kind"] != "model_response"
            or reply["sequence"] != sequence
            or not isinstance(reply["answer"], str)
        ):
            raise ValueError("EVALUATION_SOURCE_EXECUTION_FAILED")
        return reply["answer"]

    def trace(node, phase):
        send({"kind": "trace", "node": node, "phase": phase, "origin": "worker_reported"})

    # Compile the inspected bytes, avoiding a second mutable file read before execution.
    module = types.ModuleType("reviewed_original_project")
    module.__file__ = str(args.project)
    sys.modules[module.__name__] = module
    exec(compile(source, str(args.project), "exec"), module.__dict__)
    graph = module.build_graph(model, trace)
    result = graph.invoke({"query": request["query"], "answer": ""})
    if snapshot(args.project)[1] != observed:
        raise ValueError("EVALUATION_SOURCE_CHANGED")
    answer = result.get("answer")
    if not isinstance(answer, str) or len(answer.encode()) > 128000:
        raise ValueError("EVALUATION_SOURCE_EXECUTION_FAILED")
    send({"kind": "completed", "answer": answer, "model_calls": sequence})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) else "EVALUATION_SOURCE_EXECUTION_FAILED"
        if code not in {
            "EVALUATION_SOURCE_CHANGED",
            "EVALUATION_SOURCE_RUNTIME_INVALID",
            "EVALUATION_SOURCE_EXECUTION_FAILED",
            "HOSTED_CALL_LIMIT",
        }:
            code = "EVALUATION_SOURCE_EXECUTION_FAILED"
        send({"kind": "failed", "code": code})
        sys.exit(1)
