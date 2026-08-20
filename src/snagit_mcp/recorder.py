"""Drives Snagit's video capture engine over COM.

The call sequence and the delays below were established by experiment against
Snagit 2025 (25.4.0.8498); see README.md "How it drives Snagit" before changing them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .com_worker import ComWorker
from .config import Settings

SVI_WINDOW, SVI_REGION = 1, 4
SVO_FILE = 2
SRSM_FIXED = 1
SWSM_HANDLE = 2
SOFNM_FIXED = 1

SCS_IDLE, SCS_SUCCEEDED, SCS_FAILED, SCS_BUSY = 0, 10, 11, 12
CAPTURE_STATES = {
    SCS_IDLE: "idle",
    SCS_SUCCEEDED: "capture_succeeded",
    SCS_FAILED: "capture_failed",
    SCS_BUSY: "busy",
}
RECORDER_ERRORS = {
    0: "none",
    1: "init_recorder_failed",
    2: "init_encoder_failed",
    3: "recorder_threw",
    4: "encoder_threw",
    5: "starting",
    6: "pausing",
    7: "resuming",
    8: "stopping",
    9: "disk_space_low",
    10: "invalid_recording_rect",
    11: "system_audio_not_available",
    12: "system_audio_setup_failed",
    13: "no_webcam_samples",
    14: "no_screen_samples",
    15: "no_audio_samples",
    16: "audio_device_failed",
    32: "audio_device_access_denied",
    99: "unknown",
}

# Snagit needs this long after Capture() before the recorder will accept Start().
ARM_SECONDS = 3.0
# Snagit plays a 3-2-1 countdown after Start(); nothing is recorded until it ends.
COUNTDOWN_SECONDS = 3.6


class SnagitError(RuntimeError):
    pass


@dataclass
class ActiveRecording:
    output_path: Path
    target: dict[str, Any]
    started_at: float
    recording_from: float
    paused_seconds: float = 0.0
    paused_at: float | None = None
    events: list[str] = field(default_factory=list)

    def elapsed(self) -> float:
        now = self.paused_at if self.paused_at is not None else time.monotonic()
        return max(0.0, now - self.recording_from - self.paused_seconds)


class SnagitRecorder:
    def __init__(self, worker: ComWorker, settings: Settings) -> None:
        self._worker = worker
        self._settings = settings
        self._capture: Any = None
        self.active: ActiveRecording | None = None

    # ---- helpers that always run on the COM thread -------------------------

    def _dispatch(self) -> Any:
        import win32com.client

        try:
            return win32com.client.Dispatch("SNAGIT.VideoCapture")
        except Exception as exc:  # noqa: BLE001
            raise SnagitError(
                "Could not create the SNAGIT.VideoCapture COM object. Install Snagit "
                "(2023 or newer) and launch it once so it registers its COM server."
            ) from exc

    def _engine_state(self) -> dict[str, Any]:
        capture = self._dispatch()
        state = int(capture.CaptureState)
        return {
            "capture_state": CAPTURE_STATES.get(state, f"unknown({state})"),
            "capture_state_code": state,
            "capture_done": bool(capture.IsCaptureDone),
            "last_recorder_error": RECORDER_ERRORS.get(
                int(capture.LastRecorderError), str(capture.LastRecorderError)
            ),
        }

    def _configure_and_arm(
        self,
        output_path: Path,
        target: dict[str, Any],
        include_cursor: bool,
    ) -> None:
        capture = self._dispatch()

        if target["mode"] == "window":
            capture.Input = SVI_WINDOW
            options = capture.InputWindowOptions
            options.SelectionMethod = SWSM_HANDLE
            options.Handle = int(target["window_handle"])
        else:
            capture.Input = SVI_REGION
            options = capture.InputRegionOptions
            options.SelectionMethod = SRSM_FIXED
            options.UseStartPosition = True
            options.StartX = int(target["x"])
            options.StartY = int(target["y"])
            options.Width = int(target["width"])
            options.Height = int(target["height"])

        capture.Output = SVO_FILE
        video_file = capture.OutputVideoFile
        video_file.Directory = str(output_path.parent)
        video_file.Filename = output_path.stem
        video_file.FileNamingMethod = SOFNM_FIXED

        # Snagit truncates recordings to a few seconds when its recording UI is hidden.
        capture.HideRecordingUI = False
        capture.EnablePreviewWindow = False
        capture.UseMagnifierWindow = False
        capture.IncludeCursor = bool(include_cursor)

        self._capture = capture
        capture.Capture()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if int(capture.CaptureState) == SCS_BUSY:
                break
            time.sleep(0.1)
        else:
            raise SnagitError(
                "Snagit did not arm the recorder within 15s. Close any capture already in "
                "progress in Snagit and try again."
            )
        time.sleep(ARM_SECONDS)

    def _start(self) -> None:
        try:
            self._capture.Start()
        except Exception as exc:  # noqa: BLE001
            raise SnagitError(f"Snagit refused to start recording: {_com_message(exc)}") from exc
        time.sleep(COUNTDOWN_SECONDS)

    def _simple(self, action: str) -> None:
        try:
            getattr(self._capture, action)()
        except Exception as exc:  # noqa: BLE001
            raise SnagitError(f"Snagit {action} failed: {_com_message(exc)}") from exc

    def _stop_and_wait(self) -> dict[str, Any]:
        capture = self._capture
        try:
            capture.Stop()
        except Exception as exc:  # noqa: BLE001
            raise SnagitError(f"Snagit Stop() failed: {_com_message(exc)}") from exc

        deadline = time.monotonic() + self._settings.encode_timeout_seconds
        while time.monotonic() < deadline:
            if capture.IsCaptureDone and int(capture.CaptureState) != SCS_BUSY:
                break
            time.sleep(0.2)
        else:
            raise SnagitError("Timed out waiting for Snagit to finish encoding the recording.")

        result = {
            "snagit_duration_seconds": round(int(capture.RecordingDuration) / 1000, 3),
            "frame_count": int(capture.FrameCount),
            "average_frame_rate": round(float(capture.AverageFrameRate), 2),
            "succeeded": bool(capture.LastCaptureSucceeded),
            "recorder_error": RECORDER_ERRORS.get(
                int(capture.LastRecorderError), str(capture.LastRecorderError)
            ),
        }
        self._capture = None
        return result

    # ---- async API used by the MCP tools -----------------------------------

    async def engine_state(self) -> dict[str, Any]:
        return await self._worker.call(self._engine_state)

    async def start(
        self,
        output_path: Path,
        target: dict[str, Any],
        include_cursor: bool,
    ) -> ActiveRecording:
        if self.active is not None:
            raise SnagitError(
                f"A recording is already in progress ({self.active.output_path.name}). "
                "Call stop_recording first."
            )
        started_at = time.monotonic()
        await self._worker.call(self._configure_and_arm, output_path, target, include_cursor)
        await self._worker.call(self._start)
        self.active = ActiveRecording(
            output_path=output_path,
            target=target,
            started_at=started_at,
            recording_from=time.monotonic(),
        )
        return self.active

    async def pause(self) -> ActiveRecording:
        active = self._require_active()
        if active.paused_at is not None:
            raise SnagitError("The recording is already paused.")
        await self._worker.call(self._simple, "Pause")
        active.paused_at = time.monotonic()
        active.events.append("paused")
        return active

    async def resume(self) -> ActiveRecording:
        active = self._require_active()
        if active.paused_at is None:
            raise SnagitError("The recording is not paused.")
        await self._worker.call(self._simple, "Resume")
        active.paused_seconds += time.monotonic() - active.paused_at
        active.paused_at = None
        active.events.append("resumed")
        return active

    async def stop(self) -> dict[str, Any]:
        active = self._require_active()
        if active.paused_at is not None:
            await self.resume()
        result = await self._worker.call(self._stop_and_wait)
        self.active = None
        result.update(
            {
                "output_path": str(active.output_path),
                "target": active.target,
                "wall_clock_seconds": round(time.monotonic() - active.started_at, 2),
            }
        )
        return result

    def _require_active(self) -> ActiveRecording:
        if self.active is None:
            raise SnagitError("No recording is in progress. Call start_recording first.")
        return self.active


def _com_message(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    if len(args) >= 3 and isinstance(args[2], tuple) and args[2][2]:
        return str(args[2][2])
    return str(exc)
