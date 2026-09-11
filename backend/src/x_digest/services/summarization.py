import hashlib
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from x_digest.ai.contracts import SummaryOutput
from x_digest.models import Summary

PROMPT_VERSION = "single-content-v1"


@dataclass(frozen=True)
class NormalizedContent:
    source_ids: tuple[str, ...]
    text: str
    link_text: str = ""


class Summarizer(Protocol):
    model_name: str

    async def summarize(self, content: NormalizedContent) -> SummaryOutput: ...


class DeterministicSummarizer:
    """Offline deterministic provider for local development and end-to-end tests."""

    model_name = "fake-summary-v1"

    async def summarize(self, content: NormalizedContent) -> SummaryOutput:
        text = content.text.strip()
        return SummaryOutput(
            summary=text[:180] if text else "无可摘要原文",
            key_points=[text[:80] if text else "无原文"],
            topics=["ai"],
            importance=3,
            language="zh",
            source_ids=list(content.source_ids),
        )


class SummarizationService:
    def __init__(self, *, session: Session, summarizer: Summarizer) -> None:
        self._session = session
        self._summarizer = summarizer

    async def summarize(self, content: NormalizedContent) -> tuple[Summary, bool]:
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

        output = await self._summarizer.summarize(content)
        if set(output.source_ids) != set(content.source_ids):
            raise ValueError("Summary output source IDs do not match the input")
        summary = existing or Summary(
            content_fingerprint=fingerprint,
            model=self._summarizer.model_name,
            prompt_version=PROMPT_VERSION,
            generation=1,
        )
        summary.summary = output.summary
        summary.key_points = output.key_points
        summary.topics = output.topics
        summary.importance = output.importance
        summary.language = output.language
        summary.source_ids = output.source_ids
        summary.status = "succeeded"
        summary.failure_code = None
        if existing is None:
            self._session.add(summary)
        self._session.flush()
        return summary, True

    @staticmethod
    def _fingerprint(content: NormalizedContent) -> str:
        payload = "\n".join((*content.source_ids, content.text, content.link_text, PROMPT_VERSION))
        return hashlib.sha256(payload.encode()).hexdigest()

    fingerprint_for = _fingerprint
