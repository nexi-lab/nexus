# Building Plugins

**Extend Nexus with custom functionality and lifecycle hooks**

⏱️ **Time:** 30 minutes | 💡 **Difficulty:** Hard

## What You'll Learn

- Understand the Nexus plugin system architecture
- Create custom plugins with lifecycle hooks
- Implement file operation interceptors
- Build custom parsers for specialized file types
- Package and distribute plugins
- Use event-driven workflows with plugins
- Debug and test plugins

## Prerequisites

✅ Python 3.8+ installed
✅ Nexus installed (`pip install nexus-ai-fs`)
✅ Understanding of Python decorators and async/await
✅ Familiarity with Nexus core concepts
✅ Experience with Python packaging (for distribution)

## Overview

**Plugins** extend Nexus functionality without modifying core code:

- **🔌 Lifecycle Hooks** - Intercept file operations (read, write, delete)
- **📝 Custom Parsers** - Add support for new file formats
- **🔄 Event Handlers** - React to filesystem events
- **🎨 Custom Backends** - Integrate new storage systems
- **🧠 Enhanced Processing** - Add AI-powered transformations
- **📊 Analytics** - Track and analyze usage patterns

**Plugin Architecture:**

```
┌─────────────────────────────────────────────────────────┐
│  Application Layer                                      │
│  ↓ Uses Nexus API                                      │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  Plugin Layer                                           │
│  ✓ Intercepts operations via hooks                     │
│  ✓ Transforms data before/after operations             │
│  ✓ Emits events for workflows                          │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  Nexus Core                                             │
│  ✓ VFS operations (read, write, list)                  │
│  ✓ Backend routing                                     │
│  ✓ ReBAC permissions                                   │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│  Storage Backends                                       │
│  ✓ Local, S3, GCS, PostgreSQL, etc.                   │
└─────────────────────────────────────────────────────────┘
```

**Available Hooks:**

- `pre_read` / `post_read` - Before/after reading files
- `pre_write` / `post_write` - Before/after writing files
- `pre_delete` / `post_delete` - Before/after deleting files
- `on_mount` / `on_unmount` - Backend lifecycle
- `on_startup` / `on_shutdown` - Server lifecycle

---

## Step 1: Plugin Project Structure

Create a new plugin project:

```bash
# Create plugin directory
mkdir nexus-plugin-image-optimizer
cd nexus-plugin-image-optimizer

# Create structure
mkdir -p nexus_image_optimizer
touch nexus_image_optimizer/__init__.py
touch nexus_image_optimizer/plugin.py
touch pyproject.toml
touch README.md
```

**Project structure:**
```
nexus-plugin-image-optimizer/
├── nexus_image_optimizer/
│   ├── __init__.py
│   └── plugin.py
├── pyproject.toml
├── README.md
└── tests/
    └── test_plugin.py
```

---

## Step 2: Basic Plugin Implementation

Create your first plugin:

```python
# nexus_image_optimizer/plugin.py
"""
Image Optimizer Plugin
Automatically optimizes images on write
"""
from typing import Any, Dict
from nexus.core.plugin_base import NexusPlugin
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class ImageOptimizerPlugin(NexusPlugin):
    """
    Plugin that automatically optimizes images when written to Nexus
    """

    def __init__(self, config: Dict[str, Any] = None):
        super().__init__(config)
        self.config = config or {}
        self.quality = self.config.get("quality", 85)
        self.max_width = self.config.get("max_width", 1920)
        logger.info(f"ImageOptimizer initialized (quality={self.quality})")

    async def on_startup(self, filesystem):
        """Called when Nexus starts"""
        self.filesystem = filesystem
        logger.info("ImageOptimizer plugin started")

    async def on_shutdown(self):
        """Called when Nexus shuts down"""
        logger.info("ImageOptimizer plugin stopped")

    async def pre_write(self, path: str, content: bytes, metadata: Dict[str, Any]) -> tuple[bytes, Dict[str, Any]]:
        """
        Called before writing a file
        Optimize image if it's a supported format
        """
        # Check if file is an image
        if not self._is_image(path):
            return content, metadata

        logger.info(f"Optimizing image: {path}")

        try:
            # Optimize the image
            optimized_content = await self._optimize_image(content, path)

            # Add metadata
            metadata["optimized"] = True
            metadata["original_size"] = len(content)
            metadata["optimized_size"] = len(optimized_content)
            metadata["compression_ratio"] = len(optimized_content) / len(content)

            logger.info(
                f"Optimized {path}: "
                f"{len(content)} → {len(optimized_content)} bytes "
                f"({metadata['compression_ratio']:.1%})"
            )

            return optimized_content, metadata

        except Exception as e:
            logger.error(f"Failed to optimize {path}: {e}")
            return content, metadata

    async def post_write(self, path: str, metadata: Dict[str, Any]) -> None:
        """
        Called after writing a file
        Log optimization results
        """
        if metadata.get("optimized"):
            logger.info(
                f"Image saved: {path} "
                f"(saved {metadata['original_size'] - metadata['optimized_size']} bytes)"
            )

    def _is_image(self, path: str) -> bool:
        """Check if file is a supported image"""
        extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        return Path(path).suffix.lower() in extensions

    async def _optimize_image(self, content: bytes, path: str) -> bytes:
        """
        Optimize image using PIL/Pillow
        """
        try:
            from PIL import Image
            from io import BytesIO

            # Open image
            img = Image.open(BytesIO(content))

            # Resize if too large
            if img.width > self.max_width:
                ratio = self.max_width / img.width
                new_height = int(img.height * ratio)
                img = img.resize((self.max_width, new_height), Image.LANCZOS)

            # Optimize
            output = BytesIO()
            img_format = img.format or "JPEG"

            if img_format == "JPEG":
                img.save(output, format="JPEG", quality=self.quality, optimize=True)
            elif img_format == "PNG":
                img.save(output, format="PNG", optimize=True)
            else:
                img.save(output, format=img_format)

            return output.getvalue()

        except ImportError:
            logger.error("PIL/Pillow not installed. Install with: pip install Pillow")
            return content
        except Exception as e:
            logger.error(f"Image optimization failed: {e}")
            return content


# Plugin factory function (required)
def create_plugin(config: Dict[str, Any] = None) -> ImageOptimizerPlugin:
    """Factory function to create plugin instance"""
    return ImageOptimizerPlugin(config)
```

---

## Step 3: Package Configuration

Configure your plugin for distribution:

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=61.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "nexus-plugin-image-optimizer"
version = "0.1.0"
description = "Nexus plugin for automatic image optimization"
readme = "README.md"
requires-python = ">=3.8"
license = {text = "MIT"}
authors = [
    {name = "Your Name", email = "your.email@example.com"}
]
keywords = ["nexus", "plugin", "image", "optimization"]
classifiers = [
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.8",
    "Programming Language :: Python :: 3.9",
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
]

dependencies = [
    "nexus-ai-fs>=0.5.0",
    "Pillow>=10.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.0",
    "pytest-asyncio>=0.21",
    "black>=23.0",
    "mypy>=1.0",
]

[project.urls]
Homepage = "https://github.com/yourusername/nexus-plugin-image-optimizer"
Documentation = "https://github.com/yourusername/nexus-plugin-image-optimizer#readme"
Repository = "https://github.com/yourusername/nexus-plugin-image-optimizer"

[project.entry-points."nexus.plugins"]
image_optimizer = "nexus_image_optimizer.plugin:create_plugin"

[tool.setuptools]
packages = ["nexus_image_optimizer"]
```

---

## Step 4: Using Your Plugin

Load and use your plugin:

### With Embedded Mode

```python
# test_plugin.py
import nexus
from nexus_image_optimizer.plugin import create_plugin

# Create filesystem with plugin
nx = nexus.connect(config={
    "data_dir": "./nexus-data",
    "plugins": [
        {
            "module": "nexus_image_optimizer.plugin",
            "factory": "create_plugin",
            "config": {
                "quality": 85,
                "max_width": 1920
            }
        }
    ]
})

# Write an image - will be automatically optimized
with open("large_photo.jpg", "rb") as f:
    image_data = f.read()
    print(f"Original size: {len(image_data)} bytes")

nx.write("/photos/optimized.jpg", image_data)

# Read back
optimized = nx.read("/photos/optimized.jpg")
print(f"Optimized size: {len(optimized)} bytes")
print(f"Savings: {len(image_data) - len(optimized)} bytes")
```

### With Server Mode

```bash
# Start server with plugin
nexus serve --host 0.0.0.0 --port 8080 \
  --plugin nexus_image_optimizer.plugin:create_plugin \
  --plugin-config '{"quality": 85, "max_width": 1920}'
```

```python
# Client using the plugin-enabled server
import nexus

nx = nexus.connect(config={
    "url": "http://localhost:8080",
    "api_key": "your-api-key"
})

# Images written to server will be automatically optimized
nx.write("/photos/vacation.jpg", image_data)
```

---

## Step 5: Advanced Plugin - Custom Parser

Create a plugin with a custom file parser:

```python
# nexus_yaml_parser/plugin.py
"""
YAML Parser Plugin
Parse YAML files with schema validation
"""
import yaml
from typing import Any, Dict
from nexus.core.plugin_base import NexusPlugin
import logging

logger = logging.getLogger(__name__)


class YAMLParserPlugin(NexusPlugin):
    """Parse and validate YAML files"""

    async def post_read(self, path: str, content: bytes, metadata: Dict[str, Any]) -> tuple[bytes, Dict[str, Any]]:
        """
        Parse YAML files after reading
        Add parsed data to metadata
        """
        if not path.endswith((".yaml", ".yml")):
            return content, metadata

        try:
            # Parse YAML
            parsed = yaml.safe_load(content.decode("utf-8"))

            # Add to metadata
            metadata["parsed_yaml"] = parsed
            metadata["yaml_keys"] = list(parsed.keys()) if isinstance(parsed, dict) else []

            logger.info(f"Parsed YAML: {path} ({len(metadata['yaml_keys'])} keys)")

        except Exception as e:
            logger.error(f"Failed to parse YAML {path}: {e}")
            metadata["yaml_error"] = str(e)

        return content, metadata

    async def pre_write(self, path: str, content: bytes, metadata: Dict[str, Any]) -> tuple[bytes, Dict[str, Any]]:
        """
        Validate YAML before writing
        """
        if not path.endswith((".yaml", ".yml")):
            return content, metadata

        try:
            # Validate YAML syntax
            yaml.safe_load(content.decode("utf-8"))
            metadata["yaml_valid"] = True

        except yaml.YAMLError as e:
            logger.error(f"Invalid YAML in {path}: {e}")
            metadata["yaml_valid"] = False
            metadata["yaml_error"] = str(e)

            # Optionally reject invalid YAML
            if self.config.get("strict_validation", False):
                raise ValueError(f"Invalid YAML: {e}")

        return content, metadata


def create_plugin(config: Dict[str, Any] = None) -> YAMLParserPlugin:
    return YAMLParserPlugin(config)
```

---

## Step 6: Event-Driven Workflow Plugin

Create a plugin that triggers workflows:

```python
# nexus_invoice_processor/plugin.py
"""
Invoice Processor Plugin
Automatically process invoices when uploaded
"""
from nexus.core.plugin_base import NexusPlugin
from typing import Any, Dict
import logging
import asyncio

logger = logging.getLogger(__name__)


class InvoiceProcessorPlugin(NexusPlugin):
    """Process invoices automatically on upload"""

    async def post_write(self, path: str, metadata: Dict[str, Any]) -> None:
        """
        Called after file is written
        Process invoices in /invoices/ directory
        """
        # Check if this is an invoice
        if not path.startswith("/invoices/"):
            return

        logger.info(f"New invoice detected: {path}")

        # Trigger processing workflow
        await self._process_invoice(path)

    async def _process_invoice(self, path: str):
        """
        Process invoice workflow:
        1. Extract data using OCR/LLM
        2. Validate against business rules
        3. Store in database
        4. Send notification
        """
        try:
            # Read invoice
            content = self.filesystem.read(path)

            # Extract data (placeholder - integrate with LLM)
            extracted_data = await self._extract_invoice_data(content)

            # Validate
            is_valid = await self._validate_invoice(extracted_data)

            # Store results
            result_path = path.replace("/invoices/", "/invoices/processed/")
            result_data = {
                "original_path": path,
                "extracted_data": extracted_data,
                "valid": is_valid,
                "processed_at": metadata.get("timestamp")
            }

            import json
            self.filesystem.write(
                f"{result_path}.json",
                json.dumps(result_data, indent=2).encode()
            )

            # Trigger notification
            await self._notify(path, result_data)

            logger.info(f"Invoice processed: {path}")

        except Exception as e:
            logger.error(f"Failed to process invoice {path}: {e}")

    async def _extract_invoice_data(self, content: bytes) -> Dict[str, Any]:
        """Extract invoice data using LLM/OCR"""
        # Placeholder - integrate with your LLM service
        return {
            "invoice_number": "INV-12345",
            "amount": 1250.00,
            "vendor": "Acme Corp",
            "date": "2025-01-15"
        }

    async def _validate_invoice(self, data: Dict[str, Any]) -> bool:
        """Validate invoice against business rules"""
        # Example validation
        return data.get("amount", 0) > 0 and data.get("invoice_number") is not None

    async def _notify(self, path: str, result: Dict[str, Any]):
        """Send notification about processed invoice"""
        # Placeholder - integrate with your notification system
        logger.info(f"Notification sent for {path}: {result}")


def create_plugin(config: Dict[str, Any] = None) -> InvoiceProcessorPlugin:
    return InvoiceProcessorPlugin(config)
```

---

## Step 7: Testing Plugins

Write tests for your plugins:

```python
# tests/test_plugin.py
import pytest
import nexus
from nexus_image_optimizer.plugin import ImageOptimizerPlugin


@pytest.fixture
async def filesystem():
    """Create test filesystem with plugin"""
    nx = nexus.connect(config={
        "data_dir": "./test-data",
        "plugins": [
            {
                "module": "nexus_image_optimizer.plugin",
                "factory": "create_plugin",
                "config": {"quality": 85}
            }
        ]
    })
    yield nx
    # Cleanup
    nx.rmdir("/", recursive=True)


@pytest.mark.asyncio
async def test_image_optimization(filesystem):
    """Test that images are optimized on write"""
    # Create test image
    from PIL import Image
    from io import BytesIO

    img = Image.new("RGB", (2000, 2000), color="red")
    output = BytesIO()
    img.save(output, format="JPEG", quality=100)
    original_data = output.getvalue()

    # Write to filesystem
    filesystem.write("/test.jpg", original_data)

    # Read back
    optimized_data = filesystem.read("/test.jpg")

    # Verify optimization
    assert len(optimized_data) < len(original_data), "Image should be smaller"
    assert len(optimized_data) > 0, "Image should not be empty"


@pytest.mark.asyncio
async def test_non_image_passthrough(filesystem):
    """Test that non-images are not modified"""
    text_data = b"Hello, world!"

    filesystem.write("/test.txt", text_data)
    result = filesystem.read("/test.txt")

    assert result == text_data, "Text file should not be modified"


@pytest.mark.asyncio
async def test_metadata_tracking(filesystem):
    """Test that optimization metadata is tracked"""
    from PIL import Image
    from io import BytesIO

    img = Image.new("RGB", (1000, 1000), color="blue")
    output = BytesIO()
    img.save(output, format="JPEG")
    image_data = output.getvalue()

    # Write and get metadata
    filesystem.write("/test.jpg", image_data)
    stat = filesystem.stat("/test.jpg")

    # Check metadata
    assert "optimized" in stat.metadata
    assert stat.metadata["optimized"] is True
    assert "compression_ratio" in stat.metadata
```

---

## Step 8: Plugin Distribution

Publish your plugin:

```bash
# Install build tools
pip install build twine

# Build package
python -m build

# Upload to PyPI (test first)
python -m twine upload --repository testpypi dist/*

# Upload to PyPI (production)
python -m twine upload dist/*
```

**README.md:**
```markdown
# Nexus Image Optimizer Plugin

Automatically optimize images when writing to Nexus filesystem.

## Installation

```bash
pip install nexus-plugin-image-optimizer
```

## Usage

### Embedded Mode

```python
import nexus

nx = nexus.connect(config={
    "data_dir": "./data",
    "plugins": [
        {
            "module": "nexus_image_optimizer.plugin",
            "factory": "create_plugin",
            "config": {
                "quality": 85,
                "max_width": 1920
            }
        }
    ]
})

# Images are automatically optimized
nx.write("/photos/vacation.jpg", image_data)
```

### Server Mode

```bash
nexus serve --plugin nexus_image_optimizer.plugin:create_plugin
```

## Configuration

- `quality` (int): JPEG quality (0-100, default: 85)
- `max_width` (int): Maximum image width (default: 1920)

## License

MIT
```

---

## Step 9: Real-World Plugin Example

Complete example: Skill Seekers Plugin (from Nexus ecosystem):

```python
# From nexus-plugin-skill-seekers
"""
Skill Seekers Plugin
Auto-generate skills from documentation
"""
from nexus.core.plugin_base import NexusPlugin
from typing import Any, Dict
import logging
import httpx

logger = logging.getLogger(__name__)


class SkillSeekersPlugin(NexusPlugin):
    """Generate AI skills from documentation URLs"""

    async def generate_skill(
        self,
        url: str,
        name: str,
        tier: str = "agent",
        use_ai: bool = True
    ) -> str:
        """
        Generate a skill from a documentation URL

        Args:
            url: Documentation URL
            name: Skill name
            tier: Skill tier (agent/tenant/system)
            use_ai: Use AI for enhancement

        Returns:
            Path to created skill
        """
        logger.info(f"Generating skill from {url}")

        # Fetch documentation
        content = await self._fetch_docs(url)

        # Enhance with AI if enabled
        if use_ai:
            content = await self._enhance_with_ai(content, name)

        # Create skill
        skill_path = f"/workspace/.nexus/skills/{name}/SKILL.md"
        self.filesystem.mkdir(f"/workspace/.nexus/skills/{name}", parents=True, exist_ok=True)
        self.filesystem.write(skill_path, content.encode())

        logger.info(f"Skill created: {skill_path}")
        return skill_path

    async def _fetch_docs(self, url: str) -> str:
        """Fetch documentation from URL"""
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    async def _enhance_with_ai(self, content: str, name: str) -> str:
        """Enhance documentation with AI"""
        # Use LLM to structure and enhance
        # ... implementation ...
        return content


def create_plugin(config: Dict[str, Any] = None) -> SkillSeekersPlugin:
    return SkillSeekersPlugin(config)
```

---

## Troubleshooting

### Issue: Plugin Not Loading

**Problem:** Plugin doesn't appear to run

**Solution:**
```python
# Check plugin registration
import logging
logging.basicConfig(level=logging.DEBUG)

nx = nexus.connect(config={
    "plugins": [{"module": "your_plugin", "factory": "create_plugin"}]
})

# Check logs for plugin initialization
```

---

### Issue: Hook Not Called

**Problem:** Plugin hook methods not executing

**Solution:**
```python
# Ensure hook signature matches exactly
async def pre_write(self, path: str, content: bytes, metadata: Dict[str, Any]):
    # Must return tuple
    return content, metadata

# Check that you're calling the method that triggers the hook
nx.write("/test.txt", b"data")  # Triggers pre_write and post_write
```

---

### Issue: Plugin Errors Break Nexus

**Problem:** Plugin exception crashes Nexus

**Solution:**
```python
# Always wrap plugin code in try-except
async def pre_write(self, path: str, content: bytes, metadata: Dict[str, Any]):
    try:
        # Your plugin logic
        result = await self._process(content)
        return result, metadata
    except Exception as e:
        logger.error(f"Plugin error: {e}")
        # Return original content to prevent breaking operation
        return content, metadata
```

---

## Best Practices

### 1. Error Handling

```python
# ✅ Good: Graceful degradation
async def pre_write(self, path, content, metadata):
    try:
        processed = await self._process(content)
        return processed, metadata
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        return content, metadata  # Return original

# ❌ Bad: Let exceptions propagate
async def pre_write(self, path, content, metadata):
    processed = await self._process(content)  # Can crash Nexus!
    return processed, metadata
```

### 2. Performance

```python
# ✅ Good: Async operations
async def post_write(self, path, metadata):
    await self._send_webhook(path)  # Non-blocking

# ❌ Bad: Blocking operations
async def post_write(self, path, metadata):
    time.sleep(5)  # Blocks all operations!
```

### 3. Configuration

```python
# ✅ Good: Configurable behavior
class MyPlugin(NexusPlugin):
    def __init__(self, config):
        super().__init__(config)
        self.enabled = config.get("enabled", True)
        self.threshold = config.get("threshold", 100)

# Usage:
# plugins: [{"module": "my_plugin", "config": {"threshold": 200}}]
```

### 4. Logging

```python
# ✅ Good: Descriptive logging
logger.info(f"Processing {path}: {len(content)} bytes")
logger.debug(f"Config: {self.config}")
logger.error(f"Failed to process {path}: {e}", exc_info=True)

# ❌ Bad: Minimal logging
logger.info("Processing")
print("Error")  # Use logger, not print!
```

---

## What's Next?

**Congratulations!** You've mastered Nexus plugin development.

### 🔍 Recommended Next Steps

1. **[Workflow Automation](workflow-automation.md)** (15 min)
   Combine plugins with workflows for powerful automation

2. **[Administration & Operations](administration-operations.md)** (25 min)
   Deploy plugin-enabled servers in production

3. **[API Reference](../api/api.md)**
   Explore the full plugin API

### 📚 Related Concepts

- [Plugin Architecture](../concepts/plugin-architecture.md)
- [Lifecycle Hooks](../api/plugin-hooks.md)
- [Event System](../concepts/workflows-vs-triggers.md)

### 🔧 Example Plugins

- [Skill Seekers](https://github.com/nexi-lab/nexus-plugin-skill-seekers) - Auto-generate skills from docs
- [Image Optimizer](https://github.com/nexi-lab/nexus-plugin-image-optimizer) - Optimize images on write
- [Virus Scanner](https://github.com/nexi-lab/nexus-plugin-clamav) - Scan files for malware

---

## Summary

🎉 **You've completed the Building Plugins tutorial!**

**What you learned:**
- ✅ Understand plugin architecture
- ✅ Create plugins with lifecycle hooks
- ✅ Implement custom parsers
- ✅ Build event-driven workflows
- ✅ Package and distribute plugins
- ✅ Test plugins thoroughly
- ✅ Apply best practices

**Key Takeaways:**
- Plugins extend Nexus without modifying core
- Lifecycle hooks intercept operations
- Always handle errors gracefully
- Use async operations for performance
- Plugins can trigger workflows
- Proper testing prevents bugs

---

**Next:** [Workflow Automation →](workflow-automation.md)

**Questions?** Check our [Plugin API Docs](../api/plugin-api.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
