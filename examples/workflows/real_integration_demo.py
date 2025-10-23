"""Real Nexus Integration Demo - Workflows that actually process files.

This demo creates a temporary data directory that is cleaned up after running.
"""

import asyncio
import os
import shutil

import nexus
from nexus.workflows import TriggerType, WorkflowLoader, init_engine
from nexus.workflows.storage import WorkflowStore


async def main():
    """Demonstrate workflows that actually process Nexus files."""
    print("=" * 80)
    print("Nexus Workflow System - Real File Processing Demo")
    print("=" * 80)

    # Use default data directory so Python actions can reconnect
    data_dir = os.getenv("NEXUS_DATA_DIR", "./nexus-data-demo")

    # Setup Nexus
    nx = nexus.connect(config={"data_dir": data_dir})
    nx.mkdir("/inbox", exist_ok=True)
    nx.mkdir("/processed", exist_ok=True)
    nx.mkdir("/archive", exist_ok=True)

    # Setup workflow engine
    session_factory = nx.metadata.SessionLocal
    workflow_store = WorkflowStore(session_factory, tenant_id="demo")
    engine = init_engine(metadata_store=nx.metadata, workflow_store=workflow_store)

    # ================================================================
    # DEMO 1: Process text files - count lines and move
    # ================================================================
    print("\n" + "─" * 80)
    print("DEMO 1: Process incoming text files")
    print("─" * 80)

    wf1 = {
        "name": "process-text-files",
        "version": "1.0",
        "triggers": [{"type": "file_write", "pattern": "/inbox/*.txt"}],
        "actions": [
            {
                "name": "log_file",
                "type": "python",
                "code": 'print(f"Processing: {file_path}")',
            },
            {
                "name": "count_lines",
                "type": "python",
                "code": f"""
import nexus
nx = nexus.connect(config={{"data_dir": "{data_dir}"}})
content = nx.read(file_path)
lines = content.decode().split('\\n')
result = {{"line_count": len(lines), "file": file_path}}
print(f"  Lines: {{len(lines)}}")
""",
            },
            {
                "name": "move_to_processed",
                "type": "python",
                "code": f"""
import nexus
from pathlib import Path
nx = nexus.connect(config={{"data_dir": "{data_dir}"}})
filename = Path(file_path).name
dest = f"/processed/{{filename}}"
nx.rename(file_path, dest)
print(f"  Moved to: {{dest}}")
""",
            },
        ],
    }

    engine.load_workflow(WorkflowLoader.load_from_dict(wf1), enabled=True)

    # Write files and trigger workflow
    print("\n1. Writing test files to /inbox/")
    nx.write("/inbox/file1.txt", b"Line 1\nLine 2\nLine 3")
    nx.write("/inbox/file2.txt", b"Single line")

    print("\n2. Triggering workflow for file1.txt:")
    await engine.fire_event(TriggerType.FILE_WRITE, {"file_path": "/inbox/file1.txt"})

    print("\n3. Triggering workflow for file2.txt:")
    await engine.fire_event(TriggerType.FILE_WRITE, {"file_path": "/inbox/file2.txt"})

    # Verify files were moved
    print("\n4. Verification:")
    inbox_files = nx.list("/inbox")
    processed_files = nx.list("/processed")
    print(f"   Files in /inbox/: {len(inbox_files)}")
    print(f"   Files in /processed/: {len(processed_files)}")
    for f in processed_files:
        print(f"     - {f}")

    # ================================================================
    # DEMO 2: Add metadata to files
    # ================================================================
    print("\n" + "─" * 80)
    print("DEMO 2: Add metadata to processed files")
    print("─" * 80)

    wf2 = {
        "name": "add-metadata",
        "version": "1.0",
        "triggers": [{"type": "file_write", "pattern": "/processed/*"}],
        "actions": [
            {
                "name": "add_timestamp",
                "type": "python",
                "code": f"""
import nexus
from datetime import datetime
nx = nexus.connect(config={{"data_dir": "{data_dir}"}})
nx.metadata.set_file_metadata(file_path, "processed_at", datetime.now().isoformat())
nx.metadata.set_file_metadata(file_path, "status", "processed")
print(f"Added metadata to: {{file_path}}")
""",
            },
        ],
    }

    engine.load_workflow(WorkflowLoader.load_from_dict(wf2), enabled=True)

    print("\n1. Triggering metadata workflow:")
    for f in processed_files:
        await engine.fire_event(TriggerType.FILE_WRITE, {"file_path": f})

    # Verify metadata
    print("\n2. Checking metadata:")
    for f in processed_files:
        status = nx.metadata.get_file_metadata(f, "status")
        processed_at = nx.metadata.get_file_metadata(f, "processed_at")
        print(f"   {f}:")
        print(f"     status: {status or 'N/A'}")
        print(f"     processed_at: {processed_at or 'N/A'}")

    # ================================================================
    # DEMO 3: Archive processed files
    # ================================================================
    print("\n" + "─" * 80)
    print("DEMO 3: Archive processed files")
    print("─" * 80)

    wf3 = {
        "name": "archive-files",
        "version": "1.0",
        "triggers": [{"type": "manual"}],
        "actions": [
            {
                "name": "find_and_archive",
                "type": "python",
                "code": f"""
import nexus
nx = nexus.connect(config={{"data_dir": "{data_dir}"}})
files = nx.list("/processed")
archived_count = 0
for file_path in files:
    if file_path.endswith('.txt'):
        from pathlib import Path
        filename = Path(file_path).name
        dest = f"/archive/{{filename}}"
        nx.rename(file_path, dest)
        archived_count += 1
        print(f"  Archived: {{filename}}")
result = {{"archived": archived_count}}
print(f"Total archived: {{archived_count}}")
""",
            },
        ],
    }

    engine.load_workflow(WorkflowLoader.load_from_dict(wf3), enabled=True)

    print("\n1. Running archive workflow:")
    await engine.trigger_workflow("archive-files", {})

    print("\n2. Verification:")
    processed_files = nx.list("/processed")
    archive_files = nx.list("/archive")
    print(f"   Files in /processed/: {len(processed_files)}")
    print(f"   Files in /archive/: {len(archive_files)}")
    for f in archive_files:
        print(f"     - {f}")

    # ================================================================
    # DEMO 4: LLM-powered document analysis
    # ================================================================
    print("\n" + "─" * 80)
    print("DEMO 4: LLM document analysis and classification")
    print("─" * 80)

    wf4 = {
        "name": "analyze-with-llm",
        "version": "1.0",
        "triggers": [{"type": "manual"}],
        "actions": [
            {
                "name": "analyze_document",
                "type": "llm",
                "model": "claude-sonnet-4",
                "file_path": "{file_path}",
                "prompt": "Analyze this document and extract: 1) main topic, 2) document type (invoice/report/email/other), 3) suggested tags. Return as JSON with keys: topic, doc_type, tags (array)",
                "output_format": "json",
            },
            {
                "name": "save_analysis",
                "type": "python",
                "code": f"""
import nexus
nx = nexus.connect(config={{"data_dir": "{data_dir}"}})
analysis = variables.get('analyze_document_output', {{}})
print(f"  Analysis results:")
print(f"    Topic: {{analysis.get('topic', 'N/A')}}")
print(f"    Type: {{analysis.get('doc_type', 'N/A')}}")
print(f"    Tags: {{', '.join(analysis.get('tags', []))}}")

# Save as metadata
nx.metadata.set_file_metadata(file_path, "llm_topic", analysis.get('topic', ''))
nx.metadata.set_file_metadata(file_path, "llm_doc_type", analysis.get('doc_type', ''))
nx.metadata.set_file_metadata(file_path, "llm_tags", ','.join(analysis.get('tags', [])))
result = {{"analysis": analysis}}
""",
            },
        ],
    }

    engine.load_workflow(WorkflowLoader.load_from_dict(wf4), enabled=True)

    # Create a document to analyze
    print("\n1. Creating sample document:")
    invoice_content = """INVOICE #12345
Date: 2024-01-15
Customer: Acme Corp
Items:
- Software License: $500
- Support Package: $200
Total: $700
Payment Terms: Net 30
"""
    nx.write("/archive/invoice_12345.txt", invoice_content.encode())
    print("   Created: /archive/invoice_12345.txt")

    print("\n2. Running LLM analysis workflow:")
    print("   Note: Requires LLM provider configured (OPENROUTER_API_KEY)")
    try:
        await engine.trigger_workflow(
            "analyze-with-llm", {"file_path": "/archive/invoice_12345.txt"}
        )

        # Show the metadata
        print("\n3. Checking LLM-generated metadata:")
        llm_topic = nx.metadata.get_file_metadata("/archive/invoice_12345.txt", "llm_topic")
        llm_doc_type = nx.metadata.get_file_metadata("/archive/invoice_12345.txt", "llm_doc_type")
        llm_tags = nx.metadata.get_file_metadata("/archive/invoice_12345.txt", "llm_tags")
        print(f"   Topic: {llm_topic or 'N/A'}")
        print(f"   Doc Type: {llm_doc_type or 'N/A'}")
        print(f"   Tags: {llm_tags or 'N/A'}")
    except Exception as e:
        print(f"   ⚠️  LLM action failed (may need API key): {e}")

    # ================================================================
    # Summary
    # ================================================================
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    # Final counts
    inbox = nx.list("/inbox")
    processed = nx.list("/processed")
    archive = nx.list("/archive")

    print(f"""
✅ Demonstrated real Nexus file operations:
   - nx.write() - Created files
   - nx.read() - Read file contents
   - nx.rename() - Moved files between directories
   - nx.metadata operations - Added metadata to files

✅ Demonstrated workflow action types:
   - Python actions that call Nexus API
   - LLM actions for document analysis and classification
   - File processing (line counting)
   - File movement/archiving
   - Metadata management

✅ File pipeline results:
   /inbox/: {len(inbox)} files
   /processed/: {len(processed)} files
   /archive/: {len(archive)} files

Note: In v0.4.0, events are fired manually.
      In v0.5.0+, Nexus operations will auto-trigger workflows!

Data directory: {data_dir}
""")

    nx.close()

    # Cleanup: Remove demo data directory
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
        print(f"\nCleaned up: {data_dir}")


if __name__ == "__main__":
    asyncio.run(main())
