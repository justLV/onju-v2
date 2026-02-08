# ESP32 Audio Streaming Test Guide

## Quick Start

### 1. Configure ESP32 Settings

In `onjuino/onjuino.ino`, adjust these settings (lines 78-84):

```cpp
#define USE_COMPRESSION true        // Enable μ-law compression (2x bandwidth reduction)
#define USE_LOCAL_VAD true          // Enable local VAD to sleep when silent
#define VAD_RMS_THRESHOLD 3000      // RMS threshold to detect voice (tune based on your mic)
#define VAD_SILENCE_FRAMES 100      // Frames of silence before sleep (100 * 32ms = 3.2 seconds)
#define VAD_WAKEUP_FRAMES 2         // Frames of voice to wake up (2 * 32ms = 64ms)
```

**Testing configurations:**

| Test | USE_COMPRESSION | USE_LOCAL_VAD | Expected Bandwidth |
|------|----------------|---------------|-------------------|
| Baseline | false | false | ~32 kbps continuous |
| Compression only | true | false | ~16 kbps continuous |
| VAD only | false | true | ~32 kbps when talking |
| Both (optimal) | true | true | ~16 kbps when talking |

### 2. Flash ESP32

```bash
# Open Arduino IDE, select your board, upload onjuino/onjuino.ino
```

### 3. Run Test Receiver on Mac

Install dependencies:
```bash
pip3 install numpy
```

Run receiver:
```bash
# Auto-detect compression mode
python3 test_mic_receiver.py --duration 10 --output test.wav

# Or specify if you know the mode
python3 test_mic_receiver.py --compressed --duration 10 --output test_compressed.wav
```

### 4. Analyze Results

The receiver will show real-time stats:
```
[    5.1s] Packets: 167 | Bandwidth: 15.8 kbps | RMS: 2847 | Mode: μ-law
```

After recording, you'll see:
```
Recording complete!
Duration:          10.02 seconds
WAV file size:     320.6 KB
Bytes transmitted: 160.3 KB
Compression ratio: 0.50x
Average bandwidth: 15.9 kbps
Packets received:  334
Packet loss:       0.0%
```

## Tuning VAD Threshold

The `VAD_RMS_THRESHOLD` value depends on your microphone sensitivity and ambient noise:

1. **Test ambient noise:**
   ```bash
   # Record silence, watch RMS values
   python3 test_mic_receiver.py --duration 5
   ```
   Note the RMS during silence (e.g., 500-1000)

2. **Test speaking:**
   ```bash
   # Record yourself talking, watch RMS values
   python3 test_mic_receiver.py --duration 5
   ```
   Note the RMS while speaking (e.g., 3000-8000)

3. **Set threshold between them:**
   ```cpp
   // If silence = 800, speech = 4000, set threshold around 2000-2500
   #define VAD_RMS_THRESHOLD 2500
   ```

## Bandwidth Comparison

| Configuration | Bandwidth | Power Saving | Audio Quality |
|--------------|-----------|--------------|---------------|
| Raw PCM, always on | 32 kbps | None | Perfect |
| μ-law, always on | 16 kbps | None | Good (telephony quality) |
| Raw PCM, VAD | ~10 kbps avg* | Moderate | Perfect |
| μ-law, VAD | ~5 kbps avg* | High | Good |

*Assuming 30% voice activity (typical conversation)

## Compression Quality Check

Listen to the output WAV files:
```bash
# Mac built-in player
afplay test.wav
afplay test_compressed.wav

# Compare side-by-side
```

μ-law quality should be:
- ✅ Clear speech
- ✅ Good for voice recognition (Whisper handles it well)
- ⚠️ Slightly muffled compared to raw PCM
- ⚠️ Not suitable for music

## Troubleshooting

**No packets received:**
- Check ESP32 Serial output for IP address
- Verify ESP32 and Mac are on same network
- Check firewall settings

**High packet loss:**
- Check WiFi signal strength
- Reduce `VAD_SILENCE_FRAMES` to keep connection active
- Try raw PCM mode first (simpler debugging)

**VAD not working:**
- Adjust `VAD_RMS_THRESHOLD` (see tuning section above)
- Check Serial monitor for "VAD: Woke up" / "VAD: Sleeping" messages
- Set `USE_LOCAL_VAD false` to test without VAD

**Compression artifacts:**
- μ-law is lossy - some quality loss is normal
- If unacceptable, use `USE_COMPRESSION false`
- Or try ADPCM (4x compression, better quality - future work)

## Next Steps

Once basic UDP streaming is working:
1. Integrate with your existing server.py VAD pipeline
2. Update server to handle compressed packets
3. Consider WebSocket for playback direction
4. Add streaming TTS for lower latency

## Server Integration

Update `server/server.py` to handle compression:

```python
import numpy as np

# Add μ-law decode table (same as test receiver)
ULAW_DECODE_TABLE = np.array([...])

def decode_ulaw(ulaw_bytes):
    return ULAW_DECODE_TABLE[np.frombuffer(ulaw_bytes, dtype=np.uint8)]

# In listen_detect function:
data, addr = sock.recvfrom(2048)

if len(data) == 512:  # Compressed
    samples = decode_ulaw(data)
elif len(data) == 1024:  # Raw
    samples = np.frombuffer(data, dtype=np.int16)

# Continue with existing VAD pipeline...
```
