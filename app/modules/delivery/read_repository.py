"""Application-owned order fields, read only after gRPC ownership authorization.

This is enrichment, never a fallback for unavailable delivery RPCs. Recipient
fields stay out of delivery protobuf/events and external retrieval/generation.
"""
from app.core.identity import RoleName
from app.models import Order
from app.modules.orders.views import recipient_view


class DeliveryReadRepository:
    def __init__(self, db):
        self.db = db

    def view(self, delivery, identity):
        result = {"delivery_id": delivery.id, "order_id": delivery.order_id,
                  "courier_id": delivery.courier_id, "status": delivery.status,
                  "item_name": None, "quantity": None, "recipient": None}
        order = self.db.get(Order, delivery.order_id)
        if order is None:
            return result
        owner = order.user_id == identity.id and delivery.customer_id == identity.id
        courier = (identity.role == RoleName.COURIER and delivery.courier_id == identity.id
                   and delivery.status in ("in_progress", "on_the_way") and order.status == "paid")
        result.update(item_name=order.item.name if order.item else None, quantity=order.quantity)
        if identity.is_admin or owner or courier:
            result["recipient"] = recipient_view(order)
        return result
