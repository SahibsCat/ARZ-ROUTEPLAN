"""add order route-membership fields (route_id, sequence_position, unassigned tracking)

Revision ID: f3a91c7b2e10
Revises: be5c1de780e7
Create Date: 2026-08-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a91c7b2e10'
down_revision: Union[str, None] = 'be5c1de780e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('route_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('sequence_position', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('unassigned_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('previous_route_name', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('previous_vehicle_type', sa.String(), nullable=True))
        batch_op.create_foreign_key(
            'fk_orders_route_id_routes', 'routes', ['route_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_constraint('fk_orders_route_id_routes', type_='foreignkey')
        batch_op.drop_column('previous_vehicle_type')
        batch_op.drop_column('previous_route_name')
        batch_op.drop_column('unassigned_at')
        batch_op.drop_column('sequence_position')
        batch_op.drop_column('route_id')
