"""Pydantic Monty sandbox provider implementation (Issue #1316).

Implements SandboxProvider interface using Pydantic Monty — a lightweight,
in-process Python interpreter written in Rust. Provides sub-millisecond
startup, deny-by-default security, and iterative execution with host
function callbacks.

Design decisions (see Issue #1316 plan review):
    - #1A: Adapts to existing SandboxProvider ABC (Monty is experimental v0.0.4)
    - #2C: Host functions are pre-built callables passed from SandboxManager
    - #3B: Complete + iterative execution modes (serialization deferred)
    - #4A: Explicit provider selection only (--provider monty)
    - #16B: Resource limits per security profile tier
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nexus.sandbox.sandbox_provider import (
    CodeExecutionResult,
    EscalationNeeded,
    ExecutionTimeoutError,
    SandboxInfo,
    SandboxNotFoundError,
    SandboxProvider,
    SandboxProviderError,
    UnsupportedLanguageError,
    UnsupportedOperationError,
)

logger = logging.getLogger(__name__)

# Lazy import to avoid ImportError when pydantic-monty is not installed
try:
    from pydantic_monty import (
        Monty,
        MontyComplete,
        MontyRuntimeError,
        MontySnapshot,
        MontySyntaxError,
        MontyTypingError,
        ResourceLimits,
    )

    MONTY_AVAILABLE = True
except ImportError:
    MONTY_AVAILABLE = False
    logger.info("pydantic-monty not installed. MontySandboxProvider unavailable.")


# ---------------------------------------------------------------------------
# Resource limit profiles — tied to security profile tiers (Decision #16B)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MontyResourceProfile:
    """Immutable resource limits for a Monty execution context.

    Maps to SandboxSecurityProfile tiers: strict / standard / permissive.
    """

    max_duration_secs: float
    max_memory: int  # bytes
    max_allocations: int
    max_recursion_depth: int
    gc_interval: int = 1000

    def to_resource_limits(self) -> ResourceLimits:
        """Convert to pydantic_monty.ResourceLimits."""
        return ResourceLimits(
            max_duration_secs=self.max_duration_secs,
            max_memory=self.max_memory,
            max_allocations=self.max_allocations,
            max_recursion_depth=self.max_recursion_depth,
            gc_interval=self.gc_interval,
        )


# Pre-built profiles matching SandboxSecurityProfile tiers
MONTY_RESOURCE_PROFILES: dict[str, MontyResourceProfile] = {
    "strict": MontyResourceProfile(
        max_duration_secs=10.0,
        max_memory=10_000_000,  # 10 MB
        max_allocations=100_000,
        max_recursion_depth=100,
    ),
    "standard": MontyResourceProfile(
        max_duration_secs=30.0,
        max_memory=50_000_000,  # 50 MB
        max_allocations=1_000_000,
        max_recursion_depth=200,
    ),
    "permissive": MontyResourceProfile(
        max_duration_secs=120.0,
        max_memory=200_000_000,  # 200 MB
        max_allocations=5_000_000,
        max_recursion_depth=500,
    ),
}

DEFAULT_RESOURCE_PROFILE = MONTY_RESOURCE_PROFILES["standard"]


# ---------------------------------------------------------------------------
# Internal state tracking
# ---------------------------------------------------------------------------


@dataclass
class _MontyInstance:
    """Internal tracking for an active Monty sandbox instance."""

    sandbox_id: str
    created_at: datetime
    status: str  # "active", "stopped"
    resource_profile_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    host_functions: dict[str, Callable[..., Any]] = field(default_factory=dict)
    last_active_at: datetime | None = None


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------


class MontySandboxProvider(SandboxProvider):
    """Pydantic Monty in-process sandbox provider.

    Implements SandboxProvider using Monty — a Rust-based Python subset
    interpreter with sub-microsecond startup, deny-by-default security,
    and iterative execution via host function callbacks.

    Key differences from Docker/E2B:
    - In-process: no container lifecycle, no FUSE mounts
    - Python-only: language must be "python"
    - Host functions: I/O via callable callbacks, not filesystem
    - Type checking: optional pre-execution validation
    - Iterative: start()/resume() for tool-calling agents

    Args:
        resource_profile: Default resource limit profile name.
            One of "strict", "standard", "permissive".
        enable_type_checking: Whether to type-check code before execution.
    """

    SUPPORTED_LANGUAGES = {"python": "monty"}

    def __init__(
        self,
        resource_profile: str = "standard",
        enable_type_checking: bool = True,
    ) -> None:
        if not MONTY_AVAILABLE:
            raise RuntimeError(
                "pydantic-monty is not installed. "
                "Install with: pip install 'nexus-ai-fs[sandbox-monty]'"
            )
        if resource_profile not in MONTY_RESOURCE_PROFILES:
            raise ValueError(
                f"Unknown resource profile: {resource_profile!r}. "
                f"Must be one of: {', '.join(MONTY_RESOURCE_PROFILES)}"
            )
        self._default_profile_name = resource_profile
        self._enable_type_checking = enable_type_checking
        self._instances: dict[str, _MontyInstance] = {}
        logger.info(
            "Monty provider initialized (profile=%s, type_check=%s)",
            resource_profile,
            enable_type_checking,
        )

    # -- SandboxProvider ABC implementation ----------------------------------

    async def create(
        self,
        template_id: str | None = None,
        timeout_minutes: int = 10,
        metadata: dict[str, Any] | None = None,
        security_profile: Any | None = None,
    ) -> str:
        """Create a Monty sandbox instance (in-memory, sub-millisecond).

        The security_profile.name is used to select the resource limit tier.
        template_id is ignored (Monty has no templates).
        """
        _ = template_id, timeout_minutes  # Not applicable to Monty
        sandbox_id = f"monty-{uuid.uuid4().hex[:12]}"

        # Map security profile to resource tier
        profile_name = self._default_profile_name
        if security_profile is not None and hasattr(security_profile, "name"):
            tier = security_profile.name
            if tier in MONTY_RESOURCE_PROFILES:
                profile_name = tier

        now = datetime.now(UTC)
        instance = _MontyInstance(
            sandbox_id=sandbox_id,
            created_at=now,
            status="active",
            resource_profile_name=profile_name,
            metadata=metadata or {},
            last_active_at=now,
        )
        self._instances[sandbox_id] = instance

        logger.info(
            "Created Monty sandbox %s (profile=%s)",
            sandbox_id,
            profile_name,
        )
        return sandbox_id

    async def run_code(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        as_script: bool = False,
    ) -> CodeExecutionResult:
        """Execute code in Monty sandbox (complete mode).

        Captures print() output as stdout. Return value is serialized as
        JSON and appended to stdout. Monty errors map to exit_code != 0.

        Only "python" is supported. All other languages raise
        UnsupportedLanguageError.
        """
        _ = as_script  # Monty always runs as complete script
        instance = self._get_instance(sandbox_id)

        if language not in self.SUPPORTED_LANGUAGES:
            supported = ", ".join(self.SUPPORTED_LANGUAGES)
            raise UnsupportedLanguageError(
                f"Monty only supports Python. Got '{language}'. Supported: {supported}"
            )

        profile = MONTY_RESOURCE_PROFILES[instance.resource_profile_name]
        limits = profile.to_resource_limits()
        # Override timeout if caller specifies a shorter one
        if timeout < profile.max_duration_secs:
            limits = ResourceLimits(
                max_duration_secs=float(timeout),
                max_memory=profile.max_memory,
                max_allocations=profile.max_allocations,
                max_recursion_depth=profile.max_recursion_depth,
                gc_interval=profile.gc_interval,
            )

        # Collect print output
        stdout_parts: list[str] = []

        def _print_callback(_text_type: str, text: str) -> None:
            stdout_parts.append(text)

        start_time = time.monotonic()

        try:
            # Build Monty instance
            ext_fn_names = list(instance.host_functions.keys())
            monty = Monty(
                code=code,
                external_functions=ext_fn_names if ext_fn_names else None,
            )

            # Optional type checking
            if self._enable_type_checking:
                type_stubs = self._build_type_stubs(instance.host_functions)
                if type_stubs:
                    try:
                        monty.type_check(prefix_code=type_stubs)
                    except MontyTypingError as e:
                        elapsed = time.monotonic() - start_time
                        return CodeExecutionResult(
                            stdout="",
                            stderr=f"Type check error:\n{e}",
                            exit_code=2,
                            execution_time=elapsed,
                        )

            # Execute — use iterative mode if host functions exist,
            # complete mode otherwise
            if ext_fn_names:
                output = self._run_iterative(monty, instance, limits, _print_callback)
            else:
                output = monty.run(
                    limits=limits,
                    print_callback=_print_callback,
                )

            elapsed = time.monotonic() - start_time
            instance.last_active_at = datetime.now(UTC)

            # Serialize return value to stdout
            stdout = "".join(stdout_parts)
            if output is not None:
                try:
                    result_str = json.dumps(output, default=str)
                    if stdout and not stdout.endswith("\n"):
                        stdout += "\n"
                    stdout += result_str
                except (TypeError, ValueError):
                    if stdout and not stdout.endswith("\n"):
                        stdout += "\n"
                    stdout += repr(output)

            return CodeExecutionResult(
                stdout=stdout,
                stderr="",
                exit_code=0,
                execution_time=elapsed,
            )

        except MontySyntaxError as e:
            elapsed = time.monotonic() - start_time
            return CodeExecutionResult(
                stdout="".join(stdout_parts),
                stderr=f"Syntax error:\n{e}",
                exit_code=1,
                execution_time=elapsed,
            )

        except MontyRuntimeError as e:
            elapsed = time.monotonic() - start_time
            stderr = str(e)
            # Distinguish timeout from other runtime errors
            if "time limit" in stderr.lower() or "timeout" in stderr.lower():
                raise ExecutionTimeoutError(f"Code execution timed out after {elapsed:.1f}s") from e
            # Detect import/module errors that signal need for escalation
            if self._is_escalation_error(stderr):
                raise EscalationNeeded(
                    reason=f"Monty cannot handle: {stderr[:200]}",
                    suggested_tier="docker",
                ) from e
            return CodeExecutionResult(
                stdout="".join(stdout_parts),
                stderr=f"Runtime error:\n{stderr}",
                exit_code=1,
                execution_time=elapsed,
            )

        except Exception as e:
            elapsed = time.monotonic() - start_time
            logger.error(
                "Unexpected error in Monty sandbox %s: %s",
                sandbox_id,
                e,
                exc_info=True,
            )
            return CodeExecutionResult(
                stdout="".join(stdout_parts),
                stderr=f"Internal error: {e}",
                exit_code=127,
                execution_time=elapsed,
            )

    async def pause(self, sandbox_id: str) -> None:
        """Not supported — Monty sandboxes are ephemeral."""
        self._get_instance(sandbox_id)  # Validate exists
        raise UnsupportedOperationError(
            "Monty sandboxes do not support pause. Use snapshot serialization for durable state."
        )

    async def resume(self, sandbox_id: str) -> None:
        """Not supported — Monty sandboxes are ephemeral."""
        self._get_instance(sandbox_id)  # Validate exists
        raise UnsupportedOperationError(
            "Monty sandboxes do not support resume. Use snapshot deserialization to restore state."
        )

    async def destroy(self, sandbox_id: str) -> None:
        """Destroy Monty sandbox instance (remove from memory)."""
        instance = self._instances.pop(sandbox_id, None)
        if instance is None:
            raise SandboxNotFoundError(f"Monty sandbox {sandbox_id} not found")
        logger.info("Destroyed Monty sandbox %s", sandbox_id)

    async def get_info(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox information from in-memory state."""
        instance = self._get_instance(sandbox_id)
        return SandboxInfo(
            sandbox_id=instance.sandbox_id,
            status=instance.status,
            created_at=instance.created_at,
            provider="monty",
            metadata={
                "resource_profile": instance.resource_profile_name,
                "type_checking": self._enable_type_checking,
                **instance.metadata,
            },
        )

    async def is_available(self) -> bool:
        """Check if Monty runtime is available."""
        return MONTY_AVAILABLE

    async def mount_nexus(
        self,
        sandbox_id: str,
        mount_path: str,
        nexus_url: str,
        api_key: str,
        agent_id: str | None = None,
        skip_dependency_checks: bool = False,
    ) -> dict[str, Any]:
        """Not supported — Monty uses host functions instead of FUSE mounts."""
        _ = mount_path, nexus_url, api_key, agent_id, skip_dependency_checks
        self._get_instance(sandbox_id)  # Validate exists
        raise UnsupportedOperationError(
            "Monty sandboxes do not support FUSE mounts. "
            "Use host functions (read_file, write_file) for VFS access."
        )

    # -- Monty-specific public methods ---------------------------------------

    def set_host_functions(
        self,
        sandbox_id: str,
        host_functions: dict[str, Callable[..., Any]],
    ) -> None:
        """Register host function callbacks for a sandbox.

        Host functions are called when sandboxed code invokes external
        functions. They are the bridge between Monty's isolated interpreter
        and Nexus VFS/services.

        Args:
            sandbox_id: Sandbox ID.
            host_functions: Mapping of function name → callable.
                Callables receive positional and keyword args from
                the sandboxed code. They must validate all inputs
                (sandboxed code controls the arguments).

        Raises:
            SandboxNotFoundError: If sandbox doesn't exist.
        """
        instance = self._get_instance(sandbox_id)
        # Validate function names are valid Python identifiers
        for name in host_functions:
            if not name.isidentifier():
                raise ValueError(
                    f"Invalid host function name: {name!r}. Must be a valid Python identifier."
                )
        instance.host_functions = dict(host_functions)
        logger.debug(
            "Set %d host functions for sandbox %s: %s",
            len(host_functions),
            sandbox_id,
            list(host_functions.keys()),
        )

    def get_resource_profile(self, sandbox_id: str) -> MontyResourceProfile:
        """Get the resource limit profile for a sandbox."""
        instance = self._get_instance(sandbox_id)
        return MONTY_RESOURCE_PROFILES[instance.resource_profile_name]

    # -- Internal helpers ----------------------------------------------------

    def _get_instance(self, sandbox_id: str) -> _MontyInstance:
        """Get an active sandbox instance or raise SandboxNotFoundError."""
        instance = self._instances.get(sandbox_id)
        if instance is None:
            raise SandboxNotFoundError(f"Monty sandbox {sandbox_id} not found")
        if instance.status == "stopped":
            raise SandboxNotFoundError(f"Monty sandbox {sandbox_id} has been destroyed")
        return instance

    # Upper bound on iterative execution steps to prevent infinite loops.
    # A typical agent tool-call chain is <100 steps; 10 000 is extremely generous.
    _MAX_ITERATIONS: int = 10_000

    def _run_iterative(
        self,
        monty: Monty,
        instance: _MontyInstance,
        limits: ResourceLimits,
        print_callback: Callable[[str, str], None],
    ) -> Any:
        """Run Monty in iterative mode with host function dispatch.

        Uses start()/resume() to pause at each external function call,
        dispatch to the registered host function, and resume with the
        return value.

        Args:
            monty: Compiled Monty instance.
            instance: Sandbox instance with host functions.
            limits: Resource limits.
            print_callback: Callback for print() output.

        Returns:
            The final output value from the Monty execution.

        Raises:
            SandboxProviderError: If iteration count exceeds _MAX_ITERATIONS.
        """
        progress = monty.start(limits=limits, print_callback=print_callback)

        iterations = 0
        while not isinstance(progress, MontyComplete):
            iterations += 1
            if iterations > self._MAX_ITERATIONS:
                raise SandboxProviderError(
                    f"Iterative execution exceeded {self._MAX_ITERATIONS} steps. "
                    f"Possible infinite host-function loop in sandbox "
                    f"{instance.sandbox_id}."
                )
            if isinstance(progress, MontySnapshot):
                fn_name = progress.function_name
                fn_args = progress.args
                fn_kwargs = progress.kwargs

                handler = instance.host_functions.get(fn_name)
                if handler is None:
                    # Unknown function — resume with NameError
                    progress = progress.resume(
                        exception=NameError(f"Host function '{fn_name}' is not registered")
                    )
                    continue

                try:
                    result = handler(*fn_args, **fn_kwargs)
                    progress = progress.resume(return_value=result)
                except Exception as exc:
                    # Propagate host function errors back to Monty code
                    progress = progress.resume(exception=exc)
            else:
                # Unexpected state — should not happen in non-async mode
                raise SandboxProviderError(
                    f"Unexpected Monty execution state: {type(progress).__name__}"
                )

        return progress.output

    # Patterns in runtime error messages that indicate escalation is needed.
    # These are specifically for module/import errors and missing capabilities —
    # not generic NameErrors or TypeErrors which are genuine code bugs.
    _ESCALATION_PATTERNS = (
        "modulenotfounderror",
        "no module named",
        "importerror",
        "cannot import name",
        "filenotfounderror",
        "permissionerror: [errno",
    )

    # Pattern for module attribute errors — e.g., Monty has an `os` stub but
    # `os.getcwd()` is not implemented. This means the code needs real OS access.
    _MODULE_ATTR_PATTERN = "module '"

    def _is_escalation_error(self, stderr: str) -> bool:
        """Check if a runtime error indicates need for escalation.

        Returns True for:
        - Missing module imports (ModuleNotFoundError, ImportError)
        - Missing module attributes (AttributeError: module 'X' has no attribute 'Y')
        - Filesystem access errors (FileNotFoundError, PermissionError)

        Returns False for genuine code bugs (NameError, ZeroDivision, TypeError, etc.).
        """
        lower = stderr.lower()
        if any(pattern in lower for pattern in self._ESCALATION_PATTERNS):
            return True
        # Detect "AttributeError: module 'os' has no attribute 'getcwd'" etc.
        return "attributeerror" in lower and self._MODULE_ATTR_PATTERN in stderr

    @staticmethod
    def _build_type_stubs(
        host_functions: dict[str, Callable[..., Any]],
    ) -> str:
        """Build type check stubs for host functions.

        Generates stub declarations so Monty's type checker (ty) can
        validate calls to external functions.

        Returns:
            Type stub prefix code, or empty string if no functions.
        """
        if not host_functions:
            return ""

        parts = ["from typing import Any"]
        for fn_name in host_functions:
            # Generic stubs — host functions accept Any and return Any.
            # More specific stubs could be provided per-function if needed.
            parts.append(f"def {fn_name}(*args: Any, **kwargs: Any) -> Any:\n    ...")
        return "\n\n".join(parts)
