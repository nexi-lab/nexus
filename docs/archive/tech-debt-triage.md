# Tech Debt Triage Report

**Generated:** 2026-02-11
**Scope:** `/Users/taofeng/stream2/src/nexus/`
**Branch:** `feat/stream2`

---

## Summary Statistics

### `type: ignore` Comments

| Directory | Count | Risk Level |
|-----------|------:|------------|
| core/ | 118 | CRITICAL -- security-critical, permissions, ReBAC |
| remote/ (client + async_client) | 268 | LOW -- all `no-any-return` on RPC wrappers |
| cli/ | 71 | LOW -- CLI glue code, `attr-defined` on dynamic attrs |
| server/ | 32 | HIGH -- API surface, auth, RPC |
| fuse/ | 16 | MEDIUM -- optional import guards |
| workflows/ | 13 | MEDIUM -- abstract class instantiation |
| cache/ | 12 | MEDIUM -- optional redis imports |
| search/ | 10 | LOW -- optional ML library imports |
| raft/ | 7 | LOW -- optional import guards |
| backends/ | 7 | MEDIUM -- optional yaml, session factory |
| sync.py | 4 | MEDIUM -- type narrowing |
| skills/ | 3 | LOW |
| services/ | 2 | LOW |
| llm/ | 2 | LOW -- litellm API compat |
| connectors/ | 2 | LOW |
| migrations/ | 2 | LOW |
| storage/ | 0 | -- |
| sandbox/ | 0 | -- |
| portability/ | 0 | -- |
| **TOTAL** | **569** | |

### TODO / FIXME / HACK / XXX Comments

| Pattern | Count |
|---------|------:|
| TODO | 107 |
| FIXME | 1 (false positive -- in docstring example) |
| HACK | 0 |
| XXX | 3 (2 are bug fix issue placeholders, 1 is docstring) |
| **TOTAL** | **111** |

**TODO by directory:**

| Directory | Count | Risk Level |
|-----------|------:|------------|
| services/ | 32 | HIGH -- rebac_service has 21 stub TODOs |
| server/ | 22 | CRITICAL -- 10 in auth/database_local.py |
| core/ | 16 | HIGH -- permissions, ReBAC, admin checks |
| cli/ | 13 | LOW -- feature stubs |
| backends/ | 4 | LOW |
| portability/ | 3 | MEDIUM -- permissions not actually imported |
| mcp/ | 4 | LOW |
| llm/ | 4 | MEDIUM -- metrics not persisted |
| skills/ | 4 | LOW -- docstring examples |
| search/ | 1 | LOW |
| sandbox/ | 1 | LOW |
| connectors/ | 2 | LOW -- SKILL.md placeholders |
| sdk/ | 1 | LOW -- docstring example |

---

## Top 20 Most Dangerous Items

### CRITICAL -- Fix Now

**1. Missing email verification on login (auth bypass risk)**
- File: `src/nexus/server/auth/database_local.py:353`
- Comment: `# TODO: Check email_verified before allowing login to sensitive operations`
- Impact: Users can log in without verifying email ownership. An attacker could register with someone else's email and gain access.
- The email_verified field is always set to 0 and never enforced.

**2. Email verification not implemented (auth gap)**
- File: `src/nexus/server/auth/database_local.py:546-559`
- Comment: `# TODO: Implement email verification` -- `verify_email()` raises `NotImplementedError`
- Impact: No mechanism exists to verify email ownership. Combined with item 1, this is a complete gap in email-based identity assurance.

**3. Password reset not implemented (operational risk)**
- File: `src/nexus/server/auth/database_local.py:561-587`
- Comment: `# TODO: Implement password reset` -- both `request_password_reset()` and `reset_password()` raise `NotImplementedError`
- Impact: Users who forget passwords have no self-service recovery. Operational burden and potential for insecure workarounds.

**4. Missing admin check on backfill_directory_index (privilege escalation)**
- File: `src/nexus/core/nexus_fs.py:6480`
- Comment: `# TODO: Add admin check when context is provided`
- Impact: Any authenticated user could potentially trigger directory index backfill, which is an admin-only operation. Could be used to discover paths or consume resources.

**5. ReBAC conditions and expiry not checked (authorization bypass)**
- File: `src/nexus/core/rebac_manager_enhanced.py:5029`
- Comment: `# TODO: Check conditions and expiry if needed`
- Impact: Permission tuples with expiry dates or conditions may grant access indefinitely. Time-limited shares and conditional permissions are not enforced.

**6. Audit logging not persisted to database (compliance gap)**
- File: `src/nexus/server/auth/token_manager.py:519`
- Comment: `# TODO: Implement proper audit logging to database`
- Impact: Auth operations are only logged to application logger (volatile). No durable audit trail for security investigations or compliance.

**7. Permissions import silently counts but never writes (data loss)**
- File: `src/nexus/portability/import_service.py:483`
- Comment: `# TODO: Actually write to ReBAC when API is available`
- Impact: Importing a zone backup counts permissions but never restores them. Users who restore from backup lose all access control silently.

### HIGH -- Fix Soon

**8. `type: ignore[attr-defined]` on `_enforce_permissions` (38 occurrences in nexus_fs_core.py)**
- File: `src/nexus/core/nexus_fs_core.py` (lines 915, 1617, 2430, 3366, 3487, 3594, 3720)
- Pattern: `if self._enforce_permissions:  # type: ignore[attr-defined]`
- Assessment: These suppress checks on a security-critical attribute. The attribute is defined on the NexusFS class (mixin composition), so the ignores are structurally benign but mask the fact that the type checker cannot verify permission enforcement flow. If the attribute were accidentally removed or renamed, the `type: ignore` would hide the breakage, silently disabling all permission checks.

**9. zone_id always None in login responses (multi-tenant isolation)**
- File: `src/nexus/server/auth/database_local.py:312,366`
- Comment: `"zone_id": None,  # TODO: Get from ReBAC groups`
- Impact: Users get no zone context after login. Zone isolation depends on subsequent API calls to resolve zone, but this creates a window where zone is unset.

**10. zone_id always None in OAuth login (multi-tenant isolation)**
- File: `src/nexus/server/auth/oauth_user_auth.py:192`
- Comment: `"zone_id": None,  # TODO: Get from ReBAC groups`
- Impact: Same as item 9 but for OAuth flow. Zone context is never populated from ReBAC during authentication.

**11. Groups always empty in RPC auth context (group-based permissions broken)**
- File: `src/nexus/server/rpc_server.py:424`
- Comment: `groups=[],  # TODO: Extract groups from auth result if available`
- Impact: Group-based ReBAC permissions will never match because the auth context never populates group membership. Any permission granted to a group is effectively dead.

**12. ReBAC export placeholder (backup integrity)**
- File: `src/nexus/portability/export_service.py:350`
- Comment: `# TODO: Query ReBAC for all tuples related to this zone`
- Impact: Zone exports do not include ReBAC permissions. Combined with item 7, backup/restore is incomplete for access control.

**13. Bug fix issue placeholders (#XXX) in rebac_manager.py (untracked issues)**
- File: `src/nexus/core/rebac_manager.py:1687,4147`
- Comments: `# BUG FIX (Issue #XXX): Also invalidate Tiger Cache for the subject` and `# BUG FIX (Issue #XXX): ALWAYS invalidate L1 cache first`
- Impact: These are actual bug fixes that reference placeholder issue numbers. The fixes exist in code but are not tracked in the issue tracker, making them invisible to audit.

**14. agent_generation from server DB instead of JWT (stale session detection bypass)**
- File: `src/nexus/server/fastapi_server.py:707`
- Comment: `# TODO: In production, agent_generation should come from client JWT token`
- Impact: Agent session staleness detection is a no-op because both client and server read from the same DB. A compromised or revoked agent session cannot be detected.

**15. `type: ignore[override]` on rebac_write return type (interface contract violation)**
- File: `src/nexus/core/rebac_manager_enhanced.py:1498`
- Comment: `# type: ignore[override]  # Issue #1081: Returns WriteResult instead of str`
- Impact: The enhanced ReBAC manager returns a different type than its base class contract. Callers expecting `str` may get `WriteResult`, leading to subtle bugs in permission write operations.

### MEDIUM -- Plan Fix

**16. 21 stub TODOs in rebac_service.py (incomplete service extraction)**
- File: `src/nexus/services/rebac_service.py` (lines 973-1176)
- Pattern: `# TODO: Extract <method> implementation`
- Impact: The service layer delegates directly without proper extraction. This is a code organization issue but increases coupling and makes testing harder.

**17. Default zone creation not implemented (onboarding gap)**
- File: `src/nexus/server/auth/user_helpers.py:586`
- Comment: `# TODO: Implement default zone creation`
- Impact: New users with `auto_create=True` get an error instead of a default zone. Onboarding flow may break for first-time users.

**18. OAuth account listing/unlinking not implemented (feature gap)**
- File: `src/nexus/server/auth/auth_routes.py:1468,1491`
- Comments: `# TODO: Implement OAuth account listing` and `# TODO: Implement OAuth account unlinking`
- Impact: Users cannot view or manage their linked OAuth accounts. Returns 501. Security concern: users cannot unlink a compromised OAuth provider.

**19. LLM metrics not persisted (observability gap)**
- File: `src/nexus/llm/metrics.py:156,165,178,190`
- Comments: Multiple `# TODO: Implement actual database storage/retrieval`
- Impact: LLM usage metrics exist only in memory. Cost tracking, usage limits, and abuse detection have no durable storage.

**20. `type: ignore[assignment]` on SQL params in rebac_manager_enhanced.py**
- File: `src/nexus/core/rebac_manager_enhanced.py:3840,3859,3955`
- Pattern: `params = value_params  # type: ignore[assignment]`
- Impact: These suppress type mismatches in SQL parameter construction for ReBAC queries. While the runtime values are correct (lists of strings), the type suppression could mask SQL injection vectors if the code is modified without updating the types.

---

## Detailed Category Analysis

### `type: ignore` by Suppression Type

| Suppression Code | Count | Assessment |
|-----------------|------:|------------|
| `[no-any-return]` | ~350 | LOW -- Remote client wrappers returning `Any` from JSON-RPC. Structural pattern, not a real risk. |
| `[attr-defined]` | ~80 | MEDIUM to HIGH -- Mixin composition pattern. 38 in nexus_fs_core.py are on `_enforce_permissions` (security-critical). Others on `workflow_engine`, `subscription_manager`. |
| `[assignment]` | ~15 | MEDIUM -- Type narrowing issues, mostly in raft imports and SQL param building. |
| `[misc]` | ~20 | LOW -- Optional import fallbacks, sandbox overrides. |
| `[override]` | ~12 | MEDIUM -- Method signature mismatches in NexusFS mixins and sandbox methods. |
| `[arg-type]` | ~10 | MEDIUM -- Mostly LLM provider and dependency injection mismatches. |
| `[call-arg]` | ~8 | MEDIUM -- RPC server calling NexusFS methods with extra `context` kwarg. |
| `[abstract]` | ~3 | LOW -- Workflow trigger/action instantiation. |
| `[no-untyped-def]` | ~3 | LOW -- Callback functions in gmail connector and workflow engine. |
| `[union-attr]` | ~5 | LOW -- Optional event bus/lock manager access. |

### Security-Critical File Audit

| File | `type: ignore` | TODOs | Assessment |
|------|---------------:|------:|------------|
| `core/permissions.py` | 0 | 0 | Clean |
| `core/nexus_fs_rebac.py` | 0 | 0 | Clean |
| `core/rebac_manager.py` | 0 | 1 TODO, 2 XXX | **2 XXX are untracked bug fix issues** |
| `core/rebac_manager_enhanced.py` | 4 | 1 TODO | **override on rebac_write, assignment on SQL params, conditions/expiry not checked** |
| `core/nexus_fs_core.py` | 38 | 0 | **38 attr-defined on security attributes** |
| `server/auth/database_local.py` | 0 | 10 | **Email verification, password reset not implemented** |
| `server/auth/auth_routes.py` | 0 | 3 | **OAuth management not implemented** |
| `server/auth/token_manager.py` | 0 | 1 | **Audit logging not persisted** |
| `server/auth/user_helpers.py` | 0 | 3 | **Zone defaults, session preference not implemented** |
| `server/auth/oauth_user_auth.py` | 0 | 1 | **zone_id always None** |
| `server/auth/oauth_factory.py` | 1 | 0 | Benign -- `no-any-return` on factory method |
| `services/rebac_service.py` | 0 | 21 | **21 extraction stubs -- incomplete refactor** |
| `services/oauth_service.py` | 0 | 1 | Low -- future priority list |

---

## Recommendations

### Immediate Actions (Sprint 0)

1. **Implement email verification enforcement** (Items 1-2) -- Without this, email-based identity has no integrity. At minimum, add a config flag to require verification for sensitive operations.

2. **Fix ReBAC expiry/condition checking** (Item 5) -- Time-limited permissions are silently ignored. This is an authorization bypass.

3. **Add admin check to backfill_directory_index** (Item 4) -- Simple guard, high impact.

4. **File issue numbers for XXX placeholders** (Item 13) -- Replace `#XXX` with actual issue numbers for traceability.

### Short-Term (Next 2 Sprints)

5. **Implement password reset flow** (Item 3) -- Critical for operational security.

6. **Populate zone_id in auth responses** (Items 9-10) -- Required for proper multi-tenant isolation.

7. **Populate groups in RPC auth context** (Item 11) -- Without this, group-based permissions are dead code.

8. **Fix permissions import/export** (Items 7, 12) -- Backup/restore is incomplete without ReBAC data.

9. **Persist audit logs** (Item 6) -- Required for compliance and incident response.

### Medium-Term (Next Quarter)

10. **Refactor `_enforce_permissions` type: ignore pattern** (Item 8) -- Consider using Protocol classes or explicit attribute declarations in the mixin to eliminate the 38 `type: ignore[attr-defined]` suppressions on security-critical code paths.

11. **Complete rebac_service extraction** (Item 16) -- 21 stub TODOs indicate an incomplete refactor.

12. **Implement OAuth account management** (Item 18) -- Users need ability to unlink compromised providers.

13. **Address the 268 `type: ignore[no-any-return]` in remote clients** -- Consider adding proper return type generics to the RPC call methods, or using TypedDict for responses.

### Acceptable Tech Debt (LOW)

- Optional import guards in `raft/`, `fuse/`, `cache/`, `search/` -- Standard Python pattern for optional dependencies.
- CLI `attr-defined` suppressions -- Dynamic attribute access on NexusFS is expected in CLI glue code.
- Docstring examples containing `TODO`/`grep("TODO")` -- Not actual tech debt.
- `no-any-return` in remote clients -- Pervasive but low risk; the JSON-RPC layer handles type validation at the protocol level.

---

## Methodology

- All searches performed with ripgrep across `/Users/taofeng/stream2/src/nexus/`
- `type: ignore` counted with exact pattern `type: ignore`
- TODO/FIXME/HACK/XXX counted with word-boundary regex `\bPATTERN\b`
- Security-critical files examined with 3 lines of context
- Each `type: ignore` in security paths assessed as "dangerous" (suppresses real type mismatch) or "benign" (third-party/structural)
- TODOs in documentation/docstring examples excluded from risk assessment
