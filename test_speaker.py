#!/usr/bin/env python3
"""
Send an audio file to an ESP32 device via Opus over TCP.

Usage:
    python test_speaker.py <ip> [audio_file]
    python test_speaker.py 192.168.1.50 data/hello_imhere.wav
    python test_speaker.py 192.168.1.50 recording.wav --volume 10
"""
import argparse
import asyncio
import time

from pydub import AudioSegment

from pipeline.audio import opus_encode, opus_frames_to_tcp_payload
from pipeline.protocol import send_audio

SAMPLE_RATE = 16000
OPUS_FRAME_SIZE = 320


async def main():
    parser = argparse.ArgumentParser(description="Send audio file to ESP32")
    parser.add_argument("ip", help="Device IP address")
    parser.add_argument("file", nargs="?", default="data/hello_imhere.wav", help="Audio file to send")
    parser.add_argument("--port", type=int, default=3001, help="TCP port (default: 3001)")
    parser.add_argument("--volume", type=int, default=8, help="Playback volume (default: 8)")
    parser.add_argument("--mic-timeout", type=int, default=60, help="Mic timeout in seconds (default: 60)")
    args = parser.parse_args()

    print(f"Loading {args.file}...")
    audio = AudioSegment.from_file(args.file).set_channels(1).set_frame_rate(SAMPLE_RATE).set_sample_width(2)
    pcm_data = audio.raw_data
    duration = len(pcm_data) / (SAMPLE_RATE * 2)
    print(f"  {duration:.1f}s, {len(pcm_data):,} bytes PCM")

    print(f"Encoding Opus...")
    t0 = time.time()
    frames = opus_encode(pcm_data, SAMPLE_RATE, OPUS_FRAME_SIZE)
    payload = opus_frames_to_tcp_payload(frames)
    ratio = len(pcm_data) / len(payload)
    print(f"  {len(frames)} frames, {len(payload):,} bytes ({ratio:.1f}x compression, {time.time()-t0:.2f}s)")

    print(f"Sending to {args.ip}:{args.port}...")
    t0 = time.time()
    await send_audio(args.ip, args.port, payload,
                     mic_timeout=args.mic_timeout, volume=args.volume, fade=5)
    print(f"  Sent in {time.time()-t0:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
