"""Original-source protocol boundaries. Real LangGraph runs are integration evidence."""

import io
import json
import sys

import pytest

from nexus.bricks.sandbox import langgraph_source as source


class Input:
    def __init__(self, value):
        self.buffer = io.BytesIO(value)


class Output:
    def __init__(self):
        self.buffer = io.BytesIO()


def test_source_snapshot_is_exact_without_executing_project(tmp_path, monkeypatch):
    project = tmp_path / "project.py"
    project.write_text("raise RuntimeError('never execute at inspection')")
    monkeypatch.setattr(source.importlib.metadata, "version", lambda name: "1.0.5")
    raw, observed = source.snapshot(project)
    assert raw == project.read_bytes()
    assert observed["source_digest"].startswith("sha256:")
    project.write_text("# changed")
    assert source.snapshot(project)[1]["source_digest"] != observed["source_digest"]
    monkeypatch.setattr(source.importlib.metadata, "version", lambda name: "2.0.0")
    with pytest.raises(ValueError, match="RUNTIME_INVALID"):
        source.snapshot(project)


def test_source_change_is_rejected_before_project_code(tmp_path, monkeypatch):
    project = tmp_path / "project.py"
    project.write_text("raise AssertionError('must not execute')")
    monkeypatch.setattr(source.importlib.metadata, "version", lambda name: "1.0.5")
    monkeypatch.setattr(sys, "argv", ["entry", "--project", str(project)])
    frame = {"protocol": source.PROTOCOL, "kind": "execute", "expected_snapshot": {}}
    monkeypatch.setattr(sys, "stdin", Input(json.dumps(frame).encode() + b"\n"))
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        source.main()


def test_source_input_requires_framing_and_protocol(monkeypatch):
    for value in [b"x" * (source.LIMIT + 1), b"{}\n", b'{"protocol":"wrong"}\n']:
        monkeypatch.setattr(sys, "stdin", Input(value))
        with pytest.raises(ValueError):
            source.read()
