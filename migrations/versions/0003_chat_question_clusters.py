"""chat_question_clusters

Adds the chat_question_cluster table that stores LLM-grouped themes from
real user chat. Populated weekly by services.cluster_chat_questions()
when ENABLE_CHAT_CLUSTERING=true, or manually via the admin button.

Revision ID: 0003_chat_question_clusters
Revises: 0002_phase_1_ia_reorg
Create Date: 2026-05-18

"""
from alembic import op
import sqlalchemy as sa


revision = '0003_chat_question_clusters'
down_revision = '0002_phase_1_ia_reorg'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_question_cluster',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('representative_question', sa.Text, nullable=False),
        sa.Column('sample_questions', sa.Text, nullable=False),  # JSON
        sa.Column('count', sa.Integer, nullable=False, server_default='0'),
        sa.Column('first_seen_at', sa.DateTime, nullable=True),
        sa.Column('last_seen_at', sa.DateTime, nullable=True),
        sa.Column('promoted_to_faq_id', sa.Integer,
                  sa.ForeignKey('faq.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
    )


def downgrade():
    op.drop_table('chat_question_cluster')
