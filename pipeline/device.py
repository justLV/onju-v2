import logging
import time

from pipeline.conversation import ConversationBackend, create_backend
from pipeline.vad import VAD

log = logging.getLogger(__name__)


class Device:
    def __init__(self, hostname: str, ip: str, config: dict, conversation: ConversationBackend, voice: str | None = None):
        self.hostname = hostname
        self.ip = ip
        self.config = config
        self.conversation = conversation
        self.voice = voice or config["tts"].get("default_voice", "Rachel")
        self.vad = VAD(config)
        self.last_response: str | None = None
        self.led_power = 0
        self.led_update_time = 0.0

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "ip": self.ip,
            "voice": self.voice,
        }

    @classmethod
    def from_dict(cls, data: dict, config: dict) -> "Device":
        conv = create_backend(config, data["hostname"])
        return cls(
            data["hostname"],
            data["ip"],
            config,
            conversation=conv,
            voice=data.get("voice"),
        )

    def __repr__(self):
        return f"<Device {self.hostname} {self.ip}>"


class DeviceManager:
    def __init__(self, config: dict):
        self.config = config
        self.devices: dict[str, Device] = {}

    def create_device(self, hostname: str, ip: str) -> Device:
        device = self.devices.get(hostname)
        if device is None:
            conv = create_backend(self.config, hostname)
            device = Device(hostname, ip, self.config, conversation=conv)
            self.devices[hostname] = device
            log.debug(f"New device: {hostname} ({ip})")
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
