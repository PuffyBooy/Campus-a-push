"""LLM access. Only JSON-returning chat completions are used."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

from .config import LLMSettings, RetryPolicy
from .logging_utils import get_logger
from .retry import retry_call

log = get_logger("llm")


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float | None,
        purpose: str,
        images: list[str] | None = None,
    ) -> dict[str, Any]: ...


def parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("model did not return JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise LLMError("model returned JSON that is not an object")
    return data


class OpenAIClient:
    def __init__(self, api_key: str, settings: LLMSettings, retry: RetryPolicy):
        import openai

        self._openai = openai
        self._client = openai.OpenAI(api_key=api_key, timeout=settings.timeout_seconds, max_retries=0)
        self.settings = settings
        self.retry = retry

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float | None,
        purpose: str,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        """``images`` are data URLs (``data:image/jpeg;base64,...``) for vision-capable models."""
        o = self._openai
        user_content: str | list[dict[str, Any]] = user
        if images:
            user_content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": url}} for url in images
            ]
        transient = (o.APIConnectionError, o.APITimeoutError, o.RateLimitError, o.InternalServerError, LLMError)

        def call() -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": self.settings.max_output_tokens,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            response = self._client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise LLMError("model output was truncated (increase llm.max_output_tokens)")
            usage = getattr(response, "usage", None)
            log.info("LLM %s call (%s) finished; tokens=%s", purpose, model, getattr(usage, "total_tokens", "?"))
            return parse_json_object(choice.message.content or "")

        return retry_call(call, self.retry, retry_on=transient, description=f"LLM {purpose}")


class FakeLLM:
    """Deterministic stand-in for tests and offline pipeline demos.

    ``responder(purpose, system, user)`` may be supplied; otherwise canned responses are
    produced. Content produced by FakeLLM is marked as test content and is refused by the
    publisher unless ``publishing.allow_test_content`` is enabled.
    """

    def __init__(
        self, responder: Callable[[str, str, str], dict[str, Any]] | None = None, language: str = "en", words: int = 700
    ):
        self.responder = responder
        self.calls: list[dict[str, Any]] = []
        self.language = language
        self.words = words

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str,
        temperature: float | None,
        purpose: str,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"purpose": purpose, "system": system, "user": user, "model": model, "images": images})
        if self.responder:
            return self.responder(purpose, system, user)
        if purpose in {"write", "revise", "update"}:
            return self._article(user)
        if purpose == "review":
            return {
                "scores": {
                    k: 9
                    for k in (
                        "factual_accuracy",
                        "readability",
                        "structure",
                        "grammar",
                        "seo",
                        "relevance",
                        "originality",
                        "clarity",
                        "instruction_adherence",
                    )
                },
                "critical_issues": [],
                "feedback": [],
                "summary": "Offline fake review.",
            }
        if purpose == "image_text":
            return {"caption": "[TEST] Symbolbild", "description": "[TEST] Platzhalter-Bildbeschreibung"}
        if purpose == "update_assessment":
            return {"needs_update": True, "reasons": ["offline fake assessment"], "score": 6}
        raise LLMError(f"FakeLLM has no canned response for {purpose}")

    def _article(self, user: str) -> dict[str, Any]:
        topic = re.search(r"TOPIC:\s*(.+)", user)
        topic_name = topic.group(1).strip() if topic else "Technology"
        sentence = (
            f"This offline placeholder paragraph about {topic_name.lower()} exists only to exercise "
            "the publishing pipeline and must never reach a production website."
        )
        paragraphs = []
        words = 0
        section = 0
        while words < self.words:
            section += 1
            paragraphs.append(f"<h2>Section {section}</h2><p>{sentence} {sentence} {sentence}</p>")
            words += 3 * len(sentence.split()) + 2
        return {
            "title": f"[TEST] Offline article about {topic_name}",
            "teaser": f"[TEST] Placeholder teaser about {topic_name} generated without an LLM.",
            "body_html": "".join(paragraphs),
            "seo_title": f"[TEST] {topic_name}",
            "meta_description": (
                "[TEST] Placeholder meta description generated offline for pipeline testing "
                "purposes only, never for publication on a live website."
            ),
            "keywords": [topic_name.lower(), "test"],
            "slug": f"test-offline-{topic_name.lower().replace(' ', '-')}",
            "image_keywords": [topic_name.lower(), "computer"],
            "article_type": "evergreen",
            "claims": [],
            "sources": [],
        }


def create_llm(settings: LLMSettings, retry: RetryPolicy, api_key: str | None) -> LLMClient:
    if settings.provider == "fake":
        return FakeLLM()
    if settings.provider != "openai":
        raise LLMError(f"unsupported llm.provider {settings.provider!r}")
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")
    return OpenAIClient(api_key, settings, retry)
