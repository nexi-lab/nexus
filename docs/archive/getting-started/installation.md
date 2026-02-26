# Installation

## Prerequisites

- Python 3.11 or higher
- pip or uv package manager

## Using uv (Recommended)

[uv](https://github.com/astral-sh/uv) is a fast Python package installer and resolver.

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone the repository
git clone https://github.com/nexi-lab/nexus.git
cd nexus

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"
```

## Using pip

```bash
# Clone the repository
git clone https://github.com/nexi-lab/nexus.git
cd nexus

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package
pip install -e ".[dev]"
```

## Verify Installation

```bash
# Check nexus CLI
nexus --version

# Run tests to verify everything works
pytest
```

## Docker Installation

```bash
# Pull the latest image
docker pull nexus/nexus:latest

# Run in embedded mode
docker run -it --rm nexus/nexus:latest
```

## Next Steps

- [Quick Start Guide](quickstart.md)
- [Configuration](configuration.md)
- [Deployment Modes](deployment-modes.md)
