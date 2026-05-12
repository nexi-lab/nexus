from nexus.contracts.protocols.activity import EventKind


def test_op_kind_present():
    assert EventKind.OP.value == "op"


def test_exec_kind_present():
    assert EventKind.EXEC.value == "exec"


def test_existing_kinds_unchanged():
    assert EventKind.SEARCH.value == "search"
    assert EventKind.FETCH.value == "fetch"
    assert EventKind.MCP_TOOL_CALL.value == "mcp_tool_call"
