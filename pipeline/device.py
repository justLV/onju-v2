import asyncio
import logging
import time

from pipeline.conversation import ConversationBackend, create_backend
from pipeline.vad import VAD

log = logging.getLogger(__name__)


class Device:
    def __init__(self, hostname: str, ip: str, config: dict, conversation: ConversationBackend,
                 voice: str | None = None, ptt: bool = False):
        self.hostname = hostname
        self.ip = ip
        self.config = config
        self.conversation = conversation
        self.voice = voice or config["tts"].get("elevenlabs", {}).get("default_voice", "Rachel")
        self.ptt = ptt
        self.vad = None if ptt else VAD(config)
        self.last_response: str | None = None
        self.led_power = 0
        self.led_update_time = 0.0

        # PTT state
        self.ptt_buffer: list = []  # raw PCM frames during PTT
        self.processing = False     # True while ASR/LLM/TTS pipeline is running
        self.interrupted = asyncio.Event()

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "ip": self.ip,
            "voice": self.voice,
            "ptt": self.ptt,
        }

    def __repr__(self):
        mode = "PTT" if self.ptt else "VOX"
        return f"<Device {self.hostname} {self.ip} {mode}>"


class DeviceManager:
    def __init__(self, config: dict):
        self.config = config
        self.devices: dict[str, Device] = {}

    def create_device(self, hostname: str, ip: str, ptt: bool = False) -> Device:
        device = self.devices.get(hostname)
        if device is None:
            conv = create_backend(self.config, hostname)
            device = Device(hostname, ip, self.config, conversation=conv, ptt=ptt)
            self.devices[hostname] = device
            log.debug(f"New device: {device}")
        elif device.ip != ip:
            device.ip = ip
            log.debug(f"Updated {hostname} IP to {ip}")
        else:
            log.debug(f"Device {hostname} reconnected ({ip})")
        return device

    def get_by_ip(self, ip: str) -> Device | None:
        for d in self.devices.values():
            if d.ip == ip:
                return d
        return None

    def get_most_recent(self) -> Device | None:
        if self.devices:
            return next(reversed(self.devices.values()))
        return None
