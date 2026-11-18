import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identifier(prefix):
    return f"{prefix}-{uuid4().hex[:12].upper()}"


class Database:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS inventory (
                    item_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    stock INTEGER NOT NULL CHECK(stock >= 0)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL REFERENCES customers(id),
                    item_id TEXT NOT NULL REFERENCES inventory(item_id),
                    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
                    currency TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exchange_eligible INTEGER NOT NULL CHECK(exchange_eligible IN (0, 1)),
                    exchange_deadline TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resolutions (
                    reference TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE REFERENCES orders(order_id),
                    kind TEXT NOT NULL CHECK(kind IN ('exchange', 'refund')),
                    amount_minor INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id TEXT PRIMARY KEY,
                    resolution_reference TEXT NOT NULL UNIQUE REFERENCES resolutions(reference),
                    email TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    delivery_mode TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL,
                    iterations INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    messages TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM metadata WHERE key = 'seeded'").fetchone():
                return
            future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            connection.executemany(
                "INSERT INTO customers VALUES (?, ?, ?)",
                [
                    ("CUS-001", "Aarav Sharma", "aarav@example.com"),
                    ("CUS-002", "Priya Patel", "priya@example.com"),
                    ("CUS-003", "Rohan Mehta", "rohan@example.com"),
                ],
            )
            connection.executemany(
                "INSERT INTO inventory VALUES (?, ?, ?)",
                [("ITEM-001", "Wireless Headphones", 8),
                 ("ITEM-002", "Mechanical Keyboard", 0),
                 ("ITEM-003", "Smart Desk Lamp", 5)],
            )
            connection.executemany(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [("ORD-1001", "CUS-001", "ITEM-001", 249900, "INR", "Visa ending 4242", "delivered", 1, future),
                 ("ORD-1002", "CUS-002", "ITEM-002", 399900, "INR", "UPI original transaction", "delivered", 1, future),
                 ("ORD-1003", "CUS-003", "ITEM-003", 179900, "INR", "Mastercard ending 5555", "delivered", 0, past)],
            )
            connection.execute("INSERT INTO metadata VALUES ('seeded', ?)", (timestamp(),))

    def save_run(self, run):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run["run_id"], run["provider"], run["request"], run["status"],
                 run["iterations"], run["summary"], json.dumps(run["messages"]), timestamp()),
            )

