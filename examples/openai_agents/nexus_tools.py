"""Nexus File Operation Tools for OpenAI Agents SDK.

This module provides Nexus filesystem tools using the @function_tool decorator
from the OpenAI Agents SDK. Tools support standard file operations with familiar
command-line syntax to make them intuitive for agents.

Tools provided:
1. grep_files: Search file content using grep-style patterns
2. glob_files: Find files by name pattern using glob syntax
3. read_file: Read file content (with optional preview mode)
4. write_file: Write content to Nexus filesystem

These tools enable agents to interact with a Nexus filesystem (local or remote),
allowing them to search, read, analyze, and persist data across agent runs.
"""

from agents import function_tool


def get_nexus_tools(nx):
    """
    Create OpenAI Agent SDK tools from a Nexus filesystem instance.

    Args:
        nx: NexusFilesystem instance (local or remote)

    Returns:
        List of function tool definitions decorated with @function_tool
    """

    @function_tool
    async def grep_files(pattern: str, path: str = "/", case_sensitive: bool = True) -> str:
        """Search file content using grep-style patterns.

        Use this tool to find files containing specific text or code patterns.
        Searches through file contents and returns matching lines with context.

        Args:
            pattern: Text or regex pattern to search for (e.g., "async def", "TODO:", "import.*pandas")
            path: Directory path to search in (default: "/" for entire filesystem)
            case_sensitive: Whether the search is case-sensitive (default: True)

        Returns:
            String describing matches found, including file paths, line numbers, and content.
            Returns "No matches found" if pattern doesn't match anything.

        Examples:
            - grep_files("async def", "/workspace") → Find all async function definitions
            - grep_files("TODO:", "/") → Find all TODO comments in entire filesystem
            - grep_files("import pandas", "/scripts", False) → Case-insensitive pandas imports
        """
        try:
            # Execute grep with Nexus
            results = nx.grep(pattern, path, ignore_case=not case_sensitive)

            if not results:
                return f"No matches found for pattern '{pattern}' in {path}"

            # Format results into readable output
            output_lines = [f"Found {len(results)} matches for pattern '{pattern}' in {path}:\n"]

            # Group by file for better readability
            current_file = None
            for match in results[:50]:  # Limit to first 50 matches
                file_path = match.get("file", "unknown")
                line_num = match.get("line", 0)
                content = match.get("content", "").strip()

                if file_path != current_file:
                    output_lines.append(f"\n{file_path}:")
                    current_file = file_path

                output_lines.append(f"  Line {line_num}: {content}")

            if len(results) > 50:
                output_lines.append(f"\n... and {len(results) - 50} more matches")

            return "\n".join(output_lines)

        except Exception as e:
            return f"Error executing grep: {str(e)}"

    @function_tool
    async def glob_files(pattern: str, path: str = "/") -> str:
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

    @function_tool
    async def read_file(path: str, preview: bool = False) -> str:
        """Read file content.

        Use this tool to read and analyze file contents.
        Works with text files including code, documentation, and data files.
        Supports preview mode for large files.

        Args:
            path: Absolute path to the file to read (e.g., "/workspace/README.md")
            preview: If True, show only first 100 lines; if False, show entire file (default: False)

        Returns:
            File content as string, or error message if file cannot be read.

        Examples:
            - read_file("/workspace/README.md") → Read entire README file
            - read_file("/scripts/analysis.py", True) → Preview first 100 lines
            - read_file("/data/results.json") → Read JSON file
        """
        try:
            # Read file content
            content = nx.read(path)

            # Handle bytes
            if isinstance(content, bytes):
                content = content.decode("utf-8")

            # For preview mode, show first 100 lines
            if preview:
                lines = content.split("\n")
                if len(lines) > 100:
                    preview_content = "\n".join(lines[:100])
                    output = f"Preview of {path} (first 100 of {len(lines)} lines):\n\n"
                    output += preview_content
                    output += f"\n\n... ({len(lines) - 100} more lines)"
                else:
                    output = f"Content of {path} ({len(lines)} lines):\n\n"
                    output += content
            else:
                # Show full content
                output = f"Content of {path} ({len(content)} characters):\n\n"
                output += content

            return output

        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except Exception as e:
            return f"Error reading file: {str(e)}"

    @function_tool
    async def write_file(path: str, content: str) -> str:
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
            content_bytes = content.encode("utf-8") if isinstance(content, str) else content

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
