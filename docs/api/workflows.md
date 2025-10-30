# Workflow Automation

**Automate file processing pipelines with event-driven workflows**

Nexus workflows automatically trigger actions when files are created, modified, or deleted. No manual event firing needed—just write files and workflows execute in the background.

## Quick Start

```python
import nexus
from nexus.workflows import WorkflowAPI, WorkflowLoader

# Connect to Nexus
nx = nexus.connect()

# Load a workflow from YAML
workflows = WorkflowAPI()
workflows.load("process-invoices.yaml", enabled=True)

# Write a file - workflow fires automatically!
nx.write("/uploads/invoice-001.pdf", pdf_data)
# ✨ Workflow automatically processes the invoice
```

## How It Works

Workflows consist of **triggers** (when to run) and **actions** (what to do):

```yaml
name: process-invoices
version: "1.0"
description: Automatically process uploaded invoices

triggers:
  - type: file_write
    pattern: "/uploads/invoices/*.pdf"

actions:
  - name: send-webhook
    type: webhook
    config:
      url: "https://api.example.com/invoices"
      method: POST
      body:
        event: "invoice.uploaded"
        path: "${context.file_path}"
        size: "${context.size}"
```

When you upload an invoice:
```python
nx.write("/uploads/invoices/inv-001.pdf", data)
```

Nexus automatically:
1. Detects the file write event
2. Checks workflow patterns (`/uploads/invoices/*.pdf` matches)
3. Executes workflow actions (sends webhook)
4. All happens asynchronously (non-blocking)

## Workflow Definition

### Triggers

**File Events:**
- `file_write` - Fires when a file is created or updated
- `file_delete` - Fires when a file is deleted
- `file_rename` - Fires when a file is moved/renamed

**Pattern Matching:**
```yaml
triggers:
  - type: file_write
    pattern: "/uploads/*.pdf"          # Only PDFs in /uploads/
  - type: file_write
    pattern: "/uploads/**/*.pdf"       # PDFs in /uploads/ and subdirs
  - type: file_delete
    pattern: "/temp/**/*"              # Any file under /temp/
```

### Actions

**Built-in Actions:**
- `webhook` - Send HTTP request
- `shell` - Execute shell command
- `tag` - Add metadata tags
- `parse` - Parse file content
- `python` - Run Python code
- `llm` - Process with LLM

**Webhook Action:**
```yaml
actions:
  - name: notify-system
    type: webhook
    config:
      url: "https://api.example.com/events"
      method: POST
      headers:
        Authorization: "Bearer ${API_TOKEN}"
      body:
        event: "file.uploaded"
        path: "${context.file_path}"
        size: "${context.size}"
        uploaded_by: "${context.agent_id}"
```

**Shell Action:**
```yaml
actions:
  - name: compress-file
    type: shell
    config:
      command: 'gzip -c ${context.file_path} > ${context.file_path}.gz'
```

### Context Variables

All actions have access to event context:

**FILE_WRITE:**
- `${context.file_path}` - Path that was written
- `${context.size}` - File size in bytes
- `${context.etag}` - Content hash (SHA-256)
- `${context.version}` - File version number
- `${context.created}` - `true` if new file, `false` if update
- `${context.tenant_id}` - Tenant identifier
- `${context.agent_id}` - Agent identifier
- `${context.user_id}` - User identifier (if available)
- `${context.timestamp}` - ISO timestamp

**FILE_DELETE:**
- `${context.file_path}` - Path that was deleted
- `${context.size}` - File size before deletion
- `${context.etag}` - Content hash
- Other context fields same as above

**FILE_RENAME:**
- `${context.old_path}` - Original path
- `${context.new_path}` - New path
- Other context fields same as above

## Python API

### Loading Workflows

```python
from nexus.workflows import WorkflowAPI, WorkflowLoader

workflows = WorkflowAPI()

# From YAML file
workflows.load("my-workflow.yaml", enabled=True)

# From string
yaml_content = """
name: test-workflow
triggers:
  - type: file_write
    pattern: "/test/*.txt"
actions:
  - name: log
    type: shell
    config:
      command: 'echo "File written: ${context.file_path}"'
"""
definition = WorkflowLoader.load_from_string(yaml_content)
workflows.load(definition, enabled=True)

# From dict
workflow_dict = {
    "name": "test-workflow",
    "triggers": [{"type": "file_write", "pattern": "/test/*.txt"}],
    "actions": [{"name": "log", "type": "shell", "config": {...}}]
}
definition = WorkflowLoader.load_from_dict(workflow_dict)
workflows.load(definition, enabled=True)
```

### Managing Workflows

```python
# List all workflows
for workflow in workflows.list():
    print(f"{workflow['name']}: {workflow['enabled']}")

# Enable/disable
workflows.enable("my-workflow")
workflows.disable("my-workflow")

# Check status
if workflows.is_enabled("my-workflow"):
    print("Workflow is active")

# Unload
workflows.unload("my-workflow")

# Discover workflows in directory
workflows.discover(".nexus/workflows/", load=True)
```

### Manual Execution

```python
# Trigger workflow manually (without file event)
result = await workflows.execute(
    "my-workflow",
    context={"file_path": "/test/manual.txt"}
)
print(f"Status: {result.status}")
```

## CLI Commands

```bash
# Load a workflow
nexus workflows load invoice-processor.yaml

# List workflows
nexus workflows list

# Enable/disable
nexus workflows enable invoice-processor
nexus workflows disable invoice-processor

# View execution history
nexus workflows runs invoice-processor

# Test workflow manually
nexus workflows test invoice-processor --file /test.pdf

# Discover workflows in directory
nexus workflows discover .nexus/workflows/

# Unload workflow
nexus workflows unload invoice-processor
```

## Configuration

### Enable/Disable Workflows

Workflows are **enabled by default**. To disable:

```python
# Disable workflows entirely
nx = nexus.connect(config={"enable_workflows": False})

# Or via environment variable
# export NEXUS_ENABLE_WORKFLOWS=false
```

### Workflow Storage

Workflows are stored in the database (PostgreSQL or SQLite) with full execution history.

## Example Use Cases

### 1. Invoice Processing
```yaml
name: process-invoices
triggers:
  - type: file_write
    pattern: "/uploads/invoices/*.pdf"
actions:
  - name: extract-data
    type: parse
    config:
      parser: pdf
  - name: notify-accounting
    type: webhook
    config:
      url: "https://accounting.example.com/api/invoices"
      body:
        path: "${context.file_path}"
        data: "${previous.output}"
```

### 2. File Validation
```yaml
name: validate-uploads
triggers:
  - type: file_write
    pattern: "/uploads/**/*"
actions:
  - name: check-size
    type: python
    config:
      code: |
        if context['size'] > 10_000_000:
            raise ValueError("File too large")
  - name: scan-malware
    type: shell
    config:
      command: 'clamscan ${context.file_path}'
```

### 3. Automatic Cleanup
```yaml
name: cleanup-temp
triggers:
  - type: file_write
    pattern: "/temp/**/*"
actions:
  - name: schedule-deletion
    type: shell
    config:
      command: 'echo "${context.file_path}" >> /cleanup-queue.txt'
```

### 4. Real-Time Notifications
```yaml
name: notify-on-delete
triggers:
  - type: file_delete
    pattern: "/critical/**/*"
actions:
  - name: alert
    type: webhook
    config:
      url: "https://alerts.example.com/api/notify"
      body:
        severity: "high"
        message: "Critical file deleted: ${context.file_path}"
        deleted_by: "${context.agent_id}"
```

## Best Practices

### 1. Use Specific Patterns
```yaml
# ✅ Good - specific
pattern: "/uploads/invoices/*.pdf"

# ❌ Bad - too broad
pattern: "/**/*"
```

### 2. Keep Actions Small
```yaml
# ✅ Good - one action per task
actions:
  - name: parse
    type: parse
  - name: validate
    type: python
  - name: notify
    type: webhook

# ❌ Bad - too much in one action
actions:
  - name: do-everything
    type: python
    config:
      code: |
        # 100 lines of code...
```

### 3. Handle Errors Gracefully
```yaml
actions:
  - name: process
    type: webhook
    config:
      url: "https://api.example.com/process"
      max_retries: 3  # Retry on failure
      timeout: 30     # Don't hang forever
```

### 4. Use Environment Variables for Secrets
```yaml
actions:
  - name: secure-webhook
    type: webhook
    config:
      url: "https://api.example.com/secure"
      headers:
        Authorization: "Bearer ${API_TOKEN}"  # From environment
```

## Advanced Features

### Sequential Actions

Actions execute in order. Use `${previous.output}` to access previous results:

```yaml
actions:
  - name: step1
    type: parse
    config:
      parser: pdf
  - name: step2
    type: python
    config:
      code: |
        data = context['previous']['output']
        # Process data...
  - name: step3
    type: webhook
    config:
      body:
        data: "${previous.output}"
```

### Conditional Execution

Use Python actions for conditional logic:

```yaml
actions:
  - name: check-condition
    type: python
    config:
      code: |
        if context['size'] < 1000:
            raise ValueError("File too small, skip processing")
  - name: process
    type: webhook  # Only runs if check passes
```

## Troubleshooting

### Workflow Not Firing

Check:
1. Is workflow enabled? `workflows.is_enabled("name")`
2. Does pattern match? Test with: `fnmatch.fnmatch(path, pattern)`
3. Are workflows enabled globally? Check `nx.enable_workflows`

### Check Logs

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# Shows workflow execution logs
```

### Test Manually

```python
# Test trigger matching
from nexus.workflows.triggers import FileWriteTrigger
trigger = FileWriteTrigger(pattern="/uploads/*.pdf")
matches = trigger.matches({"file_path": "/uploads/test.pdf"})
print(f"Matches: {matches}")  # True/False
```

## Performance

- Events fire **asynchronously** (non-blocking)
- File operations complete immediately
- Workflows execute in background tasks
- No impact on write/delete/rename performance

## Next Steps

- **Week 2 (Planned):** Event logging for complete audit trail
- **Week 3 (Planned):** Enhanced webhooks with retries + HMAC security

## See Also

- [Workflow Demo](../../examples/py_demo/README_WORKFLOW_DEMO.md) - Complete working example
- [CLI Reference](../cli/workflows.md) - Command-line interface
- [API Reference](../api/README.md) - Full Python API
