"""Run a compiled independent-text definition on SudoCode, through the hosted pipe.

The container has no external network and no provider credential. Its loopback
OpenAI endpoint translates the native engine's model request to the existing
host-owned capability seam. The source Dify platform is not loaded or executed.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL = "agent-hosted-stdio/v1"
PROFILE = "sudocode-dify-independent-text-v1alpha1"
LIMIT = 256000


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_KEY")
            result[key] = value
        return result

    def invalid(_):
        raise ValueError("INVALID_NUMBER")

    if not raw or len(raw) > LIMIT:
        raise ValueError("FRAME_LIMIT")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def emit(value):
    raw = json.dumps({"protocol": PROTOCOL, **value}, ensure_ascii=False).encode() + b"\n"
    if len(raw) > LIMIT:
        raise ValueError("FRAME_LIMIT")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def read():
    raw = sys.stdin.buffer.readline(LIMIT + 1)
    if not raw.endswith(b"\n"):
        raise ValueError("FRAME_LIMIT")
    value = decode(raw)
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        raise ValueError("PROTOCOL")
    return value


def translate_request(body, plan):
    """Preserve native input; refuse unsupported content or parameter drift."""
    if not isinstance(body, dict) or set(body) - {
        "model",
        "messages",
        "stream",
        "stream_options",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "tools",
    }:
        raise ValueError("NATIVE_MODEL_REQUEST")
    if (
        body.get("model") != plan["source_model"]
        or body.get("stream") is not True
        or body.get("tools") not in (None, [])
        or body.get("temperature") != plan["temperature"]
        or isinstance(body.get("temperature"), bool)
        or type(body.get("max_tokens", body.get("max_completion_tokens"))) is not int
        or body.get("max_tokens", body.get("max_completion_tokens")) != (plan["max_tokens"] or 128)
        or ("max_tokens" in body and "max_completion_tokens" in body)
        or body.get("stream_options") not in (None, {"include_usage": True})
    ):
        raise ValueError("NATIVE_PARAMETER_DRIFT")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 16:
        raise ValueError("NATIVE_MESSAGES")
    translated = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in ("system", "user", "assistant")
        ):
            raise ValueError("NATIVE_MESSAGES")
        content = message["content"]
        if isinstance(content, list):
            if not content or any(
                not isinstance(part, dict)
                or set(part) != {"type", "text"}
                or part["type"] != "text"
                or not isinstance(part["text"], str)
                for part in content
            ):
                raise ValueError("NATIVE_TEXT_ONLY")
            content = "".join(part["text"] for part in content)
        if not isinstance(content, str) or len(content.encode()) > 32000:
            raise ValueError("NATIVE_TEXT_ONLY")
        translated.append({"role": message["role"], "content": content})
    result = {"messages": translated, "max_tokens": plan["max_tokens"] or 128}
    if plan["temperature"] is not None:
        result["temperature"] = plan["temperature"]
    return result


def run(plan, query):
    calls, failure, answered = [], [], []
    dispatch = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != "/v1/models":
                self.send_error(404)
                return
            raw = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": plan["source_model"],
                            "object": "model",
                            "created": 0,
                            "owned_by": "native",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            try:
                if self.path != "/v1/chat/completions" or not dispatch.acquire(blocking=False):
                    raise ValueError("NATIVE_SINGLE_MODEL_CALL")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= LIMIT or self.headers.get("Transfer-Encoding"):
                    raise ValueError("FRAME_LIMIT")
                body = translate_request(decode(self.rfile.read(length)), plan)
                calls.append(body)
                emit({"kind": "model_request", "sequence": 1, **body})
                reply = read()
                if (
                    set(reply) != {"protocol", "kind", "sequence", "answer"}
                    or reply["kind"] != "model_response"
                    or type(reply["sequence"]) is not int
                    or reply["sequence"] != 1
                    or not isinstance(reply["answer"], str)
                    or len(reply["answer"].encode()) > 128000
                ):
                    raise ValueError("MODEL_RESPONSE")
                answered.append(True)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in [
                    ({"role": "assistant", "content": reply["answer"]}, None),
                    ({}, "stop"),
                ]:
                    chunk = {
                        "id": "native-1",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": plan["source_model"],
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                failure.append(True)
                self.send_error(400, "NATIVE_REQUEST_REJECTED")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # Connection reads are bounded too; the outer supervisor remains the hard
    # process/container deadline, including a silent stdio capability provider.
    original_get = server.get_request

    def get_request():
        connection, address = original_get()
        connection.settimeout(120)
        return connection, address

    server.get_request = get_request
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="native-") as temp:
            root = Path(temp)
            for name in ("config", "workspace", "home"):
                (root / name).mkdir()
            alias, provider = "native-text", "native-pipe"
            config = {
                "auth_modes": {
                    "api-key": {
                        provider: {
                            "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                            "apiKey": "loopback-only",
                        }
                    }
                },
                "models": {
                    alias: {
                        "alias": alias,
                        "name": alias,
                        "input": ["text"],
                        "maxOutputTokens": plan["max_tokens"] or 128,
                        "extraBody": (
                            {"temperature": plan["temperature"]}
                            if plan["temperature"] is not None
                            else {}
                        ),
                        "providers": {
                            "api-key": {
                                "provider": provider,
                                "model": plan["source_model"],
                                "api": "openai-completions",
                            }
                        },
                    }
                },
            }
            (root / "config/sudocode.json").write_text(json.dumps(config))
            prompt = plan["prompts"][0]["content"].replace("{{#sys.query#}}", query)
            emit({"kind": "trace", "node": "native-sudocode", "phase": "started"})
            result = subprocess.run(
                [
                    "/app/scode",
                    "-p",
                    "--no-tools",
                    "--no-memory",
                    "--literal-input",
                    "--model",
                    alias,
                    "--auth",
                    "api-key",
                    "--permission-mode",
                    "read-only",
                    "--system-prompt",
                    prompt,
                    "--output-format",
                    "json",
                    "--",
                    query,
                ],
                cwd=root / "workspace",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=220,
                check=False,
                env={
                    "PATH": os.defpath,
                    "HOME": str(root / "home"),
                    "SUDO_CODE_CONFIG_HOME": str(root / "config"),
                    "SUDOCODE_MEMORY_DIR": str(root / "memory"),
                    "NO_COLOR": "1",
                },
            )
            value = decode(result.stdout)
            if (
                result.returncode
                or failure
                or len(answered) != 1
                or not isinstance(value, dict)
                or value.get("type") != "result"
                or value.get("is_error") is not False
                or value.get("subtype") != "success"
                or value.get("num_turns") != 1
                or value.get("model_round_trips") != 1
                or not isinstance(value.get("result"), str)
            ):
                raise ValueError("NATIVE_EXECUTION_FAILED")
            emit({"kind": "trace", "node": "native-sudocode", "phase": "completed"})
            emit({"kind": "completed", "answer": value["result"], "model_calls": len(calls)})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def main():
    config = decode(Path("/app/profile.json").read_bytes())
    request = read()
    if (
        config.get("profile") != PROFILE
        or set(config) != {"profile", "plan", "tools", "memory", "input", "max_model_calls"}
        or config["tools"] != []
        or config["memory"] != "disabled"
        or config["input"] != "literal_text"
        or type(config["max_model_calls"]) is not int
        or config["max_model_calls"] != 1
        or set(request) != {"protocol", "profile", "query"}
        or request["profile"] != PROFILE
        or not isinstance(request["query"], str)
        or not 1 <= len(request["query"]) <= 16000
    ):
        raise ValueError("NATIVE_INPUT")
    run(config["plan"], request["query"])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emit({"kind": "failed"})
        sys.exit(1)
