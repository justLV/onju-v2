# Opus Compression Implementation Plan

## Overview
Add Opus decoding to ESP32 for receiving compressed TTS audio from server over TCP, achieving ~10x compression over raw PCM (or 5x over current μ-law).

## Why Opus?
- **10-16x compression** for 16kHz mono voice (vs 2x for μ-law)
- **High quality** - suitable for human listening (unlike μ-law)
- **Bandwidth target**: 12-16 kbps (vs current 128 kbps with μ-law, 256 kbps raw)
- **WiFi margin**: 4.4x → 20-30x throughput margin
- **Resource usage**: ~20% CPU, 15-20 KB heap on ESP32-S3

## Architecture

### Current Flow (μ-law)
```
Server → [PCM 16kHz] → μ-law encode → TCP → ESP32 → μ-law decode → I2S speaker
         32 KB/s                       16 KB/s           32 KB/s
```

### New Flow (Opus)
```
Server → [PCM 16kHz] → Opus encode → TCP → ESP32 → Opus decode → I2S speaker
         32 KB/s                      1.5-2 KB/s         32 KB/s
```

## Packet Framing

### Current (PCM/μ-law)
- Fixed size chunks: 512 bytes μ-law = 32ms audio
- No frame length needed (fixed size)

### Opus (variable bitrate)
```
[2-byte length][Opus frame data]
```

- Length: uint16_t in bytes (network byte order)
- Frame: Compressed Opus frame
- Target frame size: ~1KB raw Opus data = 320-640ms of audio @ 12-16 kbps
- Frame duration: Use 20ms frames (standard for voice)

### Example:
```
For 20ms @ 16kHz @ 12 kbps:
- PCM input: 20ms × 16000 Hz × 2 bytes = 640 bytes
- Opus output: ~30 bytes per 20ms frame
- Accumulate 32 frames (640ms) → ~960 bytes → send as one packet
```

## Implementation Steps

### 1. Add Opus Library to ESP32 Firmware

**Library:** [sh123/esp32_opus_arduino](https://github.com/sh123/esp32_opus_arduino)

**Installation:**
```bash
# Option A: Arduino Library Manager
# Search for "esp32_opus" and install

# Option B: Manual (recommended for control)
cd ~/Arduino/libraries
git clone https://github.com/sh123/esp32_opus_arduino.git
```

**Or use PlatformIO:**
```ini
lib_deps =
    sh123/esp32_opus@^1.0.0
```

### 2. Modify ESP32 Firmware

**Changes to onjuino.ino:**

```cpp
#include <opus.h>

// Opus decoder state
OpusDecoder *opus_decoder = NULL;
const int OPUS_FRAME_SIZE = 320;  // 20ms @ 16kHz
int16_t opus_pcm_buffer[OPUS_FRAME_SIZE];

void setup() {
    // ... existing setup ...

    // Initialize Opus decoder
    int error;
    opus_decoder = opus_decoder_create(16000, 1, &error);  // 16kHz, mono
    if (error != OPUS_OK) {
        Serial.printf("Opus decoder create failed: %d\n", error);
    } else {
        Serial.println("Opus decoder initialized");
    }
}

// In TCP handler (replacing current PCM reception)
void handleOpusAudio(WiFiClient &client) {
    while (client.connected()) {
        // Read 2-byte frame length
        if (client.available() < 2) {
            delay(1);
            continue;
        }

        uint8_t len_bytes[2];
        client.read(len_bytes, 2);
        uint16_t frame_len = (len_bytes[0] << 8) | len_bytes[1];

        // Sanity check
        if (frame_len > 4000) {
            Serial.printf("Invalid frame length: %d\n", frame_len);
            break;
        }

        // Read Opus frame
        uint8_t opus_frame[frame_len];
        size_t bytes_read = 0;
        while (bytes_read < frame_len) {
            int avail = client.available();
            if (avail > 0) {
                int to_read = min(avail, (int)(frame_len - bytes_read));
                bytes_read += client.read(opus_frame + bytes_read, to_read);
            } else {
                delay(1);
            }
        }

        // Decode Opus frame
        int num_samples = opus_decode(
            opus_decoder,
            opus_frame, frame_len,
            opus_pcm_buffer, OPUS_FRAME_SIZE,
            0  // decode_fec (forward error correction)
        );

        if (num_samples < 0) {
            Serial.printf("Opus decode error: %d\n", num_samples);
            continue;
        }

        // Write to I2S (same as before, but from opus_pcm_buffer)
        // Convert to 32-bit and apply volume
        for (int i = 0; i < num_samples; i++) {
            wavData[totalSamplesRead++] = (int32_t)opus_pcm_buffer[i] << speaker_volume;
        }

        // Drain buffer when full (existing logic)
        // ...
    }
}
```

### 3. Server-Side Test Script

**test_opus_tts.py:**

```python
#!/usr/bin/env python3
"""
Test Opus-compressed TTS streaming to ESP32
"""
import socket
import struct
from pydub import AudioSegment
import opuslib

ESP32_IP = "192.168.68.97"
ESP32_PORT = 3001
WAV_FILE = "recording.wav"

# Opus settings
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SIZE = 320  # 20ms @ 16kHz
BITRATE = 12000   # 12 kbps for voice

def main():
    # Load audio
    audio = AudioSegment.from_wav(WAV_FILE)
    audio = audio.set_channels(CHANNELS)
    audio = audio.set_frame_rate(SAMPLE_RATE)
    audio = audio.set_sample_width(2)  # 16-bit
    pcm_data = audio.raw_data

    print(f"Loaded {len(pcm_data)} bytes of PCM audio ({len(pcm_data)/32000:.1f}s)")

    # Initialize Opus encoder
    encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP)
    encoder.bitrate = BITRATE

    # Connect to ESP32
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((ESP32_IP, ESP32_PORT))
    print(f"Connected to {ESP32_IP}:{ESP32_PORT}")

    # Send header (0xAA command with Opus flag)
    header = bytes([0xAA, 0x00, 60, 14, 5, 0])
    sock.send(header)
    print("Header sent")

    # Encode and send PCM in 20ms frames
    frame_bytes = FRAME_SIZE * 2  # 320 samples * 2 bytes
    total_opus_bytes = 0
    frame_count = 0

    for i in range(0, len(pcm_data), frame_bytes):
        pcm_frame = pcm_data[i:i+frame_bytes]

        # Pad last frame if needed
        if len(pcm_frame) < frame_bytes:
            pcm_frame += b'\x00' * (frame_bytes - len(pcm_frame))

        # Encode to Opus
        opus_frame = encoder.encode(pcm_frame, FRAME_SIZE)

        # Send with length prefix
        frame_len = len(opus_frame)
        sock.send(struct.pack('>H', frame_len))  # Big-endian uint16
        sock.send(opus_frame)

        total_opus_bytes += frame_len
        frame_count += 1

        if frame_count % 50 == 0:
            print(f"Sent {frame_count} frames, {total_opus_bytes:,} bytes")

    sock.close()

    # Statistics
    compression_ratio = len(pcm_data) / total_opus_bytes
    print(f"\nRESULTS:")
    print(f"Original PCM:     {len(pcm_data):,} bytes")
    print(f"Opus compressed:  {total_opus_bytes:,} bytes")
    print(f"Compression:      {compression_ratio:.1f}x")
    print(f"Bandwidth:        {(total_opus_bytes * 8 / (len(pcm_data)/32000)) / 1000:.1f} kbps")
    print(f"Frames sent:      {frame_count}")

if __name__ == '__main__':
    main()
```

**Dependencies:**
```bash
pip install opuslib pydub
```

### 4. Modified Header Format

Add Opus flag to header to indicate compression type:

```cpp
/*
header[0]   0xAA for audio
header[1:2] mic timeout in seconds
header[3]   volume
header[4]   fade rate
header[5]   compression type: 0=PCM, 1=μ-law, 2=Opus
*/
```

## Testing Plan

1. **Install Opus library** on ESP32
2. **Compile and flash** modified firmware
3. **Run test_opus_tts.py** with recording.wav
4. **Verify audio playback** quality
5. **Measure compression ratio** and bandwidth usage

## Expected Results

### Bandwidth Comparison
```
Raw PCM:      256 kbps (32 KB/s)
μ-law:        128 kbps (16 KB/s)  [2x compression]
Opus:         12-16 kbps (1.5-2 KB/s)  [16-21x compression]
```

### WiFi Margin
```
Current:      553.9 kbps throughput / 128 kbps μ-law = 4.3x margin
With Opus:    553.9 kbps throughput / 15 kbps opus = 36.9x margin
```

## Fallback Strategy

If Opus proves problematic:
1. **ADPCM**: 4x compression, simpler than Opus
2. **Lower sample rate**: 8kHz instead of 16kHz (2x savings)
3. **Variable bitrate μ-law**: Silence detection to skip packets

## Integration with ElevenLabs

ElevenLabs can output Opus directly:

```python
audio_stream = client.text_to_speech.convert(
    voice_id=VOICE_ID,
    text=TEXT,
    model_id="eleven_monolingual_v1",
    output_format="opus_16000"  # Native Opus output!
)
```

This avoids double-encoding (PCM → Opus on server).

## Memory Considerations

**ESP32-S3 with 2MB PSRAM:**
- Opus decoder: ~20 KB heap (use PSRAM)
- PCM buffer: 8KB (existing)
- Opus frame buffer: ~4KB max
- **Total overhead: ~24 KB** (negligible with 2MB PSRAM)

## CPU Usage

Expected: **10-20% of one core @ 240MHz** for Opus decoding at 16kHz mono.

This leaves plenty of headroom for:
- WiFi/TCP handling
- I2S audio output
- LED visualization
- Touch sensor processing

## Next Steps

1. ✅ Research Opus libraries (DONE)
2. ⬜ Install sh123/esp32_opus_arduino library
3. ⬜ Modify onjuino.ino with Opus decoder
4. ⬜ Create test_opus_tts.py script
5. ⬜ Test and validate
6. ⬜ Integrate with ElevenLabs native Opus output
7. ⬜ Update server.py to use Opus for all TTS
