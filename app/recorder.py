"""
recorder.py — Captures Teams meeting audio using sounddevice.

Strategy (no virtual audio driver or ScreenCaptureKit required):
  - Microsoft Teams creates a virtual audio device called "Microsoft Teams Audio"
    that exposes the full call audio (your mic + remote participants) as an input.
  - We capture from that device using sounddevice.
  - Simultaneously capture the Mac microphone as a backup stream.
  - Mix both streams into a single 16kHz mono WAV file for transcription.

If the Teams Audio device is not found (e.g., Teams not yet running),
falls back to microphone-only capture.
"""

import time
import threading
import numpy as np
import sounddevice as sd

from utils import mix_audio, write_wav, get_wav_tmp_path, SAMPLE_RATE


TEAMS_DEVICE_KEYWORDS = ("microsoft teams audio", "teams audio")


class AudioRecorder:
    """
    Records Teams meeting audio via the Microsoft Teams virtual audio device.

    Usage:
        recorder = AudioRecorder()
        recorder.start()
        # ... meeting in progress ...
        wav_path = recorder.stop()   # returns path to mixed WAV file
    """

    def __init__(self):
        self._mic_chunks: list[np.ndarray] = []
        self._teams_chunks: list[np.ndarray] = []
        self._mic_lock = threading.Lock()
        self._teams_lock = threading.Lock()
        self._recording = False
        self._start_time: float = 0.0
        self._mic_stream: sd.InputStream | None = None
        self._teams_stream: sd.InputStream | None = None

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def start(self) -> None:
        """Start recording mic + Teams virtual audio device."""
        if self._recording:
            return

        self._mic_chunks.clear()
        self._teams_chunks.clear()
        self._recording = True
        self._start_time = time.time()

        # 1. Microphone — always available
        self._start_stream(
            device=None,
            chunks=self._mic_chunks,
            lock=self._mic_lock,
            label="mic",
        )

        # 2. Microsoft Teams Audio virtual device — captures full call audio
        teams_idx = self._find_teams_device()
        if teams_idx is not None:
            self._start_stream(
                device=teams_idx,
                chunks=self._teams_chunks,
                lock=self._teams_lock,
                label="Teams Audio",
            )
        else:
            print("[recorder] 'Microsoft Teams Audio' device not found — "
                  "recording mic only. Make sure Teams is running.")

        print("[recorder] Recording started.")

    def stop(self) -> str:
        """
        Stop recording, mix streams, write WAV.

        Returns:
            Path to the mixed WAV file (e.g., /tmp/meeting_20260409_143022.wav)
        """
        if not self._recording:
            raise RuntimeError("Not currently recording.")

        self._recording = False

        for stream in (self._mic_stream, self._teams_stream):
            if stream:
                stream.stop()
                stream.close()
        self._mic_stream = None
        self._teams_stream = None

        duration = int(time.time() - self._start_time)
        print(f"[recorder] Stopped. Duration: {duration}s")

        with self._mic_lock:
            mic_audio = (np.concatenate(self._mic_chunks)
                         if self._mic_chunks else np.zeros(1, dtype=np.float32))
        with self._teams_lock:
            teams_audio = (np.concatenate(self._teams_chunks)
                           if self._teams_chunks else np.zeros(1, dtype=np.float32))

        mixed = mix_audio(mic_audio, teams_audio)
        wav_path = get_wav_tmp_path()
        write_wav(mixed, wav_path)
        print(f"[recorder] WAV saved: {wav_path}")
        return wav_path

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def duration(self) -> int:
        """Current recording duration in seconds. Returns 0 when not recording."""
        if not self._recording:
            return 0
        return int(time.time() - self._start_time)

    # ---------------------------------------------------------------------- #
    #  Internal helpers
    # ---------------------------------------------------------------------- #

    def _start_stream(self, device, chunks, lock, label: str) -> None:
        """Open a sounddevice InputStream for the given device."""
        def callback(indata: np.ndarray, frames: int, time_info, status):
            if status:
                print(f"[recorder/{label}] {status}")
            if self._recording:
                with lock:
                    chunks.append(indata[:, 0].copy())  # mono: channel 0

        try:
            stream = sd.InputStream(
                device=device,
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=callback,
            )
            stream.start()

            if label == "mic":
                self._mic_stream = stream
            else:
                self._teams_stream = stream

            dev_name = sd.query_devices(device)["name"] if device is not None else "default mic"
            print(f"[recorder] Started {label}: {dev_name}")

        except Exception as e:
            print(f"[recorder] Could not open {label} stream: {e}")

    @staticmethod
    def _find_teams_device() -> int | None:
        """
        Find the Microsoft Teams virtual audio input device index.

        Teams creates a virtual audio device "Microsoft Teams Audio" while running.
        Capturing from its input channel gives the full call audio
        (local mic + remote participants as processed by Teams).
        """
        try:
            for i, device in enumerate(sd.query_devices()):
                name = device.get("name", "").lower()
                if (any(kw in name for kw in TEAMS_DEVICE_KEYWORDS)
                        and device.get("max_input_channels", 0) > 0):
                    print(f"[recorder] Found Teams device [{i}]: {device['name']}")
                    return i
        except Exception as e:
            print(f"[recorder] Device search error: {e}")
        return None
