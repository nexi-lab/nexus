"""Tests for validation config loader."""

from __future__ import annotations

import os
import tempfile

from nexus.validation.config import ValidatorConfigLoader, _parse_yaml_content
from nexus.validation.models import ValidationPipelineConfig


class TestParseYamlContent:
    def test_valid_yaml(self):
        content = "validators:\n  - name: ruff\n    command: ruff check .\n"
        result = _parse_yaml_content(content)
        assert isinstance(result, dict)
        assert "validators" in result

    def test_empty_string(self):
        assert _parse_yaml_content("") == {}

    def test_invalid_yaml(self):
        assert _parse_yaml_content("{{{") == {}

    def test_non_dict_yaml(self):
        assert _parse_yaml_content("- item1\n- item2") == {}


class TestValidatorConfigLoader:
    def test_load_from_string_basic(self):
        loader = ValidatorConfigLoader()
        content = (
            "auto_run: true\n"
            "max_total_timeout: 20\n"
            "validators:\n"
            "  - name: ruff\n"
            "    command: ruff check .\n"
            "    timeout: 10\n"
        )
        config = loader.load_from_string(content)
        assert len(config.validators) == 1
        assert config.validators[0].name == "ruff"
        assert config.auto_run is True
        assert config.max_total_timeout == 20

    def test_load_from_string_empty(self):
        loader = ValidatorConfigLoader()
        config = loader.load_from_string("")
        assert config == ValidationPipelineConfig()

    def test_load_from_string_invalid_validators_type(self):
        loader = ValidatorConfigLoader()
        config = loader.load_from_string('validators: "not a list"')
        assert config.validators == []

    def test_load_from_string_missing_required_fields(self):
        loader = ValidatorConfigLoader()
        content = "validators:\n  - name: ruff\n"  # missing command
        config = loader.load_from_string(content)
        assert config.validators == []

    def test_load_from_file(self, fixtures_dir):
        loader = ValidatorConfigLoader()
        config = loader.load_from_file(str(fixtures_dir / "validators_basic.yaml"))
        assert len(config.validators) == 2
        assert config.validators[0].name == "ruff"
        assert config.validators[1].name == "mypy"

    def test_load_from_file_nonexistent(self):
        loader = ValidatorConfigLoader()
        config = loader.load_from_file("/nonexistent/path/validators.yaml")
        assert config == ValidationPipelineConfig()

    def test_mtime_caching(self):
        loader = ValidatorConfigLoader()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("validators:\n  - name: ruff\n    command: ruff check .\n")
            f.flush()
            path = f.name

        try:
            config1 = loader.load_from_file(path)
            config2 = loader.load_from_file(path)
            # Both should return same content
            assert len(config1.validators) == len(config2.validators)
            # Second call should use cache (same object)
            assert config1 is config2
        finally:
            os.unlink(path)

    def test_invalidate_specific(self):
        loader = ValidatorConfigLoader()
        loader._cache["key"] = ValidationPipelineConfig()
        loader._mtimes["key"] = 1.0
        loader.invalidate("key")
        assert "key" not in loader._cache
        assert "key" not in loader._mtimes

    def test_invalidate_all(self):
        loader = ValidatorConfigLoader()
        loader._cache["a"] = ValidationPipelineConfig()
        loader._cache["b"] = ValidationPipelineConfig()
        loader.invalidate()
        assert len(loader._cache) == 0

    def test_load_from_file_invalid_yaml(self, fixtures_dir):
        loader = ValidatorConfigLoader()
        config = loader.load_from_file(str(fixtures_dir / "validators_invalid.yaml"))
        # Should gracefully handle - validators is a string, not list
        assert config.validators == []
