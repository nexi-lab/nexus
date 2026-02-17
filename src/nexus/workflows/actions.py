"""Built-in workflow actions.

Zero imports from nexus.core or nexus.llm — all Nexus operations are accessed
via ``context.services`` (a WorkflowServices dataclass injected by the engine).
"""

import json
import logging
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import aiohttp

from nexus.workflows.types import ActionResult, WorkflowContext

logger = logging.getLogger(__name__)

class BaseAction(ABC):
    """Base class for workflow actions."""

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    @abstractmethod
    async def execute(self, context: WorkflowContext) -> ActionResult:
        """Execute the action."""
        pass

    def interpolate(self, value: str, context: WorkflowContext) -> str:
        """Interpolate variables in a string value."""
        if not isinstance(value, str):
            return value

        variables = {**context.variables}
        if context.file_path:
            variables["file_path"] = context.file_path
            variables["filename"] = Path(context.file_path).name
            variables["dirname"] = Path(context.file_path).parent.as_posix()
        if context.file_metadata:
            variables.update(context.file_metadata)

        try:
            return value.format(**variables)
        except KeyError as e:
            logger.warning(f"Variable {e} not found in context")
            return value

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

            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            parser = self.config.get("parser", "auto")

            result = await context.services.nexus_ops.parse(file_path, parser=parser)

            return ActionResult(
                action_name=self.name, success=True, output={"parsed_content": result}
            )
        except Exception as e:
            logger.error(f"Parse action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))

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

            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            tags = self.config.get("tags", [])
            remove = self.config.get("remove", False)

            interpolated_tags = [self.interpolate(tag, context) for tag in tags]

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
            logger.error(f"Tag action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))

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

            source = self.interpolate(str(self.config.get("source", context.file_path)), context)
            destination = self.interpolate(self.config["destination"], context)
            create_parents = self.config.get("create_parents", False)

            if create_parents:
                dest_path = Path(destination)
                if not dest_path.parent.exists():
                    context.services.nexus_ops.mkdir(str(dest_path.parent), parents=True)

            context.services.nexus_ops.rename(source, destination)

            return ActionResult(
                action_name=self.name,
                success=True,
                output={"source": source, "destination": destination},
            )
        except Exception as e:
            logger.error(f"Move action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))

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

            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            metadata = self.config.get("metadata", {})

            interpolated_metadata = {
                key: self.interpolate(str(value), context) for key, value in metadata.items()
            }

            for key, value in interpolated_metadata.items():
                path_rec = context.services.metadata_store.get_path(file_path)
                if path_rec:
                    context.services.metadata_store.set_file_metadata(path_rec.path_id, key, value)

            return ActionResult(
                action_name=self.name,
                success=True,
                output={"metadata": interpolated_metadata},
            )
        except Exception as e:
            logger.error(f"Metadata action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))

class LLMAction(BaseAction):
    """Execute LLM-powered action."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            if not context.services or not context.services.llm_provider:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error="llm_provider service not injected",
                )

            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            prompt = self.interpolate(str(self.config.get("prompt", "")), context)
            model = self.config.get("model", "claude-sonnet-4")
            output_format = self.config.get("output_format", "text")

            # Read file content if specified
            if file_path and context.services.nexus_ops:
                content_bytes = context.services.nexus_ops.read(file_path)
                content = (
                    content_bytes.decode()
                    if isinstance(content_bytes, bytes)
                    else str(content_bytes)
                )
                full_prompt = f"{prompt}\n\nFile content:\n{content}"
            else:
                full_prompt = prompt

            response = await context.services.llm_provider.generate(
                model=model, prompt=full_prompt, system=""
            )

            if output_format == "json":
                try:
                    output = json.loads(response)
                except json.JSONDecodeError:
                    output = {"raw": response}
            else:
                output = response

            context.variables[f"{self.name}_output"] = output

            return ActionResult(action_name=self.name, success=True, output=output)
        except Exception as e:
            logger.error(f"LLM action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))

class WebhookAction(BaseAction):
    """Send HTTP webhook."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            url = self.interpolate(self.config["url"], context)

            # SSRF protection: block private/internal IPs (Issue #1596)
            from nexus.server.security.url_validator import validate_outbound_url

            try:
                validate_outbound_url(url)
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

            interpolated_body = {
                key: self.interpolate(str(value), context) for key, value in body.items()
            }

            async with (
                aiohttp.ClientSession() as session,
                session.request(method, url, json=interpolated_body, headers=headers) as response,
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

class PythonAction(BaseAction):
    """Execute Python code via SandboxManager (Issue #1596).

    Routes all code execution through the SandboxManager brick for
    process-level isolation. Fails closed if no sandbox provider is available.
    """

    async def execute(self, context: WorkflowContext) -> ActionResult:
        code = self.config.get("code", "")
        timeout = self.config.get("timeout", 300)

        logger.debug("PythonAction: code=%d bytes, file_path=%s", len(code), context.file_path)

        # Fail closed: require SandboxManager (Issue #1596 — no bare exec())
        sandbox_mgr = (
            getattr(context.services, "sandbox_manager", None) if context.services else None
        )
        if sandbox_mgr is None:
            logger.error(
                "PythonAction refused: no sandbox_manager available in context.services. "
                "Code execution requires a sandbox provider (Docker, E2B, or Monty)."
            )
            return ActionResult(
                action_name=self.name,
                success=False,
                error="Python code execution requires a sandbox provider (not configured)",
            )

        try:
            # Get or create a sandbox scoped to this workflow execution
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

            # Execute code in isolated sandbox
            result = await sandbox_mgr.run_code(
                sandbox_id=sandbox_id,
                language="python",
                code=code,
                timeout=timeout,
            )

            if result.exit_code == 0:
                return ActionResult(
                    action_name=self.name,
                    success=True,
                    output={
                        "stdout": result.stdout,
                        "execution_time": result.execution_time,
                    },
                )
            else:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error=result.stderr or f"Exit code: {result.exit_code}",
                )

        except Exception as e:
            logger.error("PythonAction '%s' sandbox execution failed: %s", self.name, e)
            return ActionResult(
                action_name=self.name,
                success=False,
                error="Python code execution failed",
            )

class BashAction(BaseAction):
    """Execute shell command."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        try:
            command = self.interpolate(self.config.get("command", ""), context)

            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)

            return ActionResult(
                action_name=self.name,
                success=result.returncode == 0,
                output={"stdout": result.stdout, "stderr": result.stderr},
                error=result.stderr if result.returncode != 0 else None,
            )
        except Exception as e:
            logger.error(f"Bash action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))

# Built-in action registry
BUILTIN_ACTIONS = {
    "parse": ParseAction,
    "tag": TagAction,
    "move": MoveAction,
    "metadata": MetadataAction,
    "llm": LLMAction,
    "webhook": WebhookAction,
    "python": PythonAction,
    "bash": "BashAction",
}
