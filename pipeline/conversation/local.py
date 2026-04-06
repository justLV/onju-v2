import json
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
        api_key = _resolve_env(cfg.get("api_key", "none"))
        if api_key.startswith("${"):
            log.warning(f"LLM api_key env var not resolved: {api_key} — is it exported?")
        self.client = AsyncOpenAI(
            base_url=cfg["base_url"],
            api_key=api_key,
        )
        self.max_messages = cfg.get("max_messages", 20)

        self.persist_path = None
        if persist_dir := cfg.get("persist_dir"):
            os.makedirs(persist_dir, exist_ok=True)
            self.persist_path = os.path.join(persist_dir, f"{device_id}.json")

        self.messages: list[dict] = self._load() or [{"role": "system", "content": cfg["system_prompt"]}]

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
        self.save()

        log.debug(f"[{self.device_id}] LLM: {text}")
        return text

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.cfg["system_prompt"]}]
        self.save()

    def get_messages(self) -> list[dict]:
        return self.messages

    def set_messages(self, messages: list[dict]) -> None:
        self.messages = messages
        self._sanitize()

    def save(self):
        if not self.persist_path:
            return
        with open(self.persist_path, "w") as f:
            json.dump(self.messages, f, indent=2)

    def _load(self) -> list[dict] | None:
        if not self.persist_path or not os.path.exists(self.persist_path):
            return None
        try:
            with open(self.persist_path) as f:
                messages = json.load(f)
            log.info(f"[{self.device_id}] loaded {len(messages)-1} messages from {self.persist_path}")
            return messages
        except Exception as e:
            log.warning(f"[{self.device_id}] failed to load {self.persist_path}: {e}")
            return None

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
