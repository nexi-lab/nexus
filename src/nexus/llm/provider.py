"""Base LLM provider interface and implementation using litellm."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import time
import warnings
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Iterator
from functools import partial, wraps
from typing import Any, cast

import litellm
from litellm import PromptTokensDetails
from litellm import acompletion as litellm_acompletion
from litellm import completion as litellm_completion
from litellm import completion_cost as litellm_completion_cost
from litellm.exceptions import RateLimitError
from litellm.types.utils import CostPerToken, ModelInfo, ModelResponse, Usage
from litellm.utils import create_pretrained_tokenizer

from nexus.llm.cancellation import AsyncCancellationToken
from nexus.llm.config import LLMConfig
from nexus.llm.exceptions import LLMCancellationError, LLMNoResponseError
from nexus.llm.message import Message
from nexus.llm.metrics import LLMMetrics

# Suppress litellm warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")

# Models that support prompt caching (Anthropic Claude)
CACHE_PROMPT_SUPPORTED_MODELS = [
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-20240620",
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307",
    "claude-3-opus-20240229",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
]

# Models that support function calling
FUNCTION_CALLING_SUPPORTED_MODELS = [
    "claude-3-7-sonnet",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet",
    "claude-3-5-sonnet-20240620",
    "claude-3-5-sonnet-20241022",
    "claude-3.5-haiku",
    "claude-3-5-haiku-20241022",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "gpt-4o-mini",
    "gpt-4o",
    "o1-2024-12-17",
    "o3-mini-2025-01-31",
    "o3-mini",
    "o3",
    "o3-2025-04-16",
    "o4-mini",
    "o4-mini-2025-04-16",
    "gemini-2.5-pro",
    "gpt-4.1",
]

# Models that support reasoning effort parameter
REASONING_EFFORT_SUPPORTED_MODELS = [
    "o1-2024-12-17",
    "o1",
    "o3",
    "o3-2025-04-16",
    "o3-mini-2025-01-31",
    "o3-mini",
    "o4-mini",
    "o4-mini-2025-04-16",
]

# Retry exceptions
LLM_RETRY_EXCEPTIONS = (
    RateLimitError,
    litellm.Timeout,
    litellm.InternalServerError,
    LLMNoResponseError,
)


def retry_decorator(
    num_retries: int = 3,
    retry_exceptions: tuple[type[Exception], ...] = LLM_RETRY_EXCEPTIONS,
    retry_min_wait: float = 4.0,
    retry_max_wait: float = 10.0,
    retry_multiplier: float = 2.0,
) -> Callable:
    """Decorator for retrying functions with exponential backoff."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            wait_time = retry_min_wait

            for attempt in range(num_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retry_exceptions as e:
                    last_exception = e
                    if attempt < num_retries:
                        time.sleep(wait_time)
                        wait_time = min(wait_time * retry_multiplier, retry_max_wait)
                    else:
                        raise

            if last_exception:
                raise last_exception

        return wrapper

    return decorator


def async_retry_decorator(
    num_retries: int = 3,
    retry_exceptions: tuple[type[Exception], ...] = LLM_RETRY_EXCEPTIONS,
    retry_min_wait: float = 4.0,
    retry_max_wait: float = 10.0,
    retry_multiplier: float = 2.0,
) -> Callable:
    """Decorator for retrying async functions with exponential backoff."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception = None
            wait_time = retry_min_wait

            for attempt in range(num_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retry_exceptions as e:
                    last_exception = e
                    if attempt < num_retries:
                        await asyncio.sleep(wait_time)
                        wait_time = min(wait_time * retry_multiplier, retry_max_wait)
                    else:
                        raise

            if last_exception:
                raise last_exception

        return wrapper

    return decorator


class LLMResponse(ABC):
    """Response from an LLM completion."""

    @property
    @abstractmethod
    def content(self) -> str | None:
        """Response content text."""
        pass

    @property
    @abstractmethod
    def tool_calls(self) -> list[dict[str, Any]] | None:
        """Tool/function calls made by the LLM."""
        pass

    @property
    @abstractmethod
    def usage(self) -> dict[str, Any]:
        """Token usage information."""
        pass

    @property
    @abstractmethod
    def cost(self) -> float:
        """Cost of the request in USD."""
        pass

    @property
    @abstractmethod
    def response_id(self) -> str:
        """Unique ID for this response."""
        pass

    @property
    @abstractmethod
    def raw_response(self) -> Any:
        """Raw response from the provider."""
        pass


class LiteLLMResponse(LLMResponse):
    """Response wrapper for litellm responses."""

    def __init__(self, response: ModelResponse, calculated_cost: float):
        self._response = response
        self._calculated_cost = calculated_cost

    @property
    def content(self) -> str | None:
        if not self._response.get("choices") or len(self._response["choices"]) < 1:
            return None
        content = self._response["choices"][0]["message"].get("content")
        return str(content) if content is not None else None

    @property
    def tool_calls(self) -> list[dict[str, Any]] | None:
        if not self._response.get("choices") or len(self._response["choices"]) < 1:
            return None
        tool_calls = self._response["choices"][0]["message"].get("tool_calls")
        return list(tool_calls) if tool_calls is not None else None

    @property
    def usage(self) -> dict[str, Any]:
        usage = self._response.get("usage", {})
        return dict(usage) if usage else {}

    @property
    def cost(self) -> float:
        return self._calculated_cost

    @property
    def response_id(self) -> str:
        response_id = self._response.get("id", "unknown")
        return str(response_id)

    @property
    def raw_response(self) -> ModelResponse:
        return self._response


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, config: LLMConfig, metrics: LLMMetrics | None = None):
        """Initialize the provider.

        Args:
            config: LLM configuration
            metrics: Optional metrics tracker
        """
        self.config = copy.deepcopy(config)
        self.metrics = metrics if metrics is not None else LLMMetrics(model_name=config.model)
        self.model_info: ModelInfo | None = None
        self._vision_supported: bool = False
        self._function_calling_active: bool = False
        self._token_count_cache: dict[str, int] = {}
        self._token_count_cache_max_size = 1000
        self.cost_metric_supported = True

    @abstractmethod
    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> LLMResponse:
        """Send a completion request.

        Args:
            messages: List of messages
            tools: Optional list of tools for function calling
            **kwargs: Additional provider-specific parameters

        Returns:
            LLMResponse object
        """
        pass

    @abstractmethod
    async def complete_async(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> LLMResponse:
        """Send an async completion request.

        Args:
            messages: List of messages
            tools: Optional list of tools for function calling
            **kwargs: Additional provider-specific parameters

        Returns:
            LLMResponse object
        """
        pass

    @abstractmethod
    def stream(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> Iterator[str]:
        """Stream a completion response.

        Args:
            messages: List of messages
            tools: Optional list of tools for function calling
            **kwargs: Additional provider-specific parameters

        Yields:
            Response chunks as strings
        """
        pass

    @abstractmethod
    def stream_async(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> AsyncIterator[str]:
        """Stream an async completion response.

        Args:
            messages: List of messages
            tools: Optional list of tools for function calling
            **kwargs: Additional provider-specific parameters

        Yields:
            Response chunks as strings
        """
        pass

    @abstractmethod
    def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens in messages.

        Args:
            messages: List of messages

        Returns:
            Token count
        """
        pass

    def vision_is_active(self) -> bool:
        """Check if vision capabilities are enabled."""
        return not self.config.disable_vision and self._vision_supported

    def is_function_calling_active(self) -> bool:
        """Check if function calling is enabled."""
        return self._function_calling_active

    def is_caching_prompt_active(self) -> bool:
        """Check if prompt caching is supported and enabled."""
        return self.config.caching_prompt and (
            self.config.model in CACHE_PROMPT_SUPPORTED_MODELS
            or self.config.model.split("/")[-1] in CACHE_PROMPT_SUPPORTED_MODELS
        )

    def reset_metrics(self) -> None:
        """Reset metrics."""
        self.metrics.reset()
        self._token_count_cache.clear()

    @classmethod
    def from_config(cls, config: LLMConfig) -> LLMProvider:
        """Create a provider from config.

        Args:
            config: LLM configuration

        Returns:
            Appropriate LLM provider instance
        """
        # Use LiteLLM provider as default (supports all providers)
        return LiteLLMProvider(config)


class LiteLLMProvider(LLMProvider):
    """LLM provider using litellm for multi-provider support."""

    def __init__(self, config: LLMConfig, metrics: LLMMetrics | None = None):
        """Initialize the litellm provider."""
        super().__init__(config, metrics)

        # Initialize tokenizer
        if self.config.custom_tokenizer is not None:
            self.tokenizer = create_pretrained_tokenizer(self.config.custom_tokenizer)
        else:
            self.tokenizer = None

        # Initialize model info
        self._init_model_info()

        # Set up completion function
        kwargs: dict[str, Any] = {
            "temperature": self.config.temperature,
            "max_completion_tokens": self.config.max_output_tokens,
        }

        # Handle reasoning effort for o1/o3 models
        if (
            self.config.model.lower() in REASONING_EFFORT_SUPPORTED_MODELS
            or self.config.model.split("/")[-1] in REASONING_EFFORT_SUPPORTED_MODELS
        ):
            if self.config.reasoning_effort:
                kwargs["reasoning_effort"] = self.config.reasoning_effort
            kwargs.pop("temperature", None)  # Not supported for reasoning models

        self._completion_partial = partial(
            litellm_completion,
            model=self.config.model,
            api_key=self.config.api_key.get_secret_value() if self.config.api_key else None,
            base_url=self.config.base_url,
            api_version=self.config.api_version,
            custom_llm_provider=self.config.custom_llm_provider,
            timeout=self.config.timeout,
            top_p=self.config.top_p,
            drop_params=self.config.drop_params,
            seed=self.config.seed,
            **kwargs,
        )

        # Set up async completion function
        self._acompletion_partial = partial(
            litellm_acompletion,
            model=self.config.model,
            api_key=self.config.api_key.get_secret_value() if self.config.api_key else None,
            base_url=self.config.base_url,
            api_version=self.config.api_version,
            custom_llm_provider=self.config.custom_llm_provider,
            timeout=self.config.timeout,
            top_p=self.config.top_p,
            drop_params=self.config.drop_params,
            seed=self.config.seed,
            **kwargs,
        )

        # Track active async tasks for cleanup
        self._active_tasks: set[asyncio.Task] = set()

    def _init_model_info(self) -> None:
        """Initialize model information."""
        with contextlib.suppress(Exception):
            self.model_info = litellm.get_model_info(self.config.model)

        # Try without prefix if that didn't work
        if not self.model_info and "/" in self.config.model:
            with contextlib.suppress(Exception):
                self.model_info = litellm.get_model_info(self.config.model.split("/")[-1])

        # Configure max tokens
        if self.config.max_input_tokens is None:
            if self.model_info and "max_input_tokens" in self.model_info:
                self.config.max_input_tokens = self.model_info["max_input_tokens"]
            else:
                self.config.max_input_tokens = 4096

        if self.config.max_output_tokens is None:
            self.config.max_output_tokens = 4096
            if self.model_info:
                if "max_output_tokens" in self.model_info:
                    self.config.max_output_tokens = self.model_info["max_output_tokens"]
                elif "max_tokens" in self.model_info:
                    self.config.max_output_tokens = self.model_info["max_tokens"]

        # Configure function calling
        model_name_supported = (
            self.config.model in FUNCTION_CALLING_SUPPORTED_MODELS
            or self.config.model.split("/")[-1] in FUNCTION_CALLING_SUPPORTED_MODELS
            or any(m in self.config.model for m in FUNCTION_CALLING_SUPPORTED_MODELS)
        )

        if self.config.native_tool_calling is None:
            self._function_calling_active = model_name_supported
        elif self.config.native_tool_calling is False:
            self._function_calling_active = False
        else:
            self._function_calling_active = litellm.supports_function_calling(
                model=self.config.model
            )

        # Configure vision
        self._vision_supported = bool(
            litellm.supports_vision(self.config.model)
            or litellm.supports_vision(self.config.model.split("/")[-1])
            or (self.model_info is not None and self.model_info.get("supports_vision", False))
        )

    @retry_decorator(num_retries=3)
    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> LLMResponse:
        """Send a completion request."""
        # Format messages
        formatted_messages = self._format_messages(messages)

        # Prepare kwargs
        call_kwargs = kwargs.copy()
        if tools:
            call_kwargs["tools"] = tools
            if "tool_choice" not in call_kwargs:
                call_kwargs["tool_choice"] = "auto"

        # Set litellm modify_params
        litellm.modify_params = self.config.modify_params

        # Record start time
        start_time = time.time()

        # Make request
        response: ModelResponse = self._completion_partial(
            messages=formatted_messages, **call_kwargs
        )

        # Calculate latency
        latency = time.time() - start_time
        response_id = response.get("id", "unknown")
        self.metrics.add_response_latency(latency, response_id)

        # Calculate cost and update metrics
        cost = self._calculate_cost(response)

        # Update token usage
        usage: Usage | None = response.get("usage")
        if usage:
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)

            # Handle cache tokens (Anthropic)
            prompt_tokens_details: PromptTokensDetails | None = usage.get("prompt_tokens_details")
            cache_hit_tokens = (
                prompt_tokens_details.cached_tokens
                if prompt_tokens_details and prompt_tokens_details.cached_tokens
                else 0
            )
            model_extra = usage.get("model_extra", {})
            cache_write_tokens = model_extra.get("cache_creation_input_tokens", 0)

            self.metrics.add_token_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_hit_tokens,
                cache_write_tokens=cache_write_tokens,
                response_id=response_id,
            )

        return LiteLLMResponse(response, cost)

    async def complete_async(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        cancellation_token: AsyncCancellationToken | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send an async completion request with cancellation support.

        Args:
            messages: List of messages
            tools: Optional list of tools for function calling
            cancellation_token: Optional cancellation token for request cancellation
            **kwargs: Additional provider-specific parameters

        Returns:
            LLMResponse object

        Raises:
            LLMCancellationError: If request was cancelled
        """
        # Format messages
        formatted_messages = self._format_messages(messages)

        # Prepare kwargs
        call_kwargs = kwargs.copy()
        if tools:
            call_kwargs["tools"] = tools
            if "tool_choice" not in call_kwargs:
                call_kwargs["tool_choice"] = "auto"

        # Set litellm modify_params
        litellm.modify_params = self.config.modify_params

        # Create cancellation event
        cancel_event = asyncio.Event()
        completion_task: asyncio.Task | None = None

        async def check_cancellation() -> None:
            """Periodically check for cancellation requests."""
            try:
                while not cancel_event.is_set():
                    # Check cancellation token if provided
                    if cancellation_token and await cancellation_token.is_cancelled_async():
                        if completion_task and not completion_task.done():
                            completion_task.cancel()
                        cancel_event.set()
                        return

                    await asyncio.sleep(self.config.cancellation_check_interval)
            except asyncio.CancelledError:
                # Clean cancellation
                pass

        # Start cancellation checker
        check_task = asyncio.create_task(check_cancellation())
        self._active_tasks.add(check_task)
        check_task.add_done_callback(self._active_tasks.discard)

        try:
            # Record start time
            start_time = time.time()

            # Create completion task with retry
            @async_retry_decorator(
                num_retries=self.config.num_retries,
                retry_exceptions=LLM_RETRY_EXCEPTIONS,
                retry_min_wait=self.config.retry_min_wait,
                retry_max_wait=self.config.retry_max_wait,
                retry_multiplier=self.config.retry_multiplier,
            )
            async def make_completion() -> ModelResponse:
                result = await self._acompletion_partial(messages=formatted_messages, **call_kwargs)
                return cast(ModelResponse, result)

            completion_task = asyncio.create_task(make_completion())
            self._active_tasks.add(completion_task)
            completion_task.add_done_callback(self._active_tasks.discard)

            # Wait for either completion or cancellation
            done, pending = await asyncio.wait(
                [completion_task, check_task], return_when=asyncio.FIRST_COMPLETED
            )

            # Handle results
            if completion_task in done:
                # Normal completion
                cancel_event.set()
                response: ModelResponse = await completion_task

                # Calculate latency
                latency = time.time() - start_time
                response_id = response.get("id", "unknown")
                self.metrics.add_response_latency(latency, response_id)

                # Calculate cost and update metrics
                cost = self._calculate_cost(response)

                # Update token usage
                usage: Usage | None = response.get("usage")
                if usage:
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)

                    # Handle cache tokens (Anthropic)
                    prompt_tokens_details: PromptTokensDetails | None = usage.get(
                        "prompt_tokens_details"
                    )
                    cache_hit_tokens = (
                        prompt_tokens_details.cached_tokens
                        if prompt_tokens_details and prompt_tokens_details.cached_tokens
                        else 0
                    )
                    model_extra = usage.get("model_extra", {})
                    cache_write_tokens = model_extra.get("cache_creation_input_tokens", 0)

                    self.metrics.add_token_usage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        cache_read_tokens=cache_hit_tokens,
                        cache_write_tokens=cache_write_tokens,
                        response_id=response_id,
                    )

                return LiteLLMResponse(response, cost)
            else:
                # Cancellation occurred
                raise LLMCancellationError("LLM request was cancelled")

        except asyncio.CancelledError:
            raise LLMCancellationError("LLM request was cancelled") from None
        except LLMCancellationError:
            raise
        except Exception:
            raise
        finally:
            # Clean up tasks
            cancel_event.set()

            # Cancel any pending tasks
            for task in [check_task, completion_task]:
                if task and not task.done():
                    task.cancel()

            # Wait for cleanup with timeout
            pending_tasks = [t for t in [check_task, completion_task] if t and not t.done()]
            if pending_tasks:
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.gather(*pending_tasks, return_exceptions=True), timeout=0.1
                    )

    def stream(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> Iterator[str]:
        """Stream a completion response."""
        # Format messages
        formatted_messages = self._format_messages(messages)

        # Prepare kwargs
        call_kwargs = kwargs.copy()
        call_kwargs["stream"] = True
        if tools:
            call_kwargs["tools"] = tools

        # Make streaming request
        response = self._completion_partial(messages=formatted_messages, **call_kwargs)

        for chunk in response:
            if chunk.choices and len(chunk.choices) > 0:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content

    async def stream_async(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        cancellation_token: AsyncCancellationToken | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream an async completion response with cancellation support.

        Args:
            messages: List of messages
            tools: Optional list of tools for function calling
            cancellation_token: Optional cancellation token for request cancellation
            **kwargs: Additional provider-specific parameters

        Yields:
            Response chunks as strings

        Raises:
            LLMCancellationError: If streaming was cancelled
        """
        # Format messages
        formatted_messages = self._format_messages(messages)

        # Prepare kwargs
        call_kwargs = kwargs.copy()
        call_kwargs["stream"] = True
        if tools:
            call_kwargs["tools"] = tools

        # Check cancellation before starting
        if cancellation_token and await cancellation_token.is_cancelled_async():
            raise LLMCancellationError("LLM request was cancelled before streaming started")

        try:
            # Make streaming request
            response_coro = self._acompletion_partial(messages=formatted_messages, **call_kwargs)

            # Await the coroutine to get the async generator
            response = await response_coro

            # Stream chunks with periodic cancellation checks
            last_check_time = time.time()
            check_interval = self.config.cancellation_check_interval

            async for chunk in response:
                # Periodic cancellation check
                current_time = time.time()
                if current_time - last_check_time >= check_interval:
                    if cancellation_token and await cancellation_token.is_cancelled_async():
                        raise LLMCancellationError("LLM streaming was cancelled")
                    last_check_time = current_time

                # Yield content from chunk
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield delta.content

        except asyncio.CancelledError:
            raise LLMCancellationError("LLM streaming was cancelled") from None
        except LLMCancellationError:
            raise
        except Exception:
            raise

    def count_tokens(self, messages: list[Message]) -> int:
        """Count tokens in messages."""
        # Format messages for token counting
        formatted_messages = self._format_messages(messages)

        # Create cache key
        try:
            cache_key = hashlib.md5(
                (self.config.model + str(formatted_messages)).encode()
            ).hexdigest()
            if cache_key in self._token_count_cache:
                return self._token_count_cache[cache_key]
        except (TypeError, AttributeError, ValueError):
            # Cache key creation failed (invalid input types)
            cache_key = None

        # Count tokens
        try:
            token_count = int(
                litellm.token_counter(
                    model=self.config.model,
                    messages=formatted_messages,
                    custom_tokenizer=self.tokenizer,
                )
            )

            # Cache result
            if cache_key:
                if len(self._token_count_cache) >= self._token_count_cache_max_size:
                    # Remove oldest entry
                    self._token_count_cache.pop(next(iter(self._token_count_cache)))
                self._token_count_cache[cache_key] = token_count

            return token_count
        except (ValueError, TypeError, AttributeError) as e:
            # Token counting failed - return 0 as fallback
            import logging

            logging.warning(f"Token counting failed for model {self.config.model}: {e}")
            return 0

    def _format_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Format messages for the provider."""
        # Set serialization flags
        for msg in messages:
            msg.cache_enabled = self.is_caching_prompt_active()
            msg.vision_enabled = self.vision_is_active()
            msg.function_calling_enabled = self.is_function_calling_active()

        return [msg.model_dump() for msg in messages]

    def _calculate_cost(self, response: ModelResponse) -> float:
        """Calculate cost of response."""
        if not self.cost_metric_supported:
            return 0.0

        # Try custom costs first
        extra_kwargs = {}
        if self.config.input_cost_per_token and self.config.output_cost_per_token:
            cost_per_token = CostPerToken(
                input_cost_per_token=self.config.input_cost_per_token,
                output_cost_per_token=self.config.output_cost_per_token,
            )
            extra_kwargs["custom_cost_per_token"] = cost_per_token

        try:
            cost = litellm_completion_cost(completion_response=response, **extra_kwargs)  # type: ignore[arg-type]
            if cost is not None:
                self.metrics.add_cost(float(cost))
                return float(cost)
        except (KeyError, ValueError, TypeError, AttributeError):
            # Cost calculation failed for this model - try alternate method
            pass

        # Try with base model name
        if "/" in self.config.model:
            try:
                model_name = "/".join(self.config.model.split("/")[1:])
                cost = litellm_completion_cost(
                    completion_response=response,
                    model=model_name,
                    **extra_kwargs,  # type: ignore[arg-type]
                )
                if cost is not None:
                    self.metrics.add_cost(float(cost))
                    return float(cost)
            except (KeyError, ValueError, TypeError, AttributeError):
                # Cost calculation failed for base model name too
                pass

        self.cost_metric_supported = False
        return 0.0

    async def cleanup(self) -> None:
        """Clean up any active async tasks.

        Should be called when done using the provider to ensure proper cleanup.
        """
        if self._active_tasks and len(self._active_tasks) > 0:
            # Cancel all active tasks
            for task in list(self._active_tasks):
                if not task.done():
                    task.cancel()

            # Wait for cancellation with optimized timeout
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*self._active_tasks, return_exceptions=True), timeout=0.5
                )

            self._active_tasks.clear()
