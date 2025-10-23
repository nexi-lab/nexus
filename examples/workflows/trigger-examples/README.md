# Workflow Trigger Examples

This directory contains example workflows demonstrating all 7 trigger types supported by the Nexus Workflow Automation System.

## Trigger Types Overview

### 1. FILE_WRITE Trigger (`01-file-write-trigger.yaml`)
**Fires when**: A file is created or modified

**Use cases**:
- Auto-process uploaded documents
- Parse new files
- Tag incoming files
- Move files to appropriate folders

**Example**:
```yaml
triggers:
  - type: file_write
    pattern: /uploads/**/*.{pdf,docx,txt}
```

**Real Nexus integration**:
```bash
# This will trigger the workflow
nexus write /uploads/document.pdf "content"
```

---

### 2. FILE_DELETE Trigger (`02-file-delete-trigger.yaml`)
**Fires when**: A file is deleted

**Use cases**:
- Audit file deletions
- Send deletion notifications
- Clean up related resources
- Log deletion events

**Example**:
```yaml
triggers:
  - type: file_delete
    pattern: /workspace/**/*
```

**Real Nexus integration**:
```bash
# This will trigger the workflow
nexus rm /workspace/old-file.txt
```

---

### 3. FILE_RENAME Trigger (`03-file-rename-trigger.yaml`)
**Fires when**: A file is moved or renamed

**Use cases**:
- Track file history
- Update references
- Maintain rename audit log
- Sync with external systems

**Example**:
```yaml
triggers:
  - type: file_rename
    pattern: /documents/**/*
```

**Real Nexus integration**:
```bash
# This will trigger the workflow
nexus mv /documents/old-name.txt /documents/new-name.txt
```

---

### 4. METADATA_CHANGE Trigger (`04-metadata-change-trigger.yaml`)
**Fires when**: File metadata is updated

**Use cases**:
- React to status changes
- Auto-organize based on tags
- Trigger approvals
- Sync metadata to external systems

**Example**:
```yaml
triggers:
  - type: metadata_change
    pattern: /projects/**/*
    metadata_key: status  # Optional: only fire for specific key
```

**Real Nexus integration**:
```bash
# This will trigger the workflow
nexus metadata set /projects/task.txt status completed
```

---

### 5. SCHEDULE Trigger (`05-schedule-trigger.yaml`)
**Fires when**: Scheduled time matches (cron expression)

**Use cases**:
- Daily backups
- Weekly reports
- Periodic cleanup
- Scheduled data processing

**Example**:
```yaml
triggers:
  - type: schedule
    cron: "0 2 * * *"  # Daily at 2 AM
```

**⚠️ Note**: Requires scheduler service (planned for v0.7.0+)

Currently, schedule triggers are **defined but not activated**. The trigger will be stored and ready, but won't fire automatically until the Job System (#138) is implemented in v0.7.0.

---

### 6. WEBHOOK Trigger (`06-webhook-trigger.yaml`)
**Fires when**: External HTTP webhook is received

**Use cases**:
- GitHub/GitLab webhooks
- CI/CD integration
- External system events
- Third-party notifications

**Example**:
```yaml
triggers:
  - type: webhook
    webhook_id: github-webhook-12345
```

**Real Nexus integration**:
```python
# Via Python SDK
from nexus.workflows import get_workflow_api, TriggerType

workflows = get_workflow_api()
await workflows.fire_event(
    TriggerType.WEBHOOK,
    {
        "webhook_id": "github-webhook-12345",
        "payload": {"event": "push", "repo": "myrepo"}
    }
)
```

---

### 7. MANUAL Trigger (`07-manual-trigger.yaml`)
**Fires when**: Explicitly executed via CLI or API

**Use cases**:
- On-demand reports
- Manual processing
- Administrative tasks
- User-initiated workflows

**Example**:
```yaml
triggers:
  - type: manual
```

**Real Nexus integration**:
```bash
# Via CLI
nexus workflows test generate-report --file /data/input.csv

# Via Python SDK
from nexus.workflows import get_workflow_api

workflows = get_workflow_api()
execution = await workflows.execute("generate-report", file_path="/data/input.csv")
print(f"Status: {execution.status}")
```

---

## Testing These Examples

### Load all trigger examples:
```bash
nexus workflows discover examples/workflows/trigger-examples --load
```

### List loaded workflows:
```bash
nexus workflows list
```

### Test a specific workflow:
```bash
# Test file-write trigger
nexus workflows test auto-process-uploads --file /uploads/test.pdf

# Test manual trigger
nexus workflows test generate-report
```

### Trigger workflows with real Nexus operations:
```bash
# This will trigger any FILE_WRITE workflows
nexus write /uploads/document.pdf "content"

# This will trigger any FILE_DELETE workflows
nexus rm /workspace/old-file.txt

# This will trigger any FILE_RENAME workflows
nexus mv /documents/a.txt /documents/b.txt

# This will trigger any METADATA_CHANGE workflows
nexus metadata set /projects/task.txt status completed
```

---

## Integration with Nexus Operations

The workflow system integrates with Nexus through **event firing**. Here's how it works:

### Current Implementation (v0.4.0):
Workflows are triggered manually via the API:

```python
from nexus import connect
from nexus.workflows import get_workflow_api, TriggerType

# Perform Nexus operation
nx = connect()
nx.write("/inbox/file.txt", b"content")

# Fire workflow event
workflows = get_workflow_api()
await workflows.fire_event(
    TriggerType.FILE_WRITE,
    {"file_path": "/inbox/file.txt"}
)
```

### Future Enhancement (v0.5.0+):
Automatic event firing integrated into NexusFS:

```python
# In NexusFS.write() method
def write(self, path, content):
    # ... write logic ...

    # Auto-fire workflow events
    if self.workflow_engine:
        await self.workflow_engine.fire_event(
            TriggerType.FILE_WRITE,
            {"file_path": path}
        )
```

---

## Pattern Matching

All file-based triggers support **glob patterns**:

```yaml
# Match specific extension
pattern: /inbox/**/*.pdf

# Match multiple extensions
pattern: /inbox/**/*.{pdf,docx,txt}

# Match all files
pattern: /workspace/**/*

# Match specific directory
pattern: /projects/active/*

# Match by name
pattern: /reports/**/monthly-*.csv
```

---

## Best Practices

1. **Use specific patterns**: Avoid overly broad patterns like `/**/*` that match everything
2. **Test before enabling**: Use `nexus workflows test` to verify behavior
3. **Monitor executions**: Check `nexus workflows runs <name>` for history
4. **Handle errors gracefully**: Use try-catch in Python actions
5. **Keep actions atomic**: Each action should do one thing well
6. **Use descriptive names**: Name workflows and actions clearly

---

## See Also

- [Workflow System Documentation](../README.md)
- [Python API Examples](../workflow_example.py)
- [Comprehensive Demo](../comprehensive_demo.py)
- [Built-in Actions Reference](../README.md#built-in-actions)
