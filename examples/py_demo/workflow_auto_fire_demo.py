#!/usr/bin/env python3
"""
Workflow Auto-Fire Demo - Remote Mode

This demo shows how file operations automatically trigger workflows in Nexus v0.7.0+

Architecture:
1. Nexus server running on localhost:8080
2. Client connects remotely
3. Workflows load and execute automatically on file operations

Prerequisites:
- PostgreSQL running (for server mode)
- Nexus server started (see start_server.sh)

Run this demo:
    python workflow_auto_fire_demo.py
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import nexus
from nexus.workflows import WorkflowAPI, WorkflowLoader


def print_header(title: str):
    """Print a formatted header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70 + "\n")


def print_step(step: int, description: str):
    """Print a step marker."""
    print(f"\n📍 Step {step}: {description}")
    print("-" * 70)


async def main():
    """Run the workflow auto-fire demonstration."""

    print_header("🚀 Nexus Workflow Auto-Fire Demo (Remote Mode)")

    # Check for environment variables
    server_url = os.getenv("NEXUS_URL", "http://localhost:8080")
    api_key = os.getenv("NEXUS_API_KEY")

    if not api_key:
        print("⚠️  Warning: NEXUS_API_KEY not set. Using embedded mode instead.")
        print("   For remote mode, run: source .nexus-admin-env")
        print("   Falling back to embedded mode for this demo...\n")

        # Use embedded mode
        nx = nexus.connect(config={"data_dir": "./workflow-demo-data"})
        mode = "embedded"
    else:
        # Use remote mode
        from nexus.remote import RemoteNexusFS

        print(f"✅ Connecting to Nexus server at {server_url}")
        nx = RemoteNexusFS(server_url=server_url, api_key=api_key)
        mode = "remote"

    print(f"   Mode: {mode}")
    print(
        f"   Workflows enabled: {nx.enable_workflows if hasattr(nx, 'enable_workflows') else 'N/A (remote)'}"
    )

    # =========================================================================
    # Step 1: Create workflow definitions
    # =========================================================================

    print_step(1, "Creating Workflow Definitions")

    # Workflow 1: Auto-tag uploaded invoices
    invoice_workflow = """
name: invoice-auto-tagger
version: "1.0"
description: Automatically tag uploaded invoice files

triggers:
  - type: file_write
    pattern: "/uploads/invoices/*.pdf"

actions:
  - name: log-upload
    type: shell
    config:
      command: 'echo "[Invoice] New invoice uploaded: ${context.file_path} (${context.size} bytes)"'
"""

    # Workflow 2: Monitor document deletions
    deletion_workflow = """
name: deletion-monitor
version: "1.0"
description: Monitor and log file deletions

triggers:
  - type: file_delete
    pattern: "/uploads/**/*"

actions:
  - name: log-deletion
    type: shell
    config:
      command: 'echo "[Deletion] File deleted: ${context.file_path} (was ${context.size} bytes)"'
"""

    # Workflow 3: Track file moves
    rename_workflow = """
name: file-move-tracker
version: "1.0"
description: Track when files are moved/renamed

triggers:
  - type: file_rename
    pattern: "/uploads/**/*"

actions:
  - name: log-rename
    type: shell
    config:
      command: 'echo "[Rename] File moved: ${context.old_path} → ${context.new_path}"'
"""

    # Save workflow definitions to files
    workflows_dir = Path("./workflow-demo-data/workflows")
    workflows_dir.mkdir(parents=True, exist_ok=True)

    (workflows_dir / "invoice-tagger.yaml").write_text(invoice_workflow)
    (workflows_dir / "deletion-monitor.yaml").write_text(deletion_workflow)
    (workflows_dir / "rename-tracker.yaml").write_text(rename_workflow)

    print("✅ Created 3 workflow definitions:")
    print("   1. invoice-auto-tagger.yaml - Tags uploaded invoices")
    print("   2. deletion-monitor.yaml - Monitors file deletions")
    print("   3. file-move-tracker.yaml - Tracks file moves")

    # =========================================================================
    # Step 2: Load workflows into Nexus
    # =========================================================================

    print_step(2, "Loading Workflows into Nexus")

    workflows = WorkflowAPI()

    # Load each workflow
    workflow_names = []
    for yaml_file in ["invoice-tagger.yaml", "deletion-monitor.yaml", "rename-tracker.yaml"]:
        yaml_path = workflows_dir / yaml_file
        definition = WorkflowLoader.load_from_file(str(yaml_path))
        workflows.load(definition, enabled=True)
        workflow_names.append(definition.name)
        print(f"   ✅ Loaded: {definition.name}")

    # List all loaded workflows
    print("\n📋 Active workflows:")
    all_workflows = workflows.list()
    for wf in all_workflows:
        status = "🟢 enabled" if wf.get("enabled") else "🔴 disabled"
        print(
            f"   {status} {wf['name']} - {wf.get('triggers', 0)} triggers, {wf.get('actions', 0)} actions"
        )

    # =========================================================================
    # Step 3: Test auto-fire with file operations
    # =========================================================================

    print_step(3, "Testing Auto-Fire with File Operations")

    print("\n🧹 Cleaning up any existing test files from previous runs...")
    # Clean up from previous runs
    cleanup_paths = [
        "/uploads/invoices/invoice-001.pdf",
        "/uploads/invoices/invoice-001-processed.pdf",
        "/uploads/receipts/receipt-001.txt",
        "/uploads/webhooks/test.json",
    ]

    for path in cleanup_paths:
        try:
            if nx.exists(path):
                nx.delete(path)
                print(f"   🗑️  Deleted: {path}")
        except Exception:
            # Ignore errors - file might not exist
            pass

    print("   ✅ Cleanup complete")

    print("\n⏳ Performing file operations (workflows will auto-fire)...\n")

    # Create directories
    nx.mkdir("/uploads/invoices", parents=True)
    nx.mkdir("/uploads/receipts", parents=True)

    # Test 1: Upload invoice (should trigger invoice-auto-tagger)
    print("1️⃣  Uploading invoice PDF...")
    invoice_data = b"%PDF-1.4\nFake invoice content for demo"
    # Use timestamp to ensure unique filename
    import random

    unique_id = random.randint(1000, 9999)
    invoice_filename = f"invoice-{unique_id}.pdf"
    invoice_path = f"/uploads/invoices/{invoice_filename}"
    result = nx.write(invoice_path, invoice_data)
    print(f"   ✅ Written: {result['etag'][:16]}... ({result['size']} bytes)")
    print("   🔄 Workflow 'invoice-auto-tagger' should have fired!")

    # Wait a bit for async execution
    time.sleep(0.5)

    # Test 2: Upload receipt (pattern won't match, no workflow)
    print("\n2️⃣  Uploading receipt (no matching workflow)...")
    receipt_data = b"Receipt content"
    result = nx.write("/uploads/receipts/receipt-001.txt", receipt_data)
    print(f"   ✅ Written: {result['etag'][:16]}... ({result['size']} bytes)")
    print("   ℹ️  No workflow pattern matches this file")

    time.sleep(0.5)

    # Test 3: Rename/move file (should trigger file-move-tracker)
    print("\n3️⃣  Moving invoice file...")
    new_invoice_path = f"/uploads/invoices/{invoice_filename.replace('.pdf', '-processed.pdf')}"
    nx.rename(invoice_path, new_invoice_path)
    print(
        f"   ✅ Renamed: {invoice_filename} → {invoice_filename.replace('.pdf', '-processed.pdf')}"
    )
    print("   🔄 Workflow 'file-move-tracker' should have fired!")

    time.sleep(0.5)

    # Test 4: Delete file (should trigger deletion-monitor)
    print("\n4️⃣  Deleting invoice file...")
    nx.delete(new_invoice_path)
    print(f"   ✅ Deleted: {invoice_filename.replace('.pdf', '-processed.pdf')}")
    print("   🔄 Workflow 'deletion-monitor' should have fired!")

    time.sleep(0.5)

    # =========================================================================
    # Step 4: Verify workflows are active
    # =========================================================================

    print_step(4, "Verifying Workflow Status")

    print("\n✅ All workflows loaded and active:")
    for workflow_name in workflow_names:
        is_enabled = workflows.is_enabled(workflow_name)
        status = "🟢 enabled" if is_enabled else "🔴 disabled"
        print(f"   {status} {workflow_name}")

    print("\nℹ️  Note: Execution history is stored in the database but")
    print("   WorkflowAPI doesn't expose a get_runs() method yet.")
    print("   You can verify workflows fired by checking server logs or")
    print("   by adding logging to your workflow actions.")

    # =========================================================================
    # Step 5: Real-time webhook example
    # =========================================================================

    print_step(5, "Real-Time Webhook Example")

    print("Creating workflow with webhook action...")

    # Note: This webhook will fail unless you have a real endpoint
    # You can use webhook.site to test real webhooks
    webhook_workflow = """
name: real-time-webhook
version: "1.0"
description: Send webhook on file upload

triggers:
  - type: file_write
    pattern: "/uploads/webhooks/*.json"

actions:
  - name: send-webhook
    type: webhook
    config:
      url: "https://webhook.site/unique-id-here"
      method: POST
      body:
        event: "file.uploaded"
        file_path: "${context.file_path}"
        file_size: "${context.size}"
        uploaded_at: "${context.timestamp}"
"""

    (workflows_dir / "webhook-demo.yaml").write_text(webhook_workflow)
    definition = WorkflowLoader.load_from_file(str(workflows_dir / "webhook-demo.yaml"))
    workflows.load(definition, enabled=True)

    print("✅ Loaded webhook workflow")
    print("\nℹ️  To test with a real webhook:")
    print("   1. Go to https://webhook.site")
    print("   2. Copy your unique URL")
    print("   3. Update webhook_workflow.yaml with your URL")
    print("   4. Reload workflow and upload a file to /uploads/webhooks/")

    # Create test file anyway (webhook will fail but shows the concept)
    print("\n📤 Uploading test file (webhook will attempt to fire)...")
    nx.mkdir("/uploads/webhooks", parents=True)
    test_data = json.dumps({"test": "data", "timestamp": time.time()}).encode()
    nx.write("/uploads/webhooks/test.json", test_data)
    print("   ✅ File uploaded, webhook workflow triggered!")
    print("   ⚠️  Webhook delivery will fail (no valid endpoint)")

    # =========================================================================
    # Summary
    # =========================================================================

    print_header("✨ Demo Complete!")

    print("What We Demonstrated:")
    print("  ✅ Workflows automatically fire on file operations")
    print("  ✅ Pattern matching works (e.g., /uploads/invoices/*.pdf)")
    print("  ✅ Multiple triggers: FILE_WRITE, FILE_DELETE, FILE_RENAME")
    print("  ✅ No manual fire_event() calls needed")
    print("  ✅ Workflows execute asynchronously (non-blocking)")

    print("\n🎯 Key Takeaway:")
    print("   In Nexus v0.7.0+, workflows are EVENT-DRIVEN by default.")
    print("   Just write/delete/rename files - workflows trigger automatically!")

    print("\n📚 Next Steps:")
    print("   1. Check workflow execution logs above")
    print("   2. Try creating your own workflow YAML files")
    print("   3. Test with real webhook endpoints (webhook.site)")
    print("   4. Explore workflow actions: tag, parse, llm, python, shell")

    print("\n📁 Files Created:")
    print(f"   - {workflows_dir}/invoice-tagger.yaml")
    print(f"   - {workflows_dir}/deletion-monitor.yaml")
    print(f"   - {workflows_dir}/rename-tracker.yaml")
    print(f"   - {workflows_dir}/webhook-demo.yaml")

    print("\n🧹 Cleanup:")
    print("   To remove demo data: rm -rf ./workflow-demo-data")

    # Close connection
    if hasattr(nx, "close"):
        nx.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Demo interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
