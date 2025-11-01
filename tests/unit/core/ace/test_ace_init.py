"""Tests for ACE __init__ module."""


def test_import_all_ace_components():
    """Test that all ACE components can be imported."""
    from nexus.core.ace import (
        ConsolidationEngine,
        Curator,
        FeedbackManager,
        LearningLoop,
        PlaybookManager,
        Reflector,
        TrajectoryManager,
    )

    assert ConsolidationEngine is not None
    assert Curator is not None
    assert FeedbackManager is not None
    assert LearningLoop is not None
    assert PlaybookManager is not None
    assert Reflector is not None
    assert TrajectoryManager is not None


def test_module_has_all():
    """Test that __all__ is defined correctly."""
    from nexus.core import ace

    assert hasattr(ace, "__all__")
    expected_exports = [
        "TrajectoryManager",
        "Reflector",
        "Curator",
        "PlaybookManager",
        "ConsolidationEngine",
        "FeedbackManager",
        "LearningLoop",
    ]

    for item in expected_exports:
        assert item in ace.__all__


def test_all_exports_are_importable():
    """Test that all exported items can be imported."""
    from nexus.core import ace

    for item in ace.__all__:
        assert hasattr(ace, item), f"{item} is in __all__ but not in module"
