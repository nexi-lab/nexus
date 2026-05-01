"""HackerNews connector manifest (extension store discovery).

Imported by `nexus.extensions.store` for metadata-only enumeration without
loading the connector impl. The decorator-based registration in
``connector.py`` continues to drive runtime mounting; this manifest is
read by introspection (``nexus extensions list``) and by the JSON index
generator that ships with the wheel.
"""

from __future__ import annotations

from nexus.extensions.manifest import ConnectorManifest
from nexus.extensions.types import ArgType, ConnectionArg

MANIFEST = ConnectorManifest(
    name="hn_connector",
    module="nexus.backends.connectors.hn.connector",
    factory="PathHNBackend",
    description="Read-only HackerNews public API as a virtual filesystem",
    service_name="hn",
    capabilities=frozenset({"readme_doc"}),
    connection_args={
        "cache_ttl": ConnectionArg(
            type=ArgType.INTEGER,
            description="Default cache TTL in seconds",
            required=False,
            default=300,
        ),
        "stories_per_feed": ConnectionArg(
            type=ArgType.INTEGER,
            description="Number of stories per feed (1-30)",
            required=False,
            default=10,
        ),
        "include_comments": ConnectionArg(
            type=ArgType.BOOLEAN,
            description="Include nested comments in story files",
            required=False,
            default=True,
        ),
    },
    user_scoped=False,
    import_probes=("httpx",),
)
