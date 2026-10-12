"""Windows arrow-key output with deterministic release behavior."""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from ctypes import wintypes

KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
KEYS = {1: 0x25, 2: 0x27, 4: 0x26, 8: 0x28}
ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUTUNION(ctypes.Union):
    # SendInput validates cbSize against the complete Win32 INPUT union, even
    # when this application only submits keyboard events.
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", INPUTUNION)]


def send_key(virtual_key: int, pressed: bool) -> None:
    """Send one Win32 keyboard transition and surface rejected injection."""

    if os.name != "nt":
        raise OSError("Arrow-key injection is supported only on Windows.")
    event = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(virtual_key, 0, 0 if pressed else KEYEVENTF_KEYUP, 0, 0))
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))  # type: ignore[attr-defined]
    if sent != 1:
        win_error = ctypes.__dict__["WinError"]
        raise win_error()


class ArrowKeyEmitter:
    """Pulse requested arrows repeatedly and release every held key on stop."""

    def __init__(
        self,
        sender: Callable[[int, bool], None] = send_key,
        repeat_interval: float = 0.333,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sender = sender
        self._clock = clock
        self._repeat_interval = self._normalize_interval(repeat_interval)
        self._requested = 0
        self._pressed = 0
        self._release_at = 0.0
        self._next_repeat = 0.0

    @property
    def pressed(self) -> int:
        return self._pressed

    def set_repeat_interval(self, seconds: float) -> None:
        """Apply a bounded interval and restart the active repeat schedule."""

        self._repeat_interval = self._normalize_interval(seconds)
        if self._requested:
            self._next_repeat = self._clock() + self._repeat_interval

    def update(self, keys: int) -> None:
        keys &= 0x0F
        now = self._clock()
        if keys != self._requested:
            self._release_pressed()
            self._requested = keys
            if keys:
                self._press(keys)
                self._release_at = now + min(0.060, self._repeat_interval / 2)
                self._next_repeat = now + self._repeat_interval
            return
        if self._pressed and now >= self._release_at:
            self._release_pressed()
        if self._requested and not self._pressed and now >= self._next_repeat:
            self._press(self._requested)
            self._release_at = now + min(0.060, self._repeat_interval / 2)
            self._next_repeat = now + self._repeat_interval

    def _press(self, keys: int) -> None:
        for bit, virtual_key in KEYS.items():
            if keys & bit:
                self._sender(virtual_key, True)
                self._pressed |= bit

    def _release_pressed(self) -> None:
        first_error: Exception | None = None
        for bit, virtual_key in KEYS.items():
            if self._pressed & bit:
                try:
                    self._sender(virtual_key, False)
                except Exception as error:
                    if first_error is None:
                        first_error = error
                finally:
                    self._pressed &= ~bit
        if first_error is not None:
            raise first_error

    def release_all(self) -> None:
        self._requested = 0
        self._release_at = 0.0
        self._next_repeat = 0.0
        self._release_pressed()

    @staticmethod
    def _normalize_interval(seconds: float) -> float:
        return min(1.0, max(0.1, seconds))
