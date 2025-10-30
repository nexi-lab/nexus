#!/usr/bin/env python3
"""Autonomous agent task workflow using Nexus memory system.

This demo showcases how to build an autonomous task management agent using
Nexus memory primitives. The agent manages tasks through pure CRUD operations
without requiring a dedicated task management system.

Workflow:
1. Find ready work - Query for tasks with no blocking dependencies
2. Claim task - Mark task as in_progress
3. Execute work - Simulate work (sleep 1 second)
4. Discover new issues - Randomly find related work (50% chance)
5. Link discoveries - Store discovered-from relationship
6. Complete task - Mark as closed and repeat

This demonstrates:
- Flexible data storage with Nexus memory system
- Agent identity and permissions
- Task discovery and dynamic workflow generation
- Multi-agent coordination capabilities
"""

import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import nexus


def parse_task(memory_dict):
    """Parse a task from memory storage format.

    Args:
        memory_dict: Memory dictionary from query

    Returns:
        Task dict with parsed content and memory_id
    """
    task_data = json.loads(memory_dict["content"])
    return {"memory_id": memory_dict["memory_id"], "data": task_data}


def find_ready_work(nx, limit=1):
    """Find tasks with no blockers.

    Args:
        nx: Nexus connection
        limit: Maximum number of tasks to return

    Returns:
        List of ready tasks sorted by priority
    """
    all_memories = nx.memory.query(scope="agent", memory_type="task")
    all_tasks = [parse_task(m) for m in all_memories]

    # Filter to pending tasks with no blockers
    ready = [
        t for t in all_tasks if t["data"]["status"] == "pending" and not t["data"]["blocked_by"]
    ]

    # Sort by priority (1=highest)
    ready.sort(key=lambda t: t["data"]["priority"])
    return ready[:limit]


def update_task(nx, memory_id, task_data):
    """Update a task by deleting and recreating.

    Args:
        nx: Nexus connection
        memory_id: Memory ID of the task to update
        task_data: Updated task data dict

    Returns:
        New memory_id
    """
    # Delete old version
    nx.memory.delete(memory_id)

    # Create new version with updated data
    new_memory_id = nx.memory.store(json.dumps(task_data), scope="agent", memory_type="task")

    return new_memory_id


def claim_task(nx, memory_id, agent_id):
    """Claim a task by marking it as in_progress.

    Args:
        nx: Nexus connection
        memory_id: Memory ID of the task
        agent_id: ID of the agent claiming the task

    Returns:
        New memory_id after update
    """
    memory = nx.memory.get(memory_id)
    task_data = json.loads(memory["content"])

    task_data["status"] = "in_progress"
    task_data["agent_id"] = agent_id

    return update_task(nx, memory_id, task_data)


def create_discovered_task(nx, parent_task):
    """Create a new task discovered during work.

    Args:
        nx: Nexus connection
        parent_task: The parsed parent task dict

    Returns:
        Task ID of the newly created task
    """
    new_id = f"task_{random.randint(1000, 9999)}"

    task_data = {
        "task_id": new_id,
        "title": f"Test: {parent_task['data']['title']}",
        "status": "pending",
        "priority": 2,
        "blocked_by": [],
        "discovered_from": parent_task["data"]["task_id"],
        "agent_id": None,
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
    }

    nx.memory.store(json.dumps(task_data), scope="agent", memory_type="task")

    return new_id


def execute_task(nx, task):
    """Execute task and maybe discover new work.

    Args:
        nx: Nexus connection
        task: Parsed task dict

    Returns:
        List of discovered task IDs
    """
    print(f"  Working on: {task['data']['title']}")
    time.sleep(1)  # Simulate work

    discovered = []
    if random.random() < 0.5:  # 50% chance of discovery
        new_id = create_discovered_task(nx, task)
        discovered.append(new_id)
        print(f"  → Discovered: {new_id}")

    return discovered


def complete_task(nx, memory_id):
    """Mark task as completed.

    Args:
        nx: Nexus connection
        memory_id: Memory ID of the task

    Returns:
        New memory_id after update
    """
    memory = nx.memory.get(memory_id)
    task_data = json.loads(memory["content"])

    task_data["status"] = "completed"
    task_data["completed_at"] = datetime.now().isoformat()

    return update_task(nx, memory_id, task_data)


class TaskAgent:
    """Autonomous agent that processes tasks using Nexus memory system."""

    def __init__(self, agent_id="agent_demo", data_dir="./nexus-task-demo"):
        """Initialize the task agent.

        Args:
            agent_id: Unique identifier for this agent
            data_dir: Directory for Nexus data storage
        """
        self.agent_id = agent_id
        self.nx = nexus.connect(
            config={
                "data_dir": data_dir,
                "agent_id": agent_id,  # Set agent identity for memory access
            }
        )

    def show_stats(self):
        """Show task statistics."""
        memories = self.nx.memory.query(scope="agent", memory_type="task")
        tasks = [parse_task(m) for m in memories]
        stats = {"pending": 0, "in_progress": 0, "completed": 0}

        for t in tasks:
            stats[t["data"]["status"]] += 1

        print(
            f"Stats: {stats['pending']} pending | "
            f"{stats['in_progress']} in progress | "
            f"{stats['completed']} completed\n"
        )

    def run(self, max_iterations=10):
        """Run agent workflow.

        Args:
            max_iterations: Maximum number of tasks to process
        """
        print("🚀 Nexus Autonomous Task Agent")
        print(f"Agent ID: {self.agent_id}\n")

        for i in range(max_iterations):
            print(f"=== Iteration {i + 1} ===")

            # 1. Find ready work
            ready = find_ready_work(self.nx, limit=1)
            if not ready:
                print("No ready work. Done!")
                break

            task = ready[0]
            print(f"Found: {task['data']['title']}")

            # 2. Claim task (returns new memory_id)
            memory_id = claim_task(self.nx, task["memory_id"], self.agent_id)

            # 3. Execute work (and possibly discover new tasks)
            execute_task(self.nx, task)

            # 4. Complete task (returns new memory_id)
            complete_task(self.nx, memory_id)
            print("✓ Completed!")

            # 5. Show stats
            self.show_stats()
            time.sleep(0.5)

        print("=" * 50)
        print("Agent workflow completed!")
        self.show_final_summary()

    def show_final_summary(self):
        """Show final summary of all tasks."""
        memories = self.nx.memory.query(scope="agent", memory_type="task")
        tasks = [parse_task(m) for m in memories]

        print("\nFinal Task Summary:")
        print("-" * 50)

        # Group by status
        by_status = {"pending": [], "in_progress": [], "completed": []}
        for t in tasks:
            by_status[t["data"]["status"]].append(t)

        # Show completed tasks
        if by_status["completed"]:
            print(f"\n✅ Completed ({len(by_status['completed'])}):")
            for t in by_status["completed"]:
                discovered_from = t["data"].get("discovered_from")
                suffix = f" (discovered from {discovered_from})" if discovered_from else ""
                print(f"  - {t['data']['title']}{suffix}")

        # Show pending tasks
        if by_status["pending"]:
            print(f"\n⏳ Pending ({len(by_status['pending'])}):")
            for t in by_status["pending"]:
                discovered_from = t["data"].get("discovered_from")
                suffix = f" (discovered from {discovered_from})" if discovered_from else ""
                print(f"  - {t['data']['title']}{suffix}")

        # Show in-progress tasks (should be none)
        if by_status["in_progress"]:
            print(f"\n🔄 In Progress ({len(by_status['in_progress'])}):")
            for t in by_status["in_progress"]:
                print(f"  - {t['data']['title']}")


def main():
    """Main entry point."""
    agent = TaskAgent()
    agent.run(max_iterations=10)


if __name__ == "__main__":
    main()
