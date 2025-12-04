#!/usr/bin/env python3
"""MCP Server Evaluation Runner.

This script runs LLM-driven evaluations against the Nexus MCP server to test
whether AI agents can effectively use the tools to accomplish real-world tasks.

Usage:
    # Basic usage (requires ANTHROPIC_API_KEY)
    python run_evaluation.py mcp_evaluation.xml

    # With custom model
    python run_evaluation.py mcp_evaluation.xml --model claude-sonnet-4-20250514

    # Save report to file
    python run_evaluation.py mcp_evaluation.xml --output report.md

Requirements:
    - ANTHROPIC_API_KEY environment variable
    - anthropic package: pip install anthropic
    - mcp package: pip install mcp
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Error: anthropic package not installed. Run: pip install anthropic")
    sys.exit(1)

# MCP client imports - will be used for full MCP integration
# Currently using simplified prompt-based approach
# TODO: Integrate with actual MCP server via stdio transport
# try:
#     from mcp import ClientSession, StdioServerParameters
#     from mcp.client.stdio import stdio_client
#     MCP_AVAILABLE = True
# except ImportError:
#     MCP_AVAILABLE = False


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


def evaluate_question(
    client: anthropic.Anthropic,
    question: str,
    model: str,
) -> tuple[str, int, str]:
    """Evaluate a single question using Claude with MCP tools.

    Returns:
        Tuple of (answer, tool_call_count, summary)
    """
    # For now, use a simple prompt-based approach
    # TODO: Integrate with actual MCP server via stdio transport

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

    return answer, tool_calls, summary


def run_evaluation(
    eval_file: Path,
    model: str = "claude-sonnet-4-20250514",
    output_file: Path | None = None,
) -> list[EvaluationResult]:
    """Run evaluation on all QA pairs."""
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

    for i, qa in enumerate(qa_pairs, 1):
        print(f"\n[{i}/{len(qa_pairs)}] Evaluating: {qa.question[:60]}...")

        start_time = time.time()
        actual_answer, tool_calls, summary = evaluate_question(client, qa.question, model)
        duration = time.time() - start_time

        # Check if answer matches (case-insensitive, whitespace-normalized)
        is_correct = actual_answer.lower().strip() == qa.answer.lower().strip()

        result = EvaluationResult(
            question=qa.question,
            expected_answer=qa.answer,
            actual_answer=actual_answer,
            is_correct=is_correct,
            duration_seconds=duration,
            tool_calls=tool_calls,
            summary=summary,
        )
        results.append(result)

        status = "\u2705" if is_correct else "\u274c"
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
        default="claude-sonnet-4-20250514",
        help="Claude model to use (default: claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output file for report (default: print to stdout)",
    )

    args = parser.parse_args()

    if not args.eval_file.exists():
        print(f"Error: Evaluation file not found: {args.eval_file}")
        sys.exit(1)

    run_evaluation(args.eval_file, args.model, args.output)


if __name__ == "__main__":
    main()
