"""Thin wrapper around the Claude API that returns validated pydantic objects."""
import os
import time
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

_client: anthropic.Anthropic | None = None


class LLMError(RuntimeError):
    pass


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _strict(schema: dict) -> dict:
    """Make every object in a JSON schema closed and fully required (structured outputs rule)."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            schema["additionalProperties"] = False
            schema["required"] = list(schema["properties"].keys())
        for v in schema.values():
            if isinstance(v, dict):
                _strict(v)
            elif isinstance(v, list):
                for item in v:
                    _strict(item)
    return schema


# Errors worth waiting out. The SDK already retries these (max_retries=4, short exponential
# backoff); when it still fails we wait longer and try again a couple of times before giving up,
# so a burst of rate limits / "overloaded" doesn't drop a posting.
TRANSIENT = (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)
EXTRA_ATTEMPTS = 2
RETRY_WAIT = (20.0, 60.0)       # seconds before the 1st and 2nd extra attempt
_sleep = time.sleep             # patched in tests


def _user_content(user: str, cached_prefix: str) -> str | list[dict]:
    """The user turn. A stable `cached_prefix` (e.g. the resumes, identical across every call in
    a run) goes first with a cache breakpoint, so later calls read it from the prompt cache
    (system prompt + prefix) instead of paying for it again. Prefixes under the model's minimum
    (512 tokens on claude-opus-5-5) simply don't cache; nothing breaks."""
    if not cached_prefix:
        return user
    return [{"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user}]


def ask_json(
    out_model: Type[T],
    system: str,
    user: str,
    *,
    model: str = "claude-opus-5-5",
    effort: str = "medium",
    max_tokens: int = 16000,
    cached_prefix: str = "",
) -> T:
    """One structured-output call. Raises LLMError on refusal / truncation."""
    schema = _strict(out_model.model_json_schema())
    request = dict(
        model=model,
        max_tokens=max_tokens,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system,
        messages=[{"role": "user", "content": _user_content(user, cached_prefix)}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    for attempt in range(EXTRA_ATTEMPTS + 1):
        try:
            resp = client().beta.messages.create(**request)
            break
        except TRANSIENT:
            if attempt == EXTRA_ATTEMPTS:
                raise
            _sleep(RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)])
    if resp.stop_reason == "refusal":
        raise LLMError("Claude declined this request.")
    if resp.stop_reason == "max_tokens":
        raise LLMError("Claude's answer was cut off (max_tokens).")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMError("Claude returned no text.")
    return out_model.model_validate_json(text)
