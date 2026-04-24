"""One-shot migration of the OAuth encryption key from legacy metastore
(redb) files into the record_store (SQL).

Context
-------
Prior to R20.18.5, OAuth encryption keys were persisted via
``MetastoreSettingsStore`` backed by a Python ``RaftMetadataStore`` at
``~/.nexus/metastore`` (no extension). R20.18.5 swapped the wrapper to
``RustMetastoreProxy`` and appended a ``.redb`` extension to the path.
Neither change migrated existing key data, so any install that had
already written an OAuth key silently lost it on upgrade: next boot
generated an ephemeral key, and every secret encrypted under the old
key became undecryptable.

We now persist the key in the record_store (SQL) via
``SQLAlchemySystemSettingsStore`` — the correct services-tier SSOT —
which makes filesystem-metastore paths irrelevant going forward. This
module bridges the upgrade: on first boot after this change, if the
record_store has no OAuth key yet, peek at the legacy redb files and
copy the key over if found.

Idempotent: if the record_store already has a key (either migrated
earlier or written in a later boot), the legacy paths are never opened.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nexus.contracts.auth_store_protocols import SystemSettingsStoreProtocol
from nexus.lib.oauth.crypto import OAUTH_ENCRYPTION_KEY_NAME

logger = logging.getLogger(__name__)


# Paths to probe, in order. Both point at the same default location under
# ``$HOME``; the difference is the extension (pre vs. post R20.18.5).
def _legacy_redb_candidates() -> list[Path]:
    base = Path.home() / ".nexus"
    return [
        base / "metastore.redb",  # post-R20.18.5 naming
        base / "metastore",  # pre-R20.18.5 naming (Python RaftMetadataStore.embedded)
    ]


def migrate_legacy_oauth_key(
    sql_settings_store: SystemSettingsStoreProtocol,
    *,
    existing_metastore: Any | None = None,
) -> bool:
    """Copy the OAuth key from a legacy redb file into the SQL settings store.

    Args:
        sql_settings_store: The SQL-backed settings store to write the key into.
        existing_metastore: An already-open ``MetastoreABC`` (typically the main
            Kernel's ``RustMetastoreProxy``).  When provided the key is read
            through this connection, avoiding a second ``Kernel()`` that would
            hit the redb exclusive-file-lock held by the main Kernel.

    Returns:
        True if a migration actually happened this call; False if the SQL
        store already had a key, no legacy file existed, or no legacy file
        contained the key.

    Raises:
        Exception: if a legacy file holds a key but writing it into the SQL
            settings store fails. This is a data-integrity failure the
            operator needs to see; silently continuing would let the next
            request generate a fresh ephemeral key and orphan all data
            encrypted under the legacy key.
    """
    # Idempotency guard — once the SQL store has the key, the redb files
    # stop being load-bearing and we don't touch them again.
    if sql_settings_store.get_setting(OAUTH_ENCRYPTION_KEY_NAME) is not None:
        return False

    for path in _legacy_redb_candidates():
        if not path.exists():
            continue
        key = _read_oauth_key_from_redb(path, existing_metastore=existing_metastore)
        if key is None:
            continue
        sql_settings_store.set_setting(
            OAUTH_ENCRYPTION_KEY_NAME,
            key,
            description=(
                f"Migrated from legacy filesystem-metastore key store at {path} "
                "(pre-R20.18.5 / R20.18.5-era layout)."
            ),
        )
        logger.info(
            "Migrated legacy OAuth encryption key: %s -> record_store.system_settings",
            path,
        )
        return True

    return False


def _read_oauth_key_from_redb(
    path: Path,
    *,
    existing_metastore: Any | None = None,
) -> str | None:
    """Read the OAuth encryption key from a legacy metastore file.

    When *existing_metastore* is provided (a ``RustMetastoreProxy`` backed
    by the main Kernel), the key is read through that connection — avoiding
    a second ``Kernel()`` that would hit the redb exclusive-file-lock.

    For the pre-redb ``~/.nexus/metastore`` path (Python RaftMetadataStore
    format) the main Kernel's ``LocalMetastore`` cannot read it, but neither
    could the old standalone ``Kernel()`` approach (``Database::create``
    fails on a non-redb file).  Returns ``None`` with a log line in that case.

    Returns ``None`` when the file can't be opened, when the Rust kernel
    isn't importable in this process, or when the key isn't present. A
    log line at WARNING level is emitted for unexpected failures so an
    operator looking into a data-loss incident has something to grep.
    """
    # Fast path: reuse the main Kernel's metastore connection.
    if existing_metastore is not None:
        try:
            from nexus.storage.auth_stores.metastore_settings_store import (
                MetastoreSettingsStore,
            )

            store = MetastoreSettingsStore(existing_metastore)
            dto = store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)
            if dto is None:
                return None
            return dto.value
        except Exception as exc:
            logger.warning(
                "Legacy OAuth key migration could not read via existing metastore for %s: %s",
                path,
                exc,
            )
            return None

    # Slow path (original): open the redb file with a standalone Kernel().
    # This will fail with "Database already open" if the main Kernel
    # already holds the lock — acceptable for CLI / test callers that
    # don't have a main Kernel.
    try:
        from nexus.core.metastore import RustMetastoreProxy
        from nexus.storage.auth_stores.metastore_settings_store import (
            MetastoreSettingsStore,
        )
    except ImportError as exc:
        logger.warning(
            "Legacy OAuth key migration skipped (%s): %s",
            path,
            exc,
        )
        return None

    try:
        from nexus_kernel import Kernel
    except ImportError as exc:
        logger.warning(
            "Legacy OAuth key migration skipped — nexus_kernel unavailable (%s): %s",
            path,
            exc,
        )
        return None

    try:
        proxy = RustMetastoreProxy(Kernel(), str(path))
        store = MetastoreSettingsStore(proxy)
        dto = store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)
        if dto is None:
            return None
        return dto.value
    except Exception as exc:
        # Narrow log — don't fail boot on a best-effort probe — but loud
        # enough to surface in an incident grep.
        logger.warning(
            "Legacy OAuth key migration could not read %s: %s",
            path,
            exc,
        )
        return None
