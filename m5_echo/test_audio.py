#!/usr/bin/env python3
"""
Integration test for M5 Echo firmware.
1. Listens for multicast announcement
2. Waits for button press (UDP mic audio)
3. Records a few seconds of mic audio, saves to wav
4. Sends a 440Hz test tone back via TCP
"""

import socket
import struct
import time
import wave
import math
import sys
import threading

DEVICE_IP = None
UDP_PORT = 3000
TCP_PORT = 3001
SAMPLE_RATE = 16000
TONE_FREQ = 440
TONE_DURATION = 2.0  # seconds
SPEAKER_VOLUME = 12

# μ-law decode table (same as firmware)
def ulaw_to_linear(ulaw_byte):
    BIAS = 0x84
    ulaw_byte = ~ulaw_byte & 0xFF
    sign = ulaw_byte & 0x80
    exponent = (ulaw_byte >> 4) & 0x07
    mantissa = ulaw_byte & 0x0F
    sample = ((mantissa << 3) + BIAS) << exponent
    sample -= BIAS
    if sign:
        sample = -sample
    return sample

def generate_tone(freq, duration, sample_rate, volume):
    """Generate PCM 16-bit tone, returns bytes (little-endian)"""
    samples = int(sample_rate * duration)
    data = bytearray()
    for i in range(samples):
        t = i / sample_rate
        # Use a moderate amplitude so volume shift doesn't clip
        val = int(8000 * math.sin(2 * math.pi * freq * t))
        data += struct.pack('<h', val)
    return bytes(data)

def listen_multicast():
    """Listen for device multicast announcement"""
    global DEVICE_IP
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', 12345))
    mreq = struct.pack("4sl", socket.inet_aton("239.0.0.1"), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(5)
    try:
        data, addr = sock.recvfrom(1024)
        DEVICE_IP = addr[0]
        print(f"Device announced: {data.decode()} from {DEVICE_IP}")
    except socket.timeout:
        print("No multicast received (device may have already booted)")
    sock.close()

def receive_mic_audio(duration=3.0):
    """Listen for UDP mic audio packets, decode μ-law, save to wav"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', UDP_PORT))
    sock.settimeout(30)

    print(f"\nWaiting for mic audio on UDP port {UDP_PORT}...")
    print("Press and hold the button on the ATOM Echo to talk")

    all_samples = []
    packets = 0
    start = None

    try:
        while True:
            data, addr = sock.recvfrom(2048)
            if start is None:
                start = time.time()
                if DEVICE_IP is None:
                    globals()['DEVICE_IP'] = addr[0]
                print(f"Receiving audio from {addr[0]}... (hold button for {duration}s)")

            # Decode μ-law
            for byte in data:
                all_samples.append(ulaw_to_linear(byte))
            packets += 1

            if time.time() - start > duration:
                break
    except socket.timeout:
        print("Timeout waiting for audio")
        sock.close()
        return False

    sock.close()
    print(f"Received {packets} packets, {len(all_samples)} samples ({len(all_samples)/SAMPLE_RATE:.1f}s)")

    # Save to wav
    with wave.open('mic_test.wav', 'w') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        for s in all_samples:
            wf.writeframes(struct.pack('<h', max(-32768, min(32767, s))))

    print("Saved to mic_test.wav")

    # Check if we got real audio (not just silence)
    peak = max(abs(s) for s in all_samples) if all_samples else 0
    rms = (sum(s*s for s in all_samples) / len(all_samples)) ** 0.5 if all_samples else 0
    print(f"Peak: {peak}, RMS: {rms:.0f}")
    if peak < 100:
        print("WARNING: Audio appears silent - check mic")
    return True

def send_tone():
    """Send a test tone to the device via TCP"""
    if DEVICE_IP is None:
        print("No device IP known, skipping tone test")
        return False

    print(f"\nSending {TONE_FREQ}Hz tone to {DEVICE_IP}:{TCP_PORT}...")

    tone_data = generate_tone(TONE_FREQ, TONE_DURATION, SAMPLE_RATE, SPEAKER_VOLUME)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((DEVICE_IP, TCP_PORT))
    except (socket.timeout, ConnectionRefusedError) as e:
        print(f"Failed to connect: {e}")
        return False

    # Send 6-byte header: 0xAA, timeout(2), volume, fade_rate, compression(0=PCM)
    timeout_sec = 5
    header = bytes([
        0xAA,
        (timeout_sec >> 8) & 0xFF, timeout_sec & 0xFF,
        SPEAKER_VOLUME,
        10,  # fade rate
        0,   # compression = PCM
    ])
    sock.send(header)
    sock.send(tone_data)
    sock.close()
    print(f"Sent {len(tone_data)} bytes ({TONE_DURATION}s tone)")
    return True

if __name__ == '__main__':
    print("=== M5 Echo Audio Integration Test ===\n")

    # If device IP passed as arg, use it
    if len(sys.argv) > 1:
        DEVICE_IP = sys.argv[1]
        print(f"Using device IP: {DEVICE_IP}")
    else:
        listen_multicast()

    # Step 1: Test mic (receive audio)
    mic_ok = receive_mic_audio(duration=3.0)

    # Step 2: Test speaker (send tone)
    if mic_ok:
        print("\nRelease the button now, then press Enter to send test tone...")
        input()
    spk_ok = send_tone()

    if spk_ok:
        time.sleep(TONE_DURATION + 0.5)
        print("\nDid you hear the tone? (y/n)")

    print("\n=== Test complete ===")
