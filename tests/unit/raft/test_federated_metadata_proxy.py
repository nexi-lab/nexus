"""Unit tests for FederatedMetadataProxy backend_name enrichment (#163).

Tests the transparent backend_name enrichment that stamps the writer
node's address onto metadata during put/put_batch, enabling
FederationContentResolver to locate content on the correct peer.
"""

from unittest.mock import MagicMock

from nexus.contracts.metadata import FileMetadata
from nexus.raft.federated_metadata_proxy import FederatedMetadataProxy

SELF_ADDR = "10.0.0.1:50051"


def _make_metadata(path: str = "/test.txt", backend_name: str = "local") -> FileMetadata:
    return FileMetadata(
        path=path,
        backend_name=backend_name,
        physical_path="abc123",
        size=100,
        etag="abc123",
        version=1,
    )


def _make_proxy(self_address: str | None = SELF_ADDR) -> FederatedMetadataProxy:
    """Create proxy with mock resolver and root store."""
    resolver = MagicMock()
    root_store = MagicMock()

    # Default resolve: root zone, no mount chain
    resolved = MagicMock()
    resolved.mount_chain = []
    resolved.path = "/test.txt"
    resolved.zone_id = "root"
    resolved.store = root_store
    resolver.resolve.return_value = resolved

    return FederatedMetadataProxy(
        resolver=resolver,
        root_store=root_store,
        self_address=self_address,
    )


class TestEnrichBackendName:
    """_enrich_backend_name stamps node address onto backend_name."""

    def test_enriches_plain_backend_name(self):
        proxy = _make_proxy()
        meta = _make_metadata(backend_name="local")
        enriched = proxy._enrich_backend_name(meta)
        assert enriched.backend_name == f"local@{SELF_ADDR}"

    def test_skips_already_enriched(self):
        proxy = _make_proxy()
        meta = _make_metadata(backend_name=f"local@{SELF_ADDR}")
        enriched = proxy._enrich_backend_name(meta)
        assert enriched.backend_name == f"local@{SELF_ADDR}"

    def test_skips_when_no_self_address(self):
        proxy = _make_proxy(self_address=None)
        meta = _make_metadata(backend_name="local")
        enriched = proxy._enrich_backend_name(meta)
        assert enriched.backend_name == "local"

    def test_skips_when_empty_backend_name(self):
        proxy = _make_proxy()
        meta = _make_metadata(backend_name="")
        enriched = proxy._enrich_backend_name(meta)
        assert enriched.backend_name == ""

    def test_preserves_other_fields(self):
        proxy = _make_proxy()
        meta = _make_metadata(backend_name="s3")
        enriched = proxy._enrich_backend_name(meta)
        assert enriched.backend_name == f"s3@{SELF_ADDR}"
        assert enriched.path == meta.path
        assert enriched.physical_path == meta.physical_path
        assert enriched.size == meta.size
        assert enriched.etag == meta.etag


class TestPutEnrichment:
    """put() enriches backend_name before forwarding to zone store."""

    def test_put_enriches_backend_name(self):
        proxy = _make_proxy()
        meta = _make_metadata(backend_name="local")

        proxy.put(meta)

        # Verify the store received enriched metadata
        store = proxy._resolver.resolve.return_value.store
        stored_meta = store.put.call_args[0][0]
        assert stored_meta.backend_name == f"local@{SELF_ADDR}"

    def test_put_without_federation_passes_through(self):
        proxy = _make_proxy(self_address=None)
        meta = _make_metadata(backend_name="local")

        proxy.put(meta)

        store = proxy._resolver.resolve.return_value.store
        stored_meta = store.put.call_args[0][0]
        assert stored_meta.backend_name == "local"


class TestPutBatchEnrichment:
    """put_batch() enriches all metadata entries."""

    def test_put_batch_enriches_all(self):
        resolver = MagicMock()
        root_store = MagicMock()

        resolved = MagicMock()
        resolved.mount_chain = []
        resolved.zone_id = "root"
        resolved.store = root_store
        resolver.resolve.return_value = resolved
        resolver.get_store.return_value = root_store

        proxy = FederatedMetadataProxy(
            resolver=resolver,
            root_store=root_store,
            self_address=SELF_ADDR,
        )

        metas = [
            _make_metadata(path="/a.txt", backend_name="local"),
            _make_metadata(path="/b.txt", backend_name="s3"),
        ]

        # Make resolve return path-specific resolved paths
        def side_effect(path):
            r = MagicMock()
            r.mount_chain = []
            r.path = path
            r.zone_id = "root"
            r.store = root_store
            return r

        resolver.resolve.side_effect = side_effect

        proxy.put_batch(metas)

        stored_metas = root_store.put_batch.call_args[0][0]
        assert stored_metas[0].backend_name == f"local@{SELF_ADDR}"
        assert stored_metas[1].backend_name == f"s3@{SELF_ADDR}"


class TestFromZoneManager:
    """from_zone_manager() picks up advertise_addr."""

    def test_picks_up_advertise_addr(self):
        """Raft advertise_addr host is kept, but port is replaced with VFS gRPC port."""
        zone_mgr = MagicMock()
        zone_mgr.advertise_addr = "10.0.0.5:50051"
        zone_mgr.get_store.return_value = MagicMock()

        proxy = FederatedMetadataProxy.from_zone_manager(zone_mgr)
        # from_zone_manager replaces the Raft port with the VFS gRPC port (default 2028)
        assert proxy._self_address == "10.0.0.5:2028"
