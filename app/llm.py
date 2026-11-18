"""Thin wrapper around the Claude API that returns validated pydantic objects."""
import os
import time
from typing import Any, Callable, NamedTuple, Type, TypeVar

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


def _record(purpose: str, resp, seconds: float) -> None:
    """Log the call's token usage for the dashboard's Claude Usage page (never fails the call)."""
    try:
        from . import runs
        runs.record_call(purpose, getattr(resp, "model", ""), getattr(resp, "usage", None), seconds)
    except Exception as e:
        print(f"[warn] couldn't record Claude usage: {e}")


def _create(request: dict, purpose: str):
    """One API request with the long waits for transient errors; records its usage under `purpose`."""
    for attempt in range(EXTRA_ATTEMPTS + 1):
        try:
            started = time.time()
            resp = client().beta.messages.create(**request)
            _record(purpose, resp, time.time() - started)
            return resp
        except TRANSIENT:
            if attempt == EXTRA_ATTEMPTS:
                raise
            _sleep(RETRY_WAIT[min(attempt, len(RETRY_WAIT) - 1)])


class ToolReply(NamedTuple):
    """What a tool handler returns to `run_tools`. `done=True` ends the loop and returns `value`."""
    text: str
    is_error: bool = False
    done: bool = False
    value: Any = None


def run_tools(
    system: str,
    user: str,
    tools: list[dict],
    handler: Callable[[str, dict, int], ToolReply],
    *,
    purpose: str,
    model: str = "claude-opus-5-5",
    effort: str = "high",
    max_turns: int = 8,
    max_tokens: int = 16000,
    cached_prefix: str = "",
    nudge: str = "Please continue by calling one of your tools.",
) -> Any:
    """A small tool-use loop (a sub-agent). Each API request is recorded under `purpose`.

    `handler(name, input, turns_left)` runs one tool call. The loop ends when a handler returns
    ToolReply(done=True), and that reply's `value` is returned. Raises LLMError on refusal,
    truncation or when `max_turns` requests pass without finishing, so callers can fall back.

    Caching: the stable `cached_prefix` keeps its own breakpoint (shared with other calls in a
    run), and top-level automatic caching covers the growing conversation between turns.
    """
    tools = [{**t, "strict": True} for t in tools]
    messages: list[dict] = [{"role": "user", "content": _user_content(user, cached_prefix)}]
    for turn in range(max_turns):
        left = max_turns - turn - 1
        resp = _create(dict(
            model=model,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": effort},
            cache_control={"type": "ephemeral"},
        ), purpose)
        if resp.stop_reason == "refusal":
            raise LLMError("Claude declined this request.")
        if resp.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens).")
        # append the content unchanged (thinking blocks included) so the history stays append-only
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            messages.append({"role": "user", "content": nudge})
            continue
        results, finished = [], None
        for b in uses:
            try:
                reply = handler(b.name, dict(b.input or {}), left)
            except Exception as e:  # a broken tool call shouldn't end the loop
                reply = ToolReply(f"Error: {e}", is_error=True)
            if reply.done and finished is None:
                finished = reply
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": reply.text,
                            "is_error": reply.is_error})
        if finished is not None:
            return finished.value
        if left <= 2:
            results.append({"type": "text", "text": f"({left} turn{'s' if left != 1 else ''} left: "
                                                     "submit your plan before you run out.)"})
        messages.append({"role": "user", "content": results})
    raise LLMError(f"The tool loop didn't finish within {max_turns} turns.")


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
    resp = _create(request, out_model.__name__)
    if resp.stop_reason == "refusal":
        raise LLMError("Claude declined this request.")
    if resp.stop_reason == "max_tokens":
        raise LLMError("Claude's answer was cut off (max_tokens).")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if not text:
        raise LLMError("Claude returned no text.")
    return out_model.model_validate_json(text)
