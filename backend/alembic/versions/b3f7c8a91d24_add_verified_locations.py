"""add verified_locations table

Revision ID: b3f7c8a91d24
Revises: 7a2f9c1e4b83
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f7c8a91d24'
down_revision: Union[str, None] = '7a2f9c1e4b83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'verified_locations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('signature', sa.String(), nullable=False),
        sa.Column('lat', sa.Float(), nullable=False),
        sa.Column('lng', sa.Float(), nullable=False),
        sa.Column('formatted_address', sa.String(), nullable=True),
        sa.Column('sample_address', sa.String(), nullable=True),
        sa.Column('hit_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_verified_locations_signature'), 'verified_locations', ['signature'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_verified_locations_signature'), table_name='verified_locations')
    op.drop_table('verified_locations')
