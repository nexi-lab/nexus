"""Tests for HookEngine mutating/validating phases + failure policies (Issue #2064).

TDD: RED tests written first, then implementation to make them GREEN.

Tests cover:
  1. Mutating -> Validating ordering
  2. FailurePolicy.FAIL on timeout and exception
  3. FailurePolicy.IGNORE backward compatibility
  4. Context threading through mutating chain
  5. Validating hooks see mutated context
  6. POST hook failure policy
  7. failure_mode field in HookResult
  8. HookPhaseType default derivation
  9. Mixed mutating + validating + POST in one fire()
  10. Backward compatibility (existing hooks without phase_type)
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus.plugins.async_hooks import AsyncHookEngine
from nexus.plugins.hooks import PluginHooks
from nexus.services.protocols.hook_engine import (
    POST_WRITE,
    PRE_WRITE,
    FailurePolicy,
    HookCapabilities,
    HookContext,
    HookPhaseType,
    HookResult,
    HookSpec,
)
from nexus.system_services.lifecycle.hook_engine import ScopedHookEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(*, default_timeout_ms: int = 5000) -> ScopedHookEngine:
    """Create a ScopedHookEngine wrapping fresh PluginHooks."""
    inner = AsyncHookEngine(inner=PluginHooks())
    return ScopedHookEngine(inner=inner, default_timeout_ms=default_timeout_ms)


def _ctx(
    phase: str = PRE_WRITE,
    agent_id: str | None = None,
    path: str = "/test.txt",
    payload: dict[str, Any] | None = None,
) -> HookContext:
    return HookContext(
        phase=phase,
        path=path,
        zone_id="zone1",
        agent_id=agent_id,
        payload=payload or {},
    )


async def _noop(ctx: HookContext) -> HookResult:
    return HookResult(proceed=True, modified_context=None, error=None)


async def _veto(ctx: HookContext) -> HookResult:
    return HookResult(proceed=False, modified_context=None, error="vetoed")


async def _error_handler(ctx: HookContext) -> HookResult:
    raise RuntimeError("hook exploded")


async def _slow_handler(ctx: HookContext) -> HookResult:
    await asyncio.sleep(100)
    return HookResult(proceed=True, modified_context=None, error=None)


# ---------------------------------------------------------------------------
# 1. Mutating -> Validating ordering
# ---------------------------------------------------------------------------


class TestMutatingValidatingOrdering:
    """Verify mutating hooks always run before validating hooks."""

    @pytest.mark.asyncio
    async def test_mutating_runs_before_validating(self) -> None:
        """Mutating hooks (regardless of priority) execute before validating hooks."""
        engine = _make_engine()
        order: list[str] = []

        async def _mutating(ctx: HookContext) -> HookResult:
            order.append("mutating")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _validating(ctx: HookContext) -> HookResult:
            order.append("validating")
            return HookResult(proceed=True, modified_context=None, error=None)

        # Register validating FIRST (higher priority) — should still run AFTER mutating
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="val",
                priority=100,
                phase_type=HookPhaseType.VALIDATING,
            ),
            _validating,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="mut",
                priority=0,
                phase_type=HookPhaseType.MUTATING,
            ),
            _mutating,
        )

        await engine.fire(PRE_WRITE, _ctx())
        assert order == ["mutating", "validating"]

    @pytest.mark.asyncio
    async def test_multiple_mutating_then_multiple_validating(self) -> None:
        """Multiple mutating hooks run in priority order, then multiple validating."""
        engine = _make_engine()
        order: list[str] = []

        async def _track(name: str) -> None:
            order.append(name)

        async def _m1(ctx: HookContext) -> HookResult:
            await _track("m1")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _m2(ctx: HookContext) -> HookResult:
            await _track("m2")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _v1(ctx: HookContext) -> HookResult:
            await _track("v1")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _v2(ctx: HookContext) -> HookResult:
            await _track("v2")
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="m1", priority=10, phase_type=HookPhaseType.MUTATING
            ),
            _m1,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="m2", priority=5, phase_type=HookPhaseType.MUTATING
            ),
            _m2,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="v1", priority=20, phase_type=HookPhaseType.VALIDATING
            ),
            _v1,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="v2", priority=1, phase_type=HookPhaseType.VALIDATING
            ),
            _v2,
        )

        await engine.fire(PRE_WRITE, _ctx())
        assert order == ["m1", "m2", "v1", "v2"]

    @pytest.mark.asyncio
    async def test_mutating_veto_skips_validating(self) -> None:
        """If a mutating hook vetoes, validating hooks don't run."""
        engine = _make_engine()
        validating_ran = False

        async def _val(ctx: HookContext) -> HookResult:
            nonlocal validating_ran
            validating_ran = True
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="mut_veto", phase_type=HookPhaseType.MUTATING),
            _veto,
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="val", phase_type=HookPhaseType.VALIDATING),
            _val,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert not validating_ran

    @pytest.mark.asyncio
    async def test_only_validating_hooks_still_work(self) -> None:
        """When only validating hooks are registered (no mutating), they still execute."""
        engine = _make_engine()

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="val", phase_type=HookPhaseType.VALIDATING),
            _veto,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert result.error == "vetoed"


# ---------------------------------------------------------------------------
# 2. FailurePolicy.FAIL on timeout and exception
# ---------------------------------------------------------------------------


class TestFailurePolicyFail:
    """Verify FailurePolicy.FAIL aborts operation on error/timeout."""

    @pytest.mark.asyncio
    async def test_fail_policy_on_timeout(self) -> None:
        """Handler with failure_policy=FAIL that times out returns proceed=False."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="slow_fail",
                capabilities=HookCapabilities(
                    max_timeout_ms=50,
                    failure_policy=FailurePolicy.FAIL,
                ),
            ),
            _slow_handler,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert result.failure_mode == "timeout"
        assert "timed out" in (result.error or "")

    @pytest.mark.asyncio
    async def test_fail_policy_on_exception(self) -> None:
        """Handler with failure_policy=FAIL that raises returns proceed=False."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="error_fail",
                capabilities=HookCapabilities(failure_policy=FailurePolicy.FAIL),
            ),
            _error_handler,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert result.failure_mode == "error"
        assert "hook exploded" in (result.error or "")

    @pytest.mark.asyncio
    async def test_fail_policy_validating_on_error(self) -> None:
        """Validating hook with failure_policy=FAIL that raises aborts."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="val_error",
                phase_type=HookPhaseType.VALIDATING,
                capabilities=HookCapabilities(failure_policy=FailurePolicy.FAIL),
            ),
            _error_handler,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert result.failure_mode == "error"


# ---------------------------------------------------------------------------
# 3. FailurePolicy.IGNORE backward compatibility
# ---------------------------------------------------------------------------


class TestFailurePolicyIgnore:
    """Verify FailurePolicy.IGNORE (default) maintains backward compat."""

    @pytest.mark.asyncio
    async def test_ignore_policy_on_timeout(self) -> None:
        """Default: handler that times out is skipped (proceed=True)."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="slow_ignore",
                capabilities=HookCapabilities(max_timeout_ms=50),
            ),
            _slow_handler,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_ignore_policy_on_exception(self) -> None:
        """Default: handler that raises is skipped (proceed=True)."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="error_ignore"),
            _error_handler,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_default_failure_policy_is_ignore(self) -> None:
        """HookCapabilities default failure_policy is IGNORE."""
        caps = HookCapabilities()
        assert caps.failure_policy == FailurePolicy.IGNORE


# ---------------------------------------------------------------------------
# 4. Context threading through mutating chain
# ---------------------------------------------------------------------------


class TestContextThreading:
    """Verify mutating hooks thread modified_context to next hook."""

    @pytest.mark.asyncio
    async def test_second_mutating_sees_first_mutation(self) -> None:
        """Second mutating hook receives payload from first mutation."""
        engine = _make_engine()
        seen_payloads: list[dict[str, Any]] = []

        async def _mut1(ctx: HookContext) -> HookResult:
            seen_payloads.append(dict(ctx.payload))
            return HookResult(
                proceed=True,
                modified_context={"step": 1, "added_by_mut1": True},
                error=None,
            )

        async def _mut2(ctx: HookContext) -> HookResult:
            seen_payloads.append(dict(ctx.payload))
            return HookResult(
                proceed=True,
                modified_context={"step": 2, **ctx.payload},
                error=None,
            )

        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="mut1", priority=10, phase_type=HookPhaseType.MUTATING
            ),
            _mut1,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="mut2", priority=5, phase_type=HookPhaseType.MUTATING
            ),
            _mut2,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is True

        # mut1 sees original empty payload
        assert seen_payloads[0] == {}
        # mut2 sees mut1's modified context
        assert seen_payloads[1]["step"] == 1
        assert seen_payloads[1]["added_by_mut1"] is True

    @pytest.mark.asyncio
    async def test_three_mutating_hooks_chain(self) -> None:
        """Three mutating hooks each see accumulated context."""
        engine = _make_engine()
        seen: list[dict[str, Any]] = []

        async def _add_a(ctx: HookContext) -> HookResult:
            seen.append(dict(ctx.payload))
            return HookResult(proceed=True, modified_context={**ctx.payload, "a": 1}, error=None)

        async def _add_b(ctx: HookContext) -> HookResult:
            seen.append(dict(ctx.payload))
            return HookResult(proceed=True, modified_context={**ctx.payload, "b": 2}, error=None)

        async def _add_c(ctx: HookContext) -> HookResult:
            seen.append(dict(ctx.payload))
            return HookResult(proceed=True, modified_context={**ctx.payload, "c": 3}, error=None)

        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="a", priority=30, phase_type=HookPhaseType.MUTATING
            ),
            _add_a,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="b", priority=20, phase_type=HookPhaseType.MUTATING
            ),
            _add_b,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="c", priority=10, phase_type=HookPhaseType.MUTATING
            ),
            _add_c,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is True

        # Hook A sees {}
        assert seen[0] == {}
        # Hook B sees {a: 1}
        assert seen[1] == {"a": 1}
        # Hook C sees {a: 1, b: 2}
        assert seen[2] == {"a": 1, "b": 2}
        # Final modified_context has all three
        assert result.modified_context == {"a": 1, "b": 2, "c": 3}

    @pytest.mark.asyncio
    async def test_non_modifying_mutating_does_not_break_chain(self) -> None:
        """A mutating hook that doesn't modify passes original context through."""
        engine = _make_engine()
        seen: list[dict[str, Any]] = []

        async def _modify(ctx: HookContext) -> HookResult:
            seen.append(dict(ctx.payload))
            return HookResult(proceed=True, modified_context={"x": 1}, error=None)

        async def _passthrough(ctx: HookContext) -> HookResult:
            seen.append(dict(ctx.payload))
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _check(ctx: HookContext) -> HookResult:
            seen.append(dict(ctx.payload))
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="mod", priority=30, phase_type=HookPhaseType.MUTATING
            ),
            _modify,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE, handler_name="pass", priority=20, phase_type=HookPhaseType.MUTATING
            ),
            _passthrough,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="check",
                priority=10,
                phase_type=HookPhaseType.MUTATING,
            ),
            _check,
        )

        await engine.fire(PRE_WRITE, _ctx())

        # All three should see {x: 1} since passthrough doesn't change context
        assert seen[0] == {}  # mod sees original
        assert seen[1] == {"x": 1}  # passthrough sees mod's output
        assert seen[2] == {"x": 1}  # check sees same (passthrough didn't modify)


# ---------------------------------------------------------------------------
# 5. Validating hooks see mutated context
# ---------------------------------------------------------------------------


class TestValidatingSeesModifiedContext:
    """Verify validating hooks receive the final mutated context."""

    @pytest.mark.asyncio
    async def test_validating_sees_mutated_payload(self) -> None:
        """Validating hook payload contains changes from mutating hook."""
        engine = _make_engine()
        seen_by_validator: dict[str, Any] = {}

        async def _mutate(ctx: HookContext) -> HookResult:
            return HookResult(
                proceed=True,
                modified_context={"injected": "by_mutator", "original_path": ctx.path},
                error=None,
            )

        async def _validate(ctx: HookContext) -> HookResult:
            seen_by_validator.update(ctx.payload)
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="mut", phase_type=HookPhaseType.MUTATING),
            _mutate,
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="val", phase_type=HookPhaseType.VALIDATING),
            _validate,
        )

        await engine.fire(PRE_WRITE, _ctx())
        assert seen_by_validator["injected"] == "by_mutator"
        assert seen_by_validator["original_path"] == "/test.txt"

    @pytest.mark.asyncio
    async def test_validating_rejects_after_mutation(self) -> None:
        """Validating hook can reject based on mutated content."""
        engine = _make_engine()

        async def _inject_bad_content(ctx: HookContext) -> HookResult:
            return HookResult(
                proceed=True,
                modified_context={"size_mb": 500},  # over limit
                error=None,
            )

        async def _check_size(ctx: HookContext) -> HookResult:
            if ctx.payload.get("size_mb", 0) > 100:
                return HookResult(
                    proceed=False,
                    modified_context=None,
                    error="File too large after mutation",
                )
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="inject", phase_type=HookPhaseType.MUTATING),
            _inject_bad_content,
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="check", phase_type=HookPhaseType.VALIDATING),
            _check_size,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert "too large" in (result.error or "")


# ---------------------------------------------------------------------------
# 6. POST hook failure policy
# ---------------------------------------------------------------------------


class TestPostHookFailurePolicy:
    """Verify POST hooks respect FailurePolicy.FAIL."""

    @pytest.mark.asyncio
    async def test_post_hook_fail_policy_on_error(self) -> None:
        """POST hook with failure_policy=FAIL that errors returns proceed=False."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=POST_WRITE,
                handler_name="post_fail",
                capabilities=HookCapabilities(failure_policy=FailurePolicy.FAIL),
            ),
            _error_handler,
        )

        result = await engine.fire(POST_WRITE, _ctx(phase=POST_WRITE))
        assert result.proceed is False
        assert result.failure_mode == "error"

    @pytest.mark.asyncio
    async def test_post_hook_ignore_policy_on_error(self) -> None:
        """POST hook with failure_policy=IGNORE (default) continues on error."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(phase=POST_WRITE, handler_name="post_ignore"),
            _error_handler,
        )

        result = await engine.fire(POST_WRITE, _ctx(phase=POST_WRITE))
        assert result.proceed is True

    @pytest.mark.asyncio
    async def test_post_hook_fail_policy_on_timeout(self) -> None:
        """POST hook with failure_policy=FAIL that times out returns proceed=False."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=POST_WRITE,
                handler_name="post_slow_fail",
                capabilities=HookCapabilities(
                    max_timeout_ms=50,
                    failure_policy=FailurePolicy.FAIL,
                ),
            ),
            _slow_handler,
        )

        result = await engine.fire(POST_WRITE, _ctx(phase=POST_WRITE))
        assert result.proceed is False
        assert result.failure_mode == "timeout"


# ---------------------------------------------------------------------------
# 7. failure_mode field in HookResult
# ---------------------------------------------------------------------------


class TestFailureMode:
    """Verify failure_mode field is correctly set."""

    @pytest.mark.asyncio
    async def test_explicit_veto_sets_failure_mode(self) -> None:
        """Handler that explicitly vetoes has failure_mode='veto'."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="veto"),
            _veto,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert result.failure_mode == "veto"

    @pytest.mark.asyncio
    async def test_timeout_fail_sets_failure_mode_timeout(self) -> None:
        """Timeout with FAIL policy sets failure_mode='timeout'."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="slow",
                capabilities=HookCapabilities(
                    max_timeout_ms=50,
                    failure_policy=FailurePolicy.FAIL,
                ),
            ),
            _slow_handler,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.failure_mode == "timeout"

    @pytest.mark.asyncio
    async def test_error_fail_sets_failure_mode_error(self) -> None:
        """Exception with FAIL policy sets failure_mode='error'."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="err",
                capabilities=HookCapabilities(failure_policy=FailurePolicy.FAIL),
            ),
            _error_handler,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.failure_mode == "error"

    @pytest.mark.asyncio
    async def test_proceed_has_no_failure_mode(self) -> None:
        """Successful hook has failure_mode=None."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="ok"),
            _noop,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is True
        assert result.failure_mode is None

    def test_hookresult_failure_mode_field_exists(self) -> None:
        """HookResult has failure_mode field."""
        r = HookResult(proceed=True, modified_context=None, error=None)
        assert r.failure_mode is None

        r2 = HookResult(proceed=False, modified_context=None, error="x", failure_mode="veto")
        assert r2.failure_mode == "veto"


# ---------------------------------------------------------------------------
# 8. HookPhaseType default derivation
# ---------------------------------------------------------------------------


class TestPhaseTypeDefaults:
    """Verify default phase_type behavior for hooks without explicit phase_type."""

    @pytest.mark.asyncio
    async def test_legacy_hook_defaults_to_mutating(self) -> None:
        """Hook without phase_type in PRE phase behaves as mutating (can modify + veto)."""
        engine = _make_engine()

        async def _modify(ctx: HookContext) -> HookResult:
            return HookResult(
                proceed=True,
                modified_context={"legacy": True},
                error=None,
            )

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="legacy"),  # no phase_type
            _modify,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is True
        assert result.modified_context == {"legacy": True}

    @pytest.mark.asyncio
    async def test_legacy_hook_can_still_veto(self) -> None:
        """Hook without phase_type can still veto (backward compat)."""
        engine = _make_engine()
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="legacy_veto"),
            _veto,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False

    @pytest.mark.asyncio
    async def test_legacy_runs_before_explicit_validating(self) -> None:
        """Legacy hooks (default MUTATING) run before explicit VALIDATING."""
        engine = _make_engine()
        order: list[str] = []

        async def _legacy(ctx: HookContext) -> HookResult:
            order.append("legacy")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _explicit_val(ctx: HookContext) -> HookResult:
            order.append("validating")
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="val", phase_type=HookPhaseType.VALIDATING),
            _explicit_val,
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="legacy"),  # no phase_type
            _legacy,
        )

        await engine.fire(PRE_WRITE, _ctx())
        assert order == ["legacy", "validating"]


# ---------------------------------------------------------------------------
# 9. Mixed scenarios
# ---------------------------------------------------------------------------


class TestMixedScenarios:
    """Complex scenarios mixing mutating, validating, POST, and failure policies."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """Complete lifecycle: mutating modifies, validating approves, POST fires."""
        engine = _make_engine()
        phases: list[str] = []

        async def _mut(ctx: HookContext) -> HookResult:
            phases.append("mutating")
            return HookResult(
                proceed=True,
                modified_context={"mutated": True},
                error=None,
            )

        async def _val(ctx: HookContext) -> HookResult:
            phases.append("validating")
            assert ctx.payload.get("mutated") is True
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _post(ctx: HookContext) -> HookResult:
            phases.append("post")
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="mut", phase_type=HookPhaseType.MUTATING), _mut
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="val", phase_type=HookPhaseType.VALIDATING), _val
        )
        await engine.register_hook(HookSpec(phase=POST_WRITE, handler_name="post"), _post)

        # Fire PRE phase
        pre_result = await engine.fire(PRE_WRITE, _ctx())
        assert pre_result.proceed is True
        assert pre_result.modified_context == {"mutated": True}

        # Fire POST phase
        post_result = await engine.fire(POST_WRITE, _ctx(phase=POST_WRITE))
        assert post_result.proceed is True

        assert phases == ["mutating", "validating", "post"]

    @pytest.mark.asyncio
    async def test_fail_policy_mutating_skips_rest(self) -> None:
        """Mutating hook with FAIL policy that errors skips validating and rest."""
        engine = _make_engine()
        ran: list[str] = []

        async def _error_mut(ctx: HookContext) -> HookResult:
            ran.append("error_mut")
            raise RuntimeError("boom")

        async def _good_mut(ctx: HookContext) -> HookResult:
            ran.append("good_mut")
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _val(ctx: HookContext) -> HookResult:
            ran.append("val")
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="error_mut",
                priority=10,
                phase_type=HookPhaseType.MUTATING,
                capabilities=HookCapabilities(failure_policy=FailurePolicy.FAIL),
            ),
            _error_mut,
        )
        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="good_mut",
                priority=5,
                phase_type=HookPhaseType.MUTATING,
            ),
            _good_mut,
        )
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="val", phase_type=HookPhaseType.VALIDATING),
            _val,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is False
        assert result.failure_mode == "error"
        # Only the failing mutating hook ran; rest were skipped
        assert ran == ["error_mut"]

    @pytest.mark.asyncio
    async def test_validating_cannot_modify_context(self) -> None:
        """Validating hooks that return modified_context have it stripped."""
        engine = _make_engine()

        async def _sneaky_val(ctx: HookContext) -> HookResult:
            return HookResult(
                proceed=True,
                modified_context={"sneaky": True},
                error=None,
            )

        await engine.register_hook(
            HookSpec(
                phase=PRE_WRITE,
                handler_name="sneaky",
                phase_type=HookPhaseType.VALIDATING,
            ),
            _sneaky_val,
        )

        result = await engine.fire(PRE_WRITE, _ctx())
        assert result.proceed is True
        # modified_context should be None (stripped from validating hook)
        assert result.modified_context is None


# ---------------------------------------------------------------------------
# 10. Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Ensure pre-#2064 behavior is preserved for existing hooks."""

    @pytest.mark.asyncio
    async def test_hookspec_without_phase_type(self) -> None:
        """HookSpec without phase_type still works."""
        spec = HookSpec(phase=PRE_WRITE, handler_name="legacy")
        assert spec.phase_type is None

    @pytest.mark.asyncio
    async def test_hookresult_without_failure_mode(self) -> None:
        """HookResult can be created without failure_mode (defaults to None)."""
        r = HookResult(proceed=True, modified_context=None, error=None)
        assert r.failure_mode is None

    @pytest.mark.asyncio
    async def test_hookcapabilities_default_policy_is_ignore(self) -> None:
        """Default HookCapabilities has failure_policy=IGNORE."""
        caps = HookCapabilities()
        assert caps.failure_policy == FailurePolicy.IGNORE

    @pytest.mark.asyncio
    async def test_existing_pre_hook_still_sequential(self) -> None:
        """Pre-hooks without phase_type still execute sequentially in priority order."""
        engine = _make_engine()
        order: list[int] = []

        async def _p10(ctx: HookContext) -> HookResult:
            order.append(10)
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _p0(ctx: HookContext) -> HookResult:
            order.append(0)
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(HookSpec(phase=PRE_WRITE, handler_name="low", priority=0), _p0)
        await engine.register_hook(
            HookSpec(phase=PRE_WRITE, handler_name="high", priority=10), _p10
        )

        await engine.fire(PRE_WRITE, _ctx())
        assert order == [10, 0]

    @pytest.mark.asyncio
    async def test_existing_post_hooks_still_concurrent(self) -> None:
        """Post-hooks without phase_type still execute concurrently."""
        engine = _make_engine()
        entered = asyncio.Event()
        both = asyncio.Event()
        count = 0

        async def _a(ctx: HookContext) -> HookResult:
            nonlocal count
            count += 1
            if count >= 2:
                both.set()
            entered.set()
            await asyncio.wait_for(both.wait(), timeout=2.0)
            return HookResult(proceed=True, modified_context=None, error=None)

        async def _b(ctx: HookContext) -> HookResult:
            nonlocal count
            count += 1
            if count >= 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=2.0)
            return HookResult(proceed=True, modified_context=None, error=None)

        await engine.register_hook(HookSpec(phase=POST_WRITE, handler_name="a"), _a)
        await engine.register_hook(HookSpec(phase=POST_WRITE, handler_name="b"), _b)

        result = await engine.fire(POST_WRITE, _ctx(phase=POST_WRITE))
        assert result.proceed is True
        assert both.is_set()
