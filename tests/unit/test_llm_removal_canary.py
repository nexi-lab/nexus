"""Canary test: assert that removed LLM/RLM/Memory/ACE modules are gone.

Issue #2986: These modules were deliberately removed. This test prevents
accidental re-introduction.
"""

import importlib

import pytest


@pytest.mark.parametrize(
    "module_path",
    [
        "nexus.bricks.llm",
        "nexus.bricks.rlm",
        "nexus.bricks.memory",
        "nexus.services.ace",
        "nexus.services.ace_rpc_service",
        "nexus.services.llm_context_builder",
        "nexus.contracts.protocols.llm",
        "nexus.contracts.protocols.llm_provider",
        "nexus.contracts.protocols.adaptive_k",
        "nexus.services.subsystems.llm_subsystem",
    ],
)
def test_removed_module_does_not_exist(module_path: str) -> None:
    """Verify that deliberately removed modules cannot be imported.

    Stale empty directories (e.g. from prior checkouts) may create namespace
    packages that technically import but have no real content (__file__ is None).
    We treat those as "removed" too.
    """
    try:
        mod = importlib.import_module(module_path)
    except ModuleNotFoundError:
        return  # expected
    # Namespace packages from stale empty dirs have __file__ = None
    assert mod.__file__ is None, (
        f"{module_path} should be removed but is importable from {mod.__file__}"
    )
