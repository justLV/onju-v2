from typing import Protocol, runtime_checkable


@runtime_checkable
class ConversationBackend(Protocol):
    async def send(self, user_text: str) -> str:
        """Send a user message, return the assistant's response."""
        ...

    def reset(self) -> None:
        """Clear conversation history / start a new session."""
        ...

    def get_messages(self) -> list[dict]:
        """Return current message history (for persistence). May be empty for managed backends."""
        ...

    def set_messages(self, messages: list[dict]) -> None:
        """Restore message history (from persistence). No-op for managed backends."""
        ...
