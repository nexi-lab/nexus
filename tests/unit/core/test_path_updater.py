import sqlite3
from pathlib import Path

from nexus.bricks.rebac.path_updater import PathUpdater


def _make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "path_updater.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE rebac_tuples (
                tuple_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_relation TEXT,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                zone_id TEXT,
                expires_at TEXT
            );

            CREATE TABLE rebac_changelog (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT NOT NULL,
                tuple_id INTEGER NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            -- Production schema: migration tiger_resource_map_remove_tenant
            -- dropped tenant_id / zone_id because resource paths are globally
            -- unique. Unique constraint is (resource_type, resource_id).
            CREATE TABLE tiger_resource_map (
                resource_int_id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                UNIQUE (resource_type, resource_id)
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO rebac_tuples (
                subject_type, subject_id, subject_relation, relation,
                object_type, object_id, zone_id, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            [
                (
                    "user",
                    "alice",
                    None,
                    "direct_owner",
                    "file",
                    "/workspace/demo/original.txt",
                    "default",
                ),
                (
                    "user",
                    "admin",
                    None,
                    "direct_owner",
                    "file",
                    "/zone/default/workspace/demo/original.txt",
                    "default",
                ),
                (
                    "file",
                    "/workspace/demo/original.txt",
                    None,
                    "parent",
                    "file",
                    "/workspace/demo",
                    "default",
                ),
                (
                    "file",
                    "/zone/default/workspace/demo/original.txt",
                    None,
                    "parent",
                    "file",
                    "/zone/default/workspace/demo",
                    "default",
                ),
                (
                    "user",
                    "eve",
                    None,
                    "direct_owner",
                    "file",
                    "/workspace/demo/original.txt",
                    "other-zone",
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO tiger_resource_map (resource_type, resource_id)
            VALUES (?, ?)
            """,
            [
                # Paths are globally unique post-migration. The rename case
                # stores both the unscoped and the zone-scoped form as
                # independent int-ids because callers may emit either.
                ("file", "/workspace/demo/original.txt"),
                ("file", "/zone/default/workspace/demo/original.txt"),
                # A sibling file under the same prefix — must survive the
                # single-file rename cleanup (is_directory=False).
                ("file", "/workspace/demo/sibling.txt"),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _connection_factory(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def test_update_object_path_updates_mixed_scoped_rows_without_touching_other_zones(tmp_path: Path):
    db_path = _make_db(tmp_path)
    tiger_resource_map = type(
        "ResourceMap",
        (),
        {
            "_uuid_to_int": {
                ("file", "/workspace/demo/original.txt"): 1,
                ("file", "/zone/default/workspace/demo/original.txt"): 2,
                ("file", "/workspace/demo/original.txt#other"): 3,
            },
            "_int_to_uuid": {
                1: ("file", "/workspace/demo/original.txt"),
                2: ("file", "/zone/default/workspace/demo/original.txt"),
                3: ("file", "/workspace/demo/original.txt#other"),
            },
        },
    )()
    tiger_cache = type("TigerCache", (), {"_resource_map": tiger_resource_map})()

    updater = PathUpdater(
        connection_factory=lambda: _connection_factory(db_path),
        create_cursor=lambda conn: conn.cursor(),
        fix_sql=lambda sql: sql,
        invalidate_cache_cb=lambda *args, **kwargs: None,
        tiger_invalidate_cache_cb=None,
        tiger_cache=tiger_cache,
    )

    updated_count, should_bump = updater.update_object_path(
        old_path="/zone/default/workspace/demo/original.txt",
        new_path="/zone/default/workspace/demo/renamed.txt",
        object_type="file",
        is_directory=False,
    )

    assert updated_count == 4
    assert should_bump is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT subject_type, subject_id, relation, object_id, zone_id
            FROM rebac_tuples
            ORDER BY tuple_id
            """
        ).fetchall()
        assert [dict(row) for row in rows] == [
            {
                "subject_type": "user",
                "subject_id": "alice",
                "relation": "direct_owner",
                "object_id": "/workspace/demo/renamed.txt",
                "zone_id": "default",
            },
            {
                "subject_type": "user",
                "subject_id": "admin",
                "relation": "direct_owner",
                "object_id": "/zone/default/workspace/demo/renamed.txt",
                "zone_id": "default",
            },
            {
                "subject_type": "file",
                "subject_id": "/workspace/demo/renamed.txt",
                "relation": "parent",
                "object_id": "/workspace/demo",
                "zone_id": "default",
            },
            {
                "subject_type": "file",
                "subject_id": "/zone/default/workspace/demo/renamed.txt",
                "relation": "parent",
                "object_id": "/zone/default/workspace/demo",
                "zone_id": "default",
            },
            {
                "subject_type": "user",
                "subject_id": "eve",
                "relation": "direct_owner",
                "object_id": "/workspace/demo/original.txt",
                "zone_id": "other-zone",
            },
        ]

        changelog_count = conn.execute("SELECT COUNT(*) FROM rebac_changelog").fetchone()[0]
        assert changelog_count == 4

        resource_rows = conn.execute(
            """
            SELECT resource_id
            FROM tiger_resource_map
            ORDER BY resource_int_id
            """
        ).fetchall()
        # Renamed path(s) must be deleted (both unscoped and zone-scoped
        # forms); the sibling file under the same prefix must survive.
        assert [row[0] for row in resource_rows] == [
            "/workspace/demo/sibling.txt",
        ]
    finally:
        conn.close()


def test_cleanup_tiger_resource_map_does_not_emit_zone_clause(tmp_path: Path):
    """Regression: the tiger_resource_map DELETE must NOT include zone_id.

    Migration ``tiger_resource_map_remove_tenant`` dropped the tenant/zone
    axis (resource paths are globally unique). Any SQL that emits
    ``AND zone_id = ?`` against this table fails in postgres with
    ``column "zone_id" does not exist``, silently leaking stale
    (int_id ↔ path) entries through every rename/move.
    """
    db_path = tmp_path / "tiger_only.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE rebac_tuples (
                tuple_id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_type TEXT, subject_id TEXT, subject_relation TEXT,
                relation TEXT, object_type TEXT, object_id TEXT,
                zone_id TEXT, expires_at TEXT
            );
            CREATE TABLE rebac_changelog (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT, tuple_id INTEGER,
                subject_type TEXT, subject_id TEXT, relation TEXT,
                object_type TEXT, object_id TEXT, zone_id TEXT,
                created_at TEXT
            );
            -- Production schema: no zone_id column.
            CREATE TABLE tiger_resource_map (
                resource_int_id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                UNIQUE (resource_type, resource_id)
            );
            """
        )
        conn.executemany(
            "INSERT INTO tiger_resource_map (resource_type, resource_id) VALUES (?, ?)",
            [("file", "/zone/default/workspace/doc.txt")],
        )
        conn.commit()
    finally:
        conn.close()

    resource_map = type(
        "ResourceMap",
        (),
        {
            "_uuid_to_int": {("file", "/zone/default/workspace/doc.txt"): 1},
            "_int_to_uuid": {1: ("file", "/zone/default/workspace/doc.txt")},
        },
    )()
    tiger_cache = type("TigerCache", (), {"_resource_map": resource_map})()

    updater = PathUpdater(
        connection_factory=lambda: _connection_factory(db_path),
        create_cursor=lambda conn: conn.cursor(),
        fix_sql=lambda sql: sql,
        invalidate_cache_cb=lambda *args, **kwargs: None,
        tiger_invalidate_cache_cb=None,
        tiger_cache=tiger_cache,
    )

    # Must not raise OperationalError("no such column: zone_id") — this is
    # the exact failure mode seen in production postgres.
    updater.update_object_path(
        old_path="/zone/default/workspace/doc.txt",
        new_path="/zone/default/workspace/doc-renamed.txt",
        object_type="file",
        is_directory=False,
    )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT resource_id FROM tiger_resource_map ORDER BY resource_int_id"
        ).fetchall()
        assert [row[0] for row in rows] == []  # stale entry cleaned up
    finally:
        conn.close()
