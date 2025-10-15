# Nexus Development Guide

This guide covers development setup, workflow, and best practices for contributing to Nexus.

## Table of Contents

- [Getting Started](#getting-started)
- [Project Structure](#project-structure)
- [Development Workflow](#development-workflow)
- [Testing](#testing)
- [Database Migrations](#database-migrations)
- [Code Style](#code-style)
- [Contributing](#contributing)

## Additional Guides

- **[Backend Architecture](BACKEND_ARCHITECTURE.md)** - Unified backend interface, CAS, implementing new backends
- **[Database Compatibility](DATABASE_COMPATIBILITY.md)** - SQLite and PostgreSQL support

---

## Getting Started

### Prerequisites

- Python 3.11 or higher
- Git
- uv (optional, for faster package management)

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/nexi-lab/nexus.git
cd nexus

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install in development mode with dev dependencies
pip install -e ".[dev]"

# Or use uv (faster)
uv pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install
```

### Verify Installation

```bash
# Run tests
pytest

# Check code style
ruff check src/ tests/
ruff format --check src/ tests/

# Type check
mypy src/nexus/
```

---

## Project Structure

```
nexus/
├── src/nexus/              # Source code
│   ├── __init__.py         # Main entry point (nexus.connect)
│   ├── config.py           # Configuration management
│   ├── core/               # Core functionality
│   │   ├── embedded.py     # Embedded mode implementation
│   │   ├── backend.py      # Storage backend interface
│   │   ├── backends/       # Backend implementations
│   │   │   └── local.py    # Local filesystem backend
│   │   ├── metadata.py     # Metadata store interface
│   │   ├── exceptions.py   # Custom exceptions
│   │   └── schema/         # Old schema definitions (deprecated)
│   └── storage/            # New storage layer (SQLAlchemy)
│       ├── models.py       # SQLAlchemy models
│       └── metadata_store.py  # New metadata store
│
├── tests/                  # Test suite
│   ├── unit/               # Unit tests
│   │   ├── core/           # Tests for core
│   │   └── storage/        # Tests for storage
│   ├── integration/        # Integration tests (future)
│   └── e2e/                # End-to-end tests (future)
│
├── alembic/                # Database migrations
│   ├── versions/           # Migration files
│   ├── env.py              # Alembic environment
│   └── README_DATABASES.md # Migration guide
│
├── examples/               # Example code
│   ├── integrated_demo.py  # Main demo
│   └── README.md           # Examples documentation
│
├── docs/                   # Documentation
│   ├── api.md              # API documentation
│   ├── development/        # Development guides
│   │   ├── development.md  # This file
│   │   ├── BACKEND_ARCHITECTURE.md  # Backend architecture guide
│   │   └── DATABASE_COMPATIBILITY.md  # Database guide
│
├── configs/                # Example configurations
├── scripts/                # Development scripts
├── pyproject.toml          # Project configuration
├── alembic.ini             # Alembic configuration
└── README.md               # Project README
```

### Key Files

| File | Purpose |
|------|---------|
| `src/nexus/__init__.py` | Main entry point, exports `connect()` |
| `src/nexus/core/embedded.py` | Embedded mode implementation |
| `src/nexus/storage/models.py` | SQLAlchemy models |
| `src/nexus/storage/metadata_store.py` | Metadata store implementation |
| `pyproject.toml` | Dependencies, build config, tool settings |
| `alembic.ini` | Database migration settings |

---

## Development Workflow

### 1. Create a Branch

```bash
git checkout -b feature/your-feature-name
```

### 2. Make Changes

```bash
# Edit files
vim src/nexus/core/embedded.py

# Run tests continuously during development
pytest tests/unit/core/test_embedded.py -v

# Check code style
ruff check src/nexus/core/embedded.py
```

### 3. Run Full Test Suite

```bash
# All tests
pytest

# With coverage
pytest --cov=nexus --cov-report=html

# Specific test file
pytest tests/unit/storage/test_metadata_store.py -v

# Specific test
pytest tests/unit/core/test_embedded.py::test_write_creates_file -v
```

### 4. Format and Lint

```bash
# Format code
ruff format src/ tests/

# Lint and auto-fix
ruff check --fix src/ tests/

# Type check
mypy src/nexus/
```

### 5. Commit Changes

```bash
# Stage changes
git add .

# Commit (pre-commit hooks run automatically)
git commit -m "feat: add support for custom metadata"

# If pre-commit hooks fail, fix issues and try again
```

### 6. Push and Create PR

```bash
git push origin feature/your-feature-name

# Create PR on GitHub
gh pr create --title "Add custom metadata support" --body "Description..."
```

---

## Testing

### Running Tests

```bash
# All tests
pytest

# Specific directory
pytest tests/unit/storage/

# Specific file
pytest tests/unit/core/test_embedded.py

# Specific test
pytest tests/unit/core/test_embedded.py::test_write_creates_file

# With output
pytest -v

# With coverage
pytest --cov=nexus

# Skip slow tests
pytest -m "not slow"
```

### Writing Tests

#### Unit Test Template

```python
"""Unit tests for MyClass."""

import pytest
from nexus.core.mymodule import MyClass


@pytest.fixture
def my_instance():
    """Create a MyClass instance for testing."""
    return MyClass(param="value")


class TestMyClass:
    """Test suite for MyClass."""

    def test_basic_operation(self, my_instance):
        """Test basic operation."""
        result = my_instance.do_something()
        assert result == expected_value

    def test_error_handling(self, my_instance):
        """Test error handling."""
        with pytest.raises(ValueError):
            my_instance.do_invalid_thing()
```

#### Test Fixtures

Common fixtures are defined in `tests/conftest.py`:

```python
import pytest
import tempfile
from pathlib import Path
from nexus.core.embedded import Embedded


@pytest.fixture
def temp_dir():
    """Create temporary directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def embedded(temp_dir):
    """Create Embedded instance."""
    nx = Embedded(data_dir=temp_dir / "nexus-data")
    yield nx
    nx.close()
```

### Test Coverage

```bash
# Generate coverage report
pytest --cov=nexus --cov-report=html

# View report
open htmlcov/index.html

# Coverage requirements
# - Overall: 80%+
# - New code: 90%+
```

---

## Database Migrations

### Creating Migrations

```bash
# 1. Modify models in src/nexus/storage/models.py
# 2. Generate migration
alembic revision --autogenerate -m "Add new field to FilePathModel"

# 3. Review generated migration
cat alembic/versions/xxxx_add_new_field.py

# 4. Edit if needed
vim alembic/versions/xxxx_add_new_field.py

# 5. Test migration
alembic upgrade head

# 6. Test rollback
alembic downgrade -1

# 7. Re-upgrade
alembic upgrade head
```

### Migration Commands

```bash
# Show current version
alembic current

# Show migration history
alembic history

# Upgrade to latest
alembic upgrade head

# Upgrade one version
alembic upgrade +1

# Downgrade one version
alembic downgrade -1

# Downgrade to base
alembic downgrade base

# Show SQL without executing
alembic upgrade head --sql
```

### Migration Best Practices

1. **Always review auto-generated migrations**
   ```bash
   alembic revision --autogenerate -m "description"
   # Review the file before applying!
   ```

2. **Test both upgrade and downgrade**
   ```bash
   alembic upgrade head
   alembic downgrade -1
   alembic upgrade head
   ```

3. **Make migrations reversible**
   ```python
   def upgrade():
       op.add_column('table', sa.Column('new_col', sa.String(50)))

   def downgrade():
       op.drop_column('table', 'new_col')
   ```

4. **Use transactions for SQLite**
   ```python
   from alembic import op
   import sqlalchemy as sa

   def upgrade():
       with op.batch_alter_table('table_name') as batch_op:
           batch_op.add_column(sa.Column('new_col', sa.String(50)))
   ```

---

## Code Style

### Python Style Guide

We follow PEP 8 with some modifications:

- **Line length**: 100 characters (not 79)
- **String quotes**: Double quotes `"` (not single `'`)
- **Imports**: Sorted with `isort`
- **Type hints**: Required for all public functions

### Ruff Configuration

See `pyproject.toml` for full configuration:

```toml
[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "C4", "UP", "ARG", "SIM"]
ignore = ["E501"]  # Line length handled by formatter
```

### Running Formatters

```bash
# Format code
ruff format src/ tests/

# Check formatting without changes
ruff format --check src/ tests/

# Lint
ruff check src/ tests/

# Auto-fix linting issues
ruff check --fix src/ tests/
```

### Type Checking

```bash
# Type check
mypy src/nexus/

# Type check specific file
mypy src/nexus/core/embedded.py

# Strict mode (for new modules)
mypy --strict src/nexus/storage/
```

### Pre-commit Hooks

Hooks run automatically on commit:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
```

Skip hooks if needed (not recommended):

```bash
git commit --no-verify -m "message"
```

---

## Contributing

### Commit Message Convention

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `test`: Test changes
- `refactor`: Code refactoring
- `perf`: Performance improvement
- `chore`: Build/tooling changes

**Examples:**

```bash
git commit -m "feat(storage): add custom metadata support"
git commit -m "fix(embedded): handle empty path in list()"
git commit -m "docs(api): update write() method documentation"
git commit -m "test(storage): add tests for concurrent access"
```

### Pull Request Process

1. **Create feature branch**
   ```bash
   git checkout -b feature/your-feature
   ```

2. **Make changes and commit**
   ```bash
   git add .
   git commit -m "feat: add feature"
   ```

3. **Push to GitHub**
   ```bash
   git push origin feature/your-feature
   ```

4. **Create PR**
   - Use descriptive title
   - Reference related issues
   - Include test results
   - Update documentation

5. **Address review feedback**
   ```bash
   git add .
   git commit -m "address review feedback"
   git push
   ```

6. **Merge**
   - Squash commits if needed
   - Delete branch after merge

### Code Review Checklist

**Before requesting review:**
- ✅ All tests pass
- ✅ Code is formatted and linted
- ✅ Type checking passes
- ✅ Documentation updated
- ✅ Examples updated (if API changed)
- ✅ Migration created (if schema changed)
- ✅ Changelog updated (for significant changes)

**During review:**
- Be open to feedback
- Ask questions if unclear
- Discuss trade-offs
- Update based on feedback

---

## Development Tips

### Running Examples

```bash
# Run integrated demo
PYTHONPATH=src python examples/integrated_demo.py

# Run with custom data directory
PYTHONPATH=src NEXUS_DATA_DIR=/tmp/nexus python examples/integrated_demo.py
```

### Debugging

```bash
# Run tests with print output
pytest -s tests/unit/core/test_embedded.py

# Run tests with debugger
pytest --pdb tests/unit/core/test_embedded.py

# Use breakpoint() in code
def my_function():
    breakpoint()  # Drops into debugger
    return result
```

### Performance Profiling

```bash
# Profile code
python -m cProfile -o profile.stats script.py
python -m pstats profile.stats

# Or use pytest-benchmark
pytest tests/benchmark/test_performance.py --benchmark-only
```

### Database Inspection

```bash
# Inspect SQLite database
sqlite3 nexus.db

# Common queries
sqlite> .tables
sqlite> .schema file_paths
sqlite> SELECT * FROM file_paths;
sqlite> .quit
```

---

## Troubleshooting

### Common Issues

#### Import Errors

```bash
# Solution: Add src to PYTHONPATH
export PYTHONPATH=/path/to/nexus/src:$PYTHONPATH

# Or use editable install
pip install -e .
```

#### Migration Conflicts

```bash
# If multiple migrations conflict
alembic downgrade base
alembic upgrade head

# Or start fresh (development only!)
rm nexus.db
alembic upgrade head
```

#### Test Failures

```bash
# Run specific failing test with verbose output
pytest tests/unit/core/test_embedded.py::test_write_creates_file -vv

# Check for file locks
lsof | grep nexus.db

# Clean test artifacts
rm -rf .pytest_cache
rm -rf htmlcov
rm -rf .coverage
```

---

## Release Process

### Version Bumping

```bash
# Update version in pyproject.toml
vim pyproject.toml

# Update __version__ in __init__.py
vim src/nexus/__init__.py

# Commit
git add pyproject.toml src/nexus/__init__.py
git commit -m "chore: bump version to 0.1.1"
```

### Creating Release

```bash
# Tag release
git tag -a v0.1.1 -m "Release v0.1.1"
git push origin v0.1.1

# Create GitHub release
gh release create v0.1.1 --title "v0.1.1" --notes "Release notes..."
```

---

## Resources

- **API Documentation**: `docs/api.md`
- **Backend Architecture**: `docs/development/BACKEND_ARCHITECTURE.md`
- **Database Guide**: `docs/development/DATABASE_COMPATIBILITY.md`
- **Contributing**: `CONTRIBUTING.md`
- **Issues**: https://github.com/nexi-lab/nexus/issues

---

## Getting Help

- **Documentation**: Check `docs/` directory
- **Examples**: See `examples/` directory
- **Issues**: Search or create on GitHub
- **Discussions**: GitHub Discussions

---

## Next Steps

1. Set up your development environment
2. Run the test suite
3. Try the examples
4. Pick an issue to work on
5. Create your first PR!

Happy coding! 🚀
