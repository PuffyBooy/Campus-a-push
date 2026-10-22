"""Claude through the official Anthropic SDK (llm.provider: anthropic)."""

from __future__ import annotations

import re
from typing import Any

from .config import LLMSettings, RetryPolicy
from .llm import LLMError, LLMRequestTooLargeError, parse_json_object
from .logging_utils import get_logger
from .retry import retry_call

log = get_logger("llm")

EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# On a safety decline, the API re-runs the request on a suitable fallback model in the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_DATA_URL_RE = re.compile(r"^data:(?P<media>image/[\w.+-]+);base64,(?P<data>.+)$", re.S)


class LLMRefusalError(RuntimeError):
    """The model (and its fallback) declined the request; asking again does not help."""


def image_block(url: str) -> dict[str, Any]:
    m = _DATA_URL_RE.match(url)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m["media"], "data": m["data"]}}
    return {"type": "image", "source": {"type": "url", "url": url}}


class AnthropicClient:
    """Messages API client. Claude takes no temperature; ``llm.reasoning_effort`` sets the effort."""

    def __init__(self, api_key: str, settings: LLMSettings, retry: RetryPolicy):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=settings.base_url or None,
            timeout=settings.timeout_seconds,
            max_retries=settings.rate_limit_retries,  # the SDK waits as long as retry-after asks
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
        a = self._anthropic
        content: str | list[dict[str, Any]] = user
        if images:
            content = [*(image_block(url) for url in images), {"type": "text", "text": user}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.settings.max_output_tokens,
            # The system prompt is the same for every article of a run: cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if self.settings.reasoning_effort in EFFORTS:
            kwargs["output_config"] = {"effort": self.settings.reasoning_effort}
        transient = (a.APIConnectionError, a.RateLimitError, a.InternalServerError, LLMError)

        def call() -> dict[str, Any]:
            try:
                with self._client.beta.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
            except a.RequestTooLargeError as exc:
                raise LLMRequestTooLargeError(f"the request is too large for the API: {str(exc)[:300]}") from exc
            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                raise LLMRefusalError(f"{model} declined the request: {getattr(details, 'explanation', '')}")
            if message.stop_reason == "max_tokens":
                raise LLMError("model output was truncated (increase llm.max_output_tokens)")
            usage = message.usage
            log.info(
                "LLM %s call (%s) finished; tokens in=%s out=%s cached=%s",
                purpose,
                message.model,
                usage.input_tokens,
                usage.output_tokens,
                getattr(usage, "cache_read_input_tokens", 0),
            )
            text = "".join(block.text for block in message.content if block.type == "text")
            return parse_json_object(text)

        return retry_call(call, self.retry, retry_on=transient, description=f"LLM {purpose}")
