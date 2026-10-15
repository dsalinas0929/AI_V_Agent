from types import SimpleNamespace

import pytest
from google.genai import types

from agent_v.client import safe_text, validate_server
from agent_v.model import GeminiModel


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///tmp",
        "https://user:pass@host",
        "https://host?token=secret",
        "https://host/path",
    ],
)
def test_unsafe_server_urls(url):
    with pytest.raises(ValueError):
        validate_server(url)


def test_local_and_secure_urls():
    assert validate_server("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert validate_server("https://example.com") == "https://example.com"
    assert "\x1b" not in safe_text("\x1b[31mred\x1b[0m")


async def test_gemini_preserves_function_call_signature():
    signature = b"test-signature"
    part = types.Part(
        function_call=types.FunctionCall(name="read_file", args={"path": "a"}),
        thought_signature=signature,
    )

    class FakeModels:
        async def generate_content_stream(self, **kwargs):
            assert kwargs["config"].automatic_function_calling.disable

            async def stream():
                for item in [types.Part.from_text(text="Reading"), part]:
                    yield types.GenerateContentResponse(
                        candidates=[
                            types.Candidate(content=types.Content(role="model", parts=[item]))
                        ]
                    )

            return stream()

    model = object.__new__(GeminiModel)
    model.model = "test-model"
    model.client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    events = []

    async def emit(event):
        events.append(event)

    result = await model.generate([{"role": "user", "parts": [{"text": "Read a"}]}], emit)
    serialized = result.model_dump(mode="json", exclude_none=True)
    restored = types.Content.model_validate(serialized)
    assert restored.parts[1].thought_signature == signature
    assert restored.parts[1].function_call.name == "read_file"
    assert events == [{"type": "delta", "text": "Reading"}]
