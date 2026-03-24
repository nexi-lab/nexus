"""Tests for CLI behavioral contract suite (Issue #3148, Decision #9A).

Tests the contract framework itself — not a specific CLI's contracts.
Verifies that the compliance suite correctly evaluates pass/fail for
exit codes, stdout patterns, and stderr patterns.
"""

from nexus.backends.connectors.cli.contract import (
    CLIContractSuite,
    ContractCase,
    ContractResult,
)

# ---------------------------------------------------------------------------
# ContractCase verification
# ---------------------------------------------------------------------------


class TestContractCase:
    def test_verify_exit_code_success(self) -> None:
        case = ContractCase(name="test", command=["echo"], expected_exit_code=0)
        assert case.verify_exit_code(0) is True
        assert case.verify_exit_code(1) is False

    def test_verify_exit_code_failure(self) -> None:
        case = ContractCase(name="test", command=["false"], expected_exit_code=1)
        assert case.verify_exit_code(1) is True
        assert case.verify_exit_code(0) is False

    def test_verify_stdout_with_pattern(self) -> None:
        case = ContractCase(
            name="test",
            command=["echo"],
            expected_stdout_pattern=r'"messages":\s*\[',
        )
        assert case.verify_stdout('{"messages": []}') is True
        assert case.verify_stdout("no json here") is False

    def test_verify_stdout_no_pattern(self) -> None:
        """When no pattern is set, any stdout passes."""
        case = ContractCase(name="test", command=["echo"])
        assert case.verify_stdout("anything") is True
        assert case.verify_stdout("") is True

    def test_verify_stderr_with_pattern(self) -> None:
        case = ContractCase(
            name="test",
            command=["echo"],
            expected_stderr_pattern=r"error|warning",
        )
        assert case.verify_stderr("something went wrong: error") is True
        assert case.verify_stderr("all good") is False

    def test_verify_stderr_no_pattern(self) -> None:
        case = ContractCase(name="test", command=["echo"])
        assert case.verify_stderr("") is True
        assert case.verify_stderr("anything") is True

    def test_multiline_stdout_pattern(self) -> None:
        case = ContractCase(
            name="test",
            command=["echo"],
            expected_stdout_pattern=r"line1.*line2",
        )
        assert case.verify_stdout("line1\nline2") is True


# ---------------------------------------------------------------------------
# CLIContractSuite
# ---------------------------------------------------------------------------


class TestCLIContractSuite:
    def test_add_and_list_cases(self) -> None:
        suite = CLIContractSuite("gws")
        case1 = ContractCase(name="list", command=["gmail", "messages.list"])
        case2 = ContractCase(name="get", command=["gmail", "messages.get", "123"])
        suite.add(case1)
        suite.add(case2)
        assert len(suite.cases) == 2
        assert suite.cli_name == "gws"

    def test_add_all(self) -> None:
        suite = CLIContractSuite("gh")
        cases = [
            ContractCase(name="list-issues", command=["issue", "list"]),
            ContractCase(name="create-issue", command=["issue", "create"]),
        ]
        suite.add_all(cases)
        assert len(suite.cases) == 2

    def test_verify_passing_case(self) -> None:
        suite = CLIContractSuite("gws")
        case = ContractCase(
            name="list",
            command=["gmail", "messages.list"],
            expected_exit_code=0,
            expected_stdout_pattern=r'"messages"',
        )

        result = suite.verify(case, exit_code=0, stdout='{"messages": []}', stderr="")
        assert result.passed is True
        assert result.error is None
        assert "PASS" in result.summary()

    def test_verify_failing_exit_code(self) -> None:
        suite = CLIContractSuite("gws")
        case = ContractCase(
            name="list",
            command=["gmail", "messages.list"],
            expected_exit_code=0,
        )

        result = suite.verify(case, exit_code=1, stdout="", stderr="error")
        assert result.passed is False
        assert "exit code" in result.error
        assert "FAIL" in result.summary()

    def test_verify_failing_stdout(self) -> None:
        suite = CLIContractSuite("gws")
        case = ContractCase(
            name="list",
            command=["gmail", "messages.list"],
            expected_stdout_pattern=r'"messages"',
        )

        result = suite.verify(case, exit_code=0, stdout="not json", stderr="")
        assert result.passed is False
        assert "stdout" in result.error

    def test_verify_failing_stderr(self) -> None:
        suite = CLIContractSuite("gws")
        case = ContractCase(
            name="list",
            command=["gmail", "messages.list"],
            expected_stderr_pattern=r"^$",  # Expect empty stderr
        )

        result = suite.verify(case, exit_code=0, stdout="ok", stderr="warning: something")
        assert result.passed is False
        assert "stderr" in result.error

    def test_verify_multiple_failures(self) -> None:
        suite = CLIContractSuite("gws")
        case = ContractCase(
            name="list",
            command=["gmail", "messages.list"],
            expected_exit_code=0,
            expected_stdout_pattern=r'"messages"',
        )

        result = suite.verify(case, exit_code=1, stdout="bad", stderr="")
        assert result.passed is False
        assert "exit code" in result.error
        assert "stdout" in result.error

    def test_all_passed_true(self) -> None:
        results = [
            ContractResult(case=ContractCase(name="a", command=[]), passed=True),
            ContractResult(case=ContractCase(name="b", command=[]), passed=True),
        ]
        assert CLIContractSuite.all_passed(results) is True

    def test_all_passed_false(self) -> None:
        results = [
            ContractResult(case=ContractCase(name="a", command=[]), passed=True),
            ContractResult(case=ContractCase(name="b", command=[]), passed=False, error="fail"),
        ]
        assert CLIContractSuite.all_passed(results) is False

    def test_all_passed_empty(self) -> None:
        assert CLIContractSuite.all_passed([]) is True

    def test_summary_format(self) -> None:
        results = [
            ContractResult(case=ContractCase(name="pass-test", command=[]), passed=True),
            ContractResult(
                case=ContractCase(name="fail-test", command=[]),
                passed=False,
                error="bad exit code",
            ),
        ]
        summary = CLIContractSuite.summary(results)
        assert "1/2 contracts passed" in summary
        assert "PASS" in summary
        assert "FAIL" in summary

    def test_to_dict_serialization(self) -> None:
        suite = CLIContractSuite("gws")
        suite.add(
            ContractCase(
                name="list",
                command=["gmail", "messages.list"],
                expected_exit_code=0,
                expected_stdout_pattern=r'"messages"',
                description="List messages returns JSON array",
            )
        )
        exported = suite.to_dict()
        assert len(exported) == 1
        assert exported[0]["name"] == "list"
        assert exported[0]["description"] == "List messages returns JSON array"
