"""
Validates the SSE streaming path against a locally running OpenClaw gateway.
No ESP32 / Onju device required.

What it checks:
1. The agentic backend's stream() actually yields content deltas progressively.
2. The sentence_chunks() splitter emits whole sentences as they form.
3. An "ack-first" prompt produces a short opening sentence that lands well
   before the full response, which is the behavior the pipeline relies on.
4. Raw chunk inspection — logs every SSE event (tool_calls, finish_reason,
   inter-chunk gaps) so you can see exactly what OpenClaw sends mid-turn.

Usage:
    python test_stream.py                            # default tool-using prompt
    python test_stream.py "your prompt here"
    python test_stream.py --raw-only "prompt"        # skip sentence pass, only dump chunks

Reads pipeline/config.yaml for the OpenClaw base_url and api_key. Forces the
conversation backend to "agentic" regardless of what config.yaml has set.
"""
import argparse
import asyncio
import os
import re
import time

import yaml

from openai import AsyncOpenAI
from pipeline.conversation import create_backend, sentence_chunks


def _resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


DEFAULT_PROMPT = (
    "I'm going to need you to search the web for the latest news about the "
    "James Webb Space Telescope and then tell me the single most interesting "
    "finding in one sentence."
)

GAP_THRESHOLD = 1.0  # seconds — flag pauses longer than this


async def raw_chunk_inspection(client: AsyncOpenAI, kwargs: dict) -> None:
    """Hit the OpenClaw SSE endpoint directly and log every chunk's shape —
    content deltas, tool_call deltas, finish_reason, and inter-chunk gaps."""
    print("--- raw chunk inspection ---")
    print("(columns: elapsed | gap | type | detail)\n")

    kwargs = {**kwargs, "stream": True}
    stream = await client.chat.completions.create(**kwargs)

    t0 = time.monotonic()
    prev = t0
    chunk_i = 0
    content_chars = 0
    tool_call_events = 0

    async for chunk in stream:
        now = time.monotonic()
        elapsed = now - t0
        gap = now - prev
        prev = now

        gap_flag = " <<<" if gap > GAP_THRESHOLD else ""

        if not chunk.choices:
            print(f"[{elapsed:6.2f}s] +{gap:5.2f}s  empty-choices  (id={chunk.id}){gap_flag}")
            continue

        choice = chunk.choices[0]
        delta = choice.delta
        finish = choice.finish_reason

        parts: list[str] = []

        if delta.role:
            parts.append(f"role={delta.role}")

        if delta.content:
            content_chars += len(delta.content)
            text = delta.content.replace("\n", "\\n")
            parts.append(f"[{len(delta.content)}] {text}")

        if getattr(delta, "tool_calls", None):
            tool_call_events += 1
            for tc in delta.tool_calls:
                fn_name = tc.function.name if tc.function and tc.function.name else ""
                fn_args = (tc.function.arguments or "")[:80] if tc.function else ""
                tc_id = tc.id or ""
                parts.append(f"tool_call(idx={tc.index} id={tc_id} fn={fn_name} args={fn_args!r})")

        if finish:
            parts.append(f"finish_reason={finish}")

        label = " | ".join(parts) if parts else "(empty delta)"
        print(f"[{elapsed:6.2f}s] +{gap:5.2f}s  {label}{gap_flag}")
        chunk_i += 1

    total = time.monotonic() - t0
    print(f"\n[{total:6.2f}s] stream closed — "
          f"{chunk_i} chunks, {content_chars} content chars, "
          f"{tool_call_events} tool_call events\n")
    return tool_call_events


async def sentence_pass(backend, prompt: str) -> None:
    """Run the prompt through the high-level stream → sentence_chunks path."""
    print("--- sentence chunks ---")
    t0 = time.monotonic()
    first_sentence_at: float | None = None
    sentences: list[str] = []
    async for sentence in sentence_chunks(backend.stream(prompt)):
        now = time.monotonic() - t0
        if first_sentence_at is None:
            first_sentence_at = now
        tag = "ACK " if len(sentences) == 0 else "    "
        print(f"[{now:6.2f}s] {tag}{sentence}")
        sentences.append(sentence)
    total = time.monotonic() - t0
    print(f"[{total:6.2f}s] done ({len(sentences)} sentences)\n")

    print("--- summary ---")
    if first_sentence_at is not None:
        print(f"first sentence flush: {first_sentence_at:.2f}s")
    print(f"full response       : {total:.2f}s")
    if first_sentence_at is not None and total > 0:
        head_ratio = first_sentence_at / total
        print(f"ack lead ratio      : {head_ratio:.0%} "
              f"(lower = more headroom for the ack to play while tools run)")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test OpenClaw SSE streaming")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument("--raw-only", action="store_true",
                        help="Only run the raw chunk inspection, skip sentence pass")
    args = parser.parse_args()

    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "pipeline", "config.yaml",
    )
    with open(cfg_path) as f:
        config = yaml.safe_load(f)
    config["conversation"]["backend"] = "agentic"
    mcfg = config["conversation"]["agentic"]

    voice_prompt = mcfg.get("voice_prompt")

    print(f"endpoint: {mcfg['base_url']}")
    print(f"model   : {mcfg.get('model', 'openclaw/default')}")
    print(f"channel : {mcfg.get('message_channel', 'onju-voice')}")
    if voice_prompt:
        print(f"voice   : {voice_prompt[:80]}...")
    print(f"prompt  : {args.prompt}\n")

    # Build a raw OpenAI client for chunk inspection.
    content = f"{voice_prompt}\n\n{args.prompt}" if voice_prompt else args.prompt
    client = AsyncOpenAI(
        base_url=mcfg["base_url"],
        api_key=_resolve_env(mcfg.get("api_key", "none")),
        default_headers={"x-openclaw-message-channel": mcfg.get("message_channel", "onju-voice")},
    )
    raw_kwargs = dict(
        model=mcfg.get("model", "openclaw/default"),
        messages=[{"role": "user", "content": content}],
        max_tokens=mcfg.get("max_tokens", 800),
        user="test-stream",
    )
    if mcfg.get("provider_model"):
        raw_kwargs["extra_headers"] = {"x-openclaw-model": mcfg["provider_model"]}

    tool_events = await raw_chunk_inspection(client, raw_kwargs)

    if tool_events:
        print(f"** {tool_events} tool_call events detected — OpenClaw surfaces "
              f"tool deltas on this endpoint. This means we can use tool_call "
              f"events as a flush trigger for the sentence buffer.\n")
    else:
        print("** No tool_call events seen. Either the prompt didn't trigger "
              "tools or OpenClaw hides them on this endpoint.\n")

    if not args.raw_only:
        backend = create_backend(config, device_id="test-stream")
        await sentence_pass(backend, args.prompt)


if __name__ == "__main__":
    asyncio.run(main())
