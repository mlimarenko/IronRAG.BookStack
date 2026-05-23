"""Tests for BookStackClient rate-limit gate."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

from bookstack_connector.bookstack import BookStackClient
from bookstack_connector.config import BookStackSettings


def _settings(**overrides: Any) -> BookStackSettings:
    base = {
        "bookstack_base_url": "http://wiki.example.com",
        "bookstack_token_id": "tid",
        "bookstack_token_secret": "tsecret",
        "bookstack_webhook_bearer": "bearer-token",
        "ironrag_base_url": "http://ironrag.example.com",
        "ironrag_api_token": "irtoken",
        "routing_config_path": "/nonexistent/routing.yaml",
        "bookstack_min_request_interval_seconds": 0.0,
    }
    base.update(overrides)
    return BookStackSettings.model_validate(base)


def _mock_client(status_code: int = 200, json_body: dict | None = None) -> httpx.AsyncClient:
    """Return an httpx.AsyncClient stub that always returns the given status."""
    mock = MagicMock(spec=httpx.AsyncClient)
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.headers = httpx.Headers({})
    response.json.return_value = json_body or {"data": [], "total": 0}
    response.text = ""
    response.content = b""
    mock.request = AsyncMock(return_value=response)
    return mock


class TestRateLimitGate:
    async def test_zero_interval_no_sleep(self):
        """With interval=0, two sequential requests complete without extra sleep."""
        client = _mock_client()
        settings = _settings(bookstack_min_request_interval_seconds=0.0)
        bs = BookStackClient(settings, client=client)

        t0 = time.monotonic()
        await bs._request("GET", "/api/pages/1")
        await bs._request("GET", "/api/pages/2")
        elapsed = time.monotonic() - t0

        # Should be fast — definitely under 0.3 s even on a slow machine.
        assert elapsed < 0.3
        assert client.request.await_count == 2

    async def test_interval_enforced_between_requests(self):
        """Two requests with interval=0.15 s must be at least 0.14 s apart."""
        client = _mock_client()
        settings = _settings(bookstack_min_request_interval_seconds=0.15)
        bs = BookStackClient(settings, client=client)

        t0 = time.monotonic()
        await bs._request("GET", "/api/pages/1")
        t1 = time.monotonic()
        await bs._request("GET", "/api/pages/2")
        t2 = time.monotonic()

        gap = t2 - t1
        total = t2 - t0
        # Second request must wait for the remaining interval from when the
        # first one started; total elapsed ≥ interval.
        assert total >= 0.14, f"total elapsed {total:.3f}s < 0.14s"
        assert gap >= 0.0  # sanity

    async def test_concurrent_requests_serialised(self):
        """Concurrent callers must be serialised — no parallel outbound requests."""
        call_times: list[float] = []

        mock_client = MagicMock(spec=httpx.AsyncClient)
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.headers = httpx.Headers({})
        response.json.return_value = {"data": [], "total": 0}
        response.text = ""

        async def _request(*args: Any, **kwargs: Any) -> MagicMock:
            call_times.append(time.monotonic())
            return response

        mock_client.request = _request

        settings = _settings(bookstack_min_request_interval_seconds=0.1)
        bs = BookStackClient(settings, client=mock_client)

        # Fire 3 concurrent requests.
        await asyncio.gather(
            bs._request("GET", "/api/pages/1"),
            bs._request("GET", "/api/pages/2"),
            bs._request("GET", "/api/pages/3"),
        )

        assert len(call_times) == 3
        # Each request must be at least ~0.09 s after the previous.
        for i in range(1, len(call_times)):
            gap = call_times[i] - call_times[i - 1]
            assert gap >= 0.09, f"gap {i}: {gap:.3f}s < 0.09s (requests not serialised)"
