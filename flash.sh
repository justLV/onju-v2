#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"

# -------------------------------------------------------
# Target config
# -------------------------------------------------------
TARGET="${1:-onjuino}"
shift 2>/dev/null || true

case "$TARGET" in
    onjuino)
        FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200"
        PROJECT_DIR="$REPO/onjuino"
        INO_NAME="onjuino.ino"
        PORT_GLOBS=("/dev/cu.usbmodem*")
        ;;
    m5_echo|m5echo)
        FQBN="esp32:esp32:m5stack_atom:UploadSpeed=1500000"
        PROJECT_DIR="$REPO/m5_echo"
        INO_NAME="m5_echo.ino"
        PORT_GLOBS=("/dev/cu.usbserial-*" "/dev/cu.usbmodem*")
        ;;
    --*|compile*)
        # No target specified, treat as flag — default to onjuino
        set -- "$TARGET" "$@"
        TARGET="onjuino"
        FQBN="esp32:esp32:esp32s3:CDCOnBoot=cdc,PSRAM=opi,UploadSpeed=115200"
        PROJECT_DIR="$REPO/onjuino"
        INO_NAME="onjuino.ino"
        PORT_GLOBS=("/dev/cu.usbmodem*")
        ;;
    *)
        echo "Unknown target: $TARGET (expected onjuino or m5_echo)"
        exit 1
        ;;
esac

TEMPLATE="$PROJECT_DIR/credentials.h.template"
OUTPUT="$PROJECT_DIR/credentials.h"
BUILD_DIR="$PROJECT_DIR/build"

# -------------------------------------------------------
# Ensure dependencies
# -------------------------------------------------------
REQUIRED_LIBS=("Adafruit NeoPixel" "esp32_opus")
for lib in "${REQUIRED_LIBS[@]}"; do
    if ! arduino-cli lib list 2>/dev/null | grep -q "$lib"; then
        echo "Installing missing library: $lib"
        arduino-cli lib install "$lib"
    fi
done

# -------------------------------------------------------
# Flags
# -------------------------------------------------------
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
            echo "Usage: flash.sh [target] [options] [port]"
            echo ""
            echo "Targets: onjuino (default), m5_echo"
            echo ""
            echo "Options:"
            echo "  compile          Compile only, no upload"
            echo "  --regen          Force regenerate WiFi credentials"
            echo "  --force          Force recompile even if unchanged"
            echo "  --no-monitor     Skip serial monitor after flash"
            echo "  /dev/...         Upload to specific port"
            exit 0 ;;
        /dev/*) PORT="$arg" ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done

echo "Target: $TARGET"

# -------------------------------------------------------
# Generate credentials.h (only if missing or --regen)
# -------------------------------------------------------
if [ -f "$OUTPUT" ] && [ "$REGEN" = false ]; then
    echo "Using existing credentials.h (pass --regen to regenerate)"
else
    WIFI_SSID=""

    WIFI_IF=$(networksetup -listallhardwareports 2>/dev/null | awk '/Wi-Fi/{getline; print $2}')
    WIFI_IF="${WIFI_IF:-en0}"

    WIFI_SSID=$(networksetup -getairportnetwork "$WIFI_IF" 2>/dev/null | sed 's/Current Wi-Fi Network: //')
    if [ -z "$WIFI_SSID" ] || [[ "$WIFI_SSID" == *"not associated"* ]] || [[ "$WIFI_SSID" == *"not a Wi-Fi"* ]] || [[ "$WIFI_SSID" == *"Error"* ]]; then
        WIFI_SSID=""
    fi

    if [ -z "$WIFI_SSID" ]; then
        PREFERRED=$(networksetup -listpreferredwirelessnetworks "$WIFI_IF" 2>/dev/null | tail -n +2 | sed 's/^[[:space:]]*//')
        if [ -n "$PREFERRED" ]; then
            TOP_SSID=$(echo "$PREFERRED" | head -1)
            echo "Known WiFi networks:"
            NETWORK_LIST=$(echo "$PREFERRED" | head -5)
            echo "$NETWORK_LIST" | cat -n
            NUM_NETWORKS=$(echo "$NETWORK_LIST" | wc -l | tr -d ' ')
            echo ""
            read -p "WiFi SSID [$TOP_SSID]: " WIFI_SSID
            if [ -z "$WIFI_SSID" ]; then
                WIFI_SSID="$TOP_SSID"
            elif [[ "$WIFI_SSID" =~ ^[0-9]+$ ]] && [ "$WIFI_SSID" -ge 1 ] && [ "$WIFI_SSID" -le "$NUM_NETWORKS" ]; then
                WIFI_SSID=$(echo "$NETWORK_LIST" | sed -n "${WIFI_SSID}p")
            fi
        fi
    fi

    [ -z "$WIFI_SSID" ] && read -p "WiFi SSID: " WIFI_SSID
    [ -z "$WIFI_SSID" ] && { echo "ERROR: No WiFi SSID provided."; exit 1; }
    echo "WiFi SSID: $WIFI_SSID"

    echo "Retrieving WiFi password from Keychain (Touch ID may be required)..."
    WIFI_PASSWORD=$(security find-generic-password -wa "$WIFI_SSID" 2>/dev/null || true)
    if [ -z "$WIFI_PASSWORD" ]; then
        echo "Could not retrieve password for '$WIFI_SSID' from Keychain."
        read -sp "WiFi password: " WIFI_PASSWORD
        echo ""
    fi
    [ -z "$WIFI_PASSWORD" ] && { echo "ERROR: No WiFi password provided."; exit 1; }

    sed -e "s|{{WIFI_SSID}}|${WIFI_SSID}|g" \
        -e "s|{{WIFI_PASSWORD}}|${WIFI_PASSWORD}|g" \
        "$TEMPLATE" > "$OUTPUT"
    echo "Generated credentials.h"
fi
echo ""

# -------------------------------------------------------
# Generate git_hash.h (rewrite only on change to avoid recompile churn)
# -------------------------------------------------------
GIT_HASH=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "------")
GIT_HASH_FILE="$PROJECT_DIR/git_hash.h"
NEW_CONTENT="#define GIT_HASH \"${GIT_HASH}\""
if [ ! -f "$GIT_HASH_FILE" ] || [ "$(cat "$GIT_HASH_FILE")" != "$NEW_CONTENT" ]; then
    echo "$NEW_CONTENT" > "$GIT_HASH_FILE"
    echo "Updated git_hash.h to ${GIT_HASH}"
fi

# -------------------------------------------------------
# Check if compile is needed
# -------------------------------------------------------
NEEDS_COMPILE=true
if [ "$FORCE_COMPILE" = false ] && [ -d "$BUILD_DIR" ]; then
    BIN=$(find "$BUILD_DIR" -name "*.ino.bin" -maxdepth 1 2>/dev/null | head -1)
    if [ -n "$BIN" ]; then
        NEWER=$(find "$PROJECT_DIR" -maxdepth 1 \( -name "*.ino" -o -name "*.h" \) -newer "$BIN" 2>/dev/null | head -1)
        [ -z "$NEWER" ] && NEEDS_COMPILE=false
    fi
fi

# -------------------------------------------------------
# Compile
# -------------------------------------------------------
compile_firmware() {
    if [ "$NEEDS_COMPILE" = true ]; then
        echo "Compiling..."
        cd "$PROJECT_DIR"
        arduino-cli compile --fqbn "$FQBN" --build-path "$BUILD_DIR" "$INO_NAME" || exit 1
        echo "Compilation successful!"
    else
        echo "No source changes, skipping compile"
    fi
}

if [ "$COMPILE_ONLY" = true ]; then
    echo "Compile-only mode (no upload)"
    compile_firmware
    exit 0
fi

compile_firmware

# -------------------------------------------------------
# Detect port
# -------------------------------------------------------
if [ -z "$PORT" ]; then
    for glob in "${PORT_GLOBS[@]}"; do
        PORT=$(ls $glob 2>/dev/null | head -n 1 || true)
        [ -n "$PORT" ] && break
    done
    if [ -z "$PORT" ]; then
        echo "Error: No USB serial port found"
        exit 1
    fi
    echo "Auto-detected port: $PORT"
fi

echo ""
echo "Flashing $TARGET to $PORT..."

pkill -f "serial_monitor" 2>/dev/null || true
pkill -f "python.*serial" 2>/dev/null || true
sleep 1

echo "Uploading..."
cd "$PROJECT_DIR"
arduino-cli upload --fqbn "$FQBN" --port "$PORT" --input-dir "$BUILD_DIR" "$INO_NAME"

if [ $? -eq 0 ]; then
    echo ""
    echo "Upload successful!"
    if [ "$NO_MONITOR" = true ]; then
        exit 0
    fi
    echo "Starting serial monitor..."
    sleep 2
    if [ -f "$REPO/serial_monitor.py" ]; then
        python3 "$REPO/serial_monitor.py" "$PORT"
    else
        arduino-cli monitor -p "$PORT" -c baudrate=115200
    fi
else
    echo ""
    echo "Upload failed"
    echo "Try: hold BOOT button, press RESET, release BOOT, then run again"
    exit 1
fi
