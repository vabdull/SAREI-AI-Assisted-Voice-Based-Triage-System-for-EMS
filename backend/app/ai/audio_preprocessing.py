"""Audio preprocessing for the ASR pipeline.

Normalises incoming audio (any browser/codec format) into the 16 kHz
mono PCM the ASR model expects, using ffmpeg for decoding/resampling
with a soundfile fallback. Includes Windows/WSL path handling so the
same code runs in both environments.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

try:
    import librosa
except ImportError:
    librosa = None

try:
    import noisereduce as nr
except ImportError:
    nr = None

TARGET_SAMPLE_RATE = 16000

_FFMPEG_FORMAT_HINTS = {
    "audio/webm": ("webm", ".webm"),
    "audio/webm;codecs=opus": ("webm", ".webm"),
    "audio/ogg": ("ogg", ".ogg"),
    "audio/ogg;codecs=opus": ("ogg", ".ogg"),
    "audio/mp4": ("mp4", ".mp4"),
    "audio/mpeg": ("mp3", ".mp3"),
    "audio/wav": ("wav", ".wav"),
    "audio/x-wav": ("wav", ".wav"),
}


def _windows_to_wsl_path(path: str) -> str:
    normalized = os.path.abspath(path).replace("\\", "/")
    if len(normalized) >= 3 and normalized[1:3] == ":/":
        drive = normalized[0].lower()
        remainder = normalized[3:]
        return f"/mnt/{drive}/{remainder}"
    return normalized


def _resolve_ffmpeg_cmd() -> tuple[list[str], bool]:
    """Return the ffmpeg command and whether it runs inside WSL."""
    native = shutil.which("ffmpeg")
    if native:
        return [native], False

    if sys.platform.startswith("win"):
        wsl = shutil.which("wsl") or shutil.which("wsl.exe")
        if wsl:
            try:
                subprocess.run(
                    [wsl, "ffmpeg", "-version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=5,
                )
                return [wsl, "ffmpeg"], True
            except Exception:
                pass

    raise FileNotFoundError(
        "ffmpeg was not found on PATH and no usable `wsl ffmpeg` fallback was available"
    )


@dataclass
class AudioProcessingMetadata:
    original_sample_rate: int
    output_sample_rate: int
    original_duration_seconds: float
    output_duration_seconds: float
    clipped_samples_detected: bool
    used_noise_reduction: bool
    used_vad: bool


def _decode_audio_with_ffmpeg(
    audio_bytes: bytes,
    mime_type: str | None = None,
) -> tuple[np.ndarray, int]:
    format_hint, suffix = _FFMPEG_FORMAT_HINTS.get((mime_type or "").lower(), (None, ".bin"))
    ffmpeg_cmd, using_wsl = _resolve_ffmpeg_cmd()

    pipe_cmd = [
        *ffmpeg_cmd,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if format_hint:
        pipe_cmd.extend(["-f", format_hint])
    pipe_cmd.extend(
        [
            "-i",
            "pipe:0",
            "-f",
            "wav",
            "-ac",
            "1",
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "pipe:1",
        ]
    )

    try:
        process = subprocess.run(
            pipe_cmd,
            input=audio_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        logger.info(
            "Decoded audio bytes through ffmpeg pipe: mime=%s bytes=%d header=%s format_hint=%s via=%s",
            mime_type or "unknown",
            len(audio_bytes),
            audio_bytes[:16].hex(),
            format_hint or "none",
            "wsl" if using_wsl else "native",
        )
        return sf.read(io.BytesIO(process.stdout), dtype="float32")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        # Some browser-recorded containers decode reliably only from a real file.
        stderr = (
            exc.stderr.decode("utf-8", errors="ignore").strip()
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else ""
        )
        logger.warning(
            "ffmpeg pipe decode failed, retrying from temp file: mime=%s bytes=%d header=%s format_hint=%s via=%s stderr=%s",
            mime_type or "unknown",
            len(audio_bytes),
            audio_bytes[:16].hex(),
            format_hint or "none",
            "wsl" if using_wsl else "native",
            stderr or "<empty>",
        )

    input_path = None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as src:
            src.write(audio_bytes)
            input_path = src.name
        output_fd, output_path = tempfile.mkstemp(suffix=".wav")
        os.close(output_fd)
        # ffmpeg refuses to overwrite an existing file unless -y is passed.
        # Remove the placeholder created by mkstemp so the output path is free.
        os.unlink(output_path)
        ffmpeg_input_path = _windows_to_wsl_path(input_path) if using_wsl else input_path
        ffmpeg_output_path = _windows_to_wsl_path(output_path) if using_wsl else output_path

        subprocess.run(
            [
                *ffmpeg_cmd,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                ffmpeg_input_path,
                "-ac",
                "1",
                "-ar",
                str(TARGET_SAMPLE_RATE),
                "-f",
                "wav",
                ffmpeg_output_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        logger.info(
            "Decoded audio bytes through ffmpeg temp file: mime=%s bytes=%d header=%s suffix=%s via=%s",
            mime_type or "unknown",
            len(audio_bytes),
            audio_bytes[:16].hex(),
            suffix,
            "wsl" if using_wsl else "native",
        )
        return sf.read(output_path, dtype="float32")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        stderr = (
            exc.stderr.decode("utf-8", errors="ignore").strip()
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else ""
        )
        logger.exception(
            "ffmpeg decode failed completely: mime=%s bytes=%d header=%s suffix=%s via=%s stderr=%s",
            mime_type or "unknown",
            len(audio_bytes),
            audio_bytes[:16].hex(),
            suffix,
            "wsl" if using_wsl else "native",
            stderr or "<empty>",
        )
        raise RuntimeError("Unable to decode audio bytes with ffmpeg") from exc
    finally:
        for path in (input_path, output_path):
            if path and os.path.exists(path):
                os.unlink(path)


def _load_audio(
    audio_bytes: bytes,
    mime_type: str | None = None,
) -> tuple[np.ndarray, int]:
    try:
        return sf.read(io.BytesIO(audio_bytes), dtype="float32")
    except Exception:
        logger.debug("soundfile could not decode audio bytes, falling back to ffmpeg")
        return _decode_audio_with_ffmpeg(audio_bytes, mime_type=mime_type)


def preprocess_audio_chunk(
    audio_bytes: bytes,
    mime_type: str | None = None,
    enable_noise_reduction: bool = True,
) -> tuple[bytes, AudioProcessingMetadata]:
    audio_data, original_sr = _load_audio(audio_bytes, mime_type=mime_type)
    original_channels = int(audio_data.shape[1]) if getattr(audio_data, "ndim", 1) > 1 else 1

    if audio_data.ndim > 1:
        audio_data = audio_data.mean(axis=1)

    original_duration = len(audio_data) / original_sr
    clipped = bool(np.any(np.abs(audio_data) >= 1.0))

    used_nr = False
    if enable_noise_reduction and nr is not None:
        try:
            audio_data = nr.reduce_noise(y=audio_data, sr=original_sr)
            used_nr = True
        except Exception:
            logger.debug("Noise reduction failed, skipping")

    output_sr = original_sr
    if original_sr != TARGET_SAMPLE_RATE:
        if librosa is not None:
            audio_data = librosa.resample(
                audio_data, orig_sr=original_sr, target_sr=TARGET_SAMPLE_RATE
            )
        else:
            ratio = TARGET_SAMPLE_RATE / original_sr
            new_length = int(len(audio_data) * ratio)
            indices = np.linspace(0, len(audio_data) - 1, new_length)
            audio_data = np.interp(indices, np.arange(len(audio_data)), audio_data).astype(
                np.float32
            )
        output_sr = TARGET_SAMPLE_RATE

    output_duration = len(audio_data) / output_sr

    buf = io.BytesIO()
    sf.write(buf, audio_data, output_sr, format="WAV", subtype="PCM_16")
    processed_bytes = buf.getvalue()

    metadata = AudioProcessingMetadata(
        original_sample_rate=original_sr,
        output_sample_rate=output_sr,
        original_duration_seconds=round(original_duration, 4),
        output_duration_seconds=round(output_duration, 4),
        clipped_samples_detected=clipped,
        used_noise_reduction=used_nr,
        used_vad=False,
    )
    logger.info(
        "Preprocessed audio chunk: mime=%s bytes=%d sr=%d->%d channels=%d duration=%.3fs clipped=%s nr=%s",
        mime_type or "unknown",
        len(audio_bytes),
        original_sr,
        output_sr,
        original_channels,
        output_duration,
        clipped,
        used_nr,
    )
    return processed_bytes, metadata


def preprocess_audio_file(
    input_path: str, output_path: str
) -> tuple[str, AudioProcessingMetadata]:
    with open(input_path, "rb") as f:
        audio_bytes = f.read()

    processed_bytes, metadata = preprocess_audio_chunk(audio_bytes)

    with open(output_path, "wb") as f:
        f.write(processed_bytes)

    return output_path, metadata
