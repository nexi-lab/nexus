"""Agent Runtime — agent process execution engine.

System service tier (Tier 1). Manages agent process lifecycle:
spawn, resume, terminate, signal. Integrates with VFS for file I/O,
LLM providers for inference, and CAS for session checkpoints.

Design doc: docs/design/AGENT-PROCESS-ARCHITECTURE.md.
"""
