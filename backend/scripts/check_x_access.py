"""Opt-in, redacted operator probe for a private X List OAuth connection."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from x_digest.config import Settings  # noqa: E402
from x_digest.services.oauth_client import XOAuthClient  # noqa: E402
from x_digest.sources.errors import XSourceError  # noqa: E402
from x_digest.sources.x_api import XApiSource, is_valid_x_list_id  # noqa: E402


def _build_source(settings: Settings) -> tuple[XApiSource, XOAuthClient]:
    engine = create_engine(settings.database_url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    client = XOAuthClient(
        session_factory=session_factory,
        client_id=settings.x_client_id,
        redirect_uri=settings.x_oauth_redirect_uri,
        encryption_key=settings.x_token_encryption_key,
    )
    return XApiSource(token_service=client), client


async def _close(source: XApiSource, client: XOAuthClient) -> None:
    try:
        await source.aclose()
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe private X List OAuth access")
    parser.add_argument("--list-id", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm:
        parser.error("--confirm is required before making an X request")
    if not is_valid_x_list_id(args.list_id):
        parser.error("--list-id must be a numeric X List ID")

    source: XApiSource | None = None
    client: XOAuthClient | None = None
    try:
        source, client = _build_source(Settings())
        page = asyncio.run(
            source.fetch_page(list_id=args.list_id, pagination_token=None, max_results=1)
        )
    except XSourceError as error:
        print(_result_json(status="error", count=0, request_id=error.request_id))
        return 1
    except Exception:
        print(_result_json(status="error", count=0, request_id=None))
        return 1
    else:
        print(_result_json(status="ok", count=len(page.posts), request_id=page.request_id))
        return 0
    finally:
        if source is not None and client is not None:
            asyncio.run(_close(source, client))


def _result_json(*, status: str, count: int, request_id: str | None) -> str:
    return json.dumps(
        {"status": status, "count": count, "request_id": request_id}, separators=(",", ":")
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
