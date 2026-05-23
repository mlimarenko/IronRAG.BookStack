"""SourceAdapter implementation for BookStack.

What the adapter emits
======================

* ``page`` refs for every page returned by ``/api/pages``. Routing facts
  include ``shelf_slugs``, ``book_slug``, ``chapter_slug``, ``tags``,
  ``page_slug`` so the operator can map a page to a target library
  using BookStack vocabulary without the framework knowing it.
* When the framework asks for a page's full payload, the adapter also
  attaches each of the page's BookStack attachments and each inline
  image referenced in the exported markdown as dependent
  :class:`SourceItem`s. Dependents reuse the same orchestrator code path
  and obey their own kind's policy.

Reaping
=======

The framework's reaper walks IronRAG documents under the three
``bookstack:`` external-key prefixes and applies the per-kind
``on_missing`` policy. Images default to ``ignore`` because they are
content-addressed: a sha256 prefix that no live page references right
now may still be valid for a page that hides it behind a feature flag
or seasonal switch.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from ironrag_connector import SourceAdapter, SourceItem, SourceItemRef
from ironrag_connector.observability import get_logger

from .bookstack import (
    FORMAT_EXTENSION,
    BookStackClient,
    BookStackPageNotFoundError,
)
from .config import BookStackSettings
from .mapping import (
    CONNECTOR_NAME,
    KIND_ATTACHMENT,
    KIND_IMAGE,
    KIND_PAGE,
    KINDS,
    attachment_external_key,
    build_external_key,
    image_external_key,
    page_external_key,
    parse_external_key,
)
from .markdown import collect_bookstack_image_urls
from .page_context import PageContextBuilder, ShelfIndex

log = get_logger(__name__)


class BookStackAdapter(SourceAdapter):
    name = CONNECTOR_NAME
    kinds = KINDS
    # Pages are enumerated; attachments and images only come as
    # dependents from a page fetch, so reaping them on the basis of
    # this sweep's `seen` set would be wrong.
    primary_kinds = (KIND_PAGE,)

    def __init__(self, settings: BookStackSettings) -> None:
        self._settings = settings
        self._client = BookStackClient(settings)
        # Browser-facing base URL: same canonicalization as the REST
        # client (strip trailing slash, drop /api suffix) but computed
        # inline so tests that mock BookStackClient still get a real string.
        raw = settings.bookstack_base_url.rstrip("/")
        self._browser_base_url = raw[:-4] if raw.endswith("/api") else raw
        self._shelves = ShelfIndex(self._client, settings.bookstack_shelf_cache_ttl_seconds)
        self._context_builder = PageContextBuilder(
            settings,
            self._client,
            self._shelves,
            needs_shelves=True,
            needs_tags=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def external_key(self, kind: str, item_id: str) -> str:
        return build_external_key(kind, item_id)

    def parse_external_key(self, external_key: str) -> tuple[str, str] | None:
        return parse_external_key(external_key)

    async def iter_items(self) -> AsyncIterator[SourceItemRef]:
        async for page in self._client.list_pages():
            page_id = page.get("id")
            if not isinstance(page_id, int) or page_id <= 0:
                continue
            updated_at = page.get("updated_at") if isinstance(page.get("updated_at"), str) else None
            facts = await self._routing_facts_for_page(page_id, page)
            yield SourceItemRef(
                item_id=str(page_id),
                kind=KIND_PAGE,
                external_key=page_external_key(page_id),
                change_token=updated_at,
                routing_facts=facts,
                raw=page,
            )

    async def fetch(self, ref: SourceItemRef) -> SourceItem | None:
        if ref.kind != KIND_PAGE:
            log.warning("bookstack.fetch.unexpected_kind", kind=ref.kind)
            return None
        try:
            page_id = int(ref.item_id)
        except ValueError:
            return None
        try:
            content_bytes, mime_type = await self._client.export_page(
                page_id, self._settings.bookstack_page_format
            )
        except BookStackPageNotFoundError:
            return None

        slug = (
            ref.raw.get("slug")
            if isinstance(ref.raw.get("slug"), str)
            else f"page-{page_id}"
        )
        page_name = ref.raw.get("name") if isinstance(ref.raw.get("name"), str) else None
        extension = FORMAT_EXTENSION[self._settings.bookstack_page_format]
        file_name = f"{slug}.{extension}"

        # Inline images and attachments are pushed as dependents in the
        # same orchestrator pass.
        dependents: list[SourceItem] = []
        if mime_type == "text/markdown":
            dependents.extend(
                await self._build_image_dependents(
                    page_id, page_name, content_bytes, ref
                )
            )
        dependents.extend(
            await self._build_attachment_dependents(page_id, page_name, ref)
        )

        return SourceItem(
            ref=ref,
            payload=content_bytes,
            mime_type=mime_type,
            file_name=file_name,
            title=page_name,
            document_hint=self._page_document_hint(page_id, ref),
            dependents=tuple(dependents),
        )

    async def _routing_facts_for_page(
        self, page_id: int, summary: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            ctx = await self._context_builder.build(page_id)
        except BookStackPageNotFoundError:
            return {}
        return {
            "page_id": ctx.page_id,
            "page_slug": ctx.page_slug,
            "book_id": ctx.book_id,
            "book": ctx.book_slug,
            "chapter_id": ctx.chapter_id,
            "chapter": ctx.chapter_slug,
            "shelf_ids": list(ctx.shelf_ids),
            "shelf": list(ctx.shelf_slugs),
            "tag": list(ctx.tags),
        }

    async def _build_image_dependents(
        self,
        page_id: int,
        page_name: str | None,
        content_bytes: bytes,
        parent: SourceItemRef,
    ) -> list[SourceItem]:
        try:
            text = content_bytes.decode("utf-8", errors="replace")
            urls = await collect_bookstack_image_urls(
                text, self._browser_base_url
            )
        except Exception as exc:
            log.warning(
                "bookstack.fetch.collect_images_failed",
                page_id=page_id,
                error=str(exc),
            )
            return []

        items: list[SourceItem] = []
        for url in urls:
            try:
                img_bytes, mime = await self._client.download_image(url)
            except Exception as exc:
                log.warning(
                    "bookstack.fetch.image_download_failed",
                    page_id=page_id,
                    url=url,
                    error=str(exc),
                )
                continue
            if not img_bytes or not mime.startswith("image/"):
                continue
            digest = hashlib.sha256(img_bytes).hexdigest()
            file_name = url.rsplit("/", 1)[-1] or f"image-{digest[:16]}"
            ext_key = image_external_key(digest)
            ref = SourceItemRef(
                item_id=digest[:16],
                kind=KIND_IMAGE,
                external_key=ext_key,
                change_token=digest,
                routing_facts=parent.routing_facts,
                raw={"page_id": page_id, "url": url},
            )
            items.append(
                SourceItem(
                    ref=ref,
                    payload=img_bytes,
                    mime_type=mime,
                    file_name=file_name,
                    title=f"{page_name or 'page'}: {file_name}",
                    idempotency_key=f"bookstack:image:{digest[:16]}",
                    document_hint=url,
                )
            )
        return items

    async def _build_attachment_dependents(
        self,
        page_id: int,
        page_name: str | None,
        parent: SourceItemRef,
    ) -> list[SourceItem]:
        try:
            attachments = await self._client.list_page_attachments(page_id)
        except Exception as exc:
            log.warning(
                "bookstack.fetch.list_attachments_failed",
                page_id=page_id,
                error=str(exc),
            )
            return []

        items: list[SourceItem] = []
        for att in attachments:
            att_id = att.get("id")
            if not isinstance(att_id, int) or att_id <= 0:
                continue
            try:
                att_bytes, att_mime, att_filename = await self._client.get_attachment_payload(
                    att_id
                )
            except Exception as exc:
                log.warning(
                    "bookstack.fetch.attachment_download_failed",
                    page_id=page_id,
                    attachment_id=att_id,
                    error=str(exc),
                )
                continue
            if not att_bytes:
                continue
            updated_at = (
                att.get("updated_at") if isinstance(att.get("updated_at"), str) else None
            )
            ref = SourceItemRef(
                item_id=str(att_id),
                kind=KIND_ATTACHMENT,
                external_key=attachment_external_key(att_id),
                change_token=updated_at,
                routing_facts=parent.routing_facts,
                raw={"page_id": page_id, "name": att.get("name")},
            )
            att_name = att.get("name") or f"attachment-{att_id}"
            items.append(
                SourceItem(
                    ref=ref,
                    payload=att_bytes,
                    mime_type=att_mime,
                    file_name=att_filename,
                    title=f"{page_name or 'Page'}: {att_name}",
                    document_hint=f"{self._browser_base_url}/attachments/{att_id}",
                )
            )
        return items

    def _page_document_hint(self, page_id: int, ref: SourceItemRef) -> str:
        page_slug = _string_fact(ref.raw, "page_slug", "slug") or _string_fact(
            ref.routing_facts, "page_slug"
        )
        book_slug = _string_fact(ref.raw, "book_slug", "book") or _string_fact(
            ref.routing_facts, "book_slug", "book"
        )
        if page_slug and book_slug:
            return f"{self._browser_base_url}/books/{book_slug}/page/{page_slug}"
        return f"{self._browser_base_url}/pages/{page_id}"


def _string_fact(source: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None
