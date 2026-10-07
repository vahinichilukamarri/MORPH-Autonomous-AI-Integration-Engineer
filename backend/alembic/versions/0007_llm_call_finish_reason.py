"""store the provider's finish reason per LLM call

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08
"""

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("finish_reason", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "finish_reason")
