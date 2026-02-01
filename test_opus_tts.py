#!/usr/bin/env python3
"""
Test Opus-compressed audio streaming to ESP32
Demonstrates 10-16x compression over raw PCM
"""
import socket
import struct
import time
from pydub import AudioSegment
import opuslib

# Config
import sys
ESP32_IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.68.97"
ESP32_PORT = 3001
WAV_FILE = sys.argv[2] if len(sys.argv) > 2 else "recording.wav"

# Opus settings
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 320  # 20ms @ 16kHz
BITRATE = 12000   # 12 kbps for voice (adjustable: 8000-24000)

def main():
    print("="*60)
    print("Opus Compressed Audio Test")
    print("="*60)
    print(f"ESP32: {ESP32_IP}:{ESP32_PORT}")
    print(f"Source: {WAV_FILE}")
    print(f"Opus settings: {SAMPLE_RATE}Hz, {CHANNELS}ch, {BITRATE}bps")
    print()

    # Load audio
    print("Loading audio...")
    # Detect file type and load accordingly
    if WAV_FILE.endswith('.wav'):
        audio = AudioSegment.from_wav(WAV_FILE)
    elif WAV_FILE.endswith('.m4a'):
        audio = AudioSegment.from_file(WAV_FILE, format='m4a')
    elif WAV_FILE.endswith('.mp3'):
        audio = AudioSegment.from_mp3(WAV_FILE)
    else:
        audio = AudioSegment.from_file(WAV_FILE)

    audio = audio.set_channels(CHANNELS)
    audio = audio.set_frame_rate(SAMPLE_RATE)
    audio = audio.set_sample_width(2)  # 16-bit
    pcm_data = audio.raw_data

    print(f"Loaded {len(pcm_data):,} bytes of PCM audio ({len(pcm_data)/32000:.1f}s)")
    print()

    # Initialize Opus encoder
    print("Initializing Opus encoder...")
    try:
        encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP)
        print(f"Encoder created successfully (using default bitrate)")
        # Note: Setting bitrate fails with opuslib, using default instead
    except Exception as e:
        print(f"ERROR creating Opus encoder: {e}")
        import traceback
        traceback.print_exc()
        return
    print()

    # Connect to ESP32
    print(f"Connecting to ESP32...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)

    try:
        sock.connect((ESP32_IP, ESP32_PORT))
        print(f"Connected!")
        print()

        # Send header (0xAA command with Opus compression type)
        # header[5] = 2 for Opus
        header = bytes([0xAA, 0x00, 60, 14, 5, 2])
        sock.send(header)
        print(f"Header sent: {list(header)}")
        print()

        # Encode and send PCM in 20ms frames
        frame_bytes = FRAME_SIZE * 2  # 320 samples * 2 bytes = 640 bytes
        total_pcm_bytes = 0
        total_opus_bytes = 0
        frame_count = 0
        start_time = time.time()

        print("Encoding and streaming...")
        for i in range(0, len(pcm_data), frame_bytes):
            pcm_frame = pcm_data[i:i+frame_bytes]

            # Pad last frame if needed
            if len(pcm_frame) < frame_bytes:
                pcm_frame += b'\x00' * (frame_bytes - len(pcm_frame))

            # Encode to Opus
            try:
                opus_frame = encoder.encode(pcm_frame, FRAME_SIZE)
            except Exception as e:
                print(f"ERROR encoding frame {frame_count}: {e}")
                continue

            # Send with 2-byte length prefix (big-endian)
            frame_len = len(opus_frame)

            if frame_count < 5:
                print(f"  Frame {frame_count}: PCM={len(pcm_frame)} bytes -> Opus={frame_len} bytes")

            sock.send(struct.pack('>H', frame_len))
            sock.send(opus_frame)

            total_pcm_bytes += len(pcm_frame)
            total_opus_bytes += frame_len
            frame_count += 1

            if frame_count % 100 == 0:
                elapsed = time.time() - start_time
                avg_frame_size = total_opus_bytes / frame_count
                print(f"  Sent {frame_count} frames ({total_opus_bytes:,} bytes, avg frame: {avg_frame_size:.1f} bytes, {elapsed:.1f}s elapsed)")

        end_time = time.time()
        total_time = end_time - start_time

        sock.close()

        # Statistics
        compression_ratio = total_pcm_bytes / total_opus_bytes
        pcm_kbps = (total_pcm_bytes * 8) / (total_pcm_bytes / 32000) / 1000
        opus_kbps = (total_opus_bytes * 8) / (total_pcm_bytes / 32000) / 1000
        audio_duration = total_pcm_bytes / 32000

        print()
        print("="*60)
        print("RESULTS:")
        print("="*60)
        print(f"Audio duration:       {audio_duration:.1f}s")
        print(f"Frames sent:          {frame_count}")
        print(f"Total send time:      {total_time:.2f}s")
        print()
        print("SIZE COMPARISON:")
        print(f"Original PCM:         {total_pcm_bytes:,} bytes")
        print(f"Opus compressed:      {total_opus_bytes:,} bytes")
        print(f"Compression ratio:    {compression_ratio:.1f}x")
        print()
        print("BANDWIDTH COMPARISON:")
        print(f"PCM bandwidth:        {pcm_kbps:.1f} kbps")
        print(f"Opus bandwidth:       {opus_kbps:.1f} kbps")
        print(f"Bandwidth savings:    {pcm_kbps - opus_kbps:.1f} kbps ({(1 - opus_kbps/pcm_kbps)*100:.0f}%)")
        print()
        print("WIFI MARGIN:")
        network_throughput = 553.9  # From previous tests
        pcm_margin = network_throughput / pcm_kbps
        opus_margin = network_throughput / opus_kbps
        print(f"With PCM:             {pcm_margin:.1f}x margin")
        print(f"With Opus:            {opus_margin:.1f}x margin")
        print(f"Improvement:          {opus_margin/pcm_margin:.1f}x better!")
        print("="*60)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sock.close()

if __name__ == '__main__':
    main()
