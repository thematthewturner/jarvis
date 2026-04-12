import asyncio
import json
import logging
from functools import lru_cache

from config import (
    ANTHROPIC_API_KEY,
    CLAUDE_MODEL_CHAT,
    CLAUDE_MODEL_EXTRACTION,
    CLAUDE_MODEL_REPORT,
)

logger = logging.getLogger("jarvis.llm")

MODEL_ALIASES = {
    "chat": CLAUDE_MODEL_CHAT,
    "extract": CLAUDE_MODEL_EXTRACTION,
    "report": CLAUDE_MODEL_REPORT,
}


def _resolve_model(model: str | None) -> str:
    return MODEL_ALIASES.get(model or "", model or CLAUDE_MODEL_CHAT)


@lru_cache(maxsize=1)
def get_client():
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError("anthropic package is not installed") from exc
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def strip_markdown_fences(text: str) -> str:
    raw = (text or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```", 2)
        if len(parts) >= 2:
            raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def response_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def create_message_sync(
    *,
    model: str | None = None,
    max_tokens: int,
    messages: list[dict],
    system: str | None = None,
    tools: list[dict] | None = None,
):
    kwargs = {
        "model": _resolve_model(model),
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools
    return get_client().messages.create(**kwargs)


async def create_message(
    *,
    model: str | None = None,
    max_tokens: int,
    messages: list[dict],
    system: str | None = None,
    tools: list[dict] | None = None,
    timeout_seconds: int | float | None = None,
):
    kwargs = {
        "model": _resolve_model(model),
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if tools is not None:
        kwargs["tools"] = tools

    call = asyncio.to_thread(get_client().messages.create, **kwargs)
    if timeout_seconds:
        return await asyncio.wait_for(call, timeout=timeout_seconds)
    return await call


def complete_text_sync(
    *,
    model: str | None = None,
    max_tokens: int,
    user_content,
    system: str | None = None,
) -> str:
    response = create_message_sync(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return response_text(response)


async def complete_text(
    *,
    model: str | None = None,
    max_tokens: int,
    user_content,
    system: str | None = None,
    timeout_seconds: int | float | None = None,
) -> str:
    response = await create_message(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
        timeout_seconds=timeout_seconds,
    )
    return response_text(response)


def complete_json_sync(
    *,
    model: str | None = None,
    max_tokens: int,
    user_content,
    system: str | None = None,
):
    raw = complete_text_sync(
        model=model,
        max_tokens=max_tokens,
        user_content=user_content,
        system=system,
    )
    cleaned = strip_markdown_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON from Claude: %s", cleaned[:500])
        raise ValueError("Claude returned invalid JSON") from exc


async def complete_json(
    *,
    model: str | None = None,
    max_tokens: int,
    user_content,
    system: str | None = None,
    timeout_seconds: int | float | None = None,
):
    raw = await complete_text(
        model=model,
        max_tokens=max_tokens,
        user_content=user_content,
        system=system,
        timeout_seconds=timeout_seconds,
    )
    cleaned = strip_markdown_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Invalid JSON from Claude: %s", cleaned[:500])
        raise ValueError("Claude returned invalid JSON") from exc
