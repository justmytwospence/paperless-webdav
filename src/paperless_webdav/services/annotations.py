"""Best-effort index over spooled annotation uploads.

Every function here degrades rather than raises. The spool directory holds the
only copy of a user's annotations between the PUT and a successful Paperless
ingest, and that directory is in the backup set while this database is not --
so a database outage must never be able to reject or discard an upload. Worst
case the index goes cold, a repeat sync re-uploads bytes Paperless then rejects
as a duplicate, and nothing is lost.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from paperless_webdav.database import get_sync_session
from paperless_webdav.logging import get_logger
from paperless_webdav.models import AnnotatedUpload
from paperless_webdav.spool import SpooledFile

logger = get_logger(__name__)


def upload_ingested(md5: str) -> bool:
    """Whether these exact bytes are already a Paperless document.

    Deliberately narrower than "have we seen this md5". A row alone proves
    only that an upload was once recorded -- it may have failed to ingest and
    had its spool file cleared since. Only an ingested row proves the bytes
    survive somewhere, and only that is safe grounds for discarding a
    re-upload. Returns False on any error: re-spooling a duplicate wastes
    disk, while wrongly reporting True silently destroys an annotation.
    """
    if not md5:
        return False
    try:
        with get_sync_session() as session:
            row = session.execute(
                select(AnnotatedUpload.id).where(
                    AnnotatedUpload.md5 == md5,
                    AnnotatedUpload.status == "ingested",
                    AnnotatedUpload.paperless_document_id.is_not(None),
                )
            ).first()
            return row is not None
    except Exception as exc:
        logger.warning("annotation_ingested_lookup_failed", md5=md5, error=str(exc))
        return False


def record_pending(spooled: SpooledFile) -> None:
    """Index a freshly spooled upload.

    Idempotent on md5 so a retry after a partial failure cannot raise.
    """
    meta = spooled.meta
    try:
        with get_sync_session() as session:
            stmt = insert(AnnotatedUpload).values(
                spool_id=meta.get("spool_id", ""),
                md5=spooled.md5,
                size=spooled.size,
                source_document_id=meta.get("source_document_id"),
                source_confidence=meta.get("source_confidence"),
                username=meta.get("username"),
                status="pending",
                created_at=datetime.now(timezone.utc),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=[AnnotatedUpload.md5])
            session.execute(stmt)
            session.commit()
        logger.info(
            "annotation_upload_recorded",
            md5=spooled.md5,
            size=spooled.size,
            source_document_id=meta.get("source_document_id"),
        )
    except Exception as exc:
        logger.warning(
            "annotation_record_failed",
            md5=spooled.md5,
            error=str(exc),
        )


def mark_ingested(md5: str, paperless_document_id: int) -> None:
    """Record that Paperless accepted the upload as a new document."""
    _update(
        md5,
        status="ingested",
        paperless_document_id=paperless_document_id,
        ingested_at=datetime.now(timezone.utc),
    )


def mark_failed(md5: str, error: str, status: str = "failed") -> None:
    """Record a terminal ingest failure. The spool file is always retained."""
    _update(md5, status=status, error=error[:2000])


def latest_for_source(source_document_id: int) -> int | None:
    """The most recent successfully ingested annotated copy for a source."""
    try:
        with get_sync_session() as session:
            row = session.execute(
                select(AnnotatedUpload.paperless_document_id)
                .where(
                    AnnotatedUpload.source_document_id == source_document_id,
                    AnnotatedUpload.status == "ingested",
                    AnnotatedUpload.paperless_document_id.is_not(None),
                )
                .order_by(AnnotatedUpload.ingested_at.desc())
                .limit(1)
            ).first()
            return row[0] if row else None
    except Exception as exc:
        logger.warning(
            "annotation_latest_lookup_failed",
            source_document_id=source_document_id,
            error=str(exc),
        )
        return None


def _update(md5: str, **values: object) -> None:
    try:
        with get_sync_session() as session:
            obj = session.execute(
                select(AnnotatedUpload).where(AnnotatedUpload.md5 == md5)
            ).scalar_one_or_none()
            if obj is None:
                logger.warning("annotation_update_missing_row", md5=md5)
                return
            for key, value in values.items():
                setattr(obj, key, value)
            session.commit()
    except Exception as exc:
        logger.warning("annotation_update_failed", md5=md5, error=str(exc))
