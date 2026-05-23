"""Entry point: ``bookstack-connector`` or ``python -m bookstack_connector``."""

from __future__ import annotations

import uvicorn
from ironrag_connector import Orchestrator, build_app

from .adapter import BookStackAdapter
from .config import BookStackSettings
from .webhook import make_bookstack_handler


def main() -> None:
    settings = BookStackSettings()  # type: ignore[call-arg]
    adapter = BookStackAdapter(settings)

    # webhook_factory receives the framework-owned Orchestrator so the
    # webhook path dispatches through the same router/state/cursor the
    # periodic sweep uses.
    def make_handlers(orchestrator: Orchestrator) -> list:
        return [make_bookstack_handler(settings, adapter, orchestrator)]

    app = build_app(settings, adapter, webhook_factory=make_handlers)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
