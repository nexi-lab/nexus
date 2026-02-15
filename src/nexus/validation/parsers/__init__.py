"""Built-in validator parsers registry.

Each parser knows how to build a shell command and parse its output
into structured ValidationError objects.
"""

from nexus.validation.parsers.base import Validator
from nexus.validation.parsers.clippy import CargoClippyValidator
from nexus.validation.parsers.eslint import ESLintValidator
from nexus.validation.parsers.mypy import MypyValidator
from nexus.validation.parsers.ruff import RuffValidator

BUILTIN_VALIDATORS: dict[str, type[Validator]] = {
    "ruff": RuffValidator,
    "mypy": MypyValidator,
    "eslint": ESLintValidator,
    "cargo-clippy": CargoClippyValidator,
}

__all__ = [
    "BUILTIN_VALIDATORS",
    "CargoClippyValidator",
    "ESLintValidator",
    "MypyValidator",
    "RuffValidator",
    "Validator",
]
