# Refactoring Task Tracker

Quick reference for daily task tracking. See [CODEBASE_REVIEW_2026.md](../CODEBASE_REVIEW_2026.md) for full details.

---

## 🚀 Phase 1: Foundation (Weeks 1-6)

### Track 1: Code Quality - Split models.py (Weeks 1-4)

#### Week 1: Setup + Base Mixins
- [ ] **Day 1:** Create `src/nexus/storage/models/` directory structure
- [ ] **Day 1-2:** Extract base mixins to `base.py` (TimestampMixin, ZoneIsolationMixin, SoftDeleteMixin, UUIDMixin)
- [ ] **Day 2:** Extract filesystem models to `filesystem.py` (7 models)
- [ ] **Day 2:** Test: `python -c "from nexus.storage.models.filesystem import FilePathModel"`

#### Week 2: Permissions + Memory
- [ ] **Day 3-4:** Extract permissions models to `permissions.py` (10 models)
- [ ] **Day 4-5:** Extract memory models to `memory.py` (8 models)
- [ ] **Day 5:** Test: `python -c "from nexus.storage.models.permissions import ReBACTupleModel"`

#### Week 3: Auth + Remaining Models
- [ ] **Day 6-7:** Extract auth models to `auth.py` (7 models)
- [ ] **Day 7-8:** Extract workflows models to `workflows.py` (4 models)
- [ ] **Day 8-9:** Extract subscriptions models to `subscriptions.py` (5 models)
- [ ] **Day 9-10:** Extract sharing models to `sharing.py` (2 models)
- [ ] **Day 10:** Extract infrastructure models to `infrastructure.py` (9 models)

#### Week 4: Backward Compatibility
- [ ] **Day 11:** Create backward-compatible `__init__.py` (re-export all models)
- [ ] **Day 12-13:** Run automated migration import updater: `python scripts/update_migration_imports.py`
- [ ] **Day 13:** Verify alembic: `alembic check && alembic history`
- [ ] **Day 14:** Deprecate old models.py → models_deprecated.py
- [ ] **Day 15:** Integration testing: `pytest tests/integration/test_models_split.py -v`

**Checkpoint:** All imports work, alembic passes, tests green ✅

---

### Track 2: Test Infrastructure (Weeks 1-2)

#### Week 1: Service Test Fixtures
- [ ] **Day 1-2:** Create `tests/unit/services/conftest.py`
- [ ] **Day 2-3:** Write fixtures: `mock_rebac_manager`, `mock_metadata_store`, `mock_backend`
- [ ] **Day 3:** Create example test: `test_smoke.py` (verify fixtures work)

#### Week 2: ReBACService Tests (PRIORITY - Security Critical)
- [ ] **Day 4-5:** Write happy path tests (30 tests)
  - `test_rebac_check_grants_access_for_owner`
  - `test_rebac_create_tuple_success`
  - `test_share_with_user_creates_tuple`
- [ ] **Day 6-7:** Write edge case tests (40 tests)
  - `test_rebac_check_denies_unauthorized`
  - `test_rebac_check_handles_null_subject`
  - `test_rebac_check_handles_invalid_zone`
- [ ] **Day 8-9:** Write error handling tests (30 tests)
  - `test_rebac_check_handles_db_failure`
  - `test_rebac_create_handles_duplicate_tuple`
  - `test_share_with_user_validates_permissions`
- [ ] **Day 10:** Run tests: `pytest tests/unit/services/test_rebac_service.py -v --cov`

**Checkpoint:** ReBACService 100% tested (100+ tests) ✅

---

## 🔧 Phase 2: Service Extraction + Testing (Weeks 7-12)

### Track 1: Service Extraction (Weeks 7-12)

#### Weeks 7-8: Extract SearchService
- [ ] Move `nexus_fs_search.py` logic to `services/search_service.py`
- [ ] Update NexusFS to delegate: `self.search_service.search(...)`
- [ ] Test backward compatibility: existing search calls still work
- [ ] 50+ tests for SearchService

#### Weeks 9-10: Extract ReBACService Fully
- [ ] Move remaining `nexus_fs_rebac.py` logic to `services/rebac_service.py`
- [ ] Update NexusFS to delegate: `self.rebac_service.check_permission(...)`
- [ ] Test backward compatibility
- [ ] Additional tests for remaining methods

#### Weeks 11-12: Extract Remaining Services
- [ ] SkillService extraction (20+ tests)
- [ ] MemoryService extraction (20+ tests)
- [ ] OAuthService extraction (20+ tests)
- [ ] MCPService, LLMService, MountService extraction (60+ tests)

**Checkpoint:** NexusFS reduced to ~1,000 lines, services tested ✅

---

## ⚡ Phase 3: Performance - PostgreSQL HA (Weeks 13-16)

### Week 13: PostgreSQL Replication
- [ ] **Day 1-2:** Configure primary DB for replication (streaming replication)
- [ ] **Day 3-4:** Provision 1-2 read replicas (Cloud SQL/RDS or self-hosted)
- [ ] **Day 5:** Test replication lag: verify <500ms

### Week 14: SQLAlchemy Routing
- [ ] **Day 1-2:** Implement `RoutedEngine` class in `src/nexus/storage/database.py`
- [ ] **Day 3:** Route reads to replicas, writes to primary
- [ ] **Day 4-5:** Add consistency mode support (AT_LEAST_AS_FRESH for read-after-write)

### Week 15: Connection Pooling
- [ ] **Day 1-2:** Install and configure PgBouncer
- [ ] **Day 3:** Update connection strings to use PgBouncer
- [ ] **Day 4-5:** Tune pool sizes (primary: 20, replicas: 50)

### Week 16: Testing & Validation
- [ ] **Day 1-2:** Failover testing (kill primary, verify recovery)
- [ ] **Day 3-4:** Load test with 10K concurrent permission checks
- [ ] **Day 5:** Measure performance gains (target: 2-5x improvement)

**Checkpoint:** 2-5x faster reads, 50K+ user capacity ✅

---

## 📊 Quick Status

**Current Phase:** Planning Complete ✅
**Next Up:** Phase 1 Track 1 Week 1 (Create models/ directory)

**Progress:**
- Phase 1: ⬜⬜⬜⬜⬜⬜ 0/6 weeks
- Phase 2: ⬜⬜⬜⬜⬜⬜ 0/6 weeks
- Phase 3: ⬜⬜⬜⬜ 0/4 weeks

**Team:**
- Dev 1: Available for Code Quality → Service Extraction
- Dev 2: Available for Test Infrastructure → Service Testing
- DevOps: Available for PostgreSQL HA (Week 13+)

---

## 🔗 Quick Links

- **Full Plan:** [CODEBASE_REVIEW_2026.md](../CODEBASE_REVIEW_2026.md)
- **Issues:** https://github.com/nexi-lab/nexus/issues
- **CI Status:** Check GitHub Actions
- **Test Coverage:** Run `pytest --cov=nexus --cov-report=html`

---

**Last Updated:** February 9, 2026
