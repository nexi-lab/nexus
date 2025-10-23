# Nexus Workflow Automation System

The Nexus Workflow Automation System enables AI agents to define and execute automated pipelines for document processing, data transformation, and multi-step operations.

## Overview

Workflows are defined in YAML files and consist of:
- **Triggers**: Events that start workflow execution (file writes, schedules, webhooks)
- **Actions**: Operations to perform (parse, tag, move, llm, webhook, python, bash)
- **Variables**: Context data passed between actions

## Quick Start

### 1. Create a Workflow

Create a YAML file (e.g., `my-workflow.yaml`):

```yaml
name: process-documents
description: Auto-process new documents
version: 1.0

triggers:
  - type: file_write
    pattern: /inbox/**/*.pdf

actions:
  - name: parse_doc
    type: parse
    parser: pdf

  - name: tag_doc
    type: tag
    tags:
      - processed
      - pdf
```

### 2. Load the Workflow (CLI)

```bash
# Load workflow
nexus workflows load my-workflow.yaml

# List workflows
nexus workflows list

# Test workflow
nexus workflows test process-documents --file /inbox/test.pdf

# Enable/disable
nexus workflows enable process-documents
nexus workflows disable process-documents
```

### 3. Use the Python API

```python
from nexus.workflows import WorkflowAPI

# Create API instance
workflows = WorkflowAPI()

# Load workflow
workflows.load("my-workflow.yaml")

# Execute manually
result = await workflows.execute(
    "process-documents",
    file_path="/inbox/document.pdf"
)

print(f"Status: {result.status}")
print(f"Actions: {result.actions_completed}/{result.actions_total}")
```

## Trigger Types

### File Write
Triggers when a file is created or modified:
```yaml
triggers:
  - type: file_write
    pattern: /inbox/**/*.pdf
```

### File Delete
Triggers when a file is deleted:
```yaml
triggers:
  - type: file_delete
    pattern: /temp/**/*
```

### Metadata Change
Triggers when file metadata is updated:
```yaml
triggers:
  - type: metadata_change
    pattern: /workspace/**/*
    metadata_key: status  # Optional: specific key
```

### Schedule
Triggers on a schedule:
```yaml
triggers:
  - type: schedule
    cron: "0 2 * * *"  # Daily at 2 AM
```

### Webhook
Triggers via HTTP webhook:
```yaml
triggers:
  - type: webhook
    webhook_id: my-webhook-id
```

## Built-in Actions

### Parse
Parse a document:
```yaml
- name: parse_pdf
  type: parse
  parser: pdf
  file_path: "{file_path}"  # Optional, defaults to trigger file
```

### Tag
Add or remove tags:
```yaml
- name: add_tags
  type: tag
  tags:
    - processed
    - "year:{year}"
  remove: false  # Set to true to remove tags
```

### Move
Move or rename a file:
```yaml
- name: archive
  type: move
  source: "{file_path}"
  destination: /archives/{year}/{month}/
  create_parents: true
```

### Metadata
Update file metadata:
```yaml
- name: set_metadata
  type: metadata
  metadata:
    processed: "true"
    processed_at: "{timestamp}"
```

### LLM
Execute LLM-powered action:
```yaml
- name: analyze
  type: llm
  model: claude-sonnet-4
  prompt: |
    Analyze this document and extract key information.
  output_format: json  # or "text"
```

### Webhook
Send HTTP request:
```yaml
- name: notify
  type: webhook
  url: https://api.example.com/notify
  method: POST
  headers:
    Authorization: "Bearer {api_key}"
  body:
    file: "{filename}"
    status: "processed"
```

### Python
Execute Python code:
```yaml
- name: custom_logic
  type: python
  code: |
    # Access context variables
    file_path = context.file_path

    # Perform custom logic
    result = {"processed": True}
```

### Bash
Execute shell command:
```yaml
- name: compress
  type: bash
  command: gzip {file_path}
```

## Variable Interpolation

Actions can use variables from the context:

- `{file_path}` - Triggered file path
- `{filename}` - File name
- `{dirname}` - Directory name
- `{timestamp}` - Current timestamp
- `{action_name_output}` - Output from previous action

Example:
```yaml
- name: analyze
  type: llm
  prompt: "Analyze: {file_path}"

- name: use_result
  type: tag
  tags: "{analyze_output.tags}"
```

## Plugin Actions

Plugins can register custom actions:

```python
# In your plugin
class MyPlugin(NexusPlugin):
    def workflow_actions(self):
        return {
            "slack_notify": SlackNotifyAction(self),
            "custom_action": MyCustomAction(self)
        }
```

Then use in workflows:
```yaml
- name: notify_team
  type: slack_notify
  channel: "#alerts"
  message: "New file: {filename}"
```

## Example Workflows

### Invoice Processing
```yaml
name: process-invoices
version: 1.0

triggers:
  - type: file_write
    pattern: /inbox/invoices/**/*.pdf

actions:
  - name: parse_invoice
    type: parse
    parser: invoice

  - name: extract_metadata
    type: llm
    model: claude-sonnet-4
    prompt: |
      Extract: invoice number, amount, date, vendor
    output_format: json

  - name: tag_and_archive
    type: tag
    tags:
      - invoice
      - "vendor:{vendor_name}"

  - name: move_to_archive
    type: move
    destination: /archives/invoices/{year}/
    create_parents: true
```

### Auto-Tag Documents
```yaml
name: auto-tag-documents
version: 1.0

triggers:
  - type: file_write
    pattern: /workspace/**/*.{pdf,txt,md}

actions:
  - name: analyze_content
    type: llm
    model: claude-sonnet-4
    prompt: |
      Analyze this document and suggest 5-10 relevant tags.
      Return JSON array of lowercase tags.
    output_format: json

  - name: apply_tags
    type: tag
    tags: "{analyze_content_output}"
```

## CLI Reference

```bash
# Load workflow
nexus workflows load <file.yaml> [--enabled/--disabled]

# List workflows
nexus workflows list

# Test workflow
nexus workflows test <name> --file <path> [--context <json>]

# View execution history
nexus workflows runs <name> [--limit 10]

# Enable/disable
nexus workflows enable <name>
nexus workflows disable <name>

# Unload workflow
nexus workflows unload <name>

# Discover workflows in directory
nexus workflows discover [directory] [--load]
```

## Python API Reference

```python
from nexus.workflows import WorkflowAPI, TriggerType

workflows = WorkflowAPI()

# Load workflows
workflows.load("workflow.yaml")
workflows.load({"name": "test", "actions": [...]})

# List and get
workflows.list()
workflows.get("workflow-name")

# Execute
await workflows.execute("workflow-name", file_path="/path/to/file")

# Manage state
workflows.enable("workflow-name")
workflows.disable("workflow-name")
workflows.unload("workflow-name")

# Fire events
await workflows.fire_event(
    TriggerType.FILE_WRITE,
    {"file_path": "/inbox/file.pdf"}
)

# Discover
workflows.discover(".nexus/workflows", load=True)
```

## Best Practices

1. **Use descriptive names**: Make action names clear and meaningful
2. **Handle errors**: Use try-catch in Python actions for error handling
3. **Test workflows**: Use `nexus workflows test` before enabling
4. **Version workflows**: Increment version when making changes
5. **Document workflows**: Add clear descriptions
6. **Use variables**: Leverage context variables for flexibility
7. **Keep actions simple**: Break complex workflows into smaller steps
8. **Monitor executions**: Check workflow runs regularly

## Troubleshooting

### Workflow not triggering
- Check if workflow is enabled: `nexus workflows list`
- Verify trigger pattern matches file path
- Check workflow engine is initialized

### Action failing
- Test workflow with `nexus workflows test`
- Check action configuration
- Review error messages in execution results

### Variable not interpolating
- Ensure variable exists in context
- Check variable name spelling
- Use `{action_name_output}` for action results

## Advanced Topics

### Custom Actions (Plugin Development)
See plugin development documentation for creating custom actions.

### Schedule Management
Scheduled workflows require a scheduler service (future enhancement).

### Distributed Execution
For distributed workflows, see job system integration (v0.7.0+).

## Learn More

- [Nexus Documentation](https://docs.nexus.ai)
- [Plugin Development Guide](../plugins/README.md)
- [Example Workflows](./examples/)
