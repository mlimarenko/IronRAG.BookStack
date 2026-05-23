# Architecture

## Position in the stack

```
┌─────────────────┐  page_create/update/delete   ┌──────────────────────┐
│   BookStack     │ ───────────────────────────▶ │  bookstack-connector │
│   (wiki)        │   webhook (HMAC or Bearer)   │   (this service)     │
└────────┬────────┘                              └──────────┬───────────┘
         │                                                  │
         │ /api/pages/{id}/export/markdown                  │ /v1/content/documents/upload
         │ /api/pages/{id}                                  │ /v1/content/documents/{id}/replace
         │                                                  │ DELETE /v1/content/documents/{id}
         ▼                                                  ▼
┌─────────────────┐                              ┌──────────────────────┐
│   BookStack     │                              │       IronRAG        │
│   REST API      │                              │   (canonical RAG)    │
└─────────────────┘                              └──────────────────────┘
```

The connector is the **only** component that holds vendor knowledge. IronRAG
keeps using its existing `content_document.external_key` as the canonical
identity surface; everything BookStack-flavoured (token format, retry headers,
event names) is contained here.

## Event lifecycle

1. BookStack fires a webhook on page create / update / delete.
2. Connector authorises the request (HMAC SHA-256 over body **or** Bearer token).
3. `webhook.parse_event` filters for `page_create | page_update | page_delete`;
   non-page events return `202 ignored`.
4. `Orchestrator.handle_event`:
   - For create/update: fetches the page export (markdown by default) via
     BookStack REST, looks up an existing IronRAG document by
     `external_key = bookstack:page:<id>`, then either `replace` or `upload`.
   - For delete: looks up the document, calls `DELETE /v1/content/documents/{id}`.
5. IronRAG admit_mutation enqueues an ingest job; chunk reuse + image-checksum
   gate kick in for replaces (no full re-embed for unchanged content).

Idempotency: every IronRAG mutation is dispatched with
`idempotency_key = bookstack:<event_kind>:<page_id>`. Re-delivery of the same
event therefore collapses on the IronRAG side without creating duplicate
revisions.

## Failure modes and behaviour

| Event from BookStack | Outcome on the IronRAG side |
|----------------------|-----------------------------|
| `page_update` for an unknown page (404 from `/api/pages/{id}/export`) | `skipped_missing`, no IronRAG mutation |
| `page_delete` for a page never ingested | `skipped_missing`, no IronRAG mutation |
| BookStack returns 429 / 5xx | up to `BOOKSTACK_RETRY_MAX` retries with `Retry-After` honoured |
| IronRAG returns 4xx | propagated as `IronRagError`; FastAPI returns 502 to BookStack so it can retry |
| Webhook missing/invalid auth | 401 / 403; BookStack admin sees the failure and rotates the secret |

## Mapping rules

* **External key** is the only identity surface used:
  `bookstack:page:<page_id>`. A rename or slug change keeps the same key;
  IronRAG sees a `replace`, not a delete + create.
* **Books / chapters** are not ingested. Page metadata (`book_id`,
  `chapter_id`) is forwarded as `metadata` for future use; today IronRAG does
  not surface it specifically.
* **Format**: `BOOKSTACK_PAGE_FORMAT` defaults to `markdown` because IronRAG's
  Markdown adapter is the cleanest path; `html`, `plaintext`, and `pdf` are
  also accepted (the orchestrator forwards the matching MIME type to IronRAG).

## What the connector deliberately does NOT do

* No background polling / backfill — that is the role of the existing
  `~/sources/bookstack.exporter` plus a one-shot bulk upload.
* No vendor-specific logic in IronRAG itself — the IronRAG repo only sees
  uploads tagged with an external key.
* No persistence of its own. The mapping `bookstack:page:<id> → IronRAG
  document_id` is owned by IronRAG (`content_document.external_key`).
* No HMAC verification of outbound IronRAG signatures (the connector is a
  sender, not a receiver, of IronRAG webhook events; if you also want to
  receive `revision.ready` from IronRAG and forward elsewhere, that lives in
  a separate subscriber service).

## Operational notes

* Sized for low write throughput (typical wiki traffic: dozens of edits per
  hour). One uvicorn worker handles thousands of webhooks per hour easily; a
  reverse-proxy ahead of the connector is mandatory for TLS but optional for
  load distribution.
* Logs are structured JSON via structlog; key fields per event include
  `page_id`, `event_kind`, `external_key`, `action`, and
  `ironrag_document_id`.
* Health: `GET /health` returns `{"status": "ok"}`.
* When IronRAG's library is recreated (UUID changes) the connector must be
  restarted with the new `IRONRAG_LIBRARY_ID`. There is no per-event library
  override on purpose: one connector instance ↔ one library.
