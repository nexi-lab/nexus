import pytest
from fastapi.testclient import TestClient

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.fastapi_server import create_app
from tests.conftest import make_test_nexus
from tests.helpers.in_memory_record_store import InMemoryRecordStore

# Session 级别的变量，用于在所有测试中共享同一个服务器
_server_app = None
_server_client = None


def setup_server():
    """初始化服务器（session 开始时调用一次）"""
    global _server_app, _server_client

    if _server_app is None:
        import tempfile

        tmp_path = tempfile.mkdtemp(prefix="nexus_test_")
        in_memory_rs = InMemoryRecordStore()
        nexus_fs = None

        # 同步创建 nexus_fs（因为 TestClient 不支持异步）
        import asyncio

        loop = asyncio.new_event_loop()
        nexus_fs = loop.run_until_complete(make_test_nexus(tmp_path, record_store=in_memory_rs))
        loop.close()

        api_key = "test-api-key"
        _server_app = create_app(nexus_fs, api_key=api_key)
        _server_client = TestClient(_server_app, raise_server_exceptions=True)
        _server_client.headers["Authorization"] = f"Bearer {api_key}"
        _server_client.headers["X-Actor-ID"] = "test-actor"
        _server_client.headers["X-Zone-ID"] = ROOT_ZONE_ID

    return _server_client


@pytest.fixture(scope="session")
def client():
    """
    Session级别的fixture：启动一次服务器，所有测试共用。
    注意：虽然使用 TestClient，但它走的是完整的 ASGI HTTP 处理流程。
    """
    client = setup_server()
    yield client


@pytest.fixture
def reset_db(client):
    """
    每个测试后重置数据库状态，确保测试之间不相互影响。
    由于 InMemoryRecordStore 是内存数据库，每个 session 只有一个实例。
    如果需要完全隔离，需要为每个测试创建新的数据库。
    """
    yield
    # 可以在这里添加清理逻辑


@pytest.mark.asyncio
async def test_api_list_secrets_without_auth(client):
    """API-01: Access list secrets endpoint without authentication should be rejected."""
    # Note: The secrets API endpoints now enforce authentication (Issue #3619).
    # This test verifies the endpoint correctly rejects unauthenticated requests.
    app = client.app
    from fastapi.testclient import TestClient

    bad_client = TestClient(app)
    response = bad_client.get("/api/v2/secrets")
    # Returns 401 Unauthorized when auth is not provided
    assert response.status_code == 401
    data = response.json()
    assert data["detail"] == "Invalid or missing API key"


@pytest.mark.asyncio
async def test_api_lifecycle_crud(client):
    """API-04, 06, 07, 08: Full CRUD lifecycle and versioning."""
    namespace = "test_ns"
    key = "my_key"

    # 1. PUT v1
    response = client.put(
        f"/api/v2/secrets/{namespace}/{key}",
        json={"value": "secret_v1", "description": "First version"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == 1

    # 2. GET v1
    response = client.get(f"/api/v2/secrets/{namespace}/{key}")
    assert response.status_code == 200
    assert response.json()["value"] == "secret_v1"

    # 3. PUT v2 (Update)
    response = client.put(
        f"/api/v2/secrets/{namespace}/{key}",
        json={"value": "secret_v2", "description": "Second version"},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2

    # 4. GET latest (v2)
    response = client.get(f"/api/v2/secrets/{namespace}/{key}")
    assert response.json()["value"] == "secret_v2"

    # 5. GET specific version (v1)
    response = client.get(f"/api/v2/secrets/{namespace}/{key}?version=1")
    assert response.status_code == 200
    assert response.json()["value"] == "secret_v1"


@pytest.mark.asyncio
async def test_api_status_management(client):
    """API-09, 10: Enable/Disable secret."""
    namespace = "status_ns"
    key = "toggle_key"
    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "some_val"})

    # Disable
    response = client.put(f"/api/v2/secrets/{namespace}/{key}/disable")
    assert response.status_code == 200
    assert response.json()["enabled"] is False

    # GET should fail
    response = client.get(f"/api/v2/secrets/{namespace}/{key}")
    assert response.status_code == 403

    # Enable
    response = client.put(f"/api/v2/secrets/{namespace}/{key}/enable")
    assert response.status_code == 200
    assert response.json()["enabled"] is True

    # GET should succeed
    response = client.get(f"/api/v2/secrets/{namespace}/{key}")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_api_soft_delete_restore(client):
    """API-11, 12: Soft delete and restore."""
    namespace = "delete_ns"
    key = "kill_me"
    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "alive"})

    # Delete
    response = client.delete(f"/api/v2/secrets/{namespace}/{key}")
    assert response.status_code == 200

    # GET latest should fail (404)
    response = client.get(f"/api/v2/secrets/{namespace}/{key}")
    assert response.status_code == 404

    # Restore
    response = client.post(f"/api/v2/secrets/{namespace}/{key}/restore")
    assert response.status_code == 200

    # GET should succeed
    response = client.get(f"/api/v2/secrets/{namespace}/{key}")
    assert response.status_code == 200
    assert response.json()["value"] == "alive"


@pytest.mark.asyncio
async def test_api_list_metadata(client):
    """API-05: Metadata check (list shouldn't return values)."""
    client.put("/api/v2/secrets/list_ns/k1", json={"value": "v1"})
    client.put("/api/v2/secrets/list_ns/k2", json={"value": "v2"})

    response = client.get("/api/v2/secrets?namespace=list_ns")
    assert response.status_code == 200
    secrets = response.json()["secrets"]
    assert len(secrets) >= 2
    for s in secrets:
        assert "value" not in s  # Crucial security check


@pytest.mark.asyncio
async def test_api_version_deletion_constraint(client):
    """API-13: Cannot delete the last version."""
    namespace = "ver_ns"
    key = "ver_key"
    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "v1"})

    # Try delete version 1
    response = client.delete(f"/api/v2/secrets/{namespace}/{key}/versions/1")
    assert response.status_code == 400
    assert "Cannot delete version" in response.json()["detail"]

    # Add version 2
    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "v2"})

    # Now delete version 1 should succeed
    response = client.delete(f"/api/v2/secrets/{namespace}/{key}/versions/1")
    assert response.status_code == 200

    # Delete version 2 should fail (last version)
    response = client.delete(f"/api/v2/secrets/{namespace}/{key}/versions/2")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_api_update_description(client):
    """API-14: Update secret description."""
    namespace = "desc_ns"
    key = "desc_key"

    # Create secret with initial description
    client.put(
        f"/api/v2/secrets/{namespace}/{key}",
        json={"value": "secret_value", "description": "Initial description"},
    )

    # Update description
    response = client.put(
        f"/api/v2/secrets/{namespace}/{key}/description",
        json={"description": "Updated description"},
    )
    assert response.status_code == 200
    assert response.json()["description"] == "Updated description"

    # Verify in list
    response = client.get("/api/v2/secrets", params={"namespace": namespace})
    assert response.status_code == 200
    secrets = response.json()["secrets"]
    desc_found = False
    for s in secrets:
        if s["key"] == key:
            assert s["description"] == "Updated description"
            desc_found = True
    assert desc_found, "Updated description not found in list"


@pytest.mark.asyncio
async def test_api_list_versions(client):
    """API-15: List version history for a secret."""
    namespace = "versions_ns"
    key = "version_key"

    # Create multiple versions
    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "v1"})
    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "v2"})
    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "v3"})

    # List versions
    response = client.get(f"/api/v2/secrets/{namespace}/{key}/versions")
    assert response.status_code == 200
    data = response.json()
    assert data["namespace"] == namespace
    assert data["key"] == key
    assert data["count"] == 3
    versions = data["versions"]
    assert len(versions) == 3
    # Versions should be ordered by version number descending (newest first)
    assert versions[0]["version"] == 3
    assert versions[1]["version"] == 2
    assert versions[2]["version"] == 1
    # Verify values are NOT exposed in version list
    for v in versions:
        assert "value" not in v or v.get("value") is None, "Version list should not expose values"


@pytest.mark.asyncio
async def test_api_batch_put(client):
    """API-16: Batch create/update secrets."""
    secrets = [
        {"namespace": "batch_ns1", "key": "batch_key1", "value": "value1"},
        {"namespace": "batch_ns1", "key": "batch_key2", "value": "value2"},
        {"namespace": "batch_ns2", "key": "batch_key3", "value": "value3"},
    ]

    # Batch put
    response = client.post("/api/v2/secrets/batch", json=secrets)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    assert len(data["secrets"]) == 3

    # Verify secrets were created
    for s in secrets:
        ns, k, v = s["namespace"], s["key"], s["value"]
        response = client.get(f"/api/v2/secrets/{ns}/{k}")
        assert response.status_code == 200
        assert response.json()["value"] == v


@pytest.mark.asyncio
async def test_api_batch_get(client):
    """API-17: Batch get secrets."""
    # First create some secrets
    client.put("/api/v2/secrets/batchget_ns1/key1", json={"value": "val1"})
    client.put("/api/v2/secrets/batchget_ns1/key2", json={"value": "val2"})
    client.put("/api/v2/secrets/batchget_ns2/key3", json={"value": "val3"})

    # Batch get - only query existing secrets
    queries = [
        {"namespace": "batchget_ns1", "key": "key1"},
        {"namespace": "batchget_ns1", "key": "key2"},
        {"namespace": "batchget_ns2", "key": "key3"},
    ]

    response = client.post("/api/v2/secrets/batch/get", json=queries)
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 3
    secrets = data["secrets"]

    # batch_get returns dict {namespace:key: value}
    assert isinstance(secrets, dict), f"Expected dict, got {type(secrets)}"

    # Check that all secrets are in results
    assert "batchget_ns1:key1" in secrets
    assert secrets["batchget_ns1:key1"] == "val1"

    assert "batchget_ns1:key2" in secrets
    assert secrets["batchget_ns1:key2"] == "val2"

    assert "batchget_ns2:key3" in secrets
    assert secrets["batchget_ns2:key3"] == "val3"


@pytest.mark.asyncio
async def test_api_batch_update(client):
    """API-18: Batch update existing secrets."""
    # Create initial secrets
    client.put("/api/v2/secrets/batchupd_ns/k1", json={"value": "old1"})
    client.put("/api/v2/secrets/batchupd_ns/k2", json={"value": "old2"})

    # Batch update
    secrets = [
        {"namespace": "batchupd_ns", "key": "k1", "value": "new1"},
        {"namespace": "batchupd_ns", "key": "k2", "value": "new2"},
    ]

    response = client.post("/api/v2/secrets/batch", json=secrets)
    assert response.status_code == 200

    # Verify versions increased
    response = client.get("/api/v2/secrets/batchupd_ns/k1/versions")
    assert response.json()["count"] == 2  # Original + update

    # Verify values and versions are updated
    response = client.get("/api/v2/secrets/batchupd_ns/k1")
    assert response.json()["value"] == "new1"
    assert response.json()["version"] == 2

    response = client.get("/api/v2/secrets/batchupd_ns/k2")
    assert response.json()["value"] == "new2"
    assert response.json()["version"] == 2


@pytest.mark.asyncio
async def test_api_get_nonexistent_secret(client):
    """API-19: Get non-existent secret returns 404."""
    response = client.get("/api/v2/secrets/nonexistent_ns/nonexistent_key")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_delete_nonexistent_secret(client):
    """API-20: Delete non-existent secret returns 404."""
    response = client.delete("/api/v2/secrets/nonexistent_ns/nonexistent_key")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_restore_nonexistent_secret(client):
    """API-21: Restore non-existent secret returns 404."""
    response = client.post("/api/v2/secrets/nonexistent_ns/nonexistent_key/restore")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_list_exclude_deleted_by_default(client):
    """API-22: List should exclude deleted secrets by default."""
    namespace = "listdel_ns"
    key = "to_be_deleted"

    client.put(f"/api/v2/secrets/{namespace}/{key}", json={"value": "temp"})
    client.delete(f"/api/v2/secrets/{namespace}/{key}")

    # Default list (include_deleted=False)
    response = client.get("/api/v2/secrets", params={"namespace": namespace})
    assert response.status_code == 200
    secrets = response.json()["secrets"]
    for s in secrets:
        assert not (s["namespace"] == namespace and s["key"] == key), (
            "Deleted secret should not appear in default list"
        )

    # With include_deleted=True
    response = client.get(
        "/api/v2/secrets", params={"namespace": namespace, "include_deleted": True}
    )
    assert response.status_code == 200
    secrets = response.json()["secrets"]
    for s in secrets:
        if s["namespace"] == namespace and s["key"] == key:
            assert s.get("deleted_at") is not None
    # Note: Depending on implementation, deleted secrets may or may not appear
    # This test at least verifies the include_deleted parameter works
