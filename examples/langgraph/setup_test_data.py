#!/usr/bin/env python3
"""Setup test data on remote Nexus server for demo."""

import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from nexus.remote import RemoteNexusFS

# Connect to remote server
print("Connecting to remote Nexus server...")
nx = RemoteNexusFS(
    server_url="http://136.117.224.98",
    api_key=None,  # No API key needed for this server
)
print("✓ Connected")

# Create test Python files with async patterns
print("\nCreating test files...")

# File 1: API client with async
api_code = '''"""Async HTTP API client."""
import asyncio
import aiohttp

async def fetch_data(url: str):
    """Fetch data from URL asynchronously."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            return await response.json()

async def fetch_multiple(urls: list):
    """Fetch multiple URLs concurrently."""
    tasks = [fetch_data(url) for url in urls]
    return await asyncio.gather(*tasks)
'''

nx.write("/workspace/api_client.py", api_code.encode())
print("✓ Created /workspace/api_client.py")

# File 2: Database handler with async
db_code = '''"""Async database handler."""
import asyncio
import asyncpg

async def execute_query(query: str):
    """Execute database query asynchronously."""
    conn = await asyncpg.connect('postgresql://localhost/db')
    try:
        result = await conn.fetch(query)
        return result
    finally:
        await conn.close()

async def batch_insert(records: list):
    """Insert multiple records concurrently."""
    conn = await asyncpg.connect('postgresql://localhost/db')
    try:
        await conn.executemany(
            'INSERT INTO users VALUES($1, $2)',
            records
        )
    finally:
        await conn.close()
'''

nx.write("/workspace/database.py", db_code.encode())
print("✓ Created /workspace/database.py")

# File 3: Worker with async tasks
worker_code = '''"""Async task worker."""
import asyncio
from typing import Any

async def process_task(task_id: int, data: Any):
    """Process a single task asynchronously."""
    await asyncio.sleep(1)  # Simulate work
    return f"Task {task_id} complete"

async def worker(queue: asyncio.Queue):
    """Worker that processes tasks from queue."""
    while True:
        task = await queue.get()
        result = await process_task(task['id'], task['data'])
        print(result)
        queue.task_done()
'''

nx.write("/workspace/worker.py", worker_code.encode())
print("✓ Created /workspace/worker.py")

# File 4: Regular file without async
regular_code = '''"""Regular synchronous utility functions."""

def calculate_sum(numbers: list):
    """Calculate sum of numbers."""
    return sum(numbers)

def format_output(data: dict):
    """Format data for output."""
    return f"Data: {data}"
'''

nx.write("/workspace/utils.py", regular_code.encode())
print("✓ Created /workspace/utils.py")

print("\n✅ Test data setup complete!")
print("\nFiles created:")
print("  - /workspace/api_client.py (async)")
print("  - /workspace/database.py (async)")
print("  - /workspace/worker.py (async)")
print("  - /workspace/utils.py (sync)")
