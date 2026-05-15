"""Fixtures for archive integration tests (#3793)."""

from __future__ import annotations

import pytest

from tests.integration.archive.helpers import (
    boot_lightweight_nexus,
    plant_provider_key,
    plant_secret_doc,
    plant_timeline_corpus,
)


@pytest.fixture
def fresh_nexus_with_planted_secret(tmp_path):
    """NexusFS with a document whose body contains an Anthropic API key."""
    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    plant_secret_doc(fs, "eng")
    yield fs
    fs.shutdown()


@pytest.fixture
def fresh_nexus_with_provider_key(tmp_path):
    """NexusFS with a fake provider row containing an API key."""
    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    plant_provider_key(fs, "anthropic", "sk-ant-aaaaaaaaaaaaaaaaaaaa")
    yield fs
    fs.shutdown()


@pytest.fixture
def fresh_nexus_with_timeline_corpus(tmp_path):
    """NexusFS with early/in-window/late documents for audit-window testing."""
    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    plant_timeline_corpus(fs, "eng")
    yield fs
    fs.shutdown()


@pytest.fixture
def signed_archive(tmp_path):
    """A signed .nexus bundle created from a small fixture corpus."""

    from nexus.bricks.portability.export_service import ZoneExportService
    from nexus.bricks.portability.models import ZoneExportOptions
    from nexus.bricks.portability.signer import ArchiveSigner

    fs = boot_lightweight_nexus(db_path=tmp_path / "nexus.db")
    # Plant a couple of documents as the corpus
    fs.write("/eng/doc1.txt", b"hello archive", context=fs._init_cred)
    fs.write("/eng/doc2.txt", b"world archive", context=fs._init_cred)

    key_path = tmp_path / "signing_key"
    ArchiveSigner(key_path)  # generate key at key_path
    out = tmp_path / "signed.nexus"

    options = ZoneExportOptions(
        output_path=out,
        include_content=False,  # skip CAS blobs to keep test fast
        sign=True,
        strip_credentials=False,
        signing_key_path=key_path,
    )
    service = ZoneExportService(fs)
    service.export_zone("root", options)
    fs.shutdown()
    return out
