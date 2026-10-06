"""store the operator requirement text on mapping runs

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mapping_runs", sa.Column("requirement", sa.Text()))


def downgrade() -> None:
    op.drop_column("mapping_runs", "requirement")
