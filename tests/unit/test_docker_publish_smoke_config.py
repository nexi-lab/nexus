"""Static guards for Docker Publish smoke-test startup probes."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER_PUBLISH = ROOT / ".github/workflows/docker-publish.yml"
DOCKERFILE = ROOT / "Dockerfile"
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


def test_build_perf_smoke_uses_basic_health_probe() -> None:
    text = BUILD_PERF.read_text()

    assert 'step("health endpoint GET /health")' in text
    assert 'urlopen(f"{NEXUS_URL}/health", timeout=5)' in text
    assert "/healthz/ready" not in text
