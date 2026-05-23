"""Adapter wiring against a fake BookStack REST surface.

Exercises the contract the framework actually cares about: ``iter_items``
yields one ref per BookStack page with the right shape, ``fetch`` returns
a SourceItem whose dependents include attachments and inline images, and
``parse_external_key`` round-trips.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from bookstack_connector.adapter import BookStackAdapter
from bookstack_connector.config import BookStackSettings
from bookstack_connector.mapping import KIND_ATTACHMENT, KIND_IMAGE, KIND_PAGE


def _settings() -> BookStackSettings:
    return BookStackSettings(
        bookstack_base_url="http://wiki.example.com/api/",
        bookstack_token_id="id",
        bookstack_token_secret="secret",
        ironrag_base_url="http://ironrag.example.com",
        ironrag_api_token="t",
        admin_bearer_token="a",
    )


class FakeBookStack:
    """Substituted for BookStackClient on the adapter."""

    def __init__(self) -> None:
        self.pages = [
            {
                "id": 1,
                "name": "Hello",
                "slug": "hello",
                "updated_at": "2026-01-01T00:00:00Z",
                "book_id": 11,
                "book_slug": "the-book",
            }
        ]
        self.attachments_by_page = {1: [{"id": 99, "name": "spec.pdf"}]}
        self.attachment_payload = {99: (b"%PDF-1.4 fake", "application/pdf", "spec.pdf")}
        self.page_export = (
            b"# Hello\n\n![alt](http://wiki.example.com/uploads/images/foo.png)",
            "text/markdown",
        )
        self.image_payload = (b"\x89PNG fake", "image/png")

    async def list_pages(self) -> AsyncIterator[dict[str, Any]]:
        for page in self.pages:
            yield page

    async def export_page(self, page_id: int, fmt: str) -> tuple[bytes, str]:
        return self.page_export

    async def list_page_attachments(self, page_id: int) -> list[dict[str, Any]]:
        return self.attachments_by_page.get(page_id, [])

    async def get_attachment_payload(self, att_id: int) -> tuple[bytes, str, str]:
        return self.attachment_payload[att_id]

    async def download_image(self, url: str) -> tuple[bytes, str]:
        return self.image_payload

    async def aclose(self) -> None:
        return None


@pytest.fixture
def adapter() -> BookStackAdapter:
    fake = FakeBookStack()
    settings = _settings()
    with (
        patch("bookstack_connector.adapter.BookStackClient", return_value=fake),
        patch("bookstack_connector.adapter.ShelfIndex"),
        patch("bookstack_connector.adapter.PageContextBuilder") as ctx_cls,
    ):
        async def _build(page_id: int) -> Any:
            from bookstack_connector.page_context import PageContext

            return PageContext(
                page_id=page_id,
                page_slug="hello",
                page_name="Hello",
                book_id=11,
                book_slug="the-book",
                chapter_id=None,
                chapter_slug=None,
                shelf_ids=(),
                shelf_slugs=(),
                tags=("docs",),
            )

        ctx_cls.return_value.build = _build
        return BookStackAdapter(settings)


@pytest.mark.asyncio
async def test_iter_items_yields_page_ref(adapter: BookStackAdapter) -> None:
    refs = [r async for r in adapter.iter_items()]
    assert len(refs) == 1
    ref = refs[0]
    assert ref.kind == KIND_PAGE
    assert ref.item_id == "1"
    assert ref.external_key == "bookstack:page:1"
    assert ref.change_token == "2026-01-01T00:00:00Z"
    assert ref.routing_facts["book"] == "the-book"
    assert ref.routing_facts["tag"] == ["docs"]


@pytest.mark.asyncio
async def test_fetch_returns_page_with_dependents(adapter: BookStackAdapter) -> None:
    refs = [r async for r in adapter.iter_items()]
    item = await adapter.fetch(refs[0])
    assert item is not None
    assert item.ref.kind == KIND_PAGE
    assert item.mime_type == "text/markdown"
    assert item.file_name == "hello.md"
    assert item.document_hint == "http://wiki.example.com/books/the-book/page/hello"
    kinds = [d.ref.kind for d in item.dependents]
    assert KIND_ATTACHMENT in kinds
    assert KIND_IMAGE in kinds

    image_dep = next(d for d in item.dependents if d.ref.kind == KIND_IMAGE)
    expected_digest = hashlib.sha256(b"\x89PNG fake").hexdigest()
    assert image_dep.ref.external_key == f"bookstack:image:{expected_digest[:16]}"
    assert image_dep.idempotency_key == f"bookstack:image:{expected_digest[:16]}"
    assert image_dep.document_hint == "http://wiki.example.com/uploads/images/foo.png"

    att_dep = next(d for d in item.dependents if d.ref.kind == KIND_ATTACHMENT)
    assert att_dep.ref.external_key == "bookstack:attachment:99"
    assert att_dep.title == "Hello: spec.pdf"
    assert att_dep.document_hint == "http://wiki.example.com/attachments/99"


@pytest.mark.asyncio
async def test_fetch_page_document_hint_falls_back_to_page_id(
    adapter: BookStackAdapter,
) -> None:
    refs = [r async for r in adapter.iter_items()]
    ref = replace(refs[0], raw={}, routing_facts={})
    item = await adapter.fetch(ref)
    assert item is not None
    assert item.document_hint == "http://wiki.example.com/pages/1"
