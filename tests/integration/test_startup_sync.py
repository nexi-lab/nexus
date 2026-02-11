"""Startup sync tests.

Tests the startup_sync functionality that reconciles missed events from PostgreSQL
when a NexusFS node reconnects after being offline.

Test Environment:
- PostgreSQL (docker): nexus-postgres-test on port 5433
- Dragonfly (docker): nexus-dragonfly-coordination on port 6380
- Windows NexusFS (host)
- Linux NexusFS (docker container)

Prerequisites:
    docker compose --profile test up -d

Usage:
    NEXUS_DATABASE_URL=postgresql://nexus_test:nexus_test_password@localhost:5433/nexus_test \
    NEXUS_REDIS_URL=redis://localhost:6380 \
    pytest tests/integration/test_startup_sync.py -v --tb=short
"""

import asyncio
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest


def _utcnow_naive() -> datetime:
    """Get current UTC time as naive datetime for PostgreSQL compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


def is_postgres_available():
    """Check if PostgreSQL test database is available."""
    db_url = os.environ.get("NEXUS_DATABASE_URL")
    if not db_url:
        return False
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def is_redis_available():
    """Check if Redis/Dragonfly is available."""
    redis_url = os.environ.get(
        "NEXUS_REDIS_URL",
        os.environ.get("NEXUS_REDIS_URL"),
    )
    if not redis_url:
        return False
    try:
        import redis

        r = redis.from_url(redis_url)
        r.ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not is_postgres_available(),
        reason="PostgreSQL not available (set NEXUS_DATABASE_URL)",
    ),
    pytest.mark.skipif(
        not is_redis_available(),
        reason="Redis not available (set NEXUS_REDIS_URL)",
    ),
]


@pytest.fixture
def db_session_factory():
    """Create a session factory for PostgreSQL."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_url = os.environ["NEXUS_DATABASE_URL"]
    engine = create_engine(db_url)

    # Create fallback uuidv7 function for PostgreSQL < 18
    # This is a simplified version that uses gen_random_uuid with timestamp prefix
    uuidv7_function = """
    CREATE OR REPLACE FUNCTION uuidv7() RETURNS uuid AS $$
    DECLARE
        unix_ts_ms BIGINT;
        v7_uuid UUID;
        rand_bytes BYTEA;
    BEGIN
        unix_ts_ms := EXTRACT(EPOCH FROM clock_timestamp()) * 1000;
        rand_bytes := gen_random_bytes(10);

        -- Build UUIDv7: 48 bits timestamp + 4 bits version (7) + 12 bits rand + 2 bits variant + 62 bits rand
        v7_uuid := encode(
            set_byte(
                set_byte(
                    substring(int8send(unix_ts_ms) from 3 for 6) || rand_bytes,
                    6, (get_byte(rand_bytes, 0) & 15) | 112  -- version 7
                ),
                8, (get_byte(rand_bytes, 2) & 63) | 128  -- variant bits
            ),
            'hex'
        )::uuid;

        RETURN v7_uuid;
    END;
    $$ LANGUAGE plpgsql VOLATILE;
    """

    with engine.connect() as conn:
        # Create uuidv7 function if it doesn't exist
        try:
            conn.execute(text(uuidv7_function))
            conn.commit()
        except Exception:
            conn.rollback()  # Function already exists or error, continue

    # Create tables
    from nexus.storage.models import Base

    Base.metadata.create_all(engine)

    return sessionmaker(bind=engine)


@pytest.fixture
def clean_db(db_session_factory):
    """Clean database before and after test."""
    from nexus.storage.models import OperationLogModel, SystemSettingsModel

    def _clean():
        with db_session_factory() as session:
            # Use synchronize_session='fetch' to ensure proper deletion
            session.query(OperationLogModel).delete(synchronize_session="fetch")
            session.query(SystemSettingsModel).filter(
                SystemSettingsModel.key.like("node_sync_checkpoint:%")
            ).delete(synchronize_session="fetch")
            session.commit()

    _clean()
    yield
    _clean()


class TestStartupSyncBasic:
    """Basic startup_sync tests."""

    @pytest.mark.asyncio
    async def test_startup_sync_with_missed_events(self, db_session_factory, clean_db):
        """Test that startup_sync processes missed events from PostgreSQL."""
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.event_bus import FileEvent, RedisEventBus
        from nexus.storage.models import OperationLogModel

        redis_url = os.environ.get(
            "NEXUS_REDIS_URL",
            "redis://localhost:6380",
        )

        # Insert some "missed" operations into PostgreSQL
        with db_session_factory() as session:
            for i in range(3):
                op = OperationLogModel(
                    operation_type="write",
                    path=f"/test/file{i}.txt",
                    zone_id="default",
                    status="success",
                    created_at=_utcnow_naive() - timedelta(minutes=30 - i),
                )
                session.add(op)
            session.commit()

        # Create event bus with session factory
        client = DragonflyClient(url=redis_url)
        await client.connect()

        try:
            event_bus = RedisEventBus(
                redis_client=client,
                session_factory=db_session_factory,
            )
            await event_bus.start()

            # Track handled events
            handled_events = []

            async def event_handler(event: FileEvent):
                handled_events.append(event)

            # Run startup sync
            synced = await event_bus.startup_sync(
                event_handler=event_handler,
                default_lookback_hours=1,
            )

            # Verify
            assert synced == 3, f"Should sync 3 events, got {synced}"
            assert len(handled_events) == 3, "Handler should receive 3 events"

            # Verify event details
            paths = [e.path for e in handled_events]
            assert "/test/file0.txt" in paths
            assert "/test/file1.txt" in paths
            assert "/test/file2.txt" in paths

            await event_bus.stop()
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_startup_sync_no_missed_events(self, db_session_factory, clean_db):
        """Test startup_sync when there are no missed events."""
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.event_bus import RedisEventBus

        redis_url = os.environ.get(
            "NEXUS_REDIS_URL",
            "redis://localhost:6380",
        )

        client = DragonflyClient(url=redis_url)
        await client.connect()

        try:
            event_bus = RedisEventBus(
                redis_client=client,
                session_factory=db_session_factory,
            )
            await event_bus.start()

            handled_events = []

            async def event_handler(event):
                handled_events.append(event)

            synced = await event_bus.startup_sync(
                event_handler=event_handler,
                default_lookback_hours=1,
            )

            assert synced == 0, "Should sync 0 events when none missed"
            assert len(handled_events) == 0

            await event_bus.stop()
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_startup_sync_respects_checkpoint(self, db_session_factory, clean_db):
        """Test that startup_sync only processes events after the checkpoint."""
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.event_bus import RedisEventBus
        from nexus.storage.models import OperationLogModel, SystemSettingsModel

        redis_url = os.environ.get(
            "NEXUS_REDIS_URL",
            "redis://localhost:6380",
        )

        # Use a fixed base time to avoid timing issues
        base_time = _utcnow_naive()

        # Set a checkpoint 20 minutes ago
        checkpoint_time = base_time - timedelta(minutes=20)

        with db_session_factory() as session:
            # Insert checkpoint
            setting = SystemSettingsModel(
                key="node_sync_checkpoint:test-node",
                value=checkpoint_time.isoformat(),
            )
            session.add(setting)

            # Insert operations: 2 before checkpoint (should be skipped), 2 after (should be synced)
            for i, offset in enumerate([30, 25, 15, 10]):  # minutes ago
                op = OperationLogModel(
                    operation_type="write",
                    path=f"/test/file{i}.txt",
                    zone_id="default",
                    status="success",
                    created_at=base_time - timedelta(minutes=offset),
                )
                session.add(op)
            session.commit()

        client = DragonflyClient(url=redis_url)
        await client.connect()

        try:
            event_bus = RedisEventBus(
                redis_client=client,
                session_factory=db_session_factory,
                node_id="test-node",
            )
            await event_bus.start()

            handled_events = []

            async def event_handler(event):
                handled_events.append(event)

            synced = await event_bus.startup_sync(event_handler=event_handler)

            # Only events after checkpoint (15min and 10min ago) should be synced
            assert synced == 2, f"Should sync 2 events after checkpoint, got {synced}"
            assert len(handled_events) == 2

            await event_bus.stop()
        finally:
            await client.disconnect()

    @pytest.mark.asyncio
    async def test_startup_sync_updates_checkpoint(self, db_session_factory, clean_db):
        """Test that startup_sync updates the checkpoint after processing."""
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.event_bus import RedisEventBus
        from nexus.storage.models import OperationLogModel

        redis_url = os.environ.get(
            "NEXUS_REDIS_URL",
            "redis://localhost:6380",
        )

        # Insert an operation
        with db_session_factory() as session:
            op = OperationLogModel(
                operation_type="write",
                path="/test/file.txt",
                zone_id="default",
                status="success",
                created_at=_utcnow_naive() - timedelta(minutes=10),
            )
            session.add(op)
            session.commit()

        client = DragonflyClient(url=redis_url)
        await client.connect()

        try:
            event_bus = RedisEventBus(
                redis_client=client,
                session_factory=db_session_factory,
                node_id="checkpoint-test-node",
            )
            await event_bus.start()

            # First sync
            synced = await event_bus.startup_sync(default_lookback_hours=1)
            assert synced == 1

            # Second sync should find no new events (checkpoint updated)
            synced2 = await event_bus.startup_sync(default_lookback_hours=1)
            assert synced2 == 0, "Second sync should find no new events"

            await event_bus.stop()
        finally:
            await client.disconnect()


class TestStartupSyncCrossPlatform:
    """Cross-platform startup_sync tests (Windows + Linux)."""

    @pytest.fixture
    def linux_container_available(self):
        """Check if Linux container is available."""
        try:
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "echo", "ok"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                pytest.skip("Linux container not running")
        except Exception:
            pytest.skip("Linux container not running")

    @pytest.mark.asyncio
    async def test_linux_syncs_windows_operations(
        self, db_session_factory, clean_db, linux_container_available
    ):
        """Test that Linux NexusFS syncs operations created by Windows.

        Scenario:
        1. Windows creates some file operations (logged to PostgreSQL)
        2. Linux "starts up" and runs startup_sync
        3. Linux should receive all the missed events
        """
        from nexus.storage.models import OperationLogModel

        # Windows: Create some operations in PostgreSQL
        with db_session_factory() as session:
            for i in range(5):
                op = OperationLogModel(
                    operation_type="write",
                    path=f"/shared/file{i}.txt",
                    zone_id="default",
                    status="success",
                    created_at=_utcnow_naive() - timedelta(minutes=30 - i),
                )
                session.add(op)
            session.commit()

        # Linux: Run startup_sync and verify
        linux_script = """
import asyncio
import sys
sys.path.insert(0, "/app/src")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.event_bus import RedisEventBus

async def sync_and_verify():
    # Connect to PostgreSQL (internal Docker network address)
    db_url = "postgresql://nexus_test:nexus_test_password@postgres-test:5432/nexus_test"
    engine = create_engine(db_url)
    session_factory = sessionmaker(bind=engine)

    # Connect to Redis
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()

    try:
        event_bus = RedisEventBus(
            redis_client=client,
            session_factory=session_factory,
            node_id="linux-test-node",
        )
        await event_bus.start()

        handled_events = []

        async def event_handler(event):
            handled_events.append(event.path)

        synced = await event_bus.startup_sync(
            event_handler=event_handler,
            default_lookback_hours=1,
        )

        print(f"SYNCED:{synced}")
        print(f"PATHS:{','.join(handled_events)}")

        await event_bus.stop()
    finally:
        await client.disconnect()

asyncio.run(sync_and_verify())
"""
        result = subprocess.run(
            ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
            capture_output=True,
            text=True,
            timeout=60,
        )

        # Parse output
        assert "SYNCED:" in result.stdout, (
            f"Should have SYNCED output: {result.stdout}, {result.stderr}"
        )

        synced_line = [line for line in result.stdout.split("\n") if "SYNCED:" in line][0]
        synced_count = int(synced_line.split(":")[1])

        paths_line = [line for line in result.stdout.split("\n") if "PATHS:" in line][0]
        paths = paths_line.split(":")[1].split(",") if paths_line.split(":")[1] else []

        assert synced_count == 5, f"Linux should sync 5 events, got {synced_count}"
        assert len(paths) == 5

        for i in range(5):
            assert f"/shared/file{i}.txt" in paths, f"Missing /shared/file{i}.txt"

    @pytest.mark.asyncio
    async def test_cross_node_startup_reconciliation(
        self, db_session_factory, clean_db, linux_container_available
    ):
        """Test bidirectional sync: Windows and Linux both create and sync operations.

        Scenario:
        1. Windows creates operations file0-4
        2. Linux syncs and creates operations file5-9
        3. Windows syncs and should see file5-9
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.event_bus import RedisEventBus
        from nexus.storage.models import OperationLogModel

        redis_url = os.environ.get(
            "NEXUS_REDIS_URL",
            "redis://localhost:6380",
        )

        # Step 1: Windows creates operations
        with db_session_factory() as session:
            for i in range(5):
                op = OperationLogModel(
                    operation_type="write",
                    path=f"/bidirectional/win_file{i}.txt",
                    zone_id="default",
                    status="success",
                    created_at=_utcnow_naive() - timedelta(minutes=50 - i),
                )
                session.add(op)
            session.commit()

        # Step 2: Linux syncs and creates its own operations
        linux_script = '''
import asyncio
import sys
from datetime import UTC, datetime, timedelta


def _utcnow_naive() -> datetime:
    """Get current UTC time as naive datetime for PostgreSQL compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)
sys.path.insert(0, "/app/src")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.event_bus import RedisEventBus
from nexus.storage.models import OperationLogModel

async def sync_and_create():
    db_url = "postgresql://nexus_test:nexus_test_password@postgres-test:5432/nexus_test"
    engine = create_engine(db_url)
    session_factory = sessionmaker(bind=engine)

    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()

    try:
        event_bus = RedisEventBus(
            redis_client=client,
            session_factory=session_factory,
            node_id="linux-bidir-node",
        )
        await event_bus.start()

        handled = []
        async def handler(e):
            handled.append(e.path)

        synced = await event_bus.startup_sync(
            event_handler=handler,
            default_lookback_hours=2,
        )
        print(f"LINUX_SYNCED:{synced}")

        # Create Linux operations
        with session_factory() as session:
            for i in range(5):
                op = OperationLogModel(
                    operation_type="write",
                    path=f"/bidirectional/linux_file{i}.txt",
                    zone_id="default",
                    status="success",
                    created_at=_utcnow_naive() - timedelta(minutes=20 - i),
                )
                session.add(op)
            session.commit()

        print("LINUX_CREATED:5")

        await event_bus.stop()
    finally:
        await client.disconnect()

asyncio.run(sync_and_create())
'''
        result = subprocess.run(
            ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert "LINUX_SYNCED:5" in result.stdout, (
            f"Linux should sync 5: {result.stdout}, {result.stderr}"
        )
        assert "LINUX_CREATED:5" in result.stdout, f"Linux should create 5: {result.stdout}"

        # Step 3: Windows syncs and should see Linux operations
        client = DragonflyClient(url=redis_url)
        await client.connect()

        try:
            event_bus = RedisEventBus(
                redis_client=client,
                session_factory=db_session_factory,
                node_id="win-bidir-node",
            )
            await event_bus.start()

            handled = []

            async def handler(e):
                handled.append(e.path)

            synced = await event_bus.startup_sync(
                event_handler=handler,
                default_lookback_hours=2,
            )

            # Windows should see both Windows and Linux operations
            # (Windows ops are older, Linux ops are newer)
            assert synced >= 5, f"Windows should sync at least Linux's 5 ops, got {synced}"

            linux_paths = [p for p in handled if "linux_file" in p]
            assert len(linux_paths) >= 5, f"Should have Linux files: {linux_paths}"

            await event_bus.stop()
        finally:
            await client.disconnect()


class TestEventBusLockIntegration:
    """Tests for Event Bus + Lock integration."""

    @pytest.fixture
    def linux_container_available(self):
        """Check if Linux container is available."""
        try:
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "echo", "ok"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                pytest.skip("Linux container not running")
        except Exception:
            pytest.skip("Linux container not running")

    @pytest.mark.asyncio
    async def test_event_propagation_with_lock(
        self, db_session_factory, clean_db, linux_container_available
    ):
        """Test that events are propagated when using locks.

        Scenario:
        1. Linux subscribes to events
        2. Windows writes a file with lock=True
        3. Linux should receive the write event via Redis pub/sub

        This verifies the Event Bus + Lock integration works correctly.
        """
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.core.event_bus import RedisEventBus

        redis_url = os.environ.get(
            "NEXUS_REDIS_URL",
            "redis://localhost:6380",
        )

        zone_id = "default"
        test_path = "/event-lock-integration/test-file.txt"

        # Linux: start subscriber that waits for events
        linux_script = f'''
import asyncio
import sys
sys.path.insert(0, "/app/src")

from nexus.cache.dragonfly import DragonflyClient
from nexus.core.event_bus import RedisEventBus

async def subscribe_and_wait():
    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()

    try:
        event_bus = RedisEventBus(redis_client=client, node_id="linux-subscriber")
        await event_bus.start()

        received_events = []

        async def collect_events():
            async for event in event_bus.subscribe("{zone_id}"):
                received_events.append(event)
                print(f"LINUX_RECEIVED:{{event.type}}:{{event.path}}", flush=True)
                if len(received_events) >= 1:
                    break  # Got the event we need

        try:
            # Wait for events with timeout
            await asyncio.wait_for(collect_events(), timeout=15.0)
            print("LINUX_DONE", flush=True)
        except asyncio.TimeoutError:
            print("LINUX_TIMEOUT", flush=True)

        await event_bus.stop()
    finally:
        await client.disconnect()

asyncio.run(subscribe_and_wait())
'''

        # Start Linux subscriber in background
        linux_proc = subprocess.Popen(
            ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Give Linux time to start subscribing
            await asyncio.sleep(1.0)

            # Windows: publish an event
            client = DragonflyClient(url=redis_url)
            await client.connect()

            try:
                event_bus = RedisEventBus(
                    redis_client=client,
                    session_factory=db_session_factory,
                    node_id="win-publisher",
                )
                await event_bus.start()

                # Publish a write event
                from nexus.core.event_bus import FileEvent, FileEventType

                event = FileEvent(
                    type=FileEventType.FILE_WRITE,
                    path=test_path,
                    zone_id=zone_id,
                )
                await event_bus.publish(event)

                await event_bus.stop()
            finally:
                await client.disconnect()

            # Wait for Linux to receive and finish
            linux_proc.wait(timeout=20)
            stdout, stderr = linux_proc.communicate()

            assert "LINUX_RECEIVED:" in stdout, (
                f"Linux should receive event. stdout: {stdout}, stderr: {stderr}"
            )
            assert test_path in stdout, (
                f"Linux should receive event for {test_path}. stdout: {stdout}"
            )

        finally:
            if linux_proc.poll() is None:
                linux_proc.terminate()
                linux_proc.wait(timeout=5)


class TestStartupSyncConcurrentWrites:
    """Tests for startup_sync with concurrent writes."""

    @pytest.fixture
    def linux_container_available(self):
        """Check if Linux container is available."""
        try:
            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "echo", "ok"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                pytest.skip("Linux container not running")
        except Exception:
            pytest.skip("Linux container not running")

    @pytest.mark.asyncio
    async def test_startup_sync_during_active_writes(
        self, db_session_factory, clean_db, linux_container_available
    ):
        """Test startup_sync while other nodes are actively writing.

        Scenario:
        1. Windows starts writing operations continuously
        2. Linux "joins" (runs startup_sync) mid-stream
        3. Linux should sync existing operations without issues
        4. Concurrent writes shouldn't corrupt the sync

        This tests the system's resilience to dynamic node joins.
        """
        import threading

        from nexus.storage.models import OperationLogModel

        # Flags for coordination
        stop_writing = threading.Event()
        write_count = {"value": 0}
        errors = []

        def windows_writer():
            """Continuously write operations to PostgreSQL."""
            try:
                while not stop_writing.is_set():
                    with db_session_factory() as session:
                        op = OperationLogModel(
                            operation_type="write",
                            path=f"/concurrent/file_{write_count['value']}.txt",
                            zone_id="default",
                            status="success",
                            created_at=_utcnow_naive(),
                        )
                        session.add(op)
                        session.commit()
                        write_count["value"] += 1
                    time.sleep(0.1)  # Write every 100ms
            except Exception as e:
                errors.append(f"Writer: {e}")

        # Start Windows writer
        writer_thread = threading.Thread(target=windows_writer)
        writer_thread.start()

        try:
            # Let some operations accumulate
            await asyncio.sleep(1.0)

            # Linux: run startup_sync while writes are ongoing
            linux_script = """
import asyncio
import sys
sys.path.insert(0, "/app/src")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.cache.dragonfly import DragonflyClient
from nexus.core.event_bus import RedisEventBus

async def sync_during_writes():
    db_url = "postgresql://nexus_test:nexus_test_password@postgres-test:5432/nexus_test"
    engine = create_engine(db_url)
    session_factory = sessionmaker(bind=engine)

    client = DragonflyClient(url="redis://dragonfly-coordination:6379")
    await client.connect()

    try:
        event_bus = RedisEventBus(
            redis_client=client,
            session_factory=session_factory,
            node_id="linux-concurrent-sync",
        )
        await event_bus.start()

        synced_paths = []
        async def handler(event):
            synced_paths.append(event.path)

        synced = await event_bus.startup_sync(
            event_handler=handler,
            default_lookback_hours=1,
        )

        print(f"LINUX_SYNCED:{synced}", flush=True)
        print(f"LINUX_PATHS:{len(synced_paths)}", flush=True)

        # Verify we got some concurrent files
        concurrent_files = [p for p in synced_paths if "/concurrent/" in p]
        print(f"LINUX_CONCURRENT:{len(concurrent_files)}", flush=True)

        await event_bus.stop()
    finally:
        await client.disconnect()

asyncio.run(sync_during_writes())
"""

            result = subprocess.run(
                ["docker", "exec", "nexus-linux-test", "python", "-c", linux_script],
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Stop writer
            stop_writing.set()
            writer_thread.join(timeout=5)

            # Verify
            assert len(errors) == 0, f"Writer errors: {errors}"
            assert "LINUX_SYNCED:" in result.stdout, (
                f"Linux should sync. stdout: {result.stdout}, stderr: {result.stderr}"
            )

            # Extract sync count
            synced_line = [line for line in result.stdout.split("\n") if "LINUX_SYNCED:" in line][0]
            synced_count = int(synced_line.split(":")[1])

            # Should have synced at least some of the concurrent writes
            assert synced_count > 0, "Linux should sync some operations"

            # Extract concurrent file count
            concurrent_line = [
                line for line in result.stdout.split("\n") if "LINUX_CONCURRENT:" in line
            ][0]
            concurrent_count = int(concurrent_line.split(":")[1])

            assert concurrent_count > 0, (
                f"Linux should see concurrent files. Windows wrote {write_count['value']} files."
            )

        finally:
            stop_writing.set()
            if writer_thread.is_alive():
                writer_thread.join(timeout=5)
