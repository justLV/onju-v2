#!/bin/bash
# Flash firmware to ESP32

PORT=${1:-/dev/cu.usbmodem1101}
COMPILE=${2:-no}

echo "Flashing firmware to $PORT..."
echo "If upload fails, press and hold BOOT button, press RESET, then release BOOT"
echo ""

# Kill any serial monitors
pkill -f "serial_monitor" 2>/dev/null
pkill -f "python.*serial" 2>/dev/null
sleep 1

cd "$(dirname "$0")/onjuino"

# Only compile if requested
if [ "$COMPILE" = "yes" ] || [ "$COMPILE" = "y" ]; then
    echo "Compiling..."
    arduino-cli compile --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200 onjuino.ino || exit 1
    echo ""
fi

# Upload
echo "Uploading..."
arduino-cli upload -p "$PORT" --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200 onjuino.ino

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Upload successful!"
    echo ""
    echo "Starting serial monitor in 2 seconds..."
    sleep 2
    cd ..
    python3 serial_monitor.py "$PORT"
else
    echo ""
    echo "✗ Upload failed"
    echo "Try manually: Hold BOOT button, press RESET, release BOOT, then run this script again"
    exit 1
fi
