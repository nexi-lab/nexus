"""Module with content that overlaps with hello.py for testing."""


def hello_world_again():
    """Another hello world function."""
    print("Hello, world!")
    return "hello again"


def search_pattern(text: str, pattern: str) -> bool:
    """Search for a pattern in text."""
    return pattern in text
