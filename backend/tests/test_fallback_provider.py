import asyncio

import pytest

from x_digest.ai import providers
from x_digest.ai.contracts import DigestOutput, SummaryOutput, SummaryProviderResult
from x_digest.ai.openai import LLMProviderError, LLMQuotaError
from x_digest.config import Settings
from x_digest.services.digest_synthesis import (
    DigestCandidate,
    DigestProviderResult,
    DigestSource,
)
from x_digest.services.summarization import NormalizedContent

CONTENT = NormalizedContent(source_ids=("post-1",), text="text")
DIGEST_CANDIDATES = (
    DigestCandidate(
        id="unit-1",
        summary_id="summary-1",
        source_ids=("post-1",),
        summary="summary",
        key_points=("point",),
        topics=("ai",),
        importance=3,
        language="en",
        sources=(DigestSource("post-1", "alice", "text", "https://x.com/alice/status/1"),),
    ),
)


def _result(summary: str, *, usage: dict[str, object] | None = None) -> SummaryProviderResult:
    return SummaryProviderResult(
        output=SummaryOutput(
            summary=summary,
            key_points=[summary],
            topics=["ai"],
            importance=3,
            language="en",
            source_ids=["post-1"],
        ),
        token_usage=usage,
    )


class SuccessfulSummarizer:
    def __init__(self, model_name: str, summary: str) -> None:
        self.model_name = model_name
        self._summary = summary

    async def summarize(self, _content: NormalizedContent) -> SummaryProviderResult:
        return _result(self._summary, usage={"total_tokens": 7})


class FailingSummarizer:
    def __init__(self, model_name: str, error: Exception) -> None:
        self.model_name = model_name
        self._error = error

    async def summarize(self, _content: NormalizedContent) -> SummaryProviderResult:
        raise self._error


class MustNotRunSummarizer:
    model_name = "gemini-3.5-flash-lite"

    async def summarize(self, _content: NormalizedContent) -> SummaryProviderResult:
        raise AssertionError("fallback must not run")


class DigestDouble:
    def __init__(
        self,
        model_name: str,
        result: DigestProviderResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.model_name = model_name
        self._result = result
        self._error = error

    async def synthesize_digest(
        self, _candidates: tuple[DigestCandidate, ...]
    ) -> DigestProviderResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _fallback_type() -> type:
    fallback_type = getattr(providers, "QuotaFallbackSummarizer", None)
    assert fallback_type is not None
    return fallback_type


def test_fallback_summarizer_returns_openai_result_without_calling_gemini() -> None:
    provider = _fallback_type()(
        primary=SuccessfulSummarizer("gpt-5.6-luna", "openai"),
        fallback=MustNotRunSummarizer(),
    )

    result = asyncio.run(provider.summarize(CONTENT))

    assert provider.model_name == "gpt-5.6-luna"
    assert result.output.summary == "openai"
    assert result.token_usage == {"total_tokens": 7}


def test_fallback_summarizer_uses_gemini_only_for_openai_quota_error() -> None:
    provider = _fallback_type()(
        primary=FailingSummarizer("gpt-5.6-luna", LLMQuotaError("quota exhausted")),
        fallback=SuccessfulSummarizer("gemini-3.5-flash-lite", "gemini"),
    )

    result = asyncio.run(provider.summarize(CONTENT))

    assert provider.model_name == "gpt-5.6-luna"
    assert result.output.summary == "gemini"
    assert result.token_usage == {
        "total_tokens": 7,
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
    }


def test_fallback_summarizer_does_not_hide_non_quota_openai_errors() -> None:
    provider = _fallback_type()(
        primary=FailingSummarizer("gpt-5.6-luna", LLMProviderError("HTTP 500")),
        fallback=MustNotRunSummarizer(),
    )

    with pytest.raises(LLMProviderError, match="HTTP 500"):
        asyncio.run(provider.summarize(CONTENT))


def test_build_summarizer_configures_openai_primary_with_gemini_fallback() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_fallback_provider="gemini",
        llm_api_key="openai-key",
        gemini_api_key="gemini-key",
    )

    provider = providers.build_summarizer(settings)

    assert type(provider).__name__ == "QuotaFallbackSummarizer"
    assert provider.model_name == "gpt-5.6-luna"


def test_build_summarizer_requires_gemini_key_when_fallback_is_enabled() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai",
        llm_fallback_provider="gemini",
        llm_api_key="openai-key",
        gemini_api_key=None,
    )

    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        providers.build_summarizer(settings)


def test_digest_fallback_uses_secondary_only_for_quota_errors() -> None:
    result = DigestProviderResult(
        output=DigestOutput(overview="fallback", topics=[], excluded_source_ids=["post-1"]),
        token_usage={"total_tokens": 5},
    )
    provider = providers.QuotaFallbackDigestSynthesizer(
        primary=DigestDouble("openai", error=LLMQuotaError("quota")),
        fallback=DigestDouble("gemini", result=result),
    )

    actual = asyncio.run(provider.synthesize_digest(DIGEST_CANDIDATES))

    assert provider.model_name == "openai->gemini"
    assert actual == result


def test_digest_fallback_does_not_hide_non_quota_errors() -> None:
    provider = providers.QuotaFallbackDigestSynthesizer(
        primary=DigestDouble("openai", error=LLMProviderError("HTTP 500")),
        fallback=DigestDouble("gemini", error=AssertionError("must not run")),
    )

    with pytest.raises(LLMProviderError, match="HTTP 500"):
        asyncio.run(provider.synthesize_digest(DIGEST_CANDIDATES))


def test_build_digest_synthesizer_supports_deterministic_mode() -> None:
    provider = providers.build_digest_synthesizer(
        Settings(_env_file=None, llm_provider="deterministic")
    )

    result = asyncio.run(provider.synthesize_digest(DIGEST_CANDIDATES))

    assert result.output.topics[0].events[0].source_ids == ["post-1"]
