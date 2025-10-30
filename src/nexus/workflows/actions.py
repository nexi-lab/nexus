"""Built-in workflow actions."""

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
        """Execute the action.

        Args:
            context: Workflow execution context

        Returns:
            ActionResult with execution status and output
        """
        pass

    def interpolate(self, value: str, context: WorkflowContext) -> str:
        """Interpolate variables in a string value.

        Args:
            value: String with {variable} placeholders
            context: Workflow context with variables

        Returns:
            Interpolated string
        """
        if not isinstance(value, str):
            return value

        # Combine context variables with file metadata
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
        """Execute parse action."""
        try:
            from nexus import connect

            nx = connect()
            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            parser = self.config.get("parser", "auto")

            # Parse the file
            result = await nx.parse(file_path, parser=parser)  # type: ignore[attr-defined]

            return ActionResult(
                action_name=self.name, success=True, output={"parsed_content": result}
            )
        except Exception as e:
            logger.error(f"Parse action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))


class TagAction(BaseAction):
    """Add or remove tags."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        """Execute tag action."""
        try:
            from nexus import connect

            nx = connect()
            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            tags = self.config.get("tags", [])
            remove = self.config.get("remove", False)

            # Interpolate tags
            interpolated_tags = [self.interpolate(tag, context) for tag in tags]

            if remove:
                # Remove tags
                for tag in interpolated_tags:
                    await nx.remove_tag(file_path, tag)  # type: ignore[attr-defined]
            else:
                # Add tags
                for tag in interpolated_tags:
                    await nx.add_tag(file_path, tag)  # type: ignore[attr-defined]

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
        """Execute move action."""
        try:
            from nexus import connect

            nx = connect()
            source = self.interpolate(str(self.config.get("source", context.file_path)), context)
            destination = self.interpolate(self.config["destination"], context)
            create_parents = self.config.get("create_parents", False)

            # Create parent directories if needed
            if create_parents:
                dest_path = Path(destination)
                if not dest_path.parent.exists():
                    nx.mkdir(str(dest_path.parent), parents=True)

            # Move/rename the file
            nx.rename(source, destination)

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
        """Execute metadata action."""
        try:
            from nexus import connect

            nx = connect()
            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            metadata = self.config.get("metadata", {})

            # Interpolate metadata values
            interpolated_metadata = {
                key: self.interpolate(str(value), context) for key, value in metadata.items()
            }

            # Update metadata
            for key, value in interpolated_metadata.items():
                # Use metadata store directly since NexusFilesystem doesn't have set_metadata
                path_rec = nx.metadata.get_path(file_path)  # type: ignore[attr-defined]
                if path_rec:
                    nx.metadata.set_file_metadata(path_rec.path_id, key, value)  # type: ignore[attr-defined]

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
        """Execute LLM action."""
        try:
            from nexus import connect

            nx = connect()
            file_path = self.interpolate(
                str(self.config.get("file_path", context.file_path)), context
            )
            prompt = self.interpolate(str(self.config.get("prompt", "")), context)
            model = self.config.get("model", "claude-sonnet-4")
            output_format = self.config.get("output_format", "text")

            # Read file content if specified
            if file_path:
                content_bytes = nx.read(file_path)
                content = (
                    content_bytes.decode()
                    if isinstance(content_bytes, bytes)
                    else str(content_bytes)
                )
                full_prompt = f"{prompt}\n\nFile content:\n{content}"
            else:
                full_prompt = prompt

            # Execute LLM query
            from nexus.llm import get_provider  # type: ignore[attr-defined]

            provider = get_provider()
            response = await provider.generate(model=model, prompt=full_prompt, system="")

            # Parse output if JSON format requested
            if output_format == "json":
                try:
                    output = json.loads(response)
                except json.JSONDecodeError:
                    output = {"raw": response}
            else:
                output = response

            # Store output in context for subsequent actions
            context.variables[f"{self.name}_output"] = output

            return ActionResult(action_name=self.name, success=True, output=output)
        except Exception as e:
            logger.error(f"LLM action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))


class WebhookAction(BaseAction):
    """Send HTTP webhook."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        """Execute webhook action."""
        try:
            url = self.interpolate(self.config["url"], context)
            method = self.config.get("method", "POST").upper()
            headers = self.config.get("headers", {})
            body = self.config.get("body", {})

            # Interpolate body values
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
            logger.error(f"Webhook action failed: {e}")
            return ActionResult(action_name=self.name, success=False, error=str(e))


class PythonAction(BaseAction):
    """Execute Python code."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        """Execute Python action."""
        import sys
        from io import StringIO

        try:
            code = self.config.get("code", "")
            file_path = context.file_path

            print(f"[PYTHON ACTION DEBUG] Code length: {len(code)} bytes", file=sys.stderr)
            print(f"[PYTHON ACTION DEBUG] First 200 chars of code: {code[:200]}", file=sys.stderr)
            print(f"[PYTHON ACTION DEBUG] file_path: {file_path}", file=sys.stderr)

            # Create execution context
            exec_globals: dict[str, Any] = {
                "context": context,
                "file_path": file_path,
                "variables": context.variables,
            }

            # Capture stdout and stderr
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            captured_stdout = StringIO()
            captured_stderr = StringIO()

            try:
                # Redirect stdout/stderr
                sys.stdout = captured_stdout
                sys.stderr = captured_stderr

                # Add print function that explicitly uses the redirected stdout
                # This ensures print() in exec'd code uses our captured stream
                def _print(*args: Any, **kwargs: Any) -> None:
                    import builtins

                    kwargs.setdefault("file", captured_stdout)
                    builtins.print(*args, **kwargs)

                # Add to exec globals so print uses our captured stream
                exec_globals["print"] = _print

                # Execute code
                try:
                    exec(code, exec_globals)
                except Exception as exec_error:
                    # Capture any errors during code execution
                    import traceback

                    error_msg = f"Error during exec: {exec_error}\n{traceback.format_exc()}"
                    captured_stderr.write(error_msg)
                    print(f"[PYTHON ACTION ERROR] {error_msg}", file=sys.stderr)
            finally:
                # Restore stdout/stderr
                sys.stdout = old_stdout
                sys.stderr = old_stderr

            # Get captured output
            stdout_value = captured_stdout.getvalue()
            stderr_value = captured_stderr.getvalue()

            # Print captured output to stderr so it's visible
            print(
                f"[PYTHON ACTION] stdout length: {len(stdout_value)}, stderr length: {len(stderr_value)}",
                file=sys.stderr,
            )
            if stdout_value:
                print(f"[PYTHON ACTION STDOUT]\n{stdout_value}", file=sys.stderr, end="")
            if stderr_value:
                print(f"[PYTHON ACTION STDERR]\n{stderr_value}", file=sys.stderr, end="")

            # Check if there was an error during execution
            if stderr_value:
                return ActionResult(
                    action_name=self.name,
                    success=False,
                    error=stderr_value,
                )

            # Get result if 'result' variable was set
            result = exec_globals.get("result")

            return ActionResult(
                action_name=self.name,
                success=True,
                output=result,
            )
        except Exception as e:
            import traceback

            full_error = f"Python action failed: {e}\n{traceback.format_exc()}"
            print(f"[PYTHON ACTION EXCEPTION] {full_error}", file=sys.stderr)
            logger.error(full_error)
            return ActionResult(action_name=self.name, success=False, error=str(e))


class BashAction(BaseAction):
    """Execute shell command."""

    async def execute(self, context: WorkflowContext) -> ActionResult:
        """Execute bash action."""
        try:
            command = self.interpolate(self.config.get("command", ""), context)

            # Execute command
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
    "bash": BashAction,
}
