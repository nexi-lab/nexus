"""Static guards for Docker Publish smoke-test startup probes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER_PUBLISH = ROOT / ".github/workflows/docker-publish.yml"
DOCKERFILE = ROOT / "Dockerfile"
DOCKER_ENTRYPOINT = ROOT / "dockerfiles/docker-entrypoint.sh"
BUILD_PERF = ROOT / "scripts/test_build_perf_e2e.py"


def test_docker_publish_startup_gate_uses_basic_health_probe() -> None:
    text = DOCKER_PUBLISH.read_text()
    start_step = text[
        text.index("- name: Start Nexus edge container") : text.index(
            "- name: Initialize and extract credentials"
        )
    ]

    assert (
        "docker exec nexus-e2e curl --max-time 5 -sf http://127.0.0.1:2026/health"
    ) in start_step
    assert "if curl --max-time 5 -sf http://127.0.0.1:2026/health" not in start_step
    assert "/healthz/ready" not in start_step


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


def test_build_perf_smoke_uses_basic_health_probe() -> None:
    text = BUILD_PERF.read_text()

    assert 'step("health endpoint GET /health")' in text
    assert 'urlopen(f"{NEXUS_URL}/health", timeout=5)' in text
    assert "/healthz/ready" not in text


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
            "- name: Start search-plugin sidecar"
        )
    ]
    sidecar_step = text[
        text.index("- name: Start search-plugin sidecar") : text.index(
            "- name: Start Nexus edge container"
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
