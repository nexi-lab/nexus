#!/bin/bash
# Nexus CLI - LLM-Powered Document Reading Demo
#
# This demo showcases the FULL capability of Nexus LLM document reading:
# - Single document question answering
# - Multi-document querying with glob patterns
# - Semantic search integration for context retrieval
# - Citation extraction and source attribution
# - Streaming responses for real-time output
# - Different search modes (semantic, keyword, hybrid)
# - Multiple LLM models (Claude, GPT-4, OpenRouter, etc.)
# - Cost tracking and token usage
# - Remote server integration
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
# 3. LLM API key: export ANTHROPIC_API_KEY=your-key (or OPENAI_API_KEY or OPENROUTER_API_KEY)
#
# Usage:
#   ./examples/cli/llm_document_reading_demo.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

print_section() {
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "════════════════════════════════════════════════════════════"
    echo ""
}

print_subsection() {
    echo ""
    echo "─── $1 ───"
    echo ""
}

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_test() { echo -e "${MAGENTA}TEST:${NC} $1"; }

# Check prerequisites - Server must be running
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    echo ""
    echo "Setup steps:"
    echo "  1. Start server: ./scripts/init-nexus-with-auth.sh"
    echo "  2. Load credentials: source .nexus-admin-env"
    echo "  3. Run this demo: $0"
    echo ""
    exit 1
fi

# Check LLM API key
if [ -z "$ANTHROPIC_API_KEY" ] && [ -z "$OPENAI_API_KEY" ] && [ -z "$OPENROUTER_API_KEY" ]; then
    print_error "No LLM API key found. Set one of:"
    echo "  - ANTHROPIC_API_KEY (for Claude)"
    echo "  - OPENAI_API_KEY (for GPT models)"
    echo "  - OPENROUTER_API_KEY (for OpenRouter)"
    echo ""
    echo "Get API keys from:"
    echo "  - Anthropic: https://console.anthropic.com/"
    echo "  - OpenAI: https://platform.openai.com/"
    echo "  - OpenRouter: https://openrouter.ai/keys"
    echo ""
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus CLI - LLM Document Reading Demo                 ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"

if [ -n "$OPENROUTER_API_KEY" ]; then
    print_info "LLM Provider: OpenRouter"
    # Set the model you want to use with OpenRouter (defaults to Claude Sonnet 4.5)
    DEFAULT_MODEL="${OPENROUTER_MODEL:-anthropic/claude-sonnet-4.5}"
    # Don't set OPENAI_API_KEY - let the code handle OpenRouter properly
    print_info "Model: $DEFAULT_MODEL"
elif [ -n "$ANTHROPIC_API_KEY" ]; then
    print_info "LLM Provider: Anthropic Claude"
    DEFAULT_MODEL="claude-3-5-sonnet-20241022"
elif [ -n "$OPENAI_API_KEY" ]; then
    print_info "LLM Provider: OpenAI GPT"
    DEFAULT_MODEL="gpt-4o"
fi

echo ""

ADMIN_KEY="$NEXUS_API_KEY"
DEMO_BASE="/workspace/llm-demo"

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    print_subsection "Cleanup"
    nexus rmdir -r -f $DEMO_BASE 2>/dev/null || true
    rm -f /tmp/llm-demo-*.txt
    print_success "Cleaned up demo files"
}

# Gate cleanup behind KEEP flag for post-mortem inspection
if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_warning "Cleanup disabled (KEEP=1). Manual cleanup required."
fi

# ============================================================================
# PART 1: Setup Demo Documents
# ============================================================================

print_section "Part 1: Setup Demo Documents"

print_subsection "Creating demo directories"
# Create base directory first
nexus mkdir $DEMO_BASE --parents

# Grant admin FULL permissions (owner + editor for read/write)
nexus rebac create user admin direct_owner file $DEMO_BASE 2>/dev/null || true
nexus rebac create user admin direct_editor file $DEMO_BASE 2>/dev/null || true
print_info "Admin has full permissions on $DEMO_BASE"

# Create subdirectories
nexus mkdir $DEMO_BASE/docs --parents
nexus mkdir $DEMO_BASE/reports --parents
print_success "Created directories"

print_subsection "Creating sample documents"

# Create a technical documentation file
cat > /tmp/llm-demo-auth.md << 'EOF'
# Authentication System

## Overview
Our authentication system uses JWT tokens with refresh token rotation for security.

## Components

### 1. Token Generation
- Access tokens: Valid for 15 minutes
- Refresh tokens: Valid for 7 days
- Signed with RS256 algorithm

### 2. Authentication Flow
1. User submits credentials
2. Server validates against database
3. Generate access + refresh tokens
4. Return tokens to client
5. Client stores in secure storage

### 3. Token Refresh
When access token expires:
1. Client sends refresh token
2. Server validates refresh token
3. Generate new access token
4. Rotate refresh token
5. Return new tokens

## Security Features
- HTTPS only
- HttpOnly cookies
- CSRF protection
- Rate limiting on auth endpoints
- Automatic token rotation
EOF

nexus write $DEMO_BASE/docs/authentication.md --input /tmp/llm-demo-auth.md
nexus rebac create user admin direct_editor file $DEMO_BASE/docs/authentication.md 2>/dev/null || true
print_success "Created authentication.md"

# Create an API documentation file
cat > /tmp/llm-demo-api.md << 'EOF'
# API Documentation

## REST Endpoints

### User Management

**POST /api/users**
Create a new user
- Body: `{ "email": "...", "password": "...", "name": "..." }`
- Returns: User object with ID

**GET /api/users/:id**
Get user by ID
- Returns: User object

**PUT /api/users/:id**
Update user
- Body: Partial user object
- Returns: Updated user

**DELETE /api/users/:id**
Delete user
- Returns: 204 No Content

### File Operations

**POST /api/files/upload**
Upload a file
- Body: multipart/form-data
- Returns: File metadata with path

**GET /api/files/:path**
Get file content
- Returns: File content with content-type

**DELETE /api/files/:path**
Delete file
- Returns: 204 No Content

## Rate Limits
- 100 requests per minute per IP
- 1000 requests per hour per user
- Burst: 20 requests allowed
EOF

nexus write $DEMO_BASE/docs/api.md --input /tmp/llm-demo-api.md
nexus rebac create user admin direct_editor file $DEMO_BASE/docs/api.md 2>/dev/null || true
print_success "Created api.md"

# Create a quarterly report
cat > /tmp/llm-demo-q4.txt << 'EOF'
Q4 2024 Executive Summary

ACHIEVEMENTS:
1. Launched new authentication system with 99.9% uptime
2. Reduced API response time by 45%
3. Onboarded 1,200 new enterprise customers
4. Achieved SOC 2 Type II compliance

CHALLENGES:
1. Database scaling issues during peak traffic (Nov 15-20)
2. Mobile app crash rate increased to 2.3%
3. Customer support response time exceeded SLA by 15%

METRICS:
- Revenue: $4.2M (↑ 35% YoY)
- Active Users: 45,000 (↑ 28% QoQ)
- Customer Satisfaction: 4.2/5.0 (↓ 0.3)
- API Availability: 99.92%

PRIORITIES FOR Q1 2025:
1. Database horizontal scaling implementation
2. Mobile app stability improvements
3. Customer support team expansion
4. Security audit and penetration testing
EOF

nexus write $DEMO_BASE/reports/q4-2024.txt --input /tmp/llm-demo-q4.txt
nexus rebac create user admin direct_editor file $DEMO_BASE/reports/q4-2024.txt 2>/dev/null || true
print_success "Created q4-2024.txt"

# Create a bug report
cat > /tmp/llm-demo-bug.txt << 'EOF'
BUG REPORT #2847

Title: Memory leak in file upload handler

Severity: High
Status: In Progress
Assigned: Engineering Team

DESCRIPTION:
Memory usage increases continuously when uploading large files (>100MB).
After 50 uploads, server memory reaches 95% and triggers OOM killer.

REPRODUCTION STEPS:
1. Upload file >100MB via /api/files/upload
2. Repeat 50 times
3. Monitor memory with htop
4. Observe memory not being released

ROOT CAUSE:
File buffer not properly released after upload completion.
Temporary files not cleaned up in error cases.

FIX IMPLEMENTED:
- Added explicit buffer cleanup in finally block
- Implemented automatic temp file cleanup cron job
- Added memory monitoring alerts at 80% threshold

TESTING:
- Uploaded 100 files of 150MB each
- Memory usage remained stable at 45%
- No memory leaks detected
EOF

nexus write $DEMO_BASE/reports/bug-2847.txt --input /tmp/llm-demo-bug.txt
nexus rebac create user admin direct_editor file $DEMO_BASE/reports/bug-2847.txt 2>/dev/null || true
print_success "Created bug-2847.txt"

print_success "All demo documents created!"

# ============================================================================
# PART 2: Basic Document Reading
# ============================================================================

print_section "Part 2: Basic Document Reading"

print_subsection "Example 1: Ask a question about single document"
print_test "Question: What authentication flow is described in the docs?"

nexus llm read $DEMO_BASE/docs/authentication.md \
    "What is the authentication flow? List the steps." \
    --model $DEFAULT_MODEL \
    --max-tokens 500 \


print_success "Retrieved answer about authentication flow"

# ============================================================================
# PART 3: Multi-Document Querying
# ============================================================================

print_section "Part 3: Multi-Document Querying"

print_subsection "Example 2: Query across multiple documents"
print_test "Question: What API endpoints are available?"

nexus llm read "$DEMO_BASE/docs/**/*.md" \
    "What API endpoints are available? List them with HTTP methods." \
    --model $DEFAULT_MODEL \
    --max-tokens 600 \


print_success "Retrieved answer from multiple documents"

# ============================================================================
# PART 4: Detailed Output with Citations
# ============================================================================

print_section "Part 4: Detailed Output with Citations"

print_subsection "Example 3: Get detailed output with sources"
print_test "Question: What were the Q4 challenges?"

nexus llm read $DEMO_BASE/reports/q4-2024.txt \
    "What were the main challenges in Q4 2024?" \
    --model $DEFAULT_MODEL \
    --max-tokens 400 \
    --detailed \


print_success "Retrieved answer with citations and cost information"

# ============================================================================
# PART 5: Streaming Responses
# ============================================================================

print_section "Part 5: Streaming Responses"

print_subsection "Example 4: Stream response for real-time output"
print_test "Question: Summarize the bug report"

nexus llm read $DEMO_BASE/reports/bug-2847.txt \
    "Summarize this bug report: what was the issue, what was the fix?" \
    --model $DEFAULT_MODEL \
    --max-tokens 400 \
    --stream \


echo ""
print_success "Streamed response in real-time"

# ============================================================================
# PART 6: Different Search Modes
# ============================================================================

print_section "Part 6: Different Search Modes"

print_subsection "Example 5: No search (read full document)"
print_test "Reading small document without semantic search"

nexus llm read $DEMO_BASE/docs/authentication.md \
    "What security features are mentioned?" \
    --model $DEFAULT_MODEL \
    --max-tokens 300 \
    --no-search \


print_success "Read full document without semantic search"

# ============================================================================
# PART 7: Complex Analysis
# ============================================================================

print_section "Part 7: Complex Analysis"

print_subsection "Example 6: Analyze across all documents"
print_test "Question: What are the overall system capabilities and challenges?"

# Note: Use separate queries for .md and .txt files, then combine
# Or use simpler glob pattern that works across all files
nexus llm read "$DEMO_BASE/**/*" \
    "Based on all the documents, what are the key capabilities of this system and what are the main operational challenges?" \
    --model $DEFAULT_MODEL \
    --max-tokens 800 \
    --detailed \


print_success "Completed comprehensive analysis"

# ============================================================================
# PART 8: Different Models (if both API keys available)
# ============================================================================

if [ -n "$ANTHROPIC_API_KEY" ] && [ -n "$OPENAI_API_KEY" ]; then
    print_section "Part 8: Compare Different Models"

    print_subsection "Example 7a: Using Claude"
    print_test "Model: claude-sonnet-4"

    nexus llm read $DEMO_BASE/docs/api.md \
        "How many API endpoints are there for user management?" \
        --model claude-sonnet-4 \
        --max-tokens 200 \


    print_subsection "Example 7b: Using GPT-4"
    print_test "Model: gpt-4o"

    nexus llm read $DEMO_BASE/docs/api.md \
        "How many API endpoints are there for user management?" \
        --model gpt-4o \
        --max-tokens 200 \


    print_success "Compared responses from different models"
fi

# ============================================================================
# Summary
# ============================================================================

print_section "Demo Complete! 🎉"

echo "You've learned how to:"
echo ""
print_info "✓ Ask questions about single documents"
print_info "✓ Query across multiple documents with glob patterns"
print_info "✓ Get detailed output with citations and cost tracking"
print_info "✓ Stream responses for real-time output"
print_info "✓ Use different search modes (with/without semantic search)"
print_info "✓ Perform complex analysis across document collections"
print_info "✓ Use different LLM models (Claude, GPT-4, etc.)"
echo ""

print_section "Next Steps"

echo "Try these advanced scenarios:"
echo ""
echo "1. Index documents for semantic search:"
echo "   ${CYAN}nexus search index $DEMO_BASE${NC}"
echo ""
echo "2. Use hybrid search (keyword + semantic):"
echo "   ${CYAN}nexus llm read \"$DEMO_BASE/**/*.md\" \"query\" --search-mode hybrid${NC}"
echo ""
echo "3. Try different models:"
echo "   ${CYAN}nexus llm read /path \"query\" --model gpt-4o${NC}"
echo "   ${CYAN}nexus llm read /path \"query\" --model openrouter/anthropic/claude-3.5-sonnet${NC}"
echo ""
echo "4. Custom system prompts (Python SDK):"
echo "   ${CYAN}reader = nx.create_llm_reader(system_prompt=\"You are a...\")${NC}"
echo ""

print_info "Demo completed successfully!"
