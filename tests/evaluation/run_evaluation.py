#!/usr/bin/env python3
"""MCP Server Evaluation Runner.

This script runs LLM-driven evaluations against the Nexus MCP server to test
whether AI agents can effectively use the tools to accomplish real-world tasks.

Usage:
    # Basic usage with Docker MCP server (requires ANTHROPIC_API_KEY)
    python run_evaluation.py mcp_evaluation.xml

    # Use local stdio transport
    python run_evaluation.py mcp_evaluation.xml --transport stdio

    # Use HTTP transport with custom URL
    python run_evaluation.py mcp_evaluation.xml --transport http --mcp-url http://remote:8081/mcp

    # With custom model
    python run_evaluation.py mcp_evaluation.xml --model claude-opus-4-5-20251101

    # Save report to file
    python run_evaluation.py mcp_evaluation.xml --output report.md

Requirements:
    - ANTHROPIC_API_KEY environment variable
    - anthropic package: pip install anthropic
    - mcp package: pip install mcp
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# Disable proxy for httpx (used by MCP client)
# See: https://github.com/modelcontextprotocol/python-sdk/issues/1299
os.environ["NO_PROXY"] = "*"
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""

try:
    import anthropic
except ImportError:
    print("Error: anthropic package not installed. Run: pip install anthropic")
    sys.exit(1)

# MCP client imports for full MCP integration
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    print("Warning: mcp package not installed. Falling back to prompt-based evaluation.")
    print("For full MCP integration, run: pip install mcp")


@dataclass
class QAPair:
    """A question-answer pair for evaluation."""

    question: str
    answer: str


@dataclass
class EvaluationResult:
    """Result of evaluating a single question."""

    question: str
    expected_answer: str
    actual_answer: str
    is_correct: bool
    duration_seconds: float
    tool_calls: int
    summary: str
    feedback: str = ""


EVALUATION_PROMPT = """You are an AI assistant with access to tools.

When given a task, you MUST:
1. Use the available tools to complete the task
2. Provide summary of each step in your approach, wrapped in <summary> tags
3. Provide feedback on the tools provided, wrapped in <feedback> tags
4. Provide your final response, wrapped in <response> tags

Summary Requirements:
- In your <summary> tags, you must explain:
  - The steps you took to complete the task
  - Which tools you used, in what order, and why
  - The inputs you provided to each tool
  - The outputs you received from each tool
  - A summary for how you arrived at the response

Feedback Requirements:
- In your <feedback> tags, provide constructive feedback on the tools:
  - Comment on tool names: Are they clear and descriptive?
  - Comment on input parameters: Are they well-documented? Are required vs optional parameters clear?
  - Comment on descriptions: Do they accurately describe what the tool does?
  - Comment on any errors encountered during tool usage
  - Identify specific areas for improvement and explain WHY they would help
  - Be specific and actionable in your suggestions

Response Requirements:
- Your response should be concise and directly address what was asked
- Always wrap your final response in <response> tags
- If you cannot solve the task return <response>NOT_FOUND</response>
- For numeric responses, provide just the number
- For IDs, provide just the ID
- For names or text, provide the exact text requested
- Your response should go last"""


def extract_xml_content(text: str, tag: str) -> str | None:
    """Extract content from XML tags."""
    pattern = rf"<{tag}>(.*?)</{tag}>"
    matches = re.findall(pattern, text, re.DOTALL)
    return matches[-1].strip() if matches else None


def load_evaluation_file(path: Path) -> list[QAPair]:
    """Load QA pairs from an evaluation XML file."""
    tree = ET.parse(path)
    root = tree.getroot()

    qa_pairs = []
    for qa_elem in root.findall("qa_pair"):
        question_elem = qa_elem.find("question")
        answer_elem = qa_elem.find("answer")

        if question_elem is not None and answer_elem is not None:
            question = question_elem.text.strip() if question_elem.text else ""
            answer = answer_elem.text.strip() if answer_elem.text else ""
            if question and answer:
                qa_pairs.append(QAPair(question=question, answer=answer))

    return qa_pairs


# ============================================================
# Test Data Setup Functions
# ============================================================

# Path to local test data files
EVAL_WORKSPACE_LOCAL = Path(__file__).parent / "eval_workspace"
# Remote path in Nexus
EVAL_WORKSPACE_REMOTE = "/eval_workspace"


async def check_data_exists(session: ClientSession) -> bool:
    """Check if test data already exists in Nexus."""
    try:
        result = await session.call_tool(
            "nexus_list_files", arguments={"path": EVAL_WORKSPACE_REMOTE}
        )
        if result.content:
            text = (
                result.content[0].text
                if hasattr(result.content[0], "text")
                else str(result.content[0])
            )
            # Check if we have files (not just an empty directory or error)
            return "src" in text or "data" in text or "docs" in text
    except Exception:
        pass
    return False


async def upload_test_data(session: ClientSession) -> tuple[int, int]:
    """Upload test data files to Nexus using MCP write_file.

    Returns:
        Tuple of (success_count, failed_count)
    """
    if not EVAL_WORKSPACE_LOCAL.exists():
        print(f"Error: Local eval_workspace not found at {EVAL_WORKSPACE_LOCAL}")
        return 0, 0

    # Find all files to upload
    files_to_upload = []
    for path in EVAL_WORKSPACE_LOCAL.rglob("*"):
        if path.is_file():
            relative = path.relative_to(EVAL_WORKSPACE_LOCAL)
            remote_path = f"{EVAL_WORKSPACE_REMOTE}/{relative}"
            files_to_upload.append((path, remote_path))

    print(f"Uploading {len(files_to_upload)} test data files to Nexus...")

    success = 0
    failed = 0

    for local_path, remote_path in files_to_upload:
        try:
            content = local_path.read_text()
            result = await session.call_tool(
                "nexus_write_file",
                arguments={
                    "path": remote_path,
                    "content": content,
                },
            )

            # Check result
            if result.content:
                text = (
                    result.content[0].text
                    if hasattr(result.content[0], "text")
                    else str(result.content[0])
                )
                if "error" in text.lower() or "denied" in text.lower():
                    print(f"  FAILED: {remote_path} - {text[:100]}")
                    failed += 1
                else:
                    print(f"  OK: {remote_path}")
                    success += 1
            else:
                print(f"  OK: {remote_path}")
                success += 1

        except Exception as e:
            print(f"  FAILED: {remote_path} - {e}")
            failed += 1

    print(f"\nUpload complete: {success} success, {failed} failed")
    return success, failed


async def evaluate_question_with_mcp(
    client: anthropic.Anthropic,
    question: str,
    model: str,
    mcp_session: ClientSession,
    tools: list[dict],
) -> tuple[str, int, str, str]:
    """Evaluate a single question using real MCP tools.

    Returns:
        Tuple of (answer, tool_call_count, summary, feedback)
    """
    messages = [{"role": "user", "content": question}]
    tool_call_count = 0

    # Initial request
    response = await asyncio.to_thread(
        client.messages.create,
        model=model,
        max_tokens=4096,
        system=EVALUATION_PROMPT,
        messages=messages,
        tools=tools,
    )

    messages.append({"role": "assistant", "content": response.content})

    # Tool use loop - handle all tool_use blocks (including parallel tool calls)
    while response.stop_reason == "tool_use":
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        tool_results = []

        # Execute all tool calls
        for tool_use in tool_uses:
            tool_name = tool_use.name
            tool_input = tool_use.input

            try:
                tool_result = await mcp_session.call_tool(tool_name, arguments=tool_input)
                # Extract text from TextContent objects
                if hasattr(tool_result, "content"):
                    content_items = []
                    for item in tool_result.content:
                        if hasattr(item, "text"):
                            content_items.append(item.text)
                        else:
                            content_items.append(str(item))
                    tool_response = "\n".join(content_items) if content_items else str(tool_result)
                else:
                    tool_response = str(tool_result)
                tool_call_count += 1
            except Exception as e:
                tool_response = (
                    f"Error executing tool {tool_name}: {str(e)}\n{traceback.format_exc()}"
                )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": tool_response,
                }
            )

        # Send all tool results in a single message
        messages.append(
            {
                "role": "user",
                "content": tool_results,
            }
        )

        response = await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=4096,
            system=EVALUATION_PROMPT,
            messages=messages,
            tools=tools,
        )
        messages.append({"role": "assistant", "content": response.content})

    # Extract final response text
    response_text = next(
        (block.text for block in response.content if hasattr(block, "text")),
        None,
    )

    if not response_text:
        return "NOT_FOUND", tool_call_count, "No response text", "No feedback"

    # Parse XML tags
    answer = extract_xml_content(response_text, "response") or "NOT_FOUND"
    summary = extract_xml_content(response_text, "summary") or "No summary provided"
    feedback = extract_xml_content(response_text, "feedback") or "No feedback provided"

    return answer, tool_call_count, summary, feedback


def evaluate_question_prompt_based(
    client: anthropic.Anthropic,
    question: str,
    model: str,
) -> tuple[str, int, str, str]:
    """Evaluate a single question using prompt-based approach (fallback).

    Returns:
        Tuple of (answer, tool_call_count, summary, feedback)
    """
    system_prompt = """You are evaluating a Nexus MCP server. You have access to these tools:
- nexus_read_file: Read file content
- nexus_write_file: Write content to file
- nexus_list_files: List files in directory
- nexus_mkdir: Create directory
- nexus_delete_file: Delete file
- nexus_glob: Search files by pattern
- nexus_grep: Search file contents

Answer the question by describing what tools you would use and provide the final answer.
Format your response as:
TOOLS_USED: [list of tools]
ANSWER: [your final answer - just the value, nothing else]
SUMMARY: [brief explanation]"""

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )

    # Extract text from response (handle different content block types)
    response_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            response_text = block.text
            break

    # Parse response
    answer = ""
    summary = ""
    tool_calls = 0

    for line in response_text.split("\n"):
        if line.startswith("ANSWER:"):
            answer = line.replace("ANSWER:", "").strip()
        elif line.startswith("SUMMARY:"):
            summary = line.replace("SUMMARY:", "").strip()
        elif line.startswith("TOOLS_USED:"):
            tools_str = line.replace("TOOLS_USED:", "").strip()
            tool_calls = len([t for t in tools_str.split(",") if t.strip()])

    return answer, tool_calls, summary, "Prompt-based evaluation (no feedback)"


async def run_evaluation_async(
    eval_file: Path,
    model: str = "claude-opus-4-5-20251101",
    output_file: Path | None = None,
    transport: str = "http",
    mcp_url: str = "http://localhost:8081/mcp",
    setup_data: bool = False,
) -> list[EvaluationResult]:
    """Run evaluation on all QA pairs using MCP integration.

    **Testing Principle**: This evaluation is designed to test the local Docker
    Nexus MCP server. The MCP URL is constructed from environment variables to
    ensure consistency across all environments (local dev, CI, production).

    **Environment Variables**:
    - MCP_HOST: MCP server hostname (default: localhost)
    - MCP_PORT: MCP server port (default: 8081)
    - These match the variables used by docker-compose.yml and MCP server

    **Before running evaluations**:
    1. Start Docker containers: ./docker-demo.sh
    2. Verify MCP server: curl http://${MCP_HOST}:${MCP_PORT}/health
    3. Set NEXUS_API_KEY if authentication is enabled
    4. Override MCP_HOST/MCP_PORT for remote testing if needed

    **Example**:
        # Default (localhost:8081)
        python run_evaluation.py eval.xml

        # Custom port
        MCP_PORT=9000 python run_evaluation.py eval.xml

        # Remote server
        MCP_HOST=remote.server.com MCP_PORT=8081 python run_evaluation.py eval.xml
    """
    # Check for API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Load evaluation questions
    qa_pairs = load_evaluation_file(eval_file)
    print(f"Loaded {len(qa_pairs)} evaluation questions")

    results: list[EvaluationResult] = []

    if MCP_AVAILABLE:
        # Use real MCP integration
        print(f"Using MCP {transport} transport for evaluation...")

        try:
            if transport == "http":
                # HTTP transport - connect to remote MCP server
                # Pass NEXUS_API_KEY via HTTP headers
                nexus_api_key = os.environ.get("NEXUS_API_KEY")
                headers = {}
                if nexus_api_key:
                    headers["X-Nexus-API-Key"] = nexus_api_key

                async with (
                    streamablehttp_client(mcp_url, headers=headers) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()

                    # List available tools
                    tools_response = await session.list_tools()
                    tools = [
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                        }
                        for tool in tools_response.tools
                    ]
                    print(f"Connected to MCP server with {len(tools)} tools")

                    # Setup test data if needed
                    if setup_data:
                        print("\n--setup flag specified, uploading test data...")
                        await upload_test_data(session)
                    else:
                        # Check if data exists, upload if not
                        data_exists = await check_data_exists(session)
                        if not data_exists:
                            print("\nTest data not found in Nexus, uploading...")
                            success, failed = await upload_test_data(session)
                            if failed > 0:
                                print(f"Warning: {failed} files failed to upload")
                        else:
                            print("Test data already exists in Nexus")

                    # Evaluate each question
                    for i, qa in enumerate(qa_pairs, 1):
                        print(f"\n[{i}/{len(qa_pairs)}] Evaluating: {qa.question[:60]}...")

                        start_time = time.time()
                        (
                            actual_answer,
                            tool_calls,
                            summary,
                            feedback,
                        ) = await evaluate_question_with_mcp(
                            client, qa.question, model, session, tools
                        )
                        duration = time.time() - start_time

                        # Check if answer matches
                        is_correct = actual_answer.lower().strip() == qa.answer.lower().strip()

                        result = EvaluationResult(
                            question=qa.question,
                            expected_answer=qa.answer,
                            actual_answer=actual_answer,
                            is_correct=is_correct,
                            duration_seconds=duration,
                            tool_calls=tool_calls,
                            summary=summary,
                            feedback=feedback,
                        )
                        results.append(result)

                        status = "✅" if is_correct else "❌"
                        print(f"  {status} Expected: {qa.answer}, Got: {actual_answer}")

            elif transport == "stdio":
                # Stdio transport - connect to local MCP server
                server_params = StdioServerParameters(
                    command="nexus",
                    args=["mcp", "serve"],
                    env=None,
                )

                async with (
                    stdio_client(server_params) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()

                    # List available tools
                    tools_response = await session.list_tools()
                    tools = [
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "input_schema": tool.inputSchema,
                        }
                        for tool in tools_response.tools
                    ]
                    print(f"Connected to MCP server with {len(tools)} tools")

                    # Evaluate each question
                    for i, qa in enumerate(qa_pairs, 1):
                        print(f"\n[{i}/{len(qa_pairs)}] Evaluating: {qa.question[:60]}...")

                        start_time = time.time()
                        (
                            actual_answer,
                            tool_calls,
                            summary,
                            feedback,
                        ) = await evaluate_question_with_mcp(
                            client, qa.question, model, session, tools
                        )
                        duration = time.time() - start_time

                        # Check if answer matches
                        is_correct = actual_answer.lower().strip() == qa.answer.lower().strip()

                        result = EvaluationResult(
                            question=qa.question,
                            expected_answer=qa.answer,
                            actual_answer=actual_answer,
                            is_correct=is_correct,
                            duration_seconds=duration,
                            tool_calls=tool_calls,
                            summary=summary,
                            feedback=feedback,
                        )
                        results.append(result)

                        status = "✅" if is_correct else "❌"
                        print(f"  {status} Expected: {qa.answer}, Got: {actual_answer}")

            else:
                raise ValueError(f"Unsupported transport: {transport}")

        except Exception as e:
            print(f"Error connecting to MCP server: {e}")
            print(f"Full traceback:\n{traceback.format_exc()}")
            print("Falling back to prompt-based evaluation...")
            MCP_AVAILABLE_LOCAL = False
        else:
            MCP_AVAILABLE_LOCAL = True
    else:
        MCP_AVAILABLE_LOCAL = False

    # Fallback to prompt-based evaluation
    if not MCP_AVAILABLE_LOCAL:
        print("Using prompt-based evaluation (fallback)...")
        for i, qa in enumerate(qa_pairs, 1):
            print(f"\n[{i}/{len(qa_pairs)}] Evaluating: {qa.question[:60]}...")

            start_time = time.time()
            actual_answer, tool_calls, summary, feedback = evaluate_question_prompt_based(
                client, qa.question, model
            )
            duration = time.time() - start_time

            is_correct = actual_answer.lower().strip() == qa.answer.lower().strip()

            result = EvaluationResult(
                question=qa.question,
                expected_answer=qa.answer,
                actual_answer=actual_answer,
                is_correct=is_correct,
                duration_seconds=duration,
                tool_calls=tool_calls,
                summary=summary,
                feedback=feedback,
            )
            results.append(result)

            status = "✅" if is_correct else "❌"
            print(f"  {status} Expected: {qa.answer}, Got: {actual_answer}")

    # Generate report
    report = generate_report(results, model)

    if output_file:
        output_file.write_text(report)
        print(f"\nReport saved to: {output_file}")
    else:
        print("\n" + "=" * 60)
        print(report)

    return results


def run_evaluation(
    eval_file: Path,
    model: str = "claude-opus-4-5-20251101",
    output_file: Path | None = None,
    transport: str = "http",
    mcp_url: str = "http://localhost:8081/mcp",
    setup_data: bool = False,
) -> list[EvaluationResult]:
    """Run evaluation (synchronous wrapper)."""
    return asyncio.run(
        run_evaluation_async(eval_file, model, output_file, transport, mcp_url, setup_data)
    )


def generate_report(results: list[EvaluationResult], model: str) -> str:
    """Generate a markdown report from evaluation results."""
    correct = sum(1 for r in results if r.is_correct)
    total = len(results)
    accuracy = (correct / total * 100) if total > 0 else 0

    avg_duration = sum(r.duration_seconds for r in results) / total if total else 0
    total_tools = sum(r.tool_calls for r in results)

    lines = [
        "# Nexus MCP Server Evaluation Report",
        "",
        "## Summary",
        "",
        f"- **Model**: {model}",
        f"- **Accuracy**: {correct}/{total} ({accuracy:.1f}%)",
        f"- **Average Duration**: {avg_duration:.2f}s per question",
        f"- **Total Tool Calls**: {total_tools}",
        "",
        "## Results",
        "",
    ]

    for i, r in enumerate(results, 1):
        status = "\u2705" if r.is_correct else "\u274c"
        lines.extend(
            [
                f"### Question {i} {status}",
                "",
                f"**Question**: {r.question}",
                "",
                f"**Expected**: {r.expected_answer}",
                "",
                f"**Actual**: {r.actual_answer}",
                "",
                f"**Duration**: {r.duration_seconds:.2f}s | **Tool Calls**: {r.tool_calls}",
                "",
                f"**Summary**: {r.summary}",
                "",
                f"**Feedback**: {r.feedback}",
                "",
                "---",
                "",
            ]
        )

    return "\n".join(lines)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Run LLM-driven evaluations for Nexus MCP server")
    parser.add_argument("eval_file", type=Path, help="Path to evaluation XML file")
    parser.add_argument(
        "--model",
        "-m",
        default="claude-opus-4-5-20251101",
        help="Claude model to use (default: claude-opus-4-5-20251101)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output file for report (default: print to stdout)",
    )
    parser.add_argument(
        "--transport",
        "-t",
        choices=["http", "stdio"],
        default="http",
        help="MCP transport type: http (Docker/remote) or stdio (local)",
    )
    # Construct default MCP URL from environment variables (consistent with docker-compose.yml)
    mcp_host = os.environ.get("MCP_HOST", "localhost")
    mcp_port = os.environ.get("MCP_PORT", "8081")
    default_mcp_url = f"http://{mcp_host}:{mcp_port}/mcp"

    parser.add_argument(
        "--mcp-url",
        default=default_mcp_url,
        help=f"MCP server URL (for http transport, default: http://$MCP_HOST:$MCP_PORT/mcp or {default_mcp_url})",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Force upload test data even if it already exists",
    )

    args = parser.parse_args()

    if not args.eval_file.exists():
        print(f"Error: Evaluation file not found: {args.eval_file}")
        sys.exit(1)

    run_evaluation(
        args.eval_file, args.model, args.output, args.transport, args.mcp_url, args.setup
    )


if __name__ == "__main__":
    main()
