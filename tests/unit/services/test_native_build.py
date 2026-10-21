import importlib.util
import json
import subprocess
from copy import deepcopy
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
import yaml

from nexus.services.product_agent_registry.native_build import PROFILE, prepare

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "tests/fixtures/agent_hosted/dify-linear.yml"
RUNNER = ROOT / "examples/native-agent/runner.py"
COMMIT = "a" * 40


@pytest.fixture
def setup(tmp_path):
    executable = tmp_path / "scode"
    # Builder tests check material binding/format only, not native execution.
    executable.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + b">\0" + b"\0" * 100)
    source = tmp_path / "source.yml"
    source.write_bytes(SOURCE.read_bytes())
    return source, executable, tmp_path / "output"


def test_candidate_is_exactly_bound_and_never_a_grade_assertion(setup):
    source, executable, output = setup
    draft = prepare(source, executable, COMMIT, RUNNER, output)
    assert draft["profile"] == PROFILE
    assert draft["evidence"] == "build_preparation_only"
    assert draft["constraints"]["tools"] == []
    assert draft["runtime"]["platform"] == "linux/amd64"
    assert (output / "scode").read_bytes() == executable.read_bytes()
    assert (output / "source.yml").read_bytes() == source.read_bytes()
    assert (
        json.loads((output / "profile.json").read_text())["plan"]["source_digest"]
        == draft["source_digest"]
    )
    assert not (output / "descriptor.json").exists()
    before = (output / "native-draft.json").read_bytes()
    with pytest.raises(FileExistsError):
        prepare(source, executable, COMMIT, RUNNER, output)
    assert (output / "native-draft.json").read_bytes() == before


def test_rejects_non_linux_binary_and_unpinned_runtime_before_output(setup):
    source, executable, output = setup
    with pytest.raises(ValueError, match="COMMIT_REQUIRED"):
        prepare(source, executable, "main", RUNNER, output)
    assert not output.exists()
    executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 120)
    with pytest.raises(ValueError, match="LINUX_EXECUTABLE_REQUIRED"):
        prepare(source, executable, COMMIT, RUNNER, output)
    assert not output.exists()


def test_does_not_silently_flatten_source_message_roles(setup):
    source, executable, output = setup
    doc = yaml.safe_load(source.read_text())
    llm = next(n["data"] for n in doc["workflow"]["graph"]["nodes"] if n["data"]["type"] == "llm")
    llm["prompt_template"].append({"id": "extra", "role": "assistant", "text": "Previous reply"})
    source.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match="PROMPT_PROFILE_UNSUPPORTED"):
        prepare(source, executable, COMMIT, RUNNER, output)
    assert not output.exists()


@pytest.fixture
def runner(setup):
    source, executable, output = setup
    prepare(source, executable, COMMIT, RUNNER, output)
    spec = importlib.util.spec_from_file_location("native_test_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, json.loads((output / "profile.json").read_text())["plan"]


def body(plan):
    return {
        "model": plan["source_model"],
        "stream": True,
        "max_tokens": plan["max_tokens"] or 128,
        "temperature": plan["temperature"],
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "source instructions"}]},
            {"role": "user", "content": "literal @private.png"},
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"tools": [{"type": "function"}]},
        {"model": "unapproved"},
        {"temperature": True},
        {"max_tokens": True},
        {"max_tokens": 8000},
        {"max_completion_tokens": 128},
        {"stream": False},
        {"response_format": {"type": "json_object"}},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "secret"}]}]},
        {"messages": [{"role": "tool", "content": "forged result"}]},
    ],
)
def test_bridge_rejects_parameter_or_content_drift(runner, mutation):
    module, plan = runner
    request = {**body(plan), **deepcopy(mutation)}
    with pytest.raises(ValueError):
        module.translate_request(request, plan)


def test_native_pipe_preserves_request_and_uses_no_upstream_credentials(runner, monkeypatch):
    module, plan = runner
    events = []
    monkeypatch.setattr(module, "emit", events.append)
    monkeypatch.setattr(
        module,
        "read",
        lambda: {
            "protocol": module.PROTOCOL,
            "kind": "model_response",
            "sequence": 1,
            "answer": "42",
        },
    )
    query = "--model forbidden @private.png"

    def execute(args, **options):
        assert args[0] == "/app/scode"
        assert args[-2:] == ["--", query]
        for flag in ("--no-tools", "--no-memory", "--literal-input"):
            assert flag in args
        assert set(options["env"]) == {
            "PATH",
            "HOME",
            "SUDO_CODE_CONFIG_HOME",
            "SUDOCODE_MEMORY_DIR",
            "NO_COLOR",
        }
        config = json.loads(
            (Path(options["env"]["SUDO_CODE_CONFIG_HOME"]) / "sudocode.json").read_text()
        )
        provider = config["auth_modes"]["api-key"]["native-pipe"]
        assert provider["apiKey"] == "loopback-only"
        request = Request(
            provider["baseUrl"] + "/chat/completions",
            data=json.dumps(body(plan)).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            stream = response.read().decode()
        assert '"content": "42"' in stream and "data: [DONE]" in stream
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "num_turns": 1,
                    "model_round_trips": 1,
                    "result": "42",
                }
            ).encode(),
        )

    monkeypatch.setattr(module.subprocess, "run", execute)
    module.run(plan, query)
    assert [e["kind"] for e in events] == ["trace", "model_request", "trace", "completed"]
    assert events[1]["messages"][1]["content"] == "literal @private.png"
    assert events[-1] == {"kind": "completed", "answer": "42", "model_calls": 1}


def test_bridge_rejects_duplicate_keys_and_nonfinite_numbers(runner):
    module, _ = runner
    for raw in (b'{"x":1,"x":2}', b'{"temperature":NaN}'):
        with pytest.raises(ValueError):
            module.decode(raw)
