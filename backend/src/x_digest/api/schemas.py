from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class ErrorResponse(StrictModel):
    code: str
    message: str
    request_id: str


class ListCreate(StrictModel):
    platform_list_id: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    sync_interval_minutes: int = Field(default=1440, ge=1, le=525_600)
    enabled: bool = True


class ListUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    sync_interval_minutes: int | None = Field(default=None, ge=1, le=525_600)
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_update(self) -> ListUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Updated fields cannot be null")
        return self


class ListResponse(StrictModel):
    id: str
    platform_list_id: str
    name: str
    sync_interval_minutes: int
    enabled: bool
    latest_seen_at: datetime | None
    latest_seen_post_id: str | None
    created_at: datetime
    updated_at: datetime


class AuthorResponse(StrictModel):
    id: str
    username: str
    display_name: str | None
    profile_image_url: str | None


class PostStateUpdate(StrictModel):
    read: bool | None = None
    saved: bool | None = None
    ignored: bool | None = None

    @model_validator(mode="after")
    def require_update(self) -> PostStateUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one state field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("State fields cannot be null")
        return self


class PostStateResponse(StrictModel):
    read: bool
    saved: bool
    ignored: bool


class PostResponse(StrictModel):
    id: str
    platform: str
    platform_post_id: str
    text: str
    created_at: datetime
    source_url: str
    author: AuthorResponse
    state: PostStateResponse
    topics: list[str]


class PostPageResponse(StrictModel):
    items: list[PostResponse]
    next_cursor: str | None


class DigestItemResponse(StrictModel):
    id: str
    source_type: str
    source_id: str
    topic: str | None
    position: int
    snapshot: dict[str, Any] | None


class DigestResponse(StrictModel):
    id: str
    window_key: str
    version: int
    timezone: str
    rendered_content: dict[str, Any] | None
    status: str
    created_at: datetime
    items: list[DigestItemResponse] = Field(default_factory=list)


class SummaryResponse(StrictModel):
    id: str
    generation: int
    summary: str
    key_points: list[str]
    topics: list[str]
    importance: int
    language: str
    source_ids: list[str]
    status: str


class StageResultResponse(StrictModel):
    counts: dict[str, int]
    duration_ms: int
    last_error: str | None = None


class PipelineResultResponse(StrictModel):
    run_id: str
    status: Literal["succeeded", "partial", "failed"]
    stages: dict[str, StageResultResponse]


class StatusResponse(StrictModel):
    latest_run: dict[str, Any] | None
    latest_summary: dict[str, Any] | None
    deliveries: dict[str, int]
