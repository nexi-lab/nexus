"""Real exported DSL shape and semantic rejection cases; no model mocks as L2 evidence."""

from pathlib import Path

import pytest
import yaml

from nexus.services.product_agent_registry.dify_import import (
    DifyImportError,
    compile_dify_text,
    render_dify_turn,
)

SOURCE = Path(__file__).parents[2] / "fixtures/agent_hosted/dify-linear.yml"


def source():
    return yaml.safe_load(SOURCE.read_bytes())


def compile_doc(doc):
    return compile_dify_text(yaml.safe_dump(doc, allow_unicode=True).encode())


def test_exported_dify_application_preserves_model_prompt_and_query():
    plan = compile_dify_text(SOURCE.read_bytes())
    assert plan.source_model == "gemini-3-flash-preview"
    assert plan.temperature == 0
    assert plan.max_tokens is None
    messages = render_dify_turn(plan, "6 * 7")
    assert [p.role for p in messages] == ["system", "user"]
    assert (
        messages[0].content
        == source()["workflow"]["graph"]["nodes"][1]["data"]["prompt_template"][0]["text"]
    )
    assert messages[1].content == "6 * 7"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("version",), "0.7.0"),
        (("app", "mode"), "workflow"),
        (("workflow", "environment_variables"), [{"name": "api_key", "value": "example"}]),
        (("workflow", "features", "speech_to_text", "enabled"), True),
        (("workflow", "features", "file_upload", "enabled"), True),
        (("workflow", "features", "sensitive_word_avoidance", "enabled"), True),
        (("workflow", "features", "unknown_feature"), {}),
        (("workflow", "graph", "nodes", 0, "data", "variables"), [{"variable": "extra"}]),
        (("workflow", "graph", "nodes", 1, "data", "type"), "code"),
        (("workflow", "graph", "nodes", 1, "data", "retry_config"), {"retry_enabled": True}),
        (("workflow", "graph", "nodes", 1, "data", "context", "enabled"), True),
        (("workflow", "graph", "nodes", 1, "data", "vision", "enabled"), True),
        (("workflow", "graph", "nodes", 1, "data", "memory", "window", "enabled"), True),
        (("workflow", "graph", "nodes", 1, "data", "memory", "query_prompt_template"), "altered"),
        (("workflow", "graph", "nodes", 1, "data", "model", "provider"), "other"),
        (("workflow", "graph", "nodes", 1, "data", "model", "completion_params", "top_p"), 0.1),
        (
            ("workflow", "graph", "nodes", 1, "data", "model", "completion_params", "temperature"),
            float("nan"),
        ),
        (
            ("workflow", "graph", "nodes", 1, "data", "prompt_template", 0, "text"),
            "{{#env.secret#}}",
        ),
        (("workflow", "graph", "nodes", 2, "data", "answer"), "prefix {{#llm.text#}}"),
        (("workflow", "graph", "edges", 0, "sourceHandle"), "failure"),
        (("workflow", "graph", "edges", 0, "data", "condition"), "x"),
        (("dependencies", 0, "value", "marketplace_plugin_unique_identifier"), "other@sha256"),
    ],
)
def test_unsupported_semantics_are_rejected(path, value):
    doc = source()
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(DifyImportError):
        compile_doc(doc)


def test_duplicate_edges_and_unreachable_nodes_are_rejected():
    doc = source()
    doc["workflow"]["graph"]["edges"][1] = dict(doc["workflow"]["graph"]["edges"][0])
    with pytest.raises(DifyImportError):
        compile_doc(doc)


@pytest.mark.parametrize(
    "raw",
    [
        b"a: 1\na: 2",
        b"a: &a 1\nb: *a",
        b"x: !!python/object:os.system {}",
        b"[" * 40 + b"]" * 40,
        b"x" * 256001,
        b"\xff",
    ],
)
def test_untrusted_yaml_is_bounded(raw):
    with pytest.raises(DifyImportError):
        compile_dify_text(raw)


def test_history_is_not_silently_ignored():
    plan = compile_dify_text(SOURCE.read_bytes())
    with pytest.raises(DifyImportError, match="DIFY_CONVERSATION_UNSUPPORTED"):
        render_dify_turn(plan, "next", [{"role": "user", "content": "prior"}])


def test_digest_binds_exact_source_bytes_and_substitution_is_literal():
    raw = SOURCE.read_bytes()
    a, b = compile_dify_text(raw), compile_dify_text(raw + b"\n")
    assert a.source_digest != b.source_digest
    doc = source()
    doc["workflow"]["graph"]["nodes"][1]["data"]["prompt_template"][0]["text"] = (
        "Question: {{#sys.query#}}"
    )
    plan = compile_doc(doc)
    assert render_dify_turn(plan, "{{#env.key#}}")[0].content == "Question: {{#env.key#}}"
