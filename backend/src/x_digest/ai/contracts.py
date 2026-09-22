from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    key_points: list[str]
    topics: list[str]
    importance: int = Field(ge=1, le=5)
    language: str = Field(min_length=2)
    source_ids: list[str] = Field(min_length=1)


class DigestEventOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1)
    what_happened: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    event_type: Literal["news", "research", "product", "analysis", "opinion"]
    importance: int = Field(ge=1, le=5)
    source_ids: list[str] = Field(min_length=1)


class DigestTopicOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    events: list[DigestEventOutput] = Field(min_length=1)


class DigestOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    overview: str = Field(min_length=1)
    topics: list[DigestTopicOutput]
    excluded_source_ids: list[str]


@dataclass(frozen=True)
class SummaryProviderResult:
    output: SummaryOutput
    token_usage: dict[str, Any] | None = None
