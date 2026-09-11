from pydantic import BaseModel, Field


class SummaryOutput(BaseModel):
    summary: str = Field(min_length=1)
    key_points: list[str]
    topics: list[str]
    importance: int = Field(ge=1, le=5)
    language: str = Field(min_length=2)
    source_ids: list[str] = Field(min_length=1)
