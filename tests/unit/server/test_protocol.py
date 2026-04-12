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
    """Tests for parse_method_params function."""

    def test_parse_read_params(self):
        """Test parsing read method parameters."""
        params = parse_method_params("sys_read", {"path": "/test.txt"})
        assert params.path == "/test.txt"

    def test_parse_write_params(self):
        """Test parsing write method parameters."""
        params = parse_method_params("sys_write", {"path": "/file.txt", "buf": b"data"})
        assert params.path == "/file.txt"
        assert params.buf == b"data"

    def test_parse_list_params(self):
        """Test parsing list method parameters."""
        params = parse_method_params(
            "sys_readdir", {"path": "/workspace", "recursive": True, "details": False}
        )
        assert params.path == "/workspace"
        assert params.recursive is True
        assert params.details is False

    def test_parse_list_params_defaults(self):
        """Test parsing list with default parameters."""
        params = parse_method_params("sys_readdir", {})
        assert params.path == "/"
        assert params.recursive is True
        assert params.details is False

    def test_parse_unknown_method(self):
        """Test parsing unknown method raises error."""
        with pytest.raises(ValueError, match="Unknown method"):
            parse_method_params("unknown_method", {})

    def test_parse_invalid_params(self):
        """Test parsing with invalid parameters raises error."""
        with pytest.raises(ValueError, match="Invalid parameters"):
            parse_method_params("sys_read", {"invalid_param": "value"})


class TestRPCErrorCode:
    """Tests for RPCErrorCode enum."""

    def test_error_codes(self):
        """Test error code values."""
        assert RPCErrorCode.FILE_NOT_FOUND.value == -32000
        assert RPCErrorCode.INVALID_PATH.value == -32002
        assert RPCErrorCode.INTERNAL_ERROR.value == -32603
        assert RPCErrorCode.PARSE_ERROR.value == -32700


# ============================================================
# ReBAC __post_init__ list→tuple conversion tests
# ============================================================


class TestRebacPostInit:
    """Tests for __post_init__ list→tuple conversion in ReBAC Param classes."""

    def test_rebac_create_converts_lists_to_tuples(self):
        """RebacCreateParams should convert list args to tuples (JSON compat)."""
        from nexus.server.protocol import RebacCreateParams

        params = RebacCreateParams(
            subject=["user", "alice"],  # type: ignore[arg-type]
            relation="viewer",
            object=["file", "/test.txt"],  # type: ignore[arg-type]
        )
        assert isinstance(params.subject, tuple)
        assert isinstance(params.object, tuple)
        assert params.subject == ("user", "alice")
        assert params.object == ("file", "/test.txt")

    def test_rebac_check_converts_lists_to_tuples(self):
        """RebacCheckParams should convert list args to tuples."""
        from nexus.server.protocol import RebacCheckParams

        params = RebacCheckParams(
            subject=["user", "bob"],  # type: ignore[arg-type]
            permission="read",
            object=["file", "/data.csv"],  # type: ignore[arg-type]
        )
        assert isinstance(params.subject, tuple)
        assert isinstance(params.object, tuple)
        assert params.subject == ("user", "bob")

    def test_rebac_check_preserves_tuples(self):
        """RebacCheckParams should leave real tuples unchanged."""
        from nexus.server.protocol import RebacCheckParams

        params = RebacCheckParams(
            subject=("user", "carol"),
            permission="write",
            object=("file", "/doc.md"),
        )
        assert params.subject == ("user", "carol")
        assert params.object == ("file", "/doc.md")

    def test_rebac_expand_converts_object(self):
        """RebacExpandParams converts object list→tuple."""
        from nexus.server.protocol import RebacExpandParams

        params = RebacExpandParams(
            permission="read",
            object=["file", "/shared"],  # type: ignore[arg-type]
        )
        assert isinstance(params.object, tuple)
        assert params.object == ("file", "/shared")

    def test_rebac_explain_converts_both(self):
        """RebacExplainParams converts both subject and object."""
        from nexus.server.protocol import RebacExplainParams

        params = RebacExplainParams(
            subject=["user", "dave"],  # type: ignore[arg-type]
            permission="admin",
            object=["zone", "z1"],  # type: ignore[arg-type]
        )
        assert isinstance(params.subject, tuple)
        assert isinstance(params.object, tuple)

    def test_rebac_list_tuples_converts_optional(self):
        """RebacListTuplesParams converts optional tuple fields."""
        from nexus.server.protocol import RebacListTuplesParams

        params = RebacListTuplesParams(
            subject=["user", "eve"],  # type: ignore[arg-type]
            object=["file", "/x"],  # type: ignore[arg-type]
        )
        assert isinstance(params.subject, tuple)
        assert isinstance(params.object, tuple)

    def test_rebac_list_tuples_none_stays_none(self):
        """RebacListTuplesParams leaves None fields as None."""
        from nexus.server.protocol import RebacListTuplesParams

        params = RebacListTuplesParams()
        assert params.subject is None
        assert params.object is None


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

    def test_method_params_count(self):
        """METHOD_PARAMS should have a reasonable number of entries.

        Threshold history:
        * Lowered from 113 → 90 in #3701 (commit d9d429abd) under the
          mistaken assumption that ``nexus.system_services.*`` methods
          had been deleted; in reality those methods just *moved* and
          the codegen MODULES_TO_SCAN entries were stale.
        * Restored to ≥150 after Codex review of #3701 (finding #3)
          repaired MODULES_TO_SCAN to point at the post-refactor
          canonical locations (``nexus.services.*``,
          ``nexus.server.rpc.services.*``, ``nexus.bricks.auth.oauth.*``).
          This guards against silently re-introducing stale module
          paths that would once again drop METHOD_PARAMS entries and
          break ``RemoteServiceProxy`` positional binding for any
          missing RPC.
        """
        assert len(METHOD_PARAMS) >= 150, (
            f"Expected at least 150 METHOD_PARAMS entries, got {len(METHOD_PARAMS)}"
        )

    def test_method_params_names_are_strings(self):
        """All keys in METHOD_PARAMS should be non-empty strings."""
        for key in METHOD_PARAMS:
            assert isinstance(key, str) and len(key) > 0

    def test_override_classes_take_precedence(self):
        """Manual override classes should override generated ones."""
        from nexus.server._rpc_param_overrides import ReadParams as OverrideRead
        from nexus.server.protocol import ReadParams

        assert ReadParams is OverrideRead
        assert hasattr(ReadParams, "return_url"), "ReadParams should have RPC-only return_url"
        assert hasattr(ReadParams, "expires_in"), "ReadParams should have RPC-only expires_in"

    def test_merged_method_params_has_both_generated_and_overrides(self):
        """METHOD_PARAMS should contain both generated and override entries."""
        # Generated entries
        assert "sys_write" in METHOD_PARAMS
        assert "sys_readdir" in METHOD_PARAMS
        assert "grep" in METHOD_PARAMS
        # Override entries
        assert "sys_read" in METHOD_PARAMS
        assert "admin_create_key" in METHOD_PARAMS

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
        """
        from nexus.server._rpc_param_overrides import (
            MkdirAliasParams,
            RmdirAliasParams,
        )

        assert METHOD_PARAMS["mkdir"] is MkdirAliasParams
        assert METHOD_PARAMS["rmdir"] is RmdirAliasParams
        # sys_rmdir is the legacy spelling kept for older remote clients;
        # it must share the same conservative defaults.
        assert METHOD_PARAMS["sys_rmdir"] is RmdirAliasParams

        mkdir_defaults = {f.name: f.default for f in dataclasses.fields(MkdirAliasParams)}
        assert mkdir_defaults["parents"] is False
        assert mkdir_defaults["exist_ok"] is False

        rmdir_defaults = {f.name: f.default for f in dataclasses.fields(RmdirAliasParams)}
        assert rmdir_defaults["recursive"] is False

        # Round-trip through parse_method_params with only ``path`` set
        # (the way legacy clients call) — defaults must hold.
        parsed_mkdir = cast(MkdirAliasParams, parse_method_params("mkdir", {"path": "/foo"}))
        assert parsed_mkdir.parents is False
        assert parsed_mkdir.exist_ok is False

        parsed_rmdir = cast(RmdirAliasParams, parse_method_params("rmdir", {"path": "/foo"}))
        assert parsed_rmdir.recursive is False

        parsed_sys_rmdir = cast(
            RmdirAliasParams, parse_method_params("sys_rmdir", {"path": "/foo"})
        )
        assert parsed_sys_rmdir.recursive is False

    def test_remote_proxy_positional_arg_resolution_for_critical_rpcs(self):
        """Regression test for Codex review #2 finding #3.

        ``RemoteServiceProxy``/``RPCProxyBase`` use METHOD_PARAMS to map
        positional call arguments to parameter names. When an exposed
        RPC is missing from METHOD_PARAMS, ``_get_param_names`` returns
        ``[]`` and the positional caller's first argument is silently
        dropped from the JSON-RPC body — the call serializes without
        e.g. ``query`` for ``semantic_search``, ``path`` for
        ``register_workspace``, ``agent_id`` for ``register_agent``.

        Codex caught this for ``semantic_search`` (already exposed via
        SearchService and called positionally). The root cause was a
        stale ``MODULES_TO_SCAN`` in ``scripts/generate_rpc_params.py``
        that pointed at deleted ``nexus.system_services.*`` paths,
        causing the codegen to silently skip those modules. This test
        guards the critical positional-call surface so future regen
        drift can't reintroduce the same silent breakage.
        """
        from nexus.remote.rpc_proxy import RPCProxyBase

        # Each tuple: (rpc_name, expected first positional param)
        critical_positional_rpcs = [
            ("semantic_search", "query"),
            ("register_workspace", "path"),
            ("register_agent", "agent_id"),
        ]
        for rpc_name, expected_first_param in critical_positional_rpcs:
            assert rpc_name in METHOD_PARAMS, (
                f"METHOD_PARAMS missing {rpc_name!r} — positional remote calls "
                f"will silently misserialize. Check MODULES_TO_SCAN in "
                f"scripts/generate_rpc_params.py."
            )
            param_names = RPCProxyBase._get_param_names(rpc_name)
            assert param_names, (
                f"_get_param_names({rpc_name!r}) returned [] — positional binding broken"
            )
            assert param_names[0] == expected_first_param, (
                f"{rpc_name}: expected first positional={expected_first_param!r}, "
                f"got {param_names[0]!r}. RemoteServiceProxy.{rpc_name}({expected_first_param}, ...) "
                f"would serialize without {expected_first_param!r}."
            )


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
