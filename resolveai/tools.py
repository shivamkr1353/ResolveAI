import json
import re
import sqlite3
from datetime import datetime, timezone

from .database import identifier, timestamp


def success(**data):
    return {"ok": True, "data": data}


def failure(code, message, **details):
    return {"ok": False, "error": {"code": code, "message": message, **details}}


def eligible(order):
    return bool(order["exchange_eligible"]) and order["status"] == "delivered" and (
        datetime.fromisoformat(order["exchange_deadline"]) >= datetime.now(timezone.utc)
    )


def money(amount, currency):
    return f"{currency} {amount / 100:,.2f}"


def order_record(connection, order_id):
    return connection.execute("""
        SELECT o.*, c.name AS customer_name, c.email, i.name AS item_name, i.stock
        FROM orders o JOIN customers c ON c.id = o.customer_id
        JOIN inventory i ON i.item_id = o.item_id WHERE o.order_id = ?
    """, (order_id,)).fetchone()


def resolution_record(connection, order_id):
    row = connection.execute("""
        SELECT r.*, o.currency, o.payment_method, c.email,
               n.notification_id
        FROM resolutions r JOIN orders o ON o.order_id = r.order_id
        JOIN customers c ON c.id = o.customer_id
        LEFT JOIN notifications n ON n.resolution_reference = r.reference
        WHERE r.order_id = ?
    """, (order_id,)).fetchone()
    return dict(row) if row else None


class ResolutionTools:
    def __init__(self, database):
        self.database = database

    def lookup_order(self, order_id):
        with self.database.connect() as connection:
            row = order_record(connection, order_id)
            if row is None:
                return failure("ORDER_NOT_FOUND", f"Order {order_id} was not found. Please verify the order ID.")
            order = dict(row)
            order.pop("stock")
            order["exchange_eligible"] = eligible(row)
            order["resolution"] = resolution_record(connection, order_id)
            return success(**order)

    def inspect_inventory(self, item_id):
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM inventory WHERE item_id = ?", (item_id,)).fetchone()
            if row is None:
                return failure("ITEM_NOT_FOUND", f"Inventory item {item_id} was not found.")
            return success(**dict(row), available=row["stock"] > 0, checked_at=timestamp())

    def confirm_exchange(self, order_id, reason):
        return self._resolve(order_id, reason, "exchange")

    def process_refund(self, order_id, reason):
        return self._resolve(order_id, reason, "refund")

    def _resolve(self, order_id, reason, kind):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            order = order_record(connection, order_id)
            if order is None:
                return failure("ORDER_NOT_FOUND", f"Order {order_id} was not found.")
            existing = resolution_record(connection, order_id)
            if existing:
                return success(**existing, reused=True, simulation=True)
            can_exchange = eligible(order) and order["stock"] > 0
            if kind == "exchange" and not can_exchange:
                return failure("EXCHANGE_UNAVAILABLE", "Exchange is unavailable. Initiate a refund instead.")
            if kind == "refund" and can_exchange:
                return failure("EXCHANGE_REQUIRED", "The order is eligible and a replacement is in stock. Confirm an exchange.")
            reference = identifier("EXC" if kind == "exchange" else "REF")
            amount = order["amount_minor"] if kind == "refund" else 0
            if kind == "exchange":
                connection.execute("UPDATE inventory SET stock = stock - 1 WHERE item_id = ? AND stock > 0", (order["item_id"],))
                detail = f"A replacement for your {order['item_name']} has been reserved."
            else:
                detail = (
                    f"A simulated refund of {money(amount, order['currency'])} to your original payment method "
                    f"({order['payment_method']}) has been recorded. No money was moved."
                )
            message = (
                f"Hello {order['customer_name']},\n\n"
                f"We are sorry about the issue with order {order_id}. {detail}\n"
                f"Resolution reference: {reference}.\nReason: {reason}\n\n"
                "This is a ResolveAI demonstration. Exchanges, payments and email delivery are simulated locally.\n"
                "Thank you,\nResolveAI Support"
            )
            connection.execute(
                "INSERT INTO resolutions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (reference, order_id, kind, amount, reason, message, timestamp()),
            )
            connection.execute("UPDATE orders SET status = ? WHERE order_id = ?", (f"{kind}_confirmed", order_id))
            return success(**resolution_record(connection, order_id), reused=False, simulation=True)

    def notify_customer(self, email, message):
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("""
                SELECT r.reference, r.order_id FROM resolutions r
                JOIN orders o ON o.order_id = r.order_id
                JOIN customers c ON c.id = o.customer_id
                WHERE c.email = ? AND r.message = ?
            """, (email, message)).fetchone()
            if row is None:
                return failure("NOTIFICATION_MISMATCH", "Use the exact customer email and notification message from the recorded resolution.")
            existing = connection.execute(
                "SELECT * FROM notifications WHERE resolution_reference = ?", (row["reference"],)
            ).fetchone()
            if existing:
                return success(**dict(existing), reused=True)
            notification_id = identifier("MSG")
            connection.execute(
                "INSERT INTO notifications VALUES (?, ?, ?, ?, ?, ?, ?)",
                (notification_id, row["reference"], email, f"ResolveAI confirmation for {row['order_id']}",
                 message, "local_mailbox", timestamp()),
            )
            return success(notification_id=notification_id, resolution_reference=row["reference"],
                           email=email, delivery_mode="local_mailbox", reused=False)


def schema(name, description, fields):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {key: {"type": "string", "description": value} for key, value in fields.items()},
                "required": list(fields),
                "additionalProperties": False,
            },
        },
    }


TOOL_SCHEMAS = [
    schema("lookup_order", "Read an order, its customer identity, purchased item, exchange policy eligibility and any existing resolution.",
           {"order_id": "The customer's order identifier, for example ORD-1001."}),
    schema("inspect_inventory", "Read the current warehouse stock and replacement availability for an order's item.",
           {"item_id": "The item identifier returned by lookup_order, for example ITEM-001."}),
    schema("confirm_exchange", "Reserve one replacement and record a simulated exchange when the order is eligible and stock is greater than zero. Repeated calls reuse the existing resolution.",
           {"order_id": "The verified order identifier.", "reason": "A concise reason explaining the customer issue and exchange decision."}),
    schema("process_refund", "Record a simulated financial refund to the original payment method when replacements are unavailable or the order is ineligible for exchange. The server rejects refunds if an exchange is possible.",
           {"order_id": "The verified order identifier.", "reason": "A concise reason explaining the issue and why an exchange is unavailable."}),
    schema("notify_customer", "Dispatch a confirmation to the simulated local email mailbox after a resolution. No external email is sent. Use the exact email and message returned by the resolution.",
           {"email": "The customer's email returned by the recorded resolution.", "message": "The exact message returned by the recorded resolution, without edits."}),
]


class ToolDispatcher:
    def __init__(self, database, requested_order=None):
        self.tools = ResolutionTools(database)
        self.registry = {entry["function"]["name"]: getattr(self.tools, entry["function"]["name"]) for entry in TOOL_SCHEMAS}
        self.contracts = {entry["function"]["name"]: entry["function"]["parameters"] for entry in TOOL_SCHEMAS}
        self.requested_order = requested_order
        self.order = None
        self.inspected_items = set()
        self.resolution = None
        self.notification = None

    def dispatch(self, name, arguments):
        if not isinstance(name, str) or name not in self.registry:
            return failure("UNKNOWN_TOOL", "Tool execution rejected: the requested name is not in the registry.")
        try:
            if not isinstance(arguments, str) or len(arguments) > 20000:
                return failure("INVALID_ARGUMENTS", "Tool arguments must be a JSON string of at most 20000 characters.")
            values = json.loads(arguments)
        except (json.JSONDecodeError, RecursionError):
            return failure("INVALID_JSON", "Tool arguments are malformed. Return a valid JSON object.")
        contract = self.contracts[name]
        if not isinstance(values, dict) or set(values) != set(contract["required"]):
            return failure("INVALID_ARGUMENTS", f"Supply exactly these fields: {', '.join(contract['required'])}.")
        for key, value in values.items():
            if not isinstance(value, str) or not value.strip() or len(value) > 10000:
                return failure("INVALID_ARGUMENTS", f"{key} must be a non-empty string of at most 10000 characters.")
        for key in ("order_id", "item_id"):
            if key in values:
                values[key] = values[key].strip().upper()
                if not re.fullmatch(r"[A-Z]+-[A-Z0-9]{1,32}", values[key]):
                    return failure("INVALID_ARGUMENTS", f"{key} must be an identifier such as ORD-1001 or ITEM-001.")
        error = self._authorize(name, values)
        if error:
            return error
        try:
            result = self.registry[name](**values)
        except (sqlite3.Error, OSError):
            return failure("STORAGE_ERROR", "The local database is unavailable. Retry or request human assistance.")
        if result["ok"]:
            data = result["data"]
            if name == "lookup_order":
                self.order = data
                self.resolution = data["resolution"]
                if self.resolution and self.resolution["notification_id"]:
                    self.notification = {"notification_id": self.resolution["notification_id"]}
            elif name == "inspect_inventory":
                self.inspected_items.add(data["item_id"])
            elif name in ("process_refund", "confirm_exchange"):
                self.resolution = data
            elif name == "notify_customer":
                self.notification = data
        return result

    def _authorize(self, name, values):
        if name == "lookup_order":
            expected = self.requested_order or (self.order and self.order["order_id"])
            if expected and values["order_id"] != expected:
                return failure("ORDER_SCOPE_MISMATCH", "Only the order supplied by the customer may be accessed in this session.")
            return None
        if not self.order:
            return failure("LOOKUP_REQUIRED", "Look up the customer's order before invoking other tools.")
        if name == "inspect_inventory" and values["item_id"] != self.order["item_id"]:
            return failure("ITEM_SCOPE_MISMATCH", "Inspect only the item from the verified order.")
        if name in ("confirm_exchange", "process_refund"):
            if values["order_id"] != self.order["order_id"]:
                return failure("ORDER_SCOPE_MISMATCH", "Resolve only the verified order.")
            if self.order["item_id"] not in self.inspected_items:
                return failure("INVENTORY_REQUIRED", "Inspect current inventory before choosing a resolution.")
        if name == "notify_customer":
            if not self.resolution:
                return failure("RESOLUTION_REQUIRED", "Record an exchange or refund before notifying the customer.")
            if values["email"] != self.resolution["email"] or values["message"] != self.resolution["message"]:
                return failure("NOTIFICATION_MISMATCH", "Use the exact email and message from this session's recorded resolution.")
        return None
