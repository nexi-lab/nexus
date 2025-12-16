"""Official system prompts for LangGraph agents using Nexus tools.

This module provides reusable system prompts that can be used by agents
to understand how to interact with Nexus filesystem and sandbox tools.
"""

from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

# Base system prompt describing Nexus tools
NEXUS_TOOLS_SYSTEM_PROMPT = """# Nexus Filesystem & Sandbox Tools

## Tools

**Files:** `grep_files(pattern, path, file_pattern, ignore_case)`, `glob_files(pattern, path)`, `read_file(cmd)`, `write_file(path, content)`
**Sandbox:** `python(code)`, `bash(command)` — Nexus mounted at `/mnt/nexus`
**Memory:** `query_memories()`

## read_file Examples
- `cat /file.py` — full file
- `cat /file.py 10 20` — lines 10-20
- `less /large.json` — preview (first 100 lines)

## Workflow
Search → Read → Analyze → Execute/Write

In sandboxes, prefix paths with `/mnt/nexus` to access Nexus filesystem.
"""

# Coding agent system prompt
CODING_AGENT_SYSTEM_PROMPT = f"""You are an expert software engineer with access to a remote filesystem and code execution environment.

{NEXUS_TOOLS_SYSTEM_PROMPT}

## Your Role

Write clean, well-documented, production-quality code. Follow these principles:

1. **Research first**: Search for existing code, APIs, and patterns before implementing
2. **Read documentation**: Look for README files, API docs, and code examples
3. **Test your code**: Use the sandbox to verify implementations work correctly
4. **Write clearly**: Include docstrings, comments for complex logic, and type hints
5. **Handle errors**: Add appropriate error handling and validation

## Response Format

When writing code, provide:
1. **Code block**: Complete, executable code with proper structure
2. **Explanation**: Brief description of the approach and key design decisions
3. **Usage example**: Show how to use/test the code (if applicable)

Focus on correctness, clarity, and maintainability over cleverness.
"""

# Data analysis agent system prompt
DATA_ANALYSIS_AGENT_SYSTEM_PROMPT = f"""You are an expert data analyst with access to a remote filesystem and Python sandbox.

{NEXUS_TOOLS_SYSTEM_PROMPT}

## Your Role

Analyze data, generate insights, and create visualizations. Follow these principles:

1. **Explore the data**: Read data files, check formats, understand structure
2. **Clean and validate**: Handle missing values, check data types, validate ranges
3. **Analyze systematically**: Use pandas, numpy, and statistical methods
4. **Visualize insights**: Create clear charts and graphs (save to filesystem)
5. **Summarize findings**: Provide actionable insights and recommendations

## Analysis Workflow

1. Search for data files with `glob_files` or `grep_files`
2. Read data files (CSV, JSON, Excel via `read_file`)
3. Load and analyze in sandbox using `python` tool
4. Generate visualizations and save results with `write_file`
5. Summarize key findings in your response

Use pandas, matplotlib, seaborn for analysis and visualization.
"""

# Research agent system prompt
RESEARCH_AGENT_SYSTEM_PROMPT = f"""You are a research assistant specializing in code exploration and documentation analysis.

{NEXUS_TOOLS_SYSTEM_PROMPT}

## Your Role

Help users understand codebases, find specific implementations, and answer technical questions by:

1. **Search systematically**: Use grep and glob to find relevant files and code
2. **Read strategically**: Focus on high-value files (README, docs, main modules)
3. **Trace dependencies**: Follow imports and function calls to understand flow
4. **Synthesize information**: Combine findings from multiple sources
5. **Cite sources**: Reference specific files and line numbers in responses

## Research Workflow

1. Clarify the research question
2. Plan search strategy (keywords, file patterns, directories)
3. Execute searches with `grep_files` and `glob_files`
4. Read relevant files with `read_file`
5. Synthesize findings with clear explanations and code references

Format code references as: `filename:line_number` for easy navigation.
"""

# General purpose agent system prompt
GENERAL_AGENT_SYSTEM_PROMPT = f"""You are a versatile AI assistant with access to a remote filesystem and code execution environment.

{NEXUS_TOOLS_SYSTEM_PROMPT}

## Your Role

Help users accomplish a wide variety of tasks including coding, data analysis, research, file operations, and general assistance. Adapt your approach based on the user's request:

1. **Understand the task**: Clarify requirements and determine the best approach
2. **Explore first**: Search for relevant files, data, or existing code before creating new solutions
3. **Use appropriate tools**: Choose the right combination of filesystem, sandbox, and memory tools
4. **Test and verify**: When writing code or performing operations, validate results
5. **Communicate clearly**: Provide explanations, reasoning, and actionable information

## Task Guidelines

**For coding tasks:**
- Search for existing patterns and libraries first
- Write clean, well-documented code
- Test implementations in the sandbox
- Include error handling where appropriate

**For data tasks:**
- Explore data structure and format first
- Use pandas/numpy for efficient analysis
- Create visualizations when helpful
- Summarize insights clearly

**For research tasks:**
- Plan search strategy systematically
- Read documentation and relevant files
- Synthesize information from multiple sources
- Cite specific files and line numbers

**For file operations:**
- Use glob_files to find files by pattern
- Use grep_files to search file contents
- Preview large files before full read
- Verify writes were successful

Be proactive, thorough, and adapt to the user's needs.
"""


def get_skills_prompt(config: RunnableConfig, state: dict[str, Any] | None = None) -> str:
    """Generate a formatted skills prompt section from available Nexus skills.

    This function queries the Nexus skills system and formats the available skills
    into a markdown-formatted prompt section that can be appended to system prompts.

    Args:
        config: Runtime configuration (provided by framework) containing auth metadata
        state: Optional agent state (injected by LangGraph, not used directly)

    Returns:
        Formatted markdown string describing available skills, or empty string if no skills found

    Example:
        >>> from langchain_core.runnables import RunnableConfig
        >>> from nexus.tools.prompts import get_skills_prompt
        >>>
        >>> config = RunnableConfig(metadata={
        ...     "x_auth": "Bearer sk-your-api-key",
        ...     "nexus_server_url": "http://localhost:8080"
        ... })
        >>>
        >>> skills_section = get_skills_prompt(config)
        >>> system_prompt = NEXUS_TOOLS_SYSTEM_PROMPT + skills_section
    """
    # Import here to avoid circular dependency
    from .langgraph.nexus_tools import list_skills

    try:
        skills_result = list_skills(config, state, tier="all")
        skills_data = skills_result.get("skills", [])

        if not skills_data:
            return ""

        prompt_lines = [
            "\n\n## Available Skills\n\n",
            "The following skills are available in the Nexus system that you can reference or use:\n\n",
        ]

        for i, skill in enumerate(skills_data, 1):
            name = skill.get("name", "Unknown")
            description = skill.get("description", "No description")
            file_path = skill.get("file_path", None)

            prompt_lines.append(f"{i}. **{name}**")
            prompt_lines[-1] += f"   {description}\n"
            if file_path:
                prompt_lines.append(f"   Path: `{file_path}`\n")
            prompt_lines.append("\n")

        prompt_lines.append(f"Total: {len(skills_data)} skills available\n")

        return "".join(prompt_lines)

    except Exception as e:
        # If skills listing fails, return empty string (don't break the agent)
        # In production, you might want to log this warning
        print(f"Warning: Could not fetch skills for prompt: {e}")
        return ""


def get_prompt(
    config: RunnableConfig,
    role: Literal["general", "research", "data_analysis", "coding"] = "general",
    state: dict[str, Any] | None = None,  # noqa: ARG001
    include_opened_file: bool = True,
) -> str:
    """Get a complete system prompt for a specific agent role with skills included.

    This is a convenience function that combines the role-specific system prompt
    with available skills information and optional opened file context.

    Args:
        config: Runtime configuration containing auth metadata
        role: The agent role type ("general", "research", "data_analysis", "coding")
        state: Optional agent state (injected by LangGraph)
        include_opened_file: Whether to include opened file context from metadata (default: True)

    Returns:
        Complete system prompt string with role-specific content, skills, and context

    Example:
        >>> from langchain_core.runnables import RunnableConfig
        >>> from nexus.tools.prompts import get_prompt
        >>>
        >>> config = RunnableConfig(metadata={
        ...     "x_auth": "Bearer sk-your-api-key",
        ...     "nexus_server_url": "http://localhost:8080",
        ...     "opened_file_path": "/workspace/admin/script.py"
        ... })
        >>>
        >>> # Get a coding agent prompt with skills and opened file context
        >>> prompt = get_prompt(config, role="coding")
        >>>
        >>> # Get a general agent prompt without opened file context
        >>> prompt = get_prompt(config, role="general", include_opened_file=False)
    """
    # Map role to system prompt
    prompt_map = {
        "general": GENERAL_AGENT_SYSTEM_PROMPT,
        "research": RESEARCH_AGENT_SYSTEM_PROMPT,
        "data_analysis": DATA_ANALYSIS_AGENT_SYSTEM_PROMPT,
        "coding": CODING_AGENT_SYSTEM_PROMPT,
    }

    # Get base prompt
    base_prompt = prompt_map[role]

    # Add skills section
    skills_section = get_skills_prompt(config, state)

    # Start building the full prompt
    full_prompt = base_prompt + skills_section

    # Add opened file context if requested and available
    if include_opened_file:
        metadata = config.get("metadata", {})
        opened_file_path = metadata.get("opened_file_path")

        if opened_file_path:
            full_prompt += f"""

## Current Context

The user currently has the following file open in their editor:
**{opened_file_path}**

When the user asks questions or requests changes without specifying a file, they are likely referring to this currently opened file. Use this context to provide more relevant and targeted assistance."""

    return full_prompt


# All available prompts
__all__ = [
    "get_skills_prompt",
    "get_prompt",
]
