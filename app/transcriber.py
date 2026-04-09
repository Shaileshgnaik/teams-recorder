"""
transcriber.py — Transcribes WAV audio files using NVIDIA Parakeet via parakeet-mlx.

parakeet-mlx is the Apple Silicon (MLX framework) port of NVIDIA's Parakeet TDT 1.1B
speech recognition model. It runs on the Mac's Neural Engine and GPU via Apple's MLX
framework — no CUDA, no internet needed after the first model download.

Model: mlx-community/parakeet-tdt-1.1b-v2
Size: ~2GB (downloaded to ~/.cache/huggingface/ on first run)
Speed: ~10–20x real-time on M-series chips (60-min meeting → 3–6 min)

Usage:
    transcriber = ParakeetTranscriber()
    text = transcriber.transcribe("/tmp/meeting_xxx.wav")
    print(text)
"""

import os
from typing import Optional


class ParakeetTranscriber:
    """
    Wraps parakeet-mlx for speech-to-text transcription.

    The model is loaded lazily on the first call to transcribe() — this avoids
    a slow startup when the app launches. Subsequent calls reuse the loaded model
    (typically instant).

    Attributes:
        model_id: HuggingFace model ID for Parakeet MLX.
        _model: Cached model object after first load.
    """

    DEFAULT_MODEL = "mlx-community/parakeet-tdt-1.1b-v2"

    def __init__(self, model_id: str = DEFAULT_MODEL):
        self.model_id = model_id
        self._model = None   # lazy-loaded on first transcribe() call
        self._load_fn = None
        self._transcribe_fn = None

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def transcribe(self, wav_path: str) -> str:
        """
        Transcribe a WAV audio file to text using Parakeet MLX.

        The WAV file should be:
        - Sample rate: 16kHz (Parakeet's expected input)
        - Channels: mono
        - Format: 16-bit PCM or float32 (both accepted)

        Args:
            wav_path: Absolute path to the WAV file to transcribe.

        Returns:
            The full transcript as a single string.

        Raises:
            FileNotFoundError: If wav_path does not exist.
            ImportError: If parakeet-mlx is not installed.
            RuntimeError: If transcription fails.
        """
        if not os.path.exists(wav_path):
            raise FileNotFoundError(f"WAV file not found: {wav_path}")

        self._ensure_model_loaded()

        print(f"[transcriber] Transcribing {wav_path} ...")

        try:
            result = self._transcribe_fn(self._model, wav_path)
            transcript = self._extract_text(result)
            print(f"[transcriber] Done. Transcript length: {len(transcript)} chars")
            return transcript
        except Exception as e:
            raise RuntimeError(f"Parakeet transcription failed: {e}") from e

    # ---------------------------------------------------------------------- #
    #  Internal helpers
    # ---------------------------------------------------------------------- #

    def _ensure_model_loaded(self) -> None:
        """
        Load the Parakeet MLX model if not already loaded.

        parakeet-mlx provides:
          - parakeet_mlx.load_model(model_id) → loads weights from HuggingFace cache
          - parakeet_mlx.transcribe(model, audio_path) → runs inference

        The model download happens automatically on first call via HuggingFace Hub
        and is cached in ~/.cache/huggingface/. Subsequent loads use the local cache.
        """
        if self._model is not None:
            return  # already loaded

        try:
            import parakeet_mlx  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "parakeet-mlx is not installed. Install with:\n"
                "  pip install parakeet-mlx\n"
                "(Requires Apple Silicon Mac with MLX support)"
            ) from e

        from parakeet_mlx import load_model, transcribe as _transcribe

        print(f"[transcriber] Loading model: {self.model_id}")
        print("[transcriber] First run will download ~2GB. This may take a few minutes...")

        self._model = load_model(self.model_id)
        self._transcribe_fn = _transcribe
        print("[transcriber] Model loaded successfully.")

    @staticmethod
    def _extract_text(result) -> str:
        """
        Extract plain text from the parakeet-mlx transcription result.

        The result object may be:
        - A string (some versions return text directly)
        - An object with a .text attribute
        - An object with a .segments list of {text: str} dicts

        We handle all three cases defensively.
        """
        # Case 1: result is already a string
        if isinstance(result, str):
            return result.strip()

        # Case 2: result has a .text attribute
        if hasattr(result, "text"):
            return result.text.strip()

        # Case 3: result has .segments list
        if hasattr(result, "segments") and result.segments:
            return " ".join(seg["text"].strip() for seg in result.segments).strip()

        # Case 4: result is a list of segment dicts
        if isinstance(result, list):
            return " ".join(
                (seg.get("text", "") or seg.get("transcript", "")).strip()
                for seg in result
            ).strip()

        # Fallback
        return str(result).strip()

    def get_segments(self, wav_path: str) -> list[dict]:
        """
        Transcribe and return word/segment-level timestamps.

        Returns:
            List of dicts: [{"start": float, "end": float, "text": str}, ...]
            Returns empty list if segments not supported by this model version.
        """
        if not os.path.exists(wav_path):
            raise FileNotFoundError(f"WAV file not found: {wav_path}")

        self._ensure_model_loaded()

        try:
            result = self._transcribe_fn(self._model, wav_path)
            if hasattr(result, "segments") and result.segments:
                return [
                    {
                        "start": seg.get("start", 0),
                        "end": seg.get("end", 0),
                        "text": seg.get("text", "").strip(),
                    }
                    for seg in result.segments
                ]
        except Exception as e:
            print(f"[transcriber] Segment extraction failed: {e}")

        return []
