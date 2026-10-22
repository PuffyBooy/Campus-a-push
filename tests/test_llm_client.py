"""OpenAI-compatible client (Groq / OpenAI) with a fake transport - no network."""

from types import SimpleNamespace

import openai
import pytest

from typo3_publisher.config import LLMSettings, RetryPolicy, Secrets
from typo3_publisher.llm import LLMError, LLMQuotaError, OpenAIClient, api_key_env, create_llm, rate_limit_wait
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


def rate_limited(message: str) -> openai.RateLimitError:
    err = openai.RateLimitError.__new__(openai.RateLimitError)
    Exception.__init__(err, message)
    return err


GROQ_TPM = (
    "Error code: 429 - Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute (TPM): "
    "Limit 8000, Used 5942, Requested 5930. Please try again in 29.04s."
)


def test_rate_limit_wait_reads_the_requested_delay():
    assert rate_limit_wait(rate_limited(GROQ_TPM)) == pytest.approx(29.04)
    assert rate_limit_wait(rate_limited("Please try again in 1m2.5s.")) == pytest.approx(62.5)
    assert rate_limit_wait(rate_limited("Please try again in 2h3m.")) == pytest.approx(7380)
    assert rate_limit_wait(rate_limited("slow down")) is None


def test_waits_as_long_as_groq_asks_and_then_succeeds(monkeypatch):
    waits = []
    monkeypatch.setattr("typo3_publisher.retry.sleep", waits.append)
    client, fake = client_with(rate_limited(GROQ_TPM), rate_limited(GROQ_TPM), answer('{"ok": true}'))
    assert call(client) == {"ok": True}
    assert waits == [pytest.approx(30.04), pytest.approx(30.04)]
    assert len(fake.requests) == 3


def test_daily_limit_stops_with_a_clear_error(monkeypatch):
    monkeypatch.setattr("typo3_publisher.retry.sleep", lambda s: None)
    client, fake = client_with(rate_limited("Limit on tokens per day (TPD). Please try again in 1h12m3s."))
    with pytest.raises(LLMQuotaError, match="try again later"):
        call(client)
    assert len(fake.requests) == 1


def test_request_larger_than_the_minute_limit_stops_clearly(monkeypatch):
    err = openai.APIStatusError.__new__(openai.APIStatusError)
    Exception.__init__(err, "Error code: 413 - Request too large for model on tokens per minute (TPM): Limit 8000")
    err.status_code = 413
    client, fake = client_with(err)
    with pytest.raises(LLMQuotaError, match="max_chars_per_source"):
        call(client)
    assert len(fake.requests) == 1


def test_reasoning_effort_is_sent_and_dropped_for_models_without_it():
    client, fake = client_with(answer('{"ok": 1}'))
    client.settings.reasoning_effort = "low"
    assert call(client) == {"ok": 1}
    assert fake.requests[0]["reasoning_effort"] == "low"

    client, fake = client_with(bad_request("`reasoning_effort` is not supported with this model"), answer('{"ok": 2}'))
    client.settings.reasoning_effort = "low"
    assert call(client) == {"ok": 2}
    assert "reasoning_effort" not in fake.requests[1]
    assert fake.requests[1]["response_format"] == {"type": "json_object"}


def test_mistral_uses_its_endpoint_key_and_max_tokens():
    settings = LLMSettings(provider="mistral")
    assert api_key_env(settings) == "MISTRAL_API_KEY"
    client = create_llm(settings, RETRY, "m" * 32)
    assert str(client._client.base_url).startswith("https://api.mistral.ai/v1")
    client, fake = client_with(answer('{"ok": 1}'), provider="mistral")
    assert call(client) == {"ok": 1}
    assert "max_tokens" in fake.requests[0] and "max_completion_tokens" not in fake.requests[0]


def test_unknown_parameter_422_is_retried_without_it():
    err = openai.UnprocessableEntityError.__new__(openai.UnprocessableEntityError)
    Exception.__init__(err, "Error code: 422 - reasoning_effort: Extra inputs are not permitted")
    client, fake = client_with(err, answer('{"ok": 3}'), provider="mistral")
    client.settings.reasoning_effort = "low"
    assert call(client) == {"ok": 3}
    assert "reasoning_effort" not in fake.requests[1]


def test_rate_limit_without_a_hint_waits_the_default(monkeypatch):
    waits = []
    monkeypatch.setattr("typo3_publisher.retry.sleep", waits.append)
    client, _fake = client_with(rate_limited("Requests rate limit exceeded"), answer('{"ok": 4}'), provider="mistral")
    assert call(client) == {"ok": 4}
    assert waits == [pytest.approx(21)]


def tier_not_allowed() -> openai.PermissionDeniedError:
    err = openai.PermissionDeniedError.__new__(openai.PermissionDeniedError)
    Exception.__init__(
        err, "Error code: 403 - This model is not available in your subscription tier (tier_not_allowed)"
    )
    return err


def test_model_not_in_the_plan_falls_back_and_is_remembered():
    client, fake = client_with(tier_not_allowed(), answer('{"ok": 5}'), answer('{"ok": 6}'), provider="mistral")
    client.settings.fallback_models = ["mistral-medium-latest", "mistral-small-latest"]
    assert client.complete_json(system="s", user="u", model="mistral-large-latest", temperature=0.2, purpose="t") == {
        "ok": 5
    }
    assert [r["model"] for r in fake.requests] == ["mistral-large-latest", "mistral-medium-latest"]
    client.complete_json(system="s", user="u", model="mistral-large-latest", temperature=0.2, purpose="t")
    assert fake.requests[-1]["model"] == "mistral-medium-latest"  # the unavailable model is not asked again


def test_without_fallbacks_the_tier_error_is_raised():
    client, _fake = client_with(tier_not_allowed(), provider="mistral")
    with pytest.raises(openai.PermissionDeniedError):
        call(client)


def test_a_model_that_keeps_rate_limiting_hands_over_to_the_next(monkeypatch):
    waits = []
    monkeypatch.setattr("typo3_publisher.retry.sleep", waits.append)
    busy = [rate_limited("Rate limit exceeded") for _ in range(3)]
    client, fake = client_with(*busy, answer('{"ok": 7}'), answer('{"ok": 8}'), provider="mistral")
    client.settings.fallback_models = ["mistral-small-latest"]
    assert client.complete_json(system="s", user="u", model="mistral-medium-latest", temperature=0.2, purpose="t") == {
        "ok": 7
    }
    assert [r["model"] for r in fake.requests] == ["mistral-medium-latest"] * 3 + ["mistral-small-latest"]
    assert len(waits) == 2  # two waits, then the switch instead of a third
    client.complete_json(system="s", user="u", model="mistral-medium-latest", temperature=0.2, purpose="t")
    assert fake.requests[-1]["model"] == "mistral-small-latest"


def test_the_last_model_keeps_waiting_instead_of_switching(monkeypatch):
    monkeypatch.setattr("typo3_publisher.retry.sleep", lambda s: None)
    busy = [rate_limited("Rate limit exceeded") for _ in range(4)]
    client, fake = client_with(*busy, answer('{"ok": 9}'), provider="mistral")
    assert call(client) == {"ok": 9}
    assert len(fake.requests) == 5
