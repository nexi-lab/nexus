"""Example of using the Nexus Workflow API.

This script demonstrates how to:
1. Load workflow definitions
2. List workflows
3. Execute workflows manually
4. Enable/disable workflows
5. Fire events that trigger workflows
"""

import asyncio
from pathlib import Path

from nexus.workflows import TriggerType, WorkflowAPI


async def main():
    """Main example function."""
    # Create workflow API instance
    workflows = WorkflowAPI()

    print("=== Nexus Workflow API Example ===\n")

    # Example 1: Load a workflow from file
    print("1. Loading workflow from file...")
    workflow_file = Path(__file__).parent / "auto-tag-documents.yaml"
    if workflow_file.exists():
        workflows.load(workflow_file, enabled=True)
        print(f"   ✓ Loaded workflow from {workflow_file.name}\n")
    else:
        print(f"   ! Workflow file not found: {workflow_file}\n")

    # Example 2: Load a workflow from dictionary
    print("2. Loading workflow from dictionary...")
    simple_workflow = {
        "name": "example-workflow",
        "version": "1.0",
        "description": "Simple example workflow",
        "triggers": [{"type": "file_write", "pattern": "/test/**/*.txt"}],
        "actions": [
            {
                "name": "tag_file",
                "type": "tag",
                "tags": ["example", "test"],
            },
            {
                "name": "log_action",
                "type": "python",
                "code": 'print(f"Processing file: {file_path}")',
            },
        ],
    }
    workflows.load(simple_workflow, enabled=True)
    print("   ✓ Loaded example-workflow\n")

    # Example 3: List all workflows
    print("3. Listing all workflows...")
    for workflow in workflows.list():
        status = "✓ enabled" if workflow["enabled"] else "✗ disabled"
        print(f"   - {workflow['name']} v{workflow['version']} ({status})")
        print(f"     {workflow['description']}")
        print(f"     Triggers: {workflow['triggers']}, Actions: {workflow['actions']}")
    print()

    # Example 4: Get a specific workflow
    print("4. Getting workflow details...")
    workflow_def = workflows.get("example-workflow")
    if workflow_def:
        print(f"   Name: {workflow_def.name}")
        print(f"   Version: {workflow_def.version}")
        print(f"   Triggers: {len(workflow_def.triggers)}")
        print(f"   Actions: {len(workflow_def.actions)}")
        for action in workflow_def.actions:
            print(f"     - {action.name} ({action.type})")
    print()

    # Example 5: Execute a workflow manually
    print("5. Executing workflow manually...")
    execution = await workflows.execute(
        "example-workflow",
        file_path="/test/example.txt",
        context={"author": "test-user"},
    )

    if execution:
        print(f"   Execution ID: {execution.execution_id}")
        print(f"   Status: {execution.status.value}")
        print(f"   Actions completed: {execution.actions_completed}/{execution.actions_total}")

        if execution.action_results:
            print("   Action results:")
            for result in execution.action_results:
                status_icon = "✓" if result.success else "✗"
                print(f"     {status_icon} {result.action_name} ({result.duration_ms:.2f}ms)")
                if result.error:
                    print(f"       Error: {result.error}")
    print()

    # Example 6: Disable/Enable workflow
    print("6. Managing workflow state...")
    print("   Disabling workflow...")
    workflows.disable("example-workflow")
    print(f"   Status: {workflows.get_status('example-workflow')}")

    print("   Enabling workflow...")
    workflows.enable("example-workflow")
    print(f"   Status: {workflows.get_status('example-workflow')}")
    print()

    # Example 7: Fire an event
    print("7. Firing a file write event...")
    triggered = await workflows.fire_event(
        TriggerType.FILE_WRITE, {"file_path": "/test/another-file.txt"}
    )
    print(f"   Triggered {triggered} workflow(s)\n")

    # Example 8: Discover workflows in directory
    print("8. Discovering workflows in current directory...")
    workflows_dir = Path(__file__).parent
    discovered = workflows.discover(workflows_dir, load=False)
    print(f"   Found {len(discovered)} workflow(s):")
    for wf in discovered:
        print(f"     - {wf.name} v{wf.version}")
    print()

    # Example 9: Unload workflow
    print("9. Unloading workflow...")
    workflows.unload("example-workflow")
    print("   ✓ Unloaded example-workflow\n")

    print("=== Example completed ===")


if __name__ == "__main__":
    asyncio.run(main())
