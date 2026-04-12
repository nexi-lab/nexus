from nexus.factory._remote import install_remote_kernel_rpc_overrides


class _DummyTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def call_rpc(self, method: str, params: dict[str, str]) -> dict[str, object]:
        self.calls.append((method, params))
        return {"ok": True}


class _DummyNfs:
    def sys_rename(self, old_path: str, new_path: str, **kwargs: object) -> dict[str, object]:
        raise AssertionError("original client-side sys_rename should be replaced")


def test_install_remote_kernel_rpc_overrides_routes_sys_rename_to_server_rpc() -> None:
    nfs = _DummyNfs()
    transport = _DummyTransport()

    install_remote_kernel_rpc_overrides(nfs, transport)

    result = nfs.sys_rename("/workspace/old.txt", "/workspace/new.txt")

    assert result == {}
    assert transport.calls == [
        (
            "sys_rename",
            {"old_path": "/workspace/old.txt", "new_path": "/workspace/new.txt", "force": False},
        )
    ]
