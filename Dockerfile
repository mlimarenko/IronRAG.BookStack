# IronRAG ↔ BookStack connector.
#
# Build with the framework checked out alongside this repo at
# ../connectortemplate (the CI workflow stages both before docker build):
#     docker build -t pipingspace/ironrag.bookstack:latest .
#
# Run:
#     docker run -d \
#         --name ironrag-bookstack \
#         --env-file .env.local \
#         -v $(pwd)/routing.yaml:/app/routing.yaml:ro \
#         -v ironrag_bookstack_state:/var/lib/ironrag-connector \
#         -p 8088:8088 \
#         pipingspace/ironrag.bookstack:latest

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    HOST=0.0.0.0 \
    PORT=8088 \
    LOG_LEVEL=info \
    ROUTING_CONFIG_PATH=/app/routing.yaml \
    STATE_DB_PATH=/var/lib/ironrag-connector/state.sqlite

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv==0.8.0

WORKDIR /app

# Stage the framework source (resolved by the CI workflow as a sibling
# checkout) before the connector source so layer caching works.
COPY framework /app/framework
RUN uv pip install --system /app/framework

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-deps .

VOLUME ["/var/lib/ironrag-connector"]
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

CMD ["bookstack-connector"]
