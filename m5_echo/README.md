# M5 Echo - Push-to-Talk Voice Client

Firmware for the [M5Stack ATOM Echo](https://shop.m5stack.com/products/atom-echo-smart-speaker-dev-kit) that connects to the pipeline server using the same protocol as onjuino, but with push-to-talk (PTT) instead of voice-activity detection (VAD).

## How it differs from onjuino

| | onjuino | m5_echo |
|---|---|---|
| **Board** | ESP32-S3 (custom PCB) | ESP32-PICO-D4 (M5Stack ATOM Echo) |
| **Mic** | Standard I2S (separate ADC) | PDM (SPM1423, on-chip) |
| **Speaker** | MAX98357A, 6 NeoPixel LEDs | NS4168, 1 SK6812 LED |
| **Interaction** | Capacitive touch: tap to start call, double-tap to end | Physical button: hold to talk, release to listen |
| **Mic control** | VAD-driven timeout (server extends via 0xDD) | Push-to-talk only - mic active while button held |
| **Call lifecycle** | Manual start/end via touch | Auto-starts on boot, persistent until bridge disconnects |
| **PSRAM** | Yes (2MB, large playback buffer) | No (smaller buffers, more DMA buffers to compensate) |
| **Opus** | Full encoder + decoder | Decoder only (mic sends mu-law) |
| **I2S** | Single config for simultaneous TX+RX | Switches between PDM RX (mic) and I2S TX (speaker) |

## PTT behavior

- **Boot**: Connects to WiFi, announces on multicast with `PTT` flag. Bridge auto-starts a call and opens a persistent TCP connection.
- **Idle**: Bridge streams Opus audio over the persistent TCP connection. Device decodes and plays through the speaker.
- **Button press**: Immediately stops speaker playback (I2S switches to PDM mic mode). Opus task keeps reading the TCP stream but discards frames. Mic audio is captured, mu-law encoded, and sent via UDP.
- **Button release**: I2S switches back to speaker mode. Opus task resumes decoding and playing. The TCP connection stays alive throughout - no reconnection needed between turns.
- **Call end**: Bridge closes the TCP connection. Device returns to idle, waiting for a new connection.

## Hardware pin mapping (ATOM Echo)

| Function | GPIO | Notes |
|---|---|---|
| I2S BCK | 19 | Shared speaker bit clock |
| I2S WS/PDM CLK | 33 | Speaker word select / mic PDM clock |
| I2S DOUT | 22 | Speaker data (NS4168) |
| I2S DIN / PDM DATA | 23 | Mic data (SPM1423) |
| Button | 39 | Active low, input only (no internal pullup) |
| LED | 27 | SK6812 RGB (1 LED, GRB order) |

## I2S quirks on ESP32-PICO-D4

The original ESP32's I2S driver with `ALL_RIGHT` channel format treats the DMA buffer as stereo-interleaved, unlike the ESP32-S3 which supports true mono. This means:

- **Speaker**: `sample_rate` is set to `SAMPLE_RATE / 2` (8000) so the effective mono playback rate is 16kHz
- **Mic**: `sample_rate` is set to `SAMPLE_RATE * 2` (32000) and samples are de-interleaved (every other sample) to get 16kHz mono
- **PDM mic channel**: `ALL_RIGHT` is the only channel format that produces non-zero data from the SPM1423 on this board

## Protocol compatibility

Uses the same TCP/UDP protocol as onjuino:

- **UDP 3000**: Mic audio (mu-law encoded, 512 samples/packet)
- **TCP 3001**: Server commands (0xAA audio, 0xBB LED set, 0xCC LED blink, 0xDD mic timeout)
- **Multicast**: Device announces as `"m5-echo m5echo PTT"` on the configured multicast group

The `PTT` token in the multicast announcement tells the bridge to auto-start a call on discovery and keep the TCP connection open persistently (no silence-based disconnection).

## Serial commands

| Key | Action |
|---|---|
| `P` | Play local 440Hz test tone (bypasses network, tests I2S) |
| `T` | Raw mic test (prints sample values) |
| `M` | Force mic on for 10s (no button needed) |
| `m` | Force mic off |
| `A` | Re-send multicast announcement |
| `c` | Enter config mode (ssid/pass/server/volume) |
| `r` | Reboot |

Config mode is also accessible during WiFi connection (if the stored SSID is wrong).

## Building and flashing

From the repo root:

```bash
./flash.sh m5_echo              # auto-detect port, compile and flash
./flash.sh m5_echo compile      # compile only
./flash.sh m5_echo --regen      # regenerate WiFi credentials from keychain
```

Requires `arduino-cli` with the `esp32:esp32` core. The flash script auto-installs required libraries (Adafruit NeoPixel, esp32_opus).

## Testing

```bash
python serial_monitor.py     # serial monitor (auto-detects USB port)
```
