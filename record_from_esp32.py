#!/usr/bin/env python3
"""
Record audio from ESP32 via UDP
"""
import socket
import wave
import time
import numpy as np

ESP32_IP = '192.168.68.90'  # Update this if your ESP32 has a different IP
UDP_PORT = 3000
CHUNK_SIZE = 512

# μ-law decode table
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
    return ULAW_DECODE_TABLE[np.frombuffer(ulaw_bytes, dtype=np.uint8)]

print(f"Step 1: Greeting ESP32 at {ESP32_IP} to enable mic...")

# Send greeting via TCP to set mic_timeout
try:
    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.settimeout(5.0)
    tcp_sock.connect((ESP32_IP, 3001))

    # Send greeting: 0xAA header + 60 second timeout
    header = bytearray(6)
    header[0] = 0xAA  # Audio command
    header[1] = 0x00  # Timeout high byte
    header[2] = 60    # Timeout low byte (60 seconds)
    header[3] = 14    # Speaker volume
    header[4] = 5     # LED fade
    header[5] = 0     # Unused

    tcp_sock.send(header)
    tcp_sock.close()
    print(f"✓ Greeted ESP32, mic enabled for 60 seconds")
except Exception as e:
    print(f"✗ Failed to greet ESP32: {e}")
    exit(1)

time.sleep(1)

print(f"\nStep 3: Recording audio from UDP port {UDP_PORT}...")
print("Talk now!\n")

# Record UDP audio
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp_sock.bind(('0.0.0.0', UDP_PORT))
udp_sock.settimeout(1.0)

audio_frames = []
packet_count = 0
start_time = time.time()
duration = 10

try:
    while (time.time() - start_time) < duration:
        try:
            data, addr = udp_sock.recvfrom(2048)
            packet_count += 1

            # Auto-detect compression
            if len(data) == CHUNK_SIZE:
                samples = decode_ulaw(data)
                mode = "μ-law"
            elif len(data) == CHUNK_SIZE * 2:
                samples = np.frombuffer(data, dtype=np.int16)
                mode = "raw"
            else:
                continue

            audio_frames.append(samples)

            # Progress indicator
            if packet_count % 10 == 0:
                elapsed = time.time() - start_time
                rms = np.sqrt(np.mean(samples.astype(np.float32) ** 2))
                print(f"[{elapsed:4.1f}s] Packets: {packet_count:3d} | RMS: {rms:5.0f} | Mode: {mode}", end='\r', flush=True)

        except socket.timeout:
            continue

except KeyboardInterrupt:
    print("\n\nStopped by user")

print(f"\n\nRecording complete!")
print(f"Packets received: {packet_count}")

if audio_frames:
    audio_data = np.concatenate(audio_frames)
    output_file = 'recording.wav'

    with wave.open(output_file, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(audio_data.tobytes())

    duration_sec = len(audio_data) / 16000
    print(f"Saved {duration_sec:.1f}s to {output_file}")
else:
    print("No audio received!")
