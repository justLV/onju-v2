#!/usr/bin/env python3
"""
Test streaming TTS to an ESP32 device.
Generates speech via ElevenLabs, Opus-encodes, and streams over TCP.

Usage:
    python test_streaming_tts.py <ip> [--text "Hello world"]
    python test_streaming_tts.py 192.168.1.50
    python test_streaming_tts.py 192.168.1.50 --text "Testing one two three"
"""
import argparse
import asyncio
import io
import struct
import socket
import time

import opuslib
from pydub import AudioSegment
from elevenlabs import ElevenLabs

from pipeline.main import load_config

SAMPLE_RATE = 16000
OPUS_FRAME_SIZE = 320
DEFAULT_TEXT = "Hello! This is a streaming text to speech test. Notice how the audio starts playing before the full sentence is generated."


def main():
    parser = argparse.ArgumentParser(description="Stream TTS to ESP32")
    parser.add_argument("ip", help="Device IP address")
    parser.add_argument("--port", type=int, default=3001, help="TCP port (default: 3001)")
    parser.add_argument("--volume", type=int, default=14)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--voice", default=None, help="ElevenLabs voice ID (default: from config)")
    args = parser.parse_args()

    config = load_config()
    el_cfg = config["tts"]["elevenlabs"]
    api_key = el_cfg["api_key"]
    voice_id = args.voice or el_cfg["voices"].get(el_cfg.get("default_voice", "Rachel"), "21m00Tcm4TlvDq8ikWAM")

    print(f"Text: {args.text}")
    print(f"Target: {args.ip}:{args.port}")
    print()

    client = ElevenLabs(api_key=api_key)
    encoder = opuslib.Encoder(SAMPLE_RATE, 1, opuslib.APPLICATION_VOIP)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect((args.ip, args.port))

    header = bytes([0xAA, 0x00, 60, args.volume, 5, 2])  # compression=2 (Opus)
    sock.send(header)

    start_time = time.time()
    first_chunk_time = None
    total_pcm = 0
    total_opus = 0
    opus_frames = 0
    pcm_buffer = b""

    audio_stream = client.text_to_speech.convert(
        voice_id=voice_id,
        text=args.text,
        model_id="eleven_monolingual_v1",
        output_format="pcm_16000",
    )

    for pcm_chunk in audio_stream:
        if first_chunk_time is None:
            first_chunk_time = time.time()
            print(f"First audio chunk: {first_chunk_time - start_time:.3f}s")

        total_pcm += len(pcm_chunk)
        pcm_buffer += pcm_chunk

        frame_bytes = OPUS_FRAME_SIZE * 2
        while len(pcm_buffer) >= frame_bytes:
            frame = pcm_buffer[:frame_bytes]
            pcm_buffer = pcm_buffer[frame_bytes:]
            opus_frame = encoder.encode(frame, OPUS_FRAME_SIZE)
            sock.send(struct.pack(">H", len(opus_frame)))
            sock.send(opus_frame)
            total_opus += len(opus_frame)
            opus_frames += 1

    # Flush remaining
    if pcm_buffer:
        frame_bytes = OPUS_FRAME_SIZE * 2
        pcm_buffer += b"\x00" * (frame_bytes - len(pcm_buffer))
        opus_frame = encoder.encode(pcm_buffer[:frame_bytes], OPUS_FRAME_SIZE)
        sock.send(struct.pack(">H", len(opus_frame)))
        sock.send(opus_frame)
        total_opus += len(opus_frame)
        opus_frames += 1

    sock.close()
    end_time = time.time()

    audio_duration = total_pcm / (SAMPLE_RATE * 2)
    ratio = total_pcm / total_opus if total_opus else 0

    print(f"\nResults:")
    print(f"  Pipeline time:    {end_time - start_time:.2f}s")
    print(f"  Time to first audio: {first_chunk_time - start_time:.3f}s")
    print(f"  Audio duration:   {audio_duration:.1f}s")
    print(f"  Opus frames:      {opus_frames}")
    print(f"  Compression:      {ratio:.1f}x ({total_pcm:,} PCM -> {total_opus:,} Opus)")


if __name__ == "__main__":
    main()
