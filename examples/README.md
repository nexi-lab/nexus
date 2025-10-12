# Nexus Examples

This directory contains example code demonstrating various Nexus features.

## Quick Start

Basic usage example:

```bash
python quickstart.py
```

## Examples (Coming Soon)

### Embedded Mode

- `embedded_basic.py` - Basic file operations in embedded mode
- `embedded_semantic_search.py` - Semantic search example
- `embedded_llm_read.py` - LLM-powered document reading

### Client Mode

- `client_basic.py` - REST API client usage
- `client_batch_operations.py` - Batch file operations
- `client_streaming.py` - Streaming large files

### Agent Workspaces

- `agent_workspace.py` - Creating and managing agent workspaces
- `agent_memory.py` - Agent memory system
- `agent_commands.py` - Custom command execution

### Document Processing

- `parse_pdf.py` - PDF document parsing
- `parse_excel.py` - Excel spreadsheet processing
- `semantic_chunking.py` - Semantic document chunking

### Backend Integration

- `s3_backend.py` - Amazon S3 integration
- `gdrive_backend.py` - Google Drive integration
- `multi_backend.py` - Multiple backend configuration

### Advanced Features

- `llm_cache.py` - LLM KV cache management
- `job_system.py` - Background job scheduling
- `mcp_server.py` - MCP server integration

## Running Examples

1. Install Nexus:
```bash
uv pip install -e ".[dev]"
```

2. Run an example:
```bash
python examples/quickstart.py
```

## Configuration

Examples use default configuration. To customize:

```python
from nexus import Embedded, EmbeddedConfig

config = EmbeddedConfig(
    data_dir="./custom-data",
    cache_size_mb=200,
    enable_vector_search=True
)

nx = Embedded(config)
```

## More Information

- [Main README](../README.md)
- [Architecture Document](../NEXUS_COMPREHENSIVE_ARCHITECTURE.md)
- [API Documentation](../docs/api.md)
