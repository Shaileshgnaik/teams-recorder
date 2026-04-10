"""
transcriber.py — Transcribes WAV audio using mlx-whisper (Apple Silicon, local).

Uses mlx-whisper which runs on Apple's MLX framework (built into macOS on Apple
Silicon). Much lighter than openai-whisper — no PyTorch required.

Model (~145 MB) is downloaded once from HuggingFace to ~/.cache/huggingface/
on first use. No TCC speech recognition permissions required.
"""

import os
import numpy as np
import scipy.io.wavfile as wav_io

MLX_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-base.en-mlx")
WHISPER_SR = 16000  # Whisper always expects 16 kHz


class WhisperTranscriber:
    """
    Transcribes audio files using mlx-whisper on Apple Silicon.

    No TCC speech recognition permission required.
    No PyTorch — uses Apple's MLX framework instead (~50 MB install).

    Usage:
        t = WhisperTranscriber()
        text = t.transcribe("/tmp/meeting_xxx.wav")
    """

    def __init__(self):
        self._loaded = False

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def transcribe(self, wav_path: str) -> str:
        """
        Transcribe a WAV file to text.

        Args:
            wav_path: Path to a WAV file.

        Returns:
            Transcript as a plain string.
        """
        if not os.path.exists(wav_path):
            raise FileNotFoundError(f"WAV file not found: {wav_path}")

        try:
            import mlx_whisper
        except ImportError as e:
            raise ImportError(
                "mlx-whisper not installed.\n"
                "Run: pip3 install mlx-whisper"
            ) from e

        if not self._loaded:
            print(f"[transcriber] Loading mlx-whisper model '{MLX_MODEL}'...")
            self._loaded = True

        # Load WAV via scipy — avoids ffmpeg dependency entirely
        sr, audio = wav_io.read(wav_path)
        audio = self._to_float32_mono(audio, sr)

        print(f"[transcriber] Transcribing {len(audio)/WHISPER_SR:.1f}s of audio...")
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=MLX_MODEL,
            verbose=False,
        )
        text = result.get("text", "").strip()
        print(f"[transcriber] Done. {len(text)} chars.")
        return text

    # ---------------------------------------------------------------------- #
    #  Internal helpers
    # ---------------------------------------------------------------------- #

    def _to_float32_mono(self, audio: np.ndarray, sr: int) -> np.ndarray:
        """Convert WAV data to float32 mono at 16 kHz (Whisper's required format)."""
        # Stereo → mono
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        # Integer PCM → float32 in [-1, 1]
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype == np.int32:
            audio = audio.astype(np.float32) / 2147483648.0
        else:
            audio = audio.astype(np.float32)
        # Resample to 16 kHz if needed
        if sr != WHISPER_SR:
            from scipy.signal import resample_poly
            import math
            gcd = math.gcd(WHISPER_SR, sr)
            audio = resample_poly(audio, WHISPER_SR // gcd, sr // gcd).astype(np.float32)
        return audio


# Keep alias so main.py import doesn't need to change
ParakeetTranscriber = WhisperTranscriber
