import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel

app = FastAPI()
TOKEN = "Bearer test-internal-token-that-must-stay-server-side"


class StateUpdate(BaseModel):
    read: bool | None = None
    saved: bool | None = None
    ignored: bool | None = None


def require_token(authorization: str | None) -> None:
    if authorization != TOKEN:
        raise HTTPException(status_code=401, detail="internal token required")


def post(post_id: str = "post-1", author: str = "alice") -> dict[str, Any]:
    return {
        "id": post_id,
        "platform": "x",
        "platform_post_id": "19001",
        "text": "Original launch notes with practical deployment details.",
        "created_at": "2026-09-11T08:00:00Z",
        "source_url": f"https://x.com/{author}/status/19001",
        "author": {
            "id": f"author-{author}",
            "username": author,
            "display_name": author.title(),
            "profile_image_url": None,
        },
        "state": {"read": False, "saved": False, "ignored": False},
        "topics": ["ai", "engineering"],
    }


DIGEST = {
    "id": "digest-1",
    "window_key": "2026-09-11T00:00:00Z/daily",
    "version": 1,
    "timezone": "Asia/Shanghai",
    "rendered_content": {
        "title": "Daily AI Brief",
        "summary": "A <script>danger()</script> update, rendered safely.",
        "key_points": ["Shipping improved", "Costs stayed bounded"],
    },
    "status": "succeeded",
    "created_at": "2026-09-11T09:00:00Z",
    "items": [
        {
            "id": "digest-item-1",
            "source_type": "summary",
            "source_id": "post-1",
            "topic": "ai",
            "position": 0,
            "snapshot": {
                "summary": "A concise, traceable summary.",
                "source_ids": ["post-1"],
                "key_points": ["Traceable to the original post"],
            },
        }
    ],
}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/digests")
def list_digests(response: Response, authorization: str | None = Header(default=None)) -> list:
    require_token(authorization)
    response.headers["X-Request-ID"] = "mock-request-id"
    return [DIGEST]


@app.get("/api/digests/{digest_id}")
def get_digest(digest_id: str, authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    if digest_id == "error":
        raise HTTPException(status_code=503, detail="digest unavailable")
    return DIGEST


@app.post("/api/digests/{digest_id}/regenerate", status_code=201)
def regenerate_digest(digest_id: str, authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    return {**DIGEST, "id": "digest-2", "version": 2}


@app.get("/api/posts")
async def list_posts(
    authorization: str | None = Header(default=None),
    author: str | None = Query(default=None),
) -> dict[str, Any]:
    require_token(authorization)
    if author == "loading":
        await asyncio.sleep(0.6)
    if author == "error":
        raise HTTPException(status_code=503, detail="posts unavailable")
    if author == "empty":
        return {"items": [], "next_cursor": None}
    if author == "fail-actions":
        return {"items": [post("post-fail", author)], "next_cursor": None}
    return {"items": [post()], "next_cursor": None}


@app.post("/api/posts/{post_id}/state")
def update_state(
    post_id: str,
    payload: StateUpdate,
    authorization: str | None = Header(default=None),
) -> dict[str, bool]:
    require_token(authorization)
    if post_id == "post-fail":
        raise HTTPException(status_code=503, detail="state update failed")
    return {
        "read": bool(payload.read),
        "saved": bool(payload.saved),
        "ignored": bool(payload.ignored),
    }


@app.post("/api/posts/{post_id}/summaries/regenerate", status_code=201)
def regenerate_summary(
    post_id: str,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> dict[str, Any]:
    require_token(authorization)
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="idempotency key required")
    return {
        "id": "summary-2",
        "generation": 2,
        "summary": "Regenerated summary for the complete source unit.",
        "key_points": ["Freshly generated"],
        "topics": ["ai"],
        "importance": 4,
        "language": "en",
        "source_ids": [post_id],
        "status": "succeeded",
    }
