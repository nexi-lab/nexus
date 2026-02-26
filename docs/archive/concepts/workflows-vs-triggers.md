# Workflows & Triggers

## What are Workflows?

**Workflows** are event-driven automation pipelines that execute sequences of actions automatically when files are created, updated, or deleted. Think of them as "if-this-then-that" rules for your filesystem.

### Traditional Approach vs Workflows

| Manual Approach | Workflow Approach |
|----------------|-------------------|
| ❌ Poll for new files | ✅ Auto-trigger on file write |
| ❌ Write custom code for each task | ✅ YAML configuration |
| ❌ No execution history | ✅ Database-backed audit trail |
| ❌ Hard to test | ✅ Test command built-in |
| ❌ Scattered logic | ✅ Centralized definitions |

**Key Innovation:** Define **what should happen**, not **how to do it**.

---

## Core Concepts

### Triggers (When)

**Triggers** define when a workflow should run:

```yaml
triggers:
  - type: file_write        # When file is written
    pattern: "/uploads/*.pdf"  # Matching this pattern
```

### Actions (What)

**Actions** define what should happen:

```yaml
actions:
  - name: tag_file
    type: tag
    tags: [processed, invoice]
```

### Complete Workflow

```yaml
name: invoice-processor
version: "1.0"

triggers:
  - type: file_write
    pattern: "/uploads/invoices/*.pdf"

actions:
  - name: tag
    type: tag
    tags: [invoice, processed]

  - name: archive
    type: move
    source: "{file_path}"
    destination: "/archive/{filename}"
```

---

## Trigger Types

### FILE_WRITE (Most Common)

Fires when a file is created or updated:

```yaml
triggers:
  - type: file_write
    pattern: "/uploads/**/*.pdf"  # Any PDF in uploads/ recursively
```

**Use cases:**
- Process uploaded documents
- Validate data files
- Auto-tag new files
- Trigger downstream pipelines

---

### FILE_DELETE

Fires when a file is deleted:

```yaml
triggers:
  - type: file_delete
    pattern: "/temp/**/*"
```

**Use cases:**
- Cleanup related resources
- Update indexes
- Log deletions
- Cascade cleanup

---

### FILE_RENAME

Fires when a file is moved or renamed:

```yaml
triggers:
  - type: file_rename
    pattern: "/inbox/*.tmp"
```

**Use cases:**
- Detect file completion (e.g., `.tmp` → `.csv`)
- Update references
- Re-index renamed files

---

### METADATA_CHANGE

Fires when file metadata is updated:

```yaml
triggers:
  - type: metadata_change
    pattern: "**/*"
    metadata_key: "status"  # Only when 'status' changes
```

**Use cases:**
- React to status changes (draft → published)
- Trigger reviews when metadata updated
- Audit metadata changes

---

### SCHEDULE

Fires on a schedule (cron or interval):

```yaml
triggers:
  - type: schedule
    cron: "0 * * * *"  # Every hour

  # OR

  - type: schedule
    interval_seconds: 3600  # Every hour
```

**Use cases:**
- Batch processing
- Periodic cleanup
- Regular reports
- Health checks

---

### WEBHOOK

Fires via HTTP webhook:

```yaml
triggers:
  - type: webhook
    webhook_id: "abc123"  # Unique webhook ID
```

**Use cases:**
- External system integration
- CI/CD pipelines
- Third-party notifications
- Custom triggers

---

### MANUAL

Fires via API or CLI:

```yaml
triggers:
  - type: manual
```

```bash
# Trigger manually
nexus workflows test workflow-name --file /path/to/file.pdf
```

**Use cases:**
- On-demand processing
- Testing
- Admin operations
- Conditional execution

---

## Pattern Matching

Workflows use **glob patterns** to match files:

```yaml
# Match specific extension
pattern: "*.pdf"

# Match in directory
pattern: "/uploads/*.pdf"

# Recursive wildcard
pattern: "/uploads/**/*.pdf"

# Match all files
pattern: "**/*"

# Multiple extensions (not directly supported, use regex in code)
pattern: "*.{pdf,docx,txt}"  # Note: Use separate triggers instead
```

**Examples:**

| Pattern | Matches | Doesn't Match |
|---------|---------|---------------|
| `*.pdf` | `report.pdf` | `report.txt` |
| `/uploads/*.pdf` | `/uploads/doc.pdf` | `/uploads/sub/doc.pdf` |
| `/uploads/**/*.pdf` | `/uploads/sub/doc.pdf` | `/other/doc.pdf` |
| `**/*` | All files | (none) |

---

## Built-in Actions

### 1. parse - Parse Document Content

Extract text from documents:

```yaml
- name: parse_doc
  type: parse
  file_path: "{file_path}"
  parser: "auto"  # auto, pdf, docx, html, txt
```

**Outputs:**
```python
{
    "text": "Extracted document text...",
    "pages": 5,
    "metadata": {"author": "Alice", "title": "Report"}
}
```

---

### 2. tag - Add/Remove Tags

Tag files for organization:

```yaml
- name: tag_file
  type: tag
  file_path: "{file_path}"
  tags:
    - invoice
    - processed
  remove: false  # Set to true to remove tags
```

---

### 3. move - Move/Rename Files

Relocate files:

```yaml
- name: archive
  type: move
  source: "{file_path}"
  destination: "/archive/{filename}"
  create_parents: true  # Create destination dirs if needed
```

---

### 4. metadata - Update File Metadata

Set metadata fields:

```yaml
- name: set_meta
  type: metadata
  file_path: "{file_path}"
  metadata:
    status: "processed"
    processed_at: "{timestamp}"
    amount: "{extract_amount_output.amount}"
```

---

### 5. llm - LLM-Powered Actions

Use AI to process content:

```yaml
- name: extract_info
  type: llm
  file_path: "{file_path}"
  prompt: |
    Extract from this invoice:
    - Invoice number
    - Date
    - Total amount
  model: "claude-sonnet-4"
  output_format: "json"
```

**Outputs:**
```python
{
    "invoice_number": "INV-2025-001",
    "date": "2025-01-15",
    "total_amount": 1500.00
}
```

---

### 6. webhook - Send HTTP Requests

Notify external systems:

```yaml
- name: notify
  type: webhook
  url: "https://api.example.com/notify"
  method: "POST"
  headers:
    Authorization: "Bearer token123"
  body:
    event: "file.processed"
    file: "{filename}"
    amount: "{extract_info_output.total_amount}"
```

---

### 7. python - Execute Python Code

Run custom Python logic:

```yaml
- name: custom_logic
  type: python
  code: |
    import json

    # Access workflow context
    file = file_path
    vars = variables

    # Custom processing
    data = {"processed": True}
    result = {"success": True, "data": data}
```

**Available variables in code:**
- `file_path` - Current file path
- `variables` - All workflow variables
- `trigger_context` - Raw event data
- `result` - Set this to return output

---

### 8. bash - Execute Shell Commands

Run shell commands:

```yaml
- name: validate_pdf
  type: bash
  command: "file {file_path} | grep -q 'PDF'"
```

```yaml
- name: compress
  type: bash
  command: "gzip {file_path}"
```

---

## Variable Interpolation

Actions can use variables with `{variable}` syntax:

```yaml
actions:
  - name: extract
    type: llm
    prompt: "Extract data from {filename}"

  - name: move
    type: move
    destination: "/archive/{extract_output.date}/{filename}"
```

**Available variables:**

| Variable | Description | Example |
|----------|-------------|---------|
| `{file_path}` | Full file path | `/uploads/invoice.pdf` |
| `{filename}` | Just filename | `invoice.pdf` |
| `{dirname}` | Directory path | `/uploads` |
| `{timestamp}` | Current timestamp | `2025-01-15T10:30:00Z` |
| `{action_name_output}` | Output from previous action | `{extract_output.amount}` |
| `{custom_var}` | Custom variables from workflow | `{archive_dir}` |

---

## Complete Example: Invoice Processing

```yaml
name: invoice-processing
version: "1.0"
description: "Automated invoice processing pipeline"

# Custom variables
variables:
  archive_folder: "/archive/invoices"
  api_url: "https://api.example.com"

# Trigger on PDF uploads
triggers:
  - type: file_write
    pattern: "/inbox/invoices/*.pdf"

# Multi-step processing
actions:
  # Step 1: Validate PDF
  - name: validate
    type: bash
    command: "file {file_path} | grep -q 'PDF'"

  # Step 2: Parse content
  - name: parse
    type: parse
    file_path: "{file_path}"
    parser: "pdf"

  # Step 3: Extract with AI
  - name: extract
    type: llm
    file_path: "{file_path}"
    prompt: |
      Extract from this invoice:
      - Invoice number
      - Date
      - Total amount
      - Vendor name
      Return as JSON.
    model: "claude-sonnet-4"
    output_format: "json"

  # Step 4: Update metadata
  - name: update_meta
    type: metadata
    file_path: "{file_path}"
    metadata:
      invoice_number: "{extract_output.invoice_number}"
      total_amount: "{extract_output.total_amount}"
      vendor: "{extract_output.vendor_name}"
      status: "processed"

  # Step 5: Archive file
  - name: archive
    type: move
    source: "{file_path}"
    destination: "{archive_folder}/{extract_output.invoice_number}.pdf"
    create_parents: true

  # Step 6: Notify API
  - name: notify
    type: webhook
    url: "{api_url}/invoices/processed"
    method: "POST"
    body:
      invoice_number: "{extract_output.invoice_number}"
      amount: "{extract_output.total_amount}"
      status: "archived"
```

---

## CLI Commands

### Load Workflow

```bash
# Load from file
nexus workflows load invoice-processor.yaml

# Load and enable
nexus workflows load invoice-processor.yaml --enabled

# Load but keep disabled
nexus workflows load invoice-processor.yaml --disabled
```

---

### List Workflows

```bash
# List all workflows
nexus workflows list

# Output:
# invoice-processor (enabled)
# document-tagger (disabled)
# auto-archiver (enabled)
```

---

### Test Workflow

```bash
# Test with specific file
nexus workflows test invoice-processor --file /inbox/invoices/test.pdf

# Test with custom context
nexus workflows test invoice-processor --context '{"file_path": "/test.pdf"}'
```

---

### View Execution History

```bash
# View recent executions
nexus workflows runs invoice-processor

# Limit results
nexus workflows runs invoice-processor --limit 5

# Output:
# execution_id: abc123
# status: SUCCEEDED
# started_at: 2025-01-15T10:30:00Z
# actions_completed: 6/6
```

---

### Enable/Disable

```bash
# Enable workflow
nexus workflows enable invoice-processor

# Disable workflow
nexus workflows disable invoice-processor
```

---

### Unload Workflow

```bash
# Remove workflow from system
nexus workflows unload invoice-processor
```

---

### Discover Workflows

```bash
# Find workflows in directory
nexus workflows discover /path/to/workflows/

# Find and load automatically
nexus workflows discover /path/to/workflows/ --load
```

---

## Python SDK

### Basic Usage

```python
import asyncio
from nexus.workflows import WorkflowAPI

async def main():
    workflows = WorkflowAPI()

    # Load workflow
    workflows.load("invoice-processor.yaml", enabled=True)

    # List workflows
    for wf in workflows.list():
        print(f"{wf['name']}: {wf['status']}")

    # Execute manually
    result = await workflows.execute(
        "invoice-processor",
        file_path="/inbox/invoices/test.pdf"
    )

    print(f"Status: {result.status}")
    print(f"Actions completed: {result.actions_completed}/{result.actions_total}")

asyncio.run(main())
```

---

### Load from Dict

```python
workflow_def = {
    "name": "my-workflow",
    "version": "1.0",
    "triggers": [
        {"type": "file_write", "pattern": "*.pdf"}
    ],
    "actions": [
        {
            "name": "tag",
            "type": "tag",
            "tags": ["processed"]
        }
    ]
}

workflows.load(workflow_def, enabled=True)
```

---

### Fire Events Manually

```python
from nexus.workflows import TriggerType

# Fire event manually
triggered = await workflows.fire_event(
    TriggerType.FILE_WRITE,
    {
        "file_path": "/uploads/doc.pdf",
        "size": 1024,
        "timestamp": datetime.now()
    }
)

print(f"Triggered {triggered} workflows")
```

---

### Manage Workflows

```python
# Enable/disable
workflows.enable("my-workflow")
workflows.disable("my-workflow")

# Check status
if workflows.is_enabled("my-workflow"):
    print("Workflow is active")

# Get definition
definition = workflows.get("my-workflow")
print(definition.description)

# Unload
workflows.unload("my-workflow")
```

---

## Integration with Nexus Filesystem

Workflows **automatically fire** when you perform file operations:

```python
from nexus import NexusFS

# Workflows are enabled by default
nx = NexusFS(enable_workflows=True)

# Load workflow
nx.workflows.load({
    "name": "auto-tagger",
    "triggers": [{"type": "file_write", "pattern": "*.pdf"}],
    "actions": [{"name": "tag", "type": "tag", "tags": ["pdf"]}]
})

# Write file → workflow fires automatically!
nx.write("/uploads/report.pdf", pdf_data)
# → auto-tagger workflow executes in background
# → File gets tagged with ["pdf"]
```

---

## Execution Model

### Asynchronous Execution

Workflows run **asynchronously** in the background:

```python
# File operation returns immediately (non-blocking)
nx.write("/uploads/doc.pdf", data)
print("File written, workflow running in background")

# Workflow executes separately:
# 1. Trigger matches
# 2. Actions execute sequentially
# 3. Results stored in database
```

---

### Sequential Actions

Actions within a workflow execute **one at a time**:

```
Action 1: parse_doc
  ↓ (output: {"text": "..."})
Action 2: extract_data (uses parse_doc_output)
  ↓ (output: {"amount": 1500})
Action 3: update_meta (uses extract_data_output)
  ↓ (output: {"success": true})
Action 4: archive (moves file)
```

**Key points:**
- If action fails → workflow stops (FAILED status)
- Each action can access outputs from previous actions
- Variables interpolated before action execution

---

### Concurrent Workflows

Multiple workflows can execute simultaneously:

```yaml
# Workflow 1: Tag all PDFs
triggers:
  - type: file_write
    pattern: "*.pdf"

# Workflow 2: Archive PDFs in /uploads
triggers:
  - type: file_write
    pattern: "/uploads/*.pdf"
```

```python
# Both fire on same file
nx.write("/uploads/report.pdf", data)
# → Workflow 1 fires (tags file)
# → Workflow 2 fires (archives file)
# → Both run concurrently
```

---

## Real-World Use Cases

### Use Case 1: Document Processing Pipeline

```yaml
name: document-processor
triggers:
  - type: file_write
    pattern: "/uploads/**/*.pdf"

actions:
  - name: parse
    type: parse
    parser: "pdf"

  - name: extract_metadata
    type: llm
    prompt: "Extract title, date, and summary"

  - name: store_metadata
    type: metadata
    metadata:
      title: "{extract_metadata_output.title}"
      summary: "{extract_metadata_output.summary}"

  - name: tag
    type: tag
    tags: [processed, searchable]
```

---

### Use Case 2: Auto-Archive by Date

```yaml
name: auto-archiver
triggers:
  - type: file_write
    pattern: "/inbox/**/*"

actions:
  - name: extract_date
    type: llm
    prompt: "Extract date from filename or content"

  - name: archive
    type: move
    destination: "/archive/{extract_date_output.year}/{extract_date_output.month}/{filename}"
    create_parents: true
```

---

### Use Case 3: Real-Time Notifications

```yaml
name: webhook-notifier
triggers:
  - type: file_write
    pattern: "/critical/**/*"

actions:
  - name: notify_slack
    type: webhook
    url: "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
    body:
      text: "Critical file uploaded: {filename}"
      channel: "#alerts"
```

---

### Use Case 4: Data Validation

```yaml
name: csv-validator
triggers:
  - type: file_write
    pattern: "/data/**/*.csv"

actions:
  - name: validate
    type: python
    code: |
      import csv

      # Read and validate CSV
      with open(file_path) as f:
          reader = csv.DictReader(f)
          rows = list(reader)

      # Check required columns
      required = ['name', 'email', 'age']
      if not all(col in reader.fieldnames for col in required):
          raise ValueError("Missing required columns")

      result = {"valid": True, "rows": len(rows)}

  - name: tag_valid
    type: tag
    tags: [validated]

  - name: update_meta
    type: metadata
    metadata:
      validated: true
      row_count: "{validate_output.rows}"
```

---

### Use Case 5: Scheduled Cleanup

```yaml
name: temp-cleaner
triggers:
  - type: schedule
    cron: "0 0 * * *"  # Daily at midnight

actions:
  - name: cleanup
    type: python
    code: |
      import os
      from pathlib import Path
      from datetime import datetime, timedelta

      # Delete files older than 7 days
      temp_dir = Path("/temp")
      cutoff = datetime.now() - timedelta(days=7)

      deleted = 0
      for file in temp_dir.rglob("*"):
          if file.is_file():
              mtime = datetime.fromtimestamp(file.stat().st_mtime)
              if mtime < cutoff:
                  file.unlink()
                  deleted += 1

      result = {"deleted": deleted}
```

---

## Best Practices

### 1. Use Specific Patterns

```yaml
# ✅ Good: Specific pattern
pattern: "/uploads/invoices/*.pdf"

# ❌ Bad: Too broad
pattern: "**/*"  # Fires on EVERY file
```

---

### 2. Name Actions Descriptively

```yaml
# ✅ Good: Clear names
actions:
  - name: parse_invoice
  - name: extract_amount
  - name: archive_processed

# ❌ Bad: Vague names
actions:
  - name: step1
  - name: step2
  - name: step3
```

---

### 3. Handle Errors Gracefully

```yaml
# ✅ Good: Validate before processing
actions:
  - name: validate
    type: bash
    command: "file {file_path} | grep -q 'PDF'"

  - name: parse
    type: parse
    parser: "pdf"

# ❌ Bad: Assume valid input
actions:
  - name: parse
    type: parse  # Fails if not PDF
```

---

### 4. Use Variables for Configuration

```yaml
# ✅ Good: Variables for reusable values
variables:
  archive_dir: "/archive"
  api_url: "https://api.example.com"

actions:
  - name: archive
    destination: "{archive_dir}/{filename}"

# ❌ Bad: Hardcoded values
actions:
  - name: archive
    destination: "/archive/{filename}"  # Hard to change
```

---

### 5. Test Before Deploying

```bash
# ✅ Good: Test with sample file
nexus workflows test my-workflow --file /test/sample.pdf

# ❌ Bad: Deploy without testing
nexus workflows load my-workflow.yaml --enabled  # Hope it works!
```

---

## Performance Considerations

### Workflow Loading

- **Startup time**: < 100ms per workflow
- **Memory**: ~1KB per workflow definition
- **Database**: Workflow definitions stored in SQLite/PostgreSQL

### Execution Performance

- **Trigger matching**: O(n) where n = number of workflows
- **Action execution**: Sequential (one at a time)
- **Average latency**: 10-100ms for simple actions, seconds for LLM actions

### Optimization Tips

1. **Use specific patterns**: Reduces unnecessary trigger checks
2. **Limit LLM actions**: Most expensive operations
3. **Batch operations**: Use scheduled workflows for bulk processing
4. **Monitor execution history**: Check for failed workflows

---

## Troubleshooting

### Workflow Not Firing

**Check:**
1. Is workflow enabled?
   ```bash
   nexus workflows list | grep my-workflow
   ```

2. Does pattern match?
   ```python
   import fnmatch
   fnmatch.fnmatch("/uploads/doc.pdf", "*.pdf")  # True
   ```

3. Are workflows enabled in filesystem?
   ```python
   nx = NexusFS(enable_workflows=True)  # Must be True
   ```

---

### Action Failing

**Check:**
1. View execution history:
   ```bash
   nexus workflows runs my-workflow
   ```

2. Check error message in execution record

3. Test action in isolation:
   ```yaml
   # Create minimal test workflow
   actions:
     - name: test
       type: python
       code: "print('test')"
   ```

---

### Variables Not Interpolating

**Check:**
1. Variable syntax: `{variable}` not `{{variable}}`
2. Variable exists in context
3. Previous action output available

```yaml
# Debug variables
actions:
  - name: debug
    type: python
    code: |
      print(f"Available variables: {variables}")
      print(f"File path: {file_path}")
```

---

## FAQ

### Q: Can workflows modify the triggering file?

**A**: Yes! Use `{file_path}` to reference the file that triggered the workflow.

### Q: Can I chain workflows (workflow triggers workflow)?

**A**: Yes! If an action modifies a file that matches another workflow's pattern, the second workflow will fire.

### Q: How do I prevent infinite loops?

**A**: Use specific patterns and avoid circular dependencies. Example: don't have a workflow that writes to the same pattern it's triggered by.

### Q: Can I use workflows without Nexus filesystem?

**A**: Yes! Use `workflows.fire_event()` to trigger workflows manually.

### Q: What happens if action fails?

**A**: Workflow stops immediately with FAILED status. Subsequent actions don't execute.

---

## Next Steps

- **[Memory System](memory-system.md)** - Store workflow results in memory
- **[Learning Loops](learning-loops.md)** - Use workflows with agent learning
- **[Agent Permissions](agent-permissions.md)** - Control workflow access
- **[API Reference: Workflow API](/api/workflow-api/)** - Complete API docs

---

## Related Files

- Types: `src/nexus/workflows/types.py:1`
- Triggers: `src/nexus/workflows/triggers.py:1`
- Engine: `src/nexus/workflows/engine.py:1`
- Actions: `src/nexus/workflows/actions.py:1`
- API: `src/nexus/workflows/api.py:1`
- CLI: `src/nexus/cli/commands/workflows.py:1`
- Models: `src/nexus/storage/models.py:1`
- Example: `examples/py_demo/workflow_auto_fire_demo.py:1`
