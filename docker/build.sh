#!/usr/bin/env bash
#
# Build Nexus Sandbox Docker Image
#
# This script builds the nexus-sandbox:latest image with all dependencies
# pre-installed for fast sandbox container startup.
#
# Usage:
#   ./docker/build.sh           # Build production image (nexus-runtime:latest from PyPI)
#   ./docker/build.sh --dev     # Build dev image (nexus-runtime:dev from local source)
#   ./docker/build.sh --force   # Force rebuild (no cache)
#   ./docker/build.sh --version # Show version info

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

IMAGE_NAME="nexus-sandbox"
IMAGE_TAG="latest"
DOCKERFILE="docker/nexus-runtime.Dockerfile"
BUILD_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Get Nexus version from pyproject.toml if available
if [ -f "pyproject.toml" ]; then
    NEXUS_VERSION=$(grep '^version = ' pyproject.toml | sed 's/version = "\(.*\)"/\1/')
else
    NEXUS_VERSION="dev"
fi

echo -e "${BLUE}=== Building Nexus Sandbox Image ===${NC}"
echo
echo "Image: ${IMAGE_NAME}:${IMAGE_TAG}"
echo "Nexus Version: ${NEXUS_VERSION}"
echo "Build Time: ${BUILD_TIME}"
echo

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${YELLOW}❌ Docker is not running${NC}"
    echo "Please start Docker and try again"
    exit 1
fi

# Parse arguments
BUILD_ARGS=""
for arg in "$@"; do
    case "$arg" in
        --dev)
            IMAGE_TAG="dev"
            DOCKERFILE="docker/nexus-runtime-dev.Dockerfile"
            echo "Building dev image from local source"
            ;;
        --force)
            BUILD_ARGS="--no-cache"
            echo "Building with --no-cache (force rebuild)"
            ;;
        --version)
            echo "Nexus Version: ${NEXUS_VERSION}"
            echo "Image: ${IMAGE_NAME}:${IMAGE_TAG}"
            exit 0
            ;;
        --help|-h)
            echo "Build Nexus Runtime Docker Image"
            echo ""
            echo "Usage: ./docker/build.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --dev       Build dev image from local source (nexus-runtime:dev)"
            echo "  --force     Force rebuild without cache"
            echo "  --version   Show version info"
            echo "  --help      Show this help message"
            echo ""
            echo "Examples:"
            echo "  ./docker/build.sh           # Build production image"
            echo "  ./docker/build.sh --dev     # Build dev image from local code"
            echo "  ./docker/build.sh --force   # Force rebuild"
            echo "  ./docker/build.sh --dev --force  # Force rebuild dev image"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: ./docker/build.sh [--dev] [--force] [--version] [--help]"
            exit 1
            ;;
    esac
done

# Check if Dockerfile exists
if [ ! -f "$DOCKERFILE" ]; then
    echo -e "${YELLOW}❌ Dockerfile not found: $DOCKERFILE${NC}"
    echo "Please run this script from the nexus repository root"
    exit 1
fi

# Build the image
echo "Building image..."
echo

docker build \
    -f ${DOCKERFILE} \
    -t ${IMAGE_NAME}:${IMAGE_TAG} \
    --build-arg NEXUS_VERSION=${NEXUS_VERSION} \
    --build-arg BUILD_TIME=${BUILD_TIME} \
    ${BUILD_ARGS} \
    .

echo
echo -e "${GREEN}✓ Build successful!${NC}"
echo

# Show image details
echo "Image details:"
docker images ${IMAGE_NAME}:${IMAGE_TAG}
echo

# Verify installations
echo "Verifying installations..."
docker run --rm ${IMAGE_NAME}:${IMAGE_TAG} python --version
docker run --rm ${IMAGE_NAME}:${IMAGE_TAG} node --version
docker run --rm ${IMAGE_NAME}:${IMAGE_TAG} nexus --version
echo

echo -e "${GREEN}✓ Image ready to use${NC}"
echo
echo "Usage:"
echo "  nexus sandbox create my-sandbox --provider docker --template ${IMAGE_NAME}:${IMAGE_TAG}"
echo "  ./examples/cli/docker_sandbox_demo.sh"
echo
echo "To push to registry:"
echo "  docker tag ${IMAGE_NAME}:${IMAGE_TAG} ${IMAGE_NAME}:${NEXUS_VERSION}"
echo "  docker push ${IMAGE_NAME}:${IMAGE_TAG}"
echo "  docker push ${IMAGE_NAME}:${NEXUS_VERSION}"
