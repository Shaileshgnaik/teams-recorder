"""
teams_detector.py — Monitors for active Microsoft Teams meetings.

Detection strategy for Microsoft Teams v2 (Electron/WebRTC):

1. Teams process is running (psutil).
2. Any physical audio INPUT device has DeviceIsRunningSomewhere=True via CoreAudio.
   Teams MUST open the microphone when a call starts. This flips the CoreAudio
   'DeviceIsRunningSomewhere' flag from False → True for the active mic device.
   When the call ends and no other app uses the mic, the flag goes back to False.

Virtual devices (Microsoft Teams Audio, ZoomAudioDevice, etc.) are excluded —
only physical input devices (built-in mic, AirPods, USB headsets) are checked.

When a call starts  → on_join callback fires  → recording begins
When a call ends    → on_leave callback fires → recording stops + pipeline runs
"""

import ctypes
import threading
from typing import Callable, Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[detector] WARNING: psutil not installed. Teams auto-detection disabled.")

# CoreAudio / CoreFoundation via ctypes
_ca = ctypes.CDLL('/System/Library/Frameworks/CoreAudio.framework/CoreAudio')
_cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
_cf.CFStringGetCString.restype  = ctypes.c_bool
_cf.CFStringGetLength.restype   = ctypes.c_long

# CoreAudio constants
_SYS_OBJ   = ctypes.c_uint32(1)       # kAudioObjectSystemObject
_GLOB      = 0x676c6f62               # kAudioObjectPropertyScopeGlobal  ('glob')
_INPT      = 0x696e7074               # kAudioObjectPropertyScopeInput   ('inpt')
_EL        = 0
_DEVLIST   = 0x64657623               # kAudioHardwarePropertyDevices    ('dev#')
_DEVNAME   = 0x6c6e616d               # kAudioObjectPropertyName         ('lnam')
_RUNNING   = 0x68737273               # kAudioDevicePropertyDeviceIsRunningSomewhere ('hsrs')
_INCHAN    = 0x6368616e               # kAudioDevicePropertyStreamConfiguration ('chan') — used to check input channels
_STREAMS   = 0x73746d23               # kAudioDevicePropertyStreams      ('stm#')
_UTF8      = 0x08000100


class _Addr(ctypes.Structure):
    _fields_ = [('sel', ctypes.c_uint32), ('scope', ctypes.c_uint32), ('elem', ctypes.c_uint32)]


# Virtual device name fragments to EXCLUDE from mic detection
VIRTUAL_DEVICE_KEYWORDS = (
    "teams audio", "zoomaudiodevice", "blackhole",
    "loopback", "soundflower", "virtual",
)

# Process name fragments for Microsoft Teams
TEAMS_PROCESS_NAMES = ("teams", "msteams", "ms-teams")


def _ca_get_device_ids() -> list:
    a = _Addr(_DEVLIST, _GLOB, _EL)
    sz = ctypes.c_uint32(0)
    _ca.AudioObjectGetPropertyDataSize(_SYS_OBJ, ctypes.byref(a), 0, None, ctypes.byref(sz))
    count = sz.value // 4
    buf = (ctypes.c_uint32 * count)()
    _ca.AudioObjectGetPropertyData(_SYS_OBJ, ctypes.byref(a), 0, None, ctypes.byref(sz), buf)
    return list(buf)


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


def _ca_has_input_streams(dev_id: int) -> bool:
    """Return True if this device has at least one input stream (i.e. it's a mic)."""
    a = _Addr(_STREAMS, _INPT, _EL)
    sz = ctypes.c_uint32(0)
    ret = _ca.AudioObjectGetPropertyDataSize(
        ctypes.c_uint32(dev_id), ctypes.byref(a), 0, None, ctypes.byref(sz)
    )
    return ret == 0 and sz.value > 0


def _ca_is_running(dev_id: int) -> bool:
    """Return True if any process currently has an active audio session on this device."""
    a = _Addr(_RUNNING, _GLOB, _EL)
    val = ctypes.c_uint32(0)
    sz  = ctypes.c_uint32(4)
    ret = _ca.AudioObjectGetPropertyData(
        ctypes.c_uint32(dev_id), ctypes.byref(a), 0, None, ctypes.byref(sz), ctypes.byref(val)
    )
    return ret == 0 and val.value != 0


class TeamsDetector:
    """
    Background monitor for Teams meeting activity.

    Polls every 5 seconds. Fires on_join when Teams is running and a physical
    microphone becomes active (Teams opened it for a call). Fires on_leave when
    the microphone is no longer active.
    """

    POLL_INTERVAL = 5

    def __init__(self):
        self._thread:   Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._in_call   = False
        self._on_join:  Optional[Callable] = None
        self._on_leave: Optional[Callable] = None
        # Physical input device IDs discovered at startup
        self._input_device_ids: list = []

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
        self._input_device_ids = self._find_physical_input_devices()

        if self._input_device_ids:
            names = [_ca_get_name(d) for d in self._input_device_ids]
            print(f"[detector] Watching mic(s): {', '.join(names)}")
        else:
            print("[detector] No physical input devices found — detection may be limited.")

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
        Return True when:
          1. A Teams process is running, AND
          2. At least one physical microphone is active (DeviceIsRunningSomewhere=True).
        """
        if not self._teams_is_running():
            return False

        # Re-discover input devices if list is empty (hot-plug)
        if not self._input_device_ids:
            self._input_device_ids = self._find_physical_input_devices()

        active = [
            _ca_get_name(d)
            for d in self._input_device_ids
            if _ca_is_running(d)
        ]
        in_call = len(active) > 0
        print(f"[detector] Mic check — active: {active or 'none'} → {'IN CALL' if in_call else 'idle'}")
        return in_call

    def _teams_is_running(self) -> bool:
        try:
            for proc in psutil.process_iter(["name", "exe"]):
                name = (proc.info.get("name") or "").lower()
                exe  = (proc.info.get("exe")  or "").lower()
                if any(t in name for t in TEAMS_PROCESS_NAMES) or \
                   any(t in exe  for t in TEAMS_PROCESS_NAMES):
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return False

    def _find_physical_input_devices(self) -> list:
        """
        Return CoreAudio device IDs for physical input devices only.
        Excludes virtual devices (Teams Audio, ZoomAudioDevice, BlackHole, etc.).
        """
        results = []
        try:
            for dev_id in _ca_get_device_ids():
                name = _ca_get_name(dev_id).lower()
                if not name:
                    continue
                if any(v in name for v in VIRTUAL_DEVICE_KEYWORDS):
                    continue
                if _ca_has_input_streams(dev_id):
                    results.append(dev_id)
        except Exception as e:
            print(f"[detector] Device discovery error: {e}")
        return results
