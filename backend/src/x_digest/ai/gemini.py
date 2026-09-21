from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from x_digest.ai.contracts import DigestOutput, SummaryOutput, SummaryProviderResult
from x_digest.ai.digest_prompt import DIGEST_SYSTEM_INSTRUCTIONS, digest_input_json
from x_digest.ai.openai import SUMMARY_MAX_INPUT_CHARS, SYSTEM_INSTRUCTIONS, LLMProviderError
from x_digest.services.digest_synthesis import DigestCandidate, DigestProviderResult
from x_digest.services.summarization import NormalizedContent

type Sleeper = Callable[[float], Awaitable[None]]


class GeminiInteractionsSummarizer:
    """Gemini Interactions API provider using structured JSON output."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gemini-3.5-flash-lite",
        base_url: str = "https://generativelanguage.googleapis.com/v1",
        thinking_level: Literal["minimal", "low", "medium", "high"] = "low",
        timeout_seconds: float = 30.0,
        max_output_tokens: int = 1200,
        http_client: httpx.AsyncClient | None = None,
        max_attempts: int = 3,
        sleeper: Sleeper | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("A Gemini API key is required for the Gemini provider")
        self.model_name = model
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/interactions"
        self._thinking_level = thinking_level
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._http_client = http_client
        self._max_attempts = max_attempts
        self._sleeper = sleeper or asyncio.sleep

    async def summarize(self, content: NormalizedContent) -> SummaryProviderResult:
        post_text = content.text[:SUMMARY_MAX_INPUT_CHARS]
        link_budget = SUMMARY_MAX_INPUT_CHARS - len(post_text)
        payload = {
            "model": self.model_name,
            "store": False,
            "system_instruction": SYSTEM_INSTRUCTIONS,
            "input": json.dumps(
                {
                    "source_ids": list(content.source_ids),
                    "post_text": post_text,
                    "linked_content": content.link_text[:link_budget],
                },
                ensure_ascii=False,
            ),
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": SummaryOutput.model_json_schema(),
            },
            "generation_config": {
                "max_output_tokens": self._max_output_tokens,
                "thinking_level": self._thinking_level,
            },
        }
        headers = self._request_headers()
        response = await self._request_with_retries(headers, payload)
        try:
            body = response.json()
            if body.get("status") != "completed":
                raise LLMProviderError("Gemini interaction did not complete")
            output = SummaryOutput.model_validate_json(_output_text(body))
            usage = body.get("usage")
            return SummaryProviderResult(
                output=output, token_usage=dict(usage) if isinstance(usage, dict) else None
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise LLMProviderError("Gemini returned an invalid structured response") from error

    async def synthesize_digest(
        self, candidates: tuple[DigestCandidate, ...]
    ) -> DigestProviderResult:
        payload = {
            "model": self.model_name,
            "store": False,
            "system_instruction": DIGEST_SYSTEM_INSTRUCTIONS,
            "input": digest_input_json(candidates),
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": DigestOutput.model_json_schema(),
            },
            "generation_config": {
                "max_output_tokens": self._max_output_tokens,
                "thinking_level": self._thinking_level,
            },
        }
        response = await self._request_with_retries(self._request_headers(), payload)
        try:
            body = response.json()
            if body.get("status") != "completed":
                raise LLMProviderError("Gemini interaction did not complete")
            output = DigestOutput.model_validate_json(_output_text(body))
            usage = body.get("usage")
            return DigestProviderResult(
                output=output,
                token_usage=dict(usage) if isinstance(usage, dict) else None,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise LLMProviderError("Gemini returned an invalid structured response") from error

    def _request_headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    async def _request_with_retries(
        self, headers: dict[str, str], payload: dict[str, Any]
    ) -> httpx.Response:
        for attempt in range(self._max_attempts):
            try:
                if self._http_client is not None:
                    response = await self._http_client.post(
                        self._endpoint, headers=headers, json=payload, timeout=self._timeout
                    )
                else:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(self._endpoint, headers=headers, json=payload)
            except httpx.HTTPError as error:
                if attempt == self._max_attempts - 1:
                    raise LLMProviderError("Gemini request failed") from error
                await self._sleeper(0.5 * (2**attempt))
                continue
            if response.is_success:
                return response
            if response.status_code != 429 and response.status_code < 500:
                raise LLMProviderError(
                    f"Gemini request failed with HTTP {response.status_code}"
                )
            if attempt == self._max_attempts - 1:
                raise LLMProviderError(
                    f"Gemini request failed with HTTP {response.status_code}"
                )
            delay = _retry_after_seconds(response.headers.get("Retry-After"))
            await self._sleeper(delay if delay is not None else 0.5 * (2**attempt))
        raise LLMProviderError("Gemini request failed")  # pragma: no cover


def _output_text(body: dict[str, Any]) -> str:
    steps = body.get("steps")
    if not isinstance(steps, list):
        raise KeyError("steps")
    for step in reversed(steps):
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        text = "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
        if text:
            return text
    raise KeyError("model_output text")


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
