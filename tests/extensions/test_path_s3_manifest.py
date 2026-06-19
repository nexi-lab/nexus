"""#4266: path_s3 connector discovery metadata must advertise endpoint_url + signature_version.

The runtime class CONNECTION_ARGS is mirrored by the extension-store discovery
manifest and the shipped generated index (extensions.json). Manifest-driven setup
flows (/api/v2/extensions) read the index, so the new endpoint controls must be
present there too — not just on the constructor.
"""

from __future__ import annotations

from pathlib import Path

import nexus.extensions
from nexus.extensions.store import ManifestStore

NEW_ARGS = ("endpoint_url", "signature_version")


def test_manifest_source_of_truth_advertises_endpoint_args():
    from nexus.backends.connectors.s3._manifest import MANIFEST

    for arg in NEW_ARGS:
        assert arg in MANIFEST.connection_args, f"{arg} missing from path_s3 connector manifest"


def test_shipped_index_advertises_endpoint_args():
    index_path = Path(nexus.extensions.__file__).parent / "_index" / "extensions.json"
    store = ManifestStore()
    store.load_json_index(index_path)

    manifest = store.get("path_s3", kind="connector")
    for arg in NEW_ARGS:
        assert arg in manifest.connection_args, (
            f"{arg} missing from shipped extensions.json — regenerate via "
            "`python -m nexus.extensions.index build`"
        )
