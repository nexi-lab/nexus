# Workflow Automation

**Build event-driven workflows that respond to file changes automatically**

⏱️ **Time:** 15 minutes | 💡 **Difficulty:** Intermediate

## What You'll Learn

- Create automated workflows in Nexus
- Trigger workflows on file events
- Build multi-step automation pipelines
- Monitor workflow execution
- Handle workflow errors and retries
- Use workflows with remote Nexus server

## Prerequisites

✅ Python 3.8+ installed
✅ Nexus installed (`pip install nexus-ai-fs`)
✅ Basic understanding of file operations ([Simple File Storage](simple-file-storage.md))
✅ Familiarity with Python async/await (helpful but not required)

## Overview

Nexus workflows enable **event-driven automation** that responds to file system changes in real-time. Instead of polling for changes or running scheduled tasks, workflows execute automatically when specific events occur.

**Use Cases:**
- 📄 Process documents as they're uploaded
- 🔄 Sync files between systems
- 🤖 Trigger AI agent actions on new data
- 📊 Generate reports automatically
- 🔔 Send notifications on file changes
- 🗂️ Organize files based on content

**Architecture:**

```
┌─────────────────────────────────────────────────┐
│                 Your Application                │
│  ┌──────────────┐        ┌──────────────┐      │
│  │  Upload File │        │ Define       │      │
│  │  to Nexus    │───────▶│ Workflow     │      │
│  └──────────────┘        └──────┬───────┘      │
└─────────────────────────────────┼──────────────┘
                                  │ HTTP + API Key
                                  ↓
┌─────────────────────────────────────────────────┐
│              Nexus Server (Remote)              │
│  ┌──────────────────────────────────────────┐  │
│  │     Workflow Engine (Background)         │  │
│  │  ┌────────────┐  ┌────────────┐         │  │
│  │  │ Event      │  │ Workflow   │         │  │
│  │  │ Listener   │─▶│ Executor   │         │  │
│  │  └────────────┘  └──────┬─────┘         │  │
│  └────────────────────────┼────────────────┘  │
│         ↓                  ↓                   │
│  ┌──────────────┐  ┌──────────────┐          │
│  │ File Storage │  │ Workflow     │          │
│  │              │  │ History      │          │
│  └──────────────┘  └──────────────┘          │
└─────────────────────────────────────────────────┘
```

---

## Step 1: Start Nexus Server

Start a Nexus server to handle workflows:

```bash
# Start server with workflow support
nexus serve --host 0.0.0.0 --port 8080 --data-dir ./nexus-data &

# Wait for server to start
sleep 2

# Verify server is running
curl http://localhost:8080/health
```

**Expected output:**
```json
{"status":"ok","version":"0.5.0"}
```

---

## Step 2: Setup Authentication

Create an admin user and API key:

```bash
# Create admin user
nexus admin create-user admin \
  --name "Workflow Admin" \
  --email "admin@example.com"

# Create API key
nexus admin create-user-key admin \
  --description "Workflow automation key"
```

**Save the API key** and export it:

```bash
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=nxk_1234567890abcdef...  # Use YOUR key
```

---

## Step 3: Create Your First Workflow

Let's create a workflow that automatically processes text files when they're uploaded:

```python
# workflow_demo.py
import nexus
import asyncio

# Connect to remote server
nx = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "nxk_1234567890abcdef..."  # Replace with YOUR key
})

# Define a workflow function
async def process_text_file(event):
    """
    This function runs automatically when a .txt file is created
    """
    file_path = event['path']
    print(f"🔄 Processing new file: {file_path}")

    # Read the file content
    content = nx.read(file_path)
    text = content.decode('utf-8')

    # Process: count words
    word_count = len(text.split())

    # Create a summary file
    summary_path = file_path.replace('.txt', '_summary.txt')
    summary = f"File: {file_path}\nWords: {word_count}\nContent:\n{text[:100]}..."

    nx.write(summary_path, summary.encode('utf-8'))
    print(f"✅ Summary created: {summary_path}")

    return {"word_count": word_count, "summary_path": summary_path}

# Register the workflow
workflow_id = nx.register_workflow(
    name="text_processor",
    handler=process_text_file,
    trigger={
        "event": "file.created",
        "pattern": "/workspace/uploads/*.txt"  # Only .txt files
    },
    description="Automatically process uploaded text files"
)

print(f"✅ Workflow registered: {workflow_id}")
print("👂 Listening for file uploads...")

# Keep the workflow running
asyncio.get_event_loop().run_forever()
```

**Run it:**

```bash
python workflow_demo.py
```

---

## Step 4: Trigger the Workflow

Now upload a text file to trigger the workflow:

```python
# trigger_workflow.py
import nexus

nx = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "nxk_1234567890abcdef..."
})

# Upload a test file
content = b"""
Nexus is an AI-native distributed filesystem designed for building agents.
It provides persistent memory, permissions, and workflow automation.
This file will be automatically processed by our workflow!
"""

nx.write("/workspace/uploads/test.txt", content)
print("✅ File uploaded - workflow should trigger!")
```

**Run in a separate terminal:**

```bash
python trigger_workflow.py
```

**Expected output in workflow_demo.py terminal:**
```
🔄 Processing new file: /workspace/uploads/test.txt
✅ Summary created: /workspace/uploads/test_summary.txt
```

**Verify the summary was created:**

```bash
nexus cat /workspace/uploads/test_summary.txt
```

---

## Step 5: Multi-Step Workflow Pipeline

Build a more complex workflow with multiple steps:

```python
# pipeline_workflow.py
import nexus
import asyncio
import json

nx = nexus.connect()  # Uses environment variables

async def step1_extract_metadata(event):
    """Step 1: Extract file metadata"""
    file_path = event['path']
    print(f"📊 Step 1: Extracting metadata from {file_path}")

    content = nx.read(file_path).decode('utf-8')

    metadata = {
        "file_path": file_path,
        "size_bytes": len(content),
        "word_count": len(content.split()),
        "line_count": len(content.split('\n')),
        "char_count": len(content)
    }

    # Store metadata
    meta_path = file_path + ".meta.json"
    nx.write(meta_path, json.dumps(metadata, indent=2).encode())

    print(f"✅ Metadata saved to {meta_path}")
    return metadata

async def step2_categorize(event):
    """Step 2: Categorize based on size"""
    meta_path = event['path']
    print(f"🏷️  Step 2: Categorizing {meta_path}")

    metadata = json.loads(nx.read(meta_path).decode('utf-8'))

    # Categorize by size
    if metadata['word_count'] < 50:
        category = "short"
    elif metadata['word_count'] < 200:
        category = "medium"
    else:
        category = "long"

    # Move to category folder
    original_path = metadata['file_path']
    category_path = f"/workspace/categorized/{category}/{original_path.split('/')[-1]}"

    nx.copy(original_path, category_path)
    print(f"✅ Categorized as '{category}' → {category_path}")

    return {"category": category, "path": category_path}

async def step3_notify(event):
    """Step 3: Send notification (simulated)"""
    print(f"🔔 Step 3: Notification sent for {event['path']}")

    # In real app, send email/webhook/slack notification
    notification = {
        "event": "file_processed",
        "timestamp": event.get('timestamp'),
        "file": event['path']
    }

    # Log notification
    log_path = "/workspace/notifications.log"
    nx.append(log_path, (json.dumps(notification) + '\n').encode())

    print(f"✅ Logged to {log_path}")
    return notification

# Register pipeline: Step 1 → Step 2 → Step 3
workflow_id = nx.register_workflow_pipeline(
    name="document_processor",
    steps=[
        {
            "name": "extract_metadata",
            "handler": step1_extract_metadata,
            "trigger": {
                "event": "file.created",
                "pattern": "/workspace/inbox/*.txt"
            }
        },
        {
            "name": "categorize",
            "handler": step2_categorize,
            "trigger": {
                "event": "file.created",
                "pattern": "**/*.meta.json"  # Triggered by step 1
            }
        },
        {
            "name": "notify",
            "handler": step3_notify,
            "trigger": {
                "event": "file.created",
                "pattern": "/workspace/categorized/**/*.txt"  # Triggered by step 2
            }
        }
    ],
    description="Multi-step document processing pipeline"
)

print(f"✅ Pipeline registered: {workflow_id}")
print("👂 Pipeline ready...")

asyncio.get_event_loop().run_forever()
```

**Test the pipeline:**

```python
# trigger_pipeline.py
import nexus

nx = nexus.connect()

# Upload a document
nx.write("/workspace/inbox/report.txt", b"""
Executive Summary: Q4 2024 Results

Our AI-powered platform achieved 150% growth this quarter.
Key highlights:
- User base grew to 50,000 active users
- Revenue increased by 200%
- New features: workflow automation, ReBAC permissions
- Customer satisfaction: 95% positive feedback

Looking ahead to 2025, we're focused on scalability and enterprise features.
""".strip())

print("✅ Document uploaded - pipeline will execute 3 steps automatically!")
```

**Expected workflow output:**
```
📊 Step 1: Extracting metadata from /workspace/inbox/report.txt
✅ Metadata saved to /workspace/inbox/report.txt.meta.json

🏷️  Step 2: Categorizing /workspace/inbox/report.txt.meta.json
✅ Categorized as 'medium' → /workspace/categorized/medium/report.txt

🔔 Step 3: Notification sent for /workspace/categorized/medium/report.txt
✅ Logged to /workspace/notifications.log
```

---

## Step 6: Error Handling and Retries

Add robust error handling to workflows:

```python
# robust_workflow.py
import nexus
import asyncio
from datetime import datetime

nx = nexus.connect()

async def safe_process_file(event):
    """Workflow with comprehensive error handling"""
    file_path = event['path']

    try:
        print(f"🔄 Processing {file_path}...")

        # Read file with timeout
        content = nx.read(file_path, timeout=10)

        # Validate content
        if not content:
            raise ValueError("File is empty")

        # Process...
        result = process_content(content)

        # Save result
        result_path = file_path + ".result"
        nx.write(result_path, result.encode())

        print(f"✅ Success: {result_path}")
        return {"status": "success", "result_path": result_path}

    except nexus.NexusFileNotFoundError:
        print(f"❌ File not found: {file_path}")
        # Log error but don't retry
        log_error(file_path, "not_found", retry=False)

    except ValueError as e:
        print(f"❌ Validation error: {e}")
        log_error(file_path, str(e), retry=False)

    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        # Log and retry
        log_error(file_path, str(e), retry=True)
        raise  # Re-raise to trigger retry

def process_content(content):
    """Process file content (example)"""
    text = content.decode('utf-8')
    return f"Processed at {datetime.now()}: {len(text)} bytes"

def log_error(path, error, retry=False):
    """Log errors to a dedicated error log"""
    error_log = {
        "timestamp": datetime.now().isoformat(),
        "file": path,
        "error": error,
        "retry": retry
    }
    nx.append(
        "/workspace/errors.log",
        (json.dumps(error_log) + '\n').encode()
    )

# Register with retry configuration
workflow_id = nx.register_workflow(
    name="safe_processor",
    handler=safe_process_file,
    trigger={
        "event": "file.created",
        "pattern": "/workspace/process/*.txt"
    },
    retry_policy={
        "max_attempts": 3,
        "backoff_seconds": 5,  # 5s, 10s, 15s delays
        "backoff_multiplier": 1.5
    },
    description="Fault-tolerant file processor"
)

print(f"✅ Robust workflow registered: {workflow_id}")
asyncio.get_event_loop().run_forever()
```

---

## Step 7: Monitor Workflow Execution

Track and monitor your workflows:

```python
# monitor_workflows.py
import nexus

nx = nexus.connect()

# List all registered workflows
workflows = nx.list_workflows()
print(f"📋 Registered Workflows ({len(workflows)}):")
for wf in workflows:
    print(f"  - {wf['name']}: {wf['description']}")
    print(f"    Trigger: {wf['trigger']}")
    print(f"    Status: {wf['status']}")
    print()

# Get execution history for a specific workflow
history = nx.get_workflow_history("text_processor", limit=10)
print(f"📊 Recent Executions:")
for exec in history:
    status_icon = "✅" if exec['status'] == "success" else "❌"
    print(f"  {status_icon} {exec['timestamp']}: {exec['file']} - {exec['duration']}ms")

# Get workflow statistics
stats = nx.get_workflow_stats("text_processor")
print(f"\n📈 Workflow Statistics:")
print(f"  Total executions: {stats['total']}")
print(f"  Success rate: {stats['success_rate']}%")
print(f"  Avg duration: {stats['avg_duration_ms']}ms")
print(f"  Errors: {stats['errors']}")
```

---

## Step 8: Workflow Control

Pause, resume, and stop workflows:

```python
# control_workflows.py
import nexus

nx = nexus.connect()

# Pause a workflow (stop processing new events)
nx.pause_workflow("text_processor")
print("⏸️  Workflow paused")

# Upload file - won't be processed
nx.write("/workspace/uploads/test2.txt", b"This won't be processed")
print("📄 File uploaded (not processed)")

# Resume workflow
nx.resume_workflow("text_processor")
print("▶️  Workflow resumed")

# Now uploads will be processed again
nx.write("/workspace/uploads/test3.txt", b"This will be processed")
print("✅ File uploaded and processing")

# Delete a workflow
nx.delete_workflow("old_workflow_id")
print("🗑️  Old workflow deleted")

# Update workflow configuration
nx.update_workflow(
    "text_processor",
    trigger={"event": "file.created", "pattern": "**/*.txt"}  # Expanded pattern
)
print("🔄 Workflow updated")
```

---

## Complete Working Example

Here's a production-ready workflow automation system:

```python
#!/usr/bin/env python3
"""
Production Workflow Automation with Nexus
Demonstrates: file processing, error handling, monitoring
"""
import nexus
import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Any

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
NEXUS_URL = "http://localhost:8080"
NEXUS_API_KEY = "nxk_..."  # Replace with your key

class WorkflowAutomation:
    def __init__(self):
        self.nx = nexus.connect(config={
            "url": NEXUS_URL,
            "api_key": NEXUS_API_KEY
        })
        self.workflows = []

    async def process_document(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Process uploaded documents"""
        file_path = event['path']
        logger.info(f"Processing document: {file_path}")

        try:
            # Read content
            content = self.nx.read(file_path).decode('utf-8')

            # Extract metadata
            metadata = {
                "words": len(content.split()),
                "lines": len(content.split('\n')),
                "chars": len(content),
                "timestamp": datetime.now().isoformat()
            }

            # Save analysis
            analysis_path = file_path + ".analysis.json"
            self.nx.write(
                analysis_path,
                json.dumps(metadata, indent=2).encode()
            )

            logger.info(f"✅ Analysis saved: {analysis_path}")
            return {"status": "success", "metadata": metadata}

        except Exception as e:
            logger.error(f"❌ Error processing {file_path}: {e}")
            self._log_error(file_path, str(e))
            raise

    async def backup_important_files(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Backup files tagged as important"""
        file_path = event['path']
        logger.info(f"Backing up: {file_path}")

        # Create backup
        backup_path = file_path.replace('/workspace/', '/workspace/backups/')
        self.nx.copy(file_path, backup_path)

        logger.info(f"✅ Backup created: {backup_path}")
        return {"status": "success", "backup_path": backup_path}

    def _log_error(self, file_path: str, error: str):
        """Log errors to error file"""
        error_entry = {
            "timestamp": datetime.now().isoformat(),
            "file": file_path,
            "error": error
        }
        self.nx.append(
            "/workspace/system/errors.log",
            (json.dumps(error_entry) + '\n').encode()
        )

    def register_workflows(self):
        """Register all workflow handlers"""

        # Workflow 1: Document processor
        wf1 = self.nx.register_workflow(
            name="document_processor",
            handler=self.process_document,
            trigger={
                "event": "file.created",
                "pattern": "/workspace/documents/*.txt"
            },
            retry_policy={
                "max_attempts": 3,
                "backoff_seconds": 5
            }
        )
        self.workflows.append(wf1)
        logger.info(f"✅ Registered: document_processor ({wf1})")

        # Workflow 2: Backup system
        wf2 = self.nx.register_workflow(
            name="backup_important",
            handler=self.backup_important_files,
            trigger={
                "event": "file.created",
                "pattern": "/workspace/important/*.txt"
            }
        )
        self.workflows.append(wf2)
        logger.info(f"✅ Registered: backup_important ({wf2})")

    def start(self):
        """Start workflow automation system"""
        logger.info("🚀 Starting Workflow Automation System...")
        self.register_workflows()
        logger.info(f"👂 Listening for events... ({len(self.workflows)} workflows active)")

        # Keep running
        try:
            asyncio.get_event_loop().run_forever()
        except KeyboardInterrupt:
            logger.info("🛑 Shutting down gracefully...")
            self.stop()

    def stop(self):
        """Stop all workflows"""
        for wf_id in self.workflows:
            self.nx.delete_workflow(wf_id)
        logger.info("✅ All workflows stopped")

if __name__ == "__main__":
    automation = WorkflowAutomation()
    automation.start()
```

**Run the automation system:**

```bash
# Start the workflow system
python workflow_automation.py

# In another terminal, trigger workflows:
echo "Important document" | nexus write /workspace/documents/doc.txt --input -
echo "Critical data" | nexus write /workspace/important/data.txt --input -

# Check logs
nexus cat /workspace/system/errors.log
```

---

## Using CLI for Workflows

Manage workflows via CLI:

```bash
# List all workflows
nexus workflows list

# Get workflow details
nexus workflows get text_processor

# View execution history
nexus workflows history text_processor --limit 20

# Pause workflow
nexus workflows pause text_processor

# Resume workflow
nexus workflows resume text_processor

# Delete workflow
nexus workflows delete old_workflow
```

---

## Troubleshooting

### Issue: Workflow not triggering

**Problem:** File uploaded but workflow doesn't execute

**Solution:**
```python
# 1. Verify workflow is active
workflows = nx.list_workflows()
for wf in workflows:
    print(f"{wf['name']}: {wf['status']}")

# 2. Check pattern matches
# Pattern: "/workspace/uploads/*.txt"
# File must be: /workspace/uploads/file.txt (matches)
# Not: /workspace/upload/file.txt (no match)
# Not: /workspace/uploads/sub/file.txt (no match)

# 3. Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

### Issue: Workflow errors not visible

**Problem:** Workflow fails silently

**Solution:**
```python
# Add comprehensive logging
async def my_workflow(event):
    logger = logging.getLogger(__name__)
    try:
        logger.info(f"Starting: {event}")
        # ... workflow code ...
        logger.info("✅ Success")
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        raise

# Check error logs
errors = nx.get_workflow_errors("workflow_id", limit=50)
for err in errors:
    print(f"{err['timestamp']}: {err['error']}")
```

---

### Issue: High latency

**Problem:** Workflows execute slowly

**Solution:**
```python
# 1. Use batch operations
async def batch_workflow(event):
    files = nx.list_files("/workspace/batch/*.txt")

    # Process in batches
    for i in range(0, len(files), 10):
        batch = files[i:i+10]
        await asyncio.gather(*[process_file(f) for f in batch])

# 2. Optimize file operations
# Bad: Multiple round trips
content1 = nx.read("/file1.txt")
content2 = nx.read("/file2.txt")
content3 = nx.read("/file3.txt")

# Good: Batch read
contents = nx.read_many(["/file1.txt", "/file2.txt", "/file3.txt"])
```

---

## Key Concepts

### Event Types

Nexus workflows support multiple event types:

| Event | When it fires | Use case |
|-------|---------------|----------|
| `file.created` | New file written | Process uploads |
| `file.updated` | File content changed | Sync updates |
| `file.deleted` | File removed | Cleanup tasks |
| `file.moved` | File path changed | Re-index |
| `metadata.changed` | Metadata updated | Trigger based on tags |

### Pattern Matching

Workflow patterns use glob syntax:

```python
# Exact match
pattern = "/workspace/report.txt"

# All .txt files in directory
pattern = "/workspace/*.txt"

# All .txt files recursively
pattern = "/workspace/**/*.txt"

# Multiple extensions
pattern = "/workspace/*.{txt,md,json}"

# Prefix matching
pattern = "/workspace/data-*.csv"
```

### Execution Guarantees

Nexus provides:
- **At-least-once delivery**: Events may execute multiple times
- **Ordered execution**: Events for same file are ordered
- **Retry logic**: Configurable retry with backoff
- **Idempotency**: Design handlers to be idempotent

---

## Best Practices

### 1. Make Handlers Idempotent

```python
# ✅ Good: Idempotent (safe to run multiple times)
async def process_file(event):
    result_path = event['path'] + ".result"

    # Check if already processed
    if nx.exists(result_path):
        logger.info(f"Already processed: {event['path']}")
        return

    # Process and save result
    result = compute_result(event['path'])
    nx.write(result_path, result)

# ❌ Bad: Not idempotent
async def increment_counter(event):
    count = int(nx.read("/counter.txt"))
    nx.write("/counter.txt", str(count + 1).encode())
    # Running twice doubles the increment!
```

### 2. Use Structured Logging

```python
import structlog

logger = structlog.get_logger()

async def workflow_handler(event):
    logger.info("workflow_started",
        workflow="my_workflow",
        file=event['path'],
        event_id=event.get('id')
    )
    # ... process ...
    logger.info("workflow_completed",
        workflow="my_workflow",
        duration_ms=duration
    )
```

### 3. Handle Partial Failures

```python
async def process_batch(event):
    files = get_files_to_process()

    results = []
    errors = []

    for file in files:
        try:
            result = await process_one_file(file)
            results.append(result)
        except Exception as e:
            logger.error(f"Failed: {file}", exc_info=True)
            errors.append({"file": file, "error": str(e)})

    # Save summary
    summary = {
        "total": len(files),
        "succeeded": len(results),
        "failed": len(errors),
        "errors": errors
    }
    nx.write("/workspace/summary.json", json.dumps(summary).encode())
```

---

## What's Next?

Now that you've mastered workflow automation, explore more:

### 🔍 Recommended Next Steps

1. **[Multi-Tenant SaaS](multi-tenant-saas.md)** (30 min)
   Build isolated workflows per tenant

2. **[AI Agent Memory](ai-agent-memory.md)** (15 min)
   Trigger agents based on file events

3. **[Agent Framework Integration](agent-framework-integration.md)** (20 min)
   Integrate workflows with LangGraph/CrewAI

### 📚 Related Concepts

- [Workflows vs Triggers](../concepts/workflows-vs-triggers.md) - Deep dive
- [Event System](../concepts/events.md) - Event architecture
- [Async Operations](../concepts/async-operations.md) - Performance optimization

### 🔧 Advanced Topics

- [Distributed Workflows](../advanced/distributed-workflows.md) - Multi-server
- [Workflow Monitoring](../production/monitoring.md) - Observability
- [Performance Tuning](../how-to/optimize/workflow-performance.md) - Optimization

---

## Summary

🎉 **You've completed the Workflow Automation tutorial!**

**What you learned:**
- ✅ Create event-driven workflows with Nexus
- ✅ Build multi-step automation pipelines
- ✅ Handle errors and implement retries
- ✅ Monitor workflow execution
- ✅ Control workflows (pause/resume/delete)
- ✅ Use workflows with remote Nexus server

**Key Takeaways:**
- Workflows enable reactive, event-driven automation
- Design handlers to be idempotent and fault-tolerant
- Monitor and log all workflow executions
- Use patterns to filter which files trigger workflows

---

**Next:** [Multi-Tenant SaaS →](multi-tenant-saas.md)

**Questions?** Check our [API Reference](../api/workflows.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
