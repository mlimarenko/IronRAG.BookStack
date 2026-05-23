# BookStack ↔ IronRAG connector — Changelog

## 0.0.2 — 2026-05-17

- Added per-kind `document_hint` URLs so pages, attachments, and inline images
  point IronRAG citations back to their original BookStack browser URLs.

## 0.0.1 — 2026-05-17

Initial public release on top of the
[IronRAG Connector Template](https://github.com/mlimarenko/IronRAG.ConnectorTemplate).

### Sync behavior

- Periodic poll loop (default 1800s) ships only pages whose BookStack
  `updated_at` advanced since the last successful push. Webhook
  endpoint `/webhook/bookstack` (HMAC-SHA256 + admin bearer)
  dispatches through the same orchestrator.
- Three external-key namespaces, each with its own per-kind policy:
  - `bookstack:page:<id>` — page markdown export.
  - `bookstack:attachment:<id>` — base64-decoded attachment payload.
  - `bookstack:image:<sha256-prefix-16>` — content-addressed dedup of
    inline images across pages.
- Routing YAML maps shelf / book / chapter / tag / page-slug facts
  emitted by the adapter to `(workspace, library)` pairs. Per-kind
  policy overrides — pages and attachments default to
  `on_missing: delete`, images to `on_missing: ignore`.
- Orphan reaper runs after each clean sweep against the `page` kind
  only; attachments and images are dependent kinds whose lifecycle
  follows their parent.

### Distribution

- Docker image: `pipingspace/ironrag.bookstack:0.0.1` (multi-arch).
- 21 unit tests, ruff strict.
