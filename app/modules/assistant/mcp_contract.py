"""Closed input and output schemas for the local assistant MCP server."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = "2025-06-18"
MAX_FRAME = 32768
MAX_CALLS = 4

Identifier = Annotated[int, Field(gt=0, le=2147483647)]
Price = Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]
Amount = Annotated[float, Field(allow_inf_nan=False)]
Topic = Literal["overview", "architecture", "events", "roles", "payment", "cancel", "delivery"]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectRead(Closed):
    topic: Topic


class MenuRead(Closed):
    query: Annotated[str, Field(min_length=2, max_length=100, pattern=r"^[а-яёa-z0-9 .-]+$")] | None = None
    max_price: Price | None = None
    max_exclusive: bool = False
    available: bool | None = None


class BalanceRead(Closed):
    pass


class OrdersRead(Closed):
    order_id: Identifier | None = None


class Source(Closed):
    id: str
    title: str
    url: str
    text: str


class PublicItem(Closed):
    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=200)]
    price: Amount
    available: bool


class MenuResults(Closed):
    items: Annotated[list[PublicItem], Field(max_length=8)]
    has_more: bool


class BalanceResult(Closed):
    amount: Amount


class OwnOrder(Closed):
    id: Identifier
    status: Literal["processing", "paid", "cancelled", "delivered"]
    total_cost: Amount


class OrderResults(Closed):
    orders: Annotated[list[OwnOrder], Field(max_length=5)]


TOOLS = {
    "project.read": (ProjectRead, Source, "Read an approved public article: project overview, architecture, events, roles, payment, cancellation, delivery."),
    "menu.read": (
        MenuRead, MenuResults,
        "Read current dishes, exact prices and availability. Optional query is a lowercase substring of a dish or category name. "
        "max_price is inclusive unless max_exclusive is true. Pass available=true for dishes the user can order. Results are sorted by price.",
    ),
    "student.balance": (BalanceRead, BalanceResult, "Read only the signed-in student's current balance in rubles."),
    "student.orders": (
        OrdersRead, OrderResults,
        "Read only the signed-in student's orders. An optional order_id looks up one order. Without it, read the last five. "
        "Each total_cost is the historical amount in rubles; an empty result does not identify another owner's order.",
    ),
}
PUBLIC_TOOLS = ("project.read", "menu.read")


def list_tools(student: bool):
    # Гость видит статьи и меню. Для студента сервер добавляет чтение своего баланса и заказов.
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": arguments.model_json_schema(),
            "outputSchema": result.model_json_schema(),
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
        for name, (arguments, result, description) in TOOLS.items()
        if student or name in PUBLIC_TOOLS
    ]


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def decode(frame):
    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("duplicate_json_key")
            values[key] = value
        return values

    def reject_constant(_):
        raise ValueError("nonfinite_json_number")

    return json.loads(frame.decode("utf-8"), object_pairs_hook=unique, parse_constant=reject_constant)
