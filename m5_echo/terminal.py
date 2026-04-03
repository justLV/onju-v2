#!/usr/bin/env python3
"""
M5 Echo serial terminal + test tools.

Commands (type in terminal):
  P  - Play local 440Hz tone (pure I2S, no network)
  T  - Raw mic test (prints sample values)
  M  - Force mic on for 10s
  m  - Force mic off
  A  - Send multicast announcement
  r  - Reboot device
  c  - Enter config mode (ssid/pass/server/volume)
  q  - Quit this terminal
"""

import serial
import sys
import threading
import time

PORT = sys.argv[1] if len(sys.argv) > 1 else None

if not PORT:
    import glob
    ports = glob.glob('/dev/cu.usbserial-*')
    if not ports:
        print("No /dev/cu.usbserial-* found")
        sys.exit(1)
    PORT = ports[0]

print(f"Connecting to {PORT}...")
s = serial.Serial(PORT, 115200, timeout=0.1)

def reader():
    while True:
        try:
            line = s.readline()
            if line:
                print(line.decode('utf-8', errors='replace'), end='')
        except:
            break

t = threading.Thread(target=reader, daemon=True)
t.start()

print(f"Connected. Type single-char commands (P=tone, T=mic test, r=reboot, q=quit)")
print()

try:
    while True:
        ch = input()
        if ch == 'q':
            break
        if ch:
            s.write(ch[0].encode())
except (KeyboardInterrupt, EOFError):
    pass

s.close()
