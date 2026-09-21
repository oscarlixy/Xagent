from x_digest.ai.fallback import (
    DeterministicDigestSynthesizer,
    QuotaFallbackDigestSynthesizer,
    QuotaFallbackSummarizer,
)
from x_digest.ai.gemini import GeminiInteractionsSummarizer
from x_digest.ai.openai import OpenAIResponsesSummarizer
from x_digest.config import Settings
from x_digest.services.digest_synthesis import DigestSynthesizer
from x_digest.services.summarization import DeterministicSummarizer, Summarizer


def build_summarizer(settings: Settings) -> Summarizer:
    if settings.llm_provider == "deterministic":
        return DeterministicSummarizer()
    if settings.llm_provider == "gemini":
        if settings.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        return GeminiInteractionsSummarizer(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            base_url=str(settings.gemini_base_url).rstrip("/"),
            thinking_level=settings.gemini_thinking_level,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    if settings.llm_api_key is None:
        raise ValueError("LLM_API_KEY is required when LLM_PROVIDER=openai")
    primary = OpenAIResponsesSummarizer(
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        base_url=str(settings.llm_base_url).rstrip("/"),
        reasoning_effort=settings.llm_reasoning_effort,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    if settings.llm_fallback_provider == "none":
        return primary
    if settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is required when LLM_FALLBACK_PROVIDER=gemini")
    fallback = GeminiInteractionsSummarizer(
        api_key=settings.gemini_api_key.get_secret_value(),
        model=settings.gemini_model,
        base_url=str(settings.gemini_base_url).rstrip("/"),
        thinking_level=settings.gemini_thinking_level,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    return QuotaFallbackSummarizer(primary=primary, fallback=fallback)


def build_digest_synthesizer(settings: Settings) -> DigestSynthesizer:
    if settings.llm_provider == "deterministic":
        return DeterministicDigestSynthesizer()
    if settings.llm_provider == "gemini":
        if settings.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        return GeminiInteractionsSummarizer(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            base_url=str(settings.gemini_base_url).rstrip("/"),
            thinking_level=settings.gemini_thinking_level,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
        )
    if settings.llm_api_key is None:
        raise ValueError("LLM_API_KEY is required when LLM_PROVIDER=openai")
    primary = OpenAIResponsesSummarizer(
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        base_url=str(settings.llm_base_url).rstrip("/"),
        reasoning_effort=settings.llm_reasoning_effort,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    if settings.llm_fallback_provider == "none":
        return primary
    if settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is required when LLM_FALLBACK_PROVIDER=gemini")
    fallback = GeminiInteractionsSummarizer(
        api_key=settings.gemini_api_key.get_secret_value(),
        model=settings.gemini_model,
        base_url=str(settings.gemini_base_url).rstrip("/"),
        thinking_level=settings.gemini_thinking_level,
        timeout_seconds=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    return QuotaFallbackDigestSynthesizer(primary=primary, fallback=fallback)
