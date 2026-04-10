"""
teams_detector.py — Monitors for active Microsoft Teams meetings.

Detection strategy for Microsoft Teams v2 (Electron/WebRTC) on macOS 14+ (Sonoma):

macOS Sonoma intentionally blocks kAudioDevicePropertyDeviceIsRunningSomewhere
(returns error -1) and IOAudioEngine state queries as a privacy protection.
All CoreAudio/IOKit-based mic-usage detection is unavailable to unprivileged apps.

Instead, we monitor the MSTeams process resource usage. When a call starts,
MSTeams consistently gains extra threads and file descriptors. We capture a
baseline snapshot the first time MSTeams is seen, then declare a call when
both thread count and FD count exceed their baselines by set thresholds.

Thresholds are configurable via environment variables so they can be tuned
per machine without touching source code:
    TEAMS_THREAD_DELTA=2   (default: 2)
    TEAMS_FD_DELTA=4       (default: 4)

Run app/diagnose_call.py idle and in-call to find the right values for your Mac.

Secondary signal: new CoreAudio input device appeared (catches AirPods switching
from A2DP → HFP mode when Teams activates them for a call).

When a call starts  → on_join callback fires  → recording begins
When a call ends    → on_leave callback fires → recording stops + pipeline runs
"""

import os
import ctypes
import threading
from ctypes.util import find_library
from typing import Callable, Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[detector] WARNING: psutil not installed. Teams auto-detection disabled.")

# CoreAudio / CoreFoundation via ctypes (used for secondary AirPods signal).
# Use find_library() so the path resolves correctly on any macOS version.
_ca_path = find_library("CoreAudio")
_cf_path = find_library("CoreFoundation")
if not _ca_path or not _cf_path:
    raise RuntimeError("CoreAudio or CoreFoundation framework not found. macOS required.")
_ca = ctypes.CDLL(_ca_path)
_cf = ctypes.CDLL(_cf_path)
_cf.CFStringGetCString.restype = ctypes.c_bool

_SYS_OBJ = ctypes.c_uint32(1)
_GLOB    = 0x676c6f62
_INPT    = 0x696e7074
_EL      = 0
_DEVLIST = 0x64657623
_DEVNAME = 0x6c6e616d
_STREAMS = 0x73746d23
_UTF8    = 0x08000100

# How many extra threads / FDs above baseline indicate an active call.
# Defaults based on observed deltas on Apple M3 / macOS 26.3 (+3 threads, +6 FDs).
# Override via environment variables if detection is unreliable on your machine:
#   export TEAMS_THREAD_DELTA=3
#   export TEAMS_FD_DELTA=5
THREAD_DELTA_THRESHOLD = int(os.environ.get("TEAMS_THREAD_DELTA", "2"))
FD_DELTA_THRESHOLD     = int(os.environ.get("TEAMS_FD_DELTA",     "4"))

VIRTUAL_DEVICE_KEYWORDS = (
    "teams audio", "zoomaudiodevice", "blackhole",
    "loopback", "soundflower", "virtual",
)
TEAMS_PROCESS_NAMES = ("teams", "msteams", "ms-teams")


class _Addr(ctypes.Structure):
    _fields_ = [('sel', ctypes.c_uint32), ('scope', ctypes.c_uint32), ('elem', ctypes.c_uint32)]


# ── CoreAudio helpers (secondary signal only) ──────────────────────────────────

def _ca_get_name(dev_id: int) -> str:
    a = _Addr(_DEVNAME, _GLOB, _EL)
    sz = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    cfstr = ctypes.c_void_p(0)
    ret = _ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(dev_id), ctypes.byref(a), 0, None, ctypes.byref(sz), ctypes.byref(cfstr)
    )
    if ret != 0 or not cfstr.value:
        return ''
    buf = ctypes.create_string_buffer(256)
    _cf.CFStringGetCString(cfstr, buf, 256, _UTF8)
    _cf.CFRelease(cfstr)
    return buf.value.decode('utf-8', errors='ignore')


def _find_physical_input_device_ids() -> set:
    """Return CoreAudio IDs of physical input-capable devices (excludes virtual)."""
    results = set()
    try:
        a = _Addr(_DEVLIST, _GLOB, _EL)
        sz = ctypes.c_uint32(0)
        _ca.AudioObjectGetPropertyDataSize(_SYS_OBJ, ctypes.byref(a), 0, None, ctypes.byref(sz))
        count = sz.value // 4
        buf = (ctypes.c_uint32 * count)()
        _ca.AudioObjectGetPropertyData(_SYS_OBJ, ctypes.byref(a), 0, None, ctypes.byref(sz), buf)
        for dev_id in buf:
            name = _ca_get_name(dev_id).lower()
            if not name or any(v in name for v in VIRTUAL_DEVICE_KEYWORDS):
                continue
            # Check for input streams
            ia = _Addr(_STREAMS, _INPT, _EL)
            isz = ctypes.c_uint32(0)
            ret = _ca.AudioObjectGetPropertyDataSize(
                ctypes.c_uint32(dev_id), ctypes.byref(ia), 0, None, ctypes.byref(isz)
            )
            if ret == 0 and isz.value > 0:
                results.add(dev_id)
    except Exception:
        pass
    return results


class TeamsDetector:
    """
    Background monitor for Teams meeting activity.

    Primary signal:   MSTeams process thread/FD count delta from baseline.
    Secondary signal: New CoreAudio input device (AirPods HFP switch).

    Polls every 5 seconds. Fires on_join / on_leave callbacks.
    """

    POLL_INTERVAL = 5

    def __init__(self):
        self._thread:    Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._in_call    = False
        self._on_join:   Optional[Callable] = None
        self._on_leave:  Optional[Callable] = None

        # Baseline MSTeams resource usage (captured on first sighting)
        self._baseline_threads: int = 0
        self._baseline_fds:     int = 0
        self._baseline_captured = False

        # Baseline CoreAudio input devices (for AirPods signal)
        self._baseline_input_ids: set = set()

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def start(self, on_join: Callable, on_leave: Callable) -> None:
        if not PSUTIL_AVAILABLE:
            print("[detector] psutil missing — auto-detection disabled.")
            return

        self._on_join  = on_join
        self._on_leave = on_leave
        self._stop_event.clear()
        self._in_call  = False
        self._baseline_captured = False

        # Capture CoreAudio input device baseline
        self._baseline_input_ids = _find_physical_input_device_ids()
        names = {d: _ca_get_name(d) for d in self._baseline_input_ids}
        print(f"[detector] Baseline input devices: {names}")

        # Capture MSTeams process baseline (if Teams is already running)
        self._capture_msteams_baseline()

        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="TeamsDetector"
        )
        self._thread.start()
        print("[detector] Teams call detection started (polling every 5s).")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        print("[detector] Teams detection stopped.")

    @property
    def in_call(self) -> bool:
        return self._in_call

    # ------------------------------------------------------------------ #
    #  Polling loop
    # ------------------------------------------------------------------ #

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                currently_in_call = self._is_teams_in_call()

                if currently_in_call and not self._in_call:
                    self._in_call = True
                    print("[detector] Teams call detected — triggering auto-record.")
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

    # ------------------------------------------------------------------ #
    #  Detection logic
    # ------------------------------------------------------------------ #

    def _is_teams_in_call(self) -> bool:
        """
        Primary:   MSTeams thread/FD count rose above baseline by threshold.
        Secondary: New physical microphone device appeared (AirPods HFP switch).
        """
        # ---- Primary: MSTeams process resource delta ----
        msteams = self._get_msteams_proc()
        if msteams is None:
            print("[detector] MSTeams not running → idle")
            return False

        if not self._baseline_captured:
            # Teams just started; capture baseline now (assume not yet in a call)
            self._capture_msteams_baseline()

        try:
            threads = msteams.num_threads()
            fds     = msteams.num_fds()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

        t_delta = threads - self._baseline_threads
        f_delta = fds     - self._baseline_fds

        print(f"[detector] MSTeams threads={threads}({t_delta:+d}) "
              f"fds={fds}({f_delta:+d}) "
              f"thresholds: Δthreads≥{THREAD_DELTA_THRESHOLD} and Δfds≥{FD_DELTA_THRESHOLD}")

        if t_delta >= THREAD_DELTA_THRESHOLD and f_delta >= FD_DELTA_THRESHOLD:
            print(f"[detector] Primary signal fired (Δthreads={t_delta}, Δfds={f_delta}) → IN CALL")
            return True

        # ---- Secondary: new CoreAudio input device (AirPods HFP) ----
        current_input_ids = _find_physical_input_device_ids()
        new_devices = current_input_ids - self._baseline_input_ids
        if new_devices:
            names = [f"[{d}] {_ca_get_name(d)}" for d in new_devices]
            print(f"[detector] Secondary signal — new input device(s): {names} → IN CALL")
            return True

        print(f"[detector] No signal → idle")
        return False

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _get_msteams_proc(self) -> Optional["psutil.Process"]:
        """Return the MSTeams main process object, or None."""
        try:
            for proc in psutil.process_iter(["name"]):
                if proc.info.get("name") == "MSTeams":
                    return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return None

    def _capture_msteams_baseline(self) -> None:
        """Snapshot MSTeams thread and FD counts as the idle baseline."""
        proc = self._get_msteams_proc()
        if proc is None:
            return
        try:
            self._baseline_threads  = proc.num_threads()
            self._baseline_fds      = proc.num_fds()
            self._baseline_captured = True
            print(f"[detector] MSTeams baseline: threads={self._baseline_threads}, "
                  f"fds={self._baseline_fds}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def _teams_is_running(self) -> bool:
        return self._get_msteams_proc() is not None
