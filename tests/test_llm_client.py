"""OpenAI-compatible client (Groq / OpenAI) with a fake transport - no network."""

from types import SimpleNamespace

import openai
import pytest

from typo3_publisher.config import LLMSettings, RetryPolicy, Secrets
from typo3_publisher.llm import LLMError, OpenAIClient, api_key_env, create_llm
from typo3_publisher.logging_utils import redact

RETRY = RetryPolicy(attempts=3, base_delay_seconds=0, max_delay_seconds=0)


def bad_request(message: str) -> openai.BadRequestError:
    # Built without an HTTP response object: openai 1.x uses httpx, 3.x httpx2.
    err = openai.BadRequestError.__new__(openai.BadRequestError)
    Exception.__init__(err, message)
    return err


def answer(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=42),
    )


class FakeCompletions:
    def __init__(self, *results):
        self.results = list(results)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client_with(*results, provider="groq") -> tuple[OpenAIClient, FakeCompletions]:
    client = OpenAIClient("gsk_" + "a" * 30, LLMSettings(provider=provider), RETRY)
    fake = FakeCompletions(*results)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return client, fake


def call(client, **kw):
    return client.complete_json(
        system="Answer in JSON.", user="hi", model="openai/gpt-oss-120b", temperature=0.2, purpose="test", **kw
    )


def test_groq_uses_its_endpoint_and_key_variable():
    settings = LLMSettings(provider="groq")
    assert api_key_env(settings) == "GROQ_API_KEY"
    client = create_llm(settings, RETRY, "gsk_" + "b" * 30)
    assert str(client._client.base_url).startswith("https://api.groq.com/openai/v1")
    assert api_key_env(LLMSettings(provider="openai")) == "OPENAI_API_KEY"
    assert api_key_env(LLMSettings(provider="groq", api_key_env="MY_KEY")) == "MY_KEY"
    custom = create_llm(LLMSettings(provider="groq", base_url="https://proxy.example/v1"), RETRY, "k" * 10)
    assert str(custom._client.base_url).startswith("https://proxy.example/v1")
    with pytest.raises(LLMError, match="GROQ_API_KEY is not set"):
        create_llm(settings, RETRY, None)


def test_json_mode_request_and_parsing():
    client, fake = client_with(answer('{"ok": true}'))
    assert call(client) == {"ok": True}
    request = fake.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert request["model"] == "openai/gpt-oss-120b" and request["temperature"] == 0.2
    assert request["messages"][1]["content"] == "hi"


def test_invalid_json_from_groq_is_retried():
    client, fake = client_with(
        bad_request("Error code: 400 - {'error': {'code': 'json_validate_failed'}}"), answer('{"ok": 1}')
    )
    assert call(client) == {"ok": 1}
    assert len(fake.requests) == 2


def test_model_without_json_mode_falls_back_to_plain_prompt():
    client, fake = client_with(
        bad_request("response_format `json_object` is not supported with this model"), answer('Here you go: {"ok": 2}')
    )
    assert call(client) == {"ok": 2}
    assert "response_format" not in fake.requests[1]


def test_other_bad_requests_are_not_retried():
    client, fake = client_with(bad_request("model does not support image input"), answer("{}"))
    with pytest.raises(openai.BadRequestError):
        call(client, images=["data:image/png;base64,AAAA"])
    assert len(fake.requests) == 1


def test_images_are_sent_as_content_parts():
    client, fake = client_with(answer('{"caption": "c", "description": "d"}'))
    call(client, images=["data:image/png;base64,AAAA"])
    content = fake.requests[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_truncated_output_is_an_error():
    client, _ = client_with(answer('{"a": ', "length"), answer('{"a": ', "length"), answer('{"a": ', "length"))
    with pytest.raises(LLMError, match="truncated"):
        call(client)


def test_groq_key_is_redacted():
    key = "gsk_" + "Z9" * 20
    assert key not in redact(f"calling groq with {key}")
    Secrets({"GROQ_API_KEY": "custom-groq-secret-value"})
    assert "custom-groq-secret-value" not in redact("x custom-groq-secret-value y")
