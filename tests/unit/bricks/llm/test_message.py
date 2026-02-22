"""Tests for Message types (src/nexus/llm/message.py)."""

from nexus.contracts.llm_types import (
    ContentType,
    ImageContent,
    ImageDetail,
    Message,
    MessageRole,
    TextContent,
    ToolCall,
    ToolFunction,
)


class TestMessageRole:
    """Tests for the MessageRole enum."""

    def test_message_roles(self) -> None:
        """Verify all expected roles exist with correct string values."""
        assert MessageRole.SYSTEM == "system"
        assert MessageRole.USER == "user"
        assert MessageRole.ASSISTANT == "assistant"
        assert MessageRole.TOOL == "tool"

    def test_message_role_values(self) -> None:
        """Verify MessageRole can be constructed from string values."""
        assert MessageRole("system") == MessageRole.SYSTEM
        assert MessageRole("user") == MessageRole.USER
        assert MessageRole("assistant") == MessageRole.ASSISTANT
        assert MessageRole("tool") == MessageRole.TOOL


class TestContentType:
    """Tests for the ContentType enum."""

    def test_content_types(self) -> None:
        """Verify all content types and their string values."""
        assert ContentType.TEXT == "text"
        assert ContentType.IMAGE_URL == "image_url"
        assert ContentType.IMAGE_FILE == "image_file"


class TestImageDetail:
    """Tests for the ImageDetail enum."""

    def test_image_details(self) -> None:
        """Verify all image detail levels and their string values."""
        assert ImageDetail.AUTO == "auto"
        assert ImageDetail.LOW == "low"
        assert ImageDetail.HIGH == "high"


class TestTextContent:
    """Tests for the TextContent model."""

    def test_text_content_creation(self) -> None:
        """Create TextContent and verify fields."""
        content = TextContent(text="Hello, world!")

        assert content.type == ContentType.TEXT
        assert content.text == "Hello, world!"

    def test_text_content_model_dump(self) -> None:
        """Verify TextContent serializes to the expected dict format."""
        content = TextContent(text="Hello, world!")
        dumped = content.model_dump()

        assert dumped == {"type": "text", "text": "Hello, world!"}


class TestImageContent:
    """Tests for the ImageContent model."""

    def test_image_url_content(self) -> None:
        """Create ImageContent with image_url type."""
        content = ImageContent(
            type=ContentType.IMAGE_URL,
            image_url="https://example.com/image.png",
            detail=ImageDetail.HIGH,
        )

        assert content.type == ContentType.IMAGE_URL
        assert content.image_url == "https://example.com/image.png"
        assert content.detail == ImageDetail.HIGH

    def test_image_file_content(self) -> None:
        """Create ImageContent with image_file type."""
        content = ImageContent(
            type=ContentType.IMAGE_FILE,
            image_file="data:image/png;base64,abc123",
            detail=ImageDetail.LOW,
        )

        assert content.type == ContentType.IMAGE_FILE
        assert content.image_file == "data:image/png;base64,abc123"
        assert content.detail == ImageDetail.LOW

    def test_image_url_content_model_dump(self) -> None:
        """Verify ImageContent with image_url serializes correctly."""
        content = ImageContent(
            type=ContentType.IMAGE_URL,
            image_url="https://example.com/image.png",
            detail=ImageDetail.AUTO,
        )
        dumped = content.model_dump()

        assert dumped == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/image.png", "detail": "auto"},
        }

    def test_image_file_content_model_dump(self) -> None:
        """Verify ImageContent with image_file serializes correctly."""
        content = ImageContent(
            type=ContentType.IMAGE_FILE,
            image_file="data:image/png;base64,abc123",
            detail=ImageDetail.HIGH,
        )
        dumped = content.model_dump()

        assert dumped == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc123", "detail": "high"},
        }


class TestToolFunction:
    """Tests for the ToolFunction model."""

    def test_tool_function_creation(self) -> None:
        """Create ToolFunction and verify fields."""
        func = ToolFunction(name="get_weather", arguments='{"location": "SF"}')

        assert func.name == "get_weather"
        assert func.arguments == '{"location": "SF"}'


class TestToolCall:
    """Tests for the ToolCall model."""

    def test_tool_call_creation(self) -> None:
        """Create ToolCall with id, type, and function."""
        func = ToolFunction(name="get_weather", arguments='{"location": "SF"}')
        tool_call = ToolCall(id="call_123", type="function", function=func)

        assert tool_call.id == "call_123"
        assert tool_call.type == "function"
        assert tool_call.function.name == "get_weather"
        assert tool_call.function.arguments == '{"location": "SF"}'


class TestMessage:
    """Tests for the Message model."""

    def test_simple_text_message(self) -> None:
        """Create a simple user message with string content."""
        msg = Message(role=MessageRole.USER, content="Hello!")

        assert msg.role == MessageRole.USER
        assert msg.content == "Hello!"
        assert msg.name is None
        assert msg.tool_calls is None
        assert msg.tool_call_id is None

    def test_system_message(self) -> None:
        """Create a system message."""
        msg = Message(role=MessageRole.SYSTEM, content="You are a helpful assistant.")

        assert msg.role == MessageRole.SYSTEM
        assert msg.content == "You are a helpful assistant."

    def test_message_with_structured_content(self) -> None:
        """Create a message with structured content (list of TextContent and ImageContent)."""
        content = [
            TextContent(text="Look at this image:"),
            ImageContent(
                type=ContentType.IMAGE_URL,
                image_url="https://example.com/img.png",
            ),
        ]
        msg = Message(role=MessageRole.USER, content=content)

        assert isinstance(msg.content, list)
        assert len(msg.content) == 2
        assert isinstance(msg.content[0], TextContent)
        assert isinstance(msg.content[1], ImageContent)

    def test_message_with_tool_calls(self) -> None:
        """Create an assistant message with tool calls."""
        func = ToolFunction(name="search", arguments='{"query": "test"}')
        tool_call = ToolCall(id="call_456", function=func)
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="Let me search for that.",
            tool_calls=[tool_call],
        )

        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "call_456"
        assert msg.tool_calls[0].function.name == "search"

    def test_message_model_dump_simple_text(self) -> None:
        """Verify model_dump for a simple text message."""
        msg = Message(role=MessageRole.USER, content="Hello!")
        dumped = msg.model_dump()

        assert dumped == {"role": "user", "content": "Hello!"}

    def test_message_model_dump_with_name(self) -> None:
        """Verify model_dump includes name when present."""
        msg = Message(role=MessageRole.USER, content="Hi there", name="alice")
        dumped = msg.model_dump()

        assert dumped == {"role": "user", "content": "Hi there", "name": "alice"}

    def test_message_model_dump_vision_disabled(self) -> None:
        """Verify structured content flattens to string when vision is disabled."""
        content = [
            TextContent(text="Part 1"),
            TextContent(text=" Part 2"),
            ImageContent(
                type=ContentType.IMAGE_URL,
                image_url="https://example.com/img.png",
            ),
        ]
        msg = Message(role=MessageRole.USER, content=content, vision_enabled=False)
        dumped = msg.model_dump()

        # Images should be skipped, text parts concatenated
        assert dumped == {"role": "user", "content": "Part 1 Part 2"}

    def test_message_model_dump_vision_enabled(self) -> None:
        """Verify structured content serializes as list when vision is enabled."""
        content = [
            TextContent(text="Look at this:"),
            ImageContent(
                type=ContentType.IMAGE_URL,
                image_url="https://example.com/img.png",
                detail=ImageDetail.HIGH,
            ),
        ]
        msg = Message(role=MessageRole.USER, content=content, vision_enabled=True)
        dumped = msg.model_dump()

        assert dumped["role"] == "user"
        assert isinstance(dumped["content"], list)
        assert len(dumped["content"]) == 2
        assert dumped["content"][0] == {"type": "text", "text": "Look at this:"}
        assert dumped["content"][1] == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/img.png", "detail": "high"},
        }

    def test_message_model_dump_force_string_serializer(self) -> None:
        """Verify force_string_serializer flattens content even with vision enabled."""
        content = [
            TextContent(text="Hello"),
            ImageContent(
                type=ContentType.IMAGE_URL,
                image_url="https://example.com/img.png",
            ),
        ]
        msg = Message(
            role=MessageRole.USER,
            content=content,
            vision_enabled=True,
            force_string_serializer=True,
        )
        dumped = msg.model_dump()

        # force_string_serializer should flatten to string, images skipped
        assert dumped == {"role": "user", "content": "Hello"}

    def test_message_model_dump_with_tool_calls(self) -> None:
        """Verify tool_calls included in dump when function_calling_enabled=True."""
        func = ToolFunction(name="get_weather", arguments='{"city": "NYC"}')
        tool_call = ToolCall(id="call_789", function=func)
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="Checking weather...",
            tool_calls=[tool_call],
            function_calling_enabled=True,
        )
        dumped = msg.model_dump()

        assert "tool_calls" in dumped
        assert len(dumped["tool_calls"]) == 1
        assert dumped["tool_calls"][0] == {
            "id": "call_789",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "NYC"}'},
        }

    def test_message_model_dump_tool_calls_disabled(self) -> None:
        """Verify tool_calls omitted from dump when function_calling_enabled=False."""
        func = ToolFunction(name="get_weather", arguments='{"city": "NYC"}')
        tool_call = ToolCall(id="call_789", function=func)
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="Checking weather...",
            tool_calls=[tool_call],
            function_calling_enabled=False,
        )
        dumped = msg.model_dump()

        assert "tool_calls" not in dumped

    def test_message_model_dump_with_tool_call_id(self) -> None:
        """Verify tool_call_id is included in dump for tool response messages."""
        msg = Message(
            role=MessageRole.TOOL,
            content='{"temperature": 72}',
            tool_call_id="call_789",
        )
        dumped = msg.model_dump()

        assert dumped == {
            "role": "tool",
            "content": '{"temperature": 72}',
            "tool_call_id": "call_789",
        }

    def test_message_from_dict_simple_text(self) -> None:
        """Verify Message.from_dict creates a message from a simple text dict."""
        data = {"role": "user", "content": "Hello!"}
        msg = Message.from_dict(data)

        assert msg.role == MessageRole.USER
        assert msg.content == "Hello!"

    def test_message_from_dict_with_structured_content(self) -> None:
        """Verify Message.from_dict parses structured content with text and images."""
        data = {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://example.com/photo.jpg",
                        "detail": "high",
                    },
                },
            ],
        }
        msg = Message.from_dict(data)

        assert msg.role == MessageRole.USER
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2

        assert isinstance(msg.content[0], TextContent)
        assert msg.content[0].text == "What is this?"

        assert isinstance(msg.content[1], ImageContent)
        assert msg.content[1].type == ContentType.IMAGE_URL
        assert msg.content[1].image_url == "https://example.com/photo.jpg"
        assert msg.content[1].detail == ImageDetail.HIGH
