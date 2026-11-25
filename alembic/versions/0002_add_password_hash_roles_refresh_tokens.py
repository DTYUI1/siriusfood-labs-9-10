"""add password_hash, roles, refresh_tokens

Таблицы roles и refresh_tokens, колонки users.password_hash и users.role_id,
orders.total_cost. Файл создан через alembic revision --autogenerate и
доработан вручную: Alembic не знает, как заполнить новые колонки в уже
существующих строках, а без этого NOT NULL на непустой БД не применится.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 21:00:38.318931

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    roles = op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.bulk_insert(roles, [{"name": name} for name in ("student", "cook", "courier", "admin")])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_refresh_tokens_user_id"), ["user_id"], unique=False)

    # Колонки добавляются как nullable, заполняются и только потом становятся NOT NULL.
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("password_hash", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("role_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_users_role", "roles", ["role_id"], ["id"])

    # Всем существующим пользователям — роль student.
    op.execute("UPDATE users SET role_id = (SELECT id FROM roles WHERE name = 'student')")
    # Паролей у существующих пользователей нет. '!' не является bcrypt-хешем
    # и не совпадает ни с одним паролем: войти можно только после того, как
    # администратор задаст пароль (python -m app.cli set-password EMAIL).
    op.execute("UPDATE users SET password_hash = '!'")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("role_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("password_hash", existing_type=sa.String(), nullable=False)

    # Сумма уже оформленных заказов восстанавливается по текущей цене блюда.
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("total_cost", sa.Float(), nullable=True))
    op.execute(
        "UPDATE orders SET total_cost = COALESCE("
        "quantity * (SELECT price FROM menu_items WHERE menu_items.id = orders.item_id), 0)"
    )
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.alter_column("total_cost", existing_type=sa.Float(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("total_cost")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("fk_users_role", type_="foreignkey")
        batch_op.drop_column("role_id")
        batch_op.drop_column("password_hash")

    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_refresh_tokens_user_id"))

    op.drop_table("refresh_tokens")
    op.drop_table("roles")
