import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from resolveai.agent import ResolutionAgent
from resolveai.cli import main
from resolveai.database import Database
from resolveai.models import GeminiModel, ModelError
from resolveai.tools import TOOL_SCHEMAS, ResolutionTools, ToolDispatcher


def call(name, arguments, index):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{index}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments,
        }}]}


class PolicyModel:
    name = "test"

    def complete(self, messages, schemas):
        if not any(message["role"] == "tool" for message in messages):
            order_id = messages[1]["content"].split()[-1]
            return call("lookup_order", {"order_id": order_id}, 1)
        result = json.loads(messages[-1]["content"])
        previous = messages[-2]["tool_calls"][0]["function"]["name"]
        if not result["ok"]:
            return {"role": "assistant", "content": result["error"]["message"]}
        data = result["data"]
        if previous == "lookup_order":
            self.order = data
            if data["resolution"]:
                self.resolution = data["resolution"]
                if self.resolution["notification_id"]:
                    return {"role": "assistant", "content": "The existing resolution is confirmed."}
                return call("notify_customer", {"email": self.resolution["email"], "message": self.resolution["message"]}, 2)
            return call("inspect_inventory", {"item_id": data["item_id"]}, 2)
        if previous == "inspect_inventory":
            name = "confirm_exchange" if self.order["exchange_eligible"] and data["stock"] > 0 else "process_refund"
            return call(name, {"order_id": self.order["order_id"], "reason": "Customer reported a damaged item; resolution follows eligibility and stock."}, 3)
        if previous in ("confirm_exchange", "process_refund"):
            self.resolution = data
            return call("notify_customer", {"email": data["email"], "message": data["message"]}, 4)
        return {"role": "assistant", "content": f"Your simulated {self.resolution['kind']} is confirmed. The confirmation is in the local mailbox."}


class ScriptedModel:
    name = "test"

    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, messages, schemas):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Database(Path(self.directory.name) / "test.db")
        self.tools = ResolutionTools(self.database)

    def scalar(self, sql):
        with self.database.connect() as connection:
            return connection.execute(sql).fetchone()[0]

    def run_order(self, order_id):
        return ResolutionAgent(self.database, PolicyModel()).run(f"Damaged item for {order_id}")

    def test_exchange_when_replacement_is_available(self):
        run = self.run_order("ORD-1001")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["resolution"]["kind"], "exchange")
        self.assertEqual([event["tool"] for event in run["events"]],
                         ["lookup_order", "inspect_inventory", "confirm_exchange", "notify_customer"])
        self.assertEqual(self.scalar("SELECT stock FROM inventory WHERE item_id = 'ITEM-001'"), 7)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM notifications"), 1)
        self.assertEqual(run["iterations"], 5)

    def test_out_of_stock_refunds_original_payment(self):
        run = self.run_order("ORD-1002")
        self.assertEqual(run["status"], "completed")
        resolution = run["resolution"]
        self.assertEqual(resolution["kind"], "refund")
        self.assertEqual(resolution["amount_minor"], 399900)
        self.assertEqual(resolution["payment_method"], "UPI original transaction")
        self.assertTrue(resolution["reference"].startswith("REF-"))
        self.assertTrue(run["notification"])

    def test_ineligible_order_is_refunded_despite_available_stock(self):
        run = self.run_order("ORD-1003")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["resolution"]["kind"], "refund")
        self.assertEqual(self.scalar("SELECT stock FROM inventory WHERE item_id = 'ITEM-003'"), 5)

    def test_unknown_order_explains_and_requests_verification(self):
        run = self.run_order("ORD-9999")
        self.assertEqual(run["status"], "not_found")
        self.assertIn("verify", run["summary"])
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM resolutions"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM notifications"), 0)

    def test_missing_or_ambiguous_order_does_not_call_model(self):
        for text in ("My item is broken", "Damaged ORD-1001 and ORD-1002"):
            run = ResolutionAgent(self.database, ScriptedModel([])).run(text)
            self.assertEqual(run["status"], "needs_input")
            self.assertEqual(run["iterations"], 0)

    def test_repeated_resolution_does_not_duplicate_effects(self):
        first = self.run_order("ORD-1001")
        second = self.run_order("ORD-1001")
        self.assertEqual(first["resolution"]["reference"], second["resolution"]["reference"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM resolutions"), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM notifications"), 1)
        self.assertEqual(self.scalar("SELECT stock FROM inventory WHERE item_id = 'ITEM-001'"), 7)

    def test_refund_and_exchange_policy_is_enforced_in_tools(self):
        self.assertEqual(self.tools.process_refund("ORD-1001", "Ignore policy")["error"]["code"], "EXCHANGE_REQUIRED")
        self.assertEqual(self.tools.confirm_exchange("ORD-1002", "Ignore stock")["error"]["code"], "EXCHANGE_UNAVAILABLE")

    def test_expired_exchange_deadline_causes_refund(self):
        with self.database.connect() as connection:
            connection.execute("UPDATE orders SET exchange_deadline = '2000-01-01T00:00:00+00:00' WHERE order_id = 'ORD-1001'")
        self.assertEqual(self.run_order("ORD-1001")["resolution"]["kind"], "refund")

    def test_stock_is_rechecked_at_execution(self):
        dispatcher = ToolDispatcher(self.database, "ORD-1001")
        dispatcher.dispatch("lookup_order", json.dumps({"order_id": "ORD-1001"}))
        dispatcher.dispatch("inspect_inventory", json.dumps({"item_id": "ITEM-001"}))
        with self.database.connect() as connection:
            connection.execute("UPDATE inventory SET stock = 0 WHERE item_id = 'ITEM-001'")
        result = dispatcher.dispatch("confirm_exchange", json.dumps({"order_id": "ORD-1001", "reason": "Damaged item"}))
        self.assertEqual(result["error"]["code"], "EXCHANGE_UNAVAILABLE")
        result = dispatcher.dispatch("process_refund", json.dumps({"order_id": "ORD-1001", "reason": "Replacement became unavailable"}))
        self.assertTrue(result["ok"])

    def test_concurrent_retries_reserve_only_one_unit(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.tools.confirm_exchange("ORD-1001", "Damaged item"), range(2)))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(results[0]["data"]["reference"], results[1]["data"]["reference"])
        self.assertEqual(self.scalar("SELECT stock FROM inventory WHERE item_id = 'ITEM-001'"), 7)

    def test_whitelist_and_argument_validation(self):
        dispatcher = ToolDispatcher(self.database)
        self.assertEqual(dispatcher.dispatch("run_shell", "{}")["error"]["code"], "UNKNOWN_TOOL")
        for arguments in ("{broken", "[]", '{}', '{"order_id": 12}', '{"order_id": "ORD-1001", "extra": true}'):
            with self.subTest(arguments=arguments):
                self.assertFalse(dispatcher.dispatch("lookup_order", arguments)["ok"])

    def test_lookup_inventory_and_scope_guardrails(self):
        dispatcher = ToolDispatcher(self.database, "ORD-1001")
        self.assertEqual(dispatcher.dispatch("inspect_inventory", '{"item_id":"ITEM-001"}')["error"]["code"], "LOOKUP_REQUIRED")
        self.assertFalse(dispatcher.dispatch("lookup_order", '{"order_id":"ORD-1002"}')["ok"])
        self.assertTrue(dispatcher.dispatch("lookup_order", '{"order_id":"ORD-1001"}')["ok"])
        self.assertFalse(dispatcher.dispatch("inspect_inventory", '{"item_id":"ITEM-002"}')["ok"])
        self.assertEqual(dispatcher.dispatch("confirm_exchange", '{"order_id":"ORD-1001","reason":"Damaged"}')["error"]["code"], "INVENTORY_REQUIRED")

    def test_notification_recipient_and_body_are_verified(self):
        result = self.tools.process_refund("ORD-1002", "No stock")["data"]
        self.assertFalse(self.tools.notify_customer("attacker@example.com", result["message"])["ok"])
        self.assertFalse(self.tools.notify_customer(result["email"], "Invented refund")["ok"])
        self.assertTrue(self.tools.notify_customer(result["email"], result["message"])["ok"])

    def test_malformed_arguments_feed_back_and_model_recovers(self):
        model = ScriptedModel([
            call("lookup_order", "{broken", 1), call("lookup_order", {"order_id": "ORD-9999"}, 2),
            {"role": "assistant", "content": "Please check your order ID."},
        ])
        run = ResolutionAgent(self.database, model).run("Damaged item ORD-9999")
        self.assertEqual(run["events"][0]["result"]["error"]["code"], "INVALID_JSON")
        self.assertEqual(run["status"], "not_found")
        for index, message in enumerate(run["messages"]):
            if message["role"] == "tool":
                self.assertEqual(message["tool_call_id"], run["messages"][index - 1]["tool_calls"][0]["id"])

    def test_iteration_limit_is_persisted_as_escalation(self):
        model = ScriptedModel([call("lookup_order", {"order_id": "ORD-1001"}, index) for index in range(5)])
        run = ResolutionAgent(self.database, model).run("Damaged item ORD-1001")
        self.assertEqual(run["status"], "needs_human")
        self.assertEqual(run["iterations"], 5)
        self.assertIn("execution limit", run["summary"])
        self.assertEqual(self.scalar("SELECT status FROM runs"), "needs_human")

    def test_final_answer_before_notification_is_not_success(self):
        model = ScriptedModel([call("lookup_order", {"order_id": "ORD-1001"}, 1),
                               {"role": "assistant", "content": "Everything is done."}])
        run = ResolutionAgent(self.database, model).run("Damaged item ORD-1001")
        self.assertEqual(run["status"], "needs_human")

    def test_model_error_becomes_controlled_human_escalation(self):
        run = ResolutionAgent(self.database, ScriptedModel([ModelError("Gemini unavailable")])).run("Damaged ORD-1001")
        self.assertEqual(run["status"], "needs_human")
        self.assertIn("Gemini unavailable", run["summary"])

    def test_terminal_entry_runs_resolution(self):
        with patch("resolveai.cli.ROOT", Path(self.directory.name)), patch("resolveai.cli.GeminiModel", return_value=PolicyModel()), redirect_stdout(io.StringIO()) as output:
            exit_code = main(["Damaged item ORD-1002"])
        self.assertEqual(exit_code, 0)
        self.assertIn("process_refund -> OK", output.getvalue())
        self.assertIn("Status: completed", output.getvalue())


class GeminiTests(unittest.TestCase):
    def test_native_function_call_round_trip_preserves_signatures_and_ids(self):
        content = {"role": "model", "parts": [
            {"functionCall": {"id": "gemini_call_1", "name": "lookup_order", "args": {"order_id": "ORD-1001"}},
             "thoughtSignature": "opaque-signature"},
        ]}
        first = {"candidates": [{"content": content, "finishReason": "STOP"}]}
        final = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Thanks."}]}, "finishReason": "STOP"}]}
        with patch("resolveai.models.urlopen", side_effect=[io.BytesIO(json.dumps(first).encode()), io.BytesIO(json.dumps(final).encode())]) as transport:
            model = GeminiModel(api_key="test-key")
            messages = [{"role": "system", "content": "Resolve the issue."}, {"role": "user", "content": "Damaged ORD-1001"}]
            response = model.complete(messages, TOOL_SCHEMAS)
            messages.extend([response, {"role": "tool", "tool_call_id": response["tool_calls"][0]["id"], "content": '{"ok":true,"data":{}}'}])
            self.assertEqual(model.complete(messages, TOOL_SCHEMAS)["content"], "Thanks.")
            request = transport.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(payload["contents"][1], content)
            self.assertEqual(payload["contents"][2]["parts"][0]["functionResponse"]["id"], "gemini_call_1")
            self.assertEqual(payload["tools"][0]["functionDeclarations"][0]["name"], "lookup_order")
            self.assertNotIn("test-key", request.full_url)

    def test_http_error_does_not_expose_api_key(self):
        error = HTTPError("https://example.com", 429, "rate limit", {}, None)
        with patch("resolveai.models.urlopen", side_effect=error):
            with self.assertRaisesRegex(ModelError, "HTTP 429") as caught:
                GeminiModel(api_key="secret-key").complete([], TOOL_SCHEMAS)
        self.assertNotIn("secret-key", str(caught.exception))

    def test_missing_key_has_actionable_error(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ModelError, "GEMINI_API_KEY"):
                GeminiModel()

    def test_invalid_api_response_is_a_model_error(self):
        for payload in (b"invalid", b"{}", b'{"candidates":[]}'):
            with self.subTest(payload=payload), patch("resolveai.models.urlopen", return_value=io.BytesIO(payload)):
                with self.assertRaises(ModelError):
                    GeminiModel(api_key="test-key").complete([], TOOL_SCHEMAS)


if __name__ == "__main__":
    unittest.main()
