"""
main.py — macOS menu bar app for Teams meeting recording and note generation.

Thread-safety:
    rumps runs on the main thread. Background threads post (action, value) tuples
    to two queues, drained every 0.5s by @rumps.timer callbacks on the main thread:
      - _ui_queue:      UI-only updates (icon, status label, notifications)
      - _control_queue: control actions (auto-start / auto-stop recording)
"""

import os
import sys
import queue
import threading
import traceback

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

import rumps

from recorder import AudioRecorder
from transcriber import ParakeetTranscriber
from note_generator import ClaudeNoteGenerator
from teams_detector import TeamsDetector
from utils import save_markdown, ensure_notes_dir, NOTES_DIR

ICON_IDLE = "⚪"
ICON_RECORDING = "🔴"
ICON_PROCESSING = "⏳"


class TeamsRecorderApp(rumps.App):
    """
    macOS menu bar app for Teams meeting recording.

    Menu:
        ⚪  Teams Recorder
        ─────────────────
        Status: Idle               ← display only
        ─────────────────
        ▶  Start Recording
        ■  Stop Recording          ← disabled when not recording
        ─────────────────
        📁  Open Notes Folder
        ─────────────────
        Auto-detect Teams: ON ✓    ← toggleable
        ─────────────────
        Quit
    """

    def __init__(self):
        super().__init__(
            name="Teams Recorder",
            title=ICON_IDLE,
            quit_button=None,
        )

        self._recorder = AudioRecorder()
        self._transcriber = ParakeetTranscriber()
        self._note_gen = ClaudeNoteGenerator()
        self._detector = TeamsDetector()

        self._recording = False
        self._auto_detect = True
        self._pipeline_running = False

        # Background threads post tuples here; timer callbacks drain them on main thread.
        # _ui_queue actions:      ('title', str) | ('status', str) | ('notify', tuple) |
        #                         ('reset', None) | ('reset_error', str)
        # _control_queue actions: ('auto_start', None) | ('auto_stop', None)
        self._ui_queue = queue.Queue()
        self._control_queue = queue.Queue()

        # Menu items
        self._status_item = rumps.MenuItem("Status: Idle")
        self._status_item.set_callback(None)

        self._start_item = rumps.MenuItem("▶  Start Recording", callback=self._on_start_clicked)
        self._stop_item = rumps.MenuItem("■  Stop Recording", callback=self._on_stop_clicked)
        self._stop_item.set_callback(None)

        self.menu = [
            self._status_item,
            None,
            self._start_item,
            self._stop_item,
            None,
            rumps.MenuItem("📁  Open Notes Folder", callback=self._on_open_folder),
            None,
            rumps.MenuItem("Auto-detect Teams: ON ✓", callback=self._on_toggle_autodetect),
            None,
            rumps.MenuItem("Quit", callback=self._on_quit),
        ]

        ensure_notes_dir()
        self._detector.start(
            on_join=self._on_teams_joined,
            on_leave=self._on_teams_left,
        )
        print("[app] Teams Recorder started.")

    # ---------------------------------------------------------------------- #
    #  Timers — drain queues on the main thread every 0.5s
    # ---------------------------------------------------------------------- #

    @rumps.timer(0.5)
    def _ui_tick(self, _):
        """Apply pending UI updates posted by background threads."""
        while True:
            try:
                action, value = self._ui_queue.get_nowait()
            except queue.Empty:
                break

            if action == "title":
                self.title = value
            elif action == "status":
                self._status_item.title = value
            elif action == "notify":
                title, subtitle, message = value
                rumps.notification(title=title, subtitle=subtitle, message=message)
            elif action == "reset":
                self._reset_ui()
            elif action == "reset_error":
                self._reset_ui(error=value)

    @rumps.timer(0.5)
    def _control_tick(self, _):
        """Handle auto-start/stop control actions posted by TeamsDetector thread."""
        while True:
            try:
                action, _ = self._control_queue.get_nowait()
            except queue.Empty:
                break

            if action == "auto_start":
                if not self._recording and not self._pipeline_running:
                    self._start_recording(manual=False)
            elif action == "auto_stop":
                if self._recording:
                    self._stop_recording_and_process()

    # ---------------------------------------------------------------------- #
    #  Menu callbacks (main thread)
    # ---------------------------------------------------------------------- #

    def _on_start_clicked(self, _):
        if not self._recording and not self._pipeline_running:
            self._start_recording(manual=True)

    def _on_stop_clicked(self, _):
        if self._recording:
            self._stop_recording_and_process()

    def _on_open_folder(self, _):
        import subprocess
        ensure_notes_dir()
        subprocess.Popen(["open", NOTES_DIR])

    def _on_toggle_autodetect(self, sender):
        self._auto_detect = not self._auto_detect
        if self._auto_detect:
            sender.title = "Auto-detect Teams: ON ✓"
            self._detector.start(
                on_join=self._on_teams_joined,
                on_leave=self._on_teams_left,
            )
        else:
            sender.title = "Auto-detect Teams: OFF"
            self._detector.stop()
        print(f"[app] Auto-detect {'enabled' if self._auto_detect else 'disabled'}.")

    def _on_quit(self, _):
        self._detector.stop()
        if self._recording:
            try:
                self._recorder.stop()
            except Exception:
                pass
        rumps.quit_application()

    # ---------------------------------------------------------------------- #
    #  TeamsDetector callbacks (background thread → post to _control_queue)
    # ---------------------------------------------------------------------- #

    def _on_teams_joined(self):
        if self._auto_detect and not self._recording and not self._pipeline_running:
            self._control_queue.put(("auto_start", None))

    def _on_teams_left(self):
        if self._auto_detect and self._recording:
            self._control_queue.put(("auto_stop", None))

    # ---------------------------------------------------------------------- #
    #  Recording control (main thread only)
    # ---------------------------------------------------------------------- #

    def _start_recording(self, manual: bool = False) -> None:
        """Start audio capture. Must be called from main thread."""
        if self._recording:
            return

        self._recording = True
        self.title = ICON_RECORDING
        self._status_item.title = f"Status: Recording ({'manual' if manual else 'auto'})..."
        self._start_item.set_callback(None)
        self._stop_item.set_callback(self._on_stop_clicked)

        try:
            self._recorder.start()
            print(f"[app] Recording started ({'manual' if manual else 'auto'}).")
            if not manual:
                rumps.notification(
                    title="Teams Recorder",
                    subtitle="Recording started automatically",
                    message="Teams call detected. Recording in progress.",
                )
        except Exception as e:
            self._recording = False
            self._reset_ui(error=str(e))
            rumps.notification(
                title="Teams Recorder",
                subtitle="Recording failed to start",
                message=str(e),
            )

    def _stop_recording_and_process(self) -> None:
        """Stop recording and launch the pipeline in a background thread. Main thread only."""
        if not self._recording:
            return

        self._recording = False
        self._pipeline_running = True
        self.title = ICON_PROCESSING
        self._status_item.title = "Status: Stopping recording..."
        self._start_item.set_callback(None)
        self._stop_item.set_callback(None)

        # Capture duration BEFORE stop() — stop() sets _recording=False which
        # makes the duration property return 0.
        duration = self._recorder.duration

        try:
            wav_path = self._recorder.stop()
        except Exception as e:
            self._pipeline_running = False
            self._reset_ui(error=f"Stop error: {e}")
            return

        threading.Thread(
            target=self._run_pipeline,
            args=(wav_path, duration),
            daemon=True,
            name="Pipeline",
        ).start()

    # ---------------------------------------------------------------------- #
    #  Pipeline (background thread — posts to _ui_queue for UI updates)
    # ---------------------------------------------------------------------- #

    def _run_pipeline(self, wav_path: str, duration: int) -> None:
        """Transcribe → generate notes → save → notify. Runs in background thread."""
        try:
            self._ui_queue.put(("status", "Status: Transcribing audio (Parakeet)..."))
            transcript = self._transcriber.transcribe(wav_path)

            if not transcript.strip():
                raise ValueError(
                    "Transcription returned empty text. Was there audio in the recording?"
                )

            self._ui_queue.put(("status", "Status: Generating notes (Claude)..."))
            notes_markdown = self._note_gen.generate(transcript, duration_seconds=duration)

            saved_path = save_markdown(notes_markdown)
            print(f"[app] Notes saved: {saved_path}")

            self._ui_queue.put(("notify", (
                "Meeting notes ready ✓",
                os.path.basename(saved_path),
                "Saved to Documents/MeetingNotes",
            )))
            self._ui_queue.put(("reset", None))

            try:
                os.remove(wav_path)
            except OSError:
                pass

        except Exception as e:
            print(f"[app] Pipeline error: {e}")
            traceback.print_exc()
            self._ui_queue.put(("notify", (
                "Teams Recorder — Error",
                "Pipeline failed",
                str(e)[:200],
            )))
            self._ui_queue.put(("reset_error", str(e)[:80]))
        finally:
            self._pipeline_running = False

    # ---------------------------------------------------------------------- #
    #  UI helpers (main thread only)
    # ---------------------------------------------------------------------- #

    def _reset_ui(self, error: str = None) -> None:
        self.title = ICON_IDLE
        self._status_item.title = (
            f"Status: Error — {error[:60]}" if error else "Status: Idle"
        )
        self._start_item.set_callback(self._on_start_clicked)
        self._stop_item.set_callback(None)


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    if sys.platform != "darwin":
        print("ERROR: This app only runs on macOS.")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from AppKit import NSAlert, NSWarningAlertStyle
            alert = NSAlert.alloc().init()
            alert.setMessageText_("ANTHROPIC_API_KEY not set")
            alert.setInformativeText_(
                "Add your key to the .env file:\n\nANTHROPIC_API_KEY=sk-ant-...\n\nThen restart."
            )
            alert.setAlertStyle_(NSWarningAlertStyle)
            alert.addButtonWithTitle_("OK")
            alert.runModal()
        except Exception:
            print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env and restart.")
        sys.exit(1)

    TeamsRecorderApp().run()
