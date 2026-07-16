"""Add document_sizes table.

Paperless exposes a document's byte size only via /api/documents/{id}/metadata/,
one request per document, served at roughly 25/s regardless of concurrency. On a
share of a few hundred documents that fan-out takes longer than a WebDAV client
will wait, so it cannot be paid on the request path -- and because it was
previously held only in a process-local cache with a TTL, it was paid again on
every restart and every expiry, always into a client that had already timed out.

Sizes are immutable for a given document revision, so they are stored here and
measured exactly once instead.

Revision ID: 003_document_sizes
Revises: 002_remove_read_only
Create Date: 2026-08-26

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "003_document_sizes"
down_revision: str | None = "002_remove_read_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the document_sizes table."""
    op.create_table(
        "document_sizes",
        sa.Column("document_id", sa.Integer(), nullable=False),
        # Paperless' `modified` for the revision the size was measured from.
        # One row per document: a re-OCR overwrites it rather than accumulating
        # a row per edit.
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("document_id"),
    )


def downgrade() -> None:
    """Drop the document_sizes table."""
    op.drop_table("document_sizes")
