"""add suggested_lat/suggested_lng to orders

Revision ID: 7a2f9c1e4b83
Revises: 4deaa6b65894
Create Date: 2026-09-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a2f9c1e4b83'
down_revision: Union[str, None] = '4deaa6b65894'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('suggested_lat', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('suggested_lng', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('suggested_lng')
        batch_op.drop_column('suggested_lat')
