"""Pure data types — must be importable with zero backend deps."""

import sys

from nexus.extensions.types import ArgType, ConnectionArg


def test_arg_type_values():
    assert ArgType.STRING.value == "string"
    assert ArgType.SECRET.value == "secret"
    assert ArgType.PASSWORD.value == "password"
    assert ArgType.INTEGER.value == "integer"
    assert ArgType.BOOLEAN.value == "boolean"
    assert ArgType.PATH.value == "path"
    assert ArgType.OAUTH.value == "oauth"


def test_connection_arg_round_trip():
    arg = ConnectionArg(type=ArgType.STRING, description="bucket name", required=True)
    d = arg.to_dict()
    assert d == {
        "type": "string",
        "description": "bucket name",
        "required": True,
        "default": None,
        "secret": False,
        "env_var": None,
    }


def test_connection_arg_with_config_key():
    arg = ConnectionArg(type=ArgType.STRING, description="x", required=True, config_key="bucket")
    d = arg.to_dict()
    assert d["config_key"] == "bucket"


def test_types_module_has_no_backend_imports():
    """nexus.extensions.types must not pull anything from nexus.backends."""
    import nexus.extensions.types  # noqa: F401

    types_mod = sys.modules["nexus.extensions.types"]
    for name, val in vars(types_mod).items():
        if hasattr(val, "__module__") and val.__module__:
            assert not val.__module__.startswith("nexus.backends."), (
                f"{name} resolves to {val.__module__}"
            )


def test_backwards_compat_reexport():
    """ArgType and ConnectionArg must remain importable from old location."""
    from nexus.backends.base.registry import ArgType as A2
    from nexus.backends.base.registry import ConnectionArg as C2

    assert A2 is ArgType
    assert C2 is ConnectionArg
