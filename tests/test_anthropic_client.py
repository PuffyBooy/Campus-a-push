"""Claude client (llm.provider: anthropic) with a fake SDK stream - no network."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from typo3_publisher.anthropic_llm import AnthropicClient, LLMRefusalError
from typo3_publisher.config import LLMSettings, RetryPolicy
from typo3_publisher.llm import api_key_env, create_llm

RETRY = RetryPolicy(attempts=2, base_delay_seconds=0, max_delay_seconds=0)


def message(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(explanation="declined"),
        model="claude-opus-5-5",
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


class FakeMessages:
    def __init__(self, *results):
        self.results = list(results)
        self.requests: list[dict] = []

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        result = self.results.pop(0)
        return nullcontext(SimpleNamespace(get_final_message=lambda: result))


def client_with(*results, **settings) -> tuple[AnthropicClient, FakeMessages]:
    client = create_llm(LLMSettings(provider="anthropic", **settings), RETRY, "sk-ant-" + "a" * 30)
    assert isinstance(client, AnthropicClient)
    fake = FakeMessages(*results)
    client._client = SimpleNamespace(beta=SimpleNamespace(messages=fake))
    return client, fake


def call(client, **kw):
    return client.complete_json(
        system="Answer in JSON.", user="hi", model="claude-opus-5-5", temperature=0.7, purpose="test", **kw
    )


def test_request_shape_and_json_answer():
    assert api_key_env(LLMSettings(provider="anthropic")) == "ANTHROPIC_API_KEY"
    client, fake = client_with(message('```json\n{"ok": true}\n```'), reasoning_effort="high")
    assert call(client, images=["data:image/jpeg;base64,QUJD"]) == {"ok": True}
    req = fake.requests[0]
    assert "temperature" not in req and req["output_config"] == {"effort": "high"}
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    image, text = req["messages"][0]["content"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}
    assert text == {"type": "text", "text": "hi"}


def test_effort_left_out_when_not_a_claude_level():
    client, fake = client_with(message("{}"), reasoning_effort="")
    call(client)
    assert "output_config" not in fake.requests[0]


def test_truncated_answer_is_retried_and_refusal_is_not():
    client, fake = client_with(message("{", "max_tokens"), message('{"a": 1}'))
    assert call(client) == {"a": 1}
    client, fake = client_with(message("", "refusal"), message("{}"))
    with pytest.raises(LLMRefusalError):
        call(client)
    assert len(fake.requests) == 1
