"""add orders created_at

Время оформления заказа — для ежедневной статистики (Celery Beat).
У существующих заказов время неизвестно, они остаются с NULL.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22 22:39:09.947934

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("created_at", sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f("ix_orders_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_orders_created_at"))
        batch_op.drop_column("created_at")
