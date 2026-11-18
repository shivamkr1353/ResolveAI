import argparse
import os
import sqlite3
import sys
from pathlib import Path

from .agent import ResolutionAgent
from .database import Database
from .models import GeminiModel, ModelError


ROOT = Path(__file__).resolve().parent.parent


def load_environment():
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key, value = key.strip(), value.strip()
            if key in {"GEMINI_API_KEY", "GEMINI_MODEL"}:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key, value)


def trace(event):
    result = event["result"]
    outcome = "OK" if result["ok"] else result["error"]["code"]
    print(f"[{event['iteration']}/5] {event['tool']} -> {outcome}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Resolve an order issue with the Gemini resolution agent.")
    parser.add_argument("request", nargs="?", help="Customer issue including an order ID, such as ORD-1001")
    args = parser.parse_args(argv)
    try:
        load_environment()
        model = GeminiModel()
        database = Database(ROOT / "data" / "resolveai.db")
        print("ResolveAI | Gemini Resolution Agent")
        print("Refunds, exchanges and confirmation emails are simulated locally.\n")
        request = args.request or input("Describe your order issue: ")
        agent = ResolutionAgent(database, model, max_iterations=5, on_event=trace)
        run = agent.run(request)
        print(f"\n{run['summary']}\n\nStatus: {run['status']}")
        return 2 if run["status"] == "needs_human" else 0
    except (ModelError, ValueError, OSError, sqlite3.Error) as error:
        print(f"ResolveAI: {error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nSession interrupted. Any completed operations remain saved.", file=sys.stderr)
        return 130
