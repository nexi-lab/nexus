"""Build contexts pin sources without importing user Python on the management host."""

from pathlib import Path

import pytest

from nexus.services.product_agent_registry.hosted_build import prepare
from tests.unit.services.test_dify_import import SOURCE

RUNTIME = Path(__file__).parents[3] / "examples/hosted-agent"


def test_dify_context_binds_same_model_and_offline_dependencies(tmp_path):
    out = tmp_path / "context"
    draft = prepare(
        SOURCE, "dify-0.6.0-linear-text-v1", RUNTIME, out, "test-model", "gemini-3-flash-preview"
    )
    assert "image_id" not in draft
    assert draft["model_name"] == "gemini-3-flash-preview"
    dockerfile = (out / "Dockerfile").read_text()
    assert "--no-index" in dockerfile and "--require-hashes" in dockerfile
    assert "FROM python@sha256:" in dockerfile
    assert (out / "source.yml").read_bytes() == SOURCE.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(
            SOURCE,
            "dify-0.6.0-linear-text-v1",
            RUNTIME,
            out,
            "test-model",
            "gemini-3-flash-preview",
        )


def test_model_substitution_fails_before_writing_context(tmp_path):
    out = tmp_path / "context"
    with pytest.raises(ValueError, match="HOSTED_SOURCE_MODEL_MISMATCH"):
        prepare(SOURCE, "dify-0.6.0-linear-text-v1", RUNTIME, out, "test-model", "different")
    assert not out.exists()


def test_python_source_is_copied_without_import_execution(tmp_path):
    source = tmp_path / "project.py"
    source.write_text("raise RuntimeError('must not run during import')\n")
    out = tmp_path / "context"
    prepare(source, "langgraph-stdio-v1", RUNTIME, out, "test-model", "example")
    assert (out / "project.py").read_bytes() == source.read_bytes()
