"""End-to-end smoke test: drives the MCP server in-process and records a real 5s clip.

Run it after installing, upgrading Snagit, or changing the capture timings:
    .venv\\Scripts\\python.exe tests\\smoke_recording.py
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from snagit_mcp.server import server  # noqa: E402

HOLD_SECONDS = 5.0


def mp4_duration_seconds(path: Path) -> float | None:
    data = path.read_bytes()
    idx = data.find(b"mvhd")
    if idx < 0:
        return None
    pos = idx + 4
    version = data[pos]
    pos += 4 + (16 if version == 1 else 8)
    timescale = struct.unpack(">I", data[pos : pos + 4])[0]
    duration = (
        struct.unpack(">Q", data[pos + 4 : pos + 12])[0]
        if version == 1
        else struct.unpack(">I", data[pos + 4 : pos + 8])[0]
    )
    return round(duration / timescale, 3) if timescale else None


def payload(result) -> dict:
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


async def main() -> int:
    async with Client(server) as client:
        tools = await client.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))
        prompts = await client.list_prompts()
        print("prompts:", ", ".join(p.name for p in prompts.prompts))

        setup = payload(await client.call_tool("check_snagit_setup", {}))
        print("setup:", json.dumps(setup, indent=2))
        if not setup.get("ready"):
            print("FAIL: Snagit is not reachable over COM")
            return 1

        started = payload(
            await client.call_tool(
                "start_recording",
                {
                    "target": "region",
                    "x": 100,
                    "y": 100,
                    "width": 800,
                    "height": 600,
                    "file_name": "smoke-test",
                },
            )
        )
        print("started:", json.dumps(started, indent=2))

        await client.call_tool("wait", {"seconds": HOLD_SECONDS, "reason": "smoke test hold"})
        status = payload(await client.call_tool("recording_status", {}))
        print("status:", json.dumps(status, indent=2))

        stopped = payload(await client.call_tool("stop_recording", {}))
        print("stopped:", json.dumps(stopped, indent=2))

    path = Path(stopped["output_path"])
    duration = mp4_duration_seconds(path) if path.exists() else None
    print(f"file={path} exists={path.exists()} duration={duration}s")

    failures = []
    if not status.get("recording"):
        failures.append("recording_status did not report an active recording")
    if not path.exists() or path.stat().st_size < 10_000:
        failures.append("recorded file is missing or suspiciously small")
    if duration is None or abs(duration - HOLD_SECONDS) > 1.5:
        failures.append(f"recorded duration {duration}s is not close to {HOLD_SECONDS}s")

    for failure in failures:
        print("FAIL:", failure)
    print("PASS" if not failures else "FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
