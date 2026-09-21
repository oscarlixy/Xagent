import asyncio
import json

import httpx
import pytest

from x_digest.ai.contracts import DigestOutput
from x_digest.ai.digest_prompt import DIGEST_SYSTEM_INSTRUCTIONS
from x_digest.ai.openai import LLMProviderError, OpenAIResponsesSummarizer
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


def test_openai_summarizer_uses_responses_structured_output() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output_text": json.dumps(
                    {
                        "summary": "OpenAI 发布了新模型。",
                        "key_points": ["面向高吞吐摘要"],
                        "topics": ["AI", "模型"],
                        "importance": 4,
                        "language": "zh",
                        "source_ids": ["post-1"],
                    },
                    ensure_ascii=False,
                ),
                "usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesSummarizer(
        api_key="test-key",
        http_client=client,
        model="gpt-5.6-luna",
        reasoning_effort="low",
    )

    output = asyncio.run(
        provider.summarize(
            NormalizedContent(source_ids=("post-1",), text="OpenAI 发布了新模型。")
        )
    )
    asyncio.run(client.aclose())

    assert output.output.source_ids == ["post-1"]
    assert output.token_usage == {
        "input_tokens": 20,
        "output_tokens": 8,
        "total_tokens": 28,
    }
    assert captured["authorization"] == "Bearer test-key"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["store"] is False
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True


def test_openai_summarizer_redacts_provider_error_body() -> None:
    secret = "provider-secret-body"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, text=secret))
    )
    provider = OpenAIResponsesSummarizer(api_key="test-key", http_client=client)

    with pytest.raises(LLMProviderError) as raised:
        asyncio.run(
            provider.summarize(NormalizedContent(source_ids=("post-1",), text="text"))
        )
    asyncio.run(client.aclose())

    assert secret not in str(raised.value)


def test_openai_summarizer_retries_only_retryable_failures() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output_text": json.dumps(
                    {
                        "summary": "ok",
                        "key_points": ["ok"],
                        "topics": ["ai"],
                        "importance": 3,
                        "language": "en",
                        "source_ids": ["post-1"],
                    }
                ),
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesSummarizer(
        api_key="test-key", http_client=client, sleeper=sleeper, max_attempts=3
    )

    asyncio.run(provider.summarize(NormalizedContent(source_ids=("post-1",), text="text")))
    asyncio.run(client.aclose())

    assert attempts == 3
    assert delays == [0.5, 1.0]


def test_openai_summarizer_marks_exhausted_429_as_quota_error() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(429))
    )
    provider = OpenAIResponsesSummarizer(
        api_key="test-key", http_client=client, max_attempts=1
    )

    with pytest.raises(LLMProviderError) as raised:
        asyncio.run(
            provider.summarize(NormalizedContent(source_ids=("post-1",), text="text"))
        )
    asyncio.run(client.aclose())

    assert type(raised.value).__name__ == "LLMQuotaError"


def test_openai_summarizer_truncates_combined_input_deterministically() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output_text": json.dumps(
                    {
                        "summary": "ok",
                        "key_points": ["ok"],
                        "topics": ["ai"],
                        "importance": 3,
                        "language": "en",
                        "source_ids": ["post-1"],
                    }
                ),
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesSummarizer(api_key="test-key", http_client=client)
    asyncio.run(
        provider.summarize(
            NormalizedContent(source_ids=("post-1",), text="a" * 100_000, link_text="b" * 100_000)
        )
    )
    asyncio.run(client.aclose())

    provider_input = json.loads(captured["input"])  # type: ignore[arg-type]
    assert len(provider_input["post_text"] + provider_input["linked_content"]) == 120_000
    assert provider_input["post_text"] == "a" * 100_000
    assert provider_input["linked_content"] == "b" * 20_000


def test_openai_synthesizes_event_digest_with_strict_source_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content)
        captured["timeout"] = request.extensions["timeout"]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output_text": json.dumps(
                    {
                        "overview": "今日重点是 Agent 可靠性。",
                        "topics": [
                            {
                                "name": "Agent",
                                "events": [
                                    {
                                        "title": "长任务出现语言漂移",
                                        "what_happened": "长任务中的语言质量随时间下降。",
                                        "why_it_matters": "这会影响 Agent 输出的稳定性。",
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
                "usage": {"input_tokens": 30, "output_tokens": 12},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesSummarizer(
        api_key="test-key",
        http_client=client,
        model="gpt-5.6-luna",
        reasoning_effort="medium",
        timeout_seconds=17,
        max_output_tokens=900,
    )

    result = asyncio.run(provider.synthesize_digest(_digest_candidates()))
    asyncio.run(client.aclose())

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert captured["authorization"] == "Bearer test-key"
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["instructions"] == DIGEST_SYSTEM_INSTRUCTIONS
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["max_output_tokens"] == 900
    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "x_event_digest",
        "strict": True,
        "schema": DigestOutput.model_json_schema(),
    }
    provider_input = json.loads(payload["input"])
    assert provider_input["candidates"][0] == {
        "id": "unit-a",
        "summary_id": "summary-a",
        "source_ids": ["post-a1", "post-a2"],
        "summary": "Agent quality drifts over long runs.",
        "key_points": ["Review layers do not fully prevent drift."],
        "topics": ["agents"],
        "importance": 4,
        "language": "en",
        "sources": [
            {
                "id": "post-a1",
                "username": "alice",
                "text": "part one",
                "url": "https://x.com/alice/status/1",
            },
            {
                "id": "post-a2",
                "username": "alice",
                "text": "part two",
                "url": "https://x.com/alice/status/2",
            },
        ],
    }
    assert captured["timeout"] == {"connect": 17.0, "read": 17.0, "write": 17.0, "pool": 17.0}
    assert result.output.topics[0].events[0].source_ids == ["post-a1", "post-a2"]
    assert result.token_usage == {"input_tokens": 30, "output_tokens": 12}


def test_openai_digest_rejects_invalid_structured_response() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, json={"status": "completed", "output_text": "{}"}
            )
        )
    )
    provider = OpenAIResponsesSummarizer(api_key="test-key", http_client=client)

    with pytest.raises(LLMProviderError, match="invalid structured response"):
        asyncio.run(provider.synthesize_digest(_digest_candidates()))
    asyncio.run(client.aclose())
