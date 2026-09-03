"""add geocode_confidence and location_source to orders

Revision ID: 4deaa6b65894
Revises: 11c07e8d5bdc
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4deaa6b65894'
down_revision: Union[str, None] = '11c07e8d5bdc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('geocode_confidence', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('location_source', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('location_source')
        batch_op.drop_column('geocode_confidence')
