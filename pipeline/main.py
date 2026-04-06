import argparse
import asyncio
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
from pipeline.vad import VAD

log = logging.getLogger(__name__)


def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


async def udp_listener(config: dict, manager: DeviceManager, utterance_queue: asyncio.Queue):
    """Receive u-law audio from ESP32 devices, run VAD, queue complete utterances."""
    udp_port = config["network"]["udp_port"]
    chunk_bytes = config["audio"]["chunk_size"]

    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", udp_port))
    sock.setblocking(False)
    log.info(f"UDP  listening on :{udp_port}")

    tcp_port = config["network"]["tcp_port"]
    dev_cfg = config["device"]

    while True:
        data, addr = await loop.sock_recvfrom(sock, chunk_bytes * 2)
        device = manager.get_by_ip(addr[0])
        if device is None and addr[0] == "127.0.0.1":
            device = manager.get_most_recent()
        if device is None:
            continue

        pcm = decode_ulaw(data)

        utterance = device.vad.process_frame(pcm)

        prob = device.vad.speech_prob
        if prob > 0.1:
            device.led_power = min(255, int(prob * 255))
        now = time.time()
        if now - device.led_update_time > dev_cfg["led_update_period"]:
            device.led_update_time = now
            if device.led_power > 0:
                asyncio.create_task(
                    send_led_blink(device.ip, tcp_port, device.led_power, fade=dev_cfg["led_fade"])
                )
            device.led_power = 0
        if utterance is not None:
            log.info(f"VAD  utterance from {device.hostname}  ({len(utterance)/config['audio']['sample_rate']:.1f}s)")
            await utterance_queue.put((device, utterance))


async def multicast_listener(config: dict, manager: DeviceManager):
    """Listen for ESP32 device announcements and send greeting audio."""
    mcast_group = config["network"]["multicast_group"]
    mcast_port = config["network"]["multicast_port"]
    tcp_port = config["network"]["tcp_port"]
    dev_cfg = config["device"]

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
        hostname = msg.split()[0]
        log.info(f"DEVICE  {hostname} ({addr[0]})")

        device = manager.create_device(hostname, addr[0])

        greeting_path = dev_cfg.get("greeting_wav")
        if greeting_path and os.path.exists(greeting_path):
            try:
                from pydub import AudioSegment
                audio = AudioSegment.from_file(greeting_path).set_channels(1).set_frame_rate(16000).set_sample_width(2)
                pcm_data = audio.raw_data
                frames = opus_encode(pcm_data, config["audio"]["sample_rate"], config["audio"]["opus_frame_size"])
                payload = opus_frames_to_tcp_payload(frames)
                await send_audio(device.ip, tcp_port, payload,
                                 mic_timeout=dev_cfg["default_mic_timeout"],
                                 volume=dev_cfg["default_volume"],
                                 fade=dev_cfg["led_fade"])
            except Exception as e:
                log.error(f"Failed to send greeting to {hostname}: {e}")


async def process_utterances(config: dict, manager: DeviceManager, utterance_queue: asyncio.Queue):
    """Process complete utterances: ASR -> Conversation -> TTS -> Opus -> TCP."""
    tcp_port = config["network"]["tcp_port"]
    dev_cfg = config["device"]
    no_speech_threshold = 0.45

    while True:
        device, audio_int16 = await utterance_queue.get()

        try:
            await send_stop_listening(device.ip, tcp_port)

            # ASR
            pcm_bytes = audio_int16.astype(np.int16).tobytes()
            asr_result = await asr.transcribe(pcm_bytes, config)
            text = asr_result.get("text", "").strip()
            nsp = asr_result.get("no_speech_prob")

            if not text or (nsp is not None and nsp > no_speech_threshold):
                log.info(f"ASR  (no speech, resuming mic)")
                await send_audio(device.ip, tcp_port, b"",
                                 mic_timeout=dev_cfg["default_mic_timeout"],
                                 volume=0, fade=0)
                continue

            # Conversation (backend handles history, LLM call, pruning)
            response_text = await device.conversation.send(text)
            device.last_response = response_text

            log.info(f"LLM  {response_text}")

            # TTS
            pcm_response = await tts.synthesize(response_text, device.voice, config)

            # Opus encode and send
            frames = opus_encode(pcm_response, config["audio"]["sample_rate"], config["audio"]["opus_frame_size"])
            payload = opus_frames_to_tcp_payload(frames)
            await send_audio(device.ip, tcp_port, payload,
                             mic_timeout=dev_cfg["default_mic_timeout"],
                             volume=dev_cfg["default_volume"],
                             fade=dev_cfg["led_fade"])

        except Exception as e:
            log.error(f"Error processing utterance from {device.hostname}: {e}", exc_info=True)
            try:
                await send_audio(device.ip, tcp_port, b"",
                                 mic_timeout=dev_cfg["default_mic_timeout"],
                                 volume=0, fade=0)
            except Exception:
                pass

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


async def main(config_path: str = None, do_warmup: bool = False):
    config = load_config(config_path)

    log_level = getattr(logging, config.get("logging", {}).get("level", "INFO"))
    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter())
    logging.basicConfig(level=log_level, handlers=[handler])

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    manager = DeviceManager(config)

    if do_warmup:
        await warmup(config)

    utterance_queue = asyncio.Queue()

    log.info("Pipeline server starting")
    await asyncio.gather(
        udp_listener(config, manager, utterance_queue),
        multicast_listener(config, manager),
        process_utterances(config, manager, utterance_queue),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Voice pipeline server")
    parser.add_argument("config", nargs="?", default=None, help="Path to config YAML")
    parser.add_argument("--warmup", action="store_true", help="Warmup LLM and TTS on startup")
    args = parser.parse_args()
    asyncio.run(main(args.config, do_warmup=args.warmup))
