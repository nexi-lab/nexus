"""Fail-closed compiler for the explicitly supported Dify linear text profile.

No plugin code is loaded. This maps the declared OpenAI-compatible text model to
the host's resource. Independent turns only: runtime rejects conversation history.
"""

import hashlib
import math
import re

import yaml

from nexus.contracts.agent_hosted import DifyTextPlan, HostedPrompt

LIMIT = 256000
PROVIDER = "langgenius/openai_api_compatible/openai_api_compatible"
# A reviewed dependency mapping, not a wildcard assertion about other versions.
PLUGIN = (
    "langgenius/openai_api_compatible:0.0.68@"
    "ba61c5eccb814be7364117c0bec3710a51e09b20f31e37243e747a00f9377969"
)


class DifyImportError(ValueError):
    def __init__(self, code, path):
        self.code, self.path = code, path
        super().__init__(f"{code}: {path}")


def reject(path, code="DIFY_UNSUPPORTED"):
    raise DifyImportError(code, path)


class Loader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            reject("$", "DIFY_YAML_ALIAS_DENIED")
        self.depth = getattr(self, "depth", 0) + 1
        self.count = getattr(self, "count", 0) + 1
        if self.depth > 32 or self.count > 10000:
            reject("$", "DIFY_YAML_COMPLEXITY")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        out = {}
        for key, value in node.value:
            k = self.construct_object(key, deep=deep)
            if not isinstance(k, str) or k in out:
                reject("$", "DIFY_YAML_DUPLICATE_OR_NONSTRING_KEY")
            out[k] = self.construct_object(value, deep=deep)
        return out


def obj(value, keys, path):
    if not isinstance(value, dict) or set(value) - set(keys.split()):
        reject(path)
    return value


def empty(value, path):
    if value not in (None, [], {}, ""):
        reject(path)


def disabled(value, keys, path):
    if value is None:
        return
    value = obj(value, "enabled " + keys, path)
    if value.get("enabled") is not False:
        reject(path)


def compile_dify_text(source: bytes) -> DifyTextPlan:
    if not isinstance(source, bytes) or len(source) > LIMIT or not source:
        reject("$", "DIFY_SOURCE_SIZE")
    try:
        doc = yaml.load(source.decode("utf-8"), Loader=Loader)
    except (UnicodeError, yaml.YAMLError):
        reject("$", "DIFY_YAML_INVALID")
    d = obj(doc, "app dependencies kind version workflow", "$")
    if d.get("kind") != "app" or d.get("version") != "0.6.0":
        reject("version")
    app = obj(
        d.get("app"),
        "name description icon icon_background icon_type mode use_icon_as_answer_icon",
        "app",
    )
    if app.get("mode") != "advanced-chat":
        reject("app.mode")
    deps = d.get("dependencies")
    if not isinstance(deps, list) or len(deps) != 1:
        reject("dependencies")
    dep = obj(deps[0], "type current_identifier value", "dependencies[0]")
    value = obj(
        dep.get("value"), "marketplace_plugin_unique_identifier version", "dependency.value"
    )
    if (
        dep.get("type") != "marketplace"
        or dep.get("current_identifier") is not None
        or value.get("marketplace_plugin_unique_identifier") != PLUGIN
        or value.get("version") is not None
    ):
        reject("dependencies[0]")
    wf = obj(
        d.get("workflow"),
        "conversation_variables environment_variables features graph rag_pipeline_variables",
        "workflow",
    )
    for key in ("conversation_variables", "environment_variables", "rag_pipeline_variables"):
        empty(wf.get(key), "workflow." + key)
    features = obj(
        wf.get("features", {}),
        "file_upload opening_statement retriever_resource sensitive_word_avoidance speech_to_text "
        "suggested_questions suggested_questions_after_answer text_to_speech",
        "workflow.features",
    )
    empty(features.get("opening_statement"), "features.opening_statement")
    empty(features.get("suggested_questions"), "features.suggested_questions")
    for key in ("sensitive_word_avoidance", "speech_to_text", "suggested_questions_after_answer"):
        disabled(features.get(key), "", "features." + key)
    disabled(features.get("text_to_speech"), "language voice", "features.text_to_speech")
    disabled(
        features.get("file_upload"),
        "allowed_file_extensions allowed_file_types allowed_file_upload_methods "
        "fileUploadConfig image number_limits",
        "features.file_upload",
    )
    if "retriever_resource" in features:
        rr = obj(features["retriever_resource"], "enabled", "features.retriever_resource")
        if type(rr.get("enabled")) is not bool:
            reject("features.retriever_resource")
        # No knowledge/retrieval node or context is admitted; citations are always empty.
    graph = obj(wf.get("graph"), "nodes edges viewport", "workflow.graph")
    if not isinstance(graph.get("nodes"), list) or len(graph["nodes"]) != 3:
        reject("graph.nodes")
    nodes, kinds = {}, {}
    for i, node in enumerate(graph["nodes"]):
        node = obj(
            node,
            "id type data selected height width position positionAbsolute sourcePosition targetPosition",
            f"nodes[{i}]",
        )
        nid = node.get("id")
        if (
            not isinstance(nid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", nid)
            or nid in nodes
        ):
            reject(f"nodes[{i}].id")
        data = node.get("data")
        if not isinstance(data, dict) or data.get("type") not in ("start", "llm", "answer"):
            reject(f"nodes[{i}].data.type")
        kind = data["type"]
        if kind in kinds:
            reject("graph.nodes")
        nodes[nid], kinds[kind] = data, nid
    start, llm, answer = (kinds[k] for k in ("start", "llm", "answer"))
    edges = graph.get("edges")
    if not isinstance(edges, list) or len(edges) != 2:
        reject("graph.edges")
    pairs = set()
    for edge in edges:
        edge = obj(edge, "id type source target sourceHandle targetHandle data", "graph.edge")
        a, b = edge.get("source"), edge.get("target")
        if not isinstance(a, str) or not isinstance(b, str):
            reject("graph.edge")
        pairs.add((a, b))
        if (
            edge.get("sourceHandle", "source") != "source"
            or edge.get("targetHandle", "target") != "target"
        ):
            reject("graph.edge.handle")
        meta = obj(edge.get("data", {}), "sourceType targetType", "graph.edge.data")
        if (
            a not in nodes
            or b not in nodes
            or any(
                meta.get(k, expected) != expected
                for k, expected in (
                    ("sourceType", nodes[a]["type"]),
                    ("targetType", nodes[b]["type"]),
                )
            )
        ):
            reject("graph.edge.data")
    if pairs != {(start, llm), (llm, answer)}:
        reject("graph.edges")
    s = obj(nodes[start], "type title desc selected variables", "start")
    empty(s.get("variables"), "start.variables")
    a = obj(nodes[answer], "type title desc selected variables answer", "answer")
    empty(a.get("variables"), "answer.variables")
    if a.get("answer") != "{{#" + llm + ".text#}}":
        reject("answer.answer")
    m = obj(
        nodes[llm], "type title desc selected context memory model prompt_template vision", "llm"
    )
    disabled(m.get("context"), "variable_selector", "llm.context")
    disabled(m.get("vision"), "", "llm.vision")
    memory = obj(m.get("memory"), "query_prompt_template role_prefix window", "llm.memory")
    if memory.get("query_prompt_template") != "{{#sys.query#}}":
        reject("llm.memory.query_prompt_template")
    prefix = obj(memory.get("role_prefix", {}), "assistant user", "llm.memory.role_prefix")
    if any(prefix.values()):
        reject("llm.memory.role_prefix")
    window = obj(memory.get("window", {}), "enabled size", "llm.memory.window")
    if window.get("enabled") is not False:
        reject("llm.memory.window")
    model = obj(m.get("model"), "completion_params mode name provider", "llm.model")
    if model.get("mode") != "chat" or model.get("provider") != PROVIDER:
        reject("llm.model.provider")
    if not isinstance(model.get("name"), str) or not 1 <= len(model["name"]) <= 200:
        reject("llm.model.name")
    params = obj(
        model.get("completion_params", {}), "temperature max_tokens", "llm.model.completion_params"
    )
    temp, tokens = params.get("temperature"), params.get("max_tokens")
    if temp is not None and (
        type(temp) not in (int, float) or not math.isfinite(temp) or not 0 <= temp <= 2
    ):
        reject("llm.model.temperature")
    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= 4096):
        reject("llm.model.max_tokens")
    prompts = m.get("prompt_template")
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 16:
        reject("llm.prompt_template")
    result = []
    for i, prompt in enumerate(prompts):
        prompt = obj(prompt, "role text id", f"llm.prompt_template[{i}]")
        text = prompt.get("text")
        if (
            prompt.get("role") not in ("system", "user", "assistant")
            or not isinstance(text, str)
            or len(text) > 32000
            or "{{" in text.replace("{{#sys.query#}}", "")
            or "}}" in text.replace("{{#sys.query#}}", "")
        ):
            reject(f"llm.prompt_template[{i}]")
        result.append(HostedPrompt(role=prompt["role"], content=text))
    return DifyTextPlan(
        source_digest="sha256:" + hashlib.sha256(source).hexdigest(),
        source_model=model["name"],
        source_provider=PROVIDER,
        source_plugin=PLUGIN,
        llm_node=llm,
        prompts=result,
        temperature=float(temp) if temp is not None else None,
        max_tokens=tokens,
    )


def render_dify_turn(plan: DifyTextPlan, query: str, history=None) -> list[HostedPrompt]:
    if history:
        reject("history", "DIFY_CONVERSATION_UNSUPPORTED")
    if not isinstance(query, str) or not 1 <= len(query) <= 16000:
        reject("query", "DIFY_INPUT_INVALID")
    prompts = [
        HostedPrompt(role=p.role, content=p.content.replace("{{#sys.query#}}", query))
        for p in plan.prompts
    ]
    prompts.append(HostedPrompt(role="user", content=query))
    if sum(len(p.content.encode()) for p in prompts) > 128000:
        reject("query", "DIFY_INPUT_SIZE")
    return prompts
