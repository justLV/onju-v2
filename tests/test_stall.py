"""
Run the stall classifier over a set of labeled utterances and print the
results grouped by expected category, for manual review.

Each case carries a label (NONE / LOOKUP / ACTION) describing what we
expect the classifier to do, but there's no automated pass/fail — you
read the outputs yourself. Latency is printed per case and summarized at
the end against the production timeout.

Run: python tests/test_stall.py
"""
import asyncio
import os
import time

import yaml

from pipeline.conversation import stall as stall_mod


# (label, user_text, prev_user, prev_assistant)
# label describes what kind of stall (if any) we'd expect for this turn.
# prev_user / prev_assistant are None for fresh-start queries.
TEST_QUERIES: list[tuple[str, str, str | None, str | None]] = [
    # --- Conversational → NONE ---
    ("NONE", "How are you doing today?", None, None),
    ("NONE", "What's two plus two?", None, None),
    ("NONE", "What's your favorite color?", None, None),
    ("NONE", "Tell me a joke.", None, None),
    ("NONE", "Why is the sky blue?", None, None),

    # --- Retrieval → LOOKUP stall ---
    ("LOOKUP", "What's the weather like in Berlin right now?", None, None),
    ("LOOKUP", "Can you look up the latest news on space launches and summarize it?", None, None),
    ("LOOKUP", "Find me a good ramen spot nearby.", None, None),
    ("LOOKUP", "Can you search for what people are saying about the new camera release?", None, None),
    ("LOOKUP", "What's the current Bitcoin price?", None, None),
    ("LOOKUP", "Can you find flower delivery options near the office?", None, None),

    # --- Action / capture → ACTION stall (listener sound, no commitment) ---
    ("ACTION", "Remember that my next meeting is Tuesday at noon.", None, None),
    ("ACTION", "Add bread and butter to my shopping list.", None, None),
    ("ACTION", "Save this as a note: the door code is one-two-three-four.", None, None),
    ("ACTION", "Remind me to water the plants tomorrow.", None, None),
    ("ACTION", "Schedule a dentist appointment for next Wednesday.", None, None),
    ("ACTION", "Send a message to the team saying I'm running late.", None, None),
    ("ACTION", "Mark the laundry task as done on my todo list.", None, None),

    # The briefing case that originally slipped through as NONE (action + lookup)
    (
        "ACTION",
        "Can you set up a briefing tomorrow for 9 a.m.? Actually, make that every "
        "day. And the briefing should search X for any AI news you think I'd find "
        "interesting.",
        None,
        None,
    ),

    # --- Context-aware: continuations → NONE ---
    (
        "NONE", "Go on.",
        "What's the weather in Berlin right now?",
        "Berlin is about fifty degrees, partly cloudy, light wind from the west.",
    ),
    (
        "NONE", "Um, one more thing.",
        "What's the weather in Berlin right now?",
        "Berlin is about fifty degrees, partly cloudy.",
    ),
    (
        "NONE", "Tell me more.",
        "What's the latest on space launches?",
        "A private company successfully landed a booster on a floating platform yesterday.",
    ),

    # --- Context-aware: parameter-shift follow-ups → LOOKUP ---
    (
        "LOOKUP", "What about Saturday?",
        "What's the weather in Berlin on Friday?",
        "Friday is partly sunny, mid fifties.",
    ),
    (
        "LOOKUP", "And on Netflix?",
        "What's new on Hulu this week?",
        "A couple of new episodes of The Bear and a reality show premiere.",
    ),
    (
        "LOOKUP", "What about the other one?",
        "How much is the blue jacket on that store page?",
        "The blue jacket is on sale for sixty nine dollars.",
    ),

    # --- Context-aware: preface + action request → ACTION ---
    (
        "ACTION", "By the way, can you also remind me to water the plants?",
        "What's the weather in Berlin right now?",
        "About fifty degrees, partly cloudy.",
    ),
]


GREY = "\033[90m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
BOLD = "\033[1m"
RESET = "\033[0m"


async def run_one(query, prev_user, prev_assistant, config):
    t0 = time.monotonic()
    try:
        result = await stall_mod.decide_stall(
            query, config, prev_user=prev_user, prev_assistant=prev_assistant
        )
        err = None
    except Exception as e:
        result = None
        err = str(e)
    return time.monotonic() - t0, result, err


async def main():
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "pipeline", "config.yaml",
    )
    with open(cfg_path) as f:
        config = yaml.safe_load(f)

    original_timeout = config["conversation"]["stall"].get("timeout", 1.5)
    config["conversation"]["stall"]["timeout"] = 30.0
    # Force agentic mode so decide_stall runs even if config is set to conversational.
    config["conversation"]["backend"] = "agentic"

    print(f"{BOLD}model:{RESET}    {config['conversation']['stall']['model']}")
    print(f"{BOLD}endpoint:{RESET} {config['conversation']['stall']['base_url']}")
    print(f"{GREY}(production timeout is {original_timeout}s — disabled here){RESET}\n")

    results: dict[str, list[tuple[float, str, str | None, str | None, str | None]]] = {
        "NONE": [], "LOOKUP": [], "ACTION": [],
    }
    slow = 0
    errors = 0

    for label, query, prev_user, prev_assistant in TEST_QUERIES:
        elapsed, result, err = await run_one(query, prev_user, prev_assistant, config)
        if err:
            errors += 1
        if elapsed > original_timeout:
            slow += 1
        results[label].append((elapsed, query, prev_user, result, err))

    for label in ("NONE", "LOOKUP", "ACTION"):
        print(f"{BOLD}{CYAN}── {label} ──{RESET}")
        for elapsed, query, prev_user, result, err in results[label]:
            slow_tag = f" {YELLOW}SLOW{RESET}" if elapsed > original_timeout else ""
            ctx = f" {GREY}(w/ context){RESET}" if prev_user else ""
            display = err if err else repr(result)
            print(f"  [{elapsed:4.2f}s]{slow_tag}  {query}{ctx}")
            print(f"           → {display}")
        print()

    total = sum(len(v) for v in results.values())
    print(f"{GREY}{total} cases  |  {slow} over {original_timeout}s  |  {errors} errors{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
