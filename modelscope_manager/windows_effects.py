from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


def apply_windows_backdrop(hwnd: int, enabled: bool, dark: bool) -> bool:
    """Use the Windows compositor for acrylic blur instead of a Qt software effect."""
    if sys.platform != "win32" or not hwnd:
        return False

    dwmapi = ctypes.windll.dwmapi
    dark_value = ctypes.c_int(1 if dark else 0)
    dwmapi.DwmSetWindowAttribute(
        wintypes.HWND(hwnd), 20, ctypes.byref(dark_value), ctypes.sizeof(dark_value),
    )

    backdrop = ctypes.c_int(3 if enabled else 1)  # DWMSBT_TRANSIENTWINDOW / DWMSBT_NONE
    if dwmapi.DwmSetWindowAttribute(
        wintypes.HWND(hwnd), 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop),
    ) == 0:
        return enabled

    class AccentPolicy(ctypes.Structure):
        _fields_ = [
            ("state", ctypes.c_int),
            ("flags", ctypes.c_int),
            ("gradient_color", ctypes.c_uint),
            ("animation_id", ctypes.c_int),
        ]

    class WindowCompositionAttributeData(ctypes.Structure):
        _fields_ = [
            ("attribute", ctypes.c_int),
            ("data", ctypes.c_void_p),
            ("size", ctypes.c_size_t),
        ]

    accent = AccentPolicy(
        4 if enabled else 0, 0,
        0xCC202124 if dark else 0xCCF3F3F3,
        0,
    )
    data = WindowCompositionAttributeData(
        19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent),
    )
    setter = getattr(ctypes.windll.user32, "SetWindowCompositionAttribute", None)
    return bool(setter and setter(wintypes.HWND(hwnd), ctypes.byref(data)))
