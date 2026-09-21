import pytest

from x_digest.ai.contracts import DigestEventOutput, DigestOutput, DigestTopicOutput
from x_digest.services.digest_synthesis import (
    DigestCandidate,
    DigestSource,
    DigestValidationError,
    fallback_digest_output,
    validate_digest_output,
)


@pytest.fixture
def candidates() -> tuple[DigestCandidate, ...]:
    return (
        DigestCandidate(
            id="unit-a",
            summary_id="summary-a",
            source_ids=("post-a1", "post-a2"),
            summary="Agent reliability finding",
            key_points=("Quality drifts over long runs",),
            topics=("agents",),
            importance=4,
            language="en",
            sources=(
                DigestSource(
                    "post-a1", "alice", "part one", "https://x.com/alice/status/1"
                ),
                DigestSource(
                    "post-a2", "alice", "part two", "https://x.com/alice/status/2"
                ),
            ),
        ),
        DigestCandidate(
            id="unit-b",
            summary_id="summary-b",
            source_ids=("post-b",),
            summary="Conference promotion",
            key_points=(),
            topics=("events",),
            importance=1,
            language="en",
            sources=(
                DigestSource("post-b", "bob", "join us", "https://x.com/bob/status/3"),
            ),
        ),
    )


def event(title: str, source_ids: list[str]) -> DigestEventOutput:
    return DigestEventOutput(
        title=title,
        what_happened=f"What happened: {title}",
        why_it_matters=f"Why it matters: {title}",
        event_type="analysis",
        importance=4,
        source_ids=source_ids,
    )


def output(
    *events: DigestEventOutput, excluded_source_ids: list[str] | None = None
) -> DigestOutput:
    topics = [DigestTopicOutput(name="Agents", events=list(events))] if events else []
    return DigestOutput(
        overview="Overview",
        topics=topics,
        excluded_source_ids=excluded_source_ids or [],
    )


def test_valid_digest_assigns_each_source_unit_once(
    candidates: tuple[DigestCandidate, ...],
) -> None:
    result = output(
        event("Agent reliability", ["post-a1", "post-a2"]),
        excluded_source_ids=["post-b"],
    )

    validate_digest_output(result, candidates)


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (
            output(
                event("Agent reliability", ["post-a1", "post-a2", "post-unknown"]),
                excluded_source_ids=["post-b"],
            ),
            "unknown_source_id",
        ),
        (
            output(event("Agent reliability", ["post-a1", "post-a2"])),
            "missing_source_id",
        ),
        (
            output(
                event("Agent reliability", ["post-a1", "post-a2"]),
                event("Repeated source", ["post-b"]),
                excluded_source_ids=["post-b"],
            ),
            "duplicate_source_id",
        ),
        (
            output(
                event("Agent Reliability", ["post-a1", "post-a2"]),
                event("  agent reliability  ", ["post-b"]),
            ),
            "duplicate_event_title",
        ),
        (
            output(
                event("Partial thread", ["post-a1"]),
                excluded_source_ids=["post-a2", "post-b"],
            ),
            "split_source_unit",
        ),
    ],
)
def test_invalid_digest_output_has_a_stable_error_code(
    candidates: tuple[DigestCandidate, ...], result: DigestOutput, code: str
) -> None:
    with pytest.raises(DigestValidationError) as caught:
        validate_digest_output(result, candidates)

    assert caught.value.code == code


def test_fallback_keeps_one_event_per_candidate(
    candidates: tuple[DigestCandidate, ...],
) -> None:
    result = fallback_digest_output(candidates)

    assert result.overview == "共整理 2 条有效动态，按主题归纳如下。"
    assert [topic.name for topic in result.topics] == ["agents", "events"]
    assert [topic.events[0].source_ids for topic in result.topics] == [
        ["post-a1", "post-a2"],
        ["post-b"],
    ]
    assert result.topics[0].events[0].why_it_matters == "Quality drifts over long runs"
    assert result.topics[1].events[0].why_it_matters == "值得保留，便于追踪后续发展。"
    assert result.excluded_source_ids == []


def test_empty_fallback_is_a_complete_empty_digest() -> None:
    result = fallback_digest_output(())

    assert result == DigestOutput(
        overview="该时间段没有可摘要的有效动态。",
        topics=[],
        excluded_source_ids=[],
    )
