#!/bin/bash
# Run the onju-voice pipeline server.
#
# Usage:
#   ./run.sh                        # default config
#   ./run.sh --warmup               # warmup LLM+TTS on startup
#   ./run.sh --device onju=10.0.0.5 # pre-register a device

cd "$(dirname "$0")"

# opuslib uses ctypes.util.find_library('opus'), which on macOS does not search
# Homebrew prefixes. Point the dynamic loader at the brew opus lib if present.
if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    if opus_prefix="$(brew --prefix opus 2>/dev/null)" && [ -d "$opus_prefix/lib" ]; then
        export DYLD_FALLBACK_LIBRARY_PATH="$opus_prefix/lib:${DYLD_FALLBACK_LIBRARY_PATH:-/usr/local/lib:/usr/lib}"
    else
        echo "Warning: 'opus' not installed via Homebrew. Run: brew install opus"
    fi
fi

echo "Starting onju-voice pipeline..."
uv run python -m pipeline.main "$@"
