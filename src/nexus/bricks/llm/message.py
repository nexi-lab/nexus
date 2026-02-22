"""Message types for LLM interactions.

Backward-compat shim (Issue #2190): Canonical location is
``nexus.contracts.llm_types``. This module re-exports for existing importers.
"""

from nexus.contracts.llm_types import ContentType as ContentType  # noqa: F401
from nexus.contracts.llm_types import ImageContent as ImageContent  # noqa: F401
from nexus.contracts.llm_types import ImageDetail as ImageDetail  # noqa: F401
from nexus.contracts.llm_types import Message as Message  # noqa: F401
from nexus.contracts.llm_types import MessageRole as MessageRole  # noqa: F401
from nexus.contracts.llm_types import TextContent as TextContent  # noqa: F401
from nexus.contracts.llm_types import ToolCall as ToolCall  # noqa: F401
from nexus.contracts.llm_types import ToolFunction as ToolFunction  # noqa: F401
