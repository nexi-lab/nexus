"""Shared test fixtures for artifact_index tests.

Uses simple dataclass stubs instead of importing from nexus.bricks.a2a
to respect LEGO architecture cross-brick import boundaries.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

# ---- Duck-type stubs matching A2A model shapes ----


@dataclass
class StubTextPart:
    text: str
    type: str = "text"
    metadata: dict[str, Any] | None = None


@dataclass
class StubFileContent:
    url: str | None = None
    name: str | None = None
    mimeType: str | None = None
    bytes: str | None = None


@dataclass
class StubFilePart:
    file: StubFileContent
    type: str = "file"
    metadata: dict[str, Any] | None = None


@dataclass
class StubDataPart:
    data: dict[str, Any]
    type: str = "data"
    metadata: dict[str, Any] | None = None


@dataclass
class StubArtifact:
    artifactId: str
    parts: list[Any] = field(default_factory=list)
    name: str | None = None
    description: str | None = None
    metadata: dict[str, Any] | None = None


@pytest.fixture
def text_artifact() -> StubArtifact:
    return StubArtifact(
        artifactId="art-1",
        parts=[StubTextPart(text="hello world")],
    )


@pytest.fixture
def multi_part_artifact() -> StubArtifact:
    return StubArtifact(
        artifactId="art-multi",
        parts=[
            StubTextPart(text="intro"),
            StubDataPart(data={"x": 1}),
            StubFilePart(file=StubFileContent(url="http://f.com/a")),
        ],
    )
