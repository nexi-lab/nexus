"""Agent Runtime — agent loop and observer base classes.

Shared base for unmanaged (3rd-party gRPC) agent subprocess communication
over PipeBackend (DT_PIPE).  Used by services/acp/connection.py.

    agent_runtime/loop.py     = AgentLoop base class
    agent_runtime/observer.py = AgentObserver
"""
