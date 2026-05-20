from unittest.mock import MagicMock


def test_sys_service_registration_is_visible_when_kernel_client_drops_objects():
    from nexus.core.nexus_fs import NexusFS

    fs = NexusFS.__new__(NexusFS)
    service = MagicMock()
    kernel = MagicMock()
    kernel.service_lookup.return_value = None
    fs._kernel = kernel
    fs._local_services = {}
    fs._local_service_exports = {}
    fs._hook_specs = {}

    result = fs.sys_setattr("/__sys__/services/rebac", service=service, exports=("rebac_create",))

    assert result["registered"] is True
    assert fs.service("rebac") is service
    assert fs._local_service_exports["rebac"] == ("rebac_create",)
    kernel.service_enlist.assert_called_once_with(
        "rebac",
        service,
        ["rebac_create"],
        False,
    )


def test_service_lookup_prefers_process_local_service_before_kernel():
    from nexus.core.nexus_fs import NexusFS

    fs = NexusFS.__new__(NexusFS)
    local = MagicMock(name="local")
    kernel = MagicMock()
    kernel.service_lookup.return_value = MagicMock(name="kernel")
    fs._local_services = {"workspace_rpc": local}
    fs._kernel = kernel

    assert fs.service("workspace_rpc") is local
    kernel.service_lookup.assert_not_called()
