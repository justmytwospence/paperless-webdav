"""Tests for the upload spool.

The spool holds the only copy of a user's annotations between a PUT and a
successful Paperless ingest, so these tests are mostly about durability and
about not destroying bytes on any path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperless_webdav.spool import (
    SpooledUpload,
    attribute_source,
    list_pending,
    prune_ingested,
    spool_dirs,
    spool_usage_bytes,
    sweep_on_start,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    spool_dirs(tmp_path)
    return tmp_path


def _upload(root: Path, max_bytes: int = 0) -> SpooledUpload:
    return SpooledUpload(
        root,
        request_path="/academic/Doc.pdf",
        path_document_id=698,
        username="tester",
        max_bytes=max_bytes,
    )


class TestSpooledUpload:
    def test_writelines_hashes_and_sizes_correctly(self, root: Path) -> None:
        """wsgidav hands the whole chunk generator to writelines in one call."""
        import hashlib

        chunks = [b"a" * 8192, b"b" * 8192, b"tail"]
        up = _upload(root)
        up.writelines(iter(chunks))

        expected = b"".join(chunks)
        assert up.size == len(expected)
        assert up.md5 == hashlib.md5(expected).hexdigest()

    def test_close_does_not_destroy_the_upload(self, root: Path) -> None:
        """The wsgidav ordering trap: close() runs BEFORE end_write().

        A close() that released the file would discard the upload before we
        were told whether the transfer succeeded.
        """
        up = _upload(root)
        up.write(b"hello world")
        up.close()

        spooled = up.finalize(source_document_id=721, source_confidence="size")

        assert spooled.path.exists()
        assert spooled.path.read_bytes() == b"hello world"

    def test_finalize_publishes_file_and_sidecar(self, root: Path) -> None:
        up = _upload(root)
        up.write(b"x" * 100)
        spooled = up.finalize(source_document_id=721, source_confidence="size")

        assert spooled.path.parent.name == "pending"
        assert not list((root / "incoming").glob("*.part"))

        meta = json.loads(spooled.sidecar.read_text())
        assert meta["source_document_id"] == 721
        assert meta["source_confidence"] == "size"
        assert meta["path_document_id"] == 698
        assert meta["md5"] == spooled.md5
        assert meta["size"] == 100

    def test_abort_unlinks_and_leaves_nothing(self, root: Path) -> None:
        up = _upload(root)
        up.write(b"discard me")
        up.abort()

        assert not list((root / "incoming").glob("*.part"))
        assert not list((root / "pending").glob("*.pdf"))

    def test_exceeding_max_bytes_raises_507_and_cleans_up(self, root: Path) -> None:
        from wsgidav.dav_error import DAVError

        up = _upload(root, max_bytes=10)
        with pytest.raises(DAVError) as exc_info:
            up.write(b"x" * 11)

        assert exc_info.value.value == 507
        assert not list((root / "incoming").glob("*.part"))

    def test_finalize_after_abort_raises(self, root: Path) -> None:
        up = _upload(root)
        up.write(b"x")
        up.abort()
        with pytest.raises(RuntimeError):
            up.finalize(source_document_id=1, source_confidence="size")


class TestSweep:
    def test_sweep_removes_stale_parts_and_spares_fresh(self, root: Path) -> None:
        import os
        import time

        stale = root / "incoming" / "old.part"
        stale.write_bytes(b"orphan")
        old = time.time() - 7200
        os.utime(stale, (old, old))

        fresh = root / "incoming" / "new.part"
        fresh.write_bytes(b"in flight")

        assert sweep_on_start(root) == 1
        assert not stale.exists()
        assert fresh.exists()


class TestListingAndUsage:
    def test_list_pending_reads_sidecars(self, root: Path) -> None:
        up = _upload(root)
        up.write(b"y" * 50)
        up.finalize(source_document_id=721, source_confidence="size")

        pending = list_pending(root)
        assert len(pending) == 1
        assert pending[0].size == 50
        assert pending[0].meta["source_document_id"] == 721

    def test_usage_counts_all_subdirs(self, root: Path) -> None:
        up = _upload(root)
        up.write(b"z" * 64)
        up.finalize(source_document_id=1, source_confidence="size")
        # the pdf plus its json sidecar
        assert spool_usage_bytes(root) > 64


class TestAttributeSource:
    def test_replays_the_real_incident(self) -> None:
        """2026-09-06: 52,421,533 bytes arrived on doc 698's path.

        The bytes were doc 721 plus a 3,538-byte annotation layer. Writing
        where the path said would have replaced an unrelated 8 MB paper with
        52 MB of a different document.
        """
        doc_id, confidence = attribute_source(
            52_421_533,
            {698: 8_040_622, 721: 52_417_995},
            path_document_id=698,
        )
        assert doc_id == 721
        assert confidence == "size"

    def test_falls_back_to_path_and_flags_it_weak(self) -> None:
        doc_id, confidence = attribute_source(999, {698: 8_040_622}, path_document_id=698)
        assert doc_id == 698
        assert confidence == "weak"

    def test_returns_none_when_nothing_matches_and_no_path(self) -> None:
        doc_id, confidence = attribute_source(999, {}, path_document_id=None)
        assert doc_id is None
        assert confidence == "none"

    def test_rejects_a_smaller_upload(self) -> None:
        """An annotating reader appends; it never shrinks the file."""
        doc_id, confidence = attribute_source(1000, {5: 2000}, path_document_id=None)
        assert doc_id is None
        assert confidence == "none"

    def test_prefers_the_smallest_positive_delta(self) -> None:
        doc_id, _ = attribute_source(1_000_500, {1: 1_000_000, 2: 1_000_400}, path_document_id=None)
        assert doc_id == 2


class TestTruncationAndAmbiguity:
    """Regressions for the two ways this module could still lose or mis-file work."""

    def test_park_retains_bytes_unlike_abort(self, root: Path) -> None:
        """A truncated transfer must be kept, not unlinked.

        wsgidav cannot tell a client that dropped mid-body from a clean EOF, so
        the caller detects the short body -- but the prefix received may be the
        only copy of the user's work and must survive.
        """
        up = _upload(root)
        up.write(b"partial payload")
        target = up.park("partial", "expected 999 bytes, received 15")

        assert target.exists()
        assert target.read_bytes() == b"partial payload"
        assert target.parent.name == "partial"
        meta = json.loads(target.with_suffix(".json").read_text())
        assert "expected 999" in meta["reason"]

    def test_ambiguous_when_two_candidates_are_indistinguishable(self) -> None:
        """Two same-sized papers cannot be told apart by size; say so."""
        doc_id, confidence = attribute_source(
            1_000_500,
            {1: 1_000_000, 2: 1_000_010},
            path_document_id=None,
        )
        assert doc_id in (1, 2)
        assert confidence == "ambiguous"

    def test_absolute_cap_rejects_a_wildly_larger_upload(self) -> None:
        """A 5% tolerance on a large file would match unrelated documents."""
        # 2 GB file, 5% = 100 MB -- without the absolute cap a 90 MB delta
        # would be accepted as 'an annotation layer'.
        doc_id, confidence = attribute_source(
            2_000_000_000 + 90_000_000,
            {1: 2_000_000_000},
            path_document_id=None,
        )
        assert doc_id is None
        assert confidence == "none"

    def test_quota_ignores_terminal_directories(self, root: Path) -> None:
        """Ingested files must not accumulate against the quota.

        Counting them would eventually make the spool refuse every PUT with
        507 -- silently switching write-back off and restoring the data loss.
        """
        (root / "ingested" / "old.pdf").write_bytes(b"x" * 10_000)
        (root / "failed" / "bad.pdf").write_bytes(b"y" * 10_000)
        assert spool_usage_bytes(root) == 0

        up = _upload(root)
        up.write(b"z" * 500)
        up.finalize(source_document_id=1, source_confidence="size")
        assert spool_usage_bytes(root) >= 500

    def test_prune_removes_only_old_ingested(self, root: Path) -> None:
        import os
        import time

        old = root / "ingested" / "old.pdf"
        old.write_bytes(b"x")
        stamp = time.time() - 10 * 86400
        os.utime(old, (stamp, stamp))
        fresh = root / "ingested" / "fresh.pdf"
        fresh.write_bytes(b"y")

        assert prune_ingested(root, retain_days=7) == 1
        assert not old.exists()
        assert fresh.exists()
