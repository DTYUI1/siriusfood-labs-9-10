"""Recipient projection: no inference/backfill for historical or incomplete rows."""
def recipient_view(order):
    values = [getattr(order, "recipient_" + field, None) for field in ("name", "phone", "location")]
    if not all(values):
        return None
    return dict(zip(("name", "phone", "location"), values))


def kitchen_status(order):
    kitchen = getattr(order, "kitchen", None)
    return kitchen.status if kitchen else "pending"
