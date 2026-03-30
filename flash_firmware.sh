#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$REPO/onjuino/credentials.h.template"
OUTPUT="$REPO/onjuino/credentials.h"
FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200"

# -------------------------------------------------------
# Usage
# -------------------------------------------------------
COMPILE_ONLY=false
REGEN=false
FORCE_COMPILE=false
PORT=""

for arg in "$@"; do
    case "$arg" in
        compile|compile-only) COMPILE_ONLY=true ;;
        --regen) REGEN=true; FORCE_COMPILE=true ;;
        --force) FORCE_COMPILE=true ;;
        -h|--help)
            echo "Usage: flash_firmware.sh [options] [port]"
            echo "  flash_firmware.sh                    # Auto-detect port and upload"
            echo "  flash_firmware.sh /dev/cu.usbmodem1  # Upload to specific port"
            echo "  flash_firmware.sh compile            # Compile only, no upload"
            echo "  flash_firmware.sh --regen            # Force regenerate WiFi credentials"
            echo "  flash_firmware.sh --force            # Force recompile even if unchanged"
            exit 0 ;;
        /dev/*) PORT="$arg" ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# -------------------------------------------------------
# Generate credentials.h (only if missing or --regen)
# -------------------------------------------------------
if [ -f "$OUTPUT" ] && [ "$REGEN" = false ]; then
    echo "Using existing credentials.h (pass --regen to regenerate)"
else
    # --- WiFi SSID ---
    WIFI_SSID=""

    # Find the Wi-Fi interface dynamically (not always en0)
    WIFI_IF=$(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi/{getline; print $2}')
    WIFI_IF="${WIFI_IF:-en0}"

    # Method 1: networksetup (works on older macOS, redacted on Tahoe+)
    WIFI_SSID=$(networksetup -getairportnetwork "$WIFI_IF" 2>/dev/null | sed 's/Current Wi-Fi Network: //')
    if [ -z "$WIFI_SSID" ] || [[ "$WIFI_SSID" == *"not associated"* ]] || [[ "$WIFI_SSID" == *"not a Wi-Fi"* ]] || [[ "$WIFI_SSID" == *"Error"* ]]; then
        WIFI_SSID=""
    fi

    # Method 2: macOS Tahoe+ redacts SSID from most APIs, so offer the
    # preferred networks list and let the user confirm/pick
    if [ -z "$WIFI_SSID" ]; then
        PREFERRED=$(networksetup -listpreferredwirelessnetworks "$WIFI_IF" 2>/dev/null | tail -n +2 | sed 's/^[[:space:]]*//')
        if [ -n "$PREFERRED" ]; then
            TOP_SSID=$(echo "$PREFERRED" | head -1)
            echo "Known WiFi networks:"
            echo "$PREFERRED" | head -5 | cat -n
            echo ""
            read -p "WiFi SSID [$TOP_SSID]: " WIFI_SSID
            WIFI_SSID="${WIFI_SSID:-$TOP_SSID}"
        fi
    fi

    if [ -z "$WIFI_SSID" ]; then
        read -p "WiFi SSID: " WIFI_SSID
    fi

    if [ -z "$WIFI_SSID" ]; then
        echo "ERROR: No WiFi SSID provided."
        exit 1
    fi
    echo "WiFi SSID: $WIFI_SSID"

    # --- WiFi Password ---
    echo "Retrieving WiFi password from Keychain (Touch ID may be required)..."
    WIFI_PASSWORD=$(security find-generic-password -wa "$WIFI_SSID" 2>/dev/null || true)
    if [ -z "$WIFI_PASSWORD" ]; then
        echo "Could not retrieve password for '$WIFI_SSID' from Keychain."
        read -sp "WiFi password: " WIFI_PASSWORD
        echo ""
    fi

    if [ -z "$WIFI_PASSWORD" ]; then
        echo "ERROR: No WiFi password provided."
        exit 1
    fi

    # --- Generate credentials.h from template ---
    sed -e "s|{{WIFI_SSID}}|${WIFI_SSID}|g" \
        -e "s|{{WIFI_PASSWORD}}|${WIFI_PASSWORD}|g" \
        "$TEMPLATE" > "$OUTPUT"

    echo "Generated credentials.h"
fi
echo ""

# -------------------------------------------------------
# Check if compile is needed
# -------------------------------------------------------
BUILD_DIR="$REPO/onjuino/build"
NEEDS_COMPILE=true

if [ "$FORCE_COMPILE" = false ] && [ -d "$BUILD_DIR" ]; then
    BIN=$(find "$BUILD_DIR" -name "*.bin" -maxdepth 1 2>/dev/null | head -1)
    if [ -n "$BIN" ]; then
        NEWER=$(find "$REPO/onjuino" -maxdepth 1 \( -name "*.ino" -o -name "*.h" \) -newer "$BIN" 2>/dev/null | head -1)
        if [ -z "$NEWER" ]; then
            NEEDS_COMPILE=false
        fi
    fi
fi

# -------------------------------------------------------
# Compile
# -------------------------------------------------------
compile_firmware() {
    if [ "$NEEDS_COMPILE" = true ]; then
        echo "Compiling..."
        cd "$REPO/onjuino"
        arduino-cli compile --fqbn "$FQBN" --build-path "$BUILD_DIR" onjuino.ino || exit 1
        echo "✓ Compilation successful!"
    else
        echo "No source changes, skipping compile"
    fi
}

# -------------------------------------------------------
# Compile-only mode
# -------------------------------------------------------
if [ "$COMPILE_ONLY" = true ]; then
    echo "Compile-only mode (no upload)"
    compile_firmware
    exit 0
fi

# -------------------------------------------------------
# Detect ESP32 USB port
# -------------------------------------------------------
if [ -z "$PORT" ]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -n 1 || true)
    if [ -z "$PORT" ]; then
        echo "Error: No USB serial port found (looking for /dev/cu.usbmodem*)"
        exit 1
    fi
    echo "Auto-detected port: $PORT"
fi

echo "Flashing firmware to $PORT..."
echo "If upload fails, press and hold BOOT button, press RESET, then release BOOT"
echo ""

# Kill any serial monitors
pkill -f "serial_monitor" 2>/dev/null || true
pkill -f "python.*serial" 2>/dev/null || true
sleep 1

compile_firmware
echo ""

echo "Uploading..."
cd "$REPO/onjuino"
arduino-cli upload --fqbn "$FQBN" --port "$PORT" --input-dir "$BUILD_DIR" onjuino.ino

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Upload successful!"
    echo ""
    echo "Starting serial monitor in 2 seconds..."
    sleep 2
    cd "$REPO"
    python3 serial_monitor.py "$PORT"
else
    echo ""
    echo "✗ Upload failed"
    echo "Try manually: Hold BOOT button, press RESET, release BOOT, then run this script again"
    exit 1
fi
