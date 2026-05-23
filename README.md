<h1 align="center">IronRAG ↔ BookStack connector</h1>
<p align="center"><b>Sync a BookStack wiki into <a href="https://github.com/mlimarenko/IronRAG">IronRAG</a>: periodic polling + webhook intake, multimodal (pages + attachments + images).</b></p>

<p align="center">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/docker/pulls/pipingspace/ironrag.bookstack?style=flat-square&label=docker%20pulls" alt="Docker pulls">
  <img src="https://img.shields.io/badge/python-3.12%2B-blue?style=flat-square" alt="Python">
</p>

---

Built on the [IronRAG Connector Template](https://github.com/mlimarenko/IronRAG.ConnectorTemplate) — this repo only owns BookStack-specific code (REST client, markdown image collector, shelf reverse index, webhook verifier).

## What it pushes into IronRAG

| `kind` | external key | source |
|---|---|---|
| `page` | `bookstack:page:<id>` | `/api/pages/{id}/export/markdown` |
| `attachment` | `bookstack:attachment:<id>` | `/api/attachments/{id}` (base64 decoded) |
| `image` | `bookstack:image:<sha256-prefix-16>` | inline `<img>` URLs in page markdown (content-addressed dedup) |

Pages, attachments, and inline images all carry their original BookStack URL via
`document_hint`, which IronRAG surfaces in MCP citations.

Sync loop runs every `SYNC_INTERVAL_SECONDS` (default 1800s) and ships only the diff: pages whose `updated_at` advanced since the last successful push. Webhook endpoint `/webhook/bookstack` (HMAC-SHA256 + admin bearer) routes through the same orchestrator.

## Quick start (Docker)

```bash
docker run -d \
    --name ironrag-bookstack \
    --env-file .env.local \
    -v $(pwd)/routing.yaml:/app/routing.yaml:ro \
    -v ironrag_bookstack_state:/var/lib/ironrag-connector \
    -p 8088:8088 \
    pipingspace/ironrag.bookstack:latest
```

Minimal `.env.local`:

```env
BOOKSTACK_BASE_URL=https://wiki.example.com
BOOKSTACK_TOKEN_ID=...
BOOKSTACK_TOKEN_SECRET=...
BOOKSTACK_WEBHOOK_SECRET=...
IRONRAG_BASE_URL=https://ironrag.example.com
IRONRAG_API_TOKEN=...
ADMIN_BEARER_TOKEN=...
RUN_MODE=both
```

`routing.yaml` (single library):

```yaml
default:
  workspace: 00000000-0000-0000-0000-000000000000
  library:   00000000-0000-0000-0000-000000000000

policies:
  page:       { on_missing: delete }
  attachment: { on_missing: delete }
  image:      { on_missing: ignore }   # content-addressed; may be shared across pages
```

## Quick start (dev)

```bash
git clone git@github.com:mlimarenko/IronRAG.BookStack.git
cd IronRAG.BookStack
cp .env.example .env.local            # fill in BookStack + IronRAG creds
cp routing.yaml.example routing.yaml

uv sync --all-extras
uv run pytest
uv run bookstack-connector
```

## Operational notes

- See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the page → external_key mapping and failure modes.
- Manual sweep trigger: `curl -X POST http://localhost:8088/sync/run -H "Authorization: Bearer $ADMIN_BEARER_TOKEN"`.
- First sweep against a library that already contains BookStack docs: optionally run `uv run python -m bookstack_connector.seed_cursor` once to populate the SQLite cursor from existing IronRAG documents (avoids re-uploading on first pass).

## License

MIT — see [LICENSE](LICENSE).
