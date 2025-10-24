#!/bin/bash
# Semantic Search Demo - Keyword, Semantic, and Hybrid Search
#
# This script demonstrates the semantic search capabilities:
# - Keyword-only search using FTS5/tsvector (no embeddings)
# - Semantic search with OpenAI embeddings
# - Hybrid search combining keyword + semantic
# - Document indexing and chunking
# - Search statistics

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo "======================================================================"
echo "Nexus Semantic Search Demo - Keyword, Semantic, and Hybrid"
echo "======================================================================"

# Create workspace directory
if [ -n "$NEXUS_DATABASE_URL" ] || [ -n "$POSTGRES_URL" ]; then
    DEMO_DIR="${HOME}/.nexus-demo-search"
    echo -e "\n${YELLOW}📊 PostgreSQL detected - using persistent data directory${NC}"
else
    DEMO_DIR=$(mktemp -d)
    echo -e "\n${BLUE}📁 Using temporary data directory${NC}"
fi

export NEXUS_DATA_DIR="$DEMO_DIR/nexus-data"
mkdir -p "$NEXUS_DATA_DIR"

echo -e "\n📁 Data directory: $NEXUS_DATA_DIR"

# Initialize
echo -e "\n${BLUE}1. Initializing Nexus workspace...${NC}"
nexus init "$NEXUS_DATA_DIR"
echo -e "${GREEN}   ✓ Initialized${NC}"

# ============================================================
# Part 1: Create Sample Documents
# ============================================================
echo -e "\n======================================================================"
echo "PART 1: Create Sample Documents"
echo "======================================================================"

echo -e "\n${BLUE}2. Creating sample documents...${NC}"

# Create Python document
cat <<'EOF' | nexus write /docs/python.md --input -
# Python Programming

Python is a high-level, interpreted programming language known for its simplicity
and readability. It supports multiple programming paradigms including procedural,
object-oriented, and functional programming.

Python is widely used in web development, data science, machine learning, and
automation. Popular frameworks include Django, Flask, NumPy, and TensorFlow.
EOF
echo -e "${GREEN}   ✓ Created /docs/python.md${NC}"

# Create JavaScript document
cat <<'EOF' | nexus write /docs/javascript.md --input -
# JavaScript Programming

JavaScript is a dynamic, interpreted programming language primarily used for
web development. It runs in browsers and enables interactive web pages.

JavaScript is essential for frontend development with frameworks like React,
Vue, and Angular. Node.js enables JavaScript on the server-side.
EOF
echo -e "${GREEN}   ✓ Created /docs/javascript.md${NC}"

# Create Database document
cat <<'EOF' | nexus write /docs/databases.md --input -
# Database Systems

Databases are organized collections of data. There are two main types:

1. SQL Databases (Relational): PostgreSQL, MySQL, SQLite
   - Use structured tables with relationships
   - ACID compliant
   - Use SQL query language

2. NoSQL Databases: MongoDB, Redis, Cassandra
   - Flexible schemas
   - Optimized for specific use cases
   - Horizontal scaling
EOF
echo -e "${GREEN}   ✓ Created /docs/databases.md${NC}"

# Create Machine Learning document
cat <<'EOF' | nexus write /docs/machine-learning.md --input -
# Machine Learning

Machine learning is a subset of artificial intelligence that enables systems
to learn and improve from experience without explicit programming.

Key concepts include:
- Supervised Learning: Training with labeled data
- Unsupervised Learning: Finding patterns in unlabeled data
- Neural Networks: Deep learning architectures
- Natural Language Processing: Understanding text and language

Popular frameworks: TensorFlow, PyTorch, scikit-learn
EOF
echo -e "${GREEN}   ✓ Created /docs/machine-learning.md${NC}"

# Create DevOps document
cat <<'EOF' | nexus write /docs/devops.md --input -
# DevOps Practices

DevOps combines software development and IT operations to shorten the development
lifecycle and deliver high-quality software continuously.

Key practices:
- Continuous Integration/Continuous Deployment (CI/CD)
- Infrastructure as Code (IaC)
- Containerization with Docker and Kubernetes
- Monitoring and logging
- Automated testing
EOF
echo -e "${GREEN}   ✓ Created /docs/devops.md${NC}"

# ============================================================
# Part 2: Keyword-Only Search (No Embeddings)
# ============================================================
echo -e "\n======================================================================"
echo "PART 2: Keyword-Only Search (No Embeddings Required)"
echo "======================================================================"

echo -e "\n${BLUE}3. Initializing search engine (keyword-only mode)...${NC}"
nexus search init
echo -e "${GREEN}   ✓ Initialized with keyword search (FTS5/tsvector)${NC}"

echo -e "\n${BLUE}4. Indexing documents...${NC}"
nexus search index /docs
echo -e "${GREEN}   ✓ Indexed documents${NC}"

echo -e "\n${BLUE}5. Performing keyword search: 'database SQL'...${NC}"
echo ""
nexus search query "database SQL" --path /docs --limit 3 --mode keyword

# ============================================================
# Part 3: Semantic Search with OpenAI (Optional)
# ============================================================
echo -e "\n======================================================================"
echo "PART 3: Semantic Search with OpenAI Embeddings (Optional)"
echo "======================================================================"

if [ -z "$OPENAI_API_KEY" ]; then
    echo -e "\n${YELLOW}⚠️  OPENAI_API_KEY not set - skipping semantic search demo${NC}"
    echo -e "${YELLOW}   To enable semantic search:${NC}"
    echo -e "${YELLOW}   1. Install: pip install nexus-ai-fs[semantic-search-remote]${NC}"
    echo -e "${YELLOW}   2. Set: export OPENAI_API_KEY=sk-...${NC}"
    echo -e "${YELLOW}   3. Re-run this demo${NC}"
else
    echo -e "\n${BLUE}6. Re-initializing with OpenAI embeddings...${NC}"
    nexus search init --provider openai --api-key "$OPENAI_API_KEY"
    echo -e "${GREEN}   ✓ Initialized with OpenAI embeddings${NC}"

    echo -e "\n${BLUE}7. Re-indexing documents with embeddings...${NC}"
    nexus search index /docs
    echo -e "${GREEN}   ✓ Indexed documents with embeddings${NC}"

    echo -e "\n${BLUE}8. Semantic search: 'AI and neural networks'...${NC}"
    echo ""
    nexus search query "AI and neural networks" --path /docs --limit 3 --mode semantic --provider openai --api-key "$OPENAI_API_KEY"

    # ============================================================
    # Part 4: Hybrid Search (Best Results)
    # ============================================================
    echo -e "\n======================================================================"
    echo "PART 4: Hybrid Search (Keyword + Semantic)"
    echo "======================================================================"

    echo -e "\n${BLUE}9. Hybrid search: 'web development frameworks'...${NC}"
    echo ""
    nexus search query "web development frameworks" --path /docs --limit 3 --mode hybrid --provider openai --api-key "$OPENAI_API_KEY"
fi

# ============================================================
# Part 5: Search Statistics
# ============================================================
echo -e "\n======================================================================"
echo "PART 5: Search Statistics"
echo "======================================================================"

echo -e "\n${BLUE}10. Getting search statistics...${NC}"
echo ""
nexus search stats

# Summary
echo -e "\n======================================================================"
echo "Demo Complete!"
echo "======================================================================"

echo -e "\n📝 Summary:"
echo -e "   ${GREEN}✓${NC} Keyword-only search works out-of-the-box (no API keys)"
echo -e "   ${GREEN}✓${NC} Semantic search requires OpenAI API key"
echo -e "   ${GREEN}✓${NC} Hybrid search combines best of both approaches"
echo -e "   ${GREEN}✓${NC} All search modes use existing database (SQLite/PostgreSQL)"

echo -e "\n💡 Next steps:"
echo "   - Try with your own documents"
echo "   - Experiment with different chunk sizes/strategies"
echo "   - Use hybrid search for best results"

# Cleanup (only if using temporary directory)
if [ -z "$NEXUS_DATABASE_URL" ] && [ -z "$POSTGRES_URL" ]; then
    echo -e "\n${BLUE}Cleaning up temporary directory...${NC}"
    rm -rf "$DEMO_DIR"
    echo -e "${GREEN}   ✓ Cleaned up${NC}"
else
    echo -e "\n${YELLOW}Note: Data persisted in $NEXUS_DATA_DIR${NC}"
    echo -e "${YELLOW}To clean up: rm -rf $NEXUS_DATA_DIR${NC}"
fi
