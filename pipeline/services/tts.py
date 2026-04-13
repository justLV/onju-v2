import io
import logging
import os

import httpx
from pydub import AudioSegment

log = logging.getLogger(__name__)


async def synthesize(text: str, voice: str, config: dict) -> bytes:
    """Convert text to 16kHz mono PCM bytes using the configured TTS backend."""
    backend = config["tts"]["backend"]
    if backend == "elevenlabs":
        return await _elevenlabs(text, voice, config)
    if backend == "local":
        return await _local(text, config)
    raise ValueError(f"Unknown TTS backend: {backend}")


async def _elevenlabs(text: str, voice_name: str, config: dict) -> bytes:
    el_cfg = config["tts"]["elevenlabs"]
    api_key = el_cfg["api_key"]
    voice_id = el_cfg["voices"].get(voice_name, el_cfg["voices"].get(el_cfg["default_voice"]))

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {"text": text}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        mp3_bytes = resp.content

    audio = AudioSegment.from_mp3(io.BytesIO(mp3_bytes))
    audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    log.debug(f"TTS: {len(text)} chars -> {len(audio)}ms audio")
    return audio.raw_data


async def _local(text: str, config: dict) -> bytes:
    local_cfg = config["tts"]["local"]
    url = local_cfg["url"].rstrip("/") + "/v1/audio/speech"

    payload = {
        "model": local_cfg["model"],
        "input": text,
        "response_format": "wav",
    }

    # Voice cloning: pass ref_audio path (server reads from disk)
    ref_audio = local_cfg.get("ref_audio")
    if ref_audio:
        payload["ref_audio"] = os.path.abspath(ref_audio)
    ref_text = local_cfg.get("ref_text")
    if ref_text:
        payload["ref_text"] = ref_text

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        wav_bytes = resp.content

    audio = AudioSegment.from_wav(io.BytesIO(wav_bytes))
    audio = audio.set_channels(1).set_frame_rate(16000).set_sample_width(2)
    log.debug(f"TTS local: {len(text)} chars -> {len(audio)}ms audio")
    return audio.raw_data
