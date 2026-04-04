#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$REPO/credentials.h.template"
OUTPUT="$REPO/credentials.h"
FQBN="esp32:esp32:pico32:UploadSpeed=115200"
BUILD_DIR="$REPO/build"

COMPILE_ONLY=false
REGEN=false
FORCE_COMPILE=false
NO_MONITOR=false
PORT=""

for arg in "$@"; do
    case "$arg" in
        compile|compile-only) COMPILE_ONLY=true ;;
        --regen) REGEN=true; FORCE_COMPILE=true ;;
        --force) FORCE_COMPILE=true ;;
        --no-monitor) NO_MONITOR=true ;;
        -h|--help)
            echo "Usage: flash.sh [options] [port]"
            echo "  flash.sh                        # Auto-detect port and upload"
            echo "  flash.sh /dev/cu.usbserial-xxx  # Upload to specific port"
            echo "  flash.sh compile                # Compile only"
            echo "  flash.sh --regen                # Regenerate WiFi credentials"
            echo "  flash.sh --force                # Force recompile"
            echo "  flash.sh --no-monitor           # Skip serial monitor after flash"
            exit 0 ;;
        /dev/*) PORT="$arg" ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

# ── Credentials ──────────────────────────────────────────────
if [ -f "$OUTPUT" ] && [ "$REGEN" = false ]; then
    echo "Using existing credentials.h (pass --regen to regenerate)"
else
    WIFI_SSID=""
    WIFI_IF=$(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi/{getline; print $2}')
    WIFI_IF="${WIFI_IF:-en0}"

    WIFI_SSID=$(networksetup -getairportnetwork "$WIFI_IF" 2>/dev/null | sed 's/Current Wi-Fi Network: //')
    if [ -z "$WIFI_SSID" ] || [[ "$WIFI_SSID" == *"not associated"* ]] || [[ "$WIFI_SSID" == *"Error"* ]]; then
        WIFI_SSID=""
    fi

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

    [ -z "$WIFI_SSID" ] && read -p "WiFi SSID: " WIFI_SSID
    [ -z "$WIFI_SSID" ] && { echo "ERROR: No WiFi SSID"; exit 1; }
    echo "WiFi SSID: $WIFI_SSID"

    echo "Retrieving WiFi password from Keychain..."
    WIFI_PASSWORD=$(security find-generic-password -wa "$WIFI_SSID" 2>/dev/null || true)
    if [ -z "$WIFI_PASSWORD" ]; then
        read -sp "WiFi password: " WIFI_PASSWORD
        echo ""
    fi
    [ -z "$WIFI_PASSWORD" ] && { echo "ERROR: No WiFi password"; exit 1; }

    sed -e "s|{{WIFI_SSID}}|${WIFI_SSID}|g" \
        -e "s|{{WIFI_PASSWORD}}|${WIFI_PASSWORD}|g" \
        "$TEMPLATE" > "$OUTPUT"
    echo "Generated credentials.h"
fi
echo ""

# ── Check if compile needed ──────────────────────────────────
NEEDS_COMPILE=true
if [ "$FORCE_COMPILE" = false ] && [ -d "$BUILD_DIR" ]; then
    BIN=$(find "$BUILD_DIR" -name "*.bin" -maxdepth 1 2>/dev/null | head -1)
    if [ -n "$BIN" ]; then
        NEWER=$(find "$REPO" -maxdepth 1 \( -name "*.ino" -o -name "*.h" \) -newer "$BIN" 2>/dev/null | head -1)
        [ -z "$NEWER" ] && NEEDS_COMPILE=false
    fi
fi

# ── Compile ──────────────────────────────────────────────────
compile_firmware() {
    if [ "$NEEDS_COMPILE" = true ]; then
        echo "Compiling..."
        cd "$REPO"
        arduino-cli compile --fqbn "$FQBN" --build-path "$BUILD_DIR" m5_echo.ino || exit 1
        echo "Compilation successful!"
    else
        echo "No source changes, skipping compile"
    fi
}

if [ "$COMPILE_ONLY" = true ]; then
    compile_firmware
    exit 0
fi

compile_firmware

# ── Detect port ──────────────────────────────────────────────
if [ -z "$PORT" ]; then
    PORT=$(ls /dev/cu.usbserial-* 2>/dev/null | head -n 1 || true)
    if [ -z "$PORT" ]; then
        PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -n 1 || true)
    fi
    if [ -z "$PORT" ]; then
        echo "Error: No USB serial port found"
        exit 1
    fi
    echo "Auto-detected port: $PORT"
fi

echo ""
echo "Flashing to $PORT..."

pkill -f "serial_monitor" 2>/dev/null || true
pkill -f "python.*serial" 2>/dev/null || true
sleep 1

cd "$REPO"
arduino-cli upload --fqbn "$FQBN" --port "$PORT" --input-dir "$BUILD_DIR" m5_echo.ino

if [ $? -eq 0 ]; then
    echo ""
    echo "Upload successful!"
    if [ "$NO_MONITOR" = true ]; then
        exit 0
    fi
    echo ""
    echo "Starting serial monitor..."
    sleep 2
    # Try the repo-level serial monitor, fall back to arduino-cli
    if [ -f "$REPO/../serial_monitor.py" ]; then
        python3 "$REPO/../serial_monitor.py" "$PORT"
    else
        arduino-cli monitor -p "$PORT" -c baudrate=115200
    fi
else
    echo "Upload failed. Try: hold the button while plugging in USB, then run again."
    exit 1
fi
