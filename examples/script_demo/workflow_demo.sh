#!/bin/bash
# Workflow Automation Demo - Event-Driven File Processing
#
# This script demonstrates the Nexus Workflow System:
# - Loading workflows from YAML files
# - Testing workflow execution
# - Listing and managing workflows
# - Event-driven file processing

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Nexus Workflow Automation Demo${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""

# Configuration
WORKSPACE_DIR="/tmp/nexus-workflow-demo"
DATA_DIR="$WORKSPACE_DIR/nexus-data"
WORKFLOWS_DIR="$WORKSPACE_DIR/workflows"

# Clean up from previous runs
echo -e "${YELLOW}Cleaning up previous demo data...${NC}"
rm -rf "$WORKSPACE_DIR"
mkdir -p "$WORKSPACE_DIR"
mkdir -p "$WORKFLOWS_DIR"

echo -e "${GREEN}✓ Setup complete${NC}"
echo ""

# Initialize Nexus
echo -e "${BLUE}Step 1: Initializing Nexus${NC}"
nexus init "$WORKSPACE_DIR"
echo -e "${GREEN}✓ Nexus initialized${NC}"
echo ""

# Create directories for workflow demo
echo -e "${BLUE}Step 2: Creating directories${NC}"
nexus mkdir --data-dir="$DATA_DIR" /inbox
nexus mkdir --data-dir="$DATA_DIR" /processed
nexus mkdir --data-dir="$DATA_DIR" /archive

echo -e "${GREEN}✓ Created: /inbox, /processed, /archive${NC}"
echo ""

# Create a simple workflow file
echo -e "${BLUE}Step 3: Creating workflow definition${NC}"

cat > "$WORKFLOWS_DIR/process-docs.yaml" << 'EOF'
name: process-documents
version: 1.0
description: Auto-process new documents in inbox

triggers:
  - type: file_write
    pattern: /inbox/**/*.txt

actions:
  - name: log_file
    type: python
    code: |
      print(f"📄 Processing file: {file_path}")

  - name: add_tags
    type: tag
    tags:
      - processed
      - inbox-item

  - name: log_completion
    type: python
    code: |
      print(f"✓ Tagged file: {file_path}")
EOF

echo -e "${CYAN}Created: process-docs.yaml${NC}"
echo ""

# Create another workflow for cleanup
cat > "$WORKFLOWS_DIR/auto-cleanup.yaml" << 'EOF'
name: auto-cleanup
version: 1.0
description: Clean up temporary files

triggers:
  - type: file_write
    pattern: /temp/**/*

actions:
  - name: check_file
    type: python
    code: |
      print(f"🗑️  Checking temp file: {file_path}")

  - name: tag_for_cleanup
    type: tag
    tags:
      - temporary
      - cleanup-candidate
EOF

echo -e "${CYAN}Created: auto-cleanup.yaml${NC}"
echo ""

# Load workflows
echo -e "${BLUE}Step 4: Loading workflows${NC}"

echo -e "${YELLOW}Loading process-documents workflow...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows load "$WORKFLOWS_DIR/process-docs.yaml" --enabled

echo ""
echo -e "${YELLOW}Loading auto-cleanup workflow (disabled)...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows load "$WORKFLOWS_DIR/auto-cleanup.yaml" --disabled

echo ""
echo -e "${GREEN}✓ Workflows loaded${NC}"
echo ""

# List workflows
echo -e "${BLUE}Step 5: Listing all workflows${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows list

echo ""

# Discover workflows
echo -e "${BLUE}Step 6: Discovering workflows in directory${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows discover "$WORKFLOWS_DIR"

echo ""

# Create test files
echo -e "${BLUE}Step 7: Creating test files${NC}"

nexus write --data-dir="$DATA_DIR" /inbox/document1.txt "This is the first document.
It contains some important information.
"

nexus write --data-dir="$DATA_DIR" /inbox/document2.txt "This is the second document.
It has different content.
More lines here.
"

echo -e "${GREEN}✓ Created 2 test documents in /inbox/${NC}"
echo ""

# Test workflow execution
echo -e "${BLUE}Step 8: Testing workflow execution${NC}"

echo -e "${YELLOW}Testing with document1.txt...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows test process-documents --file /inbox/document1.txt

echo ""
echo -e "${YELLOW}Testing with document2.txt...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows test process-documents --file /inbox/document2.txt

echo ""
echo -e "${GREEN}✓ Workflow tests completed${NC}"
echo ""

# Enable/disable workflows
echo -e "${BLUE}Step 9: Managing workflow state${NC}"

echo -e "${YELLOW}Disabling process-documents workflow...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows disable process-documents
echo -e "${RED}✗ process-documents is now disabled${NC}"

echo ""
echo -e "${YELLOW}Enabling auto-cleanup workflow...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows enable auto-cleanup
echo -e "${GREEN}✓ auto-cleanup is now enabled${NC}"

echo ""
echo -e "${YELLOW}Current workflow status:${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows list

echo ""

echo -e "${YELLOW}Re-enabling process-documents workflow...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows enable process-documents
echo -e "${GREEN}✓ process-documents is now enabled${NC}"

echo ""

# Create a more complex workflow example
echo -e "${BLUE}Step 10: Creating advanced workflow with multiple actions${NC}"

cat > "$WORKFLOWS_DIR/pdf-processor.yaml" << 'EOF'
name: pdf-processor
version: 1.0
description: Process PDF files with multiple steps

triggers:
  - type: file_write
    pattern: /inbox/**/*.pdf

actions:
  - name: log_start
    type: python
    code: |
      print(f"📑 Starting PDF processing: {file_path}")

  - name: extract_metadata
    type: python
    code: |
      from pathlib import Path
      filename = Path(file_path).name
      print(f"   Filename: {filename}")
      print(f"   Pattern matched: /inbox/**/*.pdf")

  - name: tag_pdf
    type: tag
    tags:
      - pdf
      - needs-review
      - imported

  - name: log_complete
    type: python
    code: |
      print(f"✓ PDF processing complete: {file_path}")
EOF

echo -e "${CYAN}Created: pdf-processor.yaml${NC}"
echo ""

echo -e "${YELLOW}Loading pdf-processor workflow...${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows load "$WORKFLOWS_DIR/pdf-processor.yaml" --enabled

echo ""
echo -e "${GREEN}✓ Advanced workflow loaded${NC}"
echo ""

# Final workflow list
echo -e "${BLUE}Step 11: Final workflow list${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows list

echo ""

# Test with context
echo -e "${BLUE}Step 12: Testing workflow with custom context${NC}"
echo -e "${YELLOW}Testing with additional JSON context...${NC}"

NEXUS_DATA_DIR="$DATA_DIR" nexus workflows test process-documents \
  --file /inbox/document1.txt \
  --context '{"priority": "high", "source": "email"}'

echo ""
echo -e "${GREEN}✓ Test with custom context completed${NC}"
echo ""

# Unload a workflow
echo -e "${BLUE}Step 13: Unloading workflow${NC}"
echo -e "${YELLOW}Unloading auto-cleanup workflow...${NC}"

NEXUS_DATA_DIR="$DATA_DIR" nexus workflows unload auto-cleanup
echo -e "${RED}✗ auto-cleanup workflow unloaded${NC}"

echo ""
echo -e "${YELLOW}Remaining workflows:${NC}"
NEXUS_DATA_DIR="$DATA_DIR" nexus workflows list

echo ""

# Summary
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}Demo Complete!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo "What we demonstrated:"
echo "  • Created workflow definitions in YAML"
echo "  • Loaded workflows (enabled and disabled)"
echo "  • Listed and discovered workflows"
echo "  • Tested workflow execution with files"
echo "  • Managed workflow state (enable/disable)"
echo "  • Tested with custom context"
echo "  • Unloaded workflows"
echo ""
echo "Workflow Features:"
echo "  • Event-driven triggers (file_write, file_delete, etc.)"
echo "  • Multiple action types (python, tag, parse, move, etc.)"
echo "  • Variable interpolation {file_path}, {filename}"
echo "  • Plugin-extensible actions"
echo "  • Persistent storage in database"
echo ""
echo "Data location: $WORKSPACE_DIR"
echo ""
echo "Try these commands yourself:"
echo "  NEXUS_DATA_DIR=$DATA_DIR nexus workflows list"
echo "  NEXUS_DATA_DIR=$DATA_DIR nexus workflows test process-documents --file /inbox/test.txt"
echo "  NEXUS_DATA_DIR=$DATA_DIR nexus workflows discover $WORKFLOWS_DIR"
echo ""
echo "Learn more:"
echo "  • Full documentation: examples/workflows/README.md"
echo "  • Example workflows: examples/workflows/*.yaml"
echo "  • Python API: examples/workflows/workflow_example.py"
echo ""
