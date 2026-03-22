"""CLI behavioral contract suite for compliance testing.

Defines a set of behavioral contracts that a CLI tool must satisfy for
Nexus to interact with it correctly. The contract suite runs against both
fake CLI scripts (in CI) and real CLIs (optional integration test) to
catch behavioral drift.

Design decisions (Issue #3148):
    - Compliance suite runs against both fake and real (9A)
    - Contracts are declarative: input → expected output pattern + exit code
    - Self-documenting: the suite IS the behavioral spec for CLI integration
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContractCase:
    """A single behavioral contract for a CLI command.

    Defines what Nexus expects when invoking a CLI with specific args.
    The contract is verified against both fake and real CLI implementations.

    Attributes:
        name: Human-readable contract name.
        command: CLI command args (e.g., ["gmail", "messages.list"]).
        stdin: Optional stdin input (e.g., auth token).
        env: Optional environment variables.
        expected_exit_code: Expected exit code (0 for success).
        expected_stdout_pattern: Regex pattern stdout must match.
        expected_stderr_pattern: Regex pattern stderr must match (if any).
        timeout_seconds: Maximum execution time.
        description: What this contract verifies.
    """

    name: str
    command: list[str]
    stdin: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    expected_exit_code: int = 0
    expected_stdout_pattern: str | None = None
    expected_stderr_pattern: str | None = None
    timeout_seconds: float = 30.0
    description: str = ""

    def verify_exit_code(self, actual: int) -> bool:
        """Check if actual exit code matches expected."""
        return actual == self.expected_exit_code

    def verify_stdout(self, actual: str) -> bool:
        """Check if actual stdout matches expected pattern."""
        if self.expected_stdout_pattern is None:
            return True
        return bool(re.search(self.expected_stdout_pattern, actual, re.DOTALL))

    def verify_stderr(self, actual: str) -> bool:
        """Check if actual stderr matches expected pattern."""
        if self.expected_stderr_pattern is None:
            return True
        return bool(re.search(self.expected_stderr_pattern, actual, re.DOTALL))


@dataclass(frozen=True)
class ContractResult:
    """Result of running a contract case against a CLI."""

    case: ContractCase
    passed: bool
    actual_exit_code: int | None = None
    actual_stdout: str = ""
    actual_stderr: str = ""
    error: str | None = None
    duration_ms: float = 0.0

    def summary(self) -> str:
        """One-line summary for test output."""
        status = "PASS" if self.passed else "FAIL"
        msg = f"[{status}] {self.case.name}"
        if not self.passed and self.error:
            msg += f": {self.error}"
        return msg


class CLIContractSuite:
    """Collection of behavioral contracts for a CLI tool.

    Register contracts, then run them against any CLI executor (fake or real).
    The suite collects results and reports compliance.

    Example::

        suite = CLIContractSuite("gws")
        suite.add(ContractCase(
            name="list_messages",
            command=["gmail", "messages.list", "--limit", "1"],
            expected_exit_code=0,
            expected_stdout_pattern=r'"messages":\\s*\\[',
        ))

        results = await suite.run(executor)
        assert suite.all_passed(results)
    """

    def __init__(self, cli_name: str) -> None:
        self.cli_name = cli_name
        self._cases: list[ContractCase] = []

    def add(self, case: ContractCase) -> None:
        """Register a contract case."""
        self._cases.append(case)

    def add_all(self, cases: list[ContractCase]) -> None:
        """Register multiple contract cases."""
        self._cases.extend(cases)

    @property
    def cases(self) -> list[ContractCase]:
        """All registered contract cases."""
        return list(self._cases)

    def verify(
        self,
        case: ContractCase,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration_ms: float = 0.0,
    ) -> ContractResult:
        """Verify a single contract case against actual CLI output.

        Args:
            case: The contract to verify.
            exit_code: Actual exit code.
            stdout: Actual stdout.
            stderr: Actual stderr.
            duration_ms: Actual execution time.

        Returns:
            ContractResult with pass/fail status and details.
        """
        errors: list[str] = []

        if not case.verify_exit_code(exit_code):
            errors.append(f"exit code: expected {case.expected_exit_code}, got {exit_code}")

        if not case.verify_stdout(stdout):
            errors.append(f"stdout: did not match pattern {case.expected_stdout_pattern!r}")

        if not case.verify_stderr(stderr):
            errors.append(f"stderr: did not match pattern {case.expected_stderr_pattern!r}")

        return ContractResult(
            case=case,
            passed=len(errors) == 0,
            actual_exit_code=exit_code,
            actual_stdout=stdout,
            actual_stderr=stderr,
            error="; ".join(errors) if errors else None,
            duration_ms=duration_ms,
        )

    @staticmethod
    def all_passed(results: list[ContractResult]) -> bool:
        """Check if all contract cases passed."""
        return all(r.passed for r in results)

    @staticmethod
    def summary(results: list[ContractResult]) -> str:
        """Multi-line summary of all results."""
        lines = []
        passed = sum(1 for r in results if r.passed)
        total = len(results)
        lines.append(f"{passed}/{total} contracts passed")
        for r in results:
            lines.append(f"  {r.summary()}")
        return "\n".join(lines)

    def to_dict(self) -> list[dict[str, Any]]:
        """Serialize all contracts for documentation/export."""
        return [
            {
                "name": c.name,
                "command": c.command,
                "expected_exit_code": c.expected_exit_code,
                "expected_stdout_pattern": c.expected_stdout_pattern,
                "description": c.description,
            }
            for c in self._cases
        ]
