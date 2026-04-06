#!/usr/bin/env python3
"""
Greet an ESP32 (enables mic) then record its audio via UDP.

Usage:
    python record_from_esp32.py <ip>
    python record_from_esp32.py 192.168.1.50 --duration 15 --output recording.wav
"""
import argparse
import asyncio
import socket
import time
import wave

import numpy as np

from pipeline.audio import decode_ulaw
from pipeline.protocol import send_audio

SAMPLE_RATE = 16000
CHUNK_SIZE = 512


async def main():
    parser = argparse.ArgumentParser(description="Greet ESP32 and record mic audio")
    parser.add_argument("ip", help="ESP32 IP address")
    parser.add_argument("--port", type=int, default=3001, help="TCP port (default: 3001)")
    parser.add_argument("--udp-port", type=int, default=3000, help="UDP port (default: 3000)")
    parser.add_argument("--duration", type=int, default=10, help="Recording duration in seconds")
    parser.add_argument("--output", type=str, default="recording.wav", help="Output WAV file")
    parser.add_argument("--mic-timeout", type=int, default=60, help="Mic timeout in seconds")
    parser.add_argument("--volume", type=int, default=14)
    args = parser.parse_args()

    print(f"Greeting {args.ip}:{args.port} (enabling mic for {args.mic_timeout}s)...")
    await send_audio(args.ip, args.port, b"",
                     mic_timeout=args.mic_timeout, volume=args.volume, fade=5)
    print("Mic enabled")
    await asyncio.sleep(0.5)

    print(f"Recording from UDP :{args.udp_port} for {args.duration}s...")
    print("Talk now!\n")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.udp_port))
    sock.settimeout(1.0)

    audio_frames = []
    packet_count = 0
    start_time = time.time()

    try:
        while (time.time() - start_time) < args.duration:
            try:
                data, addr = sock.recvfrom(2048)
                packet_count += 1

                if len(data) == CHUNK_SIZE:
                    samples = decode_ulaw(data)
                elif len(data) == CHUNK_SIZE * 2:
                    samples = np.frombuffer(data, dtype=np.int16)
                else:
                    continue

                audio_frames.append(samples)

                if packet_count % 10 == 0:
                    elapsed = time.time() - start_time
                    rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))
                    print(f"\r[{elapsed:4.1f}s] Packets: {packet_count:3d} | RMS: {rms:5.0f}", end="", flush=True)

            except socket.timeout:
                continue

    except KeyboardInterrupt:
        print("\n\nStopped")

    sock.close()

    if not audio_frames:
        print("\nNo audio received!")
        return

    audio_data = np.concatenate(audio_frames)
    duration = len(audio_data) / SAMPLE_RATE

    with wave.open(args.output, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_data.tobytes())

    print(f"\n\nSaved {duration:.1f}s to {args.output} ({packet_count} packets)")


if __name__ == "__main__":
    asyncio.run(main())
