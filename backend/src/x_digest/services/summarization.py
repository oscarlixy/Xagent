import hashlib
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from x_digest.ai.contracts import SummaryOutput, SummaryProviderResult
from x_digest.models import Summary
from x_digest.repositories.transactions import ensure_sqlite_outer_transaction

PROMPT_VERSION = "single-content-v1"


@dataclass(frozen=True)
class NormalizedContent:
    source_ids: tuple[str, ...]
    text: str
    link_text: str = ""


class Summarizer(Protocol):
    model_name: str

    async def summarize(
        self, content: NormalizedContent
    ) -> SummaryProviderResult | SummaryOutput: ...


class DeterministicSummarizer:
    """Offline deterministic provider for local development and end-to-end tests."""

    model_name = "fake-summary-v1"

    async def summarize(self, content: NormalizedContent) -> SummaryProviderResult:
        text = content.text.strip()
        return SummaryProviderResult(
            output=SummaryOutput(
                summary=text[:180] if text else "无可摘要原文",
                key_points=[text[:80] if text else "无原文"],
                topics=["ai"],
                importance=3,
                language="zh",
                source_ids=list(content.source_ids),
            )
        )


class SummarizationService:
    def __init__(self, *, session: Session, summarizer: Summarizer) -> None:
        self._session = session
        self._summarizer = summarizer

    async def summarize(self, content: NormalizedContent) -> tuple[Summary, bool]:
        provider_result = await self._summarizer.summarize(content)
        return self.store_result(content, provider_result)

    def store_result(
        self,
        content: NormalizedContent,
        provider_result: SummaryProviderResult | SummaryOutput,
    ) -> tuple[Summary, bool]:
        fingerprint = self._fingerprint(content)
        existing = self._session.scalar(
            select(Summary).where(
                Summary.content_fingerprint == fingerprint,
                Summary.model == self._summarizer.model_name,
                Summary.prompt_version == PROMPT_VERSION,
                Summary.generation == 1,
            )
        )
        if existing is not None and existing.status == "succeeded":
            return existing, False

        if isinstance(provider_result, SummaryOutput):
            provider_result = SummaryProviderResult(output=provider_result)
        output = provider_result.output
        if set(output.source_ids) != set(content.source_ids):
            raise ValueError("Summary output source IDs do not match the input")
        summary = existing
        if summary is None:
            ensure_sqlite_outer_transaction(self._session)
            summary = Summary(
                content_fingerprint=fingerprint,
                model=self._summarizer.model_name,
                prompt_version=PROMPT_VERSION,
                generation=1,
                summary=output.summary,
                key_points=output.key_points,
                topics=output.topics,
                importance=output.importance,
                language=output.language,
                source_ids=output.source_ids,
            )
            try:
                with self._session.begin_nested():
                    self._session.add(summary)
                    self._session.flush()
            except IntegrityError:
                summary = self._session.scalar(
                    select(Summary).where(
                        Summary.content_fingerprint == fingerprint,
                        Summary.model == self._summarizer.model_name,
                        Summary.prompt_version == PROMPT_VERSION,
                        Summary.generation == 1,
                    )
                )
                if summary is None:
                    raise
                if summary.status == "succeeded":
                    return summary, False
        summary.summary = output.summary
        summary.key_points = output.key_points
        summary.topics = output.topics
        summary.importance = output.importance
        summary.language = output.language
        summary.source_ids = output.source_ids
        summary.token_usage = provider_result.token_usage
        summary.status = "succeeded"
        summary.failure_code = None
        self._session.flush()
        return summary, True

    @classmethod
    def record_failure(
        cls,
        *,
        session: Session,
        summarizer: Summarizer,
        content: NormalizedContent,
        failure_code: str,
    ) -> Summary:
        fingerprint = cls._fingerprint(content)
        summary = session.scalar(
            select(Summary).where(
                Summary.content_fingerprint == fingerprint,
                Summary.model == summarizer.model_name,
                Summary.prompt_version == PROMPT_VERSION,
                Summary.generation == 1,
            )
        )
        if summary is not None and summary.status == "succeeded":
            return summary
        if summary is None:
            ensure_sqlite_outer_transaction(session)
            summary = Summary(
                content_fingerprint=fingerprint,
                model=summarizer.model_name,
                prompt_version=PROMPT_VERSION,
                generation=1,
                summary="",
                key_points=[],
                topics=[],
                importance=1,
                language="und",
                source_ids=list(content.source_ids),
                status="failed",
                failure_code=failure_code,
            )
            try:
                with session.begin_nested():
                    session.add(summary)
                    session.flush()
            except IntegrityError:
                summary = session.scalar(
                    select(Summary).where(
                        Summary.content_fingerprint == fingerprint,
                        Summary.model == summarizer.model_name,
                        Summary.prompt_version == PROMPT_VERSION,
                        Summary.generation == 1,
                    )
                )
                if summary is None:
                    raise
                if summary.status == "succeeded":
                    return summary
        summary.status = "failed"
        summary.failure_code = failure_code
        session.flush()
        return summary

    @staticmethod
    def _fingerprint(content: NormalizedContent) -> str:
        payload = "\n".join((*content.source_ids, content.text, content.link_text, PROMPT_VERSION))
        return hashlib.sha256(payload.encode()).hexdigest()

    fingerprint_for = _fingerprint
