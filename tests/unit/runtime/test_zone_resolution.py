from types import SimpleNamespace

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.runtime.zone_resolution import (
    target_zone_for_context,
    zone_from_params,
    zone_from_path,
)


def test_zone_from_path_reads_zone_prefix() -> None:
    assert zone_from_path("/zone/eng/docs/a.txt") == "eng"
    assert zone_from_path("/zone/legal") == "legal"
    assert zone_from_path("/plain/path.txt") is None


def test_zone_from_path_ignores_empty_and_root_paths() -> None:
    assert zone_from_path("") is None
    assert zone_from_path("/") is None
    assert zone_from_path("/zone/") is None
    assert zone_from_path(f"/zone/{ROOT_ZONE_ID}/docs/a.txt") is None


def test_zone_from_params_prefers_explicit_zone_attribute() -> None:
    params = SimpleNamespace(zone_id="ops", path="/zone/eng/docs/a.txt")
    assert zone_from_params(params) == "ops"


def test_zone_from_params_reads_nested_batch_paths() -> None:
    params = SimpleNamespace(files=[("/zone/eng/a.txt", b"a"), ("/zone/eng/b.txt", b"b")])
    assert zone_from_params(params) == "eng"


def test_zone_from_params_reads_tuple_file_containers() -> None:
    params = {"files": (("/zone/eng/a.txt", b"a"),)}
    assert zone_from_params(params) == "eng"


def test_zone_from_params_reads_tuple_operation_containers() -> None:
    params = {"operations": ({"path": "/zone/legal/a.txt"},)}
    assert zone_from_params(params) == "legal"


def test_zone_from_params_reads_nested_dict_container_fields() -> None:
    params = {"operations": {"files": [("/zone/eng/a.txt", b"a")]}}
    assert zone_from_params(params) == "eng"


def test_zone_from_params_reads_nested_object_container_fields() -> None:
    params = SimpleNamespace(
        operations=SimpleNamespace(files=[SimpleNamespace(path="/zone/legal/a.txt")])
    )
    assert zone_from_params(params) == "legal"


def test_zone_from_params_reads_tagged_operation_tuple_payload() -> None:
    params = {"operations": [("write", {"path": "/zone/eng/a.txt"})]}
    assert zone_from_params(params) == "eng"


def test_zone_from_params_reads_tagged_operation_tuple_paths() -> None:
    params = {"operations": [("rename", "/zone/legal/a.txt", "/zone/legal/b.txt")]}
    assert zone_from_params(params) == "legal"


def test_target_zone_uses_non_root_context_when_no_path() -> None:
    context = OperationContext(user_id="alice", groups=[], zone_id="eng")
    assert target_zone_for_context(context, None) == "eng"


def test_target_zone_ignores_root_context_without_concrete_zone() -> None:
    context = OperationContext(user_id="alice", groups=[], zone_id=ROOT_ZONE_ID)
    assert target_zone_for_context(context, SimpleNamespace(path="/docs/a.txt")) is None


def test_target_zone_uses_embedded_path_for_root_multizone_context() -> None:
    context = OperationContext(
        user_id="alice",
        groups=[],
        zone_id=ROOT_ZONE_ID,
        zone_perms=(("eng", "r"), ("legal", "r")),
    )
    params = SimpleNamespace(path="/zone/legal/docs/a.txt")
    assert target_zone_for_context(context, params) == "legal"
