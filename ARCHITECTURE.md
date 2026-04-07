# Onju Voice Architecture

## System Overview

ESP32-S3 voice assistant with bidirectional audio streaming over WiFi to a server running speech recognition and text-to-speech.

```
┌─────────────────────────────────────────────────────────────┐
│                         ESP32-S3                             │
│  ┌──────────┐    ┌─────────┐    ┌──────────┐   ┌─────────┐ │
│  │   Mic    │───→│ I2S RX  │───→│ μ-law    │──→│   UDP   │ │
│  │ (INMP441)│    │ 16kHz   │    │ encode   │   │  3000   │ │
│  └──────────┘    └─────────┘    └──────────┘   └─────┬───┘ │
│                                                        │     │
│  ┌──────────┐    ┌─────────┐    ┌──────────┐   ┌─────▼───┐ │
│  │ Speaker  │◀───│ I2S TX  │◀───│  Opus    │◀──│   TCP   │ │
│  │(MAX98357)│    │ 16kHz   │    │ decode   │   │  3001   │ │
│  └──────────┘    └─────────┘    └──────────┘   └─────────┘ │
└─────────────────────────────────────────────────────────────┘
                                 WiFi
                                  │
┌─────────────────────────────────▼───────────────────────────┐
│                           Server                             │
│  ┌─────────┐    ┌──────────┐    ┌─────────────────────┐    │
│  │   UDP   │───→│  μ-law   │───→│  Speech-to-Text     │    │
│  │  3000   │    │  decode  │    │  (Whisper/Deepgram) │    │
│  └─────────┘    └──────────┘    └─────────────────────┘    │
│                                                              │
│  ┌─────────┐    ┌──────────┐    ┌─────────────────────┐    │
│  │   TCP   │◀───│  Opus    │◀───│  Text-to-Speech     │    │
│  │  3001   │    │  encode  │    │  (ElevenLabs/etc)   │    │
│  └─────────┘    └──────────┘    └─────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## Audio Paths

### Microphone → Server (UDP + μ-law)

- Sample rate: 16kHz mono, 512 samples/chunk (32ms)
- μ-law compressed: 512 bytes/chunk (16 KB/s) — 2x reduction
- UDP: no retransmissions, no connection overhead — old audio is stale anyway
- DC offset removed per-chunk before encoding

**Why μ-law over Opus upstream:** μ-law is stateless (sample-by-sample table lookup, ~1% CPU), zero buffering latency, and ASR models handle the quality fine. Opus would add 20-60ms frame buffering and 10-20% CPU for no practical benefit upstream.

**Why UDP over TCP:** Retransmissions add latency and head-of-line blocking delays newer audio. ASR handles occasional packet loss better than delayed old audio.

### Server → Speaker (TCP + Opus)

- Sample rate: 16kHz mono, 320 samples/frame (20ms)
- Opus compressed: ~35-50 bytes/frame (1.5-2 KB/s) — 14-16x reduction
- TCP: reliable ordered delivery required for Opus frame decoding

**Why Opus over μ-law downstream:** Human ears need better quality than ASR. Opus gives 14-16x compression vs μ-law's 2x, turning a tight 2.2x WiFi margin into 30x+.

**Why TCP over UDP:** Lost or out-of-order Opus frames cause decode errors. TCP's reliability guarantees are worth the slight latency cost, especially with the playback buffer absorbing jitter.

## Device Discovery & Connection

1. ESP32 boots and joins WiFi
2. Sends multicast announcement to `239.0.0.1:12345` with hostname and git hash
3. Server discovers device, learns IP
4. **Server connects to ESP32's TCP server** on port 3001 (ESP32 is the TCP server, not client)
5. ESP32 learns server IP from first TCP connection, uses it for UDP mic packets

## TCP Command Protocol

All commands use a 6-byte header. The server initiates TCP connections to the ESP32.

### 0xAA — Audio Playback
```
header[0]   = 0xAA
header[1:2] = mic_timeout (seconds, big-endian) — enable mic after audio finishes
header[3]   = volume (0-20, bit-shift)
header[4]   = LED fade rate (0-255)
header[5]   = compression type: 0=PCM, 2=Opus
```
Followed by length-prefixed Opus frames: `[2-byte big-endian length][Opus data]...`

A zero-length frame (`0x00 0x00`) signals end of speech — the ESP32 exits `opusDecodeTask`, clears `isPlaying`, and re-enables the mic. The TCP connection may stay open for reuse.

### 0xBB — Set LEDs
```
header[0]   = 0xBB
header[1]   = LED bitmask (bits 0-5)
header[2:4] = RGB color
```

### 0xCC — LED Blink (VAD visualization)
```
header[0]   = 0xCC
header[1]   = starting intensity (0-255)
header[2:4] = RGB color
header[5]   = fade rate
```
Also extends mic timeout if it's about to expire (VAD_MIC_EXTEND = 5s).

### 0xDD — Mic Timeout
```
header[0]   = 0xDD
header[1:2] = timeout (seconds, big-endian)
```
Used to stop mic while server is processing (thinking animation).

## FreeRTOS Task Architecture

The ESP32-S3's dual cores are used to separate concerns:

**Core 0 — Arduino loop:**
- TCP server: accepts connections, parses headers, handles PCM playback
- Touch/mute input handling
- UART debug commands

**Core 1 — Dedicated tasks:**
- `micTask` (4KB stack, priority 1): continuous I2S read → μ-law encode → UDP send
- `opusDecodeTask` (32KB stack, priority 1): created per-playback, reads TCP → Opus decode → I2S write
- `updateLedTask` (2KB stack, priority 2): 40Hz LED refresh with gamma-corrected fade

The 32KB stack for Opus decoding is necessary because the Opus decoder uses 10-20KB of stack internally.

## State Machine

Key state variables controlling behavior:

- `isPlaying` — blocks mic recording during playback
- `mic_timeout` — millis() deadline for mic recording; 0 = off
- `interruptPlayback` — set by center touch to abort current playback
- `mute` — hardware mute switch state (currently disabled via `DISABLE_HARDWARE_MUTE`)
- `serverIP` — learned from first TCP connection; `0.0.0.0` = no server yet

**Activation flow:**
1. Center touch → sets `mic_timeout` to now + 60s, green LED pulse
2. Server sends 0xCC (VAD blink) during speech → extends timeout by 5s if nearly expired
3. Server sends 0xDD (stop mic) when transcription complete → thinking animation
4. Server sends 0xAA (audio) with response → plays audio, then re-enables mic per header timeout

**Playback interruption:**
1. Center touch during playback → sets `interruptPlayback`, clears `isPlaying`
2. Opus/PCM task detects flag, stops decoding
3. Remaining TCP data drained (up to 1s) without playing
4. Mic enabled immediately for 60s

## LED System

6 NeoPixel LEDs, only the inner 4 (indices 1-4) used for animations. Edge LEDs (1, 4) dimmed by half for a softer visual.

- **Pulse-and-fade paradigm:** `setLed()` sets color, starting intensity, and fade rate. `updateLedTask` ramps intensity down at 40Hz.
- **Gamma correction:** LUT with gamma 1.8 (lower than typical 2.2 to avoid visible flicker at low PWM levels)
- **Audio-reactive:** During playback, amplitude of PCM samples drives LED brightness (sampled every 32ms, only ramps up — natural fade handles the down)

**Color semantics:**
- Green pulse: listening / mic active
- White pulse: audio playback / VAD visualization
- Red pulse: error / cannot listen (muted or no server)

## Volume Control

Bit-shift based: PCM samples are left-shifted by the volume value (0-20). Default 14. Set per-playback via the 0xAA header, configurable via NVS.

## Playback Buffering

```
TCP → tcpBuffer (512B) → wavData (2MB PSRAM) → I2S DMA → Speaker
```

- **Buffer threshold:** 4096 samples (256ms) before starting I2S playback — balances latency vs jitter resilience
- **Without PSRAM:** falls back to 1024 samples (64ms), 4KB allocation
- **I2S DMA:** 4 buffers × 512 samples, hardware-driven (no CPU polling)

## Configuration

Stored in NVS (ESP32 Preferences): WiFi credentials, server hostname, volume, mic timeout. Editable via UART config mode (`c` command).

## UART Debug Commands

`r` restart, `M` mic on 10min, `m` mic off, `W`/`w` LED test fast/slow, `L`/`l` LEDs max/off, `A` multicast announce, `c` config mode.
