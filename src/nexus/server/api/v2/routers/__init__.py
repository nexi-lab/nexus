"""API v2 routers — MUST_STAY HTTP endpoints only.

All query/CRUD endpoints have been migrated to @rpc_expose services.
Remaining routers provide: SSE streaming, CSV export, K8s health probes,
tus.io uploads, and async file streaming.
"""
