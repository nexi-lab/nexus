"""API v1 - Core Nexus REST endpoints.

This module exposes domain routers for the Nexus server under
the /api/ prefix (no version prefix, maintaining backward compatibility).

Endpoint groups:
- /api/locks - Distributed locking (5 endpoints)
- /api/subscriptions - Webhook subscriptions (6 endpoints)
- /api/agents/*/identity - Agent identity & verification (2 endpoints)
- /api/search - Search daemon (5 endpoints)
- /api/memory - Memory query/store (4 endpoints)
- /api/graph - Knowledge graph (4 endpoints)
- /api/v1/admin - Hotspot detection (2 endpoints)
- /api/cache - Cache warmup & stats (3 endpoints)
- /api/share - Share links (3 endpoints)
- /ws/events - WebSocket real-time events (2 endpoints)
- /api/watch - Long-polling watch (1 endpoint)
"""
