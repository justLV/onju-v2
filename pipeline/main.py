import asyncio
import atexit
import logging
import os
import socket
import struct
import sys
import time
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning, module="pydub")

import numpy as np
import yaml

from pipeline.audio import decode_ulaw, opus_encode, opus_frames_to_tcp_payload, pcm_to_wav
from pipeline.device import Device, DeviceManager
from pipeline.protocol import send_audio, send_led_blink, send_stop_listening
from pipeline.services import asr, llm, tts
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
    chunk_bytes = config["audio"]["chunk_size"]  # u-law: 1 byte per sample

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
            # Localhost test client: multicast announces from LAN IP but UDP comes from loopback
            device = manager.get_most_recent()
        if device is None:
            continue

        pcm = decode_ulaw(data)

        # VAD
        utterance = device.vad.process_frame(pcm)

        # LED visualization (proportional to speech probability)
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

        # Send greeting WAV as Opus
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


async def process_utterances(config: dict, manager: DeviceManager, utterance_queue: asyncio.Queue, llm_client):
    """Process complete utterances: ASR -> LLM -> TTS -> Opus -> TCP."""
    tcp_port = config["network"]["tcp_port"]
    dev_cfg = config["device"]
    no_speech_threshold = 0.45

    while True:
        device, audio_int16 = await utterance_queue.get()

        try:
            # Stop mic while processing
            await send_stop_listening(device.ip, tcp_port)

            # ASR
            pcm_bytes = audio_int16.astype(np.int16).tobytes()
            asr_result = await asr.transcribe(pcm_bytes, config)
            text = asr_result.get("text", "").strip()
            nsp = asr_result.get("no_speech_prob")

            if not text or (nsp is not None and nsp > no_speech_threshold):
                log.info(f"ASR  (no speech, resuming mic)")
                # Re-enable mic so device can keep listening
                await send_audio(device.ip, tcp_port, b"",
                                 mic_timeout=dev_cfg["default_mic_timeout"],
                                 volume=0, fade=0)
                continue

            # LLM
            device.messages.append({"role": "user", "content": text})
            response_text = await llm.chat(llm_client, device, config)
            device.last_response = response_text
            device.prune_messages()

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
            # Re-enable mic after errors so device doesn't get stuck
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


async def main(config_path: str = None):
    config = load_config(config_path)

    log_level = getattr(logging, config.get("logging", {}).get("level", "INFO"))
    handler = logging.StreamHandler()
    handler.setFormatter(ColorFormatter())
    logging.basicConfig(level=log_level, handlers=[handler])

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    manager = DeviceManager(config)
    atexit.register(manager.save)

    llm_client = llm.make_client(config)

    utterance_queue = asyncio.Queue()

    log.info("Pipeline server starting")
    await asyncio.gather(
        udp_listener(config, manager, utterance_queue),
        multicast_listener(config, manager),
        process_utterances(config, manager, utterance_queue, llm_client),
    )


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(config_path))
