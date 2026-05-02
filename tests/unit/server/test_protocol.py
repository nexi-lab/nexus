"""Unit tests for RPC protocol."""

import dataclasses
from datetime import datetime
from typing import cast

import pytest

from nexus.lib.rpc_codec import RPCEncoder, decode_rpc_message, encode_rpc_message
from nexus.server.protocol import (
    METHOD_PARAMS,
    RPCErrorCode,
    RPCRequest,
    RPCResponse,
    parse_method_params,
)


class TestRPCRequest:
    """Tests for RPCRequest class."""

    def test_from_dict(self):
        """Test creating RPCRequest from dict."""
        data = {
            "jsonrpc": "2.0",
            "id": "test-123",
            "method": "sys_read",
            "params": {"path": "/test.txt"},
        }
        request = RPCRequest.from_dict(data)
        assert request.jsonrpc == "2.0"
        assert request.id == "test-123"
        assert request.method == "sys_read"
        assert request.params == {"path": "/test.txt"}

    def test_to_dict(self):
        """Test converting RPCRequest to dict."""
        request = RPCRequest(
            jsonrpc="2.0", id="test-456", method="sys_write", params={"path": "/file.txt"}
        )
        result = request.to_dict()
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "test-456"
        assert result["method"] == "sys_write"
        assert result["params"] == {"path": "/file.txt"}


class TestRPCResponse:
    """Tests for RPCResponse class."""

    def test_success_response(self):
        """Test creating success response."""
        response = RPCResponse.success("req-1", {"result": "ok"})
        assert response.id == "req-1"
        assert response.result == {"result": "ok"}
        assert response.error is None

    def test_error_response(self):
        """Test creating error response."""
        response = RPCResponse.create_error(
            "req-2", RPCErrorCode.FILE_NOT_FOUND, "File not found", data={"path": "/missing.txt"}
        )
        assert response.id == "req-2"
        assert response.result is None
        assert response.error is not None
        assert response.error["code"] == -32000
        assert response.error["message"] == "File not found"
        assert response.error["data"] == {"path": "/missing.txt"}

    def test_to_dict_success(self):
        """Test converting success response to dict."""
        response = RPCResponse.success("req-3", {"files": ["/a.txt", "/b.txt"]})
        result = response.to_dict()
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "req-3"
        assert result["result"] == {"files": ["/a.txt", "/b.txt"]}
        assert "error" not in result

    def test_to_dict_error(self):
        """Test converting error response to dict."""
        response = RPCResponse.create_error("req-4", RPCErrorCode.INVALID_PATH, "Invalid path")
        result = response.to_dict()
        assert result["jsonrpc"] == "2.0"
        assert result["id"] == "req-4"
        assert "result" not in result
        assert result["error"]["code"] == -32002
        assert result["error"]["message"] == "Invalid path"


class TestRPCEncoder:
    """Tests for custom JSON encoder."""

    def test_encode_bytes(self):
        """Test encoding bytes."""
        import json

        data = {"content": b"Hello, World!"}
        encoded = json.dumps(data, cls=RPCEncoder)
        assert "__type__" in encoded
        assert "bytes" in encoded

    def test_encode_datetime(self):
        """Test encoding datetime."""
        import json

        dt = datetime(2024, 1, 15, 10, 30, 45)
        data = {"timestamp": dt}
        encoded = json.dumps(data, cls=RPCEncoder)
        assert "__type__" in encoded
        assert "datetime" in encoded
        assert "2024-01-15" in encoded

    def test_encode_object_with_dict(self):
        """Test encoding objects with __dict__."""
        import json

        class TestObject:
            def __init__(self):
                self.value = 42
                self.name = "test"

            def some_method(self):
                pass

        obj = TestObject()
        data = {"obj": obj}
        encoded = json.dumps(data, cls=RPCEncoder)
        decoded = json.loads(encoded)
        assert decoded["obj"]["value"] == 42
        assert decoded["obj"]["name"] == "test"
        assert "some_method" not in decoded["obj"]  # Methods should be filtered


class TestEncodeDecodeRPCMessage:
    """Tests for encoding/decoding RPC messages."""

    def test_encode_decode_simple(self):
        """Test encoding and decoding simple message."""
        data = {"jsonrpc": "2.0", "id": "1", "result": {"value": 123}}
        encoded = encode_rpc_message(data)
        decoded = decode_rpc_message(encoded)
        assert decoded == data

    def test_encode_decode_with_bytes(self):
        """Test encoding and decoding message with bytes."""
        data = {"jsonrpc": "2.0", "id": "2", "result": {"content": b"Test data"}}
        encoded = encode_rpc_message(data)
        decoded = decode_rpc_message(encoded)
        assert decoded["result"]["content"] == b"Test data"

    def test_encode_decode_with_datetime(self):
        """Test encoding and decoding message with datetime."""
        dt = datetime(2024, 10, 19, 12, 0, 0)
        data = {"jsonrpc": "2.0", "id": "3", "result": {"timestamp": dt}}
        encoded = encode_rpc_message(data)
        decoded = decode_rpc_message(encoded)
        # Note: microseconds might differ slightly
        assert decoded["result"]["timestamp"].year == 2024
        assert decoded["result"]["timestamp"].month == 10
        assert decoded["result"]["timestamp"].day == 19


class TestParseMethodParams:
    """Tests for parse_method_params function.

    Kernel syscalls (sys_read / sys_write / sys_readdir / sys_setattr /
    ...) bypass ``parse_method_params`` entirely — they route through
    the Rust thin-dispatch path emitted by ``codegen_kernel_abi.py``
    into ``_kernel_syscall_dispatch.dispatch_kernel_syscall``.  Tests
    that pass kernel-syscall wire names to ``parse_method_params``
    therefore exercise an unreachable code path and are removed; only
    the contract-level "unknown method raises" check survives because
    it still applies to non-kernel RPCs that continue to flow through
    ``parse_method_params``.
    """

    def test_parse_unknown_method(self):
        """Test parsing unknown method raises error."""
        with pytest.raises(ValueError, match="Unknown method"):
            parse_method_params("unknown_method", {})


class TestRPCErrorCode:
    """Tests for RPCErrorCode enum."""

    def test_error_codes(self):
        """Test error code values."""
        assert RPCErrorCode.FILE_NOT_FOUND.value == -32000
        assert RPCErrorCode.INVALID_PATH.value == -32002
        assert RPCErrorCode.INTERNAL_ERROR.value == -32603
        assert RPCErrorCode.PARSE_ERROR.value == -32700


# ============================================================
# ReBAC param-dataclass tests removed: ``RebacCreateParams`` /
# ``RebacCheckParams`` / ``RebacExpandParams`` / ``RebacExplainParams``
# / ``RebacListTuplesParams`` were Python-side wire-form dataclasses
# emitted by the legacy codegen for the @rpc_expose ReBAC chain.  After
# the ReBAC service migrated to Rust (PR #3955 / task #43), incoming
# ReBAC RPCs route through the Rust dispatch path and the wire-form
# tuples are deserialised in Rust via serde — no Python __post_init__
# list→tuple conversion needed any more.  Keeping these tests would
# only verify dead code paths.


# ============================================================
# Codegen consistency tests
# ============================================================


class TestCodegenConsistency:
    """Verify generated Param classes match @rpc_expose signatures."""

    def test_all_method_params_are_dataclasses(self):
        """Every class in METHOD_PARAMS should be a dataclass."""
        for method_name, param_class in METHOD_PARAMS.items():
            assert dataclasses.is_dataclass(param_class), (
                f"METHOD_PARAMS['{method_name}'] = {param_class.__name__} is not a dataclass"
            )

    def test_method_params_names_are_strings(self):
        """All keys in METHOD_PARAMS should be non-empty strings."""
        for key in METHOD_PARAMS:
            assert isinstance(key, str) and len(key) > 0

    # NOTE: ``test_method_params_count`` (≥150 floor),
    # ``test_override_classes_take_precedence``, and
    # ``test_merged_method_params_has_both_generated_and_overrides``
    # all asserted contracts of the legacy @rpc_expose dispatch path
    # which migrated to Rust thin-dispatch + the per-service Rust
    # ports (PR #3955).  Kernel syscalls (sys_read / sys_write /
    # sys_readdir) and migrated services (federation / mount /
    # snapshots / workspace / share_link / oauth / search / rebac /
    # mcp) no longer flow through ``METHOD_PARAMS`` at all, so the
    # 150+ floor and the "sys_read in METHOD_PARAMS" assertions are
    # facts about a code path that has been deleted.  Removing them
    # rather than weakening their thresholds, per the project rule
    # against silently bypassing the Rust syscall boundary.

    def test_parse_method_params_works_for_all(self):
        """parse_method_params should work for every METHOD_PARAMS entry (with defaults)."""
        for method_name, param_class in METHOD_PARAMS.items():
            fields = dataclasses.fields(param_class)
            required = [
                f
                for f in fields
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            ]
            if not required:
                result = parse_method_params(method_name, {})
                assert isinstance(result, param_class)

    def test_mkdir_rmdir_alias_defaults_are_conservative(self):
        """Regression test for #3701 Codex finding #1.

        NexusFS.mkdir defaults to ``parents=True, exist_ok=True``
        (mkdir -p) and NexusFS.rmdir defaults to ``recursive=True``
        (rm -rf), but the legacy ``mkdir`` / ``rmdir`` / ``sys_rmdir``
        RPC aliases must override those defaults to the conservative
        equivalents (``parents=False``, ``exist_ok=False``,
        ``recursive=False``).

        Dropping these overrides silently turns a previously safe
        ``rmdir`` call into a destructive recursive subtree delete and
        ``mkdir`` into mkdir-p — a real behavioral regression for
        legacy clients that send only ``{"path": "/foo"}``.

        After the @rpc_expose chain migrated to the codegen-emitted
        Rust thin-dispatch path, the override-class layer
        (``MkdirAliasParams`` / ``RmdirAliasParams``) was retired in
        favour of an explicit ``_apply_pre_call_defaults`` step in
        ``_kernel_syscall_dispatch`` that runs before the kernel
        invocation.  This test now pins that new layer's defaults to
        the same conservative values, so a future codegen drop can't
        silently re-introduce mkdir-p / rm-rf for bare-path callers.
        """
        from nexus.server._kernel_syscall_dispatch import _apply_pre_call_defaults

        for wire_name in ("mkdir", "sys_mkdir"):
            defaulted = _apply_pre_call_defaults(wire_name, {"path": "/foo"})
            assert defaulted["parents"] is False, f"{wire_name}: parents default leaked"
            assert defaulted["exist_ok"] is False, f"{wire_name}: exist_ok default leaked"

        for wire_name in ("rmdir", "sys_rmdir"):
            defaulted = _apply_pre_call_defaults(wire_name, {"path": "/foo"})
            assert defaulted["recursive"] is False, f"{wire_name}: recursive default leaked"

        # Caller-supplied overrides must still win over the conservative
        # defaults — the layer is "fill the missing slot", not "force".
        forced = _apply_pre_call_defaults("mkdir", {"path": "/foo", "parents": True})
        assert forced["parents"] is True

    # ``test_remote_proxy_positional_arg_resolution_for_critical_rpcs``
    # used to assert ``semantic_search`` / ``register_workspace`` /
    # ``register_agent`` lived in ``METHOD_PARAMS`` so the legacy
    # Python ``RemoteServiceProxy.__getattr__`` positional binding
    # worked.  All three services migrated to Rust (search →
    # rust/shared/lib/src/search, workspace + agent → managed_agent)
    # in PR #3955; production clients now reach them through the Rust
    # dispatch path which uses serde for positional binding (no
    # ``METHOD_PARAMS`` lookup involved).  Dropping the assertion is
    # the architecturally correct call rather than weakening it to
    # cover only the residual non-migrated names.


# ============================================================
# Parametrized coverage tests
# ============================================================


def _get_required_fields(param_class: type) -> list[dataclasses.Field]:  # type: ignore[type-arg]
    """Get fields without defaults."""
    return [
        f
        for f in dataclasses.fields(param_class)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    ]


@pytest.mark.parametrize(
    ("method_name", "param_class"),
    sorted(METHOD_PARAMS.items()),
    ids=sorted(METHOD_PARAMS.keys()),
)
def test_param_class_is_valid_dataclass(method_name: str, param_class: type) -> None:  # noqa: ARG001
    """Every Param class should be a proper dataclass with a docstring."""
    assert dataclasses.is_dataclass(param_class)
    assert param_class.__doc__ is not None, f"{param_class.__name__} missing docstring"


@pytest.mark.parametrize(
    ("method_name", "param_class"),
    sorted(METHOD_PARAMS.items()),
    ids=sorted(METHOD_PARAMS.keys()),
)
def test_required_fields_raise_typeerror(method_name: str, param_class: type) -> None:  # noqa: ARG001
    """Param classes with required fields should raise TypeError when called with no args."""
    required = _get_required_fields(param_class)
    if required:
        with pytest.raises(TypeError):
            param_class()  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("method_name", "param_class"),
    sorted(METHOD_PARAMS.items()),
    ids=sorted(METHOD_PARAMS.keys()),
)
def test_param_class_name_ends_with_params(method_name: str, param_class: type) -> None:  # noqa: ARG001
    """Every Param class name should end with 'Params'."""
    assert param_class.__name__.endswith("Params"), (
        f"{param_class.__name__} does not end with 'Params'"
    )
