# Document Q&A System

**Build an AI-powered question-answering system over your documents**

⏱️ **Time:** 10 minutes | 💡 **Difficulty:** Easy

## What You'll Learn

- Set up LLM integration (Claude, GPT, or OpenRouter)
- Ask questions about documents with AI
- Query across multiple documents simultaneously
- Extract citations and track costs
- Use different search modes and models
- Build document Q&A with both Python and CLI

## Prerequisites

✅ Completed [Simple File Storage](simple-file-storage.md) tutorial
✅ LLM API key (Anthropic, OpenAI, or OpenRouter)
✅ Nexus server running

## Overview

Nexus's LLM document reading feature lets you ask natural language questions about your documents and get AI-powered answers with automatic citations. It combines:

- **Intelligent Context Retrieval**: Semantic or keyword search finds relevant sections
- **LLM Processing**: AI understands and answers your questions
- **Automatic Citations**: Sources are tracked and attributed
- **Cost Tracking**: Monitor token usage and API costs

**Architecture:**

```
┌─────────────────┐
│  Your Question  │  ← "What were the Q4 challenges?"
└────────┬────────┘
         │
         ↓
┌─────────────────┐
│  Nexus Server   │  ← Find relevant content
│  (Search)       │     (semantic/keyword search)
└────────┬────────┘
         │ Context
         ↓
┌─────────────────┐
│  LLM Provider   │  ← Claude, GPT-4, etc.
│  (Anthropic,    │     Answer question with context
│   OpenAI, etc)  │
└────────┬────────┘
         │ Answer + Citations
         ↓
┌─────────────────┐
│  Your App       │  ← Get structured response
└─────────────────┘
```

---

## Step 1: Get an LLM API Key

You'll need an API key from one of these providers:

### Option A: Anthropic Claude (Recommended)

Claude provides excellent document understanding and analysis.

**Get your key:**
1. Visit [console.anthropic.com](https://console.anthropic.com/)
2. Sign up or log in
3. Go to API Keys section
4. Create a new key
5. Copy your key (starts with `sk-ant-`)

**Set it:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Available models:**
- `claude-sonnet-4` - Balanced (recommended)
- `claude-opus-4` - Most capable
- `claude-haiku-4` - Fastest/cheapest

---

### Option B: OpenAI GPT

GPT-4 provides strong reasoning and broad knowledge.

**Get your key:**
1. Visit [platform.openai.com](https://platform.openai.com/)
2. Sign up or log in
3. Go to API Keys
4. Create new key
5. Copy your key (starts with `sk-`)

**Set it:**
```bash
export OPENAI_API_KEY="sk-..."
```

**Available models:**
- `gpt-4o` - Latest GPT-4 Optimized
- `gpt-4o-mini` - Faster/cheaper

---

### Option C: OpenRouter (100+ Models)

OpenRouter provides access to 100+ models from multiple providers.

**Get your key:**
1. Visit [openrouter.ai/keys](https://openrouter.ai/keys)
2. Sign up or log in
3. Create API key
4. Copy your key (starts with `sk-or-`)

**Set it:**
```bash
export OPENROUTER_API_KEY="sk-or-..."
```

**Popular models:**
- `anthropic/claude-sonnet-4.5` - Latest Claude (recommended)
- `anthropic/claude-haiku-4.5` - Fast Claude
- `openrouter/google/gemini-pro-1.5` - Google Gemini

See all models: [openrouter.ai/models](https://openrouter.ai/models)

---

## Step 2: Create Sample Documents

First, let's create some sample documents to query. Make sure your Nexus server is running and you have admin credentials loaded:

```bash
# If not already running
# ./scripts/init-nexus-with-auth.sh
# source .nexus-admin-env

# Create sample documentation
cat > /tmp/auth-guide.md << 'EOF'
# Authentication Guide

## JWT Token System

Our system uses JWT (JSON Web Tokens) for authentication:
- Access tokens expire in 15 minutes
- Refresh tokens expire in 7 days
- Uses RS256 algorithm for signing
- Automatic token rotation on refresh

## Security Best Practices

1. Always use HTTPS in production
2. Store tokens in httpOnly cookies
3. Implement CSRF protection
4. Use rate limiting on auth endpoints
5. Rotate secrets regularly

## Common Issues

- **"Token expired"**: Use the refresh token endpoint to get new tokens
- **"Invalid signature"**: Check that secret key is configured correctly
- **"Missing authorization header"**: Include Bearer token in requests
EOF

# Upload to Nexus
nexus write /workspace/docs/authentication.md --input /tmp/auth-guide.md

# Create Q4 report
cat > /tmp/q4-report.txt << 'EOF'
Q4 2024 Executive Summary

ACHIEVEMENTS:
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
EOF

nexus write /workspace/reports/q4-2024.txt --input /tmp/q4-report.txt
```

You should see:
```
✓ File written to /workspace/docs/authentication.md
✓ File written to /workspace/reports/q4-2024.txt
```

---

## Step 3: Ask Your First Question (CLI)

Now let's ask a question about a document using the CLI:

```bash
nexus llm read /workspace/docs/authentication.md \
  "What are the security best practices mentioned?" \
  --model claude-sonnet-4 \
  --max-tokens 300
```

**Expected output:**
```
The security best practices mentioned are:

1. Always use HTTPS in production
2. Store tokens in httpOnly cookies
3. Implement CSRF protection
4. Use rate limiting on auth endpoints
5. Rotate secrets regularly

These practices help protect the JWT authentication system from common
security vulnerabilities.
```

🎉 **Congratulations!** You just asked your first AI-powered question about a document.

---

## Step 4: Query Multiple Documents

You can ask questions across multiple documents using glob patterns:

```bash
nexus llm read "/workspace/**/*.{md,txt}" \
  "What were the main challenges mentioned in the reports?" \
  --model claude-sonnet-4 \
  --max-tokens 500
```

**Output:**
```
The main challenges mentioned in Q4 2024 were:

1. Database performance degradation during peak hours
2. Customer churn increased to 3.2% (from 2.1%)
3. Mobile app crash rate at 1.8% (target was <1%)
4. Support ticket resolution time of 18 hours (SLA is 12 hours)

These challenges are being addressed in Q1 2025 with database read
replicas, customer retention programs, mobile app stability sprints,
and support team expansion.
```

The LLM automatically found and analyzed the relevant document (q4-2024.txt) to answer your question.

---

## Step 5: Get Detailed Results with Citations

Use `--detailed` to see sources and cost information:

```bash
nexus llm read /workspace/reports/q4-2024.txt \
  "What were the Q4 achievements and key metrics?" \
  --model claude-sonnet-4 \
  --max-tokens 600 \
  --detailed
```

**Output:**
```
Q4 2024 showed strong performance with several key achievements:

Achievements:
- Revenue grew 42% to $5.8M
- User base increased to 52,000 (+31% QoQ)
- API uptime: 99.95% (exceeded the 99.9% SLA)
- Successfully launched mobile app with 15K downloads

Key Metrics:
- Monthly Recurring Revenue: $1.9M
- Customer Acquisition Cost: $450
- Customer Lifetime Value: $3,200
- Net Promoter Score: 42

Sources:
  • /workspace/reports/q4-2024.txt

Cost: $0.0045
Tokens: 850
```

The `--detailed` flag shows:
- Complete answer
- Source files used
- API cost in USD
- Token usage

---

## Step 6: Python SDK Usage

Here's how to use the same features with the Python SDK:

```python
# document_qa_demo.py
import asyncio
from nexus import connect

async def main():
    # Connect to Nexus (uses NEXUS_URL and NEXUS_API_KEY from environment)
    async with connect() as nx:

        # Simple question
        print("=== Simple Question ===\n")
        answer = await nx.llm_read(
            path="/workspace/docs/authentication.md",
            prompt="What security best practices are mentioned?",
            model="claude-sonnet-4",
            max_tokens=300
        )
        print(answer)
        print()

        # Detailed results with citations
        print("=== Detailed Results ===\n")
        result = await nx.llm_read_detailed(
            path="/workspace/reports/q4-2024.txt",
            prompt="What were the Q4 achievements?",
            model="claude-sonnet-4",
            max_tokens=500
        )

        print(result.answer)
        print(f"\nSources: {len(result.sources)}")
        for source in result.sources:
            print(f"  • {source}")
        print(f"\nCost: ${result.cost:.4f}")
        print(f"Tokens: {result.tokens_used:,}")

if __name__ == "__main__":
    asyncio.run(main())
```

**Run it:**

```bash
python document_qa_demo.py
```

**Expected output:**
```
=== Simple Question ===

The security best practices mentioned are:
1. Always use HTTPS in production
2. Store tokens in httpOnly cookies
3. Implement CSRF protection
4. Use rate limiting on auth endpoints
5. Rotate secrets regularly

=== Detailed Results ===

Q4 2024 achievements included:
- Revenue grew 42% to $5.8M
- User base increased to 52,000 (+31% QoQ)
- API uptime: 99.95% (exceeded 99.9% SLA)
- Launched mobile app with 15K downloads

Sources: 1
  • /workspace/reports/q4-2024.txt

Cost: $0.0042
Tokens: 782
```

---

## Step 7: Streaming Responses

For long answers, you can stream the response in real-time:

### CLI:
```bash
nexus llm read /workspace/reports/q4-2024.txt \
  "Provide a comprehensive analysis of Q4 performance" \
  --model claude-sonnet-4 \
  --max-tokens 800 \
  --stream
```

The response will appear word-by-word as it's generated.

### Python:
```python
async def stream_demo():
    async with connect() as nx:
        print("Analyzing Q4 performance...\n")

        async for chunk in nx.llm_read_stream(
            path="/workspace/reports/q4-2024.txt",
            prompt="Provide a comprehensive analysis of Q4 performance",
            model="claude-sonnet-4",
            max_tokens=800
        ):
            print(chunk, end="", flush=True)

        print("\n")

asyncio.run(stream_demo())
```

---

## Advanced Features

### Different Search Modes

By default, Nexus uses **semantic search** to find relevant content. You can change this:

```bash
# Semantic search (default) - best for conceptual questions
nexus llm read /workspace/docs/*.md \
  "How does authentication work?" \
  --search-mode semantic

# Keyword search - best for exact terms
nexus llm read /workspace/docs/*.md \
  "JWT RS256" \
  --search-mode keyword

# Hybrid - combines both
nexus llm read /workspace/docs/*.md \
  "authentication security" \
  --search-mode hybrid

# No search - reads entire document (best for small files)
nexus llm read /workspace/docs/authentication.md \
  "Summarize this document" \
  --no-search
```

### Try Different Models

You can easily switch between models:

```bash
# Claude models (if ANTHROPIC_API_KEY set)
nexus llm read /path "question" --model claude-sonnet-4    # Balanced
nexus llm read /path "question" --model claude-opus-4      # Most capable
nexus llm read /path "question" --model claude-haiku-4     # Fast & cheap

# OpenAI models (if OPENAI_API_KEY set)
nexus llm read /path "question" --model gpt-4o             # Latest GPT-4
nexus llm read /path "question" --model gpt-4o-mini        # Cheaper

# OpenRouter models (if OPENROUTER_API_KEY set)
nexus llm read /path "question" --model anthropic/claude-sonnet-4.5
nexus llm read /path "question" --model openrouter/google/gemini-pro-1.5
```

### Custom System Prompts (Python Only)

Customize the AI's behavior:

```python
# Create custom reader for executive summaries
reader = nx.create_llm_reader(
    model="claude-sonnet-4",
    system_prompt=(
        "You are an executive assistant. Provide concise, "
        "bullet-point summaries focused on key business metrics "
        "and actionable insights. Use executive language."
    )
)

result = await reader.read(
    path="/workspace/reports/q4-2024.txt",
    prompt="Summarize Q4 performance for the executive team",
    max_tokens=400
)
print(result.answer)
```

---

## Complete Working Example

Here's a complete Python script demonstrating all features:

```python
#!/usr/bin/env python3
"""
Document Q&A System Demo
Prerequisites: Nexus server running with documents
"""
import asyncio
from nexus import connect

async def main():
    async with connect() as nx:
        print("=== Document Q&A System Demo ===\n")

        # 1. Simple question
        print("1️⃣ Simple Question")
        answer = await nx.llm_read(
            "/workspace/docs/authentication.md",
            "What are the token expiration times?",
            model="claude-sonnet-4"
        )
        print(f"   {answer}\n")

        # 2. Multi-document query
        print("2️⃣ Multi-Document Query")
        answer = await nx.llm_read(
            "/workspace/**/*.txt",
            "What were the Q4 challenges?",
            model="claude-sonnet-4"
        )
        print(f"   {answer}\n")

        # 3. Detailed results
        print("3️⃣ Detailed Results with Citations")
        result = await nx.llm_read_detailed(
            "/workspace/reports/q4-2024.txt",
            "What are the key metrics?",
            model="claude-sonnet-4"
        )
        print(f"   {result.answer}")
        print(f"   Sources: {result.sources}")
        print(f"   Cost: ${result.cost:.4f}\n")

        # 4. Stream response
        print("4️⃣ Streaming Response")
        print("   ", end="")
        async for chunk in nx.llm_read_stream(
            "/workspace/reports/q4-2024.txt",
            "Analyze Q4 performance trends",
            model="claude-sonnet-4",
            max_tokens=400
        ):
            print(chunk, end="", flush=True)
        print("\n")

        # 5. Custom reader
        print("5️⃣ Custom System Prompt")
        reader = nx.create_llm_reader(
            model="claude-sonnet-4",
            system_prompt="You are a technical writer. Be precise and concise."
        )
        result = await reader.read(
            "/workspace/docs/authentication.md",
            "Explain the JWT implementation",
            max_tokens=300
        )
        print(f"   {result.answer}\n")

        print("✨ Demo complete!")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Troubleshooting

### Issue: "No LLM API key found"

**Error:** `Missing API key for LLM provider`

**Solution:**
```bash
# Check which keys are set
echo $ANTHROPIC_API_KEY
echo $OPENAI_API_KEY
echo $OPENROUTER_API_KEY

# Set the one you have
export ANTHROPIC_API_KEY="sk-ant-..."
# Or
export OPENAI_API_KEY="sk-..."
# Or
export OPENROUTER_API_KEY="sk-or-..."
```

---

### Issue: Rate limit exceeded

**Error:** `RateLimitError: Rate limit exceeded`

**Solution:**
```bash
# Use a cheaper/faster model
nexus llm read /path "question" --model claude-haiku-4  # Cheaper

# Reduce max tokens
nexus llm read /path "question" --max-tokens 200  # Shorter answer

# Wait a moment and retry
sleep 60
nexus llm read /path "question"
```

---

### Issue: Document not found

**Error:** `No documents found matching pattern`

**Solution:**
```bash
# Check if files exist
nexus ls /workspace/docs

# Check your glob pattern
nexus llm read "/workspace/**/*.md" "question"  # Note quotes around glob

# Try absolute path
nexus llm read /workspace/docs/authentication.md "question"
```

---

### Issue: Answer quality is poor

**Solutions:**

1. **Use a better model:**
   ```bash
   # Instead of haiku (fast but simple)
   nexus llm read /path "question" --model claude-haiku-4

   # Try sonnet (balanced) or opus (most capable)
   nexus llm read /path "question" --model claude-sonnet-4
   nexus llm read /path "question" --model claude-opus-4
   ```

2. **Increase max tokens:**
   ```bash
   nexus llm read /path "question" --max-tokens 1000  # Longer answer
   ```

3. **Try different search mode:**
   ```bash
   # If semantic search isn't working well, try hybrid
   nexus llm read /path "question" --search-mode hybrid
   ```

4. **For small documents, skip search:**
   ```bash
   # Reads entire document for full context
   nexus llm read /path "question" --no-search
   ```

---

## Cost Optimization Tips

1. **Use appropriate models:**
   - `claude-haiku-4`: $0.25 per 1M input tokens (cheapest)
   - `claude-sonnet-4`: $3 per 1M input tokens (balanced)
   - `claude-opus-4`: $15 per 1M input tokens (most capable)

2. **Limit response length:**
   ```bash
   nexus llm read /path "question" --max-tokens 200  # Shorter = cheaper
   ```

3. **Use keyword search when possible:**
   ```bash
   # No embedding costs
   nexus llm read /path "exact term" --search-mode keyword
   ```

4. **Monitor costs with --detailed:**
   ```bash
   nexus llm read /path "question" --detailed  # Shows cost per query
   ```

---

## Key Concepts

### LLM Models

Nexus supports multiple LLM providers:

- **Anthropic Claude**: Best for document understanding, analysis, and reasoning
- **OpenAI GPT**: Strong general capabilities and broad knowledge
- **OpenRouter**: Access to 100+ models from different providers

### Search Modes

- **Semantic** (default): Vector-based search for conceptual understanding
- **Keyword**: Traditional text search for exact terms
- **Hybrid**: Combines both for comprehensive results
- **No search**: Reads entire document(s)

### Citations

When using `llm_read_detailed()` or `--detailed`, you get:
- Source file paths
- Relevance scores
- Chunk indices
- Token usage
- API costs

---

## What's Next?

Now that you've mastered document Q&A, explore more advanced features:

### 🔍 Recommended Next Steps

1. **Semantic Search** (15 min)
   Index documents for faster, more accurate retrieval

2. **AI Agent Memory** (15 min)
   Give your AI agents persistent memory

3. **Multi-Document Analysis** (20 min)
   Build advanced RAG applications

### 📚 Related Concepts

- [LLM Integration](../integrations/llm.md) - LLM provider setup
- [Semantic Search](../api/semantic-search.md) - Vector search details
- [LLM Document Reading API](../api/llm-document-reading.md) - Complete API reference

### 🔧 Advanced Topics

- [Custom System Prompts](../api/llm-document-reading.md#custom-system-prompt) - Customize AI behavior
- [Streaming Responses](../api/llm-document-reading.md#llm_read_stream) - Real-time output
- [Cost Management](../api/llm-document-reading.md#cost-management) - Optimize spending

---

## Summary

🎉 **You've completed the Document Q&A System tutorial!**

**What you learned:**
- ✅ Set up LLM API keys (Anthropic, OpenAI, or OpenRouter)
- ✅ Ask questions about documents with AI
- ✅ Query multiple documents simultaneously
- ✅ Extract citations and track costs
- ✅ Use different search modes and models
- ✅ Build Q&A systems with Python and CLI

**Time to build:** You're ready to add AI-powered document understanding to your applications!

---

**Next:** [AI Agent Memory →](ai-agent-memory.md)

**Questions?** Check the [LLM Document Reading API](../api/llm-document-reading.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
