"""baseline

Represents the schema as it was BEFORE Alembic was added to the project
(post-Phase-0 of the admin IA reorg, pre-Phase-1). This migration is a
no-op: its purpose is to give Alembic an anchor point so existing DBs
can be stamped here and then upgraded to subsequent revisions.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-18

"""
from alembic import op
import sqlalchemy as sa


revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
