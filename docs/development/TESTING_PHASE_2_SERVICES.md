# Phase 2 Service Testing & Validation Plan

**Goal:** Ensure all extracted services work correctly before wiring into NexusFS

**Status:** 5/9 services implemented, minimal testing

---

## Testing Strategy

### ✅ Level 1: Static Analysis (DONE)
- [x] Type checking (mypy) - All services pass
- [x] Lint checks (ruff) - All services pass
- [x] Code formatting - All services formatted

### ⚠️ Level 2: Unit Tests (IN PROGRESS)
- [x] VersionService: 6/10 tests passing
- [ ] MCPService: 0 tests
- [ ] LLMService: 0 tests
- [ ] OAuthService: 0 tests
- [ ] SearchService: 0 tests

### ❌ Level 3: Integration Tests (NOT STARTED)
- [ ] Test services with real database
- [ ] Test services with real filesystem
- [ ] Test OAuth flows with mock providers
- [ ] Test MCP connections with test servers

### ❌ Level 4: End-to-End Tests (NOT STARTED)
- [ ] Wire services into NexusFS
- [ ] Test through RPC API
- [ ] Test backward compatibility

---

## Quick Validation (Do This First)

### 1. Fix VersionService Tests

```bash
# Update tests to expect implemented methods
cd /Users/jinjingzhou/nexi-lab/nexus
pytest tests/unit/services/test_version_service.py -v
```

**Expected:** 10/10 tests should pass after fixing expectations

### 2. Write Smoke Tests for Other Services

Create minimal tests to verify basic functionality:

```python
# tests/unit/services/test_oauth_service.py
@pytest.mark.asyncio
async def test_oauth_list_providers():
    """Smoke test: Can we list providers?"""
    service = OAuthService(oauth_config=mock_config)
    providers = await service.oauth_list_providers()
    assert isinstance(providers, list)
```

### 3. Manual Testing Checklist

Before wiring into NexusFS, manually test each service:

- [ ] VersionService: `list_versions()`, `get_version()`, `rollback()`
- [ ] MCPService: `mcp_list_mounts()`, `mcp_mount()`
- [ ] LLMService: `llm_read()` with mock provider
- [ ] OAuthService: `oauth_list_providers()`, `oauth_get_auth_url()`
- [ ] SearchService: `initialize_semantic_search()`, `semantic_search()`

---

## Risk Assessment

### 🟢 Low Risk (Likely Working)
- **VersionService**: 60% test coverage, simple CRUD operations
- **SearchService semantic methods**: Thin wrapper over AsyncSemanticSearch

### 🟡 Medium Risk (Need Testing)
- **MCPService**: Async wrapping of blocking operations - verify `asyncio.to_thread()`
- **LLMService**: SecretStr type handling - verify API key conversion

### 🔴 High Risk (Complex Logic)
- **OAuthService**:
  - PKCE flow with state management (340-line `mcp_connect()`)
  - Multi-zone credential isolation
  - Provider name mapping

**Recommendation:** Focus testing on OAuthService first.

---

## Testing Order (Priority)

1. **Fix VersionService tests** (15 min)
2. **Write OAuthService tests** (2 hours)
   - Test provider listing
   - Test PKCE flow
   - Test credential storage
3. **Write MCPService tests** (1 hour)
   - Test mount/unmount
   - Test async wrapping
4. **Write integration tests** (3 hours)
   - Test with real SQLite database
   - Test with temporary filesystem
5. **Wire into NexusFS & test** (2 hours)
   - Add service composition
   - Test backward compatibility

---

## Quick Win: Run Existing NexusFS Tests

Check if existing tests still pass with new services:

```bash
# Run all existing tests
pytest tests/ -v --tb=short

# Check for regressions
pytest tests/unit/core/ -v
```

If these pass, it means we haven't broken anything yet.

---

## Coverage Target

- **Unit Tests:** >80% line coverage per service
- **Integration Tests:** Cover all public API methods
- **E2E Tests:** Cover major user workflows

---

## Next Steps

1. **Immediate:** Fix VersionService tests and verify 10/10 pass
2. **Short-term:** Write smoke tests for other 4 services
3. **Medium-term:** Write comprehensive unit tests
4. **Long-term:** Integration and E2E testing

**Current Status:** We have type safety and lint checks ✅, but need runtime validation ⚠️

---

**Last Updated:** 2026-01-03
