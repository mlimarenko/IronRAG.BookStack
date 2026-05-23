"""BookStack connector built on the ironrag-connector framework.

Only this package owns BookStack vocabulary (pages, books, shelves,
attachments, image URLs in markdown). Everything else — IronRAG transport,
routing, policy, sync loop, state cursor, FastAPI server, pidfile,
observability — is reused from ``ironrag_connector``.
"""

__version__ = "0.2.0"
