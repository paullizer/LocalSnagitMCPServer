# Local Snagit MCP Server

A local MCP server that lets GitHub Copilot record demo videos with the copy of
**TechSmith Snagit** already installed on your machine. Copilot drives the recording while it
drives everything else — a terminal walkthrough, a deployment, or a Playwright session against
your deployed app — and gets back an MP4.

It talks to Snagit through Snagit's own `SNAGIT.VideoCapture` COM automation interface, so there
are no hotkey simulations, no UI scripting, and no third-party screen recorder.

## Requirements

- Windows
- Snagit 2023 or newer, installed and launched at least once (verified against Snagit 2025, 25.4.0.8498)
- Python 3.10+

## Setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m snagit_mcp --selftest
```

`--selftest` should report `"ready": true`. If it does not, see [Troubleshooting](#troubleshooting).

The repo ships [.vscode/mcp.json](.vscode/mcp.json), so VS Code offers to start the server as soon
as you open the folder. To use it from any workspace, add the same block to your user-level
`mcp.json` with absolute paths instead of `${workspaceFolder}`.

Recordings land in `SNAGIT_MCP_OUTPUT_DIR` (the workspace `recordings/` folder by default, or
`%USERPROFILE%\Videos\Snagit MCP` when unset).

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SNAGIT_MCP_OUTPUT_DIR` | `%USERPROFILE%\Videos\Snagit MCP` | Where MP4s are written |
| `SNAGIT_MCP_MAX_WAIT_SECONDS` | `600` | Upper bound for the `wait` tool |
| `SNAGIT_MCP_ENCODE_TIMEOUT_SECONDS` | `300` | How long `stop_recording` waits for encoding |

## Tools

| Tool | What it does |
| --- | --- |
| `check_snagit_setup` | Snagit version, COM reachability, output folder, display count |
| `list_displays` | Monitors and their pixel bounds |
| `list_windows` | Visible windows with handles and bounds — use it to find the browser |
| `focus_window` | Restore and foreground a window before recording it |
| `start_recording` | Record a display, a window, or a fixed region to MP4 |
| `recording_status` | Whether a recording is running, and for how long |
| `pause_recording` / `resume_recording` | Skip dead air such as a deployment finishing |
| `stop_recording` | Stop, wait for encoding, return the MP4 path and duration |
| `wait` | Hold a beat so the last on-screen action is readable |
| `list_recordings` | Most recent MP4s in the output folder |

There is also a `record_app_demo` prompt (a slash command in VS Code) that gives Copilot the full
shot-list-first procedure.

## Recording a demo of a deployed app

Snagit records real pixels, so the browser has to be visible — **run Playwright headed**, not
headless. Combined with the Playwright MCP server, a session looks like this:

1. Playwright MCP opens your App Service URL in a headed browser and signs in.
2. `list_windows` with `title_contains: "Edge"` (or `Chrome`) returns the browser window handle.
3. `start_recording` with `target: "window"` and that handle. It returns after Snagit's countdown,
   so the next action is already on camera.
4. Playwright clicks through the scenario; `wait` between steps keeps it followable.
5. `stop_recording` returns the MP4 path.

For a repo walkthrough that moves between VS Code, a terminal, the Azure portal, and the browser,
use `target: "fullscreen"` with the `display_index` from `list_displays` instead of a single window.

Ask for it in plain language, for example:

> Record a demo of the deployment flow: show the prerequisites in the README, run the deploy
> script, then open the App Service URL and walk through creating a record. Use display 0.

## How it drives Snagit

These behaviours were established by experiment and the timings in
[src/snagit_mcp/recorder.py](src/snagit_mcp/recorder.py) depend on them:

- The sequence is `Capture()` to arm, a 3s settle, `Start()`, then `Stop()`. Calling `Start()`
  too soon after `Capture()` produces an empty 2 KB MP4.
- Snagit plays a ~3.4s countdown after `Start()`. `start_recording` absorbs it and only returns
  once real recording has begun.
- **Snagit's recording toolbar must stay visible.** Setting `HideRecordingUI = True` silently
  truncates recordings to a couple of seconds. The toolbar sits outside the captured region, so it
  does not show up in the video.
- Microphone and system audio are not settable over COM in this Snagit build — those setters throw,
  and a failed write can wedge the capture engine. Audio therefore follows whatever is configured
  in Snagit's own capture settings. Set it there before recording if you want narration.
- `RecordingDuration`, `FrameCount` and `AverageFrameRate` are only meaningful after `Stop()`;
  during a recording `recording_status` reports the server's own wall-clock instead.
- Output goes straight to a file. Snagit Editor does not pop up between takes.

## Verifying after a Snagit upgrade

```powershell
.\.venv\Scripts\python.exe tests\smoke_recording.py
```

Records a real 5-second clip through the MCP tools and checks the resulting MP4's duration. If a
future Snagit release changes the countdown or arming behaviour, this is what will catch it.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `check_snagit_setup` reports a COM error | Launch Snagit once so it registers its COM server, then retry |
| Recordings are empty or a couple of seconds long | The capture engine is wedged: `Stop-Process -Name SnagitCapture -Force`, then relaunch `SnagitCapture.exe` |
| `Snagit did not arm the recorder` | A capture is already open in Snagit — cancel it and retry |
| Region coordinates look shifted on a scaled display | The server sets per-monitor DPI awareness at startup; confirm `dpi_awareness` in `check_snagit_setup` |

## A note on what ends up on camera

Screen recording captures whatever is on screen, including terminal output, tokens, portal
sessions, and notifications. Close or clear anything sensitive before recording, and review the
MP4 before sharing it.