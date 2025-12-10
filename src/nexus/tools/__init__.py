"""Module for tools to interact with Nexus server."""

# Add imports for tool modules here as needed
from . import langgraph
from .langgraph import (
    CODING_AGENT_SYSTEM_PROMPT,
    DATA_ANALYSIS_AGENT_SYSTEM_PROMPT,
    NEXUS_TOOLS_SYSTEM_PROMPT,
    RESEARCH_AGENT_SYSTEM_PROMPT,
    get_nexus_tools,
)

# Define __all__ to specify what is exported when doing 'from nexus.tools import *'
__all__ = [
    "langgraph",
    "get_nexus_tools",
    "NEXUS_TOOLS_SYSTEM_PROMPT",
    "CODING_AGENT_SYSTEM_PROMPT",
    "DATA_ANALYSIS_AGENT_SYSTEM_PROMPT",
    "RESEARCH_AGENT_SYSTEM_PROMPT",
]
