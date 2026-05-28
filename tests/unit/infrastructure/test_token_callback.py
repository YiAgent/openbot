"""Tests for TokenUsageCallback and agent metadata propagation."""

from __future__ import annotations

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from openbot.infrastructure.agents._token_callback import TokenUsageCallback


class TestTokenUsageCallback:
    """Verify on_llm_end extracts usage from multiple provider formats."""

    def test_usage_metadata_primary(self) -> None:
        """langchain-anthropic / langchain-openai: msg.usage_metadata."""
        cb = TokenUsageCallback()
        msg = AIMessage(
            content="hi",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
        )
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={},
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 100
        assert cb.output_tokens == 50
        assert cb.total_tokens == 150

    def test_response_metadata_usage(self) -> None:
        """Proxy models: msg.response_metadata['usage']."""
        cb = TokenUsageCallback()
        msg = AIMessage(
            content="hi",
            response_metadata={
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 80,
                    "total_tokens": 280,
                }
            },
        )
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={},
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 200
        assert cb.output_tokens == 80
        assert cb.total_tokens == 280

    def test_response_metadata_usage_prompt_completion_keys(self) -> None:
        """Some proxies use prompt_tokens/completion_tokens keys."""
        cb = TokenUsageCallback()
        msg = AIMessage(
            content="hi",
            response_metadata={
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 60,
                    "total_tokens": 180,
                }
            },
        )
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={},
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 120
        assert cb.output_tokens == 60
        assert cb.total_tokens == 180

    def test_llm_output_token_usage(self) -> None:
        """Older LangChain convention: llm_output['token_usage']."""
        cb = TokenUsageCallback()
        msg = AIMessage(content="hi")
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={
                "token_usage": {
                    "prompt_tokens": 300,
                    "completion_tokens": 100,
                    "total_tokens": 400,
                }
            },
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 300
        assert cb.output_tokens == 100
        assert cb.total_tokens == 400

    def test_llm_output_usage_key(self) -> None:
        """Some providers: llm_output['usage']."""
        cb = TokenUsageCallback()
        msg = AIMessage(content="hi")
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={
                "usage": {
                    "prompt_tokens": 150,
                    "completion_tokens": 75,
                    "total_tokens": 225,
                }
            },
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 150
        assert cb.output_tokens == 75
        assert cb.total_tokens == 225

    def test_usage_metadata_takes_precedence(self) -> None:
        """When usage_metadata exists, llm_output is not double-counted."""
        cb = TokenUsageCallback()
        msg = AIMessage(
            content="hi",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
        )
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={
                "token_usage": {
                    "prompt_tokens": 999,
                    "completion_tokens": 999,
                    "total_tokens": 999,
                }
            },
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 100
        assert cb.output_tokens == 50
        assert cb.total_tokens == 150

    def test_accumulates_across_calls(self) -> None:
        """Multiple on_llm_end calls accumulate totals."""
        cb = TokenUsageCallback()

        for _ in range(3):
            msg = AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "total_tokens": 150,
                },
            )
            result = LLMResult(
                generations=[[ChatGeneration(message=msg)]],
                llm_output={},
            )
            cb.on_llm_end(result)

        assert cb.input_tokens == 300
        assert cb.output_tokens == 150
        assert cb.total_tokens == 450

    def test_no_usage_data(self) -> None:
        """When no usage data is available, counters stay at 0."""
        cb = TokenUsageCallback()
        msg = AIMessage(content="hi")
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={},
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 0
        assert cb.output_tokens == 0
        assert cb.total_tokens == 0

    def test_model_name_tracking(self) -> None:
        """Model name is captured from llm_output."""
        cb = TokenUsageCallback()
        msg = AIMessage(content="hi")
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={"model_name": "mimo-v2.5-pro"},
        )
        cb.on_llm_end(result)
        assert cb.model_name == "mimo-v2.5-pro"

    def test_usage_property(self) -> None:
        """The .usage property returns a complete dict."""
        cb = TokenUsageCallback()
        cb.input_tokens = 100
        cb.output_tokens = 50
        cb.total_tokens = 150
        cb.total_cost = 0.005
        cb.model_name = "test-model"

        usage = cb.usage
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["total_tokens"] == 150
        assert usage["total_cost"] == 0.005
        assert usage["model_name"] == "test-model"

    def test_no_usage_metadata_falls_back_to_response_metadata(self) -> None:
        """When usage_metadata is None, falls back to response_metadata['usage']."""
        cb = TokenUsageCallback()
        # Create a message where usage_metadata will be None (not provided)
        # and response_metadata has usage data.
        msg = AIMessage(content="hi")
        # AIMessage defaults usage_metadata to None when not provided
        assert msg.usage_metadata is None
        # Manually set response_metadata with usage data
        msg.response_metadata = {
            "usage": {
                "input_tokens": 200,
                "output_tokens": 80,
                "total_tokens": 280,
            }
        }
        result = LLMResult(
            generations=[[ChatGeneration(message=msg)]],
            llm_output={},
        )
        cb.on_llm_end(result)
        assert cb.input_tokens == 200
        assert cb.output_tokens == 80
        assert cb.total_tokens == 280


class TestAgentMetadataStore:
    """Verify _extract_and_store_metadata and pop_agent_metadata work together."""

    def test_extract_and_pop(self) -> None:
        from openbot.evaluation.runner import (
            _agent_metadata_store,
            _extract_and_store_metadata,
            pop_agent_metadata,
        )

        # Create a mock responder with a _runtime that has metadata
        class MockRuntime:
            def __init__(self) -> None:
                self._last_agent_metadata = {
                    "token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "total_tokens": 150,
                    },
                    "messages": [{"role": "assistant", "content": "hello"}],
                }

        class MockResponder:
            def __init__(self) -> None:
                self._runtime = MockRuntime()

        run_id = "test-run-123"
        # Clean up first
        _agent_metadata_store.pop(run_id, None)

        _extract_and_store_metadata(MockResponder(), run_id)
        assert run_id in _agent_metadata_store

        meta = pop_agent_metadata(run_id)
        assert meta.token_usage["input_tokens"] == 100
        assert meta.token_usage["output_tokens"] == 50
        assert len(meta.messages) == 1
        assert meta.messages[0]["role"] == "assistant"

        # pop_agent_metadata removes the entry
        assert run_id not in _agent_metadata_store

    def test_pop_missing_returns_empty(self) -> None:
        from openbot.evaluation.runner import pop_agent_metadata

        meta = pop_agent_metadata("nonexistent-id")
        assert meta.token_usage == {}
        assert meta.messages == []

    def test_extract_no_run_id(self) -> None:
        """No run_id means no-op."""
        from openbot.evaluation.runner import (
            _agent_metadata_store,
            _extract_and_store_metadata,
        )

        class MockResponder:
            _runtime = type("R", (), {"_last_agent_metadata": {"token_usage": {}}})()

        before = len(_agent_metadata_store)
        _extract_and_store_metadata(MockResponder(), None)
        assert len(_agent_metadata_store) == before

    def test_extract_no_runtime(self) -> None:
        """Responder without _runtime is a no-op."""
        from openbot.evaluation.runner import (
            _agent_metadata_store,
            _extract_and_store_metadata,
        )

        class MockResponder:
            pass

        before = len(_agent_metadata_store)
        _extract_and_store_metadata(MockResponder(), "some-id")
        assert len(_agent_metadata_store) == before


class TestLangSmithHookUsageFallback:
    """Verify the LangSmith hook's fallback reads agent_token_usage correctly."""

    def test_aggregate_usage_from_agent_token_usage(self) -> None:
        from evals.runtime.langsmith_hook import _aggregate_usage

        # No primary sources
        result = _aggregate_usage(output_usage=None, model_usage={})
        assert result is None  # no data at all

    def test_aggregate_usage_with_model_usage(self) -> None:
        """Primary path: sample.model_usage populated."""
        from unittest.mock import MagicMock

        from evals.runtime.langsmith_hook import _aggregate_usage

        mock_usage = MagicMock()
        mock_usage.input_tokens = 100
        mock_usage.output_tokens = 50
        mock_usage.total_tokens = 150
        mock_usage.total_cost = None

        result = _aggregate_usage(output_usage=None, model_usage={"test-model": mock_usage})
        assert result is not None
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["total_tokens"] == 150
