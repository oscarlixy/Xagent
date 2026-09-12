import asyncio
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel

app = FastAPI()
TOKEN = "Bearer test-internal-token-that-must-stay-server-side"
last_oauth_request: dict[str, Any] = {}


class StateUpdate(BaseModel):
    read: bool | None = None
    saved: bool | None = None
    ignored: bool | None = None


def require_token(authorization: str | None) -> None:
    if authorization != TOKEN:
        raise HTTPException(status_code=401, detail="internal token required")


def post(
    post_id: str = "post-1",
    author: str = "alice",
    *,
    source_url: str | None = None,
    text: str = "Original launch notes with practical deployment details.",
) -> dict[str, Any]:
    return {
        "id": post_id,
        "platform": "x",
        "platform_post_id": "19001",
        "text": text,
        "created_at": "2026-09-11T08:00:00Z",
        "source_url": source_url or f"https://x.com/{author}/status/19001",
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

DIGEST_TWO = {
    **DIGEST,
    "id": "digest-2",
    "version": 2,
    "items": [
        {
            **DIGEST["items"][0],
            "id": "digest-item-2",
            "source_id": "post-2",
            "snapshot": {
                **DIGEST["items"][0]["snapshot"],
                "summary": "A regenerated summary with different sources.",
                "source_ids": ["post-2"],
            },
        }
    ],
}

UNSAFE_DIGEST = {
    **DIGEST,
    "id": "digest-unsafe",
    "rendered_content": {**DIGEST["rendered_content"], "title": "Unsafe Digest"},
    "items": [
        {
            **DIGEST["items"][0],
            "id": "digest-item-unsafe",
            "snapshot": {
                "summary": "A summary with an untrusted source value.",
                "source_url": "https://attacker.example/not-x",
                "source_ids": [],
            },
        }
    ],
}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/x/oauth/callback", status_code=204)
async def oauth_callback(request: Request, authorization: str | None = Header(default=None)) -> Response:
    require_token(authorization)
    payload = await request.json()
    last_oauth_request.clear()
    last_oauth_request.update(
        {
            "body": payload,
            "headers": {
                "authorization": request.headers.get("authorization"),
                "content_type": request.headers.get("content-type"),
                "cookie": request.headers.get("cookie"),
                "x_untrusted": request.headers.get("x-untrusted"),
            },
        }
    )
    if payload.get("code") == "upstream-failure-code-must-not-leak":
        raise HTTPException(status_code=502, detail="upstream-failure-code-must-not-leak")
    return Response(status_code=204)


@app.get("/oauth-inspect")
def oauth_inspect() -> dict[str, Any]:
    return last_oauth_request


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
    if digest_id == "digest-2":
        return DIGEST_TWO
    if digest_id == "digest-unsafe":
        return UNSAFE_DIGEST
    return DIGEST


@app.post("/api/digests/{digest_id}/regenerate", status_code=201)
def regenerate_digest(digest_id: str, authorization: str | None = Header(default=None)) -> dict:
    require_token(authorization)
    return DIGEST_TWO


@app.get("/api/posts")
async def list_posts(
    authorization: str | None = Header(default=None),
    author: str | None = Query(default=None),
    to: str | None = Query(default=None),
    limit: int = Query(default=50),
    cursor: str | None = Query(default=None),
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
    if author == "unsafe-link":
        return {
            "items": [post("post-unsafe", author, source_url="http://x.com/unsafe/status/1")],
            "next_cursor": None,
        }
    if author == "date-boundary":
        items = (
            [post("post-late", author, text="A post from the final minute of the selected day.")]
            if to == "2026-09-11T23:59:59.999999Z"
            else []
        )
        return {"items": items, "next_cursor": None}
    if author == "pagination":
        return {
            "items": [
                post(
                    "post-page-2" if cursor == "cursor-page-2" else "post-page-1",
                    author,
                    text="Second page post" if cursor == "cursor-page-2" else "First page post",
                )
            ],
            "next_cursor": None if cursor == "cursor-page-2" else "cursor-page-2",
        }
    if limit == 100:
        return {
            "items": [
                post(),
                post(
                    "post-2",
                    "bob",
                    source_url="https://twitter.com/bob/status/19002",
                    text="A newly sourced post for digest version two.",
                ),
            ],
            "next_cursor": None,
        }
    return {"items": [post()], "next_cursor": None}


@app.api_route("/api/inspect", methods=["GET", "POST", "PATCH", "DELETE"])
async def inspect_proxy(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    require_token(authorization)
    response.headers["X-Request-ID"] = "inspect-request-id"
    response.headers["X-Upstream-Secret"] = "must-not-pass"
    return {
        "method": request.method,
        "query": request.url.query,
        "body": (await request.body()).decode(),
        "headers": {
            "authorization": request.headers.get("authorization"),
            "accept": request.headers.get("accept"),
            "content-type": request.headers.get("content-type"),
            "idempotency-key": request.headers.get("idempotency-key"),
            "cookie": request.headers.get("cookie"),
            "x-untrusted": request.headers.get("x-untrusted"),
        },
    }


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
