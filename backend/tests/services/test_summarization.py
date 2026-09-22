import asyncio

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from x_digest.models import Base, Summary
from x_digest.services.summarization import (
    DeterministicSummarizer,
    NormalizedContent,
    SummarizationService,
)


def test_fake_summarizer_persists_one_summary_per_content_fingerprint() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    content = NormalizedContent(source_ids=("101",), text="OpenAI 发布了新的研究成果。")

    with Session(engine) as session:
        service = SummarizationService(session=session, summarizer=DeterministicSummarizer())
        first, created = asyncio.run(service.summarize(content))
        second, created_again = asyncio.run(service.summarize(content))
        session.commit()

        assert created is True
        assert created_again is False
        assert first.id == second.id
        assert first.summary
        assert session.scalars(select(Summary)).all() == [first]


def test_concurrent_failure_does_not_downgrade_a_successful_summary() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    content = NormalizedContent(source_ids=("101",), text="Stable summary")

    with Session(engine) as session:
        summarizer = DeterministicSummarizer()
        service = SummarizationService(session=session, summarizer=summarizer)
        succeeded, _ = asyncio.run(service.summarize(content))
        SummarizationService.record_failure(
            session=session,
            summarizer=summarizer,
            content=content,
            failure_code="summary_processing_failed",
        )

        assert succeeded.status == "succeeded"
        assert succeeded.failure_code is None
