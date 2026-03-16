"""Tests for workflow protocol conformance."""

from nexus.bricks.workflows.engine import WorkflowEngine
from nexus.bricks.workflows.protocol import WorkflowProtocol, WorkflowServices


class TestWorkflowProtocol:
    """Test WorkflowProtocol conformance."""

    def test_engine_conforms_to_protocol(self):
        """Test that WorkflowEngine satisfies WorkflowProtocol."""
        engine = WorkflowEngine()
        assert isinstance(engine, WorkflowProtocol)

    def test_protocol_methods_exist(self):
        """Test that all protocol methods exist on engine."""
        engine = WorkflowEngine()
        assert hasattr(engine, "fire_event")
        assert hasattr(engine, "trigger_workflow")
        assert hasattr(engine, "load_workflow")
        assert hasattr(engine, "unload_workflow")
        assert hasattr(engine, "enable_workflow")
        assert hasattr(engine, "disable_workflow")
        assert hasattr(engine, "list_workflows")

    def test_protocol_methods_are_callable(self):
        """Test that all protocol methods are callable."""
        engine = WorkflowEngine()
        assert callable(engine.fire_event)
        assert callable(engine.trigger_workflow)
        assert callable(engine.load_workflow)
        assert callable(engine.unload_workflow)
        assert callable(engine.enable_workflow)
        assert callable(engine.disable_workflow)
        assert callable(engine.list_workflows)


class TestWorkflowServices:
    """Test WorkflowServices dataclass."""

    def test_default_services_all_none(self):
        """Test that all services default to None."""
        services = WorkflowServices()
        assert services.nexus_ops is None
        assert services.metadata_store is None
        assert services.glob_match is None

    def test_services_with_glob_match(self):
        """Test creating services with glob_match."""

        def mock_glob(path: str, patterns: list[str]) -> bool:
            return any(path.endswith(p.lstrip("*")) for p in patterns)

        services = WorkflowServices(glob_match=mock_glob)
        assert services.glob_match is mock_glob
        assert services.glob_match("/docs/readme.md", ["*.md"]) is True

    def test_services_are_independent(self):
        """Test that services are independent (no shared state)."""
        s1 = WorkflowServices()
        s2 = WorkflowServices()
        assert s1 is not s2
