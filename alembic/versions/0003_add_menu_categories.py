"""add menu categories

Таблица categories и menu_items.category_id. Существующие блюда остаются
без категории (NULL), поэтому заполнять колонку при миграции не нужно.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-22 21:41:40.514034

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    with op.batch_alter_table("menu_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("category_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_menu_items_category_id"), ["category_id"], unique=False)
        batch_op.create_foreign_key("fk_menu_items_category", "categories", ["category_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("menu_items", schema=None) as batch_op:
        batch_op.drop_constraint("fk_menu_items_category", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_menu_items_category_id"))
        batch_op.drop_column("category_id")

    op.drop_table("categories")
