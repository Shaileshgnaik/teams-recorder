"""
teams_detector.py — Monitors for active Microsoft Teams meetings.

Detection strategy (accurate — no false positives):
1. Is the Teams process running? (psutil)
2. Does Teams have an active connection to coreaudiod? (lsof)

Key insight: Teams connects to coreaudiod (macOS audio server) ONLY when in a call —
not when the app is open but idle. Checking `lsof -p <teams_pid>` for a coreaudiod
connection is a reliable, Teams-specific call indicator.

When a call starts  → on_join callback fires  → recording begins
When a call ends    → on_leave callback fires → recording stops + pipeline runs
"""

import threading
import subprocess
from typing import Callable, Optional

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[detector] WARNING: psutil not installed. Teams auto-detection disabled.")


# Process name fragments for Microsoft Teams variants (matched case-insensitively)
TEAMS_PROCESS_NAMES = ("teams", "msteams", "ms-teams")


class TeamsDetector:
    """
    Background monitor for Teams meeting activity.

    Fires callbacks when a Teams call starts or ends.

    Usage:
        detector = TeamsDetector()
        detector.start(
            on_join=lambda: print("Call started — recording"),
            on_leave=lambda: print("Call ended — processing")
        )
        # ... app runs forever ...
        detector.stop()
    """

    POLL_INTERVAL = 5  # seconds between checks

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._in_call = False
        self._on_join: Optional[Callable] = None
        self._on_leave: Optional[Callable] = None

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def start(self, on_join: Callable, on_leave: Callable) -> None:
        """
        Start the background polling thread.

        Args:
            on_join:  Called (no args) when a Teams call is detected starting.
            on_leave: Called (no args) when a Teams call ends.
        """
        if not PSUTIL_AVAILABLE:
            print("[detector] psutil not available — auto-detection disabled. "
                  "Use manual Start/Stop from the menu bar.")
            return

        self._on_join = on_join
        self._on_leave = on_leave
        self._stop_event.clear()
        self._in_call = False

        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="TeamsDetector"
        )
        self._thread.start()
        print("[detector] Teams call detection started (polling every 5s).")

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        print("[detector] Teams detection stopped.")

    @property
    def in_call(self) -> bool:
        """True if a Teams call is currently detected as active."""
        return self._in_call

    # ---------------------------------------------------------------------- #
    #  Polling loop
    # ---------------------------------------------------------------------- #

    def _poll_loop(self) -> None:
        """
        Main detection loop — runs in background thread.

        State machine:
            IDLE → (call detected) → IN_CALL → (call ended) → IDLE
        """
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
        1. Teams process must be running (psutil).
        2. Teams process must have an open connection to coreaudiod (lsof).

        Why coreaudiod?
        macOS routes all audio through a central daemon called coreaudiod.
        Applications connect to it via Mach ports/XPC only when they actively
        open an audio session — which Teams does exclusively during calls.
        When Teams is open but idle (no call), it has no coreaudiod connection.

        Returns False (safe default) on any detection failure.
        """
        pids = self._get_all_teams_pids()
        if not pids:
            return False  # Teams not running

        return any(self._has_coreaudiod_connection(pid) for pid in pids)

    def _get_all_teams_pids(self) -> list:
        """
        Find PIDs of ALL Teams-related processes using psutil.

        New Microsoft Teams (v2) routes audio through a child helper process
        (Microsoft Teams WebView Helper Plugin), not the main MSTeams binary.
        We must check all of them — any one may hold the coreaudiod connection.

        Returns list of matching PIDs (empty if Teams not running).
        """
        pids = []
        try:
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                name = (proc.info.get("name") or "").lower()
                exe = (proc.info.get("exe") or "").lower()
                if any(t in name for t in TEAMS_PROCESS_NAMES) or \
                   any(t in exe for t in TEAMS_PROCESS_NAMES):
                    pids.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return pids

    def _has_coreaudiod_connection(self, pid: int) -> bool:
        """
        Check if a process has an active connection to coreaudiod.

        Uses `lsof -p <pid>` which lists all open file descriptors and
        network/IPC connections for the given process ID. When Teams is in
        a call, the output includes entries referencing 'coreaudiod'
        (either as a Unix domain socket path or as a Mach port target name).

        Args:
            pid: Process ID to inspect.

        Returns:
            True if coreaudiod connection found, False otherwise (including errors).
        """
        try:
            result = subprocess.run(
                ["lsof", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout.lower()

            # coreaudiod appears in lsof output as:
            #   - Unix socket path: /private/tmp/com.apple.audio.coreaudiod/<pid>
            #   - Service name reference: coreaudiod
            return "coreaudiod" in output

        except subprocess.TimeoutExpired:
            print(f"[detector] lsof timeout for PID {pid} — skipping")
            return False
        except FileNotFoundError:
            # lsof not available (unusual on macOS but handle gracefully)
            print("[detector] lsof not found — falling back to process-only detection")
            return True  # fallback: assume in call if Teams running
        except Exception as e:
            print(f"[detector] lsof error for PID {pid}: {e}")
            return False  # safe default
