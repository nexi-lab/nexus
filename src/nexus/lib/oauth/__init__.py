"""Universal OAuth primitives shared by nexus-fs slim and nexus-ai-fs full.

This package ships in both wheels. Storage-backed orchestration (token manager,
credential service, factory) stays in :mod:`nexus.bricks.auth.oauth`.

Lazy re-exports are added by later tasks; importing submodules directly is
always safe.
"""
