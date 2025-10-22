#!/bin/bash
# Script to run Nexus benchmarks with various configurations

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Nexus Performance Benchmarks ===${NC}\n"

# Default: run all benchmarks
MODE=${1:-"all"}

case "$MODE" in
  "quick")
    echo -e "${YELLOW}Running quick benchmarks (throughput only)...${NC}\n"
    pytest tests/benchmarks/test_throughput.py \
      --benchmark-only \
      --benchmark-group-by=group,param:backend_type \
      --benchmark-columns=min,max,mean,median,ops
    ;;

  "dedup")
    echo -e "${YELLOW}Running deduplication benchmarks...${NC}\n"
    pytest tests/benchmarks/test_dedup.py \
      --benchmark-only \
      --benchmark-group-by=group,param:backend_type \
      --benchmark-columns=min,max,mean,median,ops
    ;;

  "cache")
    echo -e "${YELLOW}Running cache benchmarks...${NC}\n"
    pytest tests/benchmarks/test_cache.py \
      --benchmark-only \
      --benchmark-group-by=group,param:backend_type \
      --benchmark-columns=min,max,mean,median,ops
    ;;

  "concurrency")
    echo -e "${YELLOW}Running concurrency benchmarks...${NC}\n"
    pytest tests/benchmarks/test_concurrency.py \
      --benchmark-only \
      --benchmark-group-by=group,param:backend_type \
      --benchmark-columns=min,max,mean,median,ops
    ;;

  "compare")
    echo -e "${YELLOW}Running comparison benchmarks (embedded vs local_fs)...${NC}\n"
    pytest tests/benchmarks/test_throughput.py tests/benchmarks/test_dedup.py \
      --benchmark-only \
      --benchmark-compare \
      --benchmark-group-by=param:backend_type \
      --benchmark-columns=min,max,mean,median,ops
    ;;

  "save")
    BASELINE_NAME=${2:-"baseline"}
    echo -e "${YELLOW}Running and saving baseline as '${BASELINE_NAME}'...${NC}\n"
    pytest tests/benchmarks/ \
      --benchmark-only \
      --benchmark-save="$BASELINE_NAME" \
      --benchmark-group-by=group,param:backend_type
    echo -e "\n${GREEN}Baseline saved as '${BASELINE_NAME}'${NC}"
    ;;

  "all")
    echo -e "${YELLOW}Running all benchmarks (excluding problematic concurrency tests)...${NC}\n"
    pytest tests/benchmarks/test_throughput.py \
           tests/benchmarks/test_dedup.py \
           tests/benchmarks/test_cache.py \
      --benchmark-only \
      --benchmark-group-by=group,param:backend_type \
      --benchmark-columns=min,max,mean,median,ops
    ;;

  "full")
    echo -e "${YELLOW}Running FULL benchmark suite (including concurrency)...${NC}\n"
    pytest tests/benchmarks/ \
      --benchmark-only \
      --benchmark-group-by=group,param:backend_type \
      --benchmark-columns=min,max,mean,median,ops
    ;;

  *)
    echo "Usage: $0 [MODE]"
    echo ""
    echo "Modes:"
    echo "  quick       - Run only throughput benchmarks (fast)"
    echo "  dedup       - Run deduplication benchmarks"
    echo "  cache       - Run cache effectiveness benchmarks"
    echo "  concurrency - Run concurrency benchmarks"
    echo "  compare     - Run comparison between backends"
    echo "  save [NAME] - Save benchmark results as baseline"
    echo "  all         - Run core benchmarks (default, excludes concurrency)"
    echo "  full        - Run ALL benchmarks including concurrency"
    echo ""
    echo "Examples:"
    echo "  $0              # Run all core benchmarks"
    echo "  $0 quick        # Quick throughput test"
    echo "  $0 save v0.3.0  # Save baseline for version 0.3.0"
    exit 1
    ;;
esac

echo -e "\n${GREEN}✓ Benchmarks complete!${NC}"
echo -e "\nSee tests/benchmarks/RESULTS.md for analysis."
