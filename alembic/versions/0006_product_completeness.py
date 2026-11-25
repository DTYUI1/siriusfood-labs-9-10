"""Additive account invalidation, optional recipient snapshot and public menu description."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("recipient_name", sa.String(100), nullable=True))
    op.add_column("orders", sa.Column("recipient_phone", sa.String(32), nullable=True))
    op.add_column("orders", sa.Column("recipient_location", sa.String(300), nullable=True))
    op.add_column("menu_items", sa.Column("description", sa.String(2000), nullable=True))


def downgrade():
    # Operational rollback keeps additive columns. Explicit schema downgrade is destructive.
    op.drop_column("menu_items", "description")
    for name in ("recipient_location", "recipient_phone", "recipient_name"):
        op.drop_column("orders", name)
    op.drop_column("users", "auth_version")
