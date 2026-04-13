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
from pipeline.conversation import create_backend, sentence_chunks
from pipeline.conversation import stall as stall_mod
from pipeline.device import Device, DeviceManager
from pipeline.protocol import send_audio, send_led_blink, open_led_connection, write_led_blink, close_led_connection
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
    was_recording: dict[str, bool] = {}
    max_led_intensity = 150

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
                    if dev.vad_writer is not None:
                        await close_led_connection(dev.vad_writer)
                        dev.vad_writer = None
                    was_recording[hostname] = False
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
            # PTT frames arriving while we're still responding means the user
            # pressed the button to interrupt. The button press is the intent
            # signal; keep buffering so these frames become the next utterance
            # once the current turn bails out.
            if device.processing and not device.interrupted.is_set():
                log.info(f"PTT  interrupt from {device.hostname}")
                device.interrupted.set()
            device.ptt_buffer.append(pcm)
        else:
            # VOX: run VAD
            utterance = device.vad.process_frame(pcm)

            # Interrupt only on actual speech (not background noise)
            if device.processing:
                if device.vad.speech_prob > config["vad"]["threshold"]:
                    device.interrupted.set()
                continue

            # Track VAD recording transitions for persistent LED TCP
            prev_recording = was_recording.get(device.hostname, False)
            curr_recording = device.vad.recording

            if curr_recording and not prev_recording:
                # VAD just started recording — open persistent TCP for LED blinks
                device.vad_writer = await open_led_connection(device.ip, tcp_port)

            # LED feedback over persistent connection
            if device.vad.speech_prob > 0.1:
                device.led_power = min(max_led_intensity, device.led_power + dev_cfg["led_power"])
            if now - device.led_update_time > dev_cfg["led_update_period"]:
                device.led_update_time = now
                if device.led_power > 0 and device.vad_writer is not None:
                    if not write_led_blink(device.vad_writer, device.led_power, fade=dev_cfg["led_fade"]):
                        device.vad_writer = None  # connection lost
                device.led_power = 0

            was_recording[device.hostname] = curr_recording

            if utterance is not None:
                # Close persistent LED connection before queuing utterance
                if device.vad_writer is not None:
                    await close_led_connection(device.vad_writer)
                    device.vad_writer = None
                was_recording[device.hostname] = False
                log.info(f"VAD  utterance from {device.hostname} ({len(utterance)/sample_rate:.1f}s)")
                await utterance_queue.put((device, utterance))


async def greet_device(device: Device, config: dict):
    """Send greeting to a device. Always sends LED pulse for IP registration."""
    dev_cfg = config["device"]
    tcp_port = config["network"]["tcp_port"]

    # Always send LED pulse so the device learns our IP
    await send_led_blink(device.ip, tcp_port, intensity=100, r=0, g=255, b=50, fade=8)

    greeting_path = dev_cfg.get("greeting_wav")
    if not dev_cfg.get("greeting") or not greeting_path or not os.path.exists(greeting_path):
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
        try:
            parts = data.decode("utf-8").split()
        except UnicodeDecodeError:
            log.debug(f"MCAST  ignored non-UTF8 packet from {addr[0]}")
            continue
        if not parts:
            continue
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
            # Safety: close any lingering LED connection before processing
            if device.vad_writer is not None:
                await close_led_connection(device.vad_writer)
                device.vad_writer = None

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

            # Streaming LLM → sentence-buffered TTS → Opus → TCP.
            # Intermediate sends use mic_timeout=0 so the mic only reopens after
            # the final chunk has played out.
            sample_rate = config["audio"]["sample_rate"]
            opus_frame_size = config["audio"]["opus_frame_size"]

            turn_t0 = time.monotonic()
            full_response: list[str] = []
            pending: str | None = None   # sentence waiting to be flushed
            sent_partial = False           # any non-final chunk already sent?
            first_sentence_at: float | None = None
            stream_start_at: float | None = None

            async def send_sentence(sentence: str, is_final: bool) -> bool:
                """Synthesize, encode, and push one sentence. Returns True on
                success, False if interrupted or TTS failed."""
                nonlocal sent_partial
                if device.interrupted.is_set():
                    log.info(f"Interrupted before TTS")
                    return False
                try:
                    pcm = await tts.synthesize(sentence, device.voice, config)
                except Exception as e:
                    log.error(f"TTS  failed: {e}")
                    return False
                if device.interrupted.is_set():
                    log.info(f"Interrupted before send")
                    return False
                frames = opus_encode(pcm, sample_rate, opus_frame_size)
                payload = opus_frames_to_tcp_payload(frames)
                mic_timeout = dev_cfg["default_mic_timeout"] if is_final else 0
                log.info(f"SEND  [+{time.monotonic() - turn_t0:.2f}s] "
                         f"{len(frames)} opus frames to {device.ip} "
                         f"({'final' if is_final else 'partial'}: {sentence!r})")
                await send_audio(device.ip, tcp_port, payload,
                                 mic_timeout=mic_timeout,
                                 volume=dev_cfg["default_volume"],
                                 fade=dev_cfg["led_fade"])
                if not is_final:
                    sent_partial = True
                return True

            async def reopen_mic_if_needed():
                """If we already sent partial audio with mic_timeout=0, the mic
                is closed — push an empty audio to reopen it on recovery."""
                if sent_partial and not device.ptt:
                    try:
                        await send_audio(device.ip, tcp_port, b"",
                                         mic_timeout=dev_cfg["default_mic_timeout"],
                                         volume=0, fade=0)
                    except Exception:
                        pass

            # Stall decision (agentic mode only; blocking, capped by config timeout).
            # Passes the previous exchange so the classifier can recognize
            # continuations ("go on") and prefaces ("one more thing") as
            # conversational rather than tool-needing.
            stall_text: str | None = None
            if config["conversation"].get("backend") == "agentic":
                stall_text = await stall_mod.decide_stall(
                    text,
                    config,
                    prev_user=device.last_user_text,
                    prev_assistant=device.last_response,
                )
                stall_decided_at = time.monotonic() - turn_t0
                if stall_text:
                    log.info(f"STALL [+{stall_decided_at:.2f}s] decided: {stall_text!r}")
                else:
                    log.info(f"STALL [+{stall_decided_at:.2f}s] NONE")
            device.last_user_text = text

            # Fire stall TTS+send in parallel with OpenClaw warming up.
            stall_task: asyncio.Task | None = None
            extra_context: str | None = None
            if stall_text:
                stall_task = asyncio.create_task(send_sentence(stall_text, is_final=False))
                extra_context = (
                    f"(You already said aloud to the user: \"{stall_text}\" — "
                    f"don't repeat this phrase, continue naturally with the answer.)"
                )

            aborted = False
            try:
                stream_start_at = time.monotonic()
                async for sentence in sentence_chunks(
                    device.conversation.stream(text, extra_context=extra_context)
                ):
                    full_response.append(sentence)
                    if first_sentence_at is None:
                        first_sentence_at = time.monotonic()
                        ttfs_turn = first_sentence_at - turn_t0
                        ttfs_stream = first_sentence_at - stream_start_at
                        log.info(f"LLM  first sentence [+{ttfs_turn:.2f}s turn / "
                                 f"{ttfs_stream:.2f}s stream]: {sentence}")
                    else:
                        log.debug(f"LLM  sentence: {sentence}")

                    # Make sure the stall audio has finished sending before we
                    # start pushing OpenClaw content to the device.
                    if stall_task is not None and not stall_task.done():
                        await stall_task
                        stall_task = None

                    # Flush the *previous* sentence as non-final; whichever
                    # sentence is last when the stream ends becomes the final.
                    if pending is not None:
                        if not await send_sentence(pending, is_final=False):
                            aborted = True
                            break
                    pending = sentence
            except Exception as e:
                log.error(f"LLM  failed: {e}")
                if stall_task is not None:
                    try:
                        await stall_task
                    except Exception:
                        pass
                await reopen_mic_if_needed()
                continue

            # Drain the stall task if it's still pending (e.g. LLM stream
            # returned zero content).
            if stall_task is not None:
                try:
                    await stall_task
                except Exception:
                    pass
                stall_task = None

            if aborted:
                await reopen_mic_if_needed()
                continue

            if pending is not None:
                if not await send_sentence(pending, is_final=True):
                    await reopen_mic_if_needed()
                    continue
            elif sent_partial:
                # The stall played but OpenClaw returned nothing — reopen mic.
                if not device.ptt:
                    await send_audio(device.ip, tcp_port, b"",
                                     mic_timeout=dev_cfg["default_mic_timeout"],
                                     volume=0, fade=0)
            elif not device.ptt:
                # No content from the LLM and no stall — still reopen the mic.
                await send_audio(device.ip, tcp_port, b"",
                                 mic_timeout=dev_cfg["default_mic_timeout"],
                                 volume=0, fade=0)

            response_text = " ".join(full_response)
            device.last_response = response_text
            elapsed = time.monotonic() - turn_t0
            ttfs = f"{first_sentence_at - turn_t0:.2f}s" if first_sentence_at else "—"
            log.info(f"LLM  [{ttfs} first / {elapsed:.2f}s total / "
                     f"{len(full_response)} sentences / {len(response_text)} chars] "
                     f"{response_text}")

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


def _log_startup_summary(config: dict) -> None:
    """Log the active endpoints and models so it's obvious at a glance how the
    pipeline is configured for this run."""
    conv_cfg = config["conversation"]
    backend_name = conv_cfg.get("backend", "conversational")
    backend_cfg = conv_cfg.get(backend_name, {})

    log.info("Pipeline server starting")
    log.info(f"  ASR   {config['asr']['url']}")

    if backend_name == "agentic":
        model = backend_cfg.get("provider_model") or backend_cfg.get("model", "?")
        log.info(f"  LLM   agentic: {model} @ {backend_cfg.get('base_url', '?')} "
                 f"(channel={backend_cfg.get('message_channel', '?')})")
        stall_cfg = conv_cfg.get("stall", {})
        if stall_cfg.get("enabled"):
            log.info(f"  STALL {stall_cfg.get('model', '?')} @ {stall_cfg.get('base_url', '?')} "
                     f"(timeout={stall_cfg.get('timeout', 1.5)}s)")
        else:
            log.info("  STALL disabled")
    else:
        log.info(f"  LLM   conversational: {backend_cfg.get('model', '?')} "
                 f"@ {backend_cfg.get('base_url', '?')}")

    tts_cfg = config["tts"]
    tts_backend = tts_cfg.get("backend", "?")
    if tts_backend == "elevenlabs":
        el = tts_cfg.get("elevenlabs", {})
        vox = el.get("default_voice", "?")
        ptt = el.get("default_voice_ptt", vox)
        log.info(f"  TTS   elevenlabs: VOX={vox} PTT={ptt}")
    else:
        log.info(f"  TTS   {tts_backend}")


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

    _log_startup_summary(config)
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
