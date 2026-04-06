import json
import logging
import os
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
            "messages": self.conversation.get_messages(),
            "voice": self.voice,
        }

    @classmethod
    def from_dict(cls, data: dict, config: dict) -> "Device":
        conv = create_backend(config, data["hostname"])
        if messages := data.get("messages"):
            conv.set_messages(messages)
        return cls(
            data["hostname"],
            data["ip"],
            config,
            conversation=conv,
            voice=data.get("voice"),
        )

    def __repr__(self):
        msgs = self.conversation.get_messages()
        count = max(0, len(msgs) - 1)
        return f"<Device {self.hostname} {self.ip} [{count} msgs]>"


class DeviceManager:
    def __init__(self, config: dict, persist: bool = False):
        self.config = config
        self.devices: dict[str, Device] = {}
        self.persist_path = config["device"].get("registry_file", "data/devices.json") if persist else None
        if self.persist_path:
            self._load()

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

    def save(self):
        if not self.persist_path:
            return
        data = {k: v.to_dict() for k, v in self.devices.items()}
        parent = os.path.dirname(self.persist_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.persist_path, "w") as f:
            json.dump(data, f, indent=2)
        log.info(f"Saved {len(self.devices)} devices to {self.persist_path}")

    def _load(self):
        if not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path) as f:
                data = json.load(f)
            self.devices = {k: Device.from_dict(v, self.config) for k, v in data.items()}
            log.info(f"Loaded {len(self.devices)} devices from {self.persist_path}")
        except Exception as e:
            log.warning(f"Failed to load {self.persist_path}: {e}")
