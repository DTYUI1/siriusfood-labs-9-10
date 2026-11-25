"""Boundary cases for the amount in an OrderPaid event."""
import math

import pytest

from event_bus.contracts import InvalidEvent, new_event, validate_event


@pytest.mark.parametrize("amount", [0, -1, True, math.nan, math.inf, "150"])
def test_invalid_event_amount_is_rejected(amount):
    payload = new_event("order.paid", 1, 1, 150)
    payload["amount"] = amount
    with pytest.raises(InvalidEvent):
        validate_event(payload)


@pytest.mark.parametrize("amount", [0.5, 1, 150.0])
def test_positive_finite_event_amount_is_accepted(amount):
    payload = new_event("order.paid", 1, 1, 150)
    payload["amount"] = amount
    assert validate_event(payload)["amount"] == amount
