import logging
import os
import re

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


def _resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


class ManagedConversation:
    """Delegates conversation memory to a remote service (OpenClaw, etc).

    Only sends the latest user message — the remote service tracks session history
    via the session key derived from the device ID.
    """

    def __init__(self, cfg: dict, device_id: str):
        self.cfg = cfg
        self.device_id = device_id
        session_prefix = cfg.get("session_prefix", "onju-")
        self.session_key = f"{session_prefix}{device_id}"
        self.client = AsyncOpenAI(
            base_url=cfg["base_url"],
            api_key=_resolve_env(cfg.get("api_key", "none")),
            default_headers={
                "x-openclaw-session-key": self.session_key,
            },
        )

    async def send(self, user_text: str) -> str:
        kwargs = dict(
            model=self.cfg.get("model", "openclaw/default"),
            messages=[{"role": "user", "content": user_text}],
            max_tokens=self.cfg.get("max_tokens", 300),
        )

        extra_headers = {}
        if self.cfg.get("provider_model"):
            extra_headers["x-openclaw-model"] = self.cfg["provider_model"]
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        response = await self.client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
        log.debug(f"[{self.device_id}] managed LLM: {text}")
        return text

    def reset(self) -> None:
        pass  # session reset would require an API call if supported

    def get_messages(self) -> list[dict]:
        return []  # history lives on the remote service

    def set_messages(self, messages: list[dict]) -> None:
        pass  # no-op — remote service owns the history
