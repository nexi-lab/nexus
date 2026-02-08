"""Unit tests for A2A protocol Pydantic models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nexus.a2a.models import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    A2AErrorData,
    A2ARequest,
    A2AResponse,
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Artifact,
    AuthScheme,
    DataPart,
    FileContent,
    FilePart,
    Message,
    PushNotificationConfig,
    SendParams,
    Task,
    TaskArtifactUpdateEvent,
    TaskIdParams,
    TaskQueryParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
    is_valid_transition,
)

# ======================================================================
# TaskState Enum
# ======================================================================


class TestTaskState:
    def test_all_states_defined(self) -> None:
        assert len(TaskState) == 7

    def test_state_values(self) -> None:
        assert TaskState.SUBMITTED.value == "submitted"
        assert TaskState.WORKING.value == "working"
        assert TaskState.INPUT_REQUIRED.value == "input-required"
        assert TaskState.COMPLETED.value == "completed"
        assert TaskState.FAILED.value == "failed"
        assert TaskState.CANCELED.value == "canceled"
        assert TaskState.REJECTED.value == "rejected"

    def test_terminal_states(self) -> None:
        assert TaskState.COMPLETED in TERMINAL_STATES
        assert TaskState.FAILED in TERMINAL_STATES
        assert TaskState.CANCELED in TERMINAL_STATES
        assert TaskState.REJECTED in TERMINAL_STATES
        assert TaskState.SUBMITTED not in TERMINAL_STATES
        assert TaskState.WORKING not in TERMINAL_STATES
        assert TaskState.INPUT_REQUIRED not in TERMINAL_STATES

    def test_valid_transitions_from_submitted(self) -> None:
        valid = VALID_TRANSITIONS[TaskState.SUBMITTED]
        assert TaskState.WORKING in valid
        assert TaskState.CANCELED in valid
        assert TaskState.REJECTED in valid
        assert TaskState.COMPLETED not in valid

    def test_valid_transitions_from_working(self) -> None:
        valid = VALID_TRANSITIONS[TaskState.WORKING]
        assert TaskState.COMPLETED in valid
        assert TaskState.FAILED in valid
        assert TaskState.CANCELED in valid
        assert TaskState.INPUT_REQUIRED in valid
        assert TaskState.SUBMITTED not in valid

    def test_valid_transitions_from_input_required(self) -> None:
        valid = VALID_TRANSITIONS[TaskState.INPUT_REQUIRED]
        assert TaskState.WORKING in valid
        assert TaskState.CANCELED in valid
        assert TaskState.FAILED in valid
        assert TaskState.COMPLETED not in valid

    def test_terminal_states_have_no_transitions(self) -> None:
        for state in TERMINAL_STATES:
            assert len(VALID_TRANSITIONS[state]) == 0

    def test_is_valid_transition_positive(self) -> None:
        assert is_valid_transition(TaskState.SUBMITTED, TaskState.WORKING)
        assert is_valid_transition(TaskState.WORKING, TaskState.COMPLETED)
        assert is_valid_transition(TaskState.INPUT_REQUIRED, TaskState.WORKING)

    def test_is_valid_transition_negative(self) -> None:
        assert not is_valid_transition(TaskState.COMPLETED, TaskState.WORKING)
        assert not is_valid_transition(TaskState.SUBMITTED, TaskState.COMPLETED)
        assert not is_valid_transition(TaskState.FAILED, TaskState.SUBMITTED)


# ======================================================================
# Part types
# ======================================================================


class TestTextPart:
    def test_basic(self) -> None:
        part = TextPart(text="hello")
        assert part.type == "text"
        assert part.text == "hello"
        assert part.metadata is None

    def test_with_metadata(self) -> None:
        part = TextPart(text="hi", metadata={"lang": "en"})
        assert part.metadata == {"lang": "en"}

    def test_serialization(self) -> None:
        part = TextPart(text="hello")
        d = part.model_dump()
        assert d["type"] == "text"
        assert d["text"] == "hello"

    def test_empty_text_allowed(self) -> None:
        part = TextPart(text="")
        assert part.text == ""

    def test_missing_text_raises(self) -> None:
        with pytest.raises(ValidationError):
            TextPart()  # type: ignore[call-arg]


class TestFilePart:
    def test_with_url(self) -> None:
        part = FilePart(file=FileContent(url="https://example.com/f.pdf", mimeType="application/pdf"))
        assert part.type == "file"
        assert part.file.url == "https://example.com/f.pdf"

    def test_with_bytes(self) -> None:
        part = FilePart(file=FileContent(bytes="dGVzdA==", name="test.txt"))
        assert part.file.bytes == "dGVzdA=="
        assert part.file.name == "test.txt"

    def test_serialization(self) -> None:
        part = FilePart(file=FileContent(url="https://example.com/f.txt"))
        d = part.model_dump()
        assert d["type"] == "file"
        assert d["file"]["url"] == "https://example.com/f.txt"


class TestDataPart:
    def test_basic(self) -> None:
        part = DataPart(data={"key": "value"})
        assert part.type == "data"
        assert part.data == {"key": "value"}

    def test_nested_data(self) -> None:
        part = DataPart(data={"nested": {"deep": [1, 2, 3]}})
        assert part.data["nested"]["deep"] == [1, 2, 3]

    def test_empty_data_allowed(self) -> None:
        part = DataPart(data={})
        assert part.data == {}


# ======================================================================
# Message and Artifact
# ======================================================================


class TestMessage:
    def test_user_message(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="hello")])
        assert msg.role == "user"
        assert len(msg.parts) == 1

    def test_agent_message(self) -> None:
        msg = Message(role="agent", parts=[TextPart(text="response")])
        assert msg.role == "agent"

    def test_invalid_role(self) -> None:
        with pytest.raises(ValidationError):
            Message(role="system", parts=[TextPart(text="oops")])  # type: ignore[arg-type]

    def test_mixed_parts(self) -> None:
        msg = Message(
            role="user",
            parts=[
                TextPart(text="see attached"),
                FilePart(file=FileContent(url="https://example.com/doc.pdf")),
                DataPart(data={"structured": True}),
            ],
        )
        assert len(msg.parts) == 3

    def test_empty_parts_allowed(self) -> None:
        msg = Message(role="user", parts=[])
        assert msg.parts == []

    def test_serialization_roundtrip(self) -> None:
        msg = Message(
            role="user",
            parts=[TextPart(text="hello"), DataPart(data={"key": 1})],
        )
        d = msg.model_dump(mode="json")
        msg2 = Message.model_validate(d)
        assert msg2.role == msg.role
        assert len(msg2.parts) == 2


class TestArtifact:
    def test_basic(self) -> None:
        art = Artifact(
            artifactId="art-1",
            parts=[TextPart(text="result")],
        )
        assert art.artifactId == "art-1"
        assert len(art.parts) == 1

    def test_with_metadata(self) -> None:
        art = Artifact(
            artifactId="art-2",
            name="Report",
            description="Analysis report",
            parts=[TextPart(text="...")],
            metadata={"format": "markdown"},
        )
        assert art.name == "Report"
        assert art.metadata == {"format": "markdown"}


# ======================================================================
# Task and TaskStatus
# ======================================================================


class TestTaskStatus:
    def test_basic(self) -> None:
        status = TaskStatus(state=TaskState.SUBMITTED)
        assert status.state == TaskState.SUBMITTED
        assert status.message is None
        assert status.timestamp is None

    def test_with_message(self) -> None:
        msg = Message(role="agent", parts=[TextPart(text="working on it")])
        status = TaskStatus(state=TaskState.WORKING, message=msg)
        assert status.message is not None

    def test_with_timestamp(self) -> None:
        now = datetime.now(UTC)
        status = TaskStatus(state=TaskState.COMPLETED, timestamp=now)
        assert status.timestamp == now


class TestTask:
    def test_minimal(self) -> None:
        task = Task(
            id="task-1",
            status=TaskStatus(state=TaskState.SUBMITTED),
        )
        assert task.id == "task-1"
        assert task.contextId is None
        assert task.artifacts == []
        assert task.history == []

    def test_full(self) -> None:
        msg = Message(role="user", parts=[TextPart(text="do something")])
        task = Task(
            id="task-2",
            contextId="ctx-1",
            status=TaskStatus(state=TaskState.WORKING),
            artifacts=[Artifact(artifactId="a1", parts=[TextPart(text="result")])],
            history=[msg],
            metadata={"priority": "high"},
        )
        assert task.contextId == "ctx-1"
        assert len(task.artifacts) == 1
        assert len(task.history) == 1

    def test_serialization_roundtrip(self) -> None:
        task = Task(
            id="t-3",
            status=TaskStatus(state=TaskState.SUBMITTED, timestamp=datetime.now(UTC)),
            history=[Message(role="user", parts=[TextPart(text="hello")])],
        )
        d = task.model_dump(mode="json")
        task2 = Task.model_validate(d)
        assert task2.id == task.id
        assert task2.status.state == TaskState.SUBMITTED


# ======================================================================
# Streaming Events
# ======================================================================


class TestTaskStatusUpdateEvent:
    def test_non_final(self) -> None:
        event = TaskStatusUpdateEvent(
            taskId="t1",
            status=TaskStatus(state=TaskState.WORKING),
        )
        assert event.final is False

    def test_final(self) -> None:
        event = TaskStatusUpdateEvent(
            taskId="t1",
            status=TaskStatus(state=TaskState.COMPLETED),
            final=True,
        )
        assert event.final is True


class TestTaskArtifactUpdateEvent:
    def test_basic(self) -> None:
        event = TaskArtifactUpdateEvent(
            taskId="t1",
            artifact=Artifact(artifactId="a1", parts=[TextPart(text="chunk")]),
        )
        assert event.taskId == "t1"


# ======================================================================
# Agent Card
# ======================================================================


class TestAgentCard:
    def test_minimal(self) -> None:
        card = AgentCard(
            name="TestAgent",
            description="A test agent",
            url="https://example.com/a2a",
            version="1.0.0",
        )
        assert card.name == "TestAgent"
        assert card.capabilities.streaming is False

    def test_full(self) -> None:
        card = AgentCard(
            name="NexusAgent",
            description="Nexus filesystem agent",
            url="https://nexus.example.com/a2a",
            version="0.7.1",
            provider=AgentProvider(organization="Nexus"),
            capabilities=AgentCapabilities(streaming=True, pushNotifications=False),
            authentication=[AuthScheme(type="apiKey")],
            skills=[
                AgentSkill(
                    id="search",
                    name="Search",
                    description="Search files",
                    tags=["search"],
                )
            ],
        )
        assert len(card.skills) == 1
        assert card.capabilities.streaming is True

    def test_serialization_excludes_none(self) -> None:
        card = AgentCard(
            name="Test",
            description="Test",
            url="http://localhost/a2a",
            version="1.0.0",
        )
        d = card.model_dump(mode="json", exclude_none=True)
        assert "provider" not in d
        assert "documentationUrl" not in d

    def test_default_modes(self) -> None:
        card = AgentCard(
            name="T", description="T", url="http://x/a2a", version="1"
        )
        assert "text/plain" in card.defaultInputModes
        assert "text/plain" in card.defaultOutputModes


class TestAgentSkill:
    def test_basic(self) -> None:
        skill = AgentSkill(id="s1", name="Skill1", description="Does things")
        assert skill.tags == []
        assert skill.examples is None

    def test_with_tags_and_examples(self) -> None:
        skill = AgentSkill(
            id="s2",
            name="Skill2",
            description="Does more",
            tags=["tag1", "tag2"],
            examples=["example 1"],
        )
        assert len(skill.tags) == 2


# ======================================================================
# JSON-RPC Envelope
# ======================================================================


class TestA2ARequest:
    def test_basic(self) -> None:
        req = A2ARequest(method="a2a.tasks.send", id="req-1")
        assert req.jsonrpc == "2.0"
        assert req.params is None

    def test_with_params(self) -> None:
        req = A2ARequest(
            method="a2a.tasks.get",
            params={"taskId": "t1"},
            id=42,
        )
        assert req.params == {"taskId": "t1"}
        assert req.id == 42

    def test_invalid_jsonrpc_version(self) -> None:
        with pytest.raises(ValidationError):
            A2ARequest(jsonrpc="1.0", method="test", id="1")  # type: ignore[arg-type]


class TestA2AResponse:
    def test_success(self) -> None:
        resp = A2AResponse.success("req-1", {"taskId": "t1"})
        assert resp.result == {"taskId": "t1"}
        assert resp.error is None

    def test_error(self) -> None:
        err = A2AErrorData(code=-32001, message="Task not found")
        resp = A2AResponse.from_error("req-1", err)
        assert resp.error is not None
        assert resp.error.code == -32001
        assert resp.result is None

    def test_error_with_data(self) -> None:
        err = A2AErrorData(
            code=-32001, message="Task not found", data={"taskId": "t1"}
        )
        resp = A2AResponse.from_error("req-1", err)
        assert resp.error.data == {"taskId": "t1"}


# ======================================================================
# Method Params
# ======================================================================


class TestSendParams:
    def test_basic(self) -> None:
        params = SendParams(
            message=Message(role="user", parts=[TextPart(text="hello")])
        )
        assert params.message.role == "user"
        assert params.configuration is None

    def test_with_metadata(self) -> None:
        params = SendParams(
            message=Message(role="user", parts=[TextPart(text="hi")]),
            metadata={"source": "test"},
        )
        assert params.metadata == {"source": "test"}


class TestTaskQueryParams:
    def test_basic(self) -> None:
        params = TaskQueryParams(taskId="t1")
        assert params.historyLength is None

    def test_with_history_length(self) -> None:
        params = TaskQueryParams(taskId="t1", historyLength=5)
        assert params.historyLength == 5


class TestTaskIdParams:
    def test_basic(self) -> None:
        params = TaskIdParams(taskId="t1")
        assert params.taskId == "t1"


class TestPushNotificationConfig:
    def test_basic(self) -> None:
        config = PushNotificationConfig(url="https://example.com/webhook")
        assert config.token is None

    def test_with_auth(self) -> None:
        config = PushNotificationConfig(
            url="https://example.com/webhook",
            authentication=AuthScheme(type="httpBearer"),
        )
        assert config.authentication is not None
