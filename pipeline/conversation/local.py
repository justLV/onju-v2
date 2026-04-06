import logging
import os
import re

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


def _resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


class LocalConversation:
    """Manages conversation history locally and sends full context to any OpenAI-compatible endpoint."""

    def __init__(self, cfg: dict, device_id: str):
        self.cfg = cfg
        self.device_id = device_id
        self.client = AsyncOpenAI(
            base_url=cfg["base_url"],
            api_key=_resolve_env(cfg.get("api_key", "none")),
        )
        self.messages: list[dict] = [{"role": "system", "content": cfg["system_prompt"]}]
        self.max_messages = cfg.get("max_messages", 20)

    async def send(self, user_text: str) -> str:
        self._sanitize()
        self.messages.append({"role": "user", "content": user_text})

        kwargs = dict(
            model=self.cfg["model"],
            messages=self.messages,
            max_tokens=self.cfg.get("max_tokens", 300),
        )
        if self.cfg.get("thinking_budget") is not None:
            kwargs["extra_body"] = {
                "google": {"thinking_config": {"thinking_budget": self.cfg["thinking_budget"]}}
            }

        response = await self.client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        self.messages.append({"role": "assistant", "content": text})
        self._prune()

        log.debug(f"[{self.device_id}] LLM: {text}")
        return text

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.cfg["system_prompt"]}]

    def get_messages(self) -> list[dict]:
        return self.messages

    def set_messages(self, messages: list[dict]) -> None:
        self.messages = messages
        self._sanitize()

    def _prune(self):
        while len(self.messages) > self.max_messages:
            self.messages.pop(1)

    def _sanitize(self):
        """Ensure messages alternate user/assistant after the system prompt."""
        cleaned = [self.messages[0]] if self.messages and self.messages[0]["role"] == "system" else []
        expected = "user"
        start = 1 if cleaned else 0
        for msg in self.messages[start:]:
            if msg["role"] == "system":
                continue
            if msg["role"] == expected:
                cleaned.append(msg)
                expected = "assistant" if expected == "user" else "user"
        if len(cleaned) > 1 and cleaned[-1]["role"] == "user":
            cleaned.pop()
        self.messages = cleaned
