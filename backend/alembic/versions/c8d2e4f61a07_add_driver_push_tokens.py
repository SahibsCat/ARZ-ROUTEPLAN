"""add driver_push_tokens table (route-assigned notifications)

Revision ID: c8d2e4f61a07
Revises: b3f7c8a91d24
Create Date: 2026-11-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d2e4f61a07'
down_revision: Union[str, None] = 'b3f7c8a91d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'driver_push_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('driver_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(), nullable=False),
        sa.Column('platform', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=True),
        sa.ForeignKeyConstraint(['driver_id'], ['drivers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_driver_push_tokens_driver_id', 'driver_push_tokens', ['driver_id'])
    op.create_index('ix_driver_push_tokens_token', 'driver_push_tokens', ['token'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_driver_push_tokens_token', table_name='driver_push_tokens')
    op.drop_index('ix_driver_push_tokens_driver_id', table_name='driver_push_tokens')
    op.drop_table('driver_push_tokens')
