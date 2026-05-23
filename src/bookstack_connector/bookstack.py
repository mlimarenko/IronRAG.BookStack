"""Async BookStack REST client.

Surface used by the connector:

* `get_page_metadata(page_id)`         → page JSON (name, slug, book_id, chapter_id, tags, ...)
* `get_book_metadata(book_id)`         → book JSON (slug, name, tags, ...)
* `get_chapter_metadata(chapter_id)`   → chapter JSON
* `export_page(page_id, fmt)`          → raw bytes + canonical MIME
* `list_pages()`                       → iterable over all pages
* `list_shelves()`                     → iterable over all shelves
* `get_shelf_metadata(shelf_id)`       → shelf JSON with `books: [...]` array

Auth and retry semantics match `~/sources/bookstack.exporter`.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import time
from collections.abc import AsyncIterator
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from ironrag_connector.observability import get_logger

from .config import BookStackSettings, ExportFormat

log = get_logger(__name__)

FORMAT_MIME = {
    "markdown": "text/markdown",
    "html": "text/html",
    "plaintext": "text/plain",
    "pdf": "application/pdf",
}

FORMAT_EXTENSION = {
    "markdown": "md",
    "html": "html",
    "plaintext": "txt",
    "pdf": "pdf",
}

RETRYABLE_STATUS = {429, 502, 503, 504}


class BookStackError(RuntimeError):
    """BookStack API surfaced an error we cannot retry past."""


class BookStackPageNotFoundError(BookStackError):
    """A specific page does not exist (HTTP 404)."""


class BookStackClient:
    def __init__(
        self, settings: BookStackSettings, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._normalise_base_url(settings.bookstack_base_url),
            timeout=settings.request_timeout_seconds,
            headers={
                "Authorization": (
                    f"Token {settings.bookstack_token_id}:{settings.bookstack_token_secret}"
                ),
                "Accept": "application/json",
            },
        )
        # Rate-limit gate: enforce a minimum interval between outbound requests.
        # asyncio.Lock serialises concurrent callers so only one request fires
        # at a time, then we sleep for the remainder of the min interval before
        # releasing — giving the next waiter a clean slot.
        self._rate_lock = asyncio.Lock()
        self._last_request_at: float = 0.0

    @staticmethod
    def _normalise_base_url(base_url: str) -> str:
        base_url = base_url.rstrip("/")
        if base_url.endswith("/api"):
            return base_url[:-4]
        return base_url

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> BookStackClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        attempt = 0
        while True:
            # Rate-limit gate: hold the lock for the full request + inter-call
            # sleep so that concurrent callers are naturally serialised.
            async with self._rate_lock:
                min_interval = self._settings.bookstack_min_request_interval_seconds
                if min_interval > 0:
                    elapsed = time.monotonic() - self._last_request_at
                    wait = min_interval - elapsed
                    if wait > 0:
                        await asyncio.sleep(wait)
                self._last_request_at = time.monotonic()
                response = await self._client.request(
                    method, path, params=params, headers={"Accept": accept}
                )

            retryable = (
                response.status_code in RETRYABLE_STATUS
                and attempt < self._settings.bookstack_retry_max
            )
            if retryable:
                delay = self._retry_delay(response.headers, attempt)
                log.warning(
                    "bookstack.retry",
                    status=response.status_code,
                    attempt=attempt,
                    delay_seconds=delay,
                    path=path,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            return response

    def _retry_delay(self, headers: httpx.Headers, attempt: int) -> float:
        retry_after = headers.get("Retry-After")
        if retry_after:
            stripped = retry_after.strip()
            if stripped.isdigit():
                return min(float(stripped), self._settings.bookstack_retry_max_sleep_seconds)
            try:
                target = parsedate_to_datetime(stripped)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                from time import time

                delta = max(0.0, target.timestamp() - time())
                return min(delta, self._settings.bookstack_retry_max_sleep_seconds)
            except (TypeError, ValueError, OverflowError):
                pass
        delay = self._settings.bookstack_retry_backoff_seconds * (2**attempt)
        return min(delay, self._settings.bookstack_retry_max_sleep_seconds)

    async def get_page_metadata(self, page_id: int) -> dict[str, Any]:
        return await self._get_json(f"/api/pages/{page_id}", entity=f"page {page_id}")

    async def get_book_metadata(self, book_id: int) -> dict[str, Any]:
        return await self._get_json(f"/api/books/{book_id}", entity=f"book {book_id}")

    async def get_chapter_metadata(self, chapter_id: int) -> dict[str, Any]:
        return await self._get_json(
            f"/api/chapters/{chapter_id}", entity=f"chapter {chapter_id}"
        )

    async def get_shelf_metadata(self, shelf_id: int) -> dict[str, Any]:
        return await self._get_json(f"/api/shelves/{shelf_id}", entity=f"shelf {shelf_id}")

    async def _get_json(self, path: str, *, entity: str) -> dict[str, Any]:
        response = await self._request("GET", path)
        if response.status_code == 404:
            raise BookStackPageNotFoundError(f"BookStack {entity} not found")
        if response.status_code >= 400:
            raise BookStackError(
                f"BookStack {path} → {response.status_code}: {response.text[:400]}"
            )
        return response.json()

    async def export_page(self, page_id: int, fmt: ExportFormat) -> tuple[bytes, str]:
        response = await self._request(
            "GET",
            f"/api/pages/{page_id}/export/{fmt}",
            accept=FORMAT_MIME[fmt],
        )
        if response.status_code == 404:
            raise BookStackPageNotFoundError(
                f"BookStack page {page_id} not found during export"
            )
        if response.status_code >= 400:
            raise BookStackError(
                f"BookStack export {fmt} for page {page_id} → {response.status_code}: "
                f"{response.text[:400]}"
            )
        return response.content, FORMAT_MIME[fmt]

    async def list_pages(self, page_size: int = 100) -> AsyncIterator[dict[str, Any]]:
        async for item in self._iterate("/api/pages", page_size):
            yield item

    async def list_shelves(self, page_size: int = 100) -> AsyncIterator[dict[str, Any]]:
        async for item in self._iterate("/api/shelves", page_size):
            yield item

    async def list_page_attachments(self, page_id: int) -> list[dict[str, Any]]:
        """All attachments belonging to one page via /api/attachments?filter[uploaded_to]=<id>."""
        results: list[dict[str, Any]] = []
        async for item in self._iterate(
            "/api/attachments", 100, extra_params={"filter[uploaded_to]": str(page_id)}
        ):
            results.append(item)
        return results

    async def get_attachment_payload(
        self, attachment_id: int
    ) -> tuple[bytes, str, str]:
        """Return (bytes, mime_type, file_name) for a single attachment.

        BookStack returns base64 in ``content`` for non-external attachments.
        For external attachments (URL pointer), download from the URL.
        MIME type is guessed from the file extension.
        """
        detail = await self._get_json(
            f"/api/attachments/{attachment_id}",
            entity=f"attachment {attachment_id}",
        )
        name: str = detail.get("name") or f"attachment-{attachment_id}"
        extension: str = detail.get("extension") or ""
        file_name = name if "." in name else (f"{name}.{extension}" if extension else name)

        mime_type, _ = mimetypes.guess_type(file_name)
        if not mime_type:
            mime_type = "application/octet-stream"

        is_external: bool = bool(detail.get("external", False))
        content_field = detail.get("content") or ""

        if is_external:
            # External attachments store a URL in content.
            url: str = content_field if isinstance(content_field, str) else ""
            if not url:
                return b"", mime_type, file_name
            response = await self._client.get(url, follow_redirects=True)
            if response.status_code == 404:
                return b"", mime_type, file_name
            if response.status_code >= 400:
                raise BookStackError(
                    f"External attachment {attachment_id} download → "
                    f"{response.status_code}: {response.text[:400]}"
                )
            return response.content, mime_type, file_name

        # Non-external: content is base64-encoded file bytes.
        if isinstance(content_field, str) and content_field:
            try:
                raw_bytes = base64.b64decode(content_field)
                return raw_bytes, mime_type, file_name
            except Exception:
                pass
        return b"", mime_type, file_name

    async def download_image(self, image_url: str) -> tuple[bytes, str]:
        """Fetch a BookStack /uploads/... image using the same Authorization header.

        Returns (bytes, mime_type).  On 404 returns (b"", "") as a soft miss.
        """
        response = await self._client.get(image_url, follow_redirects=True)
        if response.status_code == 404:
            log.debug("bookstack.download_image.not_found", url=image_url)
            return b"", ""
        if response.status_code >= 400:
            raise BookStackError(
                f"BookStack image download {image_url} → {response.status_code}: "
                f"{response.text[:200]}"
            )
        content_type = response.headers.get("content-type", "")
        mime_type = content_type.split(";")[0].strip() or "application/octet-stream"
        return response.content, mime_type

    async def _iterate(
        self,
        path: str,
        page_size: int,
        *,
        extra_params: dict[str, str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        while True:
            params: dict[str, Any] = {"count": page_size, "offset": offset}
            if extra_params:
                params.update(extra_params)
            response = await self._request("GET", path, params=params)
            if response.status_code >= 400:
                raise BookStackError(
                    f"BookStack {path} → {response.status_code}: {response.text[:400]}"
                )
            payload = response.json()
            items: list[dict[str, Any]] = payload.get("data", [])
            for item in items:
                yield item
            total = payload.get("total", offset + len(items))
            offset += page_size
            if offset >= total or not items:
                return
