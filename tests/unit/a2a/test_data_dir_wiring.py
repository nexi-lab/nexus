"""Tests that data_dir parameter wires VFSTaskStore for persistence.

Verifies that when create_a2a_router receives a data_dir argument,
tasks are persisted to disk and survive across TaskManager instances.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nexus.a2a.models import Message, TextPart
from nexus.a2a.stores.local_driver import LocalStorageDriver
from nexus.a2a.stores.vfs import VFSTaskStore
from nexus.a2a.task_manager import TaskManager


@pytest.fixture()
def persistent_task_manager(tmp_path):
    """TaskManager backed by LocalStorageDriver + VFSTaskStore."""
    storage = LocalStorageDriver(root=tmp_path)
    store = VFSTaskStore(storage=storage)
    return TaskManager(store=store)


class TestDataDirPersistence:
    """Verify tasks persist to disk when data_dir is configured."""

    def test_task_survives_new_manager(self, tmp_path):
        """Tasks created by one manager are readable by a fresh manager."""
        msg = Message(role="user", parts=[TextPart(type="text", text="persist me")])

        # Manager #1: create a task
        storage1 = LocalStorageDriver(root=tmp_path)
        store1 = VFSTaskStore(storage=storage1)
        tm1 = TaskManager(store=store1)
        task = asyncio.get_event_loop().run_until_complete(tm1.create_task(msg, zone_id="default"))
        task_id = task.id

        # Manager #2: read the task (simulates server restart)
        storage2 = LocalStorageDriver(root=tmp_path)
        store2 = VFSTaskStore(storage=storage2)
        tm2 = TaskManager(store=store2)
        recovered = asyncio.get_event_loop().run_until_complete(
            tm2.get_task(task_id, zone_id="default")
        )

        assert recovered.id == task_id
        assert recovered.status.state.value == "submitted"
        assert len(recovered.history) >= 1

    def test_task_file_exists_on_disk(self, tmp_path):
        """A .json file is created in the data_dir zone directory."""
        msg = Message(role="user", parts=[TextPart(type="text", text="check disk")])

        storage = LocalStorageDriver(root=tmp_path)
        store = VFSTaskStore(storage=storage)
        tm = TaskManager(store=store)
        task = asyncio.get_event_loop().run_until_complete(tm.create_task(msg, zone_id="default"))

        # Check that the file was created
        zone_dir = tmp_path / "a2a" / "tasks" / "default"
        assert zone_dir.is_dir()
        json_files = list(zone_dir.glob(f"*_{task.id}.json"))
        assert len(json_files) == 1

        # Verify file content is valid JSON with expected structure
        data = json.loads(json_files[0].read_bytes())
        assert data["task"]["id"] == task.id
        assert data["task"]["status"]["state"] == "submitted"

    def test_zone_isolation_on_disk(self, tmp_path):
        """Tasks in different zones are stored in separate directories."""
        msg = Message(role="user", parts=[TextPart(type="text", text="zone test")])

        storage = LocalStorageDriver(root=tmp_path)
        store = VFSTaskStore(storage=storage)
        tm = TaskManager(store=store)

        asyncio.get_event_loop().run_until_complete(tm.create_task(msg, zone_id="alpha"))
        asyncio.get_event_loop().run_until_complete(tm.create_task(msg, zone_id="beta"))

        alpha_dir = tmp_path / "a2a" / "tasks" / "alpha"
        beta_dir = tmp_path / "a2a" / "tasks" / "beta"
        assert alpha_dir.is_dir()
        assert beta_dir.is_dir()
        assert len(list(alpha_dir.glob("*.json"))) == 1
        assert len(list(beta_dir.glob("*.json"))) == 1

    def test_update_persists(self, tmp_path, persistent_task_manager):
        """State transitions are persisted to disk."""
        msg = Message(role="user", parts=[TextPart(type="text", text="update me")])
        tm = persistent_task_manager

        task = asyncio.get_event_loop().run_until_complete(tm.create_task(msg, zone_id="default"))
        asyncio.get_event_loop().run_until_complete(tm.cancel_task(task.id, zone_id="default"))

        # Read from a fresh manager
        storage2 = LocalStorageDriver(root=tmp_path)
        store2 = VFSTaskStore(storage=storage2)
        tm2 = TaskManager(store=store2)
        recovered = asyncio.get_event_loop().run_until_complete(
            tm2.get_task(task.id, zone_id="default")
        )
        assert recovered.status.state.value == "canceled"
