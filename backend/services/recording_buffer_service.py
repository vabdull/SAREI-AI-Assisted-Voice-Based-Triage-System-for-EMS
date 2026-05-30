"""
recording_buffer_service — accumulate live-call audio for one recording.

Why this exists
---------------
The live ASR path (`POST /inference/live-chunk`) receives audio as a
stream of *independent* MediaRecorder segments. Each segment is a
self-contained webm/mp4 blob with its own container header, so the raw
bytes cannot be concatenated into a single valid audio file.

Instead, every chunk is already decoded + resampled to 16 kHz mono
PCM_16 WAV by ``preprocess_audio_chunk`` for the ASR. We capture those
decoded samples here, per case, in memory. When the call ends
(`POST /inference/finalize-recording`) we concatenate the samples and
write ONE playable WAV file + a ``CallRecording`` row.

This keeps the hot path cheap (just an in-memory append) and produces a
single clean recording per call.
"""

from __future__ import annotations

import io
import logging
import threading

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class RecordingBufferService:
    """Per-process, per-case PCM sample buffer. Thread-safe."""

    def __init__(self) -> None:
        self._buffers: dict[int, list[np.ndarray]] = {}
        self._lock = threading.Lock()

    def append_wav_bytes(self, case_id: int, wav_bytes: bytes) -> None:
        """Decode a preprocessed 16 kHz mono WAV chunk and buffer its
        samples. Never raises into the live path: a decode failure just
        skips this chunk."""
        if not wav_bytes:
            return
        try:
            data, _sr = sf.read(io.BytesIO(wav_bytes), dtype="float32")
            if data.ndim > 1:  # safety: collapse to mono
                data = data.mean(axis=1)
        except Exception:
            logger.warning(
                "recording_buffer: failed to decode chunk for case=%s",
                case_id,
                exc_info=True,
            )
            return
        with self._lock:
            self._buffers.setdefault(case_id, []).append(data)

    def has_audio(self, case_id: int) -> bool:
        with self._lock:
            return bool(self._buffers.get(case_id))

    def finalize_to_wav(self, case_id: int) -> bytes | None:
        """Concatenate buffered samples into a single WAV byte string and
        clear the buffer. Returns ``None`` when nothing was buffered."""
        with self._lock:
            segments = self._buffers.pop(case_id, None)
        if not segments:
            return None
        combined = np.concatenate(segments)
        buf = io.BytesIO()
        sf.write(buf, combined, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    def reset(self, case_id: int) -> None:
        """Drop any buffered audio for a case (e.g. on call restart)."""
        with self._lock:
            self._buffers.pop(case_id, None)


_instance: RecordingBufferService | None = None
_instance_lock = threading.Lock()


def get_recording_buffer_service() -> RecordingBufferService:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = RecordingBufferService()
    return _instance
