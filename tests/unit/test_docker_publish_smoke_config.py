"""Static guards for Docker Publish smoke-test startup probes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER_PUBLISH = ROOT / ".github/workflows/docker-publish.yml"
DOCKERFILE = ROOT / "Dockerfile"
DOCKER_ENTRYPOINT = ROOT / "dockerfiles/docker-entrypoint.sh"


def test_image_healthcheck_uses_bounded_basic_health_probe() -> None:
    text = DOCKERFILE.read_text()
    healthcheck = text[text.index("# Healthcheck") : text.index("ENTRYPOINT")]

    assert "curl --max-time 5 -f" in healthcheck
    assert "/health" in healthcheck
    assert "/healthz/ready" not in healthcheck


def test_entrypoint_startup_wait_uses_bounded_basic_health_probe() -> None:
    text = DOCKER_ENTRYPOINT.read_text()
    wait_for_health = text[
        text.index("wait_for_health()") : text.index("load_saved_mounts_if_needed()")
    ]

    assert 'curl --max-time 5 -sf "http://localhost:${port}/health"' in wait_for_health
    assert 'curl -sf "http://localhost:${port}/health"' not in wait_for_health
    assert "/healthz/ready" not in wait_for_health
    assert (
        'echo -e "${YELLOW}⚠ Server health check timeout after ${max} attempts '
        '(continuing anyway)${NC}"\n    return 0'
    ) in wait_for_health


def test_search_plugin_sidecar_has_embedder_wiring() -> None:
    """#4646: the sidecar must boot with a usable local embedder.

    The published plugin-host image deliberately ships neither the ORT
    dylib nor the model (docs/deployment/search-plugin.md), so the
    workflow must provision + mount them — otherwise hybrid queries are
    typed-unavailable, the HERB gate scores 0, and edge promotion
    sticks.
    """
    text = DOCKER_PUBLISH.read_text()
    provision_step = text[
        text.index("- name: Provision embedder assets") : text.index(
            "- name: Start the published Rust Search host"
        )
    ]
    sidecar_step = text[
        text.index("- name: Start the published Rust Search host") : text.index(
            "- name: Verify caller credentials"
        )
    ]

    # Provisioning covers both halves of the local-ONNX contract.
    assert "onnxruntime-linux-x64" in provision_step
    for model_file in (
        "model.onnx",
        "tokenizer.json",
        "config.json",
        "special_tokens_map.json",
        "tokenizer_config.json",
    ):
        assert model_file in provision_step, f"missing model file {model_file}"

    # The sidecar container actually receives them.
    assert "-e ORT_DYLIB_PATH=/embedder/ort/libonnxruntime.so" in sidecar_step
    assert "-e NEXUS_SEARCH_MODEL_DIR=/embedder/model" in sidecar_step
    assert "-v /tmp/nexus-embedder:/embedder:ro" in sidecar_step
