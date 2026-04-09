"""
utils.py — Shared helper functions for the Teams Recorder app.
Handles: audio mixing, WAV writing, Markdown saving, directory management, filename generation.
"""

import os
import datetime
import numpy as np
import scipy.io.wavfile as wav


NOTES_DIR = os.path.expanduser("~/Documents/MeetingNotes")
SAMPLE_RATE = 16000


def ensure_notes_dir() -> str:
    """Create ~/Documents/MeetingNotes/ if it doesn't exist. Returns the path."""
    os.makedirs(NOTES_DIR, exist_ok=True)
    return NOTES_DIR


def get_note_filename() -> str:
    """
    Generate a timestamped Markdown filename.
    Example: 2026-04-09_14-30_meeting.md
    """
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M") + "_meeting.md"


def get_wav_tmp_path() -> str:
    """
    Generate a unique temp WAV path in /tmp/.
    Example: /tmp/meeting_20260409_143022.wav
    """
    now = datetime.datetime.now()
    return f"/tmp/meeting_{now.strftime('%Y%m%d_%H%M%S')}.wav"


def mix_audio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    Mix two float32 mono audio arrays of potentially different lengths.

    Steps:
    1. Pad the shorter array with zeros to match the longer one
    2. Add both arrays together
    3. Clip to [-1.0, 1.0] to prevent distortion

    Args:
        a: First audio array (e.g., microphone)
        b: Second audio array (e.g., system audio)

    Returns:
        Mixed float32 numpy array
    """
    if len(a) == 0 and len(b) == 0:
        return np.zeros(1, dtype=np.float32)
    if len(a) == 0:
        return b.astype(np.float32)
    if len(b) == 0:
        return a.astype(np.float32)

    # Pad shorter array with silence so both have equal length
    max_len = max(len(a), len(b))
    a_padded = np.pad(a.astype(np.float32), (0, max_len - len(a)))
    b_padded = np.pad(b.astype(np.float32), (0, max_len - len(b)))

    # Mix and clip to prevent clipping distortion
    mixed = a_padded + b_padded
    return np.clip(mixed, -1.0, 1.0)


def write_wav(audio: np.ndarray, path: str, sr: int = SAMPLE_RATE) -> str:
    """
    Write a float32 numpy array as a 16-bit PCM WAV file.

    scipy.io.wavfile expects int16 for 16-bit PCM, so we scale before writing.

    Args:
        audio: float32 array with values in [-1.0, 1.0]
        path: Output file path (e.g., /tmp/meeting_xxx.wav)
        sr: Sample rate in Hz (default 16000 — required by Parakeet)

    Returns:
        The path that was written to.
    """
    # Convert float32 [-1, 1] → int16 [-32768, 32767]
    audio_int16 = (audio * 32767).astype(np.int16)
    wav.write(path, sr, audio_int16)
    return path


def save_markdown(content: str, filename: str | None = None) -> str:
    """
    Save meeting notes Markdown content to ~/Documents/MeetingNotes/.

    Args:
        content: Markdown string to write
        filename: Optional custom filename. Auto-generated if not provided.

    Returns:
        Full path to the saved file.
    """
    ensure_notes_dir()
    fname = filename or get_note_filename()
    full_path = os.path.join(NOTES_DIR, fname)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return full_path


def format_duration(seconds: int) -> str:
    """
    Format a duration in seconds as a human-readable string.
    Example: 3723 → '1h 2m 3s'
    """
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)
