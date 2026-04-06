#!/usr/bin/env python3
"""Quick test: can we receive UDP on port 3000?"""
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", 3000))
sock.settimeout(15)
print("Listening on :3000 — press PTT on M5 within 15s...")
try:
    data, addr = sock.recvfrom(2048)
    print(f"Got {len(data)}B from {addr}")
except socket.timeout:
    print("No packets received (timeout)")
finally:
    sock.close()
