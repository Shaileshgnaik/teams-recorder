"""
recorder.py — Captures microphone + Teams system audio simultaneously via:
  - sounddevice: for microphone input
  - ScreenCaptureKit (via pyobjc): for capturing Teams app audio on macOS Tahoe

On macOS 14+ (Sonoma/Sequoia/Tahoe), ScreenCaptureKit allows capturing audio from specific
applications without any virtual audio driver (no BlackHole needed). Requires the app to
have "Screen Recording" permission granted in System Settings → Privacy & Security.

The two streams are recorded as float32 numpy arrays at 16kHz mono, then mixed and saved
as a single WAV file for transcription.
"""

import threading
import time
import numpy as np
import sounddevice as sd
from datetime import datetime

from utils import mix_audio, write_wav, get_wav_tmp_path, SAMPLE_RATE


# --------------------------------------------------------------------------- #
#  ScreenCaptureKit imports via pyobjc
#  These are macOS-only. They will import fine on Tahoe with pyobjc installed.
# --------------------------------------------------------------------------- #
try:
    from ScreenCaptureKit import (
        SCShareableContent,
        SCStream,
        SCStreamConfiguration,
        SCContentFilter,
    )
    from CoreMedia import CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer
    from CoreMedia import CMSampleBufferGetNumSamples
    import AVFoundation  # noqa: F401  needed for AVAudioFormat
    SCK_AVAILABLE = True
except ImportError:
    SCK_AVAILABLE = False
    print("[recorder] WARNING: pyobjc-framework-ScreenCaptureKit not found. "
          "System audio capture disabled. Install with: pip install pyobjc-framework-ScreenCaptureKit")


class AudioRecorder:
    """
    Records microphone and Teams system audio simultaneously.

    Usage:
        recorder = AudioRecorder()
        recorder.start()
        # ... meeting happens ...
        wav_path = recorder.stop()   # returns path to mixed WAV file
    """

    TEAMS_BUNDLE_IDS = (
        "com.microsoft.teams2",   # Teams 2.x (new Teams)
        "com.microsoft.teams",    # Teams classic
        "MSTeams",
    )

    def __init__(self):
        self._mic_chunks: list[np.ndarray] = []
        self._sys_chunks: list[np.ndarray] = []
        self._mic_lock = threading.Lock()
        self._sys_lock = threading.Lock()
        self._recording = False
        self._mic_stream: sd.InputStream | None = None
        self._sc_stream = None          # SCStream object
        self._sc_delegate = None        # SCStreamOutput delegate
        self._start_time: float = 0.0

    # ---------------------------------------------------------------------- #
    #  Public API
    # ---------------------------------------------------------------------- #

    def start(self) -> None:
        """Start recording. Launches mic capture + ScreenCaptureKit system audio."""
        if self._recording:
            return
        self._mic_chunks.clear()
        self._sys_chunks.clear()
        self._recording = True
        self._start_time = time.time()

        # 1. Start microphone capture
        self._start_mic()

        # 2. Start system audio capture (Teams app audio via ScreenCaptureKit)
        if SCK_AVAILABLE:
            # SCK setup involves async callbacks; run in background thread
            t = threading.Thread(target=self._setup_scstream, daemon=True)
            t.start()
        else:
            print("[recorder] System audio not available — recording mic only.")

        print("[recorder] Recording started.")

    def stop(self) -> str:
        """
        Stop recording. Mixes mic + system audio and saves to a temp WAV file.

        Returns:
            Path to the mixed WAV file (e.g., /tmp/meeting_20260409_143022.wav)
        """
        if not self._recording:
            raise RuntimeError("Not currently recording.")

        self._recording = False

        # Stop mic stream
        if self._mic_stream:
            self._mic_stream.stop()
            self._mic_stream.close()
            self._mic_stream = None

        # Stop ScreenCaptureKit stream
        if self._sc_stream:
            try:
                self._sc_stream.stopCaptureWithCompletionHandler_(lambda err: None)
            except Exception as e:
                print(f"[recorder] SCStream stop error (non-fatal): {e}")
            self._sc_stream = None

        duration = int(time.time() - self._start_time)
        print(f"[recorder] Recording stopped. Duration: {duration}s")

        # Flatten chunk lists into single arrays
        with self._mic_lock:
            mic_audio = np.concatenate(self._mic_chunks) if self._mic_chunks else np.zeros(1, dtype=np.float32)
        with self._sys_lock:
            sys_audio = np.concatenate(self._sys_chunks) if self._sys_chunks else np.zeros(1, dtype=np.float32)

        # Mix and write WAV
        mixed = mix_audio(mic_audio, sys_audio)
        wav_path = get_wav_tmp_path()
        write_wav(mixed, wav_path)
        print(f"[recorder] WAV saved to {wav_path}")
        return wav_path

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def duration(self) -> int:
        """Current recording duration in seconds."""
        if not self._recording:
            return 0
        return int(time.time() - self._start_time)

    # ---------------------------------------------------------------------- #
    #  Microphone capture (sounddevice)
    # ---------------------------------------------------------------------- #

    def _start_mic(self) -> None:
        """Open a sounddevice InputStream for the default microphone at 16kHz mono."""
        def callback(indata: np.ndarray, frames: int, time_info, status):
            if status:
                print(f"[recorder/mic] {status}")
            if self._recording:
                with self._mic_lock:
                    self._mic_chunks.append(indata[:, 0].copy())  # mono: take channel 0

        self._mic_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=callback,
        )
        self._mic_stream.start()

    # ---------------------------------------------------------------------- #
    #  System audio capture (ScreenCaptureKit via pyobjc)
    # ---------------------------------------------------------------------- #

    def _setup_scstream(self) -> None:
        """
        Configure and start an SCStream that captures audio from the Teams app.

        How ScreenCaptureKit works:
        1. SCShareableContent.getWithCompletionHandler_() → async call that returns
           a list of running apps (SCRunningApplication) and windows (SCWindow).
        2. We find the Teams app by bundle ID.
        3. SCContentFilter(desktopIndependentWindow:) or filter by application.
        4. SCStreamConfiguration → enable audio, set sample rate.
        5. SCStream(filter:configuration:delegate:) → start capture.
        6. Our delegate receives CMSampleBuffer objects containing PCM audio data.
        """
        done_event = threading.Event()
        error_holder = [None]

        def on_shareable_content(content, error):
            if error:
                print(f"[recorder/sck] SCShareableContent error: {error}")
                error_holder[0] = error
                done_event.set()
                return

            # Find the Teams application
            teams_app = None
            for app in content.applications():
                bundle_id = app.bundleIdentifier() or ""
                if any(bid in bundle_id for bid in self.TEAMS_BUNDLE_IDS):
                    teams_app = app
                    print(f"[recorder/sck] Found Teams app: {bundle_id}")
                    break

            if teams_app is None:
                print("[recorder/sck] Teams not found — capturing all system audio instead.")
                # Fall back: capture all desktop audio
                filter_ = SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                    content.displays()[0],
                    [],   # exclude nobody
                    [],
                )
            else:
                # Capture only Teams audio (no screen pixels needed)
                filter_ = SCContentFilter.alloc().initWithDesktopIndependentWindow_(
                    # We need at least one window — pick the first Teams window
                    next(
                        (w for w in content.windows() if w.owningApplication() and
                         any(bid in (w.owningApplication().bundleIdentifier() or "")
                             for bid in self.TEAMS_BUNDLE_IDS)),
                        None,
                    ) or content.windows()[0]
                )
                # Alternatively, capture all display audio and filter by app at the
                # ScreenCaptureKit level by excluding all OTHER apps:
                other_apps = [a for a in content.applications()
                              if a.bundleIdentifier() != (teams_app.bundleIdentifier() or "")]
                filter_ = SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                    content.displays()[0],
                    other_apps,
                    [],
                )

            # Configure the stream: audio only, 16kHz mono
            config = SCStreamConfiguration.alloc().init()
            config.setCapturesAudio_(True)
            config.setExcludesCurrentProcessAudio_(True)  # don't capture our own audio
            config.setSampleRate_(SAMPLE_RATE)
            config.setChannelCount_(1)

            # Create delegate that receives audio buffers.
            # NSObject subclasses must use the alloc().initWith...() pattern —
            # calling _SCStreamDelegate(...) invokes Python __init__ which pyobjc ignores.
            self._sc_delegate = _SCStreamDelegate.alloc().initWithChunks_lock_recorder_(
                self._sys_chunks, self._sys_lock, self
            )

            # Create and start the stream
            self._sc_stream = SCStream.alloc().initWithFilter_configuration_delegate_(
                filter_, config, self._sc_delegate
            )

            def on_stream_started(error):
                if error:
                    print(f"[recorder/sck] Stream start error: {error}")
                else:
                    print("[recorder/sck] System audio stream started.")
                done_event.set()

            self._sc_stream.startCaptureWithCompletionHandler_(on_stream_started)

        # Async: get the list of capturable content
        SCShareableContent.getWithCompletionHandler_(on_shareable_content)
        done_event.wait(timeout=10)


# --------------------------------------------------------------------------- #
#  SCStream delegate — receives CMSampleBuffer audio callbacks
# --------------------------------------------------------------------------- #

if SCK_AVAILABLE:
    from Foundation import NSObject

    class _SCStreamDelegate(NSObject):
        """
        Objective-C delegate for SCStream audio output.

        ScreenCaptureKit delivers audio as CMSampleBuffer objects.
        We convert each buffer to a float32 numpy array and append it
        to the shared sys_chunks list.
        """

        def initWithChunks_lock_recorder_(self, chunks, lock, recorder):
            self = super().init()
            if self is not None:
                self._chunks = chunks
                self._lock = lock
                self._recorder = recorder
            return self

        def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
            """Called by ScreenCaptureKit for each audio buffer."""
            if not self._recorder.recording:
                return
            try:
                audio_array = _cmsamplebuffer_to_numpy(sample_buffer)
                if audio_array is not None and len(audio_array) > 0:
                    with self._lock:
                        self._chunks.append(audio_array)
            except Exception as e:
                print(f"[recorder/sck] Buffer conversion error: {e}")

else:
    # Stub so the rest of the module doesn't break on import
    class _SCStreamDelegate:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass


def _cmsamplebuffer_to_numpy(sample_buffer) -> np.ndarray | None:
    """
    Convert a CMSampleBuffer (from ScreenCaptureKit) to a float32 numpy array.

    CMSampleBuffer is Apple's container for media data. For audio, the PCM
    samples are stored in an AudioBufferList inside the buffer.

    Steps:
    1. Get the AudioBufferList from the CMSampleBuffer
    2. Access the raw bytes from each AudioBuffer
    3. Interpret bytes as float32 (ScreenCaptureKit delivers float32 PCM)
    4. Return as numpy array
    """
    try:
        import ctypes

        # Get number of samples in this buffer
        num_samples = CMSampleBufferGetNumSamples(sample_buffer)
        if num_samples == 0:
            return None

        # Extract AudioBufferList
        block_buffer_ref = None
        audio_buffer_list, block_buffer_ref = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sample_buffer,
            None,   # audio_buffer_list_size
            None,   # block_buffer_allocator
            None,   # block_buffer_memory_allocator
            0,      # flags
            None,   # block_buffer_out (returned via ref)
        )

        if audio_buffer_list is None:
            return None

        # The AudioBufferList contains one or more AudioBuffer structs
        # Each AudioBuffer has: mNumberChannels, mDataByteSize, mData (void*)
        # For mono float32 at 16kHz: mData points to float32 samples
        buffers = audio_buffer_list.mBuffers
        all_samples = []
        for buf in buffers:
            n_bytes = buf.mDataByteSize
            if n_bytes > 0 and buf.mData:
                raw = (ctypes.c_float * (n_bytes // 4)).from_address(buf.mData)
                samples = np.frombuffer(raw, dtype=np.float32).copy()
                all_samples.append(samples)

        if not all_samples:
            return None

        # Average channels if stereo (should be mono given our config, but just in case)
        combined = np.mean(np.stack(all_samples), axis=0)
        return combined.astype(np.float32)

    except Exception as e:
        print(f"[recorder/sck] _cmsamplebuffer_to_numpy error: {e}")
        return None
