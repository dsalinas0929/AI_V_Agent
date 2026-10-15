"""Server-only SayGM Chat Completions adapter using the existing HTTP dependency."""

import json

import httpx
from google.genai import types

from agent_v.model import SYSTEM, TOOLS

BASE_URL = "https://api.saygm.com/v1"
DEFAULT_MODEL = "gpt-5.4-mini"


class ProviderError(RuntimeError):
    """A safe error message that can be shown to clients without leaking provider data."""


def provider_error(status: int) -> ProviderError:
    messages = {
        401: "SayGM rejected the API key. Check GM_API_KEY on the server.",
        402: "SayGM credit is exhausted. Check your SayGM account balance.",
        403: "SayGM denied access. Check your account and model permissions.",
        404: "SayGM model or endpoint not found. Check AGENT_V_MODEL on the server.",
        429: "SayGM rate limit reached. Wait and retry.",
    }
    return ProviderError(
        messages.get(status, f"SayGM request failed (HTTP {status}). Retry later.")
    )


def messages_from_history(history: list[dict]) -> list[dict]:
    """Keep the existing on-disk format; translate it only at the provider boundary."""
    messages = [{"role": "system", "content": SYSTEM}]
    pending = []
    for index, item in enumerate(history):
        content = types.Content.model_validate(item)
        parts = content.parts or []
        text = "".join(part.text or "" for part in parts if not part.thought)
        if content.role == "model":
            calls = []
            for part in parts:
                if call := part.function_call:
                    call_id = call.id or f"call_{index}_{len(calls)}"
                    calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.args or {}),
                            },
                        }
                    )
                    pending.append((call_id, call.name))
            message = {"role": "assistant", "content": text or None}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
        else:
            if text:
                messages.append({"role": "user", "content": text})
            for part in parts:
                if result := part.function_response:
                    match = next(
                        (
                            entry
                            for entry in pending
                            if (entry[0] == result.id if result.id else entry[1] == result.name)
                        ),
                        None,
                    )
                    if match is None:
                        raise ProviderError("Conversation has an unmatched tool result. Use /new.")
                    pending.remove(match)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": match[0],
                            "content": json.dumps(result.response or {}),
                        }
                    )
    return messages


def function_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": declaration.name,
                "description": declaration.description,
                "parameters": declaration.parameters_json_schema,
            },
        }
        for tool in TOOLS
        for declaration in tool.function_declarations or []
    ]


class SayGMModel:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, transport=None):
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=BASE_URL + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(90, connect=15),
            follow_redirects=False,
            transport=transport,
        )

    async def generate(self, history: list[dict], emit) -> types.Content:
        payload = {
            "model": self.model,
            "messages": messages_from_history(history),
            "tools": function_tools(),
            "stream": True,
            "max_completion_tokens": 4096,
        }
        try:
            return await self._stream(payload, emit)
        except httpx.TimeoutException:
            raise ProviderError("SayGM request timed out. Retry later.") from None
        except httpx.HTTPError:
            raise ProviderError(
                "Connection to SayGM failed or was interrupted. Retry later."
            ) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ProviderError(
                "SayGM returned an invalid response. No requested tools were run."
            ) from None

    async def _stream(self, payload, emit):
        texts = []
        calls = {}
        finish_reason = None
        received = 0
        async with self.client.stream("POST", "chat/completions", json=payload) as response:
            if response.status_code != 200:
                # Never send raw upstream errors (which may echo credentials/prompts) to the CLI.
                raise provider_error(response.status_code)
            async for line in response.aiter_lines():
                received += len(line)
                if received > 1_000_000:
                    raise ProviderError("SayGM response exceeded the size limit.")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                if event.get("error"):
                    raise ProviderError("SayGM reported a streaming error. Retry later.")
                for choice in event.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or delta.get("refusal")
                    if text:
                        texts.append(text)
                        await emit({"type": "delta", "text": text})
                    for fragment in delta.get("tool_calls") or []:
                        index = fragment["index"]
                        if not isinstance(index, int) or not 0 <= index < 8:
                            raise ProviderError("SayGM requested too many tools in one step.")
                        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if fragment.get("id"):
                            call["id"] = fragment["id"]
                        function = fragment.get("function") or {}
                        call["name"] += function.get("name") or ""
                        call["arguments"] += function.get("arguments") or ""
        if finish_reason not in {"stop", "tool_calls"}:
            raise ProviderError(
                "SayGM response was incomplete or filtered. No requested tools were run."
            )
        if calls and finish_reason != "tool_calls":
            raise ProviderError(
                "SayGM returned incomplete tool calls. No requested tools were run."
            )
        parts = [types.Part.from_text(text="".join(texts))] if texts else []
        seen_ids = set()
        for index in sorted(calls):
            call = calls[index]
            args = json.loads(call["arguments"])
            if not call["id"] or not call["name"] or not isinstance(args, dict):
                raise ValueError("Incomplete tool call")
            if call["id"] in seen_ids:
                raise ValueError("Duplicate tool call ID")
            seen_ids.add(call["id"])
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(id=call["id"], name=call["name"], args=args)
                )
            )
        if not parts:
            raise ProviderError("SayGM returned no answer. Try rephrasing your request.")
        return types.Content(role="model", parts=parts)

    async def close(self):
        await self.client.aclose()
