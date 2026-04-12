import re
from typing import AsyncIterator

from pipeline.conversation.base import ConversationBackend
from pipeline.conversation.conversational import ConversationalBackend
from pipeline.conversation.agentic import AgenticBackend

# Primary: punctuation followed by whitespace (safe, standard).
_SENTENCE_END = re.compile(r"[.!?\n]+\s+")
# Fallback: punctuation with no space, but only when preceded by a lowercase
# letter and followed by uppercase.  Catches OpenClaw's chunk-boundary joins
# ("now.The") without breaking abbreviations like "U.S." (uppercase before dot).
_SENTENCE_END_NOSPACE = re.compile(r"(?<=[a-z])[.!?]+(?=[A-Z])")


async def sentence_chunks(deltas: AsyncIterator[str]) -> AsyncIterator[str]:
    """Buffer text deltas and yield one sentence at a time, plus any trailing
    fragment when the stream ends."""
    buffer = ""
    async for delta in deltas:
        buffer += delta
        while True:
            m = _SENTENCE_END.search(buffer)
            if not m:
                m = _SENTENCE_END_NOSPACE.search(buffer)
            if not m:
                break
            sentence = buffer[: m.end()].strip()
            buffer = buffer[m.end():]
            if sentence:
                yield sentence
    tail = buffer.strip()
    if tail:
        yield tail


def create_backend(config: dict, device_id: str) -> ConversationBackend:
    """Create a conversation backend based on config."""
    conv_cfg = config["conversation"]
    backend = conv_cfg.get("backend", "conversational")

    if backend == "conversational":
        return ConversationalBackend(conv_cfg["conversational"], device_id)
    elif backend == "agentic":
        return AgenticBackend(conv_cfg["agentic"], device_id)
    else:
        raise ValueError(f"Unknown conversation backend: {backend}")
