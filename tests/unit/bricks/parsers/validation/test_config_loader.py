from __future__ import annotations

import os

from nexus.bricks.parsers.validation.config import ValidatorConfigLoader


def _yaml_for(command: str) -> str:
    return f"validators:\n  - name: ruff\n    command: {command}\n"


def test_load_from_string_reparses_when_cached_content_changes() -> None:
    loader = ValidatorConfigLoader()

    first = loader.load_from_string(_yaml_for("ruff check ."), cache_key="workspace")
    second = loader.load_from_string(_yaml_for("ruff check src"), cache_key="workspace")

    assert second is not first
    assert second.validators[0].command == "ruff check src"


def test_load_from_file_reparses_when_mtime_changes(tmp_path) -> None:
    config_path = tmp_path / "validators.yaml"
    loader = ValidatorConfigLoader()

    config_path.write_text(_yaml_for("ruff check ."))
    first = loader.load_from_file(str(config_path))

    config_path.write_text(_yaml_for("ruff check src"))
    new_mtime = os.path.getmtime(config_path) + 1
    os.utime(config_path, (new_mtime, new_mtime))

    second = loader.load_from_file(str(config_path))

    assert second is not first
    assert second.validators[0].command == "ruff check src"
