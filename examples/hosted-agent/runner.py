"""Text-only worker. All effects leave through the host-owned stdio capability seam."""

import json
import sys
from pathlib import Path

from langgraph.graph import END, START, StateGraph

PROTOCOL = "agent-hosted-stdio/v1"
LIMIT = 256000
sequence = 0


def emit(value):
    raw = json.dumps({"protocol": PROTOCOL, **value}, ensure_ascii=False).encode() + b"\n"
    if len(raw) > LIMIT:
        raise ValueError("FRAME_LIMIT")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def read():
    raw = sys.stdin.buffer.readline(LIMIT + 1)
    if not raw.endswith(b"\n") or len(raw) > LIMIT:
        raise ValueError("FRAME_LIMIT")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ValueError("PROTOCOL")
    return value


def model(messages, max_tokens=128, temperature=None):
    global sequence
    sequence += 1
    request = {
        "kind": "model_request",
        "sequence": sequence,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        request["temperature"] = temperature
    emit(request)
    reply = read()
    if (
        reply.get("sequence") != sequence
        or reply.get("kind") != "model_response"
        or not isinstance(reply.get("answer"), str)
    ):
        raise ValueError("MODEL_RESPONSE")
    return reply["answer"]


def trace(node, phase):
    emit({"kind": "trace", "node": node, "phase": phase})


def dify(plan):
    def generate(state):
        trace(plan["llm_node"], "started")
        messages = [
            {"role": p["role"], "content": p["content"].replace("{{#sys.query#}}", state["query"])}
            for p in plan["prompts"]
        ]
        messages.append({"role": "user", "content": state["query"]})
        output = model(messages, plan["max_tokens"] or 128, plan["temperature"])
        trace(plan["llm_node"], "completed")
        return {**state, "answer": output}

    graph = StateGraph(dict)
    graph.add_node("generate", generate)
    graph.add_edge(START, "generate")
    graph.add_edge("generate", END)
    return graph.compile()


def main():
    config = json.loads(Path("/app/profile.json").read_text())
    request = read()
    if set(request) != {"protocol", "profile", "query"} or request["profile"] != config["profile"]:
        raise ValueError("INPUT")
    if not isinstance(request["query"], str) or not 1 <= len(request["query"]) <= 16000:
        raise ValueError("INPUT")
    if config["profile"] == "dify-0.6.0-linear-text-v1":
        graph = dify(config["plan"])
    elif config["profile"] == "langgraph-stdio-v1":
        from project import build_graph

        graph = build_graph(model, trace)
    else:
        raise ValueError("PROFILE")
    result = graph.invoke({"query": request["query"], "answer": ""}, {"recursion_limit": 32})
    emit({"kind": "completed", "answer": result["answer"], "model_calls": sequence})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit({"kind": "failed"})
        sys.exit(1)
