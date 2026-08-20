"""Win32 helpers for picking what Snagit should record."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SW_RESTORE = 9
MONITORINFOF_PRIMARY = 1
DWMWA_EXTENDED_FRAME_BOUNDS = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEX)]
dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]


def ensure_dpi_awareness() -> str:
    """Report physical pixels from GetWindowRect so regions line up with Snagit."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return "per-monitor-v2"
    except AttributeError:
        pass
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:  # noqa: BLE001
        pass
    return "system" if user32.SetProcessDPIAware() else "unaware"


def _rect_to_dict(rect: wintypes.RECT) -> dict[str, int]:
    return {
        "x": rect.left,
        "y": rect.top,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _process_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def get_window_bounds(hwnd: int) -> dict[str, int]:
    """Visible frame bounds, excluding the invisible drop-shadow border when available."""
    rect = wintypes.RECT()
    if (
        dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        != 0
    ):
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return _rect_to_dict(rect)


def window_info(hwnd: int) -> dict[str, Any]:
    return {
        "handle": int(hwnd),
        "title": _window_text(hwnd),
        "process": _process_name(hwnd),
        "class_name": _class_name(hwnd),
        "minimized": bool(user32.IsIconic(hwnd)),
        "bounds": get_window_bounds(hwnd),
    }


def list_windows(title_contains: str | None = None, include_minimized: bool = False) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    needle = (title_contains or "").lower()

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        info = window_info(hwnd)
        if not info["title"]:
            return True
        if info["minimized"] and not include_minimized:
            return True
        if info["bounds"]["width"] < 80 or info["bounds"]["height"] < 80:
            return True
        if needle and needle not in info["title"].lower() and needle not in info["process"].lower():
            return True
        results.append(info)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return results


def focus_window(hwnd: int) -> dict[str, Any]:
    if not user32.IsWindow(hwnd):
        raise ValueError(f"Window handle {hwnd} does not exist")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    info = window_info(hwnd)
    info["foreground"] = int(user32.GetForegroundWindow()) == int(hwnd)
    return info


def list_monitors() -> list[dict[str, Any]]:
    monitors: list[dict[str, Any]] = []

    def callback(hmonitor: int, _hdc: int, _rect: Any, _lparam: int) -> bool:
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            monitors.append(
                {
                    "device": info.szDevice,
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    "bounds": _rect_to_dict(info.rcMonitor),
                    "work_area": _rect_to_dict(info.rcWork),
                }
            )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    return monitors


def primary_monitor_bounds() -> dict[str, int]:
    for monitor in list_monitors():
        if monitor["primary"]:
            return monitor["bounds"]
    return virtual_screen_bounds()


def virtual_screen_bounds() -> dict[str, int]:
    return {
        "x": user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        "y": user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        "width": user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        "height": user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    }
