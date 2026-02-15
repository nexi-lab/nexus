"""Validation runner — orchestrates the validation pipeline inside sandboxes.

Coordinates detection, script building, execution, and result parsing
into a single pipeline that runs all applicable validators.
"""

from __future__ import annotations

import logging
import shlex
import time
from typing import TYPE_CHECKING

from nexus.sandbox.sandbox_provider import validate_mount_path
from nexus.validation.config import ValidatorConfigLoader
from nexus.validation.detector import detect_project_validators
from nexus.validation.models import (
    ValidationPipelineConfig,
    ValidationResult,
    ValidatorConfig,
)
from nexus.validation.parsers import BUILTIN_VALIDATORS
from nexus.validation.script_builder import (
    build_simple_validation_script,
    parse_simple_script_output,
)

if TYPE_CHECKING:
    from nexus.sandbox.sandbox_provider import SandboxProvider

logger = logging.getLogger(__name__)


class ValidationRunner:
    """Orchestrates validation pipeline inside sandboxes."""

    def __init__(self, config_loader: ValidatorConfigLoader | None = None) -> None:
        self._config_loader = config_loader or ValidatorConfigLoader()

    async def validate(
        self,
        sandbox_id: str,
        provider: SandboxProvider,
        workspace_path: str = "/workspace",
        config: ValidationPipelineConfig | None = None,
    ) -> list[ValidationResult]:
        """Run all applicable validators in a sandbox.

        Pipeline:
        1. Load config (cached) or auto-detect project type
        2. Build combined bash script
        3. Execute via provider.run_code()
        4. Parse structured output using per-validator parsers
        5. Return list[ValidationResult]

        Args:
            sandbox_id: Sandbox identifier.
            provider: Sandbox provider for executing commands.
            workspace_path: Path to workspace root in sandbox.
            config: Explicit config, or None for auto-detection.

        Returns:
            List of validation results, one per validator.
        """
        validate_mount_path(workspace_path)
        pipeline_start = time.monotonic()

        # 1. Resolve validator configs
        validator_configs = await self._resolve_configs(
            sandbox_id, provider, workspace_path, config
        )

        if not validator_configs:
            logger.debug("No validators applicable for sandbox %s", sandbox_id)
            return []

        # 2. Build combined script
        timeout = config.max_total_timeout if config else 30
        script = build_simple_validation_script(validator_configs, workspace_path)

        # 3. Execute
        try:
            exec_result = await provider.run_code(sandbox_id, "bash", script, timeout=timeout)
        except Exception as e:
            logger.error("Validation script execution failed: %s", e)
            return [
                ValidationResult(
                    validator="pipeline",
                    passed=False,
                    errors=[],
                    duration_ms=int((time.monotonic() - pipeline_start) * 1000),
                )
            ]

        # 4. Parse structured output
        raw_results = parse_simple_script_output(exec_result.stdout)

        # 5. Build ValidationResults using per-validator parsers
        results: list[ValidationResult] = []
        for raw in raw_results:
            name = str(raw["name"])
            validator_start = time.monotonic()

            # Find matching config
            matching_config = next((c for c in validator_configs if c.name == name), None)

            # Get parser
            parser_cls = BUILTIN_VALIDATORS.get(name)
            if parser_cls and matching_config:
                parser = parser_cls(matching_config)
                errors = parser.parse_output(
                    str(raw.get("stdout", "")),
                    str(raw.get("stderr", "")),
                    int(raw.get("exit_code", 1)),
                )
            else:
                errors = []

            exit_code = int(raw.get("exit_code", 1))
            duration = int((time.monotonic() - validator_start) * 1000)

            results.append(
                ValidationResult(
                    validator=name,
                    passed=exit_code == 0 and len(errors) == 0,
                    errors=errors,
                    duration_ms=duration,
                )
            )

        total_ms = int((time.monotonic() - pipeline_start) * 1000)
        logger.info(
            "Validation pipeline completed in %dms: %d validators, %d total errors",
            total_ms,
            len(results),
            sum(len(r.errors) for r in results),
        )

        return results

    async def _resolve_configs(
        self,
        sandbox_id: str,
        provider: SandboxProvider,
        workspace_path: str,
        explicit_config: ValidationPipelineConfig | None,
    ) -> list[ValidatorConfig]:
        """Resolve which validators to run.

        Priority:
        1. Explicit config passed as argument
        2. validators.yaml in workspace (loaded via config_loader)
        3. Auto-detection from project files

        Args:
            sandbox_id: Sandbox identifier.
            provider: Sandbox provider.
            workspace_path: Workspace root path.
            explicit_config: Explicitly provided config.

        Returns:
            List of enabled ValidatorConfig objects.
        """
        if explicit_config and explicit_config.validators:
            return [v for v in explicit_config.validators if v.enabled]

        # Try to load validators.yaml from workspace
        try:
            yaml_result = await provider.run_code(
                sandbox_id,
                "bash",
                f"cat {shlex.quote(workspace_path + '/validators.yaml')} 2>/dev/null",
                timeout=5,
            )
            if yaml_result.exit_code == 0 and yaml_result.stdout.strip():
                loaded = self._config_loader.load_from_string(
                    yaml_result.stdout, cache_key=f"{sandbox_id}:{workspace_path}"
                )
                if loaded.validators:
                    return [v for v in loaded.validators if v.enabled]
        except Exception as e:
            logger.debug("Could not load validators.yaml: %s", e)

        # Fall back to auto-detection
        detected_names = await detect_project_validators(sandbox_id, provider, workspace_path)

        configs: list[ValidatorConfig] = []
        for name in detected_names:
            validator_cls = BUILTIN_VALIDATORS.get(name)
            if validator_cls:
                # Create a default config and instantiate to get the command
                instance = validator_cls()
                configs.append(instance.config)

        return configs
