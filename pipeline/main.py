import asyncio
import atexit
import logging
import os
import socket
import struct
import sys
import time

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
    log.info(f"UDP listening on :{udp_port}")

    tcp_port = config["network"]["tcp_port"]
    dev_cfg = config["device"]

    while True:
        data, addr = await loop.sock_recvfrom(sock, chunk_bytes * 2)
        device = manager.get_by_ip(addr[0])
        if device is None:
            continue

        pcm = decode_ulaw(data)

        # LED visualization
        is_speech = device.vad.is_speech_now
        if is_speech:
            device.led_power = min(255, device.led_power + dev_cfg["led_power"])
        now = time.time()
        if now - device.led_update_time > dev_cfg["led_update_period"]:
            device.led_update_time = now
            if device.led_power > 0:
                asyncio.create_task(
                    send_led_blink(device.ip, tcp_port, device.led_power, fade=dev_cfg["led_fade"])
                )
            device.led_power = 0

        # VAD
        utterance = device.vad.process_frame(pcm)
        if utterance is not None:
            log.info(f"Utterance from {device.hostname} ({len(utterance)/config['audio']['sample_rate']:.1f}s)")
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
    log.info(f"Multicast listening on {mcast_group}:{mcast_port}")

    while True:
        data, addr = await loop.sock_recvfrom(sock, 1024)
        msg = data.decode("utf-8")
        hostname = msg.split()[0]
        log.info(f"Device announced: {hostname} from {addr[0]}")

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
            nsp = asr_result.get("no_speech_prob", 1.0)

            if not text or nsp > no_speech_threshold:
                log.debug(f"Ignoring non-speech from {device.hostname} (nsp={nsp:.2f})")
                continue

            log.info(f"[{device.hostname}] User: {text}")

            # LLM
            device.messages.append({"role": "user", "content": text})
            response_text = await llm.chat(llm_client, device, config)
            device.last_response = response_text
            device.prune_messages()

            log.info(f"[{device.hostname}] Assistant: {response_text}")

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

        utterance_queue.task_done()


async def main(config_path: str = None):
    config = load_config(config_path)

    log_level = getattr(logging, config.get("logging", {}).get("level", "INFO"))
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    manager = DeviceManager(config)
    atexit.register(manager.save)

    llm_client = llm.make_client(config)

    utterance_queue = asyncio.Queue()

    log.info("Starting pipeline server")
    await asyncio.gather(
        udp_listener(config, manager, utterance_queue),
        multicast_listener(config, manager),
        process_utterances(config, manager, utterance_queue, llm_client),
    )


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(main(config_path))
