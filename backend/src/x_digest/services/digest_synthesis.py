from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from x_digest.ai.contracts import (
    DigestEventOutput,
    DigestOutput,
    DigestTopicOutput,
)


@dataclass(frozen=True)
class DigestSource:
    id: str
    username: str
    text: str
    url: str


@dataclass(frozen=True)
class DigestCandidate:
    id: str
    summary_id: str | None
    source_ids: tuple[str, ...]
    summary: str
    key_points: tuple[str, ...]
    topics: tuple[str, ...]
    importance: int
    language: str
    sources: tuple[DigestSource, ...]


@dataclass(frozen=True)
class DigestProviderResult:
    output: DigestOutput
    token_usage: dict[str, Any] | None = None


class DigestSynthesizer(Protocol):
    model_name: str

    async def synthesize_digest(
        self, candidates: tuple[DigestCandidate, ...]
    ) -> DigestProviderResult:
        raise NotImplementedError


class DigestValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_digest_output(
    output: DigestOutput, candidates: tuple[DigestCandidate, ...]
) -> None:
    expected_ids = [source_id for candidate in candidates for source_id in candidate.source_ids]
    event_assignments = [
        (f"event:{topic_index}:{event_index}", source_id)
        for topic_index, topic in enumerate(output.topics)
        for event_index, event in enumerate(topic.events)
        for source_id in event.source_ids
    ]
    assignments = event_assignments + [
        ("excluded", source_id) for source_id in output.excluded_source_ids
    ]
    assigned_ids = [source_id for _location, source_id in assignments]

    expected_set = set(expected_ids)
    if any(source_id not in expected_set for source_id in assigned_ids):
        raise DigestValidationError("unknown_source_id")
    if any(source_id not in assigned_ids for source_id in expected_ids):
        raise DigestValidationError("missing_source_id")
    if any(count > 1 for count in Counter(assigned_ids).values()):
        raise DigestValidationError("duplicate_source_id")

    normalized_titles = [
        " ".join(event.title.split()).casefold()
        for topic in output.topics
        for event in topic.events
    ]
    if any(count > 1 for count in Counter(normalized_titles).values()):
        raise DigestValidationError("duplicate_event_title")

    locations = {source_id: location for location, source_id in assignments}
    for candidate in candidates:
        if len({locations[source_id] for source_id in candidate.source_ids}) != 1:
            raise DigestValidationError("split_source_unit")


def fallback_digest_output(candidates: tuple[DigestCandidate, ...]) -> DigestOutput:
    if not candidates:
        return DigestOutput(
            overview="该时间段没有可摘要的有效动态。",
            topics=[],
            excluded_source_ids=[],
        )

    topics: dict[str, list[DigestEventOutput]] = {}
    for candidate in candidates:
        topic = candidate.topics[0] if candidate.topics else "General"
        summary = candidate.summary.strip() or "未命名动态"
        topics.setdefault(topic, []).append(
            DigestEventOutput(
                title=summary[:120],
                what_happened=summary,
                why_it_matters=(
                    candidate.key_points[0]
                    if candidate.key_points
                    else "值得保留，便于追踪后续发展。"
                ),
                event_type="analysis",
                importance=candidate.importance,
                source_ids=list(candidate.source_ids),
            )
        )

    return DigestOutput(
        overview=f"共整理 {len(candidates)} 条有效动态，按主题归纳如下。",
        topics=[
            DigestTopicOutput(name=topic, events=events) for topic, events in topics.items()
        ],
        excluded_source_ids=[],
    )
