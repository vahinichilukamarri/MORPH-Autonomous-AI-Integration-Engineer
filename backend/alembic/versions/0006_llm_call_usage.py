"""store the provider's full usage block and total_tokens per LLM call

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("total_tokens", sa.Integer(), nullable=True))
    op.add_column("llm_calls", sa.Column("usage", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "usage")
    op.drop_column("llm_calls", "total_tokens")
