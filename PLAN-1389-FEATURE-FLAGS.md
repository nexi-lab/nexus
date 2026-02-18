# Implementation Plan: Feature Flags for Deployment Modes (#1389)

## Agreed Decisions (all 16 issues resolved)

| # | Issue | Decision |
|---|-------|----------|
| 1 | Dead FeaturesConfig | Expand into DeploymentProfile enum + per-feature flags |
| 2 | No profile-to-brick mapping | Profile-to-brick mapping in frozen dataclass |
| 3 | NEXUS_MODE collision | New env var NEXUS_PROFILE (keep NEXUS_MODE for topology) |
| 4 | No runtime introspection | Add GET /api/v2/features endpoint |
| 5 | Scattered env var gates | Centralize into DeploymentProfile.enabled_bricks() |
| 6 | factory.py unconditional loading | Pass enabled_bricks to create_nexus_services() |
| 7 | KernelServices optionality | Already done — all fields Optional[T] = None |
| 8 | config.py imports server/ | Move OAuthConfig to nexus/config.py |
| 9 | Zero test coverage | TDD approach — tests first |
| 10 | No config import boundary test | Add config.py import boundary test |
| 11 | No E2E profile test | Add profile-specific E2E test |
| 12 | Override conflict behavior | Explicit override wins + log warning |
| 13 | Cold-start imports | Gated imports (comes free from #6) |
| 14 | Memory footprint | Add startup log with profile stats |
| 15 | No startup benchmark test | sys.modules check in test |
| 16 | Features endpoint caching | Compute once at startup, serve from app.state |

---

## Phase 1: Foundation — DeploymentProfile enum + config (Issues 1, 2, 3, 8)

### Step 1.1: Move OAuthConfig out of server/ (Issue 8)
- Move `OAuthConfig` class from `nexus/server/auth/oauth_config.py` to `nexus/auth_config.py` (or inline into `nexus/config.py`)
- Update all imports
- Add import boundary test for config.py (Issue 10)

### Step 1.2: Create DeploymentProfile enum
- File: `src/nexus/core/deployment_profile.py`
- Enum: `DeploymentProfile(StrEnum)` with values: `embedded`, `lite`, `full`, `cloud`
- Frozen dataclass: `ProfileConfig` mapping each profile to its enabled brick set
- Method: `DeploymentProfile.default_bricks() -> frozenset[str]`

### Step 1.3: Define brick catalog
- Brick names (strings): `storage`, `eventlog`, `namespace`, `agent_registry`, `permissions`, `cache`, `search`, `pay`, `llm`, `skills`, `sandbox`, `workflows`, `a2a`, `scheduler`, `discovery`, `mcp`, `observability`, `memory`, `uploads`
- Profile mappings:
  - `embedded`: {storage, eventlog}
  - `lite`: {storage, eventlog, namespace, agent_registry, permissions, cache, scheduler}
  - `full`: all bricks
  - `cloud`: all bricks + federation features

### Step 1.4: Wire into NexusConfig
- Add `profile: str = "full"` field to NexusConfig (default: full for backward compat)
- Add env var `NEXUS_PROFILE` to env_mapping in config.py
- Add validator: profile must be in DeploymentProfile enum values
- Expand FeaturesConfig to include all brick-level flags (not just the current 6)
- Implement override logic: profile sets defaults, individual flags override with warning

---

## Phase 2: Tests First (Issues 9, 10, 15)

### Step 2.1: test_deployment_profile.py (unit)
- Test enum values
- Test default brick sets per profile
- Test that `full` is superset of `lite`, `lite` is superset of `embedded`
- Test override behavior (explicit flag overrides profile default)
- Test override warning logging

### Step 2.2: test_config_import_boundary.py (unit)
- Test config.py does not import from nexus.server

### Step 2.3: test_factory_feature_gating.py (unit)
- Test factory skips search when profile=lite
- Test factory skips pay when profile=lite
- Test factory includes all when profile=full
- Test KernelServices has None for disabled bricks

### Step 2.4: test_lite_profile_startup.py (benchmark)
- Test that lite profile does NOT import search/pay/llm/skills modules (sys.modules check)
- Test startup time < 2 seconds

---

## Phase 3: Factory Gating (Issues 5, 6, 13)

### Step 3.1: Compute enabled_bricks at factory entry
- In `create_nexus_services()`, accept `profile: DeploymentProfile = DeploymentProfile.FULL`
- Compute effective brick set: profile defaults merged with explicit overrides
- Log: `INFO: Profile=lite, Enabled: [...], Skipped: [...]`

### Step 3.2: Gate each service section
- Wrap each service instantiation block in `if "brick_name" in enabled_bricks:`
- Move imports inside gated blocks
- Services NOT in enabled_bricks → field stays None in KernelServices

### Step 3.3: Gate lifespan modules
- `startup_search()`: check enabled_bricks
- `startup_services()`: check enabled_bricks per service
- `startup_observability()`: always-on (kernel-level)
- `startup_realtime()`: check enabled_bricks for event bus

---

## Phase 4: Runtime Introspection (Issues 4, 16)

### Step 4.1: Compute features_info at startup
- In lifespan, compute: `{"profile": "full", "mode": "standalone", "enabled_bricks": [...], "version": "..."}`
- Store in `app.state.features_info` (computed once)

### Step 4.2: Add GET /api/v2/features endpoint
- Returns app.state.features_info (O(1), no computation)
- No auth required (public discovery endpoint)

---

## Phase 5: Startup Logging (Issue 14)

### Step 5.1: Profile stats in factory
- After service creation, log enabled/skipped bricks
- Log RSS memory if available (psutil or resource module)

---

## Phase 6: E2E Validation (Issue 11)

### Step 6.1: Profile-specific E2E test
- Start server with NEXUS_PROFILE=lite
- Verify /api/v2/features returns correct profile
- Verify search endpoints return 404 or "feature disabled"
- Verify core endpoints (health, files) still work
- Start server with NEXUS_PROFILE=full
- Verify all endpoints work

### Step 6.2: Run full E2E with permissions enabled
- Validate logs show correct profile
- Verify no performance regression
