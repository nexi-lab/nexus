"""Tests for LLM message types."""

import json

from nexus.llm.message import (
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
    """Test MessageRole enum."""

    def test_message_roles(self):
        """Test all message roles."""
        assert MessageRole.SYSTEM == "system"
        assert MessageRole.USER == "user"
        assert MessageRole.ASSISTANT == "assistant"
        assert MessageRole.TOOL == "tool"

    def test_message_role_values(self):
        """Test MessageRole can be created from strings."""
        assert MessageRole("system") == MessageRole.SYSTEM
        assert MessageRole("user") == MessageRole.USER
        assert MessageRole("assistant") == MessageRole.ASSISTANT
        assert MessageRole("tool") == MessageRole.TOOL


class TestContentType:
    """Test ContentType enum."""

    def test_content_types(self):
        """Test all content types."""
        assert ContentType.TEXT == "text"
        assert ContentType.IMAGE_URL == "image_url"
        assert ContentType.IMAGE_FILE == "image_file"


class TestImageDetail:
    """Test ImageDetail enum."""

    def test_image_details(self):
        """Test all image detail levels."""
        assert ImageDetail.AUTO == "auto"
        assert ImageDetail.LOW == "low"
        assert ImageDetail.HIGH == "high"


class TestTextContent:
    """Test TextContent class."""

    def test_text_content_creation(self):
        """Test creating text content."""
        content = TextContent(text="Hello world")
        assert content.type == ContentType.TEXT
        assert content.text == "Hello world"

    def test_text_content_model_dump(self):
        """Test text content serialization."""
        content = TextContent(text="Test message")
        dumped = content.model_dump()
        assert dumped == {"type": "text", "text": "Test message"}


class TestImageContent:
    """Test ImageContent class."""

    def test_image_url_content(self):
        """Test image URL content."""
        content = ImageContent(
            type=ContentType.IMAGE_URL, image_url="https://example.com/image.png"
        )
        assert content.type == ContentType.IMAGE_URL
        assert content.image_url == "https://example.com/image.png"
        assert content.detail == ImageDetail.AUTO

    def test_image_file_content(self):
        """Test image file content."""
        content = ImageContent(
            type=ContentType.IMAGE_FILE, image_file="/path/to/image.png", detail=ImageDetail.HIGH
        )
        assert content.type == ContentType.IMAGE_FILE
        assert content.image_file == "/path/to/image.png"
        assert content.detail == ImageDetail.HIGH

    def test_image_url_content_model_dump(self):
        """Test image URL content serialization."""
        content = ImageContent(
            type=ContentType.IMAGE_URL,
            image_url="https://example.com/image.png",
            detail=ImageDetail.HIGH,
        )
        dumped = content.model_dump()
        assert dumped == {
            "type": "image_url",
            "image_url": {"url": "https://example.com/image.png", "detail": "high"},
        }

    def test_image_file_content_model_dump(self):
        """Test image file content serialization."""
        content = ImageContent(
            type=ContentType.IMAGE_FILE, image_file="/path/image.png", detail=ImageDetail.LOW
        )
        dumped = content.model_dump()
        assert dumped == {
            "type": "image_url",
            "image_url": {"url": "/path/image.png", "detail": "low"},
        }


class TestToolFunction:
    """Test ToolFunction class."""

    def test_tool_function_creation(self):
        """Test creating tool function."""
        func = ToolFunction(name="get_weather", arguments='{"location": "SF"}')
        assert func.name == "get_weather"
        assert func.arguments == '{"location": "SF"}'


class TestToolCall:
    """Test ToolCall class."""

    def test_tool_call_creation(self):
        """Test creating tool call."""
        func = ToolFunction(name="search", arguments='{"query": "test"}')
        tool_call = ToolCall(id="call_123", type="function", function=func)
        assert tool_call.id == "call_123"
        assert tool_call.type == "function"
        assert tool_call.function.name == "search"
        assert tool_call.function.arguments == '{"query": "test"}'


class TestMessage:
    """Test Message class."""

    def test_simple_text_message(self):
        """Test simple text message."""
        msg = Message(role=MessageRole.USER, content="Hello")
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"
        assert msg.name is None
        assert msg.tool_calls is None

    def test_system_message(self):
        """Test system message."""
        msg = Message(role=MessageRole.SYSTEM, content="You are a helpful assistant")
        assert msg.role == MessageRole.SYSTEM
        assert msg.content == "You are a helpful assistant"

    def test_message_with_structured_content(self):
        """Test message with structured content."""
        content = [
            TextContent(text="Look at this image:"),
            ImageContent(type=ContentType.IMAGE_URL, image_url="https://example.com/img.png"),
        ]
        msg = Message(role=MessageRole.USER, content=content)
        assert msg.role == MessageRole.USER
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2

    def test_message_with_tool_calls(self):
        """Test message with tool calls."""
        tool_call = ToolCall(
            id="call_1",
            type="function",
            function=ToolFunction(name="get_weather", arguments='{"city": "NYC"}'),
        )
        msg = Message(role=MessageRole.ASSISTANT, content=None, tool_calls=[tool_call])
        assert msg.role == MessageRole.ASSISTANT
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "call_1"

    def test_message_model_dump_simple_text(self):
        """Test message serialization with simple text."""
        msg = Message(role=MessageRole.USER, content="Hello")
        dumped = msg.model_dump()
        assert dumped == {"role": "user", "content": "Hello"}

    def test_message_model_dump_with_name(self):
        """Test message serialization with name."""
        msg = Message(role=MessageRole.USER, content="Hello", name="John")
        dumped = msg.model_dump()
        assert dumped == {"role": "user", "content": "Hello", "name": "John"}

    def test_message_model_dump_vision_disabled(self):
        """Test message serialization with vision disabled."""
        content = [
            TextContent(text="Hello"),
            ImageContent(type=ContentType.IMAGE_URL, image_url="https://example.com/img.png"),
            TextContent(text="World"),
        ]
        msg = Message(role=MessageRole.USER, content=content, vision_enabled=False)
        dumped = msg.model_dump()
        # Images should be stripped, text concatenated
        assert dumped == {"role": "user", "content": "HelloWorld"}

    def test_message_model_dump_vision_enabled(self):
        """Test message serialization with vision enabled."""
        content = [
            TextContent(text="Look at this:"),
            ImageContent(type=ContentType.IMAGE_URL, image_url="https://example.com/img.png"),
        ]
        msg = Message(role=MessageRole.USER, content=content, vision_enabled=True)
        dumped = msg.model_dump()
        assert dumped["role"] == "user"
        assert isinstance(dumped["content"], list)
        assert len(dumped["content"]) == 2
        assert dumped["content"][0] == {"type": "text", "text": "Look at this:"}
        assert dumped["content"][1]["type"] == "image_url"

    def test_message_model_dump_force_string_serializer(self):
        """Test message serialization with forced string serializer."""
        content = [TextContent(text="Hello"), TextContent(text=" World")]
        msg = Message(
            role=MessageRole.USER,
            content=content,
            vision_enabled=True,
            force_string_serializer=True,
        )
        dumped = msg.model_dump()
        assert dumped == {"role": "user", "content": "Hello World"}

    def test_message_model_dump_with_tool_calls(self):
        """Test message serialization with tool calls."""
        tool_call = ToolCall(
            id="call_123",
            type="function",
            function=ToolFunction(name="search", arguments='{"q": "test"}'),
        )
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="Let me search for that",
            tool_calls=[tool_call],
            function_calling_enabled=True,
        )
        dumped = msg.model_dump()
        assert dumped["role"] == "assistant"
        assert dumped["content"] == "Let me search for that"
        assert "tool_calls" in dumped
        assert len(dumped["tool_calls"]) == 1
        assert dumped["tool_calls"][0]["id"] == "call_123"
        assert dumped["tool_calls"][0]["function"]["name"] == "search"

    def test_message_model_dump_tool_calls_disabled(self):
        """Test that tool calls are omitted when function_calling_enabled is False."""
        tool_call = ToolCall(
            id="call_123",
            type="function",
            function=ToolFunction(name="search", arguments='{"q": "test"}'),
        )
        msg = Message(
            role=MessageRole.ASSISTANT,
            content="Let me search",
            tool_calls=[tool_call],
            function_calling_enabled=False,
        )
        dumped = msg.model_dump()
        assert "tool_calls" not in dumped

    def test_message_model_dump_with_tool_call_id(self):
        """Test message serialization with tool_call_id."""
        msg = Message(
            role=MessageRole.TOOL,
            content="Search results...",
            tool_call_id="call_123",
        )
        dumped = msg.model_dump()
        assert dumped["role"] == "tool"
        assert dumped["content"] == "Search results..."
        assert dumped["tool_call_id"] == "call_123"

    def test_message_from_dict_simple_text(self):
        """Test creating message from dict with simple text."""
        data = {"role": "user", "content": "Hello"}
        msg = Message.from_dict(data)
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"

    def test_message_from_dict_with_structured_content(self):
        """Test creating message from dict with structured content."""
        data = {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this:"},
                {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
            ],
        }
        msg = Message.from_dict(data)
        assert msg.role == MessageRole.USER
        assert isinstance(msg.content, list)
        assert len(msg.content) == 2
        assert isinstance(msg.content[0], TextContent)
        assert msg.content[0].text == "Look at this:"
        assert isinstance(msg.content[1], ImageContent)
        assert msg.content[1].image_url == "https://example.com/img.png"

    def test_message_from_dict_with_image_detail(self):
        """Test creating message from dict with image detail."""
        data = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/img.png", "detail": "high"},
                }
            ],
        }
        msg = Message.from_dict(data)
        assert isinstance(msg.content, list)
        assert msg.content[0].detail == ImageDetail.HIGH

    def test_message_from_dict_with_tool_calls(self):
        """Test creating message from dict with tool calls."""
        data = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                }
            ],
        }
        msg = Message.from_dict(data)
        assert msg.role == MessageRole.ASSISTANT
        assert msg.content is None
        assert len(msg.tool_calls) == 1
        assert msg.tool_calls[0].id == "call_123"
        assert msg.tool_calls[0].function.name == "get_weather"
        assert json.loads(msg.tool_calls[0].function.arguments) == {"city": "SF"}

    def test_message_from_dict_with_name(self):
        """Test creating message from dict with name field."""
        data = {"role": "user", "content": "Hello", "name": "Alice"}
        msg = Message.from_dict(data)
        assert msg.name == "Alice"

    def test_message_from_dict_with_tool_call_id(self):
        """Test creating message from dict with tool_call_id."""
        data = {"role": "tool", "content": "Result", "tool_call_id": "call_123"}
        msg = Message.from_dict(data)
        assert msg.role == MessageRole.TOOL
        assert msg.tool_call_id == "call_123"

    def test_message_roundtrip_serialization(self):
        """Test that message can be serialized and deserialized."""
        original = Message(role=MessageRole.USER, content="Test message", name="TestUser")
        original.function_calling_enabled = True
        dumped = original.model_dump()
        restored = Message.from_dict(dumped)
        assert restored.role == original.role
        assert restored.content == original.content
        assert restored.name == original.name
