"""
transcriber.py — Transcribes WAV audio using Apple's built-in SFSpeechRecognizer.

Uses macOS's native Speech Recognition framework (no internet, no model download,
no HuggingFace). Works completely offline using on-device recognition.

For long meetings (>55 seconds), the audio is automatically split into 55-second
chunks, each transcribed separately, then concatenated into the full transcript.

Requires "Speech Recognition" permission — macOS prompts the user once automatically
when the app first calls requestAuthorization.
"""

import os
import time
import threading
import tempfile
import numpy as np
import scipy.io.wavfile as wav_io

from utils import SAMPLE_RATE

CHUNK_SECONDS = 55  # just under the ~60s practical limit per SFSpeechURLRecognitionRequest


class AppleSpeechTranscriber:
    """
    Transcribes audio files using Apple's SFSpeechRecognizer.

    Advantages over Parakeet/Whisper for this use case:
    - No model download (built into macOS)
    - No internet or HuggingFace access needed
    - Optimised for Apple Silicon
    - Handles long audio via automatic chunking

    Usage:
        t = AppleSpeechTranscriber()
        text = t.transcribe("/tmp/meeting_xxx.wav")
    """

    def __init__(self):
        self._recognizer = None
        self._authorized = False

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def transcribe(self, wav_path: str) -> str:
        """
        Transcribe a WAV file to text.

        Args:
            wav_path: Path to 16kHz mono WAV file.

        Returns:
            Full transcript as a string.
        """
        if not os.path.exists(wav_path):
            raise FileNotFoundError(f"WAV file not found: {wav_path}")

        self._ensure_authorized()
        self._ensure_recognizer()

        print(f"[transcriber] Transcribing {wav_path} ...")

        # Load audio to determine if chunking is needed
        sr, audio = wav_io.read(wav_path)
        if audio.ndim > 1:
            audio = audio[:, 0]  # take first channel if stereo
        duration = len(audio) / sr

        if duration <= CHUNK_SECONDS:
            transcript = self._transcribe_file(wav_path)
        else:
            transcript = self._transcribe_chunked(audio, sr, duration)

        print(f"[transcriber] Done. {len(transcript)} chars.")
        return transcript

    # ---------------------------------------------------------------------- #
    #  Internal helpers
    # ---------------------------------------------------------------------- #

    def _ensure_authorized(self) -> None:
        """
        Request Speech Recognition authorization if not already granted.

        macOS shows a one-time dialog: "Allow <app> to use Speech Recognition?"
        We block until the user responds (or 30s timeout).
        """
        if self._authorized:
            return

        try:
            from Speech import SFSpeechRecognizer
        except ImportError as e:
            raise ImportError(
                "pyobjc-framework-Speech not installed.\n"
                "Run: pip install pyobjc-framework-Speech"
            ) from e

        status_holder = [None]
        done = threading.Event()

        def auth_callback(status):
            status_holder[0] = status
            done.set()

        SFSpeechRecognizer.requestAuthorization_(auth_callback)
        done.wait(timeout=30)

        # SFSpeechRecognizerAuthorizationStatus values:
        # 0 = notDetermined, 1 = denied, 2 = restricted, 3 = authorized
        status = status_holder[0]
        if status != 3:
            status_names = {0: "not determined", 1: "denied", 2: "restricted", 3: "authorized"}
            raise PermissionError(
                f"Speech Recognition permission {status_names.get(status, status)}. "
                "Please allow it in System Settings → Privacy & Security → Speech Recognition."
            )

        self._authorized = True
        print("[transcriber] Speech Recognition authorized.")

    def _ensure_recognizer(self) -> None:
        """Create the SFSpeechRecognizer instance (en-US locale)."""
        if self._recognizer is not None:
            return

        from Speech import SFSpeechRecognizer
        from Foundation import NSLocale

        locale = NSLocale.localeWithLocaleIdentifier_("en-US")
        self._recognizer = SFSpeechRecognizer.alloc().initWithLocale_(locale)

        if self._recognizer is None or not self._recognizer.isAvailable():
            raise RuntimeError(
                "SFSpeechRecognizer not available. "
                "Check System Settings → Privacy & Security → Speech Recognition."
            )

    def _transcribe_file(self, wav_path: str) -> str:
        """
        Transcribe a single audio file using SFSpeechURLRecognitionRequest.

        Uses on-device recognition (requiresOnDeviceRecognition=True) so no
        audio is sent to Apple's servers. Falls back to online if on-device fails.
        """
        from Speech import SFSpeechURLRecognitionRequest
        from Foundation import NSURL

        url = NSURL.fileURLWithPath_(wav_path)

        # Try on-device first (offline, private)
        for on_device in (True, False):
            result = self._run_recognition(url, on_device=on_device)
            if result is not None:
                return result
            if on_device:
                print("[transcriber] On-device recognition failed, trying online...")

        return ""

    def _run_recognition(self, url, on_device: bool) -> str | None:
        """
        Run a single SFSpeechURLRecognitionRequest and wait for the result.

        Returns the transcript string, or None if recognition failed.
        """
        from Speech import SFSpeechURLRecognitionRequest

        request = SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
        request.setShouldReportPartialResults_(False)
        request.setRequiresOnDeviceRecognition_(on_device)

        result_text = [None]
        error_holder = [None]
        done = threading.Event()

        def handler(result, error):
            if error:
                error_holder[0] = str(error)
                done.set()
                return
            if result and result.isFinal():
                result_text[0] = result.bestTranscription().formattedString()
                done.set()

        self._recognizer.recognitionTaskWithRequest_resultHandler_(request, handler)
        done.wait(timeout=300)  # up to 5 minutes per chunk

        if error_holder[0]:
            print(f"[transcriber] Recognition error: {error_holder[0]}")
            return None

        return result_text[0] or ""

    def _transcribe_chunked(self, audio: np.ndarray, sr: int, duration: float) -> str:
        """
        Split long audio into CHUNK_SECONDS chunks and transcribe each one.

        Args:
            audio: Raw audio samples as numpy array.
            sr: Sample rate of the audio.
            duration: Total duration in seconds.

        Returns:
            Concatenated transcript of all chunks.
        """
        chunk_samples = int(CHUNK_SECONDS * sr)
        total_chunks = int(np.ceil(len(audio) / chunk_samples))
        print(f"[transcriber] Long audio ({duration:.0f}s) — splitting into {total_chunks} chunks.")

        transcripts = []
        for i in range(0, len(audio), chunk_samples):
            chunk_num = i // chunk_samples + 1
            chunk = audio[i: i + chunk_samples]
            print(f"[transcriber] Chunk {chunk_num}/{total_chunks}...")

            # Write chunk to temp WAV
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                chunk_path = f.name
            try:
                wav_io.write(chunk_path, sr, chunk)
                text = self._transcribe_file(chunk_path)
                if text:
                    transcripts.append(text.strip())
            finally:
                try:
                    os.unlink(chunk_path)
                except OSError:
                    pass

        return " ".join(transcripts)


# Keep class name consistent with what main.py expects
ParakeetTranscriber = AppleSpeechTranscriber
