# Agent Frameworks

nexus-fs provides file operations as tools for AI agent frameworks.
Each example below shows a minimal working integration.

## LangChain

Use nexus-fs as a document loader or as a tool in a LangChain agent.

### Document loader

```python
# skip-test
from langchain_core.documents import Document
import nexus.fs

fs = nexus.fs.mount_sync("local://./docs")

def load_documents(path: str) -> list[Document]:
    """Load all files under a path as LangChain documents."""
    files = fs.ls(path, detail=True)
    docs = []
    for entry in files:
        if entry.get("entry_type") == 0:  # 0 = file, 1 = directory
            content = fs.read(entry["path"])
            docs.append(Document(
                page_content=content.decode(errors="replace"),
                metadata={"source": entry["path"], "size": entry.get("size", 0)},
            ))
    return docs
```

### As a tool

```python
# skip-test
from langchain_core.tools import tool
import nexus.fs

fs = nexus.fs.mount_sync("s3://my-bucket", "local://./workspace")

@tool
def read_file(path: str) -> str:
    """Read a file from the mounted filesystem."""
    return fs.read(path).decode(errors="replace")

@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file."""
    fs.write(path, content.encode())
    return f"Written to {path}"

@tool
def list_files(path: str) -> list[str]:
    """List files in a directory."""
    return fs.ls(path)
```

## CrewAI

Provide nexus-fs as a tool to CrewAI agents:

```python
# skip-test
from crewai import Agent, Task, Crew
from crewai.tools import tool
import nexus.fs

fs = nexus.fs.mount_sync("local://./workspace")

@tool("Read File")
def read_file(path: str) -> str:
    """Read a file from the workspace."""
    return fs.read(path).decode(errors="replace")

@tool("Write File")
def write_file(path: str, content: str) -> str:
    """Write content to a file in the workspace."""
    fs.write(path, content.encode())
    return f"Saved to {path}"

researcher = Agent(
    role="Researcher",
    goal="Analyze files in the workspace",
    tools=[read_file, write_file],
)
```

## Claude SDK

Use nexus-fs with the Anthropic Claude SDK for tool use:

```python
# skip-test
import anthropic
import nexus.fs

fs = nexus.fs.mount_sync("local://./workspace")

tools = [
    {
        "name": "read_file",
        "description": "Read a file from the mounted filesystem",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

def handle_tool_call(name: str, input: dict) -> str:
    if name == "read_file":
        return fs.read(input["path"]).decode(errors="replace")
    raise ValueError(f"Unknown tool: {name}")
```

## OpenAI Agents

```python
# skip-test
from openai import OpenAI
import nexus.fs

fs = nexus.fs.mount_sync("local://./workspace")

tools = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]

def handle_function_call(name: str, arguments: dict) -> str:
    if name == "read_file":
        return fs.read(arguments["path"]).decode(errors="replace")
    raise ValueError(f"Unknown function: {name}")
```

## LangGraph

nexus-fs works with LangGraph state graphs as a tool node:

```python
# skip-test
from langchain_core.tools import tool
import nexus.fs

fs = nexus.fs.mount_sync("s3://my-bucket", "local://./cache")

@tool
def filesystem_read(path: str) -> str:
    """Read a file from any mounted backend."""
    return fs.read(path).decode(errors="replace")

@tool
def filesystem_write(path: str, content: str) -> str:
    """Write content to any mounted backend."""
    fs.write(path, content.encode())
    return f"Written {len(content)} bytes to {path}"

# Use these tools in a LangGraph ToolNode
```

## Pattern: shared filesystem across agents

All agent frameworks benefit from the same pattern — mount once,
share the filesystem instance across agents:

```python
# skip-test
import nexus.fs

# Mount shared workspace + cloud storage
fs = nexus.fs.mount_sync("local://./workspace", "s3://shared-data")

# Every agent/tool gets the same fs instance
# Agent A writes to /local/workspace/report.md
# Agent B reads from /local/workspace/report.md
# Agent C reads from /s3/shared-data/reference.csv
```

This gives agents a shared, unified namespace without manually
passing files between them.
