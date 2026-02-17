"""Tests for LLM metrics tracking (src/nexus/llm/metrics.py)."""

from nexus.llm.metrics import LLMMetrics, ResponseLatency, TokenUsage


class TestTokenUsage:
    """Test TokenUsage dataclass."""

    def test_token_usage_dataclass_fields(self) -> None:
        """Test TokenUsage field defaults and values."""
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.cache_write_tokens == 0

    def test_token_usage_total_tokens(self) -> None:
        """Test total_tokens property."""
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50)
        assert usage.total_tokens == 150

    def test_token_usage_addition(self) -> None:
        """Test adding two TokenUsage objects."""
        a = TokenUsage(
            prompt_tokens=100, completion_tokens=50, cache_read_tokens=10, cache_write_tokens=5
        )
        b = TokenUsage(
            prompt_tokens=200, completion_tokens=100, cache_read_tokens=20, cache_write_tokens=10
        )
        result = a + b
        assert result.prompt_tokens == 300
        assert result.completion_tokens == 150
        assert result.cache_read_tokens == 30
        assert result.cache_write_tokens == 15

class TestResponseLatency:
    """Test ResponseLatency dataclass."""

    def test_response_latency_creation(self) -> None:
        """Test creating a ResponseLatency."""
        rl = ResponseLatency(latency=1.5, response_id="resp_123")
        assert rl.latency == 1.5
        assert rl.response_id == "resp_123"
        assert rl.timestamp > 0

class TestLLMMetrics:
    """Test LLMMetrics class."""

    def test_add_cost_accumulates(self) -> None:
        """Test that cost accumulates correctly."""
        metrics = LLMMetrics(model_name="test-model")
        assert metrics.accumulated_cost == 0.0
        metrics.add_cost(0.01)
        assert metrics.accumulated_cost == 0.01
        metrics.add_cost(0.02)
        assert abs(metrics.accumulated_cost - 0.03) < 1e-9

    def test_add_token_usage_accumulates(self) -> None:
        """Test that token usage accumulates across multiple calls."""
        metrics = LLMMetrics(model_name="test-model")
        metrics.add_token_usage(prompt_tokens=100, completion_tokens=50)
        metrics.add_token_usage(
            prompt_tokens=200, completion_tokens=100, cache_read_tokens=10, cache_write_tokens=5
        )
        assert metrics.accumulated_token_usage.prompt_tokens == 300
        assert metrics.accumulated_token_usage.completion_tokens == 150
        assert metrics.accumulated_token_usage.cache_read_tokens == 10
        assert metrics.accumulated_token_usage.cache_write_tokens == 5

    def test_add_response_latency_records(self) -> None:
        """Test that response latencies are recorded."""
        metrics = LLMMetrics(model_name="test-model")
        metrics.add_response_latency(1.5, "resp_1")
        metrics.add_response_latency(2.0, "resp_2")
        assert len(metrics.response_latencies) == 2
        assert metrics.response_latencies[0].latency == 1.5
        assert metrics.response_latencies[1].response_id == "resp_2"

    def test_reset_clears_all(self) -> None:
        """Test that reset clears all metrics."""
        metrics = LLMMetrics(model_name="test-model")
        metrics.add_cost(0.05)
        metrics.add_token_usage(prompt_tokens=100, completion_tokens=50)
        metrics.add_response_latency(1.0, "resp_1")
        metrics.reset()
        assert metrics.accumulated_cost == 0.0
        assert metrics.accumulated_token_usage.prompt_tokens == 0
        assert metrics.accumulated_token_usage.completion_tokens == 0
        assert len(metrics.response_latencies) == 0

    def test_average_latency(self) -> None:
        """Test average latency calculation."""
        metrics = LLMMetrics(model_name="test-model")
        assert metrics.average_latency is None
        metrics.add_response_latency(1.0, "resp_1")
        metrics.add_response_latency(3.0, "resp_2")
        assert metrics.average_latency == 2.0

    def test_total_requests(self) -> None:
        """Test total requests count."""
        metrics = LLMMetrics(model_name="test-model")
        assert metrics.total_requests == 0
        metrics.add_response_latency(1.0, "resp_1")
        assert metrics.total_requests == 1

    def test_to_dict(self) -> None:
        """Test metrics serialization to dict."""
        metrics = LLMMetrics(model_name="test-model")
        metrics.add_cost(0.01)
        metrics.add_token_usage(prompt_tokens=100, completion_tokens=50)
        d = metrics.to_dict()
        assert d["model_name"] == "test-model"
        assert d["accumulated_cost"] == 0.01
        assert d["token_usage"]["prompt_tokens"] == 100
        assert d["token_usage"]["completion_tokens"] == 50
        assert d["token_usage"]["total_tokens"] == 150

    def test_from_dict(self) -> None:
        """Test metrics deserialization from dict."""
        data = {
            "model_name": "test-model",
            "accumulated_cost": 0.05,
            "token_usage": {
                "prompt_tokens": 200,
                "completion_tokens": 100,
                "cache_read_tokens": 10,
                "cache_write_tokens": 5,
            },
            "response_latencies": [
                {"latency": 1.5, "response_id": "resp_1", "timestamp": 1000.0},
            ],
        }
        metrics = LLMMetrics.from_dict(data)
        assert metrics.model_name == "test-model"
        assert metrics.accumulated_cost == 0.05
        assert metrics.accumulated_token_usage.prompt_tokens == 200
        assert len(metrics.response_latencies) == 1
