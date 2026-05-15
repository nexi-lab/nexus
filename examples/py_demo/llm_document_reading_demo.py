#!/usr/bin/env python3
"""Nexus Python SDK - LLM Document Reading Demo

This demo showcases LLM-powered document reading with Nexus:
- Simple document Q&A
- Multi-document analysis
- Citation extraction
- Streaming responses
- Custom system prompts
- Cost tracking

Prerequisites:
1. Install Nexus: pip install nexus-ai-fs
2. Set API key: export ANTHROPIC_API_KEY=your-key
3. Optional: For semantic search: pip install nexus-ai-fs[semantic-search-remote]

Usage:
    python examples/py_demo/llm_document_reading_demo.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from nexus import connect


async def main():
    """Run LLM document reading demo."""
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   Nexus Python SDK - LLM Document Reading Demo          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # Check API key
    api_key = (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    if not api_key:
        print("❌ No LLM API key found. Set one of:")
        print("  - ANTHROPIC_API_KEY (for Claude)")
        print("  - OPENAI_API_KEY (for GPT models)")
        print("  - OPENROUTER_API_KEY (for OpenRouter)")
        print()
        print("Get API keys from:")
        print("  - Anthropic: https://console.anthropic.com/")
        print("  - OpenAI: https://platform.openai.com/")
        print("  - OpenRouter: https://openrouter.ai/keys")
        return

    # Determine model
    if os.getenv("OPENROUTER_API_KEY"):
        # OpenRouter: use any model available on their platform (defaults to Claude Sonnet 4.5)
        model = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
        # Don't set environment variables - let the code handle OpenRouter properly
        print(f"ℹ️  Using OpenRouter: {model}")
    elif os.getenv("ANTHROPIC_API_KEY"):
        model = "claude-3-5-sonnet-20241022"
        print(f"ℹ️  Using Anthropic Claude: {model}")
    else:
        model = "gpt-4o"
        print(f"ℹ️  Using OpenAI GPT: {model}")

    print()

    # Connect to Nexus
    async with connect() as nx:
        demo_base = "/workspace/llm-python-demo"

        # ===================================================================
        # Part 1: Setup Demo Documents
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Part 1: Setup Demo Documents")
        print("════════════════════════════════════════════════════════════")
        print()

        # Create directories
        nx.mkdir(f"{demo_base}/docs", parents=True)
        nx.mkdir(f"{demo_base}/reports", parents=True)

        # Create sample documentation
        auth_doc = """# Authentication System

## JWT Token Implementation

Our system uses JWT (JSON Web Tokens) for authentication with these features:
- Access tokens expire in 15 minutes
- Refresh tokens expire in 7 days
- RS256 algorithm for signing
- Automatic token rotation on refresh

## Security Best Practices
1. Always use HTTPS in production
2. Store tokens in httpOnly cookies
3. Implement CSRF protection
4. Use rate limiting on auth endpoints
5. Rotate secrets regularly

## Common Issues
- "Token expired" → Use refresh token endpoint
- "Invalid signature" → Check secret key configuration
- "Missing authorization header" → Include Bearer token
"""

        nx.sys_write(f"{demo_base}/docs/authentication.md", auth_doc)
        print("✓ Created authentication.md")

        # Create API documentation
        api_doc = """# REST API Reference

## User Endpoints

### GET /api/users
List all users
- Auth: Required
- Rate limit: 100/min
- Returns: Array of user objects

### POST /api/users
Create new user
- Auth: Admin only
- Body: { email, password, name }
- Returns: Created user object

### PUT /api/users/:id
Update user
- Auth: Required (self or admin)
- Body: Partial user object
- Returns: Updated user

### DELETE /api/users/:id
Delete user
- Auth: Admin only
- Returns: 204 No Content

## File Endpoints

### POST /api/files/upload
Upload file
- Auth: Required
- Max size: 100MB
- Content-Type: multipart/form-data

### GET /api/files/:path
Download file
- Auth: Required
- Returns: File content
"""

        nx.sys_write(f"{demo_base}/docs/api-reference.md", api_doc)
        print("✓ Created api-reference.md")

        # Create quarterly report
        q4_report = """Q4 2024 Performance Report

HIGHLIGHTS:
✓ Revenue grew 42% to $5.8M
✓ User base increased to 52,000 (+31% QoQ)
✓ API uptime: 99.95% (exceeded 99.9% SLA)
✓ Launched mobile app with 15K downloads

CHALLENGES:
⚠ Database performance degradation during peak hours
⚠ Customer churn increased to 3.2% (from 2.1%)
⚠ Mobile app crash rate: 1.8% (target: <1%)
⚠ Support ticket resolution time: 18 hours (SLA: 12 hours)

KEY METRICS:
- Monthly Recurring Revenue: $1.9M
- Customer Acquisition Cost: $450
- Customer Lifetime Value: $3,200
- Net Promoter Score: 42

ACTION ITEMS FOR Q1 2025:
1. Implement database read replicas
2. Launch customer retention program
3. Mobile app stability sprint
4. Expand support team by 40%
"""

        nx.sys_write(f"{demo_base}/reports/q4-2024.txt", q4_report)
        print("✓ Created q4-2024.txt")

        print()
        print("✅ Demo documents created!")
        print()

        # ===================================================================
        # Part 2: Simple Document Reading
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Part 2: Simple Document Reading")
        print("════════════════════════════════════════════════════════════")
        print()

        print("📄 Question: What security best practices are mentioned?")
        print()

        answer = await nx.llm_read(
            path=f"{demo_base}/docs/authentication.md",
            prompt="What are the security best practices mentioned? List them.",
            model=model,
            max_tokens=300,
        )

        print("💬 Answer:")
        print(answer)
        print()

        # ===================================================================
        # Part 3: Multi-Document Analysis with Citations
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Part 3: Multi-Document Analysis with Citations")
        print("════════════════════════════════════════════════════════════")
        print()

        print("📚 Question: What API endpoints are available and what are their rate limits?")
        print()

        result = await nx.llm_read_detailed(
            path=f"{demo_base}/docs/**/*.md",
            prompt="What API endpoints are available? Include their rate limits.",
            model=model,
            max_tokens=500,
        )

        print("💬 Answer:")
        print(result.answer)
        print()

        # Show citations
        if result.citations:
            print(f"📖 Sources ({len(result.citations)}):")
            seen_paths = set()
            for citation in result.citations:
                if citation.path not in seen_paths:
                    seen_paths.add(citation.path)
                    score_str = f" (relevance: {citation.score:.2f})" if citation.score else ""
                    print(f"  • {citation.path}{score_str}")
            print()

        # Show cost
        if result.cost:
            print(f"💰 Cost: ${result.cost:.4f}")
        if result.tokens_used:
            print(f"🎯 Tokens: {result.tokens_used:,}")
        print()

        # ===================================================================
        # Part 4: Streaming Response
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Part 4: Streaming Response")
        print("════════════════════════════════════════════════════════════")
        print()

        print("📊 Question: What were the Q4 challenges and action items?")
        print()
        print("💬 Answer (streaming):")

        async for chunk in nx.llm_read_stream(
            path=f"{demo_base}/reports/q4-2024.txt",
            prompt="What were the main challenges in Q4 and what action items are planned for Q1?",
            model=model,
            max_tokens=400,
        ):
            print(chunk, end="", flush=True)

        print("\n")

        # ===================================================================
        # Part 5: Advanced - Custom Reader
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Part 5: Advanced - Custom Reader with System Prompt")
        print("════════════════════════════════════════════════════════════")
        print()

        print("🎭 Using custom system prompt for executive summary style")
        print()

        # Create custom reader with specific instructions
        reader = nx.create_llm_reader(
            model=model,
            system_prompt=(
                "You are an executive assistant. Provide concise, "
                "bullet-point summaries focused on key business metrics and actionable insights. "
                "Use executive language and avoid technical jargon."
            ),
        )

        result = await reader.read(
            path=f"{demo_base}/reports/q4-2024.txt",
            prompt="Summarize Q4 performance for the executive team",
            max_tokens=400,
        )

        print("💬 Executive Summary:")
        print(result.answer)
        print()

        # ===================================================================
        # Part 6: Comprehensive Analysis
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Part 6: Comprehensive Analysis Across All Documents")
        print("════════════════════════════════════════════════════════════")
        print()

        print("🔍 Question: What are the overall system capabilities and current priorities?")
        print()

        result = await nx.llm_read_detailed(
            path=f"{demo_base}/**/*.{{md,txt}}",
            prompt=(
                "Based on all the documentation and reports, what are the key capabilities "
                "of this system and what are the current business priorities?"
            ),
            model=model,
            max_tokens=800,
        )

        print("💬 Comprehensive Analysis:")
        print(result.answer)
        print()

        print(f"📊 Analyzed {len(result.sources)} documents")
        if result.cost:
            print(f"💰 Total cost: ${result.cost:.4f}")
        print()

        # ===================================================================
        # Cleanup
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Cleanup")
        print("════════════════════════════════════════════════════════════")
        print()

        await nx.sys_rmdir(demo_base, recursive=True, force=True)
        print("✓ Cleaned up demo files")
        print()

        # ===================================================================
        # Summary
        # ===================================================================
        print("════════════════════════════════════════════════════════════")
        print("  Demo Complete! 🎉")
        print("════════════════════════════════════════════════════════════")
        print()

        print("You've learned how to:")
        print("  ✓ Ask questions about documents with nx.llm_read()")
        print("  ✓ Get detailed results with citations using nx.llm_read_detailed()")
        print("  ✓ Stream responses with nx.llm_read_stream()")
        print("  ✓ Create custom readers with system prompts")
        print("  ✓ Analyze multiple documents with glob patterns")
        print("  ✓ Track costs and token usage")
        print()

        print("Next steps:")
        print("  • Index documents for semantic search")
        print("  • Try different models (GPT-4, Claude Opus, etc.)")
        print("  • Use with remote Nexus server")
        print("  • Build custom RAG applications")
        print()


if __name__ == "__main__":
    asyncio.run(main())
