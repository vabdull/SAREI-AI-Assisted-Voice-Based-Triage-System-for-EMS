"""Speech-to-text runtime built on an NVIDIA NeMo model.

Loads the fine-tuned Arabic FastConformer model once and serves both
short live audio chunks and batch file transcription. Calls into the
model are serialised with a lock (NeMo is not thread-safe) and run
through the shared audio-preprocessing pipeline first.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import soundfile as sf

from backend.ai.audio_preprocessing import (
    AudioProcessingMetadata,
    preprocess_audio_chunk,
    preprocess_audio_file,
)
from backend.core.config import ROOT_DIR, get_settings
from backend.core.paths import resolve_existing_storage_path

logger = logging.getLogger(__name__)

try:
    import torch
except ImportError:
    torch = None

try:
    import nemo.collections.asr as nemo_asr
except ImportError:
    nemo_asr = None


@dataclass
class StreamingTranscriptEvent:
    chunk_index: int
    text: str
    is_final: bool
    start_ms: int = 0
    end_ms: int = 0
    confidence: float = 0.0
    preprocessing: AudioProcessingMetadata | None = None


@dataclass
class BatchTranscriptResult:
    audio_path: str
    text: str
    preprocessing: AudioProcessingMetadata | None = None


class StreamingAsrService:
    _model_cache: dict[tuple[str, str, str], Any] = {}
    _transcribe_lock = threading.Lock()

    def __init__(
        self,
        model_path: str | None = None,
        decoder_type: str | None = None,
        device: str | None = None,
    ) -> None:
        settings = get_settings()
        self.model_path = model_path or settings.nemo_model_path
        self.decoder_type = (decoder_type or settings.asr_decoder_type).lower()
        self.device = (device or settings.asr_device).lower()
        self.batch_size = settings.asr_batch_size

    @staticmethod
    def _require_nemo() -> None:
        if torch is None or nemo_asr is None:
            raise RuntimeError(
                "NeMo ASR is not available. Install torch and nemo_toolkit[asr]."
            )

    def _resolve_device(self) -> str:
        if self.device == "cpu":
            return "cpu"
        if self.device == "cuda":
            if torch is None or not torch.cuda.is_available():
                raise RuntimeError("ASR device is set to CUDA but no GPU is available.")
            return "cuda"
        if torch is not None and torch.cuda.is_available():
            return "cuda"
        return "cpu"

    @staticmethod
    def _extract_text(transcription: Any) -> str:
        if isinstance(transcription, str):
            return transcription
        if hasattr(transcription, "text"):
            return str(transcription.text)
        if hasattr(transcription, "pred_text"):
            return str(transcription.pred_text)
        return str(transcription)

    def _cache_key(self) -> tuple[str, str, str]:
        return (self.model_path, self.decoder_type, self._resolve_device())

    @staticmethod
    def _disable_cuda_graphs_if_supported(model: Any) -> None:
        decoding_wrapper = getattr(model, "decoding", None)
        greedy_decoder = getattr(decoding_wrapper, "decoding", None)
        disable_fn = getattr(greedy_decoder, "disable_cuda_graphs", None)
        if callable(disable_fn):
            try:
                disable_fn()
                logger.info("Disabled NeMo RNNT CUDA graphs for ASR stability")
            except Exception:
                logger.warning("Failed to disable NeMo RNNT CUDA graphs", exc_info=True)

    def _load_model(self) -> Any:
        """Return the cached NeMo model for (path, decoder, device).

        The expensive one-off steps (``restore_from``, decoder strategy
        change, ``to(device)``, ``freeze``, CUDA-graph disable) happen
        ONCE per cache key. The previous implementation re-ran the
        CUDA-graph disable on every call, which triggered the
        "Cannot unfreeze partially" error when preview and final
        transcriptions raced on the same cached model.
        """
        self._require_nemo()
        model_path = Path(self.model_path)
        if not model_path.is_absolute():
            model_path = ROOT_DIR / model_path
        if not model_path.is_file():
            raise RuntimeError(f"Configured NeMo model not found: {model_path}")

        cache_key = self._cache_key()
        cached = self._model_cache.get(cache_key)
        if cached is not None:
            return cached

        device = cache_key[2]
        logger.info(
            "Loading NeMo ASR model from %s using %s decoding on %s",
            model_path,
            self.decoder_type,
            device,
        )
        model = nemo_asr.models.ASRModel.restore_from(
            restore_path=str(model_path),
            map_location=device,
        )
        if hasattr(model, "change_decoding_strategy"):
            model.change_decoding_strategy(decoder_type=self.decoder_type, verbose=False)
        elif self.decoder_type != "ctc":
            logger.warning(
                "Configured ASR decoder '%s' is not supported by model %s; using model default.",
                self.decoder_type,
                type(model).__name__,
            )
        if hasattr(model, "to"):
            model = model.to(device)
        model.eval()
        if hasattr(model, "freeze"):
            model.freeze()
        if device == "cuda" and self.decoder_type == "rnnt":
            self._disable_cuda_graphs_if_supported(model)
        self.__class__._model_cache[cache_key] = model
        return model

    def _transcribe_paths(self, audio_paths: list[str], batch_size: int) -> list[str]:
        with self.__class__._transcribe_lock:
            model = self._load_model()
            try:
                predictions = model.transcribe(audio_paths, batch_size=batch_size)
            except RuntimeError as exc:
                if "CUDAGraph::replay" not in str(exc):
                    raise
                logger.warning(
                    "RNNT CUDA graph replay failed; clearing cached ASR model and retrying once without CUDA graphs"
                )
                self.__class__._model_cache.pop(self._cache_key(), None)
                model = self._load_model()
                predictions = model.transcribe(audio_paths, batch_size=batch_size)
        return [self._extract_text(prediction) for prediction in predictions]

    def transcribe_chunk(
        self,
        audio_bytes: bytes,
        chunk_index: int,
        is_final: bool = False,
        mime_type: str | None = None,
    ) -> StreamingTranscriptEvent:
        processed_bytes, metadata = preprocess_audio_chunk(audio_bytes, mime_type=mime_type)

        try:
            model = self._load_model()
        except RuntimeError:
            logger.warning("NeMo unavailable – returning empty transcript for chunk %d", chunk_index)
            return StreamingTranscriptEvent(
                chunk_index=chunk_index,
                text="",
                is_final=is_final,
                preprocessing=metadata,
            )

        audio_data, sr = sf.read(io.BytesIO(processed_bytes))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            sf.write(tmp_path, audio_data, sr)
            transcriptions = self._transcribe_paths([tmp_path], batch_size=1)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        text = transcriptions[0] if transcriptions else ""
        return StreamingTranscriptEvent(
            chunk_index=chunk_index,
            text=text,
            is_final=is_final,
            # The model does not emit a per-utterance confidence, so we
            # report a fixed placeholder: high when text was produced,
            # zero when the chunk was empty/silent.
            confidence=0.9 if text else 0.0,
            preprocessing=metadata,
        )

    def transcribe_files(
        self,
        audio_paths: list[str],
        batch_size: int | None = None,
    ) -> list[BatchTranscriptResult]:
        batch_size = batch_size or self.batch_size
        try:
            self._load_model()
        except RuntimeError:
            logger.warning("NeMo unavailable – returning empty batch transcriptions")
            return [BatchTranscriptResult(audio_path=path, text="") for path in audio_paths]
        prepared_inputs: list[tuple[str, str, AudioProcessingMetadata | None]] = []
        cleanup_paths: list[str] = []
        try:
            for path in audio_paths:
                resolved_path = str(resolve_existing_storage_path(path))
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    prepared_path = tmp.name
                try:
                    _, metadata = preprocess_audio_file(resolved_path, prepared_path)
                except Exception:
                    if os.path.exists(prepared_path):
                        os.unlink(prepared_path)
                    raise
                prepared_inputs.append((path, prepared_path, metadata))
                cleanup_paths.append(prepared_path)

            results: list[BatchTranscriptResult] = []
            for i in range(0, len(prepared_inputs), batch_size):
                batch = prepared_inputs[i : i + batch_size]
                transcriptions = self._transcribe_paths(
                    [prepared_path for _, prepared_path, _ in batch],
                    batch_size=batch_size,
                )
                for (original_path, _, metadata), text in zip(batch, transcriptions):
                    results.append(
                        BatchTranscriptResult(
                            audio_path=original_path,
                            text=text,
                            preprocessing=metadata,
                        )
                    )

            return results
        finally:
            for cleanup_path in cleanup_paths:
                if os.path.exists(cleanup_path):
                    os.unlink(cleanup_path)
