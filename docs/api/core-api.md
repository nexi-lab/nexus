# Core API

← [API Documentation](README.md)

The core API provides the main entry point for connecting to Nexus and initializing the filesystem.

## nexus.connect()

The main entry point for using Nexus. Auto-detects deployment mode and returns the appropriate client.

```python
def connect(
    config: str | Path | dict | NexusConfig | None = None
) -> Embedded
```

**Parameters:**
- `config` (optional): Configuration source
  - `None`: Auto-discover from environment/files (default)
  - `str | Path`: Path to config file (`.yaml` or `.json`)
  - `dict`: Configuration dictionary
  - `NexusConfig`: Pre-loaded configuration object

**Returns:**
- `NexusFilesystem`: Nexus filesystem instance (mode-dependent)

**Raises:**
- `ValueError`: If configuration is invalid
- `NotImplementedError`: If mode is not yet implemented (monolithic/distributed)

**Examples:**

```python
# Auto-detect (uses defaults)
nx = nexus.connect()

# With inline config
nx = nexus.connect(config={"data_dir": "./my-data"})

# From config file
nx = nexus.connect(config="./config.yaml")

# From environment
# Set NEXUS_DATA_DIR=/path/to/data
nx = nexus.connect()
```

## See Also

- [Getting Started](getting-started.md) - Quick start guide
- [Configuration](configuration.md) - Detailed configuration options
- [File Operations](file-operations.md) - Working with files
- [Error Handling](error-handling.md) - Exception handling

## Next Steps

1. Configure your [backend storage](configuration.md#multi-backend-support)
2. Learn about [file operations](file-operations.md)
3. Set up [permissions](permissions.md) for multi-user environments
