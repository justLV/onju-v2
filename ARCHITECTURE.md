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

## Audio Path Details

### Microphone → Server (UDP + μ-law)

**Path:** Mic → I2S → μ-law encode → UDP → Server

**Specifications:**
- Sample rate: 16kHz mono
- Chunk size: 480 samples (30ms)
- Raw PCM: 960 bytes/chunk (32 KB/s)
- μ-law compressed: 480 bytes/chunk (16 KB/s)
- **Compression: 2x**

### Server → Speaker (TCP + Opus)

**Path:** Server → Opus encode → TCP → Opus decode → I2S → Speaker

**Specifications:**
- Sample rate: 16kHz mono
- Frame size: 320 samples (20ms)
- Raw PCM: 640 bytes/frame (32 KB/s)
- Opus compressed: ~35-50 bytes/frame (1.5-2 KB/s)
- **Compression: 14-16x**

## Design Choices

### Why μ-law for Microphone?

**Requirements:**
- Low latency for real-time conversation
- Acceptable quality for speech recognition (ASR)
- Low CPU overhead on ESP32

**μ-law advantages:**
1. ✅ **Ultra-low latency**: No buffering, sample-by-sample encoding
2. ✅ **Minimal CPU**: Simple table lookup, ~1% CPU overhead
3. ✅ **Good enough for ASR**: Speech recognition doesn't need high fidelity
4. ✅ **Stateless**: Each sample independent, no frame dependencies
5. ✅ **2x bandwidth reduction**: 16 KB/s vs 32 KB/s raw PCM

**Why not Opus for mic?**
- ❌ Frame buffering adds latency (20-60ms)
- ❌ Higher CPU overhead (10-20%)
- ❌ ASR models work fine with μ-law quality
- ❌ Overkill for one-way upstream audio

### Why Opus for Speaker?

**Requirements:**
- High quality for human listening
- Handle poor WiFi conditions (jitter, low throughput)
- Minimize bandwidth to maximize WiFi margin

**Opus advantages:**
1. ✅ **Excellent quality**: Designed for voice, much better than μ-law
2. ✅ **14-16x compression**: Critical bandwidth savings
3. ✅ **WiFi margin**: 2.2x → 30x throughput margin
4. ✅ **Jitter resistance**: Large buffering (8192 samples) smooths network hiccups
5. ✅ **Industry standard**: ElevenLabs and other TTS APIs support native Opus output

**Why not μ-law for speaker?**
- ❌ Only 2x compression (insufficient for poor WiFi)
- ❌ Noticeable quality degradation for human listening
- ❌ Still susceptible to jitter with only 2.2x WiFi margin

### Why UDP for Microphone?

**UDP advantages:**
1. ✅ **Lower latency**: No TCP handshake/ACK overhead
2. ✅ **Packet loss acceptable**: ASR models are robust to occasional dropouts
3. ✅ **Simpler**: No connection management, just blast packets
4. ✅ **Real-time friendly**: Old packets aren't retransmitted (they're already stale)

**Why not TCP?**
- ❌ Retransmissions add latency
- ❌ Head-of-line blocking delays newer audio if old packets lost
- ❌ ASR can handle gaps better than delayed old audio

### Why TCP for Speaker?

**TCP advantages:**
1. ✅ **Reliable delivery**: Every Opus frame must arrive for decoding
2. ✅ **Ordered packets**: Opus frames must be decoded in sequence
3. ✅ **Flow control**: Prevents overwhelming ESP32 buffer
4. ✅ **Opus frame framing**: Easy length-prefixed packet protocol

**Why not UDP?**
- ❌ Lost Opus frames cause decode errors
- ❌ Out-of-order packets break playback
- ❌ Opus isn't designed for packet loss (unlike Opus-in-RTP which has FEC)

## WiFi Throughput Considerations

**Measured WiFi throughput:** 553.9 kbps (worst-case in home)

### Before Opus (μ-law speaker):
```
Microphone:  16 KB/s (128 kbps) - μ-law
Speaker:     16 KB/s (128 kbps) - μ-law
Total:       32 KB/s (256 kbps)
WiFi margin: 553.9 / 256 = 2.2x
```
**Problem:** 2.2x margin insufficient for reliable operation in different locations

### After Opus (speaker only):
```
Microphone:  16 KB/s (128 kbps) - μ-law (unchanged)
Speaker:      2 KB/s (16 kbps)  - Opus
Total:       18 KB/s (144 kbps)
WiFi margin: 553.9 / 144 = 3.8x
```
**Better, but...**

### Full-duplex conversation scenario:
When speaking and listening simultaneously:
```
Total bandwidth: 16 + 16 = 32 kbps (Opus speaker + μ-law mic)
WiFi margin: 553.9 / 32 = 17.3x
```
**Much better!** But typically speaker OR mic active, not both.

### Typical usage (one-way at a time):
```
Speaking:   16 kbps (mic only)    → 34.6x margin
Listening:  16 kbps (speaker only) → 34.6x margin
```
**Excellent margin for reliable operation anywhere in home**

## ESP32 Resource Usage

### Memory (with 2MB PSRAM):
- Opus decoder: ~20 KB heap
- Opus decode task stack: 32 KB
- PCM playback buffer: 8 KB (2MB / 256 = 8192 samples)
- μ-law mic buffer: 480 bytes
- **Total: ~60 KB (3% of PSRAM)**

### CPU Usage @ 240MHz:
- Opus decoding: ~10-20% of one core (during playback)
- μ-law encoding: ~1% of one core (during recording)
- I2S/WiFi/LEDs: ~10% of one core
- **Total: ~30-40% peak usage** (plenty of headroom)

### Stack Considerations:
- Default Arduino loop task: 8KB stack
- Opus decoder internal buffers: 10-20KB stack usage
- **Solution:** Dedicated FreeRTOS task with 32KB stack for Opus decoding

## Protocol Details

### Microphone UDP Packets
```
┌────────────────────────────┐
│   480 bytes μ-law data     │  (30ms of audio)
└────────────────────────────┘
```
- No header, just raw μ-law samples
- Sent continuously when mic active
- Server decodes on reception

### Speaker TCP Stream
```
┌──────┬──────────────────────────────┐
│ 0xAA │ Header (6 bytes)             │
├──────┼──────────────────────────────┤
│ Frame 1: [2-byte len][Opus data]    │
│ Frame 2: [2-byte len][Opus data]    │
│ Frame 3: [2-byte len][Opus data]    │
│ ...                                  │
└──────────────────────────────────────┘
```

**Header format:**
```
header[0] = 0xAA                    // Audio command
header[1:2] = mic_timeout (seconds) // When to enable mic after audio
header[3] = volume (0-20)           // Bit shift for volume control
header[4] = LED fade rate (0-255)   // Visual feedback speed
header[5] = compression type        // 0=PCM, 1=μ-law, 2=Opus
```

**Opus frame format:**
```
[2-byte big-endian length][Opus compressed data]
```
- Length: 0-4000 bytes (typically 35-50 bytes for 20ms voice)
- ESP32 reads length, then reads exact frame data
- Decodes to 320 PCM samples (20ms @ 16kHz)

## Future Considerations

### Potential Improvements:
1. **Opus for microphone**: If latency becomes less critical, could achieve 16x compression upstream too
2. **Variable bitrate**: Adjust Opus bitrate based on WiFi conditions (8-24 kbps)
3. **Forward Error Correction**: Enable Opus FEC for lossy WiFi environments
4. **Adaptive buffering**: Increase buffer size if jitter detected
5. **Native Opus from TTS**: Use ElevenLabs `output_format="opus_16000"` to avoid double-encoding

### Hardware Considerations:
- ESP32-S3's dual-core architecture allows parallel mic/speaker processing
- PSRAM critical for large playback buffer (smooth WiFi jitter)
- I2S handles audio I/O without CPU intervention (DMA)
- WiFi throughput varies by location (2.4GHz congestion, walls, distance)

## Summary

The asymmetric compression strategy optimizes for the different requirements of each audio direction:

- **Microphone (μ-law/UDP):** Prioritizes latency and simplicity for ASR
- **Speaker (Opus/TCP):** Prioritizes quality and bandwidth efficiency for human listening

This design achieves:
- 🎯 High quality TTS playback
- 🎯 Low latency voice capture
- 🎯 30x+ WiFi margin for reliability
- 🎯 Minimal CPU/memory overhead
- 🎯 Jitter-resistant operation throughout home
