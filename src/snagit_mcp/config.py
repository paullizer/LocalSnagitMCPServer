"""Configuration for the Snagit MCP server."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")
_PROGRAM_FILES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "TechSmith",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "TechSmith",
]


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    max_wait_seconds: float
    encode_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        raw_dir = os.environ.get("SNAGIT_MCP_OUTPUT_DIR")
        output_dir = Path(raw_dir).expanduser() if raw_dir else Path.home() / "Videos" / "Snagit MCP"
        return cls(
            output_dir=output_dir.resolve(),
            max_wait_seconds=float(os.environ.get("SNAGIT_MCP_MAX_WAIT_SECONDS", "600")),
            encode_timeout_seconds=float(os.environ.get("SNAGIT_MCP_ENCODE_TIMEOUT_SECONDS", "300")),
        )

    def ensure_output_dir(self) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        return self.output_dir

    def resolve_output_file(self, file_name: str) -> Path:
        """Map a caller-supplied name onto a safe path inside the output directory."""
        stem = _UNSAFE_FILENAME.sub("-", Path(file_name).name).strip(" .-")
        if not stem:
            stem = "recording"
        if stem.lower().endswith(".mp4"):
            stem = stem[:-4]
        target = (self.ensure_output_dir() / f"{stem}.mp4").resolve()
        if target.parent != self.output_dir:
            raise ValueError("Resolved output path escaped the configured output directory")
        return target


def find_snagit_install() -> tuple[Path, str] | None:
    """Return (SnagitCapture.exe path, version) for the newest installed Snagit."""
    candidates: list[tuple[str, Path]] = []
    for root in _PROGRAM_FILES:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            exe = child / "SnagitCapture.exe"
            if exe.is_file():
                candidates.append((child.name, exe))
    if not candidates:
        return None
    name, exe = sorted(candidates)[-1]
    return exe, _file_version(exe) or name


def _file_version(exe: Path) -> str | None:
    try:
        import win32api

        info = win32api.GetFileVersionInfo(str(exe), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:  # noqa: BLE001 - version info is best effort
        return None
