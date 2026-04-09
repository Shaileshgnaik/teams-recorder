"""
overlay.py — Always-on-top floating status window for Teams Recorder.

Shows recording state as a small pill in the corner of the screen.
Stays visible even when the menu bar icon is hidden by Teams' call widget.
User can drag it to reposition.
"""

from AppKit import (
    NSPanel,
    NSWindowStyleMaskBorderless,
    NSFloatingWindowLevel,
    NSColor,
    NSTextField,
    NSFont,
    NSScreen,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
)
from Foundation import NSMakeRect

WIDTH  = 180
HEIGHT = 30


class StatusOverlay:
    """
    Small draggable floating window showing recording state.

    Always on top, joins all Spaces, visible in full-screen mode.
    Positioned in the top-right corner by default; user can drag to move.

    States:
        idle       → hidden
        recording  → visible, red background,    "🔴 Recording..."
        processing → visible, dark background,   "⏳ Processing..."
        error      → visible, orange background, "⚠️ <short msg>"
    """

    def __init__(self):
        screen_frame = NSScreen.mainScreen().visibleFrame()
        margin = 12
        x = screen_frame.origin.x + screen_frame.size.width  - WIDTH  - margin
        y = screen_frame.origin.y + screen_frame.size.height - HEIGHT - margin

        self._window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, WIDTH, HEIGHT),
            NSWindowStyleMaskBorderless,
            2,   # NSBackingStoreBuffered
            False,
        )
        self._window.setLevel_(NSFloatingWindowLevel)
        self._window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self._window.setAlphaValue_(0.92)
        self._window.setOpaque_(False)
        self._window.setMovableByWindowBackground_(True)
        self._window.setHidesOnDeactivate_(False)

        # Rounded corners via layer
        cv = self._window.contentView()
        cv.setWantsLayer_(True)
        cv.layer().setCornerRadius_(10.0)
        cv.layer().setMasksToBounds_(True)

        # Label centred in the window
        self._label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(0, 5, WIDTH, HEIGHT - 8)
        )
        self._label.setStringValue_("")
        self._label.setEditable_(False)
        self._label.setBordered_(False)
        self._label.setBackgroundColor_(NSColor.clearColor())
        self._label.setTextColor_(NSColor.whiteColor())
        self._label.setFont_(NSFont.boldSystemFontOfSize_(12.0))
        self._label.setAlignment_(1)  # NSTextAlignmentCenter
        cv.addSubview_(self._label)

    # ------------------------------------------------------------------ #
    #  Public state setters (call from main thread only)
    # ------------------------------------------------------------------ #

    def show_recording(self):
        self._set(
            text="🔴  Recording...",
            bg=NSColor.colorWithRed_green_blue_alpha_(0.75, 0.10, 0.10, 1.0),
        )

    def show_processing(self):
        self._set(
            text="⏳  Processing...",
            bg=NSColor.colorWithRed_green_blue_alpha_(0.15, 0.15, 0.15, 1.0),
        )

    def show_error(self, msg: str):
        self._set(
            text=f"⚠️  {msg[:22]}",
            bg=NSColor.colorWithRed_green_blue_alpha_(0.70, 0.30, 0.00, 1.0),
        )

    def hide(self):
        self._window.orderOut_(None)

    # ------------------------------------------------------------------ #
    #  Internal
    # ------------------------------------------------------------------ #

    def _set(self, text: str, bg: NSColor):
        self._window.setBackgroundColor_(bg)
        self._label.setStringValue_(text)
        self._window.orderFrontRegardless()
