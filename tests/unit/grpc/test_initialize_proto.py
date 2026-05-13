from nexus.grpc import initialize_pb2
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc


def test_initialize_messages_exist() -> None:
    request = initialize_pb2.InitializeRequest(
        client_name="pytest",
        client_version="0.0",
        protocol_version="0.1.0",
    )
    response = initialize_pb2.InitializeResponse(
        server_name="nexus",
        server_version="0.10.0",
        protocol_version="0.1.0",
        capabilities=initialize_pb2.Capabilities(
            posix=initialize_pb2.PosixCapabilities(read=True, stat=True),
            extensions=["x-nexus:versioning"],
        ),
    )

    assert request.client_name == "pytest"
    assert response.capabilities.posix.read is True
    assert "x-nexus:versioning" in response.capabilities.extensions


def test_posix_capabilities_preserve_scalar_presence() -> None:
    partial = initialize_pb2.PosixCapabilities(read=True)
    parsed_partial = initialize_pb2.PosixCapabilities()
    parsed_partial.ParseFromString(partial.SerializeToString())

    assert parsed_partial.HasField("read") is True
    assert parsed_partial.HasField("write") is False

    explicit_false = initialize_pb2.PosixCapabilities(write=False)
    parsed_false = initialize_pb2.PosixCapabilities()
    parsed_false.ParseFromString(explicit_false.SerializeToString())

    assert parsed_false.HasField("write") is True
    assert parsed_false.write is False


def test_nexus_vfs_stub_has_initialize() -> None:
    assert hasattr(vfs_pb2_grpc.NexusVFSServiceStub, "__init__")
    service = vfs_pb2.DESCRIPTOR.services_by_name["NexusVFSService"]
    assert "Initialize" in service.methods_by_name
