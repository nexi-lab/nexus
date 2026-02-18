"""NexusREPL — RLM execution environment wrapping Nexus SandboxManager.

Implements the BaseEnv pattern from the rlm library: setup(), load_context(),
execute_code(), cleanup(). Bridges the sync BaseEnv interface with Nexus's
async SandboxManager by using asyncio.run() inside the thread pool executor.

Architecture Decisions:
  - Issue 2B: run_in_executor (NexusREPL is sync, runs in thread pool)
  - Issue 5A: API tools only (no FUSE mount)
  - Issue 6A: HTTP to Nexus API (tools call REST)
  - Issue 14A: Per-request sandbox (no pooling)
"""


import asyncio
import logging
from typing import Any

from nexus.rlm.tools import build_tools_injection_code
from nexus.rlm.types import REPLResult, RLMInfrastructureError

logger = logging.getLogger(__name__)

# Maximum output chars shown to the model per REPL execution
_MAX_OUTPUT_CHARS = 20_000


class NexusREPL:
    """REPL environment that wraps Nexus SandboxManager.

    Each NexusREPL instance manages a single sandbox. The sandbox is created
    on setup() and destroyed on cleanup(). Code is executed via
    SandboxManager.run_code() with stateful Jupyter kernel (as_script=False).

    This class is intentionally synchronous — it runs inside a thread pool
    executor via run_in_executor(). asyncio.run() is safe in this context
    because no pre-existing event loop exists in the thread.
    """

    def __init__(
        self,
        sandbox_manager: Any,
        user_id: str,
        zone_id: str,
        nexus_api_url: str,
        nexus_api_key: str,
        sandbox_provider: str | None = None,
        sandbox_timeout: int = 300,
    ) -> None:
        self._sandbox_manager = sandbox_manager
        self._user_id = user_id
        self._zone_id = zone_id
        self._nexus_api_url = nexus_api_url
        self._nexus_api_key = nexus_api_key
        self._sandbox_provider = sandbox_provider
        self._sandbox_timeout = sandbox_timeout
        self._sandbox_id: str | None = None

    @property
    def sandbox_id(self) -> str | None:
        """The active sandbox ID, or None if not set up."""
        return self._sandbox_id

    def setup(self) -> None:
        """Create sandbox and inject Nexus tools.

        Creates a new sandbox via SandboxManager, then injects the
        pre-loaded tool functions (nexus_read, nexus_search, etc.)
        into the sandbox's REPL environment.

        Raises:
            RLMInfrastructureError: If sandbox creation fails.
        """
        try:
            result = asyncio.run(
                self._sandbox_manager.create_sandbox(
                    name=f"rlm-{self._user_id[:8]}",
                    user_id=self._user_id,
                    zone_id=self._zone_id,
                    provider=self._sandbox_provider,
                    ttl_minutes=15,
                )
            )
            self._sandbox_id = result["sandbox_id"]
            logger.info(
                "RLM sandbox created: %s (provider: %s)", self._sandbox_id, result.get("provider")
            )
        except Exception as exc:
            raise RLMInfrastructureError(f"Failed to create sandbox: {exc}") from exc

        # Inject Nexus tools into the REPL
        tools_code = build_tools_injection_code(
            api_url=self._nexus_api_url,
            api_key=self._nexus_api_key,
            zone_id=self._zone_id,
        )
        try:
            asyncio.run(
                self._sandbox_manager.run_code(
                    sandbox_id=self._sandbox_id,
                    language="python",
                    code=tools_code,
                    timeout=30,
                )
            )
        except Exception as exc:
            logger.warning("Failed to inject tools into sandbox: %s", exc)
            # Non-fatal — tools may not be available but sandbox is usable

    def load_context(self, context_payload: dict | list | str) -> None:
        """Load context metadata into the sandbox.

        Following the RLM paradigm (Decision 15A), we only pass metadata
        (query + paths) — NOT the actual file content. The model uses
        nexus_read() and nexus_search() to lazily fetch what it needs.

        Args:
            context_payload: Context metadata — string, dict, or list.

        Raises:
            RLMInfrastructureError: If sandbox is not set up.
        """
        if self._sandbox_id is None:
            raise RLMInfrastructureError("Cannot load context: sandbox not set up")

        # Serialize context to a variable in the sandbox
        if isinstance(context_payload, str):
            code = (
                f"context = {context_payload!r}\nprint(f'Context loaded: {{len(context)}} chars')"
            )
        else:
            import json

            serialized = json.dumps(context_payload, default=str)
            code = f"import json\ncontext = json.loads({serialized!r})\nprint(f'Context loaded: {{type(context).__name__}}')"

        try:
            asyncio.run(
                self._sandbox_manager.run_code(
                    sandbox_id=self._sandbox_id,
                    language="python",
                    code=code,
                    timeout=30,
                )
            )
        except Exception as exc:
            logger.warning("Failed to load context: %s", exc)

    def execute_code(self, code: str) -> REPLResult:
        """Execute code in the sandbox and return the result.

        Translates SandboxManager's CodeExecutionResult to REPLResult.
        Output is truncated to _MAX_OUTPUT_CHARS to match the RLM paper's
        design (force model to use programmatic access instead of dumping).

        Args:
            code: Python code to execute.

        Returns:
            REPLResult with stdout, stderr, execution_time, exit_code.

        Raises:
            RLMInfrastructureError: If sandbox is not set up.
        """
        if self._sandbox_id is None:
            raise RLMInfrastructureError("Cannot execute code: sandbox not set up")

        try:
            result = asyncio.run(
                self._sandbox_manager.run_code(
                    sandbox_id=self._sandbox_id,
                    language="python",
                    code=code,
                    timeout=self._sandbox_timeout,
                    as_script=False,  # Stateful Jupyter kernel
                )
            )
        except Exception as exc:
            return REPLResult(
                stdout="",
                stderr=f"Execution error: {exc}",
                execution_time=0.0,
                exit_code=1,
            )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # Truncate long output (RLM paper: 8192 default, we use 20K)
        if len(stdout) > _MAX_OUTPUT_CHARS:
            stdout = (
                stdout[:_MAX_OUTPUT_CHARS]
                + f"\n... [output truncated, {len(result.stdout)} total chars]"
            )

        return REPLResult(
            stdout=stdout,
            stderr=stderr,
            execution_time=getattr(result, "execution_time", 0.0),
            exit_code=getattr(result, "exit_code", 0),
        )

    def cleanup(self) -> None:
        """Stop and destroy the sandbox.

        Safe to call multiple times or before setup().
        """
        if self._sandbox_id is None:
            return

        try:
            asyncio.run(self._sandbox_manager.stop_sandbox(self._sandbox_id))
            logger.info("RLM sandbox stopped: %s", self._sandbox_id)
        except Exception as exc:
            logger.warning("Failed to stop sandbox %s: %s", self._sandbox_id, exc)
        finally:
            self._sandbox_id = None
