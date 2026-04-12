"""
Quick benchmark of the stall classifier against Gemini to see:
- How long each call actually takes
- Whether the model returns NONE or a stall phrase as expected
- Whether we're blowing past the 1.5s timeout

Run: python test_stall.py
"""
import asyncio
import os
import time

import yaml

from pipeline.conversation import stall as stall_mod


# Each entry is (user_text, prev_user, prev_assistant).
# prev_user / prev_assistant can be None for fresh-start queries.
TEST_QUERIES: list[tuple[str, str | None, str | None]] = [
    # --- No context (fresh start) ---
    # Conversational (should be NONE)
    ("How are you doing today?", None, None),
    ("What's two plus two?", None, None),
    ("What's your favorite color?", None, None),
    ("Tell me a joke.", None, None),
    # Fetch / lookup (should stall)
    ("What's the weather like in Berlin right now?", None, None),
    ("Can you look up the latest news on space launches and summarize it?", None, None),
    ("Find me a good ramen spot nearby.", None, None),
    ("Can you search for what people are saying about the new camera release?", None, None),
    ("What time is it in Tokyo?", None, None),
    # Capture / act / schedule — should NOT stall (stall can't make these promises)
    ("Remember that my next meeting is Tuesday at noon.", None, None),
    ("Add bread and butter to my shopping list.", None, None),
    ("Save this as a note: the door code is one-two-three-four.", None, None),
    ("Remind me to water the plants tomorrow.", None, None),
    ("Schedule a dentist appointment for next Wednesday.", None, None),
    ("Send a message to the team saying I'm running late.", None, None),
    ("Mark the laundry task as done on my todo list.", None, None),
    # Mixed — lookup component should dominate
    ("Can you find flower delivery options near the office?", None, None),

    # --- With context (should recognize follow-ups / prefaces) ---
    # Pure continuations — NONE
    (
        "Go on.",
        "What's the weather in Berlin right now?",
        "Berlin is about fifty degrees, partly cloudy, light wind from the west.",
    ),
    (
        "Um, one more thing.",
        "What's the weather in Berlin right now?",
        "Berlin is about fifty degrees, partly cloudy.",
    ),
    (
        "Tell me more.",
        "What's the latest on space launches?",
        "A private company successfully landed a booster on a floating platform yesterday.",
    ),
    # Shorthand follow-ups that DO need a new lookup — should stall
    (
        "What about Saturday?",
        "What's the weather in Berlin on Friday?",
        "Friday is partly sunny, mid fifties.",
    ),
    (
        "Same but for Tuesday.",
        "What's on my calendar Monday?",
        "Monday is open until 3pm, then a one-hour block.",
    ),
    (
        "What about the other one?",
        "How much is the blue jacket on that store page?",
        "The blue jacket is on sale for sixty nine dollars.",
    ),
    # Preface with a real request mixed in — should follow request
    (
        "By the way, can you also remind me to water the plants?",
        "What's the weather in Berlin right now?",
        "About fifty degrees, partly cloudy.",
    ),
]


async def main():
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "pipeline", "config.yaml",
    )
    with open(cfg_path) as f:
        config = yaml.safe_load(f)

    # Force the timeout off so we see true latency
    original_timeout = config["conversation"]["stall"].get("timeout", 1.5)
    config["conversation"]["stall"]["timeout"] = 30.0

    print(f"model:    {config['conversation']['stall']['model']}")
    print(f"endpoint: {config['conversation']['stall']['base_url']}")
    print(f"(original timeout was {original_timeout}s — disabled for this test)\n")

    for query, prev_user, prev_assistant in TEST_QUERIES:
        t0 = time.monotonic()
        try:
            result = await stall_mod.decide_stall(
                query, config, prev_user=prev_user, prev_assistant=prev_assistant
            )
        except Exception as e:
            result = f"ERROR: {e}"
        elapsed = time.monotonic() - t0
        flag = " TIMEOUT" if elapsed > original_timeout else ""
        ctx = " (has context)" if prev_user else ""
        print(f"[{elapsed:5.2f}s]{flag}  {query!r}{ctx}")
        print(f"          -> {result!r}\n")


if __name__ == "__main__":
    asyncio.run(main())
