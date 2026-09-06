"""Add annotated_uploads table.

Index over annotated PDFs pushed back from a reading device. Deliberately only
an index: the spool directory and its JSON sidecars are the system of record,
because that directory is inside the backup set and this database is not. The
table can be dropped and rebuilt from `ingested/` filenames without losing a
byte of anyone's annotations.

The unique index on md5 is the whole idempotency scheme -- a sync client
re-uploads on every sync, and the md5 of the bytes is the identity of the
annotation state, so an unchanged file is recognised and discarded before any
Paperless traffic happens.

Revision ID: 004_annotated_uploads
Revises: 003_document_sizes
Create Date: 2026-09-06

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "004_annotated_uploads"
down_revision: str | None = "003_document_sizes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotated_uploads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("spool_id", sa.String(length=64), nullable=False),
        sa.Column("md5", sa.String(length=32), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("source_confidence", sa.String(length=16), nullable=True),
        sa.Column("paperless_document_id", sa.Integer(), nullable=True),
        sa.Column("superseded_document_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # unique=True here rather than a separate UniqueConstraint, so the
    # migration and the model (unique=True, index=True on md5) describe one
    # index rather than two overlapping objects.
    op.create_index("ix_annotated_uploads_md5", "annotated_uploads", ["md5"], unique=True)
    op.create_index(
        "ix_annotated_uploads_source", "annotated_uploads", ["source_document_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_annotated_uploads_source", table_name="annotated_uploads")
    op.drop_index("ix_annotated_uploads_md5", table_name="annotated_uploads")
    op.drop_table("annotated_uploads")
