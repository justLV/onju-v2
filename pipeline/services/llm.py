import logging
import os
import re

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


def _resolve_env(value: str) -> str:
    """Expand ${VAR} references in a string to environment variables."""
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def make_client(config: dict) -> AsyncOpenAI:
    cfg = config["llm"]
    return AsyncOpenAI(
        base_url=cfg["base_url"],
        api_key=_resolve_env(cfg.get("api_key", "none")),
    )


async def chat(client: AsyncOpenAI, device, config: dict) -> str:
    """Send conversation to LLM, append assistant reply to device.messages, return text."""
    cfg = config["llm"]

    kwargs = dict(
        model=cfg["model"],
        messages=device.messages,
        max_tokens=cfg.get("max_tokens", 300),
    )
    if cfg.get("thinking_budget") is not None:
        kwargs["extra_body"] = {
            "google": {"thinking_config": {"thinking_budget": cfg["thinking_budget"]}}
        }
    response = await client.chat.completions.create(**kwargs)

    text = response.choices[0].message.content or ""
    device.messages.append({"role": "assistant", "content": text})
    log.debug(f"LLM raw: {text}")
    return text
