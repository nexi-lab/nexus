"""Nexus File Operation Tools for LangGraph ReAct Agent - Examples Extension.

This module extends the official Nexus tools with additional web search capabilities.

Official Nexus Tools (from nexus.tools.langgraph):
1. grep_files: Search file content using grep-style commands
2. glob_files: Find files by name pattern using glob syntax
3. read_file: Read file content using cat/less-style commands
4. write_file: Write content to Nexus filesystem
5. python: Execute Python code in Nexus-managed sandbox
6. bash: Execute bash commands in Nexus-managed sandbox
7. query_memories: Query and retrieve stored memory records

Additional Web Tools (examples only):
8. web_search: Search the web for current information
9. web_crawl: Fetch and extract web page content as markdown

These tools enable agents to interact with a remote Nexus filesystem and execute
code in isolated Nexus-managed sandboxes, allowing them to search, read, analyze, persist
data, and run code across agent runs.

Authentication:
    API key is REQUIRED via metadata.x_auth: "Bearer <token>"
    Frontend automatically passes the authenticated user's API key in request metadata.
    Each tool creates an authenticated RemoteNexusFS instance using the extracted token.
"""

import logging
import os

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

# Import official Nexus tools
from nexus.tools.langgraph.nexus_tools import get_nexus_tools as get_official_nexus_tools

logger = logging.getLogger(__name__)


def get_nexus_tools():
    """
    Create LangGraph tools that connect to Nexus server with per-request authentication.

    Extends the official Nexus tools with web search and crawl capabilities.

    Returns:
        List of LangGraph tool functions that require x_auth in metadata

    Usage:
        tools = get_nexus_tools()
        agent = create_react_agent(model=llm, tools=tools)

        # Frontend passes API key in metadata:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": "Find Python files"}]},
            metadata={"x_auth": "Bearer sk-your-api-key"}
        )
    """

    # Web Search and Fetch Tools
    @tool
    def web_search(query: str, config: RunnableConfig, max_results: int = 5) -> str:  # noqa: ARG001
        """Search the web for current information. Returns titles, URLs, snippets.

        Args:
            query: Search query
            max_results: Max results (default 5, max 20)

        Examples: web_search("Python asyncio best practices"), web_search("latest AI research 2024", max_results=10)
        """
        try:
            # Import here to avoid requiring tavily when not used
            from tavily import TavilyClient

            # Get API key from environment
            api_key = os.getenv("TAVILY_API_KEY")
            if not api_key:
                return "Error: TAVILY_API_KEY not found in environment variables"

            # Initialize Tavily client
            client = TavilyClient(api_key=api_key)

            # Perform search
            response = client.search(query=query, max_results=max_results)

            # Format results
            if not response or "results" not in response:
                return f"No results found for query: {query}"

            results = response.get("results", [])
            if not results:
                return f"No results found for query: {query}"

            output_lines = [f"Found {len(results)} results for '{query}':\n"]

            for i, result in enumerate(results, 1):
                title = result.get("title", "No title")
                url = result.get("url", "No URL")
                content = result.get("content", "No content")

                output_lines.append(f"\n{i}. {title}")
                output_lines.append(f"   URL: {url}")
                output_lines.append(
                    f"   {content[:300]}..." if len(content) > 300 else f"   {content}"
                )

            # Add answer if available
            if "answer" in response and response["answer"]:
                output_lines.insert(1, f"\nAnswer: {response['answer']}\n")

            return "\n".join(output_lines)

        except ImportError:
            return "Error: tavily-python package not installed. Run: pip install tavily-python"
        except Exception as e:
            return f"Error performing web search: {str(e)}"

    @tool
    def web_crawl(url: str, config: RunnableConfig) -> str:  # noqa: ARG001
        """Fetch and extract web page content as clean markdown with metadata.

        Args:
            url: Web page URL to fetch

        Examples: web_crawl("https://docs.python.org/3/library/asyncio.html"), web_crawl("https://github.com/example/repo")
        """
        try:
            # Import here to avoid requiring firecrawl when not used
            from firecrawl import FirecrawlApp

            # Get API key from environment
            api_key = os.getenv("FIRECRAWL_API_KEY")
            if not api_key:
                return "Error: FIRECRAWL_API_KEY not found in environment variables"

            # Initialize Firecrawl client
            app = FirecrawlApp(api_key=api_key)

            # Scrape the URL (API v4.5.0+ returns Document object with formats parameter)
            result = app.scrape(url, formats=["markdown", "html"])

            if not result:
                return f"Error: Failed to fetch content from {url}"

            # Extract content from Document object (Firecrawl v4.5.0+)
            # Result is a Document object with attributes: markdown, metadata, html, etc.
            markdown_content = getattr(result, "markdown", "")
            metadata_obj = getattr(result, "metadata", None)

            # Extract metadata fields
            metadata = {}
            if metadata_obj:
                # metadata_obj might be a dict or an object with attributes
                if isinstance(metadata_obj, dict):
                    metadata = metadata_obj
                else:
                    # Extract common metadata fields
                    for field in ["title", "description", "url", "language", "author"]:
                        value = getattr(metadata_obj, field, None)
                        if value:
                            metadata[field] = value

            if not markdown_content:
                return f"Error: No content extracted from {url}"

            # Format output
            output_lines = [f"Content from {url}:\n"]

            # Add metadata if available
            if metadata:
                title = metadata.get("title", "")
                description = metadata.get("description", "")
                if title:
                    output_lines.append(f"Title: {title}")
                if description:
                    output_lines.append(f"Description: {description}")
                output_lines.append("")

            # Check content length and return error if too large
            max_length = 30000
            if len(markdown_content) > max_length:
                return (
                    f"Error: Web page content from {url} is too large ({len(markdown_content)} characters). "
                    f"Maximum allowed is {max_length} characters. "
                    f"Consider fetching a more specific page or processing the content in smaller sections."
                )

            output_lines.append(markdown_content)

            return "\n".join(output_lines)

        except ImportError:
            return "Error: firecrawl-py package not installed. Run: pip install firecrawl-py"
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            return f"Error fetching web page: {str(e)}\n\nDetails:\n{error_details}"

    # Get official Nexus tools and add web tools
    official_tools = get_official_nexus_tools()

    # Return all tools (official + web)
    tools = official_tools + [web_search, web_crawl]

    return tools
