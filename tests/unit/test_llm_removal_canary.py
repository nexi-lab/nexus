"""Canary test: assert that removed LLM/RLM/Memory/ACE modules are gone.

Issue #2986: These modules were deliberately removed. This test prevents
accidental re-introduction.
"""

import importlib

import pytest


@pytest.mark.parametrize(
    "module_path",
    [
        "nexus.services.ace_rpc_service",
        "nexus.services.llm_context_builder",
        "nexus.contracts.protocols.llm",
        "nexus.contracts.protocols.llm_provider",
        "nexus.contracts.protocols.adaptive_k",
        "nexus.services.subsystems.llm_subsystem",
    ],
)
def test_removed_module_does_not_exist(module_path: str) -> None:
    """Verify that deliberately removed modules cannot be imported."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_path)
