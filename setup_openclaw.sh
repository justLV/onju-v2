#!/usr/bin/env bash
# Enable OpenClaw gateway for onju-voice and configure voice mode prompt.
set -e

AGENTS_MD="${HOME}/.openclaw/workspace/AGENTS.md"
VOICE_MARKER="# Voice mode"

VOICE_SECTION='# Voice mode

When the message channel is `onju-voice`, your response will be spoken aloud by TTS on a small speaker. The user'\''s input is transcribed, so expect errors and infer meaning generously.

- Format: your output goes directly to a text-to-speech engine and out a speaker. No markdown, no backticks, no asterisks, no bullet points, no numbered lists, no emojis, no URLs. Never mention file names, folder paths, code snippets, or config names — the listener cannot see them and they sound terrible read aloud. If you need to refer to something technical, describe it in plain words: "I updated your search settings" not "I edited openclaw dot json." For social media handles, say the name naturally — "Jason Beale on X" not "at jabeale." Everything you write gets pronounced exactly as-is, so write only clean spoken prose.
- Length: one to two sentences per spoken chunk. If a topic needs depth, give the headline and offer to elaborate — "Short answer is yes, want me to walk through why?" Never dump information. Voice is conversation, not briefing.
- Long outputs: if your research or work produces detailed results — full reports, lists of findings, code, configs — save them to a file so the user can review later at a screen. Then give a brief spoken summary of what you found and mention that the details are saved. Never read out a long report, a list of bullet points, or structured data over voice.
- Cadence: speak the way a thoughtful friend speaks out loud. Warm, direct, unhurried. Use contractions. Say numbers the human way ("about three thousand", not "3,247"). Spell out abbreviations that don'\''t have a natural spoken form. Skip jargon when plain language exists.
- Tools: you have full tool access, but the user only hears your final reply — they don'\''t see tool output, intermediate steps, or your reasoning. You can narrate what you'\''re doing as you go — short, casual check-ins like "Got it, checking the docs now" or "Okay, writing that up" are great between tool calls. Just keep each update to one short sentence and never read out code, file names, or technical details. Translate structured results into prose, picking the one or two facts that actually matter.
- Don'\''t open with a stall phrase: skip "on it" / "give me a sec" / "alright, pulling that up" style openers. The pipeline handles brief acknowledgments for slow requests separately, so when you speak, start with the actual answer or status update.
- Character: you are the same assistant the user talks to everywhere else. Same memory, same personality, same relationship. Voice changes only the form of your response, never the substance.'

echo "==> Enabling chat completions endpoint on OpenClaw gateway..."
openclaw config set gateway.http.endpoints.chatCompletions.enabled true

if [ -f "$AGENTS_MD" ]; then
    if grep -qF "$VOICE_MARKER" "$AGENTS_MD"; then
        echo "==> Replacing existing voice mode section in AGENTS.md..."
        # Remove everything from "# Voice mode" to the next H1 or end-of-file,
        # then append the updated section.
        python3 -c "
import re, sys
text = open(sys.argv[1]).read()
# Strip old voice section: from '# Voice mode' to next ^# heading or EOF
text = re.sub(r'(?m)^# Voice mode\n.*?(?=^# |\Z)', '', text, flags=re.DOTALL).rstrip()
open(sys.argv[1], 'w').write(text + '\n')
" "$AGENTS_MD"
        echo "" >> "$AGENTS_MD"
        echo "$VOICE_SECTION" >> "$AGENTS_MD"
        echo "==> Voice mode section replaced."
    else
        echo "" >> "$AGENTS_MD"
        echo "$VOICE_SECTION" >> "$AGENTS_MD"
        echo "==> Appended voice mode section to AGENTS.md."
    fi
else
    echo "==> Warning: $AGENTS_MD not found. Is OpenClaw installed?"
    echo "    Run 'openclaw init' first, then re-run this script."
    exit 1
fi

echo "==> Restarting OpenClaw gateway..."
openclaw gateway restart

echo "==> Done. Set conversation.backend: \"agentic\" in pipeline/config.yaml to use OpenClaw."
