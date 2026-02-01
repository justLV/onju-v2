#!/bin/bash
# Run Opus test while monitoring serial output

ESP32_IP=${1:-192.168.68.95}
AUDIO_FILE=${2:-/Users/justin/Desktop/her_8s.m4a}

echo "Starting serial monitor in background..."
~/.local/share/mise/installs/python/3.12.12/bin/python3 -c "
import glob, serial, time

port = sorted(glob.glob('/dev/cu.usbmodem*'))[0]
ser = serial.Serial(port, 115200, timeout=0.1)

print('=== SERIAL OUTPUT ===')
while True:
    try:
        if ser.in_waiting:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line:
                print(f'[ESP32] {line}')
    except:
        break
    time.sleep(0.01)
" &
SERIAL_PID=$!

sleep 2

echo ""
echo "Running Opus test..."
~/.local/share/mise/installs/python/3.12.12/bin/python3 test_opus_tts.py "$ESP32_IP" "$AUDIO_FILE"

echo ""
echo "Waiting for remaining serial output..."
sleep 3

kill $SERIAL_PID 2>/dev/null
echo "Done"
