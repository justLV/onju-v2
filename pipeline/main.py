import argparse
import asyncio
import json
import logging
import os
import socket
import time
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")

import numpy as np
import yaml

from pipeline.audio import decode_ulaw, opus_encode, opus_frames_to_tcp_payload, pcm_to_wav
from pipeline.conversation import create_backend
from pipeline.device import Device, DeviceManager
from pipeline.protocol import send_audio, send_led_blink, send_stop_listening
from pipeline.services import asr, tts

log = logging.getLogger(__name__)


def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


async def udp_listener(config: dict, manager: DeviceManager, utterance_queue: asyncio.Queue):
    """Receive u-law audio from ESP32 devices, run VAD or PTT buffering, queue complete utterances."""
    udp_port = config["network"]["udp_port"]
    chunk_bytes = config["audio"]["chunk_size"]
    sample_rate = config["audio"]["sample_rate"]

    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", udp_port))
    sock.setblocking(False)
    log.info(f"UDP  listening on :{udp_port}")

    tcp_port = config["network"]["tcp_port"]
    dev_cfg = config["device"]

    ptt_timeout = 0.5
    min_utterance_samples = int(sample_rate * 0.3)
    last_packet_time: dict[str, float] = {}

    while True:
        # Short timeout to detect PTT release (packet stream stops)
        try:
            data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, chunk_bytes * 2), timeout=0.2)
        except asyncio.TimeoutError:
            now = time.time()
            for hostname, dev in list(manager.devices.items()):
                if hostname not in last_packet_time:
                    continue
                gap = now - last_packet_time[hostname]
                if gap < ptt_timeout:
                    continue

                # PTT device: flush buffer when packets stop
                if dev.ptt and dev.ptt_buffer:
                    audio = np.concatenate(dev.ptt_buffer)
                    dev.ptt_buffer.clear()
                    if len(audio) > min_utterance_samples:
                        log.info(f"PTT  end from {hostname} ({len(audio)/sample_rate:.1f}s)")
                        await utterance_queue.put((dev, audio))

                # VOX device: flush if VAD was recording but packets stopped
                elif not dev.ptt and dev.vad.recording:
                    audio = np.concatenate(dev.vad.buffer) if dev.vad.buffer else None
                    dev.vad.reset()
                    if audio is not None and len(audio) > min_utterance_samples:
                        log.info(f"VAD  timeout from {hostname} ({len(audio)/sample_rate:.1f}s)")
                        await utterance_queue.put((dev, audio))
            continue

        device = manager.get_by_ip(addr[0])
        if device is None and addr[0] == "127.0.0.1":
            device = manager.get_most_recent()
        if device is None:
            continue

        now = time.time()
        last_packet_time[device.hostname] = now
        pcm = decode_ulaw(data)

        if device.ptt:
            # PTT: just buffer, no VAD needed
            if device.processing:
                continue
            device.ptt_buffer.append(pcm)
        else:
            # VOX: run VAD
            utterance = device.vad.process_frame(pcm)

            # Interrupt only on actual speech (not background noise)
            if device.processing:
                if device.vad.speech_prob > config["vad"]["threshold"]:
                    device.interrupted.set()
                continue

            # LED feedback (only for VOX devices)
            # Only send a new blink when VAD sees a peak — the device
            # handles fade-down itself via updateLedTask.
            prob = device.vad.speech_prob
            new_level = int(prob * dev_cfg["led_power"]) if prob > 0.1 else 0
            if new_level > device.led_power:
                device.led_power = min(dev_cfg["led_power"], new_level)
            if now - device.led_update_time > dev_cfg["led_update_period"]:
                device.led_update_time = now
                if device.led_power > 0:
                    asyncio.create_task(
                        send_led_blink(device.ip, tcp_port, device.led_power, fade=dev_cfg["led_fade"])
                    )
                    device.led_power = 0

            if utterance is not None:
                log.info(f"VAD  utterance from {device.hostname} ({len(utterance)/sample_rate:.1f}s)")
                await utterance_queue.put((device, utterance))


async def greet_device(device: Device, config: dict):
    """Send greeting WAV to a device (Opus-encoded over TCP)."""
    dev_cfg = config["device"]
    tcp_port = config["network"]["tcp_port"]
    greeting_path = dev_cfg.get("greeting_wav")
    if not dev_cfg.get("greeting", True) or not greeting_path or not os.path.exists(greeting_path):
        return
    try:
        from pydub import AudioSegment
        audio = AudioSegment.from_file(greeting_path).set_channels(1).set_frame_rate(16000).set_sample_width(2)
        frames = opus_encode(audio.raw_data, config["audio"]["sample_rate"], config["audio"]["opus_frame_size"])
        payload = opus_frames_to_tcp_payload(frames)
        await send_audio(device.ip, tcp_port, payload,
                         mic_timeout=dev_cfg["default_mic_timeout"],
                         volume=dev_cfg["default_volume"],
                         fade=dev_cfg["led_fade"])
    except Exception as e:
        log.error(f"Failed to send greeting to {device.hostname}: {e}")


async def multicast_listener(config: dict, manager: DeviceManager):
    """Listen for ESP32 device announcements and send greeting audio."""
    mcast_group = config["network"]["multicast_group"]
    mcast_port = config["network"]["multicast_port"]

    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", mcast_port))
    group = socket.inet_aton(mcast_group)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton("0.0.0.0"))
    sock.setblocking(False)
    log.info(f"MCAST  listening on {mcast_group}:{mcast_port}")

    while True:
        data, addr = await loop.sock_recvfrom(sock, 1024)
        msg = data.decode("utf-8")
        parts = msg.split()
        hostname = parts[0]
        ptt = "PTT" in (p.upper() for p in parts)
        log.info(f"DEVICE  {hostname} ({addr[0]}) {'PTT' if ptt else 'VOX'}")
        device = manager.create_device(hostname, addr[0], ptt=ptt)
        await greet_device(device, config)


async def process_utterances(config: dict, manager: DeviceManager, utterance_queue: asyncio.Queue):
    """Process complete utterances: ASR -> Conversation -> TTS -> Opus -> TCP."""
    tcp_port = config["network"]["tcp_port"]
    dev_cfg = config["device"]
    no_speech_threshold = 0.45

    while True:
        device, audio_int16 = await utterance_queue.get()

        device.processing = True
        device.interrupted.clear()

        try:
            # Tell VOX devices to stop listening while we process.
            # Uses a 30s hold so callActive stays true on the device.
            if not device.ptt:
                await send_stop_listening(device.ip, tcp_port)

            # ASR
            pcm_bytes = audio_int16.astype(np.int16).tobytes()
            try:
                asr_result = await asr.transcribe(pcm_bytes, config)
            except Exception as e:
                log.error(f"ASR  failed ({config['asr']['url']}): {e}")
                continue
            text = asr_result.get("text", "").strip()
            nsp = asr_result.get("no_speech_prob")

            if not text or (nsp is not None and nsp > no_speech_threshold):
                log.info(f"ASR  (no speech)")
                if not device.ptt:
                    await send_audio(device.ip, tcp_port, b"",
                                     mic_timeout=dev_cfg["default_mic_timeout"],
                                     volume=0, fade=0)
                continue

            # Check for interrupt before LLM
            if device.interrupted.is_set():
                log.info(f"Interrupted before LLM")
                continue

            # Conversation
            try:
                response_text = await device.conversation.send(text)
            except Exception as e:
                log.error(f"LLM  failed: {e}")
                continue
            device.last_response = response_text
            log.info(f"LLM  {response_text}")

            # Check for interrupt before TTS
            if device.interrupted.is_set():
                log.info(f"Interrupted before TTS")
                continue

            # TTS
            try:
                pcm_response = await tts.synthesize(response_text, device.voice, config)
                log.info(f"TTS  {len(pcm_response)} bytes ({len(pcm_response)/32000:.1f}s)")
            except Exception as e:
                log.error(f"TTS  failed: {e}")
                continue

            # Check for interrupt before sending audio
            if device.interrupted.is_set():
                log.info(f"Interrupted before send")
                continue

            # Opus encode and send
            frames = opus_encode(pcm_response, config["audio"]["sample_rate"], config["audio"]["opus_frame_size"])
            payload = opus_frames_to_tcp_payload(frames)
            log.info(f"SEND  {len(frames)} opus frames to {device.ip}")
            await send_audio(device.ip, tcp_port, payload,
                             mic_timeout=dev_cfg["default_mic_timeout"],
                             volume=dev_cfg["default_volume"],
                             fade=dev_cfg["led_fade"])

        except Exception as e:
            log.error(f"Pipeline error ({device.hostname}): {e}")
            if not device.ptt:
                try:
                    await send_audio(device.ip, tcp_port, b"",
                                     mic_timeout=dev_cfg["default_mic_timeout"],
                                     volume=0, fade=0)
                except Exception:
                    pass
        finally:
            device.processing = False

        utterance_queue.task_done()


class ColorFormatter(logging.Formatter):
    GREY = "\033[90m"
    WHITE = "\033[37m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"
    RESET = "\033[0m"

    LEVEL_COLORS = {
        logging.DEBUG: GREY,
        logging.INFO: WHITE,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, self.WHITE)
        ts = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()
        return f"{self.GREY}{ts}{self.RESET}  {color}{msg}{self.RESET}"


async def control_server(config: dict, manager: DeviceManager):
    """Tiny HTTP endpoint for runtime device management."""
    port = config.get("network", {}).get("control_port", 3002)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode().strip().split()
            if len(parts) < 2:
                writer.close()
                return
            method, path = parts[0], parts[1]

            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode().partition(":")
                headers[k.strip().lower()] = v.strip()

            body = b""
            if cl := int(headers.get("content-length", 0)):
                body = await reader.readexactly(cl)

            if path == "/devices" and method == "GET":
                data = {k: v.to_dict() for k, v in manager.devices.items()}
                resp = json.dumps(data, indent=2)
                _http_respond(writer, 200, resp)

            elif path == "/devices" and method == "POST":
                payload = json.loads(body)
                ip = payload["ip"]
                hostname = payload.get("hostname", f"manual-{ip.replace('.', '-')}")
                ptt = payload.get("ptt", False)
                device = manager.create_device(hostname, ip, ptt=ptt)
                log.info(f"DEVICE  {hostname} ({ip}) {'PTT' if ptt else 'VOX'} (control)")
                asyncio.create_task(greet_device(device, config))
                _http_respond(writer, 201, json.dumps({"hostname": hostname, "ip": ip, "ptt": ptt}))

            elif path == "/devices" and method == "DELETE":
                payload = json.loads(body)
                hostname = payload.get("hostname")
                if hostname and hostname in manager.devices:
                    del manager.devices[hostname]
                    log.info(f"DEVICE  {hostname} removed (control)")
                    _http_respond(writer, 200, json.dumps({"removed": hostname}))
                else:
                    _http_respond(writer, 404, json.dumps({"error": "not found"}))

            else:
                _http_respond(writer, 404, "not found")

            await writer.drain()
        except Exception as e:
            log.debug(f"Control request error: {e}")
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info(f"CTRL listening on :{port}")
    async with server:
        await server.serve_forever()


def _http_respond(writer: asyncio.StreamWriter, status: int, body: str):
    reason = {200: "OK", 201: "Created", 404: "Not Found"}.get(status, "OK")
    writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())


async def warmup(config: dict):
    """Validate conversation backend and TTS are reachable."""
    log.info("Warming up conversation backend and TTS...")

    async def _warmup_conversation():
        t0 = time.time()
        try:
            backend = create_backend(config, "_warmup")
            text = await backend.send("Hi")
            if not text.strip():
                log.error(f"LLM  warmup FAILED ({time.time() - t0:.1f}s): empty response")
            else:
                log.info(f"LLM  warmup OK  ({time.time() - t0:.1f}s) -> {text.strip()!r}")
        except Exception as e:
            log.error(f"LLM  warmup FAILED ({time.time() - t0:.1f}s): {e}")

    async def _warmup_tts():
        t0 = time.time()
        try:
            pcm = await tts.synthesize("Hello.", "default", config)
            duration_ms = len(pcm) / (16000 * 2) * 1000
            if duration_ms < 100:
                log.error(f"TTS  warmup FAILED ({time.time() - t0:.1f}s): audio too short ({duration_ms:.0f}ms)")
            else:
                log.info(f"TTS  warmup OK  ({time.time() - t0:.1f}s) -> {duration_ms:.0f}ms audio")
        except Exception as e:
            log.error(f"TTS  warmup FAILED ({time.time() - t0:.1f}s): {e}")

    await asyncio.gather(_warmup_conversation(), _warmup_tts())
    log.info("Warmup complete")


def _parse_device_arg(entry: str) -> tuple[str, str, bool]:
    """Parse device arg like 'name=ip:ptt' or 'ip:ptt' or 'ip'."""
    ptt = False
    if entry.endswith(":ptt"):
        ptt = True
        entry = entry[:-4]
    parts = entry.split("=", 1)
    if len(parts) == 2:
        hostname, ip = parts
    else:
        ip = parts[0]
        hostname = f"manual-{ip.replace('.', '-')}"
    return hostname, ip, ptt


async def main(config_path: str = None, do_warmup: bool = False, devices: list[str] = None):
    config = load_config(config_path)

    log_level = getattr(logging, config.get("logging", {}).get("level", "INFO"))
    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter())
    logging.basicConfig(level=log_level, handlers=[handler])

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    manager = DeviceManager(config)

    startup_greets = []
    for entry in (devices or []):
        hostname, ip, ptt = _parse_device_arg(entry)
        device = manager.create_device(hostname, ip, ptt=ptt)
        log.info(f"DEVICE  {hostname} ({ip}) {'PTT' if ptt else 'VOX'} (manual)")
        startup_greets.append(greet_device(device, config))
    if startup_greets:
        await asyncio.gather(*startup_greets)

    if do_warmup:
        await warmup(config)

    utterance_queue = asyncio.Queue()

    log.info("Pipeline server starting")
    await asyncio.gather(
        udp_listener(config, manager, utterance_queue),
        multicast_listener(config, manager),
        process_utterances(config, manager, utterance_queue),
        control_server(config, manager),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Voice pipeline server")
    parser.add_argument("config", nargs="?", default=None, help="Path to config YAML")
    parser.add_argument("--warmup", action="store_true", help="Warmup LLM and TTS on startup")
    parser.add_argument("--device", action="append", dest="devices", metavar="[NAME=]IP[:ptt]",
                        help="Register device (e.g. --device onju=192.168.1.50:ptt)")
    args = parser.parse_args()
    asyncio.run(main(args.config, do_warmup=args.warmup, devices=args.devices))
