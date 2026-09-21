from x_digest.ai.contracts import SummaryOutput, SummaryProviderResult
from x_digest.ai.openai import LLMQuotaError
from x_digest.services.digest_synthesis import (
    DigestCandidate,
    DigestProviderResult,
    DigestSynthesizer,
    fallback_digest_output,
)
from x_digest.services.summarization import NormalizedContent, Summarizer


class QuotaFallbackSummarizer:
    """Use a secondary provider only when the primary exhausts its quota retries."""

    def __init__(self, *, primary: Summarizer, fallback: Summarizer) -> None:
        self.model_name = primary.model_name
        self._primary = primary
        self._fallback = fallback

    async def summarize(
        self, content: NormalizedContent
    ) -> SummaryProviderResult | SummaryOutput:
        try:
            return await self._primary.summarize(content)
        except LLMQuotaError:
            result = await self._fallback.summarize(content)
            if isinstance(result, SummaryOutput):
                result = SummaryProviderResult(output=result)
            usage = dict(result.token_usage or {})
            usage.update({"provider": "gemini", "model": self._fallback.model_name})
            return SummaryProviderResult(output=result.output, token_usage=usage)


class DeterministicDigestSynthesizer:
    model_name = "deterministic-digest-v1"

    async def synthesize_digest(
        self, candidates: tuple[DigestCandidate, ...]
    ) -> DigestProviderResult:
        return DigestProviderResult(output=fallback_digest_output(candidates))


class QuotaFallbackDigestSynthesizer:
    def __init__(self, *, primary: DigestSynthesizer, fallback: DigestSynthesizer) -> None:
        self._primary = primary
        self._fallback = fallback
        self.model_name = f"{primary.model_name}->{fallback.model_name}"

    async def synthesize_digest(
        self, candidates: tuple[DigestCandidate, ...]
    ) -> DigestProviderResult:
        try:
            return await self._primary.synthesize_digest(candidates)
        except LLMQuotaError:
            return await self._fallback.synthesize_digest(candidates)
