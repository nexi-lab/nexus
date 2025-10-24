"""Nexus File Operation Tools for LangGraph ReAct Agent.

This module provides four practical file operation tools that wrap Nexus filesystem
capabilities for use with LangGraph agents. Tools use familiar command-line syntax
to make them intuitive for agents to use.

1. grep_files: Search file content using grep-style commands
2. glob_files: Find files by name pattern using glob syntax
3. read_file: Read file content using cat/less-style commands
4. write_file: Write content to Nexus filesystem

These tools enable agents to interact with a remote Nexus filesystem, allowing them
to search, read, analyze, and persist data across agent runs.
"""

import shlex
from typing import Any, Optional

from langchain_core.tools import tool


def get_nexus_tools(nx):
    """
    Create LangGraph tools from a Nexus filesystem instance.

    Args:
        nx: NexusFilesystem instance (local or remote)

    Returns:
        List of LangGraph tool functions
    """

    @tool
    def grep_files(grep_cmd: str) -> str:
        """Search file content using grep-style commands.

        Use this tool to find files containing specific text or code patterns.
        Follows grep command syntax for familiarity.

        Args:
            grep_cmd: Grep command in format: "pattern [path] [options]"
                     - pattern: Required. Text or regex to search for (quote if contains spaces)
                     - path: Optional. Directory to search (default: "/")
                     - Options: -i (case insensitive)

                     Examples:
                     - "async def /workspace"
                     - "'import pandas' /scripts -i"
                     - "TODO:"
                     - "function.*calculate /src"

        Returns:
            String describing matches found, including file paths, line numbers, and content.
            Returns "No matches found" if pattern doesn't match anything.

        Examples:
            - grep_files("async def /workspace") → Find all async function definitions
            - grep_files("TODO:") → Find all TODO comments in entire filesystem
            - grep_files("'import pandas' /scripts -i") → Case-insensitive pandas imports
        """
        try:
            # Parse grep command
            parts = shlex.split(grep_cmd)
            if not parts:
                return "Error: Empty grep command. Usage: grep_files('pattern [path] [options]')"

            pattern = parts[0]
            path = "/"
            case_sensitive = True

            # Parse remaining arguments
            i = 1
            while i < len(parts):
                arg = parts[i]
                if arg == "-i":
                    case_sensitive = False
                elif not arg.startswith("-"):
                    # Assume it's a path
                    path = arg
                i += 1

            # Execute grep
            results = nx.grep(pattern, path, ignore_case=not case_sensitive)

            if not results:
                return f"No matches found for pattern '{pattern}' in {path}"

            # Format results into readable output
            output_lines = [f"Found {len(results)} matches for pattern '{pattern}' in {path}:\n"]

            # Group by file for better readability
            current_file = None
            for match in results[:50]:  # Limit to first 50 matches
                file_path = match.get('file', 'unknown')
                line_num = match.get('line', 0)
                content = match.get('content', '').strip()

                if file_path != current_file:
                    output_lines.append(f"\n{file_path}:")
                    current_file = file_path

                output_lines.append(f"  Line {line_num}: {content}")

            if len(results) > 50:
                output_lines.append(f"\n... and {len(results) - 50} more matches")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error executing grep: {str(e)}\nUsage: grep_files('pattern [path] [options]')"

    @tool
    def glob_files(pattern: str, path: str = "/") -> str:
        """Find files by name pattern using glob syntax.

        Use this tool to find files matching a specific naming pattern.
        Supports standard glob patterns like wildcards and recursive search.

        Args:
            pattern: Glob pattern to match filenames (e.g., "*.py", "**/*.md", "test_*.py")
            path: Directory path to search in (default: "/" for entire filesystem)

        Returns:
            String listing all matching file paths, one per line.
            Returns "No files found" if no matches.

        Examples:
            - glob_files("*.py", "/workspace") → Find all Python files
            - glob_files("**/*.md", "/docs") → Find all Markdown files recursively
            - glob_files("test_*.py", "/tests") → Find all test files
        """
        try:
            files = nx.glob(pattern, path)

            if not files:
                return f"No files found matching pattern '{pattern}' in {path}"

            # Format results
            output_lines = [f"Found {len(files)} files matching '{pattern}' in {path}:\n"]
            output_lines.extend(f"  {file}" for file in files[:100])  # Limit to first 100

            if len(files) > 100:
                output_lines.append(f"\n... and {len(files) - 100} more files")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error finding files: {str(e)}"

    @tool
    def read_file(read_cmd: str) -> str:
        """Read file content using cat/less-style commands.

        Use this tool to read and analyze file contents.
        Works with text files including code, documentation, and data files.
        Supports both 'cat' (full content) and 'less' (preview) commands.

        Args:
            read_cmd: Read command in format: "[cat|less] path"
                     - cat: Display entire file content
                     - less: Display first 100 lines as preview
                     - path: File path to read

                     Examples:
                     - "cat /workspace/README.md"
                     - "less /scripts/analysis.py"
                     - "/data/results.json" (defaults to cat)

        Returns:
            File content as string, or error message if file cannot be read.

        Examples:
            - read_file("cat /workspace/README.md") → Read entire README
            - read_file("less /scripts/large_file.py") → Preview first 100 lines
            - read_file("/data/results.json") → Read JSON file (defaults to cat)
        """
        try:
            # Parse read command
            parts = shlex.split(read_cmd.strip())
            if not parts:
                return "Error: Empty read command. Usage: read_file('[cat|less] path')"

            # Determine command type and path
            if parts[0] in ["cat", "less"]:
                command = parts[0]
                if len(parts) < 2:
                    return f"Error: Missing file path. Usage: read_file('{command} path')"
                path = parts[1]
            else:
                # Default to cat if no command specified
                command = "cat"
                path = parts[0]

            # Read file content
            content = nx.read(path)

            # Handle bytes
            if isinstance(content, bytes):
                content = content.decode('utf-8')

            # For 'less', show preview
            if command == "less":
                lines = content.split('\n')
                if len(lines) > 100:
                    preview_content = '\n'.join(lines[:100])
                    output = f"Preview of {path} (first 100 of {len(lines)} lines):\n\n"
                    output += preview_content
                    output += f"\n\n... ({len(lines) - 100} more lines)"
                else:
                    output = f"Content of {path} ({len(lines)} lines):\n\n"
                    output += content
            else:
                # For 'cat', show full content
                output = f"Content of {path} ({len(content)} characters):\n\n"
                output += content

            return output

        except FileNotFoundError:
            return f"Error: File not found: {read_cmd}"
        except Exception as e:
            return f"Error reading file: {str(e)}\nUsage: read_file('[cat|less] path')"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to Nexus filesystem.

        Use this tool to save analysis results, reports, or generated content.
        Creates parent directories automatically if they don't exist.
        Overwrites existing files.

        Args:
            path: Absolute path where file should be written (e.g., "/reports/summary.md")
            content: Text content to write to the file

        Returns:
            Success message with file path and size, or error message if write fails.

        Examples:
            - write_file("/reports/summary.md", "# Summary\\n...") → Save analysis report
            - write_file("/workspace/config.json", "{}") → Create config file
            - write_file("/data/results.txt", "Results:\\n...") → Save results
        """
        try:
            # Convert string to bytes for Nexus
            content_bytes = content.encode('utf-8') if isinstance(content, str) else content

            # Write file (Nexus creates parent directories automatically)
            nx.write(path, content_bytes)

            # Verify write was successful
            if nx.exists(path):
                size = len(content_bytes)
                return f"Successfully wrote {size} bytes to {path}"
            else:
                return f"Error: Failed to write file {path} (file does not exist after write)"

        except Exception as e:
            return f"Error writing file {path}: {str(e)}"

    # Return all tools
    return [grep_files, glob_files, read_file, write_file]
