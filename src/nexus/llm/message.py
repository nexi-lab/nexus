"""Message types for LLM interactions."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    """Role of a message in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ContentType(StrEnum):
    """Type of content in a message."""

    TEXT = "text"
    IMAGE_URL = "image_url"
    IMAGE_FILE = "image_file"


class ImageDetail(StrEnum):
    """Level of detail for image analysis."""

    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class ImageContent(BaseModel):
    """Image content in a message."""

    type: Literal[ContentType.IMAGE_URL, ContentType.IMAGE_FILE] = ContentType.IMAGE_URL
    image_url: str | None = None
    image_file: str | None = None
    detail: ImageDetail = ImageDetail.AUTO

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Convert to dict format expected by LLM providers."""
        if self.type == ContentType.IMAGE_URL and self.image_url:
            return {
                "type": "image_url",
                "image_url": {"url": self.image_url, "detail": self.detail.value},
            }
        elif self.type == ContentType.IMAGE_FILE and self.image_file:
            return {
                "type": "image_url",
                "image_url": {"url": self.image_file, "detail": self.detail.value},
            }
        return super().model_dump(**kwargs)


class TextContent(BaseModel):
    """Text content in a message."""

    type: Literal[ContentType.TEXT] = ContentType.TEXT
    text: str

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
        """Convert to dict format expected by LLM providers."""
        return {"type": "text", "text": self.text}


class ToolCall(BaseModel):
    """A tool/function call made by the LLM."""

    id: str
    type: Literal["function"] = "function"
    function: ToolFunction


class ToolFunction(BaseModel):
    """Function details in a tool call."""

    name: str
    arguments: str  # JSON string of arguments


class Message(BaseModel):
    """A message in a conversation with an LLM."""

    role: MessageRole
    content: str | list[TextContent | ImageContent] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    # Serialization flags
    cache_enabled: bool = Field(default=False, exclude=True)
    vision_enabled: bool = Field(default=False, exclude=True)
    function_calling_enabled: bool = Field(default=False, exclude=True)
    force_string_serializer: bool = Field(default=False, exclude=True)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
        """Convert to dict format expected by LLM providers."""
        result: dict[str, Any] = {"role": self.role.value}

        # Handle content serialization
        if self.content is not None:
            if isinstance(self.content, str):
                result["content"] = self.content
            elif isinstance(self.content, list):
                if self.force_string_serializer or not self.vision_enabled:
                    # Flatten to string if vision is disabled or forced
                    text_parts = []
                    for item in self.content:
                        if isinstance(item, TextContent):
                            text_parts.append(item.text)
                        elif isinstance(item, ImageContent):
                            # Skip images if vision is disabled
                            pass
                    result["content"] = "".join(text_parts)
                else:
                    # Serialize as list for vision-enabled models
                    result["content"] = [item.model_dump() for item in self.content]

        # Add optional fields
        if self.name is not None:
            result["name"] = self.name

        if self.tool_calls is not None and self.function_calling_enabled:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ]

        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id

        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        """Create a Message from a dict."""
        role = MessageRole(data["role"])
        content = data.get("content")

        # Parse content
        parsed_content: str | list[TextContent | ImageContent] | None = None
        if content is not None:
            if isinstance(content, str):
                parsed_content = content
            elif isinstance(content, list):
                parsed_content = []
                for item in content:
                    if item["type"] == "text":
                        parsed_content.append(TextContent(text=item["text"]))
                    elif item["type"] == "image_url":
                        url = item.get("image_url", {}).get("url")
                        detail = item.get("image_url", {}).get("detail", "auto")
                        parsed_content.append(
                            ImageContent(
                                type=ContentType.IMAGE_URL,
                                image_url=url,
                                detail=ImageDetail(detail),
                            )
                        )

        # Parse tool calls
        tool_calls = None
        if "tool_calls" in data:
            tool_calls = [
                ToolCall(
                    id=tc["id"],
                    type=tc["type"],
                    function=ToolFunction(
                        name=tc["function"]["name"], arguments=tc["function"]["arguments"]
                    ),
                )
                for tc in data["tool_calls"]
            ]

        return cls(
            role=role,
            content=parsed_content,
            name=data.get("name"),
            tool_calls=tool_calls,
            tool_call_id=data.get("tool_call_id"),
        )
