# Glossary

**Quick reference for all Nexus terminology, acronyms, and technical concepts**

---

## A

### ACE (Agentic Context Engineering)
A framework for building self-improving AI agents through structured learning loops. ACE enables agents to learn from experience by recording trajectories, generating reflections, and creating reusable playbooks.

**Related:** [Learning Loops](concepts/learning-loops.md), [ACE API](api/ace-learning-loop.md)

### Agent
An autonomous AI entity that can perform tasks, make decisions, and interact with the Nexus filesystem. Agents can have their own identity, permissions, and memory.

**Related:** [Agent Permissions](concepts/agent-permissions.md)

### Agent-Tier Skills
Personal skills stored in `/workspace/.nexus/skills/` that are private to a specific agent or user. These have the highest priority in skill discovery.

**Related:** [Skills System](concepts/skills-system.md), [Skills Management](learning-paths/skills-management.md)

### API Key
Authentication credential used to access Nexus in server mode. Format: `nxk_...` for user keys, `nxk_agent_...` for agent keys.

**Related:** [Administration & Operations](learning-paths/administration-operations.md)

---

## B

### Backend
Storage layer that handles actual data persistence. Nexus supports multiple backends: local filesystem, S3, GCS, PostgreSQL, Redis, MongoDB.

**Related:** [Mounts & Backends](concepts/mounts-and-backends.md), [Multi-Backend Storage](learning-paths/multi-backend-storage.md)

### Blob
Binary large object - the actual content stored in Nexus, addressed by its SHA-256 hash in the content-addressable storage system.

**Related:** [Content-Addressable Storage](concepts/content-addressable-storage.md)

---

## C

### CAS (Content-Addressable Storage)
Storage mechanism where content is identified by its cryptographic hash (SHA-256) rather than location. Enables automatic deduplication and immutable version history.

**Related:** [Content-Addressable Storage](concepts/content-addressable-storage.md)

### CLI (Command Line Interface)
Text-based interface for interacting with Nexus. Commands start with `nexus` (e.g., `nexus read`, `nexus write`).

**Related:** [CLI Reference](api/cli/index.md)

### Consolidation
Process of merging and compressing memory entries to optimize storage and improve retrieval. Can be done synchronously or asynchronously.

**Related:** [Memory Consolidation](api/memory-consolidation.md)

---

## D

### Default Context
Automatically set tenant, namespace, and user context for operations. Simplifies API calls by not requiring explicit context parameters.

**Related:** [Configuration](api/configuration.md)

---

## E

### Embedded Mode
Deployment mode where Nexus runs directly within your application process. No separate server required, no authentication needed. Best for development and single-user scenarios.

**Related:** [Deployment Modes](getting-started/deployment-modes.md)

---

## F

### Feedback
User or system input about agent behavior, stored for learning and improvement. Part of the ACE learning loop.

**Related:** [Learning Loops](concepts/learning-loops.md)

### FUSE (Filesystem in Userspace)
Technology that allows mounting Nexus as a regular filesystem directory on Linux/macOS, enabling traditional file operations.

**Related:** [FUSE Mounts](advanced/fuse.md)

---

## G

### Group
Collection of users or agents that can be granted permissions collectively. Supports hierarchical relationships.

**Related:** [Team Collaboration](learning-paths/team-collaboration.md)

---

## H

### Hook
Plugin lifecycle method that intercepts operations. Types: `pre_read`, `post_read`, `pre_write`, `post_write`, `pre_delete`, `post_delete`, `on_mount`, `on_unmount`, `on_startup`, `on_shutdown`.

**Related:** [Plugin Hooks](api/plugins/hooks.md), [Building Plugins](learning-paths/building-plugins.md)

---

## I

### Identity
The authenticated entity (user or agent) performing operations. Determines permissions and memory namespace.

**Related:** [Agent Permissions](concepts/agent-permissions.md)

---

## L

### Learning Loop
Iterative process where agents record experiences (trajectories), reflect on outcomes, and generate reusable patterns (playbooks).

**Related:** [Learning Loops](concepts/learning-loops.md), [ACE](api/ace-learning-loop.md)

### LLM Document Reading
Feature that allows querying documents using natural language via LLMs, with citation support showing source locations.

**Related:** [LLM Document Reading](api/llm-document-reading.md)

---

## M

### MCP (Model Context Protocol)
Protocol for providing context to AI models. Nexus can serve as an MCP server, exposing filesystem operations to AI assistants.

**Related:** [MCP Integration](integrations/mcp.md), [MCP Server CLI](api/cli/mcp.md)

### Memory
Persistent storage of agent knowledge and context. Organized hierarchically: agent > session > namespace.

**Related:** [Memory System](concepts/memory-system.md), [Memory Management](api/memory-management.md)

### Metadata
Structured information about files including size, timestamps, permissions, content type, custom attributes, and version history.

**Related:** [Metadata API](api/metadata.md)

### Mount
Virtual mapping of a backend storage system to a path in the Nexus filesystem. Enables federation across multiple storage backends.

**Related:** [Mounts](api/mounts.md), [Mounts & Backends](concepts/mounts-and-backends.md)

### Multi-Tenancy
Architecture supporting multiple isolated customer environments (tenants) within a single Nexus deployment.

**Related:** [Multi-Tenancy](concepts/multi-tenancy.md), [Multi-Tenant SaaS](learning-paths/multi-tenant-saas.md)

---

## N

### Namespace
Logical container for organizing data and memory. Provides isolation boundary for multi-tenancy.

**Related:** [Multi-Tenancy](concepts/multi-tenancy.md)

---

## O

### Object
Generic term for any entity in Nexus that can have permissions: files, directories, namespaces, groups, workflows, skills.

**Related:** [ReBAC Explained](concepts/rebac-explained.md)

---

## P

### Parser
Plugin component that processes specific file types (e.g., YAML, JSON, images) and extracts metadata or transforms content.

**Related:** [Building Plugins](learning-paths/building-plugins.md)

### Playbook
Reusable pattern or template generated from successful agent trajectories. Stored in memory for future reference.

**Related:** [Playbook Management](api/playbook-management.md)

### Plugin
Extension that adds custom functionality to Nexus via lifecycle hooks, parsers, or event handlers.

**Related:** [Plugin System](concepts/plugin-system.md), [Building Plugins](learning-paths/building-plugins.md)

---

## R

### ReBAC (Relationship-Based Access Control)
Permission system based on relationships between subjects and objects. Inspired by Google Zanzibar. More flexible than traditional ACLs or RBAC.

**Related:** [ReBAC Explained](concepts/rebac-explained.md), [Permissions API](api/permissions.md)

### Reflection
Analysis of agent trajectory to extract learnings, identify patterns, and generate playbooks. Part of ACE learning loop.

**Related:** [Learning Loops](concepts/learning-loops.md)

### Remote Mode
See **Server Mode**.

---

## S

### Sandbox
Isolated execution environment for running untrusted code safely. Supports Docker and E2B backends.

**Related:** [Sandbox Management](concepts/sandbox-management.md), [Sandbox CLI](api/cli/sandbox.md)

### SDK (Software Development Kit)
Python library for interacting with Nexus programmatically. Install with `pip install nexus-ai-fs`.

**Related:** [Getting Started](api/getting-started.md)

### Semantic Search
Search capability that finds content by meaning rather than exact keyword matching. Uses vector embeddings.

**Related:** [Semantic Search](api/semantic-search.md)

### Server Mode
Deployment mode where Nexus runs as a standalone server with authentication, multi-user support, and remote access.

**Related:** [Deployment Modes](getting-started/deployment-modes.md), [Server Setup](deployment/server-setup.md)

### Session
Temporary context for a group of related operations. Sessions can have their own memory namespace.

**Related:** [Workspace Management](api/workspace-management.md)

### SHA-256
Cryptographic hash function used to generate content addresses in CAS. Produces 256-bit (32-byte) hash values.

**Related:** [Content-Addressable Storage](concepts/content-addressable-storage.md)

### Skill
Reusable AI capability packaged as a Markdown file with YAML frontmatter. Can be discovered and shared across agents.

**Related:** [Skills System](concepts/skills-system.md), [Skills Management](learning-paths/skills-management.md)

### Skill Seekers
Plugin that auto-generates skills from documentation URLs using AI enhancement.

**Related:** [Skill Seekers Plugin](api/plugins/skill-seekers.md)

### Subject
Entity that can perform actions: user, agent, or group. The "who" in permission tuples.

**Related:** [ReBAC Explained](concepts/rebac-explained.md)

### System-Tier Skills
Global skills stored in `/system/skills/` that are available to all users and agents. Lowest priority in skill discovery.

**Related:** [Skills System](concepts/skills-system.md)

---

## T

### Tenant
Isolated customer environment in a multi-tenant deployment. Has its own namespace, users, and data.

**Related:** [Multi-Tenancy](concepts/multi-tenancy.md)

### Tenant-Tier Skills
Organization-wide skills stored in `/shared/skills/` that are available to all members of a tenant.

**Related:** [Skills System](concepts/skills-system.md)

### Trajectory
Recorded sequence of agent actions, observations, and outcomes. Used for learning and reflection in ACE.

**Related:** [Trajectory Tracking](api/trajectory-tracking.md)

### Trigger
Deprecated term. See **Workflow**.

**Related:** [Workflows vs Triggers](concepts/workflows-vs-triggers.md)

### Tuple
Permission relationship expressed as `(subject, relation, object)`. Example: `(user:alice, can_read, file:/docs/report.pdf)`.

**Related:** [ReBAC Explained](concepts/rebac-explained.md)

---

## V

### Version
Immutable snapshot of a file at a specific point in time. Nexus maintains complete version history using CAS.

**Related:** [Versioning API](api/versioning.md)

### VFS (Virtual File System)
Unified interface that abstracts multiple backend storage systems, presenting them as a single filesystem hierarchy.

**Related:** [What is Nexus?](concepts/what-is-nexus.md)

---

## W

### Workflow
Event-driven automation that executes when files matching specific patterns are created or modified. Replaces the older "trigger" terminology.

**Related:** [Workflows](api/workflows.md), [Workflows vs Triggers](concepts/workflows-vs-triggers.md), [Workflow Automation](learning-paths/workflow-automation.md)

### Workspace
Root directory for user or agent operations. Typically mounted at `/workspace/`.

**Related:** [Workspace Management](api/workspace-management.md)

---

## Z

### Zanzibar
Google's relationship-based authorization system that inspired Nexus ReBAC implementation.

**Related:** [ReBAC Explained](concepts/rebac-explained.md)

---

## Common Abbreviations

| Abbreviation | Full Term | Description |
|--------------|-----------|-------------|
| **ACE** | Agentic Context Engineering | Learning framework for AI agents |
| **API** | Application Programming Interface | Programmatic interface for Nexus |
| **CAS** | Content-Addressable Storage | Hash-based storage system |
| **CLI** | Command Line Interface | Text-based command interface |
| **FUSE** | Filesystem in Userspace | Mount Nexus as regular filesystem |
| **GCS** | Google Cloud Storage | Cloud storage backend |
| **LLM** | Large Language Model | AI model for text processing |
| **MCP** | Model Context Protocol | AI context protocol |
| **POSIX** | Portable Operating System Interface | Unix-like filesystem API standard |
| **ReBAC** | Relationship-Based Access Control | Permission system |
| **S3** | Simple Storage Service | AWS object storage |
| **SDK** | Software Development Kit | Python library for Nexus |
| **VFS** | Virtual File System | Unified filesystem abstraction |
| **YAML** | YAML Ain't Markup Language | Human-readable data format |

---

## Quick Reference: File Path Conventions

| Path | Purpose | Example |
|------|---------|---------|
| `/workspace/` | User/agent working directory | `/workspace/my-project/` |
| `/shared/` | Tenant-wide shared resources | `/shared/docs/` |
| `/system/` | System-wide global resources | `/system/skills/` |
| `/.nexus/` | Nexus metadata and configuration | `/workspace/.nexus/skills/` |
| `/db/` | Database mount point | `/db/public/users` |

---

## Quick Reference: Permission Relations

| Relation | Description | Example Use Case |
|----------|-------------|------------------|
| `can_read` | Read access to object | View file contents |
| `can_write` | Write/modify access | Edit files, create subdirectories |
| `can_delete` | Delete access | Remove files or directories |
| `can_execute` | Execute access | Run scripts or workflows |
| `admin` | Full administrative access | Manage users, configure system |
| `member` | Group membership | User belongs to team |
| `owner` | Ownership relationship | Creator of resource |
| `parent` | Hierarchical relationship | Directory contains file |

---

## Quick Reference: CLI Commands

| Command | Description | Example |
|---------|-------------|---------|
| `nexus read` | Read file contents | `nexus read /workspace/file.txt` |
| `nexus write` | Write file contents | `nexus write /workspace/file.txt "content"` |
| `nexus ls` | List directory | `nexus ls /workspace/` |
| `nexus mkdir` | Create directory | `nexus mkdir /workspace/new-dir` |
| `nexus rm` | Remove file | `nexus rm /workspace/old-file.txt` |
| `nexus cat` | Display file contents | `nexus cat /workspace/file.txt` |
| `nexus serve` | Start server | `nexus serve --port 8080` |
| `nexus memory` | Memory operations | `nexus memory store "key" "value"` |
| `nexus rebac` | Permission management | `nexus rebac create --subject user:alice --relation can_read --object file:/docs` |
| `nexus skills` | Skill management | `nexus skills list` |
| `nexus admin` | Admin operations | `nexus admin create-user alice` |

---

## See Also

- **[Core Concepts](concepts/what-is-nexus.md)** - Understand how Nexus works
- **[Getting Started](getting-started/quickstart.md)** - Begin using Nexus
- **[API Reference](api/index.md)** - Complete API documentation
- **[Learning Paths](learning-paths/simple-file-storage.md)** - Step-by-step tutorials

---

**Need to add a term?** Contribute to this glossary on [GitHub](https://github.com/nexi-lab/nexus/edit/main/docs/glossary.md).
