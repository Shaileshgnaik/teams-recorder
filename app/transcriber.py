"""
transcriber.py — Transcribes WAV audio using mlx-whisper (Apple Silicon, local).

Uses mlx-whisper which runs on Apple's MLX framework (built into macOS on Apple
Silicon). Much lighter than openai-whisper — no PyTorch required.

Model (~145 MB) is downloaded once from HuggingFace to ~/.cache/huggingface/
on first use. No TCC speech recognition permissions required.
"""

import os

MLX_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-base.en-mlx")


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
            print("[transcriber] (First run downloads ~145 MB to ~/.cache/huggingface/)")
            self._loaded = True

        print(f"[transcriber] Transcribing {wav_path}...")
        result = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=MLX_MODEL,
            verbose=False,
        )
        text = result.get("text", "").strip()
        print(f"[transcriber] Done. {len(text)} chars.")
        return text


# Keep alias so main.py import doesn't need to change
ParakeetTranscriber = WhisperTranscriber
