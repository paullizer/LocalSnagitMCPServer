"""MCP server exposing Snagit screen recording to a coding agent."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import windows
from .com_worker import ComWorker
from .config import Settings, find_snagit_install
from .recorder import SnagitError, SnagitRecorder

INSTRUCTIONS = """\
Records the screen with a locally installed TechSmith Snagit so you can produce demo videos.

Typical loop: start_recording -> drive the app (Playwright, terminal, editor) -> stop_recording.

Rules that matter for usable video:
- Snagit records the physical screen, so anything you want on camera must be visible. Run browsers
  headed and in the foreground; never record a headless session.
- start_recording only returns after Snagit's ~3.6s countdown, so it is safe to act immediately
  once it returns.
- Use the wait tool to hold a beat on screen after each visible step; instant automation is hard
  to follow on video.
- Snagit's recording toolbar must stay visible while recording (hiding it truncates the capture).
  It sits outside the recorded region, so it does not appear in the video.
"""

settings = Settings.from_env()
_worker: ComWorker | None = None
_recorder: SnagitRecorder | None = None

server = MCPServer(name="snagit", version="0.1.0", instructions=INSTRUCTIONS)


def get_recorder() -> SnagitRecorder:
    global _worker, _recorder
    if _recorder is None:
        windows.ensure_dpi_awareness()
        _worker = ComWorker()
        _recorder = SnagitRecorder(_worker, settings)
    return _recorder


def _even(value: int) -> int:
    return max(16, int(value) - (int(value) % 2))


def _next_output_path(file_name: str | None) -> Path:
    name = file_name or f"recording-{datetime.now():%Y%m%d-%H%M%S}"
    path = settings.resolve_output_file(name)
    counter = 2
    while path.exists():
        path = settings.resolve_output_file(f"{path.stem}-{counter}")
        counter += 1
    return path


@server.tool()
async def check_snagit_setup() -> dict[str, Any]:
    """Verify Snagit is installed and reachable over COM, and report the output folder."""
    install = find_snagit_install()
    result: dict[str, Any] = {
        "snagit_installed": install is not None,
        "snagit_path": str(install[0]) if install else None,
        "snagit_version": install[1] if install else None,
        "output_directory": str(settings.ensure_output_dir()),
        "dpi_awareness": windows.ensure_dpi_awareness(),
        "displays": len(windows.list_monitors()),
    }
    try:
        result["com"] = await get_recorder().engine_state()
        result["ready"] = True
    except SnagitError as exc:
        result["com_error"] = str(exc)
        result["ready"] = False
    return result


@server.tool()
async def list_displays() -> dict[str, Any]:
    """List monitors with their pixel bounds, for choosing what fullscreen means."""
    monitors = windows.list_monitors()
    return {
        "displays": [{"index": i, **m} for i, m in enumerate(monitors)],
        "virtual_screen": windows.virtual_screen_bounds(),
    }


@server.tool()
async def list_windows(title_contains: str = "", include_minimized: bool = False) -> dict[str, Any]:
    """Find on-screen windows (e.g. the Playwright browser) to target or focus."""
    found = windows.list_windows(title_contains or None, include_minimized)
    return {"count": len(found), "windows": found}


@server.tool()
async def focus_window(window_handle: int) -> dict[str, Any]:
    """Restore and bring a window to the foreground before recording it."""
    return windows.focus_window(window_handle)


@server.tool()
async def start_recording(
    target: Literal["fullscreen", "window", "region"] = "fullscreen",
    window_handle: int | None = None,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    display_index: int = 0,
    file_name: str | None = None,
    include_cursor: bool = True,
    bring_to_front: bool = True,
) -> dict[str, Any]:
    """Start recording the screen to an MP4. Returns after Snagit's countdown, so act immediately.

    Use target="window" with window_handle from list_windows to follow a browser window,
    target="region" with x/y/width/height for a fixed area, or target="fullscreen" for a display.
    """
    recorder = get_recorder()

    if target == "window":
        if window_handle is None:
            raise ValueError("window_handle is required when target='window' (see list_windows)")
        if bring_to_front:
            windows.focus_window(window_handle)
            await asyncio.sleep(0.4)
        info = windows.window_info(window_handle)
        resolved = {
            "mode": "window",
            "window_handle": int(window_handle),
            "window_title": info["title"],
            "bounds": info["bounds"],
        }
    elif target == "region":
        if None in (x, y, width, height):
            raise ValueError("x, y, width and height are required when target='region'")
        resolved = {
            "mode": "region",
            "x": int(x),
            "y": int(y),
            "width": _even(width),
            "height": _even(height),
        }
    else:
        monitors = windows.list_monitors()
        if not 0 <= display_index < len(monitors):
            raise ValueError(f"display_index {display_index} is out of range (found {len(monitors)})")
        bounds = monitors[display_index]["bounds"]
        resolved = {
            "mode": "region",
            "display_index": display_index,
            "x": bounds["x"],
            "y": bounds["y"],
            "width": _even(bounds["width"]),
            "height": _even(bounds["height"]),
        }

    output_path = _next_output_path(file_name)
    active = await recorder.start(output_path, resolved, include_cursor)
    return {
        "recording": True,
        "output_path": str(active.output_path),
        "target": resolved,
        "include_cursor": include_cursor,
        "note": "Countdown finished - Snagit is recording now.",
    }


@server.tool()
async def recording_status() -> dict[str, Any]:
    """Report whether a recording is in progress and how long it has been running."""
    active = get_recorder().active
    if active is None:
        return {"recording": False, "output_directory": str(settings.output_dir)}
    return {
        "recording": True,
        "paused": active.paused_at is not None,
        "elapsed_seconds": round(active.elapsed(), 1),
        "output_path": str(active.output_path),
        "target": active.target,
        "events": active.events,
    }


@server.tool()
async def pause_recording() -> dict[str, Any]:
    """Pause the current recording, e.g. while waiting on a slow deployment."""
    active = await get_recorder().pause()
    return {"paused": True, "elapsed_seconds": round(active.elapsed(), 1)}


@server.tool()
async def resume_recording() -> dict[str, Any]:
    """Resume a paused recording."""
    active = await get_recorder().resume()
    return {"paused": False, "elapsed_seconds": round(active.elapsed(), 1)}


@server.tool()
async def stop_recording(discard: bool = False) -> dict[str, Any]:
    """Stop recording, wait for Snagit to finish encoding, and return the MP4 path."""
    recorder = get_recorder()
    result = await recorder.stop()
    path = Path(result["output_path"])

    deadline = time.monotonic() + 15
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.3)

    if discard:
        path.unlink(missing_ok=True)
        result["discarded"] = True
        result["output_path"] = None
        return result

    result["file_exists"] = path.exists()
    result["file_size_bytes"] = path.stat().st_size if path.exists() else 0
    return result


@server.tool()
async def wait(seconds: float, reason: str = "") -> dict[str, Any]:
    """Hold still for a moment so the last on-screen action is readable in the video."""
    if seconds <= 0:
        raise ValueError("seconds must be greater than 0")
    capped = min(float(seconds), settings.max_wait_seconds)
    await asyncio.sleep(capped)
    return {"waited_seconds": capped, "reason": reason, "capped": capped < float(seconds)}


@server.tool()
async def list_recordings(limit: int = 10) -> dict[str, Any]:
    """List the most recent MP4 files produced in the output folder."""
    directory = settings.ensure_output_dir()
    files = sorted(directory.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
    return {
        "output_directory": str(directory),
        "recordings": [
            {
                "path": str(f),
                "size_bytes": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
            }
            for f in files[: max(1, limit)]
        ],
    }


@server.prompt()
def record_app_demo(what_to_show: str, app_url: str = "") -> str:
    """Plan and record a narrated-by-actions demo video of an app or repo workflow."""
    target = f" of {app_url}" if app_url else ""
    return f"""\
Record a demo video{target} showing: {what_to_show}

Follow this procedure:
1. Call check_snagit_setup and fix anything it reports before continuing.
2. Write out the shot list first: the ordered on-screen steps, and for each one what the viewer
   should see. Keep each step to a single idea.
3. Stage the screen before recording - open the browser headed and maximized, open the terminal or
   editor panes you need, and close anything private or distracting.
4. Pick the capture target: list_windows to record just the browser window, or list_displays plus
   target="fullscreen" when the demo moves between apps.
5. start_recording, then perform the steps. After each visible action call wait(1.5-3) so the
   viewer can read the screen. Type into forms with a per-character delay rather than pasting.
6. Use pause_recording / resume_recording to skip long waits such as a deployment finishing.
7. stop_recording, then report the MP4 path and the shot list you actually captured.

Constraints: Playwright must run headed and in the foreground, since Snagit records real pixels.
Do not use the Snagit recording toolbar yourself - the tools drive it.
"""
