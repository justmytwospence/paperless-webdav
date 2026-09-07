"""Durable landing zone for WebDAV uploads.

A PUT is never applied to the document at the request path. The path is not
trustworthy: the WebDAV filename index is an exact-match dict, so
"Bayesian Workflow.pdf" (doc 721) and "Bayesian workflow.pdf" (doc 698) are two
distinct server paths that collapse to one file on a case-insensitive client.
On 2026-09-06 a Boox downloaded 721, annotated it, and PUT it back over 698's
path -- writing those bytes where the path said would have replaced an unrelated
8 MB paper with 52 MB of a different document.

So bytes land here first, and which document they belong to is decided later
from the bytes themselves. The upload streams to disk in the chunks wsgidav
hands us, is fsync'd before the 204 is sent, and lands in a directory that is
inside the backup set. From the moment finalize() returns, the annotated PDF
survives a crash, a lost database, and a failed Paperless ingest.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from paperless_webdav.logging import get_logger

logger = get_logger(__name__)

# Subdirectories of the spool root. Every one of them is a terminal state that
# a human or the ingest worker can act on; nothing is ever deleted on failure.
SPOOL_SUBDIRS = (
    "incoming",  # .part files, mid-PUT
    "pending",  # complete, awaiting ingest
    "ingested",  # accepted by Paperless, pruned after retain_days
    "failed",  # ingest failed repeatedly; bytes retained
    "quarantine",  # ingested but checksum did not verify; bytes retained
    "unmatched",  # could not attribute to a source document; bytes retained
    "partial",  # transfer ended short of Content-Length; bytes retained
)

# States that still owe work, and therefore the only ones that should count
# against the spool quota.
ACTIVE_SUBDIRS = ("incoming", "pending")

# A .part older than this at startup belongs to a PUT that died with the
# process. Nothing resumes an aborted WebDAV upload, so it is unreachable.
STALE_PART_SECONDS = 3600

# The largest plausible annotation layer. PDF incremental updates for
# highlights and notes are kilobytes; 64 MB is generous by orders of magnitude
# and exists only to stop a proportional tolerance from growing large enough to
# match an unrelated document on a big file.
MAX_ANNOTATION_DELTA = 64 * 1024 * 1024

# If the two best candidates are this close, size cannot distinguish them.
AMBIGUOUS_DELTA_WINDOW = 64 * 1024


def spool_dirs(root: Path) -> None:
    """Create the spool tree. Idempotent, safe to call on every startup."""
    for name in SPOOL_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)


def sweep_on_start(root: Path, max_age_seconds: int = STALE_PART_SECONDS) -> int:
    """Remove .part files orphaned by a crash mid-upload.

    Only touches `incoming/`, and only entries older than max_age_seconds, so a
    PUT in flight from another worker is never destroyed.

    Returns:
        Number of stale part files removed
    """
    incoming = root / "incoming"
    if not incoming.is_dir():
        return 0

    now = time.time()
    removed = 0
    for part in incoming.glob("*.part"):
        try:
            if now - part.stat().st_mtime < max_age_seconds:
                continue
            part.unlink()
            removed += 1
            logger.info("spool_stale_part_removed", path=str(part))
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("spool_sweep_failed", path=str(part), error=str(exc))
    return removed


def spool_usage_bytes(root: Path, subdirs: Iterable[str] = ACTIVE_SUBDIRS) -> int:
    """Bytes held in the states that still owe work.

    Deliberately excludes the terminal directories by default. Counting
    `ingested/` would let successfully-handled uploads accumulate against the
    quota until the spool refused new PUTs with 507 -- silently switching
    write-back off and resurrecting the data loss this whole module exists to
    prevent. `failed/`, `quarantine/` and `unmatched/` are excluded for the
    same reason: they need a human, not a back-pressure signal aimed at a
    device that is holding the only copy of someone's annotations.
    """
    total = 0
    for name in subdirs:
        d = root / name
        if not d.is_dir():
            continue
        for entry in d.iterdir():
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:  # pragma: no cover - racing with the worker
                continue
    return total


def prune_ingested(root: Path, retain_days: int) -> int:
    """Delete successfully ingested spool files older than retain_days.

    Only touches `ingested/` -- by definition those bytes are already a
    Paperless document and therefore already in the backup set.

    Returns:
        Number of files removed
    """
    if retain_days <= 0:
        return 0
    ingested = root / "ingested"
    if not ingested.is_dir():
        return 0

    cutoff = time.time() - retain_days * 86400
    removed = 0
    for entry in ingested.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning("spool_prune_failed", path=str(entry), error=str(exc))
    if removed:
        logger.info("spool_pruned", removed=removed, retain_days=retain_days)
    return removed


@dataclass(frozen=True)
class SpooledFile:
    """A completed upload sitting in `pending/`."""

    path: Path
    sidecar: Path
    md5: str
    size: int
    meta: dict[str, Any]


class SpooledUpload:
    """File-like sink handed to wsgidav for the duration of one PUT.

    Hashes inline while writing, so a 52 MB upload costs one 8 KB chunk plus a
    hash context rather than being buffered whole -- the previous implementation
    returned an io.BytesIO, which at cheroot's ten threads is half a gigabyte of
    nominal exposure before anything is even persisted.

    The close() contract is the subtle part. wsgidav's request_server calls
    close() and *then* end_write(), so a close() that released the file would
    destroy the upload before we were told the transfer succeeded. close() here
    flushes and fsyncs but deliberately keeps the descriptor open; finalize()
    or abort() is what actually releases it.
    """

    def __init__(
        self,
        root: Path,
        *,
        request_path: str,
        path_document_id: int | None,
        username: str | None,
        max_bytes: int,
    ) -> None:
        self._root = root
        self._request_path = request_path
        self._path_document_id = path_document_id
        self._username = username
        self._max_bytes = max_bytes

        self.spool_id = uuid.uuid4().hex
        self._part = root / "incoming" / f"{self.spool_id}.part"
        self._fh = open(self._part, "wb")  # noqa: SIM115 - lifetime is explicit
        self._md5 = hashlib.md5()
        self.size = 0
        self._released = False

    # -- file-like surface wsgidav uses -------------------------------------

    def write(self, data: bytes) -> int:
        """Write one chunk, hashing as we go."""
        self.size += len(data)
        if self._max_bytes and self.size > self._max_bytes:
            # Refuse loudly rather than accept bytes we will not keep. 507 is
            # the honest inverse of the 204 that caused this whole incident.
            from wsgidav.dav_error import (  # type: ignore[import-untyped]
                HTTP_INSUFFICIENT_STORAGE,
                DAVError,
            )

            self.abort()
            logger.warning(
                "spool_upload_too_large",
                path=self._request_path,
                size=self.size,
                max_bytes=self._max_bytes,
            )
            raise DAVError(HTTP_INSUFFICIENT_STORAGE, "Upload exceeds the configured maximum")

        self._md5.update(data)
        self._fh.write(data)
        return len(data)

    def writelines(self, chunks: Iterable[bytes]) -> None:
        """wsgidav hands the whole chunk generator here in one call."""
        for chunk in chunks:
            self.write(chunk)

    def flush(self) -> None:
        if not self._released:
            self._fh.flush()

    def close(self) -> None:
        """Flush and fsync, but do NOT release the descriptor.

        wsgidav calls close() before end_write(); releasing here would discard
        the upload before we learn whether the transfer completed.
        """
        if self._released:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())

    # -- terminal operations -------------------------------------------------

    @property
    def md5(self) -> str:
        return self._md5.hexdigest()

    def __del__(self) -> None:
        """Last-resort fd release if neither finalize() nor abort() ran.

        Deliberately does nothing but close: __del__ can run during interpreter
        shutdown, where module globals are already torn down and any logging or
        import raises (KeyError: '__import__'). The .part file is left on disk
        for the startup sweeper -- losing an fd is recoverable, losing bytes is
        not.
        """
        if not getattr(self, "_released", True):
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001 - nothing is safe to do here
                pass

    def park(self, subdir: str, reason: str) -> Path:
        """Release and retain the bytes in a terminal directory.

        For situations that are not success but where the received prefix may
        still be the user's only copy of real work -- a truncated transfer
        above all. abort() unlinks; this does not.
        """
        if self._released:
            raise RuntimeError("SpooledUpload already released")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._released = True

        target = self._root / subdir / f"{self.spool_id}-{self.md5[:12]}.pdf"
        os.replace(self._part, target)
        target.with_suffix(".json").write_text(
            json.dumps(
                {
                    "spool_id": self.spool_id,
                    "md5": self.md5,
                    "size": self.size,
                    "request_path": self._request_path,
                    "path_document_id": self._path_document_id,
                    "username": self._username,
                    "reason": reason,
                },
                indent=2,
                sort_keys=True,
            )
        )
        logger.error(
            "spool_upload_parked",
            spool_id=self.spool_id,
            reason=reason,
            size=self.size,
            path=str(target),
        )
        return target

    def abort(self) -> None:
        """Release and unlink. Used for failed transfers and duplicates."""
        if self._released:
            return
        self._released = True
        try:
            self._fh.close()
        finally:
            self._part.unlink(missing_ok=True)

    def finalize(
        self,
        *,
        source_document_id: int | None,
        source_confidence: str,
        share_name: str | None = None,
    ) -> SpooledFile:
        """Atomically publish the upload into `pending/` with a sidecar.

        The rename is within one filesystem so it is atomic, and the sidecar is
        written before the directory is fsync'd, so a crash cannot leave a
        pending file whose provenance is unknown.
        """
        if self._released:
            raise RuntimeError("SpooledUpload already released")

        # close() may not have been called (wsgidav does, tests may not).
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._released = True

        digest = self.md5
        target = self._root / "pending" / f"{source_document_id or 'unknown'}-{digest[:12]}.pdf"
        sidecar = target.with_suffix(".json")

        meta: dict[str, Any] = {
            "spool_id": self.spool_id,
            "md5": digest,
            "size": self.size,
            "request_path": self._request_path,
            "path_document_id": self._path_document_id,
            "source_document_id": source_document_id,
            "source_confidence": source_confidence,
            "share": share_name,
            "username": self._username,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        try:
            os.replace(self._part, target)
        except OSError:
            # The bytes are still in incoming/ as a .part, which the startup
            # sweeper is designed to delete. Move it somewhere terminal so a
            # complete upload is never mistaken for an abandoned transfer.
            rescue = self._root / "failed" / f"{self.spool_id}-{digest[:12]}.pdf"
            try:
                os.replace(self._part, rescue)
                logger.error(
                    "spool_publish_failed_rescued",
                    spool_id=self.spool_id,
                    md5=digest,
                    size=self.size,
                    path=str(rescue),
                )
            except OSError:
                logger.error(
                    "spool_publish_failed_orphan",
                    spool_id=self.spool_id,
                    md5=digest,
                    size=self.size,
                    path=str(self._part),
                )
            raise

        # Everything past the rename is durability polish. The invariant --
        # "the annotated PDF is a file in a backed-up directory" -- already
        # holds, so a failure here must not turn a successful spool into a 500
        # that makes the client think it lost the upload.
        try:
            sidecar.write_text(json.dumps(meta, indent=2, sort_keys=True))
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            logger.error(
                "spool_sidecar_failed",
                spool_id=self.spool_id,
                md5=digest,
                path=str(target),
                error=str(exc),
            )

        logger.info(
            "spool_upload_persisted",
            spool_id=self.spool_id,
            md5=digest,
            size=self.size,
            source_document_id=source_document_id,
            source_confidence=source_confidence,
            path=str(target),
        )
        return SpooledFile(path=target, sidecar=sidecar, md5=digest, size=self.size, meta=meta)


def list_pending(root: Path) -> list[SpooledFile]:
    """Completed uploads awaiting ingest, oldest first.

    Directory-driven on purpose: the spool is the system of record and the
    database is a rebuildable index, so the worker keeps running with the
    database down or lost.
    """
    pending = root / "pending"
    if not pending.is_dir():
        return []

    out: list[SpooledFile] = []
    for path in sorted(pending.glob("*.pdf"), key=lambda p: p.stat().st_mtime):
        sidecar = path.with_suffix(".json")
        try:
            meta = json.loads(sidecar.read_text()) if sidecar.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("spool_sidecar_unreadable", path=str(sidecar), error=str(exc))
            meta = {}
        out.append(
            SpooledFile(
                path=path,
                sidecar=sidecar,
                md5=str(meta.get("md5", "")),
                size=int(meta.get("size", path.stat().st_size)),
                meta=meta,
            )
        )
    return out


def attribute_source(
    upload_size: int,
    candidates: dict[int, int],
    path_document_id: int | None,
) -> tuple[int | None, str]:
    """Decide which document an upload came from, using its size.

    An annotating reader appends an incremental update to the PDF, so the
    uploaded file is the original plus a small delta -- never smaller, and not
    dramatically larger. That is enough to pick the right document out of a
    share even when the request path points at the wrong one.

    On the real incident: 52,421,533 bytes against {698: 8,040,622,
    721: 52,417,995} yields 721 (delta 3,538) and rejects 698 (delta 44 MB).

    Args:
        upload_size: Bytes received
        candidates: {document_id: stored_size} for the share
        path_document_id: Whatever the request path resolved to, used only as a
            last resort and flagged weak

    Returns:
        (document_id or None, confidence) where confidence is
        "size" | "conflict" | "ambiguous" | "weak" | "none". Only "size" is safe
        to act on without review; everything else must be routed for human
        attention rather than silently attached to a document.

    A note on why "size" now also requires the path to agree. On 2026-09-06 a
    Boox uploaded 8,200,062 bytes of annotated doc 722 (stored 7,108,617). The
    annotation layer was 1,091,445 bytes -- 42,869 past the 1 MB floor -- so the
    TRUE source was excluded, while unrelated doc 698 ("Bayesian workflow",
    stored 8,040,622) sat 159,440 away and won outright. The runner-up was
    310 KB back, well outside AMBIGUOUS_DELTA_WINDOW, so this returned "size":
    full confidence, wrong document, no warning. Six documents in that share
    were inside the window and the real one was not among them.

    Raising the tolerance does not fix this -- widen it enough to admit 722 and
    698 still wins, because it is genuinely closer in size. Size cannot order
    these correctly at any threshold. What distinguishes them is content: an
    annotating reader appends, so md5(upload[:stored_size]) equals the source's
    checksum exactly (verified true for 722, false for 698 on that live file).
    This function does not have the bytes or the checksums, so it cannot do
    that test; bin/paperless-annotation-ingest in the homelab repo does it at
    ingest time. What this function can do is stop claiming certainty when its
    one signal is contradicted by the client's own statement of what it wrote.
    """
    # An annotation layer is kilobytes to a few megabytes -- it is not a
    # percentage of the file. A purely proportional tolerance grows with file
    # size (2.6 MB on a 52 MB paper) and starts matching unrelated documents,
    # so cap it absolutely as well.
    tolerance = min(MAX_ANNOTATION_DELTA, max(1024 * 1024, int(upload_size * 0.05)))

    within = sorted(
        (
            (upload_size - stored, doc_id)
            for doc_id, stored in candidates.items()
            # Strictly append-only: a reader never shrinks the file, so a
            # negative delta is not this document.
            if 0 <= upload_size - stored <= tolerance
        )
    )

    if within:
        best_delta, best_id = within[0]
        # A near-tie means two documents in the share are the same size to
        # within an annotation layer, so size cannot tell them apart. Say so
        # rather than picking one and calling it confident.
        if len(within) > 1 and within[1][0] - best_delta <= AMBIGUOUS_DELTA_WINDOW:
            logger.warning(
                "spool_attribution_ambiguous",
                upload_size=upload_size,
                best_document_id=best_id,
                runner_up_document_id=within[1][1],
                best_delta=best_delta,
                runner_up_delta=within[1][0],
            )
            return best_id, "ambiguous"
        # The client told us which resource it PUT to. When that contradicts the
        # size winner, one of the two signals is wrong and nothing here can say
        # which -- unless the delta is small enough to be unmistakably a single
        # appended annotation layer rather than a coincidence of file sizes.
        # That is the line between the two incidents: 721 won by 3,538 bytes (an
        # append, correct against a lying path); 698 won by 159,440 (a
        # coincidence, wrong against a truthful path). At or below the window,
        # size stays authoritative; above it, defer to the client's own
        # statement and route for review instead of claiming certainty.
        if (
            path_document_id is not None
            and path_document_id != best_id
            and best_delta > AMBIGUOUS_DELTA_WINDOW
        ):
            logger.warning(
                "spool_attribution_conflict",
                upload_size=upload_size,
                path_document_id=path_document_id,
                size_document_id=best_id,
                size_delta=best_delta,
            )
            return path_document_id, "conflict"
        return best_id, "size"

    if path_document_id is not None:
        logger.warning(
            "spool_attribution_weak",
            upload_size=upload_size,
            path_document_id=path_document_id,
            candidate_count=len(candidates),
        )
        return path_document_id, "weak"

    return None, "none"
