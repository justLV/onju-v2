import logging

import httpx
import numpy as np

from pipeline.audio import pcm_to_wav

log = logging.getLogger(__name__)


async def transcribe(pcm_int16_bytes: bytes, config: dict) -> dict:
    """Send PCM audio to the ASR service and return {"text": ..., "no_speech_prob": ...}."""
    wav_bytes = pcm_to_wav(
        np.frombuffer(pcm_int16_bytes, dtype=np.int16),
        rate=config["audio"]["sample_rate"],
    )
    url = config["asr"]["url"].rstrip("/") + "/transcribe"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
        )
        resp.raise_for_status()
        data = resp.json()

    log.debug(f"ASR: \"{data['text']}\" ({data.get('transcribe_time_s', '?')}s, nsp={data.get('no_speech_prob', '?')})")
    return data
