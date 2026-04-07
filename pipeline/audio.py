import ctypes.util
import io
import os
import struct
import sys

# macOS: help ctypes find Homebrew's libopus
if sys.platform == "darwin" and ctypes.util.find_library("opus") is None:
    _brew_lib = "/opt/homebrew/lib"
    if os.path.exists(os.path.join(_brew_lib, "libopus.dylib")):
        os.environ.setdefault("DYLD_LIBRARY_PATH", _brew_lib)

import numpy as np
import opuslib
from scipy.io.wavfile import write as wav_write

# u-law decompression table (ITU-T G.711)
ULAW_TABLE = np.array([
    -32124, -31100, -30076, -29052, -28028, -27004, -25980, -24956,
    -23932, -22908, -21884, -20860, -19836, -18812, -17788, -16764,
    -15996, -15484, -14972, -14460, -13948, -13436, -12924, -12412,
    -11900, -11388, -10876, -10364,  -9852,  -9340,  -8828,  -8316,
     -7932,  -7676,  -7420,  -7164,  -6908,  -6652,  -6396,  -6140,
     -5884,  -5628,  -5372,  -5116,  -4860,  -4604,  -4348,  -4092,
     -3900,  -3772,  -3644,  -3516,  -3388,  -3260,  -3132,  -3004,
     -2876,  -2748,  -2620,  -2492,  -2364,  -2236,  -2108,  -1980,
     -1884,  -1820,  -1756,  -1692,  -1628,  -1564,  -1500,  -1436,
     -1372,  -1308,  -1244,  -1180,  -1116,  -1052,   -988,   -924,
      -876,   -844,   -812,   -780,   -748,   -716,   -684,   -652,
      -620,   -588,   -556,   -524,   -492,   -460,   -428,   -396,
      -372,   -356,   -340,   -324,   -308,   -292,   -276,   -260,
      -244,   -228,   -212,   -196,   -180,   -164,   -148,   -132,
      -120,   -112,   -104,    -96,    -88,    -80,    -72,    -64,
       -56,    -48,    -40,    -32,    -24,    -16,     -8,      0,
     32124,  31100,  30076,  29052,  28028,  27004,  25980,  24956,
     23932,  22908,  21884,  20860,  19836,  18812,  17788,  16764,
     15996,  15484,  14972,  14460,  13948,  13436,  12924,  12412,
     11900,  11388,  10876,  10364,   9852,   9340,   8828,   8316,
      7932,   7676,   7420,   7164,   6908,   6652,   6396,   6140,
      5884,   5628,   5372,   5116,   4860,   4604,   4348,   4092,
      3900,   3772,   3644,   3516,   3388,   3260,   3132,   3004,
      2876,   2748,   2620,   2492,   2364,   2236,   2108,   1980,
      1884,   1820,   1756,   1692,   1628,   1564,   1500,   1436,
      1372,   1308,   1244,   1180,   1116,   1052,    988,    924,
       876,    844,    812,    780,    748,    716,    684,    652,
       620,    588,    556,    524,    492,    460,    428,    396,
       372,    356,    340,    324,    308,    292,    276,    260,
       244,    228,    212,    196,    180,    164,    148,    132,
       120,    112,    104,     96,     88,     80,     72,     64,
        56,     48,     40,     32,     24,     16,      8,      0,
], dtype=np.int16)


def decode_ulaw(data: bytes) -> np.ndarray:
    indices = np.frombuffer(data, dtype=np.uint8)
    return ULAW_TABLE[indices]


def pcm_to_wav(samples: np.ndarray, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    wav_write(buf, rate, samples.astype(np.int16))
    return buf.getvalue()


def opus_encode(pcm_data: bytes, sample_rate: int = 16000, frame_size: int = 320) -> list[bytes]:
    encoder = opuslib.Encoder(sample_rate, 1, opuslib.APPLICATION_VOIP)
    frame_bytes = frame_size * 2  # 16-bit mono
    frames = []
    for i in range(0, len(pcm_data), frame_bytes):
        chunk = pcm_data[i:i + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk += b'\x00' * (frame_bytes - len(chunk))
        frames.append(encoder.encode(chunk, frame_size))
    return frames


def opus_frames_to_tcp_payload(opus_frames: list[bytes]) -> bytes:
    parts = []
    for frame in opus_frames:
        parts.append(struct.pack('>H', len(frame)))
        parts.append(frame)
    parts.append(struct.pack('>H', 0))  # end-of-speech marker
    return b''.join(parts)
