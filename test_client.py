"""
Test client that emulates an ESP32 onju-voice device.
Runs a TCP server (receives audio/commands from pipeline), records mic audio,
encodes as u-law, and streams over UDP to the pipeline server.

Usage:
    python test_client.py [server_ip]      # default: 127.0.0.1
    python test_client.py --no-mic         # playback only, no mic
"""
import argparse
import ctypes.util
import io
import os
import socket
import struct
import sys
import threading
import time

# macOS: help ctypes find Homebrew's libopus
if sys.platform == "darwin" and ctypes.util.find_library("opus") is None:
    _brew_lib = "/opt/homebrew/lib"
    if os.path.exists(os.path.join(_brew_lib, "libopus.dylib")):
        os.environ.setdefault("DYLD_LIBRARY_PATH", _brew_lib)

import numpy as np
import opuslib
import pyaudio

# Audio settings matching ESP32
SAMPLE_RATE = 16000
CHUNK_SIZE = 480        # 30ms at 16kHz
UDP_PORT = 3000
TCP_PORT = 3001
MULTICAST_GROUP = "239.0.0.1"
MULTICAST_PORT = 12345
HOSTNAME = "test-client"

# u-law compression (matching ESP32 audio_compression.h)
def encode_ulaw(pcm_int16: np.ndarray) -> bytes:
    samples = pcm_int16.astype(np.int32)
    sign = np.where(samples < 0, 0x80, 0)
    magnitude = np.abs(samples)
    magnitude = np.clip(magnitude, 0, 32635)
    magnitude = magnitude + 0x84
    exponent = np.floor(np.log2(magnitude)).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    ulaw = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8).tobytes()

# u-law decompression table (same as pipeline/audio.py)
ULAW_TABLE = np.array([
    -32124,-31100,-30076,-29052,-28028,-27004,-25980,-24956,
    -23932,-22908,-21884,-20860,-19836,-18812,-17788,-16764,
    -15996,-15484,-14972,-14460,-13948,-13436,-12924,-12412,
    -11900,-11388,-10876,-10364, -9852, -9340, -8828, -8316,
     -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
     -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
     -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
     -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
     -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
     -1372, -1308, -1244, -1180, -1116, -1052,  -988,  -924,
      -876,  -844,  -812,  -780,  -748,  -716,  -684,  -652,
      -620,  -588,  -556,  -524,  -492,  -460,  -428,  -396,
      -372,  -356,  -340,  -324,  -308,  -292,  -276,  -260,
      -244,  -228,  -212,  -196,  -180,  -164,  -148,  -132,
      -120,  -112,  -104,   -96,   -88,   -80,   -72,   -64,
       -56,   -48,   -40,   -32,   -24,   -16,    -8,     0,
     32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
     23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
     15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
     11900, 11388, 10876, 10364,  9852,  9340,  8828,  8316,
      7932,  7676,  7420,  7164,  6908,  6652,  6396,  6140,
      5884,  5628,  5372,  5116,  4860,  4604,  4348,  4092,
      3900,  3772,  3644,  3516,  3388,  3260,  3132,  3004,
      2876,  2748,  2620,  2492,  2364,  2236,  2108,  1980,
      1884,  1820,  1756,  1692,  1628,  1564,  1500,  1436,
      1372,  1308,  1244,  1180,  1116,  1052,   988,   924,
       876,   844,   812,   780,   748,   716,   684,   652,
       620,   588,   556,   524,   492,   460,   428,   396,
       372,   356,   340,   324,   308,   292,   276,   260,
       244,   228,   212,   196,   180,   164,   148,   132,
       120,   112,   104,    96,    88,    80,    72,    64,
        56,    48,    40,    32,    24,    16,     8,     0,
], dtype=np.int16)


class TestClient:
    def __init__(self, server_ip: str, enable_mic: bool = True):
        self.server_ip = server_ip
        self.enable_mic = enable_mic
        self.mic_active = False
        self.mic_timeout = 0
        self.running = True

        self.pa = pyaudio.PyAudio()
        self.opus_decoder = opuslib.Decoder(SAMPLE_RATE, 1)

        # Output stream for playback
        self.out_stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=CHUNK_SIZE,
        )

    def tcp_server(self):
        """Listen for incoming TCP connections from the pipeline server (like ESP32 port 3001)."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", TCP_PORT))
        srv.listen(1)
        srv.settimeout(1.0)
        print(f"TCP server listening on :{TCP_PORT}")

        while self.running:
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue

            # Read 6-byte header
            header = b""
            while len(header) < 6:
                chunk = client.recv(6 - len(header))
                if not chunk:
                    break
                header += chunk

            if len(header) < 6:
                client.close()
                continue

            cmd = header[0]

            if cmd == 0xAA:
                timeout = (header[1] << 8) | header[2]
                volume = header[3]
                fade = header[4]
                compression = header[5]
                print(f"  AUDIO: timeout={timeout}s vol={volume} compression={compression}")
                self._handle_audio(client, compression, volume)
                self.mic_timeout = time.time() + max(timeout, 60)
                self.mic_active = True
                print(f"  Mic enabled for {max(timeout, 60)}s")

            elif cmd == 0xBB:
                bitmask = header[1]
                r, g, b = header[2], header[3], header[4]
                print(f"  LED SET: mask={bitmask:#04x} rgb=({r},{g},{b})")

            elif cmd == 0xCC:
                intensity = header[1]
                r, g, b = header[2], header[3], header[4]
                fade = header[5]
                bar_len = intensity * 30 // 255
                bar = "\033[32m" + "█" * bar_len + "\033[90m" + "░" * (30 - bar_len) + "\033[0m"
                print(f"\r  VAD {bar} {intensity:3d}", end="", flush=True)
                # Extend mic timeout if nearly expired (like ESP32)
                if self.mic_active and self.mic_timeout > time.time():
                    if self.mic_timeout < time.time() + 5:
                        self.mic_timeout = time.time() + 5

            elif cmd == 0xDD:
                timeout = (header[1] << 8) | header[2]
                self.mic_timeout = time.time() + timeout
                if timeout == 0:
                    self.mic_active = False
                    print("\n  MIC STOP (server processing)")
                else:
                    print(f"\n  MIC TIMEOUT: {timeout}s")

            else:
                print(f"  Unknown command: {cmd:#04x}")

            client.close()

    def _handle_audio(self, client: socket.socket, compression: int, volume: int):
        """Receive and play audio from TCP connection."""
        if compression == 2:
            self._play_opus(client, volume)
        elif compression == 0:
            self._play_pcm(client, volume)
        else:
            print(f"  Unsupported compression type: {compression}")

    def _play_opus(self, client: socket.socket, volume: int):
        """Decode Opus frames and play through speakers."""
        frame_count = 0
        total_samples = 0

        while True:
            # Read 2-byte frame length
            len_buf = b""
            while len(len_buf) < 2:
                chunk = client.recv(2 - len(len_buf))
                if not chunk:
                    break
                len_buf += chunk
            if len(len_buf) < 2:
                break

            frame_len = struct.unpack('>H', len_buf)[0]
            if frame_len == 0 or frame_len > 4000:
                break

            # Read opus frame
            frame_data = b""
            while len(frame_data) < frame_len:
                chunk = client.recv(frame_len - len(frame_data))
                if not chunk:
                    break
                frame_data += chunk
            if len(frame_data) < frame_len:
                break

            # Decode and play
            pcm = self.opus_decoder.decode(frame_data, 320)
            self.out_stream.write(pcm)

            frame_count += 1
            total_samples += 320

        duration = total_samples / SAMPLE_RATE
        print(f"  Played {frame_count} Opus frames ({duration:.1f}s)")

    def _play_pcm(self, client: socket.socket, volume: int):
        """Read raw PCM and play through speakers."""
        total_bytes = 0
        while True:
            data = client.recv(4096)
            if not data:
                break
            self.out_stream.write(data)
            total_bytes += len(data)

        duration = total_bytes / (SAMPLE_RATE * 2)
        print(f"  Played {total_bytes} PCM bytes ({duration:.1f}s)")

    def mic_streamer(self):
        """Record from mic, encode as u-law, send over UDP to server (like ESP32 mic task)."""
        if not self.enable_mic:
            return

        in_stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        print(f"Mic streamer ready -> UDP {self.server_ip}:{UDP_PORT}")

        packets_sent = 0
        was_active = False

        while self.running:
            data = in_stream.read(CHUNK_SIZE, exception_on_overflow=False)

            if not self.mic_active or time.time() > self.mic_timeout:
                if was_active:
                    print(f"Mic stopped ({packets_sent} packets sent)")
                    was_active = False
                    packets_sent = 0
                    self.mic_active = False
                continue

            if not was_active:
                print("Mic started streaming")
                was_active = True

            pcm = np.frombuffer(data, dtype=np.int16)

            # DC offset removal
            pcm = pcm - np.mean(pcm).astype(np.int16)

            # u-law encode and send
            ulaw = encode_ulaw(pcm)
            sock.sendto(ulaw, (self.server_ip, UDP_PORT))
            packets_sent += 1

        in_stream.close()
        sock.close()

    def announce(self):
        """Send multicast announcement (like ESP32 on boot)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        msg = f"{HOSTNAME} test-client".encode()
        sock.sendto(msg, (MULTICAST_GROUP, MULTICAST_PORT))
        sock.close()
        print(f"Multicast announcement sent as '{HOSTNAME}'")

    def run(self):
        print(f"Test client starting (server: {self.server_ip})")
        print(f"  TCP server: :{TCP_PORT}")
        print(f"  UDP target: {self.server_ip}:{UDP_PORT}")
        print(f"  Mic: {'enabled' if self.enable_mic else 'disabled'}")
        print()

        self.announce()

        threads = [
            threading.Thread(target=self.tcp_server, daemon=True),
        ]
        if self.enable_mic:
            threads.append(threading.Thread(target=self.mic_streamer, daemon=True))

        for t in threads:
            t.start()

        try:
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nShutting down...")
            self.running = False

        self.out_stream.close()
        self.pa.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ESP32 onju-voice test client")
    parser.add_argument("server", nargs="?", default="127.0.0.1", help="Pipeline server IP")
    parser.add_argument("--no-mic", action="store_true", help="Disable mic recording")
    args = parser.parse_args()

    client = TestClient(args.server, enable_mic=not args.no_mic)
    client.run()
