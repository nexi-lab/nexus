#!/bin/bash
set -e

echo "🚀 Setting up Nexus development environment..."

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "📦 Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$PATH"
fi

# Create virtual environment
echo "🔧 Creating virtual environment..."
uv venv

# Activate virtual environment
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    source .venv/Scripts/activate
else
    source .venv/bin/activate
fi

# Install dependencies
echo "📚 Installing dependencies..."
uv pip install -e ".[dev,test]"

# Install pre-commit hooks
echo "🔗 Installing pre-commit hooks..."
pre-commit install

# Create necessary directories
echo "📁 Creating directories..."
mkdir -p nexus-data/{workspace,shared,external,system,archives}
mkdir -p logs

# Copy example config
if [ ! -f configs/config.yaml ]; then
    echo "📝 Creating config file..."
    cp configs/config.example.yaml configs/config.yaml
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Activate virtual environment: source .venv/bin/activate"
echo "  2. Update configs/config.yaml with your settings"
echo "  3. Run tests: make test"
echo "  4. Start development server: make run"
echo ""
