from types import SimpleNamespace

from nexus.contracts.backend_features import BackendFeature
from nexus.grpc.capability_discovery import (
    PROTOCOL_VERSION,
    backend_capability_dict,
    build_initialize_response_dict,
    capability_for_path,
    empty_posix,
    posix_from_backend_features,
    writable_posix,
)


def test_posix_from_backend_features_maps_known_features() -> None:
    posix = posix_from_backend_features(
        {
            BackendFeature.DIRECTORY_LISTING,
            BackendFeature.PATH_DELETE,
            BackendFeature.RENAME,
        }
    )

    assert posix["read"] is True
    assert posix["stat"] is True
    assert posix["readdir"] is True
    assert posix["unlink"] is True
    assert posix["rmdir"] is True
    assert posix["rename"] is True
    assert posix["write"] is False


def test_build_initialize_response_dict_includes_mounts_and_extensions() -> None:
    kernel = SimpleNamespace(get_mount_points=lambda: ["/root", "/root/read-only"])
    nexus_fs = SimpleNamespace(_kernel=kernel)

    payload = build_initialize_response_dict(
        nexus_fs=nexus_fs,
        exposed_methods={"grep": object(), "glob": object(), "workspace_snapshot": object()},
        server_version="0.10.0",
        rust_mounts={
            "/": {
                "backend_name": "cas_local",
                "backend_type": "cas_local",
                "rust_native": True,
                "external": False,
                "posix": writable_posix(),
                "features": ["cas", "native_versioning"],
                "extensions": [],
            },
            "/read-only": {
                "backend_name": "gdrive",
                "backend_type": "gdrive",
                "rust_native": False,
                "external": True,
                "posix": {**empty_posix(), "read": True, "stat": True, "readdir": True},
                "features": ["directory_listing"],
                "extensions": ["x-nexus:versioning"],
            },
        },
    )

    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["capabilities"]["commands"]["grep"]["supported"] is True
    assert payload["capabilities"]["commands"]["glob"]["supported"] is True
    assert payload["capabilities"]["workspace"]["snapshot"] is True
    assert payload["capabilities"]["workspace"]["restore"] is False
    assert payload["capabilities"]["workspace"]["watch"] is False
    assert payload["capabilities"]["backends"]["/"]["posix"]["write"] is True
    assert payload["capabilities"]["backends"]["/read-only"]["posix"]["write"] is False
    assert "x-nexus:versioning" in payload["capabilities"]["extensions"]


def test_build_initialize_response_dict_marks_absent_commands_false() -> None:
    nexus_fs = SimpleNamespace(_kernel=SimpleNamespace(get_mount_points=lambda: []))

    payload = build_initialize_response_dict(
        nexus_fs=nexus_fs,
        exposed_methods={},
        server_version="0.10.0",
    )

    assert payload["capabilities"]["commands"]["grep"]["supported"] is False
    assert payload["capabilities"]["commands"]["glob"]["supported"] is False


def test_backend_capability_dict_preserves_explicit_empty_posix_as_deny_all() -> None:
    backend = backend_capability_dict(features={BackendFeature.CAS}, posix={})

    assert backend["posix"] == empty_posix()


def test_capability_for_path_treats_missing_matched_posix_keys_as_unknown() -> None:
    capabilities = {
        "posix": writable_posix(),
        "backends": {
            "/": {"posix": writable_posix()},
            "/mnt/readonly": {
                "posix": {"read": True, "stat": True, "readdir": True, "unlink": False}
            },
        },
    }

    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "read") is True
    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "unlink") is False
    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "write") is None
    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "rename") is None


def test_capability_for_path_treats_missing_top_level_posix_keys_as_unknown() -> None:
    capabilities = {"posix": {"read": True}}

    assert capability_for_path(capabilities, "/workspace/file.txt", "read") is True
    assert capability_for_path(capabilities, "/workspace/file.txt", "write") is None


def test_capability_for_path_uses_longest_mount_prefix() -> None:
    capabilities = {
        "posix": writable_posix(),
        "backends": {
            "/": {"posix": writable_posix()},
            "/mnt/readonly": {
                "posix": {**empty_posix(), "read": True, "stat": True, "readdir": True}
            },
        },
    }

    assert capability_for_path(capabilities, "/tmp/file.txt", "write") is True
    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "write") is False
    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "read") is True


def test_capability_for_path_respects_mount_path_boundaries() -> None:
    capabilities = {
        "posix": empty_posix(),
        "backends": {
            "/mnt": {"posix": {**empty_posix(), "write": False}},
            "/mnt2": {"posix": {**empty_posix(), "write": True}},
        },
    }

    assert capability_for_path(capabilities, "/mnt/file.txt", "write") is False
    assert capability_for_path(capabilities, "/mnt2/file.txt", "write") is True
