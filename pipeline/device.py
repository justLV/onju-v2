import json
import logging
import os
import time

from pipeline.vad import VAD

log = logging.getLogger(__name__)


class Device:
    def __init__(self, hostname: str, ip: str, config: dict, messages: list | None = None, voice: str | None = None):
        self.hostname = hostname
        self.ip = ip
        self.config = config
        self.voice = voice or config["tts"].get("default_voice", "Rachel")
        self.messages = messages or [{"role": "system", "content": config["llm"]["system_prompt"]}]
        self.vad = VAD(config)
        self.last_response: str | None = None
        self.led_power = 0
        self.led_update_time = 0.0

    def prune_messages(self):
        max_msgs = self.config["llm"]["max_messages"]
        while len(self.messages) > max_msgs:
            self.messages.pop(1)  # keep system prompt at [0]

    def sanitize_messages(self):
        """Ensure messages alternate user/assistant after the system prompt.
        Drops messages that break alternation and trims trailing user messages
        (orphaned from crashes where the LLM never responded)."""
        cleaned = [self.messages[0]] if self.messages and self.messages[0]["role"] == "system" else []
        expected = "user"
        start = 1 if cleaned else 0
        for msg in self.messages[start:]:
            if msg["role"] == "system":
                continue
            if msg["role"] == expected:
                cleaned.append(msg)
                expected = "assistant" if expected == "user" else "user"
        # Trim trailing user message (no LLM response = orphaned)
        if len(cleaned) > 1 and cleaned[-1]["role"] == "user":
            cleaned.pop()
        self.messages = cleaned

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "ip": self.ip,
            "messages": self.messages,
            "voice": self.voice,
        }

    @classmethod
    def from_dict(cls, data: dict, config: dict) -> "Device":
        device = cls(
            data["hostname"],
            data["ip"],
            config,
            messages=data.get("messages"),
            voice=data.get("voice"),
        )
        device.sanitize_messages()
        return device

    def __repr__(self):
        return f"<Device {self.hostname} {self.ip} [{len(self.messages)-1} msgs]>"


class DeviceManager:
    def __init__(self, config: dict, persist: bool = False):
        self.config = config
        self.devices: dict[str, Device] = {}
        self.persist_path = config["device"].get("persist_file", "data/devices.json") if persist else None
        if self.persist_path:
            self._load()

    def create_device(self, hostname: str, ip: str) -> Device:
        device = self.devices.get(hostname)
        if device is None:
            device = Device(hostname, ip, self.config)
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
        """Return the most recently created device (fallback for localhost testing)."""
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
