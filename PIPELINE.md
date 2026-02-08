# Pipeline Server

Async voice pipeline that connects ESP32 onju-voice devices to ASR, LLM, and TTS services.

```
ESP32 (mic) ──UDP/μ-law──▶ Pipeline ──HTTP──▶ ASR Service
                              │
                              ├──▶ LLM (OpenAI-compatible)
                              │
                              ├──▶ TTS (ElevenLabs)
                              │
ESP32 (speaker) ◀──TCP/Opus──┘
```

## Prerequisites

**ASR Service** — [parakeet-asr-server](https://github.com/justLV/parakeet-asr-server) running on port 8100.

**LLM** — Any OpenAI-compatible server. Examples:
```bash
# Local (mlx_lm)
mlx_lm.server --model mlx-community/gemma-3-4b-it-qat-4bit --port 8080

# Local (Ollama)
ollama serve  # default port 11434

# Hosted — just set base_url and api_key in config.yaml
```

**TTS** — ElevenLabs API key (add to `config.yaml`).

## Setup

```bash
# From repo root
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt

# macOS: install system libraries
brew install opus portaudio
```

## Configuration

```bash
cp pipeline/config.yaml.example pipeline/config.yaml
# Edit config.yaml with your API keys and preferences
```

## Running

Ensure the prerequisite services are running, then start the pipeline from the repo root:

```bash
source .venv/bin/activate
python -m pipeline.main
```

## Test Client

A Python script that emulates an ESP32 device (TCP server, Opus decoding, mic streaming):

```bash
# From repo root
python test_client.py                  # localhost
python test_client.py 192.168.1.50     # remote server
python test_client.py --no-mic         # playback only
```

## Config Reference

| Section | Key | Description |
|---------|-----|-------------|
| `asr.url` | ASR service endpoint | Default: `http://localhost:8100` |
| `llm.base_url` | OpenAI-compatible API base | Ollama, mlx_lm, OpenRouter, OpenAI |
| `llm.model` | Model name | Passed to chat completions API |
| `tts.backend` | TTS provider | Currently: `elevenlabs` |
| `vad.*` | Voice activity detection | Tune thresholds for sensitivity |
| `network.*` | Ports | UDP 3000 (mic), TCP 3001 (speaker), multicast 239.0.0.1:12345 |
