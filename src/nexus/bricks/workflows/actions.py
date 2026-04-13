"""Built-in workflow actions.

Zero imports from nexus.core or nexus.llm — all Nexus operations are accessed
via ``context.services`` (a WorkflowServices dataclass injected by the engine).

Security hardening (Issues #1756, #1596):
- safe_interpolate() uses regex substitution instead of .format() to prevent
  Python format-string attribute access attacks ({0.__class__.__mro__}).
- LLMAction uses a hardcoded safety system prompt and wraps file content in
  XML data tags for data-instruction separation.
- BashAction and PythonAction require SandboxManager (fail-closed).
- WebhookAction validates URLs against SSRF blocklist.
- All actions use generic error messages with structured logging.
"""

import contextlib
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import aiohttp

from nexus.bricks.workflows.types import ActionResult, WorkflowContext
from nexus.lib.security.prompt_sanitizer import (
    sanitize_for_prompt,
)
from nexus.lib.security.url_validator import validate_outbound_url

logger = logging.getLogger(__name__)


class BaseAction(ABC):
    """Base class for workflow actions."""

    # Pre-compiled regex for safe variable interpolation (Issue #1756).
    # Only matches simple {variable_name} — no attribute access, indexing,
    # or format specs like {0.__class__} or {config[SECRET]}.
    _SAFE_VAR_RE = re.compile(r"\{(\w+)\}")

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @abstractmethod
    async def execute(self, context: WorkflowContext) -> ActionResult:
        """Execute the action."""
        pass

    def safe_interpolate(self, value: str, context: WorkflowContext) -> str:
        """Safely interpolate variables using regex substitution (Issue #1756).

        Uses ``re.sub`` instead of ``str.format()`` to prevent Python format
        string injection attacks (e.g., ``{0.__class__.__mro__}``).

        Only simple ``{variable_name}`` patterns are replaced. Anything with
        dots, brackets, or format specs is left as-is.
        """
        if not isinstance(value, str):
            return value

        variables = {**context.variables}
        if context.file_path:
            variables["file_path"] = context.file_path
            variables["filename"] = Path(context.file_path).name
            variables["dirname"] = Path(context.file_path).parent.as_posix()
        if context.file_metadata:
            variables.update(context.file_metadata)

        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key in variables:
                return str(variables[key])
            # Leave unresolved placeholders as-is
            return match.group(0)

        return self._SAFE_VAR_RE.sub(_replace, value)


class ParseAction(BaseAction):
    """Parse a document."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            if not context.services or not context.services.nexus_ops:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error="nexus_ops service not injected",
                )

            file_path = self.safe_interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            parser = self.config.get("parser", "auto")

            result = await context.services.nexus_ops.parse(file_path, parser=parser)

            return ActionResult(
                action_name=self.name, success=True, output={"parsed_content": result}
            )
        except Exception as e:
            logger.error("Parse action '%s' failed: %s", self.name, e, exc_info=True)
            return ActionResult(action_name=self.name, success=False, error="Parse action failed")


class TagAction(BaseAction):
    """Add or remove tags."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            if not context.services or not context.services.nexus_ops:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error="nexus_ops service not injected",
                )

            file_path = self.safe_interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            tags = self.config.get("tags", [])
            remove = self.config.get("remove", False)

            interpolated_tags = [self.safe_interpolate(tag, context) for tag in tags]

            if remove:
                for tag in interpolated_tags:
                    await context.services.nexus_ops.remove_tag(file_path, tag)
            else:
                for tag in interpolated_tags:
                    await context.services.nexus_ops.add_tag(file_path, tag)

            return ActionResult(
                action_name=self.name,
                success=True,
                output={"tags": interpolated_tags, "removed": remove},
            )
        except Exception as e:
            logger.error("Tag action '%s' failed: %s", self.name, e, exc_info=True)
            return ActionResult(action_name=self.name, success=False, error="Tag action failed")


class MoveAction(BaseAction):
    """Move or rename a file."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            if not context.services or not context.services.nexus_ops:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error="nexus_ops service not injected",
                )

            source = self.safe_interpolate(
                str(self.config.get("source", context.file_path)), context
            )
            destination = self.safe_interpolate(self.config["destination"], context)
            create_parents = self.config.get("create_parents", False)

            if create_parents:
                parent_dir = str(Path(destination).parent)
                # Use VFS mkdir (not local Path.exists) — idempotent to avoid TOCTOU
                with contextlib.suppress(Exception):
                    context.services.nexus_ops.mkdir(parent_dir, parents=True)

            await context.services.nexus_ops.rename(source, destination)

            return ActionResult(
                action_name=self.name,
                success=True,
                output={"source": source, "destination": destination},
            )
        except Exception as e:
            logger.error("Move action '%s' failed: %s", self.name, e, exc_info=True)
            return ActionResult(action_name=self.name, success=False, error="Move action failed")


class MetadataAction(BaseAction):
    """Update file metadata."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            if not context.services or not context.services.metadata_store:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error="metadata_store service not injected",
                )

            file_path = self.safe_interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            metadata = self.config.get("metadata", {})

            interpolated_metadata = {
                key: self.safe_interpolate(str(value), context) for key, value in metadata.items()
            }

            # Hoist path lookup outside loop to avoid N+1 queries (Issue #3063)
            path_rec = await context.services.metadata_store.get_path(file_path)
            if path_rec:
                for key, value in interpolated_metadata.items():
                    await context.services.metadata_store.set_file_metadata(
                        path_rec.path_id, key, value
                    )

            return ActionResult(
                action_name=self.name,
                success=True,
                output={"metadata": interpolated_metadata},
            )
        except Exception as e:
            logger.error("Metadata action '%s' failed: %s", self.name, e, exc_info=True)
            return ActionResult(
                action_name=self.name, success=False, error="Metadata action failed"
            )


class WebhookAction(BaseAction):
    """Send HTTP webhook (Issue #1596/#1756 hardened).

    Security measures:
    - SSRF protection via validate_outbound_url().
    - Body values sanitized with sanitize_for_prompt() + safe_interpolate().
    - Generic error messages (no internal detail leakage).
    - Uses shared HTTP session from context.services when available.
    """

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            url = self.safe_interpolate(self.config["url"], context)

            # SSRF protection: block private/internal IPs (Issue #1596)
            try:
                _url, _resolved_ips = validate_outbound_url(url)
            except ValueError as ssrf_err:
                logger.warning("Webhook SSRF blocked for action '%s': %s", self.name, ssrf_err)
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error="Webhook URL blocked by security policy",
                )

            method = self.config.get("method", "POST").upper()
            headers = self.config.get("headers", {})
            body = self.config.get("body", {})

            # Use safe_interpolate + strip control characters (Issue #1756)
            interpolated_body = {
                key: sanitize_for_prompt(self.safe_interpolate(str(value), context))
                for key, value in body.items()
            }

            # Use shared HTTP session if available (Issue #1756 V2)
            shared_session = (
                getattr(context.services, "http_session", None) if context.services else None
            )

            if shared_session is not None:
                async with shared_session.request(
                    method, url, json=interpolated_body, headers=headers
                ) as response:
                    response_text = await response.text()
                    status = response.status
            else:
                async with (
                    aiohttp.ClientSession() as session,
                    session.request(
                        method, url, json=interpolated_body, headers=headers
                    ) as response,
                ):
                    response_text = await response.text()
                    status = response.status

            return ActionResult(
                action_name=self.name,
                success=status < 400,
                output={"status": status, "response": response_text},
            )
        except Exception as e:
            logger.error("Webhook action '%s' failed: %s", self.name, e)
            return ActionResult(
                action_name=self.name,
                success=False,
                error="Webhook delivery failed",
            )


class SandboxedAction(BaseAction):
    """Base class for actions requiring sandbox execution (Issue #1756).

    Routes all code execution through the SandboxManager brick for
    process-level isolation. Fails closed if no sandbox provider is available.
    """

    async def _run_in_sandbox(
        self,
        context: WorkflowContext,
        language: str,
        code: str,
        timeout: int,
    ) -> ActionResult:
        """Execute code in a sandboxed environment.

        Args:
            context: Workflow execution context.
            language: Execution language ("python" or "bash").
            code: Code/command to execute.
            timeout: Execution timeout in seconds.

        Returns:
            ActionResult with sandbox execution results.
        """
        sandbox_mgr = (
            getattr(context.services, "sandbox_manager", None) if context.services else None
        )
        if sandbox_mgr is None:
            logger.error(
                "%sAction refused: no sandbox_manager available in context.services. "
                "Code execution requires a sandbox provider (Docker, E2B, or Monty).",
                language.title(),
            )
            return ActionResult(
                action_name=self.name,
                success=False,
                error=f"{language.title()} execution requires a sandbox provider (not configured)",
            )

        try:
            zone_id = context.zone_id
            user_id = context.variables.get("user_id", "workflow")
            agent_id = context.variables.get("agent_id")

            sandbox = await sandbox_mgr.get_or_create_sandbox(
                name=f"workflow-{context.workflow_id}",
                user_id=user_id,
                zone_id=zone_id,
                agent_id=agent_id,
                ttl_minutes=30,
            )
            sandbox_id = sandbox["sandbox_id"]

            result = await sandbox_mgr.run_code(
                sandbox_id=sandbox_id,
                language=language,
                code=code,
                timeout=timeout,
            )

            if result.exit_code == 0:
                return ActionResult(
                    action_name=self.name,
                    success=True,
                    output={
                        "stdout": result.stdout,
                        **({"stderr": result.stderr} if language == "bash" else {}),
                        **(
                            {"execution_time": result.execution_time}
                            if language == "python"
                            else {}
                        ),
                    },
                )
            else:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    output={"stdout": result.stdout, "stderr": result.stderr},
                    error=result.stderr or f"Exit code: {result.exit_code}",
                )

        except Exception as e:
            logger.error(
                "%sAction '%s' sandbox execution failed: %s",
                language.title(),
                self.name,
                e,
            )
            return ActionResult(
                action_name=self.name,
                success=False,
                error=f"{language.title()} execution failed",
            )


class PythonAction(SandboxedAction):
    """Execute Python code via SandboxManager (Issue #1596).

    Routes all code execution through the SandboxManager brick for
    process-level isolation. Fails closed if no sandbox provider is available.
    """

    async def execute(self, context: WorkflowContext) -> ActionResult:
        code = self.config.get("code", "")
        timeout = self.config.get("timeout", 300)
        logger.debug("PythonAction: code=%d bytes, file_path=%s", len(code), context.file_path)
        return await self._run_in_sandbox(context, "python", code, timeout)


class BashAction(SandboxedAction):
    """Execute shell command via SandboxManager (Issue #1756).

    Routes all command execution through the SandboxManager brick for
    process-level isolation. Fails closed if no sandbox provider is available.
    """

    async def execute(self, context: WorkflowContext) -> ActionResult:
        command = self.safe_interpolate(self.config.get("command", ""), context)
        timeout = self.config.get("timeout", 30)
        return await self._run_in_sandbox(context, "bash", command, timeout)


# Built-in action registry
BUILTIN_ACTIONS = {
    "parse": ParseAction,
    "tag": TagAction,
    "move": MoveAction,
    "metadata": MetadataAction,
    "webhook": WebhookAction,
    "python": PythonAction,
    "bash": BashAction,
}
