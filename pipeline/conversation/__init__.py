from pipeline.conversation.base import ConversationBackend
from pipeline.conversation.local import LocalConversation
from pipeline.conversation.managed import ManagedConversation


def create_backend(config: dict, device_id: str) -> ConversationBackend:
    """Create a conversation backend based on config."""
    conv_cfg = config["conversation"]
    backend = conv_cfg.get("backend", "local")

    if backend == "local":
        return LocalConversation(conv_cfg["local"], device_id)
    elif backend == "managed":
        return ManagedConversation(conv_cfg["managed"], device_id)
    else:
        raise ValueError(f"Unknown conversation backend: {backend}")
