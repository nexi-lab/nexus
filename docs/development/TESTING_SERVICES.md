# Testing Strategy for Phase 2 Services

**Related:** [PHASE_2_PROGRESS.md](PHASE_2_PROGRESS.md)
**Branch:** `refactor/phase-2-core-refactoring`

This document outlines the testing strategy for the 9 service layer services extracted during Phase 2 refactoring.

---

## Testing Philosophy

**Test at 3 Levels:**
1. **Unit Tests** - Test services in isolation with mocked dependencies
2. **Integration Tests** - Test services with real components (database, filesystem)
3. **End-to-End Tests** - Test via FastAPI server (RPC calls)

**Key Principles:**
- ✅ Test async methods with `@pytest.mark.asyncio`
- ✅ Mock external dependencies (database, network, filesystem)
- ✅ Use fixtures from `tests/unit/conftest.py` for database isolation
- ✅ Test both success and error paths
- ✅ Test permission enforcement
- ✅ Test edge cases and validation

---

## Test Structure

```
tests/
├── unit/
│   └── services/              # ← New directory for service tests
│       ├── __init__.py
│       ├── conftest.py        # Service-specific fixtures
│       ├── test_search_service.py
│       ├── test_rebac_service.py
│       ├── test_mount_service.py
│       ├── test_version_service.py
│       ├── test_mcp_service.py
│       ├── test_llm_service.py
│       ├── test_oauth_service.py
│       └── test_skill_service.py
├── integration/
│   └── services/              # ← Integration tests for services
│       ├── test_search_integration.py
│       ├── test_rebac_integration.py
│       └── ...
└── conftest.py                # Global fixtures (database isolation, etc.)
```

---

## Unit Testing Pattern

### 1. Basic Service Test Template

```python
"""Tests for ServiceName."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from nexus.services.service_name import ServiceName
from nexus.core.permissions import OperationContext


class TestServiceName:
    """Test ServiceName functionality."""

    @pytest.fixture
    def mock_dependencies(self):
        """Create mock dependencies for service."""
        return {
            "metadata_store": MagicMock(),
            "cas_store": MagicMock(),
            "permission_enforcer": MagicMock(),
        }

    @pytest.fixture
    def service(self, mock_dependencies):
        """Create service instance with mocked dependencies."""
        return ServiceName(**mock_dependencies)

    @pytest.fixture
    def context(self):
        """Create operation context for tests."""
        return OperationContext(
            user="test_user",
            groups=["test_group"],
            zone_id="test_tenant"
        )

    # ========================================================================
    # Success Path Tests
    # ========================================================================

    @pytest.mark.asyncio
    async def test_method_success(self, service, context):
        """Test method with valid inputs."""
        # Arrange
        service.dependency.method.return_value = "expected_result"

        # Act
        result = await service.method("arg", context=context)

        # Assert
        assert result == "expected_result"
        service.dependency.method.assert_called_once_with("arg")

    # ========================================================================
    # Error Path Tests
    # ========================================================================

    @pytest.mark.asyncio
    async def test_method_not_found(self, service, context):
        """Test method with non-existent resource."""
        # Arrange
        service.dependency.method.side_effect = FileNotFoundError()

        # Act & Assert
        with pytest.raises(FileNotFoundError):
            await service.method("nonexistent", context=context)

    @pytest.mark.asyncio
    async def test_method_permission_denied(self, service, context):
        """Test method with insufficient permissions."""
        # Arrange
        service.permission_enforcer.check_permission.return_value = False

        # Act & Assert
        with pytest.raises(PermissionError):
            await service.method("path", context=context)

    # ========================================================================
    # Edge Cases
    # ========================================================================

    @pytest.mark.asyncio
    async def test_method_empty_input(self, service, context):
        """Test method with empty input."""
        with pytest.raises(ValueError, match="cannot be empty"):
            await service.method("", context=context)

    @pytest.mark.asyncio
    async def test_method_none_context(self, service):
        """Test method with None context."""
        # Some methods should handle None context (permissive mode)
        result = await service.method("arg", context=None)
        assert result is not None
```

### 2. Testing Async Methods

All service methods are async, so use `@pytest.mark.asyncio`:

```python
import pytest

class TestAsyncService:
    @pytest.mark.asyncio
    async def test_async_method(self, service):
        """Test async service method."""
        result = await service.async_method("arg")
        assert result == "expected"
```

### 3. Mocking Dependencies

Use `unittest.mock` for mocking:

```python
from unittest.mock import AsyncMock, MagicMock, patch

# Mock async methods
mock_rebac = AsyncMock()
mock_rebac.rebac_check.return_value = True

# Mock sync methods
mock_metadata = MagicMock()
mock_metadata.get_file.return_value = {"path": "/test.txt"}

# Patch module-level imports
@patch("nexus.services.service_name.SomeDependency")
async def test_with_patch(self, mock_dep):
    mock_dep.return_value.method.return_value = "result"
    # Test code here
```

### 4. Testing with Real Database

Use `isolated_db` fixture from `tests/unit/conftest.py`:

```python
import pytest
from nexus.storage.metadata_store import SQLAlchemyMetadataStore

class TestServiceWithDB:
    @pytest.mark.asyncio
    async def test_with_real_db(self, isolated_db):
        """Test with real database."""
        # Create real metadata store
        metadata = SQLAlchemyMetadataStore(db_path=str(isolated_db))

        # Create service with real dependencies
        service = VersionService(
            metadata_store=metadata,
            cas_store=MagicMock(),  # Can still mock some deps
        )

        # Test code here

        # Cleanup
        metadata.close()
```

---

## Integration Testing Pattern

Integration tests use real components:

```python
"""Integration tests for ServiceName."""

import pytest
from pathlib import Path

from nexus.services.service_name import ServiceName
from nexus.storage.metadata_store import SQLAlchemyMetadataStore
from nexus.storage.cas_store import CASStore


class TestServiceNameIntegration:
    """Integration tests for ServiceName."""

    @pytest.fixture
    def temp_dir(self, tmp_path):
        """Create temporary directory for test files."""
        return tmp_path / "test_data"

    @pytest.fixture
    def metadata_store(self, isolated_db):
        """Create real metadata store."""
        store = SQLAlchemyMetadataStore(db_path=str(isolated_db))
        yield store
        store.close()

    @pytest.fixture
    def cas_store(self, temp_dir):
        """Create real CAS store."""
        cas_dir = temp_dir / "cas"
        cas_dir.mkdir(parents=True)
        return CASStore(cas_path=str(cas_dir))

    @pytest.fixture
    def service(self, metadata_store, cas_store):
        """Create service with real dependencies."""
        return ServiceName(
            metadata_store=metadata_store,
            cas_store=cas_store,
            enforce_permissions=False,  # Disable for integration tests
        )

    @pytest.mark.asyncio
    async def test_end_to_end_workflow(self, service, context):
        """Test complete workflow with real components."""
        # Create a file
        result1 = await service.create("test.txt", b"content", context=context)
        assert result1["path"] == "test.txt"

        # Read it back
        content = await service.read("test.txt", context=context)
        assert content == b"content"

        # Update it
        result2 = await service.update("test.txt", b"new content", context=context)
        assert result2["version"] == 2

        # List versions
        versions = await service.list_versions("test.txt", context=context)
        assert len(versions) == 2
```

---

## End-to-End Testing Pattern

E2E tests use FastAPI server:

```python
"""End-to-end tests for ServiceName via RPC."""

import pytest
from httpx import AsyncClient

from nexus.server.fastapi_server import create_app


class TestServiceNameE2E:
    """E2E tests for ServiceName via FastAPI."""

    @pytest.fixture
    async def client(self):
        """Create test client."""
        app = create_app()
        async with AsyncClient(app=app, base_url="http://test") as client:
            yield client

    @pytest.mark.asyncio
    async def test_rpc_call(self, client):
        """Test service method via RPC."""
        response = await client.post(
            "/rpc/service_method",
            json={
                "path": "/test.txt",
                "version": 2,
            },
            headers={"Authorization": "Bearer test_token"}
        )

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
```

---

## Testing Checklist

For each service method, ensure tests cover:

### ✅ Success Paths
- [ ] Valid inputs produce expected outputs
- [ ] Method called with correct parameters
- [ ] Return values have correct structure
- [ ] Side effects occur (database writes, etc.)

### ✅ Error Paths
- [ ] Invalid inputs raise ValueError
- [ ] Missing resources raise NotFoundError
- [ ] Permission denied raises PermissionError
- [ ] Database errors are handled gracefully

### ✅ Edge Cases
- [ ] Empty strings/lists/dicts
- [ ] None values
- [ ] Very large inputs
- [ ] Concurrent access (race conditions)
- [ ] Resource limits (max file size, etc.)

### ✅ Permission Enforcement
- [ ] System context bypasses checks
- [ ] Admin context bypasses checks
- [ ] User context enforces permissions
- [ ] None context behavior (permissive or strict?)

### ✅ Validation
- [ ] Path validation (absolute paths, no traversal)
- [ ] Input sanitization
- [ ] Type checking
- [ ] Range checking (versions, sizes, etc.)

---

## Running Tests

### Run All Service Tests
```bash
pytest tests/unit/services/ -v
```

### Run Specific Service Tests
```bash
pytest tests/unit/services/test_version_service.py -v
```

### Run With Coverage
```bash
pytest tests/unit/services/ --cov=nexus.services --cov-report=html
```

### Run Only Fast Tests (Skip Integration)
```bash
pytest tests/unit/services/ -m "not integration" -v
```

### Run Async Tests Only
```bash
pytest tests/unit/services/ -k "async" -v
```

---

## Example: Testing VersionService

See [tests/unit/services/test_version_service.py](tests/unit/services/test_version_service.py) for complete example.

**Key patterns:**
1. Mock CAS store for version content
2. Mock metadata store for version history
3. Test rollback creates new version (not destructive)
4. Test diff_versions with different modes
5. Test permission checks for read/write operations

---

## Next Steps

1. **Create `tests/unit/services/` directory**
2. **Create service-specific conftest.py** with common fixtures
3. **Start with small service** (VersionService or MCPService)
4. **Write tests alongside implementation extraction**
5. **Achieve >80% code coverage** for each service

---

## Resources

- **Pytest Docs:** https://docs.pytest.org/
- **Pytest-asyncio:** https://pytest-asyncio.readthedocs.io/
- **Unittest.mock:** https://docs.python.org/3/library/unittest.mock.html
- **Existing Tests:** [tests/unit/core/](tests/unit/core/) for reference patterns

---

**Last Updated:** 2026-01-03
