# Contributing to Nexus

Thank you for your interest in contributing to Nexus! This document provides guidelines and instructions for contributing.

## Getting Started

### Prerequisites

- Python 3.11 or higher
- [uv](https://github.com/astral-sh/uv) package manager
- Git

### Development Setup

1. Fork and clone the repository:
```bash
git clone https://github.com/yourusername/nexus.git
cd nexus
```

2. Run the setup script:
```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

3. Activate the virtual environment:
```bash
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

## Development Workflow

### Making Changes

1. Create a new branch:
```bash
git checkout -b feature/your-feature-name
```

2. Make your changes and write tests

3. Run tests:
```bash
make test
```

4. Format and lint:
```bash
make format
make lint
```

5. Commit your changes:
```bash
git add .
git commit -m "Description of your changes"
```

### Code Style

We use:
- **ruff** for linting and formatting
- **mypy** for type checking
- **black** style guide (100 character line length)

Run before committing:
```bash
make format  # Auto-format code
make lint    # Check for issues
```

### Testing

We use pytest for testing. Write tests for all new features.

```bash
# Run all tests
make test

# Run with coverage
make test-cov

# Run specific test file
pytest tests/unit/test_client.py

# Run specific test
pytest tests/unit/test_client.py::test_client_initialization
```

### Type Checking

All code should be fully typed. Run mypy:

```bash
mypy src/nexus
```

## Pull Request Process

1. Update the README.md if needed
2. Update documentation for new features
3. Ensure all tests pass
4. Ensure code is formatted and linted
5. Create a pull request with a clear description

### PR Description Template

```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
Describe testing done

## Checklist
- [ ] Tests added/updated
- [ ] Documentation updated
- [ ] Code formatted (make format)
- [ ] Linting passes (make lint)
- [ ] Type checking passes (mypy)
```

## Code Structure

```
nexus/
├── src/nexus/          # Main source code
│   ├── core/           # Core filesystem functionality
│   ├── api/            # REST API
│   ├── storage/        # Storage backends
│   ├── agents/         # Agent memory system
│   ├── parsers/        # Document parsers
│   ├── mcp/            # MCP integration
│   ├── jobs/           # Job system
│   ├── auth/           # Authentication
│   └── utils/          # Utilities
├── tests/              # Tests
│   ├── unit/           # Unit tests
│   ├── integration/    # Integration tests
│   └── performance/    # Performance tests
├── docs/               # Documentation
├── examples/           # Example code
└── scripts/            # Development scripts
```

## Commit Message Guidelines

Use conventional commits format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation
- `style`: Formatting
- `refactor`: Code restructuring
- `test`: Tests
- `chore`: Maintenance

Examples:
```
feat(storage): add S3 backend support
fix(api): handle timeout errors correctly
docs(readme): update installation instructions
```

## Architecture Guidelines

### Design Principles

1. **"Everything as a File"**: Configuration, memory, jobs as files
2. **Three Deployment Modes**: Support embedded, monolithic, and distributed
3. **Backend Agnostic**: Abstract storage backends
4. **Type Safety**: Full type annotations
5. **Testability**: Write testable, modular code

### Adding New Features

#### Adding a Storage Backend

1. Create new file in `src/nexus/storage/backends/`
2. Implement `Backend` interface
3. Register in `BackendRegistry`
4. Add tests
5. Update documentation

#### Adding a Parser

1. Create new file in `src/nexus/parsers/`
2. Implement `Parser` interface
3. Register in `ParserRegistry`
4. Add tests
5. Update documentation

## Documentation

- Update docstrings for all public APIs
- Follow Google style docstrings
- Update README.md for user-facing changes
- Add examples for new features

Example docstring:
```python
def read_file(path: str) -> bytes:
    """
    Read file content from virtual path.

    Args:
        path: Virtual path to read

    Returns:
        File content as bytes

    Raises:
        FileNotFoundError: If file does not exist
        PermissionError: If access is denied

    Example:
        >>> content = await client.read("/workspace/data.txt")
    """
```

## Getting Help

- **GitHub Issues**: For bug reports and feature requests
- **GitHub Discussions**: For questions and discussions
- **Slack**: Join our community (link in README)

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 License.
