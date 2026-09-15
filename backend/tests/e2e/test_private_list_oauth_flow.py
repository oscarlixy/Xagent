import asyncio
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from x_digest.config import Settings
from x_digest.main import create_app
from x_digest.models import Base
from x_digest.repositories.lists import upsert_list
from x_digest.scheduler import (
    PipelineSchedulerServices,
    _run_pipeline,
    build_production_pipeline,
    build_production_pipeline_runner,
)
from x_digest.services import oauth_client as oauth_client_module
from x_digest.services.oauth_tokens import TokenVault
from x_digest.sources.x_api import XApiSource

FERNET_KEY = SecretStr("MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
INTERNAL_TOKEN = SecretStr("test-only-internal-api-token-0123456789abcdef")


class LoopBoundAsyncClient(httpx.AsyncClient):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.created_loop = asyncio.get_running_loop()
        self.closed_loop: asyncio.AbstractEventLoop | None = None

        async def checked_handler(request: httpx.Request) -> httpx.Response:
            if asyncio.get_running_loop() is not self.created_loop:
                raise RuntimeError("client request crossed event loops")
            return handler(request)

        super().__init__(transport=httpx.MockTransport(checked_handler))

    async def aclose(self) -> None:
        if asyncio.get_running_loop() is not self.created_loop:
            raise RuntimeError("client cleanup crossed event loops")
        self.closed_loop = asyncio.get_running_loop()
        await super().aclose()


async def _contend_process_refresh_lock_once() -> None:
    """Exercise the lock's waiting path before crossing scheduler job loops."""

    lock = oauth_client_module._PROCESS_REFRESH_LOCK
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold() -> None:
        async with lock:
            holder_entered.set()
            await release_holder.wait()

    async def wait() -> None:
        await holder_entered.wait()
        async with lock:
            return

    holder = asyncio.create_task(hold())
    waiter = asyncio.create_task(wait())
    await holder_entered.wait()
    await asyncio.sleep(0.01)
    release_holder.set()
    await asyncio.gather(holder, waiter)


def _settings() -> Settings:
    return Settings(
        internal_api_token=INTERNAL_TOKEN,
        x_client_id="test-x-client-id",
        x_oauth_redirect_uri="https://reader.test/api/x/callback",
        x_token_encryption_key=FERNET_KEY,
    )


def test_web_app_default_pipeline_uses_the_oauth_backed_x_source() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    api_client = httpx.AsyncClient(transport=transport)
    oauth_client = httpx.AsyncClient(transport=transport)
    app = create_app(
        settings=_settings(), x_api_http_client=api_client, oauth_http_client=oauth_client
    )

    source = app.state.pipeline_service._ingestion._source

    assert isinstance(source, XApiSource)
    assert source._token_service is app.state.x_oauth_client
    asyncio.run(api_client.aclose())
    asyncio.run(oauth_client.aclose())


def test_scheduler_production_pipeline_uses_the_oauth_backed_x_source() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    api_client = httpx.AsyncClient(transport=transport)
    oauth_http_client = httpx.AsyncClient(transport=transport)

    pipeline, oauth_client, source = build_production_pipeline(
        _settings(), factory, x_api_http_client=api_client, oauth_http_client=oauth_http_client
    )

    assert isinstance(pipeline._ingestion._source, XApiSource)
    assert source is pipeline._ingestion._source
    assert source._token_service is oauth_client
    asyncio.run(api_client.aclose())
    asyncio.run(oauth_http_client.aclose())


def test_scheduled_pipeline_creates_and_closes_provider_clients_in_each_job_loop() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        x_list = upsert_list(
            session,
            platform_list_id="123456789",
            name="Private list",
        )
        list_id = x_list.id
        TokenVault(session=session, encryption_key=FERNET_KEY).store(
            provider="x",
            token_payload={
                "access_token": "expired-access",
                "refresh_token": "current-refresh",
                "expires_in": 1,
                "scope": "tweet.read users.read list.read offline.access",
            },
            now=datetime(2020, 1, 1, tzinfo=UTC),
        )

    oauth_clients: list[LoopBoundAsyncClient] = []
    api_clients: list[LoopBoundAsyncClient] = []
    oauth_requests: list[httpx.Request] = []
    api_requests: list[httpx.Request] = []

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        oauth_requests.append(request)
        invocation = len(oauth_requests)
        return httpx.Response(
            200,
            json={
                "access_token": f"refreshed-access-{invocation}",
                "refresh_token": f"rotated-refresh-{invocation}",
                "expires_in": 1,
                "scope": "tweet.read users.read list.read offline.access",
            },
        )

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    def oauth_client_factory() -> httpx.AsyncClient:
        client = LoopBoundAsyncClient(oauth_handler)
        oauth_clients.append(client)
        return client

    def api_client_factory() -> httpx.AsyncClient:
        client = LoopBoundAsyncClient(api_handler)
        api_clients.append(client)
        return client

    runner = build_production_pipeline_runner(
        _settings(),
        factory,
        oauth_http_client_factory=oauth_client_factory,
        x_api_http_client_factory=api_client_factory,
    )
    services = PipelineSchedulerServices(session_factory=factory, pipeline_runner=runner)

    _run_pipeline(services, list_id)
    _run_pipeline(services, list_id)

    assert len(oauth_requests) == 2
    assert len(api_requests) == 2
    assert len(oauth_clients) == 2
    assert len(api_clients) == 2
    assert oauth_clients[0].created_loop is api_clients[0].created_loop
    assert oauth_clients[1].created_loop is api_clients[1].created_loop
    assert oauth_clients[0].created_loop is not oauth_clients[1].created_loop
    assert all(client.closed_loop is client.created_loop for client in oauth_clients + api_clients)
    assert all(client.is_closed for client in oauth_clients + api_clients)


def test_concurrent_scheduled_pipelines_coordinate_refresh_across_job_loops(
    tmp_path: Path,
) -> None:
    asyncio.run(_contend_process_refresh_lock_once())
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'scheduler.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        list_ids = [
            upsert_list(
                session,
                platform_list_id=platform_list_id,
                name=f"Private list {platform_list_id}",
            ).id
            for platform_list_id in ("123456789", "987654321")
        ]
        TokenVault(session=session, encryption_key=FERNET_KEY).store(
            provider="x",
            token_payload={
                "access_token": "expired-access",
                "refresh_token": "current-refresh",
                "expires_in": 1,
                "scope": "tweet.read users.read list.read offline.access",
            },
            now=datetime(2020, 1, 1, tzinfo=UTC),
        )

    clients_ready = threading.Barrier(2)
    oauth_clients: list[LoopBoundAsyncClient] = []
    api_clients: list[LoopBoundAsyncClient] = []
    oauth_requests: list[httpx.Request] = []
    api_requests: list[httpx.Request] = []

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        oauth_requests.append(request)
        time.sleep(0.2)
        return httpx.Response(
            200,
            json={
                "access_token": "shared-refreshed-access",
                "refresh_token": "shared-rotated-refresh",
                "expires_in": 3600,
                "scope": "tweet.read users.read list.read offline.access",
            },
        )

    def api_handler(request: httpx.Request) -> httpx.Response:
        api_requests.append(request)
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    def oauth_client_factory() -> httpx.AsyncClient:
        client = LoopBoundAsyncClient(oauth_handler)
        oauth_clients.append(client)
        clients_ready.wait(timeout=2)
        return client

    def api_client_factory() -> httpx.AsyncClient:
        client = LoopBoundAsyncClient(api_handler)
        api_clients.append(client)
        return client

    production_runner = build_production_pipeline_runner(
        _settings(),
        factory,
        oauth_http_client_factory=oauth_client_factory,
        x_api_http_client_factory=api_client_factory,
    )

    async def bounded_runner(list_id: str):
        asyncio.get_running_loop().set_debug(True)
        return await asyncio.wait_for(production_runner(list_id), timeout=2)

    services = PipelineSchedulerServices(
        session_factory=factory,
        pipeline_runner=bounded_runner,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_run_pipeline, services, list_id) for list_id in list_ids]
        for future in futures:
            future.result(timeout=5)

    assert len(oauth_requests) == 1
    assert len(api_requests) == 2
    assert len(oauth_clients) == 2
    assert len(api_clients) == 2
    assert oauth_clients[0].created_loop is not oauth_clients[1].created_loop
    assert all(client.closed_loop is client.created_loop for client in oauth_clients + api_clients)
    assert all(client.is_closed for client in oauth_clients + api_clients)
