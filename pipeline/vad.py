import numpy as np
import webrtcvad
from collections import deque


class VAD:
    def __init__(self, config: dict):
        vad_cfg = config["vad"]
        audio_cfg = config["audio"]

        self.sample_rate = audio_cfg["sample_rate"]
        self.chunk_size = audio_cfg["chunk_size"]
        frames_per_sec = self.sample_rate // self.chunk_size

        self.start_ratio = vad_cfg["start_ratio"]
        self.silence_ratio = vad_cfg["silence_ratio"]
        self.silence_time = vad_cfg["silence_time"]

        window_frames = int(vad_cfg["window_s"] * frames_per_sec)
        prebuf_frames = int(vad_cfg["pre_buffer_s"] * frames_per_sec)

        self.vad = webrtcvad.Vad(vad_cfg["aggressiveness"])
        self.window: deque[bool] = deque(maxlen=window_frames)
        self.pre_buffer: deque[np.ndarray] = deque(maxlen=prebuf_frames)
        self.buffer: list[np.ndarray] = []
        self.recording = False
        self.silence_count = 0
        self.frames_per_sec = frames_per_sec

    def reset(self):
        self.buffer = []
        self.recording = False
        self.silence_count = 0
        self.window.clear()

    def process_frame(self, pcm_int16: np.ndarray) -> np.ndarray | None:
        """Feed a 30ms PCM frame. Returns complete utterance audio (int16) when speech ends, else None."""
        raw = pcm_int16.tobytes()
        is_speech = self.vad.is_speech(raw, self.sample_rate)
        self.window.append(is_speech)

        if len(self.window) < self.window.maxlen:
            return None

        ratio = sum(self.window) / len(self.window)

        if not self.recording:
            self.pre_buffer.append(pcm_int16)
            if ratio > self.start_ratio:
                self.recording = True
                self.buffer.extend(self.pre_buffer)
                self.pre_buffer.clear()
            return None

        self.buffer.append(pcm_int16)

        if ratio < self.silence_ratio:
            self.silence_count += 1
            if self.silence_count > self.silence_time * self.frames_per_sec:
                audio = np.concatenate(self.buffer)
                self.reset()
                return audio
        else:
            self.silence_count = 0

        return None

    @property
    def is_speech_now(self) -> bool:
        if not self.window:
            return False
        return bool(self.window[-1])
