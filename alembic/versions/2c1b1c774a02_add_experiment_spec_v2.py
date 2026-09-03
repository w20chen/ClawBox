"""add ExperimentSpec v2 to managed runs

Revision ID: 2c1b1c774a02
Revises: bf40a779a85c
"""
from alembic import op
import sqlalchemy as sa

revision = "2c1b1c774a02"
down_revision = "bf40a779a85c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("managed_runs", sa.Column("experiment_spec", sa.Text(),
                                            nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("managed_runs", "experiment_spec")
