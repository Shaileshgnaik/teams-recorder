"""
teams_detector.py — Monitors for active Microsoft Teams meetings.

Detection strategy for Microsoft Teams v2 (Electron/WebRTC-based):
1. Is the Teams process running? (psutil)
2. Is the Microsoft Teams Audio virtual device producing non-silent audio? (sounddevice probe)

Key insight: Teams v2 uses WebRTC audio which bypasses traditional coreaudiod IPC
connections (lsof shows nothing). Instead, we probe the "Microsoft Teams Audio"
virtual device — it carries live call audio when a call is active, and is silent
when Teams is open but idle.

When a call starts  → on_join callback fires  → recording begins
When a call ends    → on_leave callback fires → recording stops + pipeline runs
"""

import threading
import numpy as np
from typing import Callable, Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[detector] WARNING: psutil not installed. Teams auto-detection disabled.")

try:
    import sounddevice as sd
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    SOUNDDEVICE_AVAILABLE = False
    print("[detector] WARNING: sounddevice not installed. Teams auto-detection disabled.")


# Process name fragments for Microsoft Teams variants (matched case-insensitively)
TEAMS_PROCESS_NAMES = ("teams", "msteams", "ms-teams")

# Keywords to find the Microsoft Teams Audio virtual device
TEAMS_DEVICE_KEYWORDS = ("microsoft teams audio", "teams audio")

# Audio RMS threshold: above this = call audio present, below = silence/idle
# Teams virtual device noise floor is effectively 0; any call audio is >> 0.0005
AUDIO_ACTIVE_THRESHOLD = 0.0005

# Duration (seconds) of audio to sample when probing for call activity
PROBE_DURATION = 0.3


class TeamsDetector:
    """
    Background monitor for Teams meeting activity.

    Detection approach:
    - Polls every 5 seconds
    - If Teams is running AND its virtual audio device has non-silent audio → call detected
    - If audio goes silent after a call → call ended

    Usage:
        detector = TeamsDetector()
        detector.start(
            on_join=lambda: print("Call started"),
            on_leave=lambda: print("Call ended")
        )
        detector.stop()
    """

    POLL_INTERVAL = 5  # seconds between checks

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._in_call = False
        self._on_join: Optional[Callable] = None
        self._on_leave: Optional[Callable] = None
        self._teams_device_index: Optional[int] = None

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def start(self, on_join: Callable, on_leave: Callable) -> None:
        if not PSUTIL_AVAILABLE or not SOUNDDEVICE_AVAILABLE:
            print("[detector] Required libraries missing — auto-detection disabled.")
            return

        self._on_join = on_join
        self._on_leave = on_leave
        self._stop_event.clear()
        self._in_call = False
        self._teams_device_index = self._find_teams_device()

        if self._teams_device_index is None:
            print("[detector] Microsoft Teams Audio device not found — "
                  "auto-detection disabled. Start Teams first.")
        else:
            print(f"[detector] Teams Audio device: index {self._teams_device_index}. "
                  "Polling every 5s.")

        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="TeamsDetector"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        print("[detector] Teams detection stopped.")

    @property
    def in_call(self) -> bool:
        return self._in_call

    # ---------------------------------------------------------------------- #
    #  Polling loop
    # ---------------------------------------------------------------------- #

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                currently_in_call = self._is_teams_in_call()

                if currently_in_call and not self._in_call:
                    self._in_call = True
                    print("[detector] Teams call started — triggering auto-record.")
                    if self._on_join:
                        self._on_join()

                elif not currently_in_call and self._in_call:
                    self._in_call = False
                    print("[detector] Teams call ended — stopping recording.")
                    if self._on_leave:
                        self._on_leave()

            except Exception as e:
                print(f"[detector] Poll error (non-fatal): {e}")

            self._stop_event.wait(timeout=self.POLL_INTERVAL)

    # ---------------------------------------------------------------------- #
    #  Detection logic
    # ---------------------------------------------------------------------- #

    def _is_teams_in_call(self) -> bool:
        """
        Return True only when Teams is actively in a call.

        Two-stage check:
        1. Teams process is running (psutil).
        2. Teams Audio virtual device has non-silent audio (sounddevice probe).
        """
        if not self._teams_is_running():
            return False

        # Re-discover the device in case Teams restarted
        if self._teams_device_index is None:
            self._teams_device_index = self._find_teams_device()
            if self._teams_device_index is None:
                return False

        return self._teams_audio_is_active()

    def _teams_is_running(self) -> bool:
        """Return True if any Teams process is found."""
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                name = (proc.info.get("name") or "").lower()
                exe = (proc.info.get("exe") or "").lower()
                if any(t in name for t in TEAMS_PROCESS_NAMES) or \
                   any(t in exe for t in TEAMS_PROCESS_NAMES):
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return False

    def _teams_audio_is_active(self) -> bool:
        """
        Probe the Teams Audio virtual device for 300ms and check if it's
        producing non-silent audio. Returns True if RMS > threshold.

        Teams routes all call audio through this virtual device. When idle,
        the device produces silence (RMS ≈ 0). During a call, audio is present.
        """
        try:
            device_info = sd.query_devices(self._teams_device_index)
            native_sr = int(device_info["default_samplerate"])
            samples = int(native_sr * PROBE_DURATION)

            audio = sd.rec(
                samples,
                samplerate=native_sr,
                channels=1,
                device=self._teams_device_index,
                dtype="float32",
            )
            sd.wait()

            rms = float(np.sqrt(np.mean(audio ** 2)))
            print(f"[detector] Teams Audio RMS: {rms:.6f} "
                  f"({'active' if rms > AUDIO_ACTIVE_THRESHOLD else 'silent'})")
            return rms > AUDIO_ACTIVE_THRESHOLD

        except Exception as e:
            print(f"[detector] Audio probe error: {e}")
            # Device unavailable — reset so we re-discover next poll
            self._teams_device_index = None
            return False

    def _find_teams_device(self) -> Optional[int]:
        """Find the Microsoft Teams Audio virtual device index."""
        try:
            for i, dev in enumerate(sd.query_devices()):
                name = dev["name"].lower()
                if any(k in name for k in TEAMS_DEVICE_KEYWORDS):
                    if dev["max_input_channels"] > 0:
                        return i
        except Exception as e:
            print(f"[detector] Device discovery error: {e}")
        return None
