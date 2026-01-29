#!/usr/bin/env python3
"""
Simple speaker test - send a WAV file to ESP32
Uses same approach as working server implementation
"""
import socket
import time
from pydub import AudioSegment

ESP32_IP = "192.168.68.97"
ESP32_PORT = 3001
WAV_FILE = "recording.wav"  # Use our test recording

print(f"Testing speaker with {WAV_FILE}")
print(f"Connecting to {ESP32_IP}:{ESP32_PORT}...")

# Load and convert WAV (same as server does)
audio = AudioSegment.from_wav(WAV_FILE)
audio = audio.set_channels(1)       # Mono
audio = audio.set_frame_rate(16000) # 16kHz
audio = audio.set_sample_width(2)   # 16-bit
pcm_data = audio.raw_data

print(f"Audio loaded: {len(pcm_data):,} bytes ({len(pcm_data)/32000:.1f}s)")

# Connect to ESP32
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(10.0)
sock.connect((ESP32_IP, ESP32_PORT))

print("Connected! Sending header...")

# Send header (same as server)
# header[0]   = 0xAA (audio command)
# header[1:2] = mic timeout in seconds (60s)
# header[3]   = volume (14)
# header[4]   = LED fade (5)
# header[5]   = unused
mic_timeout = 60
volume = 14
fade = 5

header = bytes([
    0xAA,
    (mic_timeout & 0xFF00) >> 8,  # High byte
    mic_timeout & 0xFF,            # Low byte
    volume,
    fade,
    0
])

sock.send(header)
print(f"Header sent: {list(header)}")

# Send audio data
print(f"Sending {len(pcm_data):,} bytes of audio...")
start_time = time.time()
sock.sendall(pcm_data)
end_time = time.time()

print(f"Audio sent in {end_time - start_time:.2f}s")
print("Waiting for playback to complete...")

time.sleep(2)
sock.close()

print("Done! Did you hear audio?")
