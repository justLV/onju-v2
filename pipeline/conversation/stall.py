import asyncio
import logging
import os
import re

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


def _resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


_client: AsyncOpenAI | None = None
_client_key: tuple | None = None


def _get_client(cfg: dict) -> AsyncOpenAI:
    """Lazy singleton so we reuse the HTTP connection pool across turns."""
    global _client, _client_key
    key = (cfg["base_url"], cfg.get("api_key", ""))
    if _client is None or _client_key != key:
        _client = AsyncOpenAI(
            base_url=cfg["base_url"],
            api_key=_resolve_env(cfg.get("api_key", "none")),
        )
        _client_key = key
    return _client


async def decide_stall(
    user_text: str,
    config: dict,
    prev_user: str | None = None,
    prev_assistant: str | None = None,
) -> str | None:
    """Ask a fast classifier model whether the voice assistant should say a
    brief stall phrase before answering. Returns the stall text to speak, or
    None if the query is conversational and needs no stall (or the classifier
    failed/timed out).

    Only runs in agentic mode — conversational backends already respond
    quickly and don't benefit from a stall.

    Pass the previous user/assistant exchange as context so the classifier
    can recognize follow-ups, continuation cues ("go on"), and mid-conversation
    prefaces ("one more thing") as conversational rather than tool-needing."""
    conv_cfg = config.get("conversation", {})
    if conv_cfg.get("backend") != "agentic":
        return None

    cfg = conv_cfg.get("stall")
    if not cfg or not cfg.get("enabled", False):
        return None

    timeout = cfg.get("timeout", 1.5)

    if prev_user or prev_assistant:
        context_block = "Previous turn in this conversation:\n"
        if prev_user:
            context_block += f"  User said: {prev_user}\n"
        if prev_assistant:
            context_block += f"  You replied: {prev_assistant}\n"
    else:
        context_block = "(No previous turn — this is the start of the conversation.)"

    prompt = (
        cfg["prompt"]
        .replace("{user_text}", user_text)
        .replace("{recent_context}", context_block)
    )

    kwargs = dict(
        model=cfg["model"],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=cfg.get("max_tokens", 200),
    )
    # Gemini 2.5 models think by default — disable it for the stall call since
    # we need sub-second latency and the classification is trivial.
    # https://ai.google.dev/gemini-api/docs/openai
    if cfg.get("reasoning_effort"):
        kwargs["reasoning_effort"] = cfg["reasoning_effort"]

    try:
        response = await asyncio.wait_for(
            _get_client(cfg).chat.completions.create(**kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.info(f"STALL timeout after {timeout}s — skipping")
        return None
    except Exception as e:
        log.warning(f"STALL classifier failed: {e}")
        return None

    text = (response.choices[0].message.content or "").strip()
    if not text or text.upper().startswith("NONE"):
        return None
    return text
