# Building Nexus Connectors

This guide explains how to build a connector that integrates external services (Gmail, Calendar, Slack, etc.) into Nexus's filesystem abstraction.

## Architecture Overview

Connectors in Nexus follow a **mixin-based architecture** that provides opt-in functionality:

```
┌─────────────────────────────────────────────────────────────┐
│                     Your Connector                          │
├─────────────────────────────────────────────────────────────┤
│  Backend (base class)           - Core filesystem ops       │
│  CacheConnectorMixin           - Database-backed caching    │
│  SkillDocMixin                 - SKILL.md documentation     │
│  ValidatedMixin                - Pydantic schema validation │
│  TraitBasedMixin               - Operation traits           │
│  CheckpointMixin               - Rollback support           │
└─────────────────────────────────────────────────────────────┘
```

## Quick Start

Here's the minimal structure for a new connector:

```
src/nexus/
├── backends/
│   └── myservice_connector.py      # Main connector implementation
└── connectors/
    └── myservice/
        ├── __init__.py             # Package exports
        ├── SKILL.md                # Agent-facing documentation
        ├── schemas.py              # Pydantic validation schemas
        └── errors.py               # Error registry
```

## Step 1: Create the Connector Backend

```python
# src/nexus/backends/myservice_connector.py

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.connectors.base import (
    CheckpointMixin,
    ConfirmLevel,
    OpTraits,
    Reversibility,
    SkillDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.connectors.myservice.errors import ERROR_REGISTRY


class MyServiceConnectorBackend(
    Backend,
    CacheConnectorMixin,
    SkillDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
    CheckpointMixin,
):
    """MyService connector backend."""

    # Skill documentation settings
    SKILL_NAME = "myservice"

    # Operation traits define validation requirements
    OPERATION_TRAITS = {
        "create_item": OpTraits(
            reversibility=Reversibility.FULL,      # Can be undone
            confirm=ConfirmLevel.INTENT,           # Needs agent_intent
            checkpoint=True,                        # Enable rollback
            intent_min_length=10,                   # Min chars for intent
        ),
        "delete_item": OpTraits(
            reversibility=Reversibility.NONE,      # Cannot undo
            confirm=ConfirmLevel.EXPLICIT,         # Needs confirm: true
            checkpoint=True,
            intent_min_length=10,
        ),
    }

    # Error registry for helpful messages
    ERROR_REGISTRY = ERROR_REGISTRY

    def __init__(self, token_manager_db: str, **kwargs):
        self.token_manager_db = token_manager_db
        # ... initialization

    @property
    def name(self) -> str:
        return "myservice"

    def generate_skill_doc(self, mount_path: str) -> str:
        """Load SKILL.md from static file."""
        import importlib.resources as resources

        try:
            content = (
                resources.files("nexus.connectors.myservice")
                .joinpath("SKILL.md")
                .read_text(encoding="utf-8")
            )
            # Replace mount path placeholder
            content = content.replace("/mnt/myservice/", mount_path)
            return content
        except Exception:
            return super().generate_skill_doc(mount_path)

    # Implement Backend methods: list_dir, read_content, write_content, etc.
```

## Step 2: Define Pydantic Schemas

```python
# src/nexus/connectors/myservice/schemas.py

from typing import Annotated
from pydantic import BaseModel, Field, field_validator


class CreateItemSchema(BaseModel):
    """Schema for creating items.

    Example:
        ```yaml
        # agent_intent: User wants to create a new project item
        name: Project Alpha
        description: Main project for Q1
        confirm: true
        ```
    """

    agent_intent: Annotated[
        str,
        Field(min_length=10, description="Why this action is needed"),
    ]
    name: Annotated[
        str,
        Field(min_length=1, max_length=255, description="Item name"),
    ]
    description: Annotated[
        str | None,
        Field(default=None, description="Item description"),
    ]
    confirm: Annotated[
        bool,
        Field(default=False, description="Explicit confirmation"),
    ]

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate and normalize name."""
        return v.strip()
```

## Step 3: Create Error Registry

```python
# src/nexus/connectors/myservice/errors.py

from nexus.connectors.base import ErrorDef

ERROR_REGISTRY: dict[str, ErrorDef] = {
    "MISSING_AGENT_INTENT": ErrorDef(
        message="Missing required 'agent_intent' comment",
        skill_section="required-format",
        fix_example="# agent_intent: User requested to create a new item",
    ),
    "AGENT_INTENT_TOO_SHORT": ErrorDef(
        message="agent_intent must be at least 10 characters",
        skill_section="required-format",
        fix_example="# agent_intent: Creating item as requested by user",
    ),
    "MISSING_CONFIRM": ErrorDef(
        message="This operation requires 'confirm: true'",
        skill_section="create-item",
        fix_example="confirm: true",
    ),
    "INVALID_NAME": ErrorDef(
        message="Item name is invalid or too long",
        skill_section="create-item",
        fix_example="name: Valid Item Name",
    ),
}
```

## Step 4: Write SKILL.md

```markdown
# MyService Connector

## Mount Path
`/mnt/myservice/`

## Overview
The MyService connector provides file-based access to MyService items.

## Directory Structure
```
/mnt/myservice/
  items/
    {item_id}.yaml     # Item files
    _new.yaml          # Write here to create
  .skill/
    SKILL.md
```

## Operations

### Create Item

Write to `items/_new.yaml`:

```yaml
# agent_intent: User requested to create a new project item
name: Project Alpha
description: Main project for Q1
confirm: true
```

## Required Format

All write operations require:
1. `# agent_intent:` comment explaining why (min 10 chars)
2. `confirm: true` for destructive operations

## Error Codes

| Code | Description | Fix |
|------|-------------|-----|
| MISSING_AGENT_INTENT | No agent_intent comment | Add `# agent_intent: ...` |
| MISSING_CONFIRM | Missing confirmation | Add `confirm: true` |
```

## Step 5: Package Exports

```python
# src/nexus/connectors/myservice/__init__.py

from nexus.connectors.myservice.errors import ERROR_REGISTRY
from nexus.connectors.myservice.schemas import CreateItemSchema

__all__ = ["ERROR_REGISTRY", "CreateItemSchema"]
```

## Mixin Reference

### SkillDocMixin

Provides SKILL.md integration:

| Attribute | Description |
|-----------|-------------|
| `SKILL_NAME` | Identifier (e.g., "gmail") |
| `SKILL_DIR` | Directory name (default: ".skill") |
| `generate_skill_doc(mount_path)` | Override to load static SKILL.md |

### ValidatedMixin

Provides Pydantic validation:

| Attribute | Description |
|-----------|-------------|
| `SCHEMAS` | Dict mapping operation names to Pydantic models |
| `validate_content(operation, data)` | Validate against schema |

### TraitBasedMixin

Provides operation trait validation:

| Attribute | Description |
|-----------|-------------|
| `OPERATION_TRAITS` | Dict mapping operations to `OpTraits` |
| `ERROR_REGISTRY` | Dict mapping error codes to `ErrorDef` |
| `validate_traits(operation, data)` | Validate traits |

### OpTraits Fields

| Field | Type | Description |
|-------|------|-------------|
| `reversibility` | `Reversibility` | FULL, PARTIAL, or NONE |
| `confirm` | `ConfirmLevel` | NONE, INTENT, EXPLICIT, or USER |
| `checkpoint` | `bool` | Enable rollback support |
| `intent_min_length` | `int` | Minimum chars for agent_intent |

### ConfirmLevel Hierarchy

```
NONE (0)     → No confirmation needed
INTENT (1)   → Requires # agent_intent: comment
EXPLICIT (2) → Requires intent + confirm: true
USER (3)     → Must ask user for confirmation
```

### CheckpointMixin

Provides rollback support:

| Method | Description |
|--------|-------------|
| `create_checkpoint(operation, metadata)` | Create rollback point |
| `complete_checkpoint(id, state)` | Mark as complete |
| `rollback_checkpoint(id)` | Rollback operation |
| `clear_checkpoint(id)` | Clear checkpoint |

## Testing Your Connector

### Unit Tests

```python
# tests/unit/connectors/test_myservice_schemas.py

import pytest
from pydantic import ValidationError
from nexus.connectors.myservice.schemas import CreateItemSchema


def test_valid_create():
    schema = CreateItemSchema(
        agent_intent="User wants to create a new item",
        name="Test Item",
        confirm=True,
    )
    assert schema.name == "Test Item"


def test_missing_intent_fails():
    with pytest.raises(ValidationError):
        CreateItemSchema(name="Test", confirm=True)
```

### Integration Tests

```python
# tests/integration/test_myservice_connector.py

import pytest
from nexus.connectors.base import ValidationError


class TestTraitValidation:
    def test_missing_intent_raises_error(self, backend):
        with pytest.raises(ValidationError) as exc:
            backend.validate_traits("create_item", {"name": "Test"})
        assert exc.value.code == "MISSING_AGENT_INTENT"
```

### E2E Tests

```python
# scripts/test_myservice_e2e.py

def test_skill_md_generation(backend):
    doc = backend.generate_skill_doc("/mnt/myservice/")
    assert "# MyService Connector" in doc
    assert "agent_intent" in doc


def test_create_item(backend, context):
    content = b"""# agent_intent: Creating test item
name: Test Item
confirm: true
"""
    response = backend.write_content(content, context)
    assert response.is_ok()
```

## OAuth Integration

For services requiring OAuth:

```python
def __init__(self, token_manager_db: str, user_email: str | None = None):
    from nexus.server.auth.token_manager import TokenManager

    self.token_manager = TokenManager(db_url=token_manager_db)
    self.user_email = user_email
    self._register_oauth_provider()

def _register_oauth_provider(self):
    from nexus.server.auth.oauth_factory import OAuthProviderFactory

    factory = OAuthProviderFactory()
    provider = factory.create_provider("myservice")
    self.token_manager.register_provider("myservice", provider)
```

Add OAuth config to `configs/oauth.yaml`:

```yaml
providers:
  - name: myservice
    display_name: MyService
    provider_class: nexus.server.auth.myservice_oauth.MyServiceOAuthProvider
    scopes:
      - read
      - write
    client_id_env: NEXUS_OAUTH_MYSERVICE_CLIENT_ID
    client_secret_env: NEXUS_OAUTH_MYSERVICE_CLIENT_SECRET
```

## Best Practices

1. **Static SKILL.md**: Write comprehensive documentation for agents
2. **Meaningful Errors**: Include fix examples in ERROR_REGISTRY
3. **Trait Configuration**: Use appropriate reversibility and confirm levels
4. **Schema Validation**: Validate all inputs with Pydantic
5. **Checkpoints**: Enable for reversible operations
6. **Test Coverage**: Unit, integration, and E2E tests

## Examples

See existing connectors for reference:
- `src/nexus/backends/gmail_connector.py` - Email with OAuth
- `src/nexus/backends/gcalendar_connector.py` - Calendar events
- `src/nexus/backends/slack_connector.py` - Chat integration
