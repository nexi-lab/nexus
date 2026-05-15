"""Pydantic discriminated-union manifest contract tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.extensions.errors import ReservedNameError
from nexus.extensions.manifest import (
    AnyManifest,
    BrickManifest,
    ConnectorManifest,
    PluginManifest,
    RuntimeDep,
    parse_manifest,
)
from nexus.extensions.types import ArgType, ConnectionArg


class TestRuntimeDep:
    def test_python_dep(self):
        dep = RuntimeDep(kind="python", name="boto3")
        assert dep.kind == "python"
        assert dep.name == "boto3"
        assert dep.extras == ()
        assert dep.install_hint is None

    def test_binary_dep_with_hint(self):
        dep = RuntimeDep(kind="binary", name="git", install_hint="apt install git")
        assert dep.install_hint == "apt install git"

    def test_service_dep(self):
        dep = RuntimeDep(kind="service", name="postgres")
        assert dep.kind == "service"

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValidationError):
            RuntimeDep(kind="cosmic", name="x")

    def test_extras_immutable(self):
        dep = RuntimeDep(kind="python", name="a", extras=("b", "c"))
        assert dep.extras == ("b", "c")

    def test_json_round_trip(self):
        dep = RuntimeDep(kind="python", name="boto3", extras=("s3",))
        data = dep.model_dump()
        again = RuntimeDep.model_validate(data)
        assert again == dep


class TestExtensionManifestBase:
    """Reserved-name validation lives on the base class; exercise via PluginManifest
    (fewest required fields)."""

    def _make(self, name: str) -> PluginManifest:
        return PluginManifest(
            name=name,
            module="x.y",
            factory="Z",
            entry_point_group="nexus.plugins",
        )

    @pytest.mark.parametrize(
        "bad_name,reason",
        [
            ("", "empty"),
            ("_leading", "leading underscore"),
            ("nexus", "reserved nexus name"),
            ("nexus-internal", "reserved nexus prefix"),
            ("*", "glob"),
        ],
    )
    def test_reserved_name_rejected(self, bad_name: str, reason: str):
        with pytest.raises(ReservedNameError):
            self._make(bad_name)

    def test_normal_name_accepted(self):
        m = self._make("my-extension")
        assert m.name == "my-extension"

    def test_module_required(self):
        with pytest.raises(ValidationError):
            PluginManifest(
                name="ok",
                module="",
                factory="Z",
                entry_point_group="nexus.plugins",
            )

    def test_factory_required(self):
        with pytest.raises(ValidationError):
            PluginManifest(
                name="ok",
                module="x.y",
                factory="",
                entry_point_group="nexus.plugins",
            )


class TestConnectorManifest:
    def test_minimal(self):
        m = ConnectorManifest(
            name="hn",
            module="nexus.backends.connectors.hn.connector",
            factory="HNConnector",
            service_name="hn",
        )
        assert m.kind == "connector"
        assert m.service_name == "hn"
        assert m.capabilities == frozenset()
        assert m.user_scoped is False

    def test_with_capabilities_and_args(self):
        m = ConnectorManifest(
            name="x",
            module="m",
            factory="F",
            service_name="x",
            capabilities=frozenset({"streaming"}),
            connection_args={"url": ConnectionArg(type=ArgType.STRING, description="endpoint")},
            user_scoped=True,
        )
        assert "streaming" in m.capabilities
        assert "url" in m.connection_args


class TestBrickManifest:
    def test_independent_tier(self):
        m = BrickManifest(
            name="search",
            module="nexus.bricks.search.brick_factory",
            factory="create",
            tier="independent",
            result_key="search_service",
            profile_gate="search",
        )
        assert m.kind == "brick"
        assert m.tier == "independent"
        assert m.profile_gate == "search"

    def test_dependent_tier_with_artifacts(self):
        m = BrickManifest(
            name="upload",
            module="m",
            factory="create",
            tier="dependent",
            result_key="upload_service",
            produces=("upload_observer",),
            consumes=("artifact_bus",),
        )
        assert m.produces == ("upload_observer",)
        assert m.consumes == ("artifact_bus",)

    def test_invalid_tier_rejected(self):
        with pytest.raises(ValidationError):
            BrickManifest(
                name="x",
                module="m",
                factory="F",
                tier="weird",
                result_key="r",
            )


class TestPluginManifest:
    def test_minimal(self):
        m = PluginManifest(
            name="koi",
            module="koi.plugin",
            factory="KoiPlugin",
        )
        assert m.kind == "plugin"
        assert m.entry_point_group == "nexus.plugins"
        assert m.hooks == {}
        assert m.commands == {}

    def test_hooks_and_commands(self):
        m = PluginManifest(
            name="x",
            module="m",
            factory="F",
            hooks={"on_boot": "m.on_boot"},
            commands={"do": "m.do_command"},
        )
        assert m.hooks["on_boot"] == "m.on_boot"


class TestDiscriminatedUnion:
    def test_parse_connector(self):
        data = {
            "kind": "connector",
            "name": "hn",
            "module": "m",
            "factory": "F",
            "service_name": "hn",
        }
        m = parse_manifest(data)
        assert isinstance(m, ConnectorManifest)

    def test_parse_brick(self):
        data = {
            "kind": "brick",
            "name": "search",
            "module": "m",
            "factory": "F",
            "tier": "independent",
            "result_key": "r",
        }
        m = parse_manifest(data)
        assert isinstance(m, BrickManifest)

    def test_parse_plugin(self):
        data = {
            "kind": "plugin",
            "name": "koi",
            "module": "m",
            "factory": "F",
        }
        m = parse_manifest(data)
        assert isinstance(m, PluginManifest)

    def test_unknown_kind_rejected(self):
        data = {"kind": "robot", "name": "x", "module": "m", "factory": "F"}
        with pytest.raises(ValidationError):
            parse_manifest(data)

    def test_json_round_trip_each_kind(self):
        manifests: list[AnyManifest] = [
            ConnectorManifest(name="hn", module="m", factory="F", service_name="hn"),
            BrickManifest(
                name="search",
                module="m",
                factory="F",
                tier="independent",
                result_key="r",
            ),
            PluginManifest(name="koi", module="m", factory="F"),
        ]
        for original in manifests:
            data = original.model_dump()
            parsed = parse_manifest(data)
            assert parsed == original
