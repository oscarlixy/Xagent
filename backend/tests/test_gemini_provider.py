import asyncio
import json

import httpx
import pytest

from x_digest.ai.contracts import DigestOutput
from x_digest.ai.digest_prompt import DIGEST_SYSTEM_INSTRUCTIONS
from x_digest.ai.gemini import GeminiInteractionsSummarizer
from x_digest.ai.openai import LLMProviderError
from x_digest.ai.providers import build_summarizer
from x_digest.config import Settings
from x_digest.services.digest_synthesis import DigestCandidate, DigestSource
from x_digest.services.summarization import NormalizedContent


def _digest_candidates() -> tuple[DigestCandidate, ...]:
    return (
        DigestCandidate(
            id="unit-a",
            summary_id="summary-a",
            source_ids=("post-a1", "post-a2"),
            summary="Agent quality drifts over long runs.",
            key_points=("Review layers do not fully prevent drift.",),
            topics=("agents",),
            importance=4,
            language="en",
            sources=(
                DigestSource("post-a1", "alice", "part one", "https://x.com/alice/status/1"),
                DigestSource("post-a2", "alice", "part two", "https://x.com/alice/status/2"),
            ),
        ),
    )


def _completed_response() -> dict[str, object]:
    return {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "summary": "Gemini 发布了新模型。",
                                "key_points": ["面向高吞吐摘要"],
                                "topics": ["AI", "模型"],
                                "importance": 4,
                                "language": "zh",
                                "source_ids": ["post-1"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            }
        ],
        "usage": {
            "total_input_tokens": 20,
            "total_output_tokens": 8,
            "total_thought_tokens": 2,
            "total_tokens": 30,
        },
    }


def test_gemini_summarizer_uses_v1_interactions_structured_output() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers["x-goog-api-key"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=_completed_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiInteractionsSummarizer(
        api_key="test-key",
        http_client=client,
        model="gemini-3.5-flash-lite",
        thinking_level="low",
    )

    result = asyncio.run(
        provider.summarize(
            NormalizedContent(
                source_ids=("post-1",),
                text="Gemini 发布了新模型。",
                link_text="链接内容",
            )
        )
    )
    asyncio.run(client.aclose())

    assert captured["url"] == "https://generativelanguage.googleapis.com/v1/interactions"
    assert captured["api_key"] == "test-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gemini-3.5-flash-lite"
    assert payload["store"] is False
    assert payload["system_instruction"]
    assert payload["generation_config"] == {
        "max_output_tokens": 1200,
        "thinking_level": "low",
    }
    assert payload["response_format"]["type"] == "text"
    assert payload["response_format"]["mime_type"] == "application/json"
    assert payload["response_format"]["schema"]["additionalProperties"] is False
    provider_input = json.loads(payload["input"])
    assert provider_input == {
        "source_ids": ["post-1"],
        "post_text": "Gemini 发布了新模型。",
        "linked_content": "链接内容",
    }
    assert result.output.source_ids == ["post-1"]
    assert result.token_usage == {
        "total_input_tokens": 20,
        "total_output_tokens": 8,
        "total_thought_tokens": 2,
        "total_tokens": 30,
    }


def test_gemini_summarizer_uses_last_model_output_and_joins_text_blocks() -> None:
    body = _completed_response()
    body["steps"] = [
        {"type": "model_output", "content": [{"type": "text", "text": "ignored"}]},
        {
            "type": "model_output",
            "content": [
                {"type": "text", "text": '{"summary":"ok","key_points":["ok"],'},
                {"type": "text", "text": '"topics":["ai"],"importance":3,'},
                {
                    "type": "text",
                    "text": '"language":"en","source_ids":["post-1"]}',
                },
            ],
        },
    ]
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    )
    provider = GeminiInteractionsSummarizer(api_key="test-key", http_client=client)

    result = asyncio.run(
        provider.summarize(NormalizedContent(source_ids=("post-1",), text="text"))
    )
    asyncio.run(client.aclose())

    assert result.output.summary == "ok"


@pytest.mark.parametrize(
    "body",
    [
        {"status": "incomplete", "steps": []},
        {"status": "completed", "steps": []},
        {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "{}"}]}],
        },
    ],
)
def test_gemini_summarizer_rejects_incomplete_or_invalid_responses(
    body: dict[str, object],
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    )
    provider = GeminiInteractionsSummarizer(api_key="test-key", http_client=client)

    with pytest.raises(LLMProviderError):
        asyncio.run(
            provider.summarize(NormalizedContent(source_ids=("post-1",), text="text"))
        )
    asyncio.run(client.aclose())


def test_gemini_summarizer_redacts_provider_error_body() -> None:
    secret = "provider-secret-body"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, text=secret))
    )
    provider = GeminiInteractionsSummarizer(api_key="test-key", http_client=client)

    with pytest.raises(LLMProviderError) as raised:
        asyncio.run(
            provider.summarize(NormalizedContent(source_ids=("post-1",), text="text"))
        )
    asyncio.run(client.aclose())

    assert secret not in str(raised.value)


def test_gemini_summarizer_retries_retryable_failures_and_honors_retry_after() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if attempts == 2:
            return httpx.Response(503)
        return httpx.Response(200, json=_completed_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiInteractionsSummarizer(
        api_key="test-key", http_client=client, sleeper=sleeper, max_attempts=3
    )

    asyncio.run(provider.summarize(NormalizedContent(source_ids=("post-1",), text="text")))
    asyncio.run(client.aclose())

    assert attempts == 3
    assert delays == [2.0, 1.0]


def test_gemini_summarizer_truncates_combined_input_deterministically() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completed_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiInteractionsSummarizer(api_key="test-key", http_client=client)
    asyncio.run(
        provider.summarize(
            NormalizedContent(
                source_ids=("post-1",), text="a" * 100_000, link_text="b" * 100_000
            )
        )
    )
    asyncio.run(client.aclose())

    provider_input = json.loads(captured["input"])  # type: ignore[arg-type]
    assert len(provider_input["post_text"] + provider_input["linked_content"]) == 120_000
    assert provider_input["post_text"] == "a" * 100_000
    assert provider_input["linked_content"] == "b" * 20_000


def test_build_summarizer_selects_gemini_with_provider_specific_settings() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="gemini",
        gemini_api_key="gemini-key",
        gemini_model="gemini-3.5-flash-lite",
        gemini_base_url="https://generativelanguage.googleapis.com/v1",
        gemini_thinking_level="minimal",
    )

    provider = build_summarizer(settings)

    assert isinstance(provider, GeminiInteractionsSummarizer)
    assert provider.model_name == "gemini-3.5-flash-lite"


def test_build_summarizer_requires_gemini_api_key() -> None:
    settings = Settings(_env_file=None, llm_provider="gemini", gemini_api_key=None)

    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        build_summarizer(settings)


def test_gemini_synthesizes_event_digest_with_strict_source_payload() -> None:
    captured: dict[str, object] = {}
    digest_body = {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "overview": "今日重点是 Agent 可靠性。",
                                "topics": [
                                    {
                                        "name": "Agent",
                                        "events": [
                                            {
                                                "title": "长任务出现语言漂移",
                                                "what_happened": "长任务中的语言质量下降。",
                                                "why_it_matters": "这会影响 Agent 稳定性。",
                                                "event_type": "analysis",
                                                "importance": 4,
                                                "source_ids": ["post-a1", "post-a2"],
                                            }
                                        ],
                                    }
                                ],
                                "excluded_source_ids": [],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            }
        ],
        "usage": {"total_tokens": 42},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured["api_key"] = request.headers["x-goog-api-key"]
        captured["payload"] = json.loads(request.content)
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=digest_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = GeminiInteractionsSummarizer(
        api_key="test-key",
        http_client=client,
        model="gemini-3.5-flash-lite",
        thinking_level="minimal",
        timeout_seconds=19,
        max_output_tokens=950,
    )

    result = asyncio.run(provider.synthesize_digest(_digest_candidates()))
    asyncio.run(client.aclose())

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert captured["api_key"] == "test-key"
    assert payload["system_instruction"] == DIGEST_SYSTEM_INSTRUCTIONS
    assert payload["generation_config"] == {
        "max_output_tokens": 950,
        "thinking_level": "minimal",
    }
    assert payload["response_format"] == {
        "type": "text",
        "mime_type": "application/json",
        "schema": DigestOutput.model_json_schema(),
    }
    assert json.loads(payload["input"])["candidates"][0]["source_ids"] == [
        "post-a1",
        "post-a2",
    ]
    assert captured["timeout"] == {"connect": 19.0, "read": 19.0, "write": 19.0, "pool": 19.0}
    assert result.output.topics[0].events[0].source_ids == ["post-a1", "post-a2"]
    assert result.token_usage == {"total_tokens": 42}


def test_gemini_digest_rejects_invalid_structured_response() -> None:
    body = {
        "status": "completed",
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": "{}"}]}
        ],
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    )
    provider = GeminiInteractionsSummarizer(api_key="test-key", http_client=client)

    with pytest.raises(LLMProviderError, match="invalid structured response"):
        asyncio.run(provider.synthesize_digest(_digest_candidates()))
    asyncio.run(client.aclose())
