import logging
from collections import deque

import numpy as np
import onnxruntime

log = logging.getLogger(__name__)

# Silero VAD v5+ constants for 16kHz
_CONTEXT_SIZE = 64
_NUM_SAMPLES = 512


def _load_silero_onnx() -> onnxruntime.InferenceSession:
    """Load the Silero VAD ONNX model from the silero-vad package."""
    import silero_vad
    from pathlib import Path

    pkg_dir = Path(silero_vad.__file__).parent / "data"
    model_path = pkg_dir / "silero_vad.onnx"
    opts = onnxruntime.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    return onnxruntime.InferenceSession(str(model_path), sess_options=opts)


class VAD:
    def __init__(self, config: dict):
        vad_cfg = config["vad"]
        audio_cfg = config["audio"]

        self.sample_rate = audio_cfg["sample_rate"]
        self.chunk_size = audio_cfg["chunk_size"]
        frames_per_sec = self.sample_rate // self.chunk_size

        self.threshold = vad_cfg["threshold"]
        self.neg_threshold = vad_cfg["neg_threshold"]
        self.silence_time = vad_cfg["silence_time"]

        prebuf_frames = int(vad_cfg["pre_buffer_s"] * frames_per_sec)

        self.session = _load_silero_onnx()
        self.pre_buffer: deque[np.ndarray] = deque(maxlen=prebuf_frames)
        self.buffer: list[np.ndarray] = []
        self.recording = False
        self.silence_count = 0
        self.frames_per_sec = frames_per_sec
        self.speech_prob = 0.0

        # ONNX state: LSTM hidden (2, 1, 128) and audio context (1, 64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT_SIZE), dtype=np.float32)
        self._sr = np.array(self.sample_rate, dtype=np.int64)

    def reset(self):
        self.buffer = []
        self.recording = False
        self.silence_count = 0
        self.speech_prob = 0.0
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT_SIZE), dtype=np.float32)

    def process_frame(self, pcm_int16: np.ndarray) -> np.ndarray | None:
        """Feed a 512-sample (32ms) PCM frame. Returns utterance audio (int16) when speech ends."""
        # Convert int16 -> float32 normalized [-1, 1]
        pcm_f32 = pcm_int16.astype(np.float32) / 32768.0
        x = pcm_f32.reshape(1, -1)  # (1, 512)

        # Prepend context from previous frame
        x_with_ctx = np.concatenate([self._context, x], axis=1)  # (1, 576)

        # Run ONNX inference
        ort_inputs = {"input": x_with_ctx, "state": self._state, "sr": self._sr}
        out, self._state = self.session.run(None, ort_inputs)

        # Update context for next frame
        self._context = x_with_ctx[:, -_CONTEXT_SIZE:]

        self.speech_prob = float(out[0, 0])

        if not self.recording:
            self.pre_buffer.append(pcm_int16)
            if self.speech_prob >= self.threshold:
                self.recording = True
                self.buffer.extend(self.pre_buffer)
                self.pre_buffer.clear()
            return None

        self.buffer.append(pcm_int16)

        if self.speech_prob < self.neg_threshold:
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
        return self.speech_prob >= self.threshold
