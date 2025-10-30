#!/usr/bin/env python3
"""Setup demo tasks for autonomous agent task workflow.

This script creates initial tasks in the Nexus memory system to demonstrate
how agents can manage and coordinate work using memory primitives.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import nexus


def setup_tasks():
    """Create initial demo tasks."""
    # Connect to local Nexus instance with agent identity
    # Use the same agent_id that will be used by the agent runner
    nx = nexus.connect(
        config={
            "data_dir": "./nexus-task-demo",
            "agent_id": "agent_demo",  # Use same ID as the agent that will process tasks
        }
    )

    # Define initial tasks
    tasks = [
        {"id": "task_001", "title": "Implement authentication", "priority": 1},
        {"id": "task_002", "title": "Design database schema", "priority": 1},
        {"id": "task_003", "title": "Setup CI/CD pipeline", "priority": 2},
        {"id": "task_004", "title": "Write API documentation", "priority": 3},
    ]

    print("🚀 Setting up demo tasks...")
    print("Data directory: ./nexus-task-demo\n")

    # Store each task in memory system
    for t in tasks:
        task_data = {
            "task_id": t["id"],
            "title": t["title"],
            "status": "pending",  # pending | in_progress | completed
            "priority": t["priority"],  # 1=highest
            "blocked_by": [],  # List of task_ids that block this
            "discovered_from": None,  # Parent task_id
            "agent_id": None,
            "created_at": datetime.now().isoformat(),
            "completed_at": None,
        }

        # Serialize task to JSON for storage
        memory_id = nx.memory.store(json.dumps(task_data), scope="agent", memory_type="task")
        print(f"✓ Created: {t['title']} (priority: {t['priority']}) - {memory_id}")

    print(f"\n✅ Created {len(tasks)} initial tasks")
    print("\nRun 'python agent.py' to start the agent workflow.")


if __name__ == "__main__":
    setup_tasks()
