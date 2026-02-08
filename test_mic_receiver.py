#!/usr/bin/env python3
"""
ESP32 Mic UDP Test Receiver
Receives audio from ESP32, detects compression, saves to WAV
"""

import socket
import struct
import wave
import time
import argparse
from collections import deque
import numpy as np

SAMPLE_RATE = 16000
CHUNK_SIZE = 512
CHUNK_BYTES_RAW = CHUNK_SIZE * 2      # int16 = 2 bytes
CHUNK_BYTES_COMPRESSED = CHUNK_SIZE   # μ-law = 1 byte

# μ-law lookup table for decoding (same as ESP32)
ULAW_DECODE_TABLE = np.array([
    -32124,-31100,-30076,-29052,-28028,-27004,-25980,-24956,
    -23932,-22908,-21884,-20860,-19836,-18812,-17788,-16764,
    -15996,-15484,-14972,-14460,-13948,-13436,-12924,-12412,
    -11900,-11388,-10876,-10364,-9852,-9340,-8828,-8316,
    -7932,-7676,-7420,-7164,-6908,-6652,-6396,-6140,
    -5884,-5628,-5372,-5116,-4860,-4604,-4348,-4092,
    -3900,-3772,-3644,-3516,-3388,-3260,-3132,-3004,
    -2876,-2748,-2620,-2492,-2364,-2236,-2108,-1980,
    -1884,-1820,-1756,-1692,-1628,-1564,-1500,-1436,
    -1372,-1308,-1244,-1180,-1116,-1052,-988,-924,
    -876,-844,-812,-780,-748,-716,-684,-652,
    -620,-588,-556,-524,-492,-460,-428,-396,
    -372,-356,-340,-324,-308,-292,-276,-260,
    -244,-228,-212,-196,-180,-164,-148,-132,
    -120,-112,-104,-96,-88,-80,-72,-64,
    -56,-48,-40,-32,-24,-16,-8,0,
    32124,31100,30076,29052,28028,27004,25980,24956,
    23932,22908,21884,20860,19836,18812,17788,16764,
    15996,15484,14972,14460,13948,13436,12924,12412,
    11900,11388,10876,10364,9852,9340,8828,8316,
    7932,7676,7420,7164,6908,6652,6396,6140,
    5884,5628,5372,5116,4860,4604,4348,4092,
    3900,3772,3644,3516,3388,3260,3132,3004,
    2876,2748,2620,2492,2364,2236,2108,1980,
    1884,1820,1756,1692,1628,1564,1500,1436,
    1372,1308,1244,1180,1116,1052,988,924,
    876,844,812,780,748,716,684,652,
    620,588,556,524,492,460,428,396,
    372,356,340,324,308,292,276,260,
    244,228,212,196,180,164,148,132,
    120,112,104,96,88,80,72,64,
    56,48,40,32,24,16,8,0
], dtype=np.int16)

def decode_ulaw(ulaw_bytes):
    """Decode μ-law compressed audio to int16 PCM"""
    return ULAW_DECODE_TABLE[np.frombuffer(ulaw_bytes, dtype=np.uint8)]

def calculate_rms(samples):
    """Calculate RMS of audio samples"""
    return np.sqrt(np.mean(samples.astype(np.float32) ** 2))

def main():
    parser = argparse.ArgumentParser(description='ESP32 mic UDP receiver')
    parser.add_argument('--port', type=int, default=3000, help='UDP port (default: 3000)')
    parser.add_argument('--output', type=str, default='test_recording.wav', help='Output WAV file')
    parser.add_argument('--duration', type=int, default=10, help='Recording duration in seconds (0 = infinite)')
    parser.add_argument('--compressed', action='store_true', help='Expect μ-law compressed audio')
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', args.port))
    sock.settimeout(1.0)

    print(f"Listening on UDP port {args.port}...")
    print(f"Expected format: {'μ-law compressed' if args.compressed else 'raw int16 PCM'}")
    print(f"Output file: {args.output}")
    print("Waiting for packets...\n")

    audio_frames = []
    packet_count = 0
    total_bytes = 0
    start_time = None
    last_stats_time = None
    rms_history = deque(maxlen=20)

    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)

                if start_time is None:
                    start_time = time.time()
                    last_stats_time = start_time
                    print(f"Connected to ESP32: {addr[0]}:{addr[1]}\n")

                packet_count += 1
                total_bytes += len(data)

                # Auto-detect compression based on packet size
                if len(data) == CHUNK_BYTES_COMPRESSED:
                    # Compressed μ-law
                    samples = decode_ulaw(data)
                    is_compressed = True
                elif len(data) == CHUNK_BYTES_RAW:
                    # Raw PCM int16
                    samples = np.frombuffer(data, dtype=np.int16)
                    is_compressed = False
                else:
                    print(f"Warning: Unexpected packet size {len(data)} bytes (expected {CHUNK_BYTES_COMPRESSED} or {CHUNK_BYTES_RAW})")
                    continue

                audio_frames.append(samples)
                rms = calculate_rms(samples)
                rms_history.append(rms)

                # Print stats every second
                current_time = time.time()
                if current_time - last_stats_time >= 1.0:
                    elapsed = current_time - start_time
                    bandwidth_kbps = (total_bytes * 8) / (elapsed * 1000)
                    avg_rms = np.mean(list(rms_history))

                    print(f"\r[{elapsed:6.1f}s] Packets: {packet_count:4d} | "
                          f"Bandwidth: {bandwidth_kbps:5.1f} kbps | "
                          f"RMS: {avg_rms:6.0f} | "
                          f"Mode: {'μ-law' if is_compressed else 'raw  '}",
                          end='', flush=True)

                    last_stats_time = current_time

                # Check duration
                if args.duration > 0 and (current_time - start_time) >= args.duration:
                    print("\n\nRecording duration reached.")
                    break

            except socket.timeout:
                if start_time is not None:
                    print("\n\nTimeout - no packets received for 1 second")
                    break
                continue

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")

    if not audio_frames:
        print("No audio received!")
        return

    # Combine all frames
    print(f"\n\nSaving {len(audio_frames)} chunks to {args.output}...")
    audio_data = np.concatenate(audio_frames)

    # Save to WAV
    with wave.open(args.output, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(audio_data.tobytes())

    # Final stats
    duration = len(audio_data) / SAMPLE_RATE
    file_size_kb = len(audio_data) * 2 / 1024
    transmitted_kb = total_bytes / 1024
    compression_ratio = transmitted_kb / file_size_kb if file_size_kb > 0 else 1.0

    print(f"\n{'='*60}")
    print(f"Recording complete!")
    print(f"{'='*60}")
    print(f"Duration:          {duration:.2f} seconds")
    print(f"Samples:           {len(audio_data):,}")
    print(f"WAV file size:     {file_size_kb:.1f} KB")
    print(f"Bytes transmitted: {transmitted_kb:.1f} KB")
    print(f"Compression ratio: {compression_ratio:.2f}x")
    print(f"Average bandwidth: {(total_bytes * 8) / (duration * 1000):.1f} kbps")
    print(f"Packets received:  {packet_count}")
    print(f"Packet loss:       {(1 - packet_count / (duration / 0.03)) * 100:.1f}%")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()
