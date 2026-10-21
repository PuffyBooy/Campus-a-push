"""LLM access. Only JSON-returning chat completions are used."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, Protocol

from . import retry as retry_module
from .config import LLMSettings, RetryPolicy
from .logging_utils import get_logger
from .retry import retry_call

log = get_logger("llm")


class LLMError(RuntimeError):
    pass


class LLMQuotaError(RuntimeError):
    """The provider's limit needs a longer wait than llm.rate_limit_max_wait_seconds (e.g. a daily limit)."""


class LLMRequestTooLargeError(LLMQuotaError):
    """This one request exceeds the provider's per-minute token limit; waiting does not help."""


def rate_limit_wait(exc: Exception) -> float | None:
    """Seconds the provider asks to wait: Retry-After header, or "try again in 1m2.5s" in the message."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    try:
        if header:
            return float(header)
    except ValueError:
        pass
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", str(exc))
    if not m or not any(m.groups()):
        return None
    hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    return hours * 3600 + minutes * 60 + seconds


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


# OpenAI-compatible chat-completions providers: default endpoint and API-key variable.
PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {"base_url": "", "key_env": "OPENAI_API_KEY"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY"},
}


def api_key_env(settings: LLMSettings) -> str:
    return settings.api_key_env or PROVIDERS.get(settings.provider, PROVIDERS["openai"])["key_env"]


class OpenAIClient:
    """Chat-completions client for OpenAI and OpenAI-compatible providers such as Groq."""

    def __init__(self, api_key: str, settings: LLMSettings, retry: RetryPolicy):
        import openai

        base_url = settings.base_url or PROVIDERS.get(settings.provider, PROVIDERS["openai"])["base_url"] or None
        self._openai = openai
        self._client = openai.OpenAI(
            api_key=api_key, base_url=base_url, timeout=settings.timeout_seconds, max_retries=0
        )
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
                # Mistral's chat completions know only max_tokens; OpenAI deprecated it for reasoning models.
                ("max_tokens" if self.settings.provider == "mistral" else "max_completion_tokens"): (
                    self.settings.max_output_tokens
                ),
            }
            if self.settings.reasoning_effort:
                kwargs["reasoning_effort"] = self.settings.reasoning_effort
            if temperature is not None:
                kwargs["temperature"] = temperature
            try:
                response = self._create(kwargs, purpose)
            except (o.BadRequestError, o.UnprocessableEntityError) as exc:  # 422: Mistral's unknown-field error
                text = str(exc)
                if "json_validate_failed" in text:
                    # Groq rejects answers that are not valid JSON; asking again usually works.
                    raise LLMError(f"provider could not produce valid JSON: {text[:200]}") from exc
                # A model without JSON mode or adjustable reasoning: ask again without that parameter.
                param = next((p for p in ("reasoning_effort", "response_format") if p in text and p in kwargs), None)
                if param is None:
                    raise
                log.warning("model %s does not accept %s; retrying without it", model, param)
                kwargs.pop(param)
                response = self._create(kwargs, purpose)
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise LLMError("model output was truncated (increase llm.max_output_tokens)")
            usage = getattr(response, "usage", None)
            log.info("LLM %s call (%s) finished; tokens=%s", purpose, model, getattr(usage, "total_tokens", "?"))
            return parse_json_object(choice.message.content or "")

        return retry_call(call, self.retry, retry_on=transient, description=f"LLM {purpose}")

    def _create(self, kwargs: dict[str, Any], purpose: str) -> Any:
        """One request. On a rate limit, wait as long as the provider asks (Groq's free tier allows
        only a few thousand tokens per minute, so one article step often has to wait ~30-60 s)."""
        s = self.settings
        for attempt in range(s.rate_limit_retries + 1):
            try:
                return self._client.chat.completions.create(**kwargs)
            except self._openai.APIStatusError as exc:
                if getattr(exc, "status_code", None) == 413 or "Request too large" in str(exc):
                    raise LLMRequestTooLargeError(
                        "the request is larger than the provider allows per minute; lower "
                        "research.max_chars_per_source / research.max_sources or upgrade the plan: "
                        f"{str(exc)[:300]}"
                    ) from exc
                if not isinstance(exc, self._openai.RateLimitError):
                    raise
                # No hint (e.g. Mistral): wait a fixed time instead of retrying at once.
                wait = rate_limit_wait(exc) or s.rate_limit_default_wait_seconds
                if wait > s.rate_limit_max_wait_seconds:
                    raise LLMQuotaError(
                        f"{s.provider} rate limit needs a wait of {wait:.0f}s (more than "
                        f"llm.rate_limit_max_wait_seconds={s.rate_limit_max_wait_seconds:.0f}); try again later: "
                        f"{str(exc)[:300]}"
                    ) from exc
                if attempt >= s.rate_limit_retries:
                    raise
                log.warning(
                    "LLM %s: %s rate limit, waiting %.0fs as requested (%d/%d)",
                    purpose,
                    s.provider,
                    wait + 1,
                    attempt + 1,
                    s.rate_limit_retries,
                )
                retry_module.sleep(wait + 1)
        raise AssertionError("unreachable")  # pragma: no cover


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
    if settings.provider not in PROVIDERS:
        raise LLMError(f"unsupported llm.provider {settings.provider!r}")
    if not api_key:
        raise LLMError(f"{api_key_env(settings)} is not set (llm.provider: {settings.provider})")
    return OpenAIClient(api_key, settings, retry)
