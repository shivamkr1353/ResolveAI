import json
import os
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


ORDER_PATTERN = re.compile(r"\bORD[-\s]?(\d{1,12})\b", re.IGNORECASE)


def extract_order_ids(text):
    return list(dict.fromkeys(f"ORD-{match}" for match in ORDER_PATTERN.findall(text)))


class ModelError(Exception):
    pass


class GeminiModel:
    name = "gemini"

    def __init__(self, model=None, api_key=None, timeout=45):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        self.timeout = timeout
        if not self.api_key:
            raise ModelError("Set GEMINI_API_KEY in .env or your terminal environment before running the agent.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.model):
            raise ModelError("GEMINI_MODEL must be a model ID such as gemini-3.8-flash.")

    def complete(self, messages, schemas):
        contents = []
        calls = {}
        system = []
        for message in messages:
            if message["role"] == "system":
                system.append({"text": message["content"]})
            elif message["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": message["content"]}]})
            elif message["role"] == "assistant":
                for call in message.get("tool_calls", []):
                    calls[call["id"]] = call
                contents.append(message["provider_content"])
            elif message["role"] == "tool":
                call = calls[message["tool_call_id"]]
                result = {"name": call["function"]["name"], "response": json.loads(message["content"])}
                if call.get("provider_id"):
                    result["id"] = call["provider_id"]
                contents.append({"role": "user", "parts": [{"functionResponse": result}]})
        declarations = [{"name": entry["function"]["name"], "description": entry["function"]["description"],
                         "parametersJsonSchema": entry["function"]["parameters"]} for entry in schemas]
        payload = {"systemInstruction": {"parts": system}, "contents": contents,
                   "tools": [{"functionDeclarations": declarations}],
                   "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}}}
        request = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read(2_000_000))
        except HTTPError as error:
            explanations = {400: "Check the model and request configuration.",
                            401: "Check GEMINI_API_KEY.", 403: "Check GEMINI_API_KEY and API access.",
                            404: "Check GEMINI_MODEL and model availability.",
                            429: "The API quota or rate limit was reached. Retry later."}
            detail = explanations.get(error.code, "The Gemini service is unavailable. Retry later.")
            raise ModelError(f"Gemini returned HTTP {error.code}. {detail}") from None
        except (URLError, socket.timeout, TimeoutError, OSError):
            raise ModelError("The Gemini service could not be reached. Check connectivity or retry later.") from None
        except (json.JSONDecodeError, UnicodeError):
            raise ModelError("The Gemini service returned invalid JSON.") from None
        try:
            candidate = data["candidates"][0]
            if candidate.get("finishReason") not in (None, "STOP"):
                raise ModelError("Gemini could not finish a valid response. Human assistance is required.")
            content = candidate["content"]
            parts = content["parts"]
            if not isinstance(parts, list) or not all(isinstance(part, dict) for part in parts):
                raise TypeError
            result = {"role": "assistant", "content": "".join(
                part["text"] for part in parts if "text" in part and not part.get("thought")
            ), "provider_content": content}
            tool_calls = []
            for part in parts:
                if "functionCall" in part:
                    call = part["functionCall"]
                    tool_calls.append({
                        "id": f"call_{uuid4().hex}", "provider_id": call.get("id"), "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call.get("args", {}))},
                    })
            if tool_calls:
                result["tool_calls"] = tool_calls
            return result
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("Gemini returned no usable response. Check the request or retry later.") from None
