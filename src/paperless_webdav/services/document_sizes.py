# src/paperless_webdav/services/document_sizes.py
"""Durable storage for measured document sizes.

Sizes are expensive to obtain (see models.DocumentSize) and immutable once
measured, so they are persisted rather than cached. Every function here is
best-effort: the store is an optimisation over re-probing Paperless, and a
database hiccup must degrade a listing to "slow", never to "broken".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from paperless_webdav.database import get_sync_session
from paperless_webdav.logging import get_logger
from paperless_webdav.models import DocumentSize

logger = get_logger(__name__)


def load_sizes(document_ids: Iterable[int]) -> dict[int, tuple[str | None, int]]:
    """Return ``{document_id: (version, size)}`` for ids already measured.

    Callers must compare the returned version against the document's current
    `modified` before trusting the size; a mismatch means the file was replaced
    and has to be re-measured.
    """
    ids = list(document_ids)
    if not ids:
        return {}

    try:
        with get_sync_session() as session:
            rows = session.execute(
                select(
                    DocumentSize.document_id,
                    DocumentSize.version,
                    DocumentSize.size,
                ).where(DocumentSize.document_id.in_(ids))
            ).all()
    except Exception as e:
        # Fall back to probing Paperless rather than failing the listing.
        logger.warning("size_store_load_failed", requested=len(ids), error=str(e))
        return {}

    logger.debug("size_store_loaded", requested=len(ids), found=len(rows))
    return {row.document_id: (row.version, row.size) for row in rows}


def store_sizes(sizes: Mapping[int, int], versions: Mapping[int, str | None]) -> None:
    """Persist measured sizes, superseding any earlier revision of each document.

    Written per chunk by the prefetch path rather than once at the end, so that
    a client which disconnects mid-listing still leaves behind everything
    measured up to that point.
    """
    if not sizes:
        return

    now = datetime.now(timezone.utc)
    rows = [
        {
            "document_id": doc_id,
            "version": versions.get(doc_id),
            "size": size,
            "updated_at": now,
        }
        for doc_id, size in sizes.items()
    ]

    try:
        with get_sync_session() as session:
            stmt = insert(DocumentSize).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=[DocumentSize.document_id],
                set_={
                    "version": stmt.excluded.version,
                    "size": stmt.excluded.size,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            session.execute(stmt)
            session.commit()
    except Exception as e:
        logger.warning("size_store_write_failed", count=len(rows), error=str(e))
        return

    logger.debug("size_store_wrote", count=len(rows))
