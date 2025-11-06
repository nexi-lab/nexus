# CLI: Sandbox Management

← [CLI Reference](index.md) | [API Documentation](../README.md)

This document describes CLI commands for sandbox management and their Python API equivalents.

Sandboxes provide secure, isolated code execution environments for AI agents. Run Python, JavaScript, and Bash code safely in ephemeral cloud sandboxes.

---

## Table of Contents

### Sandbox Operations
- [sandbox create](#sandbox-create---create-sandbox) - Create new sandbox
- [sandbox run](#sandbox-run---execute-code) - Execute code
- [sandbox list](#sandbox-list---list-sandboxes) - List all sandboxes
- [sandbox status](#sandbox-status---get-status) - Get sandbox status
- [sandbox pause](#sandbox-pause---pause-sandbox) - Pause sandbox
- [sandbox resume](#sandbox-resume---resume-sandbox) - Resume sandbox
- [sandbox stop](#sandbox-stop---stop-sandbox) - Stop and destroy sandbox

---

## Sandbox Operations

### sandbox create - Create sandbox

Create a new code execution sandbox.

**CLI:**
```bash
# Create with default TTL (10 minutes)
nexus sandbox create my-sandbox

# Create with custom TTL
nexus sandbox create data-analysis --ttl 30

# Create with E2B template
nexus sandbox create ml-training --template custom-gpu-template

# JSON output for scripting
nexus sandbox create test-sandbox --json
```

**Python API:**
```python
# Create sandbox
sandbox = nx.sandbox_create(
    name="my-sandbox",
    ttl_minutes=10
)

# Create with custom TTL
sandbox = nx.sandbox_create(
    name="data-analysis",
    ttl_minutes=30
)

# Create with template
sandbox = nx.sandbox_create(
    name="ml-training",
    ttl_minutes=60,
    template_id="custom-gpu-template"
)

# Access sandbox_id
sandbox_id = sandbox["sandbox_id"]
print(f"Created: {sandbox_id}")
```

**Options:**
- `--ttl, -t INTEGER`: Idle timeout in minutes (default: 10)
- `--template TEXT`: Provider template ID (e.g., E2B template)
- `--json, -j`: Output as JSON

**Returns:**
```json
{
  "sandbox_id": "ipi7dxuc5687axlhm5hmd",
  "name": "my-sandbox",
  "user_id": "user123",
  "agent_id": null,
  "tenant_id": "default",
  "provider": "e2b",
  "template_id": null,
  "status": "active",
  "created_at": "2025-11-03T01:03:40",
  "last_active_at": "2025-11-03T01:03:40",
  "paused_at": null,
  "stopped_at": null,
  "ttl_minutes": 10,
  "expires_at": "2025-11-03T01:13:40",
  "uptime_seconds": 0.005
}
```

---

### sandbox run - Execute code

Run code in a sandbox. Supports Python, JavaScript, and Bash.

**CLI:**
```bash
# Run Python code (inline)
nexus sandbox run sb_123 -c "print('Hello World')"

# Run Python from file
nexus sandbox run sb_123 -f script.py

# Run from stdin
echo "print('test')" | nexus sandbox run sb_123 -c -

# Run JavaScript
nexus sandbox run sb_123 -l javascript -c "console.log('Hello')"

# Run Bash
nexus sandbox run sb_123 -l bash -c "ls -la && df -h"

# Custom timeout (60 seconds)
nexus sandbox run sb_123 -c "import time; time.sleep(5)" --timeout 60

# JSON output
nexus sandbox run sb_123 -c "print(42)" --json
```

**Python API:**
```python
# Run Python code
result = nx.sandbox_run(
    sandbox_id="sb_123",
    language="python",
    code="print('Hello World')"
)

# Run JavaScript
result = nx.sandbox_run(
    sandbox_id="sb_123",
    language="javascript",
    code="console.log('Hello')"
)

# Run Bash
result = nx.sandbox_run(
    sandbox_id="sb_123",
    language="bash",
    code="ls -la && df -h"
)

# Custom timeout
result = nx.sandbox_run(
    sandbox_id="sb_123",
    language="python",
    code="import time; time.sleep(5)",
    timeout=60
)

# Check results
print("STDOUT:", result["stdout"])
print("STDERR:", result["stderr"])
print("Exit Code:", result["exit_code"])
print("Time:", result["execution_time"], "seconds")
```

**Options:**
- `--language, -l CHOICE`: Programming language (python|javascript|bash) - default: python
- `--code, -c TEXT`: Code to execute (use `-` for stdin)
- `--file, -f PATH`: File containing code to execute
- `--timeout INTEGER`: Execution timeout in seconds (default: 30)
- `--json, -j`: Output as JSON

**Returns:**
```json
{
  "stdout": "Hello World\n",
  "stderr": "",
  "exit_code": 0,
  "execution_time": 0.56
}
```

**Supported Languages:**
- `python`: Python 3.12 with common libraries
- `javascript`: Node.js with async/await support
- `bash`: Bash shell with standard Unix tools

---

### sandbox list - List sandboxes

List sandboxes with optional filtering by user, agent, or tenant.

By default, lists sandboxes for the current user. Use filter options to narrow results or (for admins) to view sandboxes for other users.

**Status Verification:**
- **Without `--verify`**: Returns cached status from database (fast, may be stale)
- **With `--verify`**: Checks actual status with Docker/E2B provider (slower but accurate)

Use `--verify` when you need to ensure status is current, especially if sandboxes may have been killed externally.

**CLI:**
```bash
# List sandboxes for current user (table format)
nexus sandbox list

# List sandboxes for specific user (admin only)
nexus sandbox list --user-id alice

# List sandboxes for specific agent
nexus sandbox list --agent-id agent_123

# List sandboxes for specific tenant
nexus sandbox list --tenant-id tenant_456

# Combine filters
nexus sandbox list --agent-id agent_123 --tenant-id tenant_456

# Verify status with provider (slower but accurate)
nexus sandbox list --verify

# Combine filtering and verification
nexus sandbox list --user-id alice --verify

# List as JSON
nexus sandbox list --json
```

**Python API:**
```python
# List sandboxes for current user
result = nx.sandbox_list()

# List sandboxes with filtering
result = nx.sandbox_list(context={
    "user": "alice",           # Filter by user
    "agent_id": "agent_123",   # Filter by agent
    "tenant_id": "tenant_456"  # Filter by tenant
})

# List with status verification
result = nx.sandbox_list(verify_status=True)

# Combine filtering and verification
result = nx.sandbox_list(
    context={"user": "alice"},
    verify_status=True
)

for sandbox in result["sandboxes"]:
    status = sandbox['status']
    verified = sandbox.get('verified', False)
    print(f"{sandbox['name']}: {status} {'(verified)' if verified else ''}")
```

**Options:**
- `--user-id, -u TEXT`: Filter by user ID
- `--agent-id, -a TEXT`: Filter by agent ID
- `--tenant-id, -t TEXT`: Filter by tenant ID
- `--verify, -v`: Verify status with provider (slower but accurate)
- `--json, -j`: Output as JSON

**Returns:**
```json
{
  "sandboxes": [
    {
      "sandbox_id": "ipi7dxuc5687axlhm5hmd",
      "name": "demo-sandbox-1",
      "status": "active",
      "created_at": "2025-11-03T01:03:40",
      "expires_at": "2025-11-03T01:13:40",
      "ttl_minutes": 10,
      "uptime_seconds": 123.4
    }
  ]
}
```

**With `--verify` flag, additional fields are included:**
```json
{
  "sandboxes": [
    {
      "sandbox_id": "ipi7dxuc5687axlhm5hmd",
      "name": "demo-sandbox-1",
      "status": "active",
      "verified": true,
      "provider_status": "active",
      "created_at": "2025-11-03T01:03:40",
      "expires_at": "2025-11-03T01:13:40",
      "ttl_minutes": 10,
      "uptime_seconds": 123.4
    }
  ]
}
```

- `verified`: Boolean indicating if status was successfully verified with provider
- `provider_status`: Actual status from provider (may differ from `status` if DB was stale)

---

### sandbox status - Get status

Get detailed status and metadata for a sandbox.

**CLI:**
```bash
# Get status (human-readable)
nexus sandbox status sb_123

# Get status as JSON
nexus sandbox status sb_123 --json
```

**Python API:**
```python
# Get sandbox status
status = nx.sandbox_status(sandbox_id="sb_123")

print(f"Name: {status['name']}")
print(f"Status: {status['status']}")
print(f"Provider: {status['provider']}")
print(f"Uptime: {status['uptime_seconds']} seconds")
print(f"Expires: {status['expires_at']}")
```

**Options:**
- `--json, -j`: Output as JSON

**Returns:**
```json
{
  "sandbox_id": "ipi7dxuc5687axlhm5hmd",
  "name": "demo-sandbox-1",
  "user_id": "user123",
  "agent_id": null,
  "tenant_id": "default",
  "provider": "e2b",
  "template_id": "agupxq1ug1k3r5ujs8ma",
  "status": "active",
  "created_at": "2025-11-03T01:03:40",
  "last_active_at": "2025-11-03T01:05:22",
  "paused_at": null,
  "stopped_at": null,
  "ttl_minutes": 10,
  "expires_at": "2025-11-03T01:15:22",
  "uptime_seconds": 102.3
}
```

**Status Values:**
- `active`: Running and ready to execute code
- `paused`: Paused (saved state, no resource consumption)
- `stopped`: Destroyed (cannot be resumed)

---

### sandbox pause - Pause sandbox

Pause a sandbox to save costs. Preserves state but stops resource consumption.

**Note:** Not all providers support pause/resume. E2B does not support pause - use `stop` instead.

**CLI:**
```bash
# Pause sandbox
nexus sandbox pause sb_123

# Pause with JSON output
nexus sandbox pause sb_123 --json
```

**Python API:**
```python
# Pause sandbox
result = nx.sandbox_pause(sandbox_id="sb_123")

print(f"Status: {result['status']}")
print(f"Paused at: {result['paused_at']}")
```

**Options:**
- `--json, -j`: Output as JSON

**Returns:**
```json
{
  "sandbox_id": "sb_123",
  "name": "my-sandbox",
  "status": "paused",
  "paused_at": "2025-11-03T01:10:00",
  "expires_at": null
}
```

---

### sandbox resume - Resume sandbox

Resume a paused sandbox.

**Note:** Not all providers support pause/resume. E2B does not support resume - create a new sandbox instead.

**CLI:**
```bash
# Resume sandbox
nexus sandbox resume sb_123

# Resume with JSON output
nexus sandbox resume sb_123 --json
```

**Python API:**
```python
# Resume sandbox
result = nx.sandbox_resume(sandbox_id="sb_123")

print(f"Status: {result['status']}")
print(f"Expires: {result['expires_at']}")
```

**Options:**
- `--json, -j`: Output as JSON

**Returns:**
```json
{
  "sandbox_id": "sb_123",
  "name": "my-sandbox",
  "status": "active",
  "paused_at": null,
  "expires_at": "2025-11-03T01:20:00"
}
```

---

### sandbox stop - Stop sandbox

Stop and destroy a sandbox. This permanently destroys the sandbox and all its data.

**CLI:**
```bash
# Stop sandbox
nexus sandbox stop sb_123

# Stop with JSON output
nexus sandbox stop sb_123 --json
```

**Python API:**
```python
# Stop sandbox
result = nx.sandbox_stop(sandbox_id="sb_123")

print(f"Status: {result['status']}")
print(f"Stopped at: {result['stopped_at']}")
```

**Options:**
- `--json, -j`: Output as JSON

**Returns:**
```json
{
  "sandbox_id": "sb_123",
  "name": "my-sandbox",
  "status": "stopped",
  "stopped_at": "2025-11-03T01:15:00",
  "expires_at": null
}
```

---

## Common Workflows

### Quick One-Off Execution

```bash
# Create, run, and cleanup
SANDBOX=$(nexus sandbox create temp --json | jq -r '.sandbox_id')
nexus sandbox run $SANDBOX -c "print('Hello')"
nexus sandbox stop $SANDBOX
```

### Reuse Sandbox for Multiple Tasks

```bash
# Create once
SANDBOX=$(nexus sandbox create batch-processor --ttl 30 --json | jq -r '.sandbox_id')

# Run multiple tasks (TTL resets each time)
nexus sandbox run $SANDBOX -c "print('Task 1')"
nexus sandbox run $SANDBOX -c "print('Task 2')"
nexus sandbox run $SANDBOX -c "print('Task 3')"

# Cleanup
nexus sandbox stop $SANDBOX
```

### Data Analysis Pipeline

```bash
# Create sandbox
SANDBOX=$(nexus sandbox create data-analysis --ttl 60 --json | jq -r '.sandbox_id')

# Run analysis
nexus sandbox run $SANDBOX -f load_data.py
nexus sandbox run $SANDBOX -f transform_data.py
nexus sandbox run $SANDBOX -f generate_report.py

# Get results
nexus sandbox run $SANDBOX -c "cat report.txt"

# Cleanup
nexus sandbox stop $SANDBOX
```

### Multi-Language Workflow

```bash
SANDBOX=$(nexus sandbox create multi-lang --ttl 30 --json | jq -r '.sandbox_id')

# Process data with Python
nexus sandbox run $SANDBOX -l python -c "
import json
data = {'count': 42}
with open('data.json', 'w') as f:
    json.dump(data, f)
"

# Transform with JavaScript
nexus sandbox run $SANDBOX -l javascript -c "
const fs = require('fs');
const data = JSON.parse(fs.readFileSync('data.json'));
console.log('Count:', data.count);
"

# System info with Bash
nexus sandbox run $SANDBOX -l bash -c "
cat data.json
ls -lh
df -h
"

nexus sandbox stop $SANDBOX
```

---

## Error Handling

### Handling Timeouts

```bash
# Code that times out
nexus sandbox run $SANDBOX -c "
import time
time.sleep(100)
" --timeout 5

# Error: Code execution exceeded 5 second timeout
```

### Handling Execution Errors

```bash
# Code with errors
RESULT=$(nexus sandbox run $SANDBOX -c "print(1/0)" --json)

# Check exit code
EXIT_CODE=$(echo $RESULT | jq -r '.exit_code')
if [ "$EXIT_CODE" != "0" ]; then
    echo "Error occurred:"
    echo $RESULT | jq -r '.stderr'
fi
```

### Handling Provider Errors

```bash
# E2B doesn't support pause
nexus sandbox pause $SANDBOX
# Error: E2B doesn't support pause/resume. Use stop to destroy the sandbox.

# Use stop instead
nexus sandbox stop $SANDBOX
```

---

## Environment Variables

### Required for E2B Provider

```bash
# E2B API key (required)
export E2B_API_KEY="your-e2b-api-key"

# E2B team ID (optional)
export E2B_TEAM_ID="your-team-id"

# E2B template ID (optional, for custom templates)
export E2B_TEMPLATE_ID="your-template-id"
```

### Nexus Configuration

```bash
# Data directory
export NEXUS_DATA_DIR="/path/to/nexus-data"

# Database URL
export NEXUS_DATABASE_URL="postgresql://user:pass@localhost/nexus"

# Server URL (for remote mode)
export NEXUS_URL="http://localhost:8080"
export NEXUS_API_KEY="your-api-key"
```

---

## Best Practices

### 1. Always Cleanup

```python
sandbox = nx.sandbox_create("task")
try:
    result = nx.sandbox_run(sandbox["sandbox_id"], "python", code)
    return result
finally:
    nx.sandbox_stop(sandbox["sandbox_id"])
```

### 2. Use Appropriate TTLs

```bash
# Short TTL for one-off tasks
nexus sandbox create quick-task --ttl 5

# Longer TTL for interactive sessions
nexus sandbox create user-session --ttl 30

# Extended TTL for batch jobs
nexus sandbox create batch-job --ttl 120
```

### 3. Handle Errors Gracefully

```python
try:
    result = nx.sandbox_run(sandbox_id, "python", code, timeout=30)
    if result["exit_code"] != 0:
        logger.error(f"Code failed: {result['stderr']}")
except Exception as e:
    logger.error(f"Sandbox execution error: {e}")
```

### 4. Monitor Sandbox Usage

```bash
# List active sandboxes
nexus sandbox list

# Check for zombies
for sandbox in $(nexus sandbox list --json | jq -r '.sandboxes[] | select(.status=="active") | .sandbox_id'); do
    STATUS=$(nexus sandbox status $sandbox --json)
    UPTIME=$(echo $STATUS | jq -r '.uptime_seconds')
    if [ $(echo "$UPTIME > 3600" | bc) -eq 1 ]; then
        echo "Warning: Sandbox $sandbox has been running for over 1 hour"
    fi
done
```

### 5. Secure Code Execution

```python
# NEVER pass credentials in code
# BAD
code = f"api_key = '{os.getenv('API_KEY')}'"

# GOOD - use environment variables in sandbox
code = """
import os
api_key = os.getenv('API_KEY')  # Set via E2B template
"""
```

---

## Troubleshooting

### Problem: "E2B API key required"

```bash
# Solution: Set environment variable
export E2B_API_KEY="your-key"
```

### Problem: "Sandbox manager not initialized"

```bash
# Solution: Ensure E2B_API_KEY is set before starting server
export E2B_API_KEY="your-key"
nexus serve --host 0.0.0.0 --port 8080
```

### Problem: Sandbox creation is slow

**E2B**: First sandbox takes 2-5 seconds (cold start). Subsequent sandboxes are faster.

**Solution**: Reuse sandboxes for multiple tasks.

### Problem: Code execution times out

```bash
# Increase timeout
nexus sandbox run $SANDBOX -c "slow_code()" --timeout 120
```

---

## See Also

- [Sandbox Management Concept](../../concepts/sandbox-management.md) - Deep dive into sandboxes
- [Core API Reference](../core-api.md) - Python API reference
- [Comprehensive Demo](../../../examples/cli/sandbox_comprehensive_demo.sh) - Full demo script
- [E2B Documentation](https://e2b.dev/docs) - E2B provider docs

---

← [CLI Reference](index.md) | [API Documentation](../README.md)
