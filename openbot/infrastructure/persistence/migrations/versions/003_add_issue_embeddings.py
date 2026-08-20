"""Add issue_embeddings table with pgvector for dedup.

Revision ID: 003
Requires: pgvector extension
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Create issue_embeddings table
    op.create_table(
        "issue_embeddings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=False),  # stored as vector via raw SQL
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("repo", "issue_number", name="uq_issue_embeddings_repo_issue"),
    )

    # Create IVFFlat index for fast cosine similarity search
    # Note: This requires the table to have data before the index is effective.
    # For empty tables, the index is created but will be rebuilt as data grows.
    op.execute("""
        CREATE INDEX ix_issue_embeddings_embedding
        ON issue_embeddings
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100)
    """)

    op.create_index("ix_issue_embeddings_repo", "issue_embeddings", ["repo"])


def downgrade() -> None:
    op.drop_table("issue_embeddings")
    op.execute("DROP EXTENSION IF EXISTS vector")
