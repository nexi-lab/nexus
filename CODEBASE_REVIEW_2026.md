# Nexus Codebase Review & Refactoring Plan (February 2026)

**Review Date:** February 9, 2026
**Reviewer:** Claude Code (Opus 4.6)
**Codebase Version:** v0.7.1.dev0
**Total LOC:** ~407,400 lines Python across 937 files

---

## Executive Summary

Comprehensive review of Nexus AI-native filesystem identified **4 critical issues** across Architecture, Code Quality, Testing, and Performance. All issues have approved solutions with detailed implementation plans.

**Total Effort:** 16-20 weeks (4-5 months)
**Calendar Time:** ~3-4 months with 2 developers in parallel
**Priority:** HIGH - Blocking Phase 2 service extraction and production readiness

---

## Review Methodology

- **Approach:** SMALL CHANGE (1 top issue per section)
- **Best Practices Research:** 2026 industry standards for AI storage, ReBAC, FastAPI, SQLAlchemy
- **User Preferences Applied:**
  - DRY is important (flag repetition aggressively) ✅
  - Well-tested code is non-negotiable ✅
  - "Engineered enough" (not under/over-engineered) ✅
  - Handle edge cases (thoughtfulness > speed) ✅
  - Explicit over clever ✅

---

## 🏗️ Issue #1: Architecture - NexusFS God Object

### Problem
**Location:** `src/nexus/core/nexus_fs.py:1-7608` (7,608 lines)

- **NexusFS** is a god object with 147 methods across 8+ mixins
- Total mixin code: ~12,000 lines across:
  - `nexus_fs_core.py` (4,170 lines)
  - `nexus_fs_rebac.py` (2,585 lines)
  - `nexus_fs_search.py` (2,803 lines)
  - Plus: ShareLinks, Mounts, OAuth, Skills, MCP, LLM, Events mixins

**Violations:**
- ❌ DRY: Logic duplicated between mixins and service layers
- ❌ Testing: Cannot test subsystems in isolation
- ❌ Unclear boundaries: Method Resolution Order (MRO) is non-obvious
- ❌ Tight coupling: Services depend on NexusFS mixins (circular import risk)

### Solution: ✅ Option A - Accelerate Service Extraction (Phase 2)

**What:** Complete ongoing service extraction from mixins
- Move all business logic into independent services
- Reduce NexusFS to thin coordinator/facade (~1,000 lines)
- Maintain backward compatibility via delegation

**Effort:** 6-8 weeks
**Risk:** Medium (breaking changes if not careful)
**Impact:** Dramatically improves testability, reduces coupling
**Priority:** HIGH

**Services to Extract:**
1. ✅ ReBACService (security-critical)
2. ✅ SearchService (core functionality)
3. ✅ LLMService, MCPService, OAuthService
4. ✅ SkillService, MemoryService, MountService

**Success Criteria:**
- [ ] NexusFS reduced to ~1,000 lines
- [ ] All 8+ mixins extracted to services
- [ ] Services independently testable
- [ ] Backward compatibility maintained
- [ ] All tests pass

---

## 📝 Issue #2: Code Quality - Monolithic models.py

### Problem
**Location:** `src/nexus/storage/models.py:1-4609` (174KB, 4,609 lines)

- **54 SQLAlchemy model classes** in one file
- **100+ DRY violations:**
  - `created_at` defined 44 times (identical code)
  - `zone_id` defined 34 times
  - 104 total timestamp fields (created_at/updated_at/deleted_at)

**Domains Mixed:**
- Filesystem: FilePathModel, DirectoryEntryModel, etc.
- Permissions: ReBACTupleModel, TigerCacheModel, etc.
- Memory/AI: MemoryModel, TrajectoryModel, EntityModel
- Auth: UserModel, APIKeyModel, OAuthCredentialModel
- Workflows: WorkflowModel, WorkflowExecutionModel
- Subscriptions: SubscriptionModel, PaymentTransactionMeta
- Sharing: ShareLinkModel, ShareLinkAccessLogModel
- Infrastructure: SandboxMetadataModel, MountConfigModel

**Impact:**
- Navigation nightmare (scroll 4,609 lines to find model)
- Merge conflicts (multiple devs touching same file)
- Testing difficulty (cannot test domains in isolation)
- Unclear boundaries (new devs confused about domain separation)

### Solution: ✅ Option A - Split by Domain + Create Base Mixins

**What:** Split into 8-10 domain-specific modules
```
src/nexus/storage/models/
├── __init__.py          # Re-export for backward compatibility
├── base.py              # Base + common mixins
├── filesystem.py        # File storage models
├── permissions.py       # ReBAC models
├── memory.py            # AI memory models
├── auth.py              # User/auth models
├── workflows.py         # Workflow models
├── subscriptions.py     # Billing models
├── sharing.py           # Share link models
└── infrastructure.py    # System config models
```

**Mixins to Create:**
- `TimestampMixin` (created_at, updated_at) - eliminates 44 duplications
- `ZoneIsolationMixin` (zone_id) - eliminates 34 duplications
- `SoftDeleteMixin` (deleted_at) - eliminates 26 duplications
- `UUIDMixin` (UUID primary key generation)

**Effort:** 3-4 weeks
**Risk:** Low (backward compatible via __init__.py)
**Impact:** Eliminates 100+ DRY violations, improves navigation
**Priority:** MEDIUM

**Success Criteria:**
- [ ] 10 domain files created (~400-600 lines each)
- [ ] 4 common mixins extracted
- [ ] Backward compatibility maintained (old imports work)
- [ ] 85+ alembic migrations updated
- [ ] All tests pass

**Detailed Plan:** See Phase 1 Track 1 below

---

## 🧪 Issue #3: Tests - Service Layer Missing Unit Tests

### Problem
**Location:** `src/nexus/services/*.py` vs `tests/unit/services/test_*.py`

- **13 service implementation files** (13,000+ lines)
- **Only 3 unit test files** for services
- **10 services with 0 unit tests:**
  1. **ReBACService** (1,378 lines) - SECURITY-CRITICAL ⚠️
  2. **SearchService** (678 lines) - Core functionality
  3. **LLMService** (520 lines)
  4. **MCPService, OAuthService, SkillService, MountService** (8 more)

**Impact:**
- Security risk: ReBACService has no unit tests (permission bugs could leak data)
- Regression risk: Cannot refactor services confidently
- Edge cases untested: No tests for error paths, null checks, boundary conditions
- Cannot test in isolation: Must rely on integration tests

### Solution: ✅ Option A - Prioritized Test Coverage (Security-First)

**What:** Write comprehensive unit tests prioritized by criticality
1. **ReBACService** (100+ tests) - permission checks, edge cases, error handling
2. **SearchService** (50+ tests) - query expansion, ranking, filters
3. **LLMService, MCPService, OAuthService** (30+ tests each)
4. **Remaining services** (20+ tests each)

**Total:** 300-400 new unit tests

**Effort:** 4-6 weeks
**Risk:** Low (pure test additions)
**Impact:** 100% service coverage, enables confident refactoring
**Priority:** HIGH

**Test Structure:**
```python
# tests/unit/services/test_rebac_service.py

@pytest.fixture
def mock_rebac_manager(mocker):
    """Mock EnhancedReBACManager dependency."""
    return mocker.Mock(spec=EnhancedReBACManager)

@pytest.fixture
def rebac_service(mock_rebac_manager):
    """Provide isolated ReBACService for testing."""
    return ReBACService(rebac_manager=mock_rebac_manager)

def test_rebac_check_grants_access_for_owner(rebac_service, mock_rebac_manager):
    """Test that owner always has access."""
    mock_rebac_manager.check.return_value = True
    result = rebac_service.rebac_check(...)
    assert result is True

def test_rebac_check_denies_access_for_unauthorized_user(rebac_service):
    """Test permission denial for non-owner."""
    # ... edge case test

def test_rebac_check_handles_null_subject_gracefully(rebac_service):
    """Test error handling for invalid input."""
    # ... error path test
```

**Success Criteria:**
- [ ] 300-400 new unit tests written
- [ ] 100% service coverage achieved
- [ ] All edge cases documented via tests
- [ ] Security-critical paths (ReBACService) fully tested
- [ ] Test suite runs in <5 minutes

---

## ⚡ Issue #4: Performance - PostgreSQL RecordStore SPOF

### Problem
**Location:** `src/nexus/storage/database.py` + `src/nexus/storage/sqlalchemy_metadata_store.py`

- **PostgreSQL RecordStore** is single point of failure
- **No read replicas, no failover** configured at application layer
- Handles all critical data:
  - ReBAC tuples (every permission check)
  - Users & authentication (every request)
  - Memory models (AI workloads)
  - Audit logs (write-heavy)
  - Workflows, version history

**Performance Impact:**
- Permission checks hit primary DB (no read replica)
- `list()` operations do batch checks but still hit primary
- Write contention: audit logs compete with read queries
- No caching layer for auth tokens
- Single PostgreSQL failure = complete outage

**Scalability:**
- Current: ~5K concurrent users max
- No horizontal scaling (single primary)

### Solution: ✅ Option A - Read Replicas + Connection Pooling

**What:** Set up PostgreSQL streaming replication + routing
- **1-2 read replicas** for read-heavy workloads
- **SQLAlchemy read/write routing** in connection pool
- **PgBouncer** for connection pooling (100-200 → 20 connections)
- Route reads to replicas, writes to primary

**Architecture:**
```python
class DatabaseConfig:
    primary_url: str              # Write operations
    replica_urls: list[str]       # Read operations (round-robin)

def get_engine(config: DatabaseConfig):
    primary = create_engine(config.primary_url, pool_size=20)
    replicas = [create_engine(url, pool_size=50) for url in config.replica_urls]
    return RoutedEngine(primary=primary, replicas=replicas)
```

**Effort:** 3-4 weeks
**Risk:** Medium (replication lag 100-500ms)
**Impact:** 2-5x faster permission checks, scales to 50K+ users
**Priority:** MEDIUM-HIGH

**Performance Gains:**
- Permission checks: 2-5x faster (no write contention)
- List operations: 30-50% faster (parallel reads)
- Concurrent users: 5K → 50K+
- Availability: Reads continue if primary is slow

**Success Criteria:**
- [ ] 1-2 PostgreSQL read replicas configured
- [ ] SQLAlchemy routing implemented (reads→replica, writes→primary)
- [ ] PgBouncer connection pooling added
- [ ] Replication lag monitored (<500ms)
- [ ] Load tests show 2-5x improvement
- [ ] Failover tested (manual intervention required)

**Optional Enhancement:** Combine with managed PostgreSQL HA (Cloud SQL/RDS) for automatic failover

---

## 📅 Implementation Roadmap

### Phase 1: Foundation (Weeks 1-6) — PARALLEL TRACKS

#### Track 1: Code Quality (Weeks 1-4)
- **Week 1:** Create directory structure + extract base mixins
  - Task 1.1: Create `models/` directory structure
  - Task 1.2: Extract Base class + 4 common mixins (`base.py`)
  - Task 1.3: Extract filesystem models (7 models → `filesystem.py`)
- **Week 2:** Extract permissions + memory models
  - Task 2.1: Extract permissions models (10 models → `permissions.py`)
  - Task 2.2: Extract memory models (8 models → `memory.py`)
- **Week 3:** Extract auth + workflows + remaining models
  - Task 3.1: Extract auth models (7 models → `auth.py`)
  - Task 3.2: Extract workflows, subscriptions, sharing, infrastructure
- **Week 4:** Backward compatibility + migration
  - Task 4.1: Create backward-compatible `__init__.py`
  - Task 4.2: Update 85+ alembic migrations (automated script)
  - Task 4.3: Deprecate old `models.py` → `models_deprecated.py`
  - Task 4.4: Integration testing

**Deliverables:**
- ✅ 10 domain-specific model files (8 domains + base + __init__)
- ✅ 4 common mixins (100+ DRY violations eliminated)
- ✅ 100% backward compatibility
- ✅ 85+ alembic migrations updated

#### Track 2: Test Infrastructure (Weeks 1-2)
- **Week 1:** Create service test fixtures
  - Task: Create `tests/unit/services/conftest.py` with mock fixtures
  - Fixtures: `mock_rebac_manager`, `mock_metadata_store`, `mock_backend`
- **Week 2:** ReBACService tests (PRIORITY)
  - Task: Write 100+ tests for ReBACService
  - Coverage: Happy paths, edge cases, error handling
  - Test categories: Permission checks, sharing, error paths

**Deliverables:**
- ✅ Service test fixtures created
- ✅ ReBACService 100% tested (100+ tests)

---

### Phase 2: Service Extraction + Testing (Weeks 7-12)

#### Track 1: Architecture (Weeks 7-12)
- **Weeks 7-8:** Extract SearchService from NexusFSSearchMixin
  - Move search logic to independent SearchService
  - Update NexusFS to delegate to SearchService
  - Maintain backward compatibility
- **Weeks 9-10:** Extract ReBACService fully
  - Move remaining ReBAC logic from mixin
  - Complete ReBACService extraction
- **Weeks 11-12:** Extract Skills, Memory, OAuth services
  - SkillService extraction
  - MemoryService extraction
  - OAuthService extraction
- **Goal:** Reduce NexusFS to ~1,000 lines (coordinator only)

**Deliverables:**
- ✅ NexusFS reduced from 7,608 → ~1,000 lines
- ✅ 6-8 services fully extracted
- ✅ Backward compatibility maintained

#### Track 2: Tests (Weeks 7-12) — PARALLEL
- **Week 7:** SearchService tests (50+ tests)
- **Weeks 8-9:** LLMService, MCPService, OAuthService tests (90+ tests)
- **Weeks 10-12:** Remaining service tests (150+ tests)
- **Goal:** 100% service coverage

**Deliverables:**
- ✅ 300-400 total unit tests
- ✅ 100% service coverage
- ✅ All edge cases documented

---

### Phase 3: Performance & Production Readiness (Weeks 13-16)

#### PostgreSQL HA (Weeks 13-16)
- **Week 13:** Set up PostgreSQL streaming replication
  - Configure primary DB for replication
  - Provision 1-2 read replicas
  - Test replication lag
- **Week 14:** Implement SQLAlchemy read/write routing
  - Create `RoutedEngine` class
  - Route reads to replicas, writes to primary
  - Handle replication lag with consistency modes
- **Week 15:** Add PgBouncer connection pooling
  - Install and configure PgBouncer
  - Update connection strings
  - Tune pool sizes
- **Week 16:** Test failover + load testing
  - Failover testing (kill primary, verify recovery)
  - Load test with 10K concurrent permission checks
  - Measure performance gains

**Deliverables:**
- ✅ 1-2 read replicas operational
- ✅ SQLAlchemy routing implemented
- ✅ PgBouncer connection pooling
- ✅ 2-5x performance improvement verified
- ✅ Failover tested

---

## 📊 Effort Summary

| Phase | Track | Duration | Effort | Priority |
|-------|-------|----------|--------|----------|
| 1 | Code Quality | 4 weeks | Medium | MEDIUM |
| 1 | Test Infrastructure | 2 weeks | Low | HIGH |
| 2 | Service Extraction | 6 weeks | High | HIGH |
| 2 | Service Testing | 6 weeks | Medium | HIGH |
| 3 | PostgreSQL HA | 4 weeks | Medium | MEDIUM-HIGH |

**Total Calendar Time:** 16 weeks (4 months) with 2 developers in parallel
**Total Effort:** 22 person-weeks

**Team Recommendations:**
- **Developer 1:** Code Quality (Weeks 1-4) → Service Extraction (Weeks 7-12)
- **Developer 2:** Test Infrastructure (Weeks 1-2) → Service Testing (Weeks 7-12)
- **DevOps:** PostgreSQL HA (Weeks 13-16, can overlap with Phase 2)

---

## 🎯 Success Criteria (Overall)

### Architecture
- [ ] NexusFS reduced to ~1,000 lines (from 7,608)
- [ ] 6-8 services fully extracted
- [ ] All services independently testable
- [ ] Backward compatibility maintained
- [ ] No regression in functionality

### Code Quality
- [ ] 100+ DRY violations eliminated
- [ ] 54 models split into 10 domain files
- [ ] All imports still work (backward compatible)
- [ ] Alembic migrations updated and tested
- [ ] Code review approved

### Tests
- [ ] 300-400 new unit tests written
- [ ] 100% service layer coverage
- [ ] Security-critical paths (ReBACService) fully tested
- [ ] All edge cases documented
- [ ] Test suite runs in <5 minutes

### Performance
- [ ] 2-5x faster permission checks
- [ ] 30-50% faster list operations
- [ ] Scales to 50K+ concurrent users
- [ ] Replication lag <500ms
- [ ] Failover tested

---

## 📚 Best Practices References

### Architecture & Design
- [Best AI Storage Systems: Top 5 Options in 2026](https://cloudian.com/guides/ai-infrastructure/best-ai-storage-systems-top-5-options-in-2026/)
- [Understanding ReBAC and Google Zanzibar](https://medium.com/@mehmet.tosun/understanding-rebac-and-google-zanzibar-a-practical-implementation-in-net-core-bff20a012e45)
- [Monorepo Guide: Manage Repositories & Microservices](https://www.aviator.co/blog/monorepo-a-hands-on-guide-for-managing-repositories-and-microservices/)
- [Building Scalable Microservices with Python FastAPI](https://medium.com/@kanishk.khatter/building-scalable-microservices-with-python-fastapi-design-and-best-practices-0dd777141b29)

### Code Quality
- [FastAPI DDD Example](https://github.com/NEONKID/fastapi-ddd-example)
- [Domain Model with SQLAlchemy](https://blog.szymonmiks.pl/p/domain-model-with-sqlalchemy/)
- [Mastering SQLAlchemy: Comprehensive Guide](https://medium.com/@ramanbazhanau/mastering-sqlalchemy-a-comprehensive-guide-for-python-developers-ddb3d9f2e829)

### Performance
- [Solving the N+1 Query Problem: 30s to <1s](https://medium.com/@nkangprecious26/solving-the-n-1-query-problem-how-i-reduced-api-response-time-from-30s-to-1s-1fcd819c34e6)
- [SQLAlchemy Performance FAQ](https://docs.sqlalchemy.org/en/21/faq/performance.html)

---

## 📝 Notes & Decisions

### Architectural Decisions
1. **Service Extraction Approach:** Gradual extraction over microservices (modular monolith)
   - **Rationale:** Lower risk, maintains backward compatibility, "engineered enough"
   - **Alternative Considered:** Service mesh (rejected as over-engineered)

2. **Models Split Strategy:** Domain-driven design with backward compatibility
   - **Rationale:** Aligns with DDD best practices, maintains existing imports
   - **Alternative Considered:** Repository pattern (rejected as too complex for first pass)

3. **Testing Strategy:** Prioritized by criticality (security-first)
   - **Rationale:** ReBACService is security-critical and must be tested first
   - **Alternative Considered:** AI-generated tests (considered as supplement only)

4. **Performance Approach:** Read replicas over distributed cache
   - **Rationale:** Solves SPOF, scales reads, standard PostgreSQL practice
   - **Alternative Considered:** Redis cache (rejected due to invalidation complexity)

### Open Questions
- [ ] Should we combine PostgreSQL read replicas with managed HA (Cloud SQL/RDS)?
  - **Recommendation:** Yes, for production readiness (automatic failover)
  - **Effort:** +1-2 weeks
  - **Cost:** $200-500/month

- [ ] Should we use AI (Copilot/Claude) to accelerate test writing?
  - **Recommendation:** Yes, for test scaffolding (human review required)
  - **Time Savings:** 30-40%

---

## 🔄 Status Tracking

**Current Status:** Planning Complete ✅
**Next Action:** Begin Phase 1 Track 1 (models.py split)
**Last Updated:** February 9, 2026

### Progress Checklist

#### Phase 1 (Weeks 1-6)
- [ ] Week 1: Base mixins extracted
- [ ] Week 2: Permissions + memory models split
- [ ] Week 3: Auth + workflows + remaining models split
- [ ] Week 4: Backward compatibility + integration tests
- [ ] Week 1-2 (parallel): Service test fixtures + ReBACService tests

#### Phase 2 (Weeks 7-12)
- [ ] Weeks 7-8: SearchService extracted
- [ ] Weeks 9-10: ReBACService fully extracted
- [ ] Weeks 11-12: Skills, Memory, OAuth services extracted
- [ ] Weeks 7-12 (parallel): All service tests completed

#### Phase 3 (Weeks 13-16)
- [ ] Week 13: PostgreSQL replication configured
- [ ] Week 14: SQLAlchemy routing implemented
- [ ] Week 15: PgBouncer connection pooling
- [ ] Week 16: Load testing + failover validation

---

## 📞 Contact & Review

**PM:** Tao Feng
**AI Developer:** Claude Opus 4.6 (via Claude Code)
**Review Method:** Interactive (1 top issue per section)

**Questions or Feedback:**
- GitHub Issues: https://github.com/nexi-lab/nexus/issues
- Open a PR with prefix: `[REFACTOR]`

---

**End of Codebase Review & Refactoring Plan**
