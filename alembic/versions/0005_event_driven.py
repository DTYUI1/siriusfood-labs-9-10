"""Event-driven: inbox/outbox, кухня, уникальная доставка на заказ."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("dedup_key", sa.String(160), nullable=False, unique=True),
        sa.Column("routing_key", sa.String(100), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_outbox_published_at", "outbox", ["published_at"])
    op.create_table(
        "processed_events",
        sa.Column("consumer", sa.String(50), primary_key=True),
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("processed_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "kitchen_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False),
    )
    with op.batch_alter_table("deliveries") as batch:
        batch.create_unique_constraint("uq_deliveries_order_id", ["order_id"])


def downgrade():
    with op.batch_alter_table("deliveries") as batch:
        batch.drop_constraint("uq_deliveries_order_id", type_="unique")
    op.drop_table("kitchen_orders")
    op.drop_table("processed_events")
    op.drop_index("ix_outbox_published_at", table_name="outbox")
    op.drop_table("outbox")
