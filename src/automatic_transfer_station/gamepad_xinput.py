from __future__ import annotations

import ctypes
from dataclasses import dataclass

ERROR_SUCCESS = 0
ERROR_DEVICE_NOT_CONNECTED = 1167

BUTTON = {
    "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LEFT_THUMB": 0x0040, "RIGHT_THUMB": 0x0080,
    "LB": 0x0100, "RB": 0x0200, "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _XINPUT_GAMEPAD)]


@dataclass(frozen=True)
class GamepadSnapshot:
    connected: bool
    packet: int
    lx: float
    ly: float
    rx: float
    ry: float
    lt: float
    rt: float
    buttons: int

    def pressed(self, mask: int) -> bool:
        return bool(self.buttons & mask)


_DISCONNECTED = GamepadSnapshot(False, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)


def _norm_thumb(v: int) -> float:
    return max(-1.0, v / 32768.0) if v < 0 else min(1.0, v / 32767.0)


class GamepadReader:
    """Reads one XInput controller. Polls a single cached index once connected;
    only rescans all slots (slowly, while disconnected) to (re)discover."""

    def __init__(self, xinput):
        self._x = xinput
        self._index: int | None = None
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        fn = getattr(self._x, "XInputGetState", None)
        # Real ctypes function pointers expose `restype`; plain test fakes
        # (bound methods) do not — so this guard safely skips mock objects.
        if fn is not None and hasattr(fn, "restype"):
            fn.restype = ctypes.c_uint
            fn.argtypes = [ctypes.c_uint, ctypes.POINTER(_XINPUT_STATE)]

    def read_state(self, index: int | None = None) -> GamepadSnapshot:
        """Return a snapshot of the controller state.

        If *index* is omitted the reader polls the cached slot (if any),
        otherwise scans slots 0–3 until one responds.  When a slot that was
        cached (or explicitly supplied) reports disconnected, the cache is
        cleared so the next no-arg call rescans all four slots.

        Note: passing an explicit *index* that fails (controller absent) also
        clears the cached slot, so the next no-arg call rescans all slots.
        """
        st = _XINPUT_STATE()
        candidates = [index] if index is not None else ([self._index] if self._index is not None else [0, 1, 2, 3])
        for idx in candidates:
            if idx is None:
                continue
            rc = int(self._x.XInputGetState(idx, ctypes.byref(st)))
            if rc == ERROR_SUCCESS:
                self._index = idx
                g = st.Gamepad
                return GamepadSnapshot(
                    True, int(st.dwPacketNumber),
                    _norm_thumb(g.sThumbLX), _norm_thumb(g.sThumbLY),
                    _norm_thumb(g.sThumbRX), _norm_thumb(g.sThumbRY),
                    g.bLeftTrigger / 255.0, g.bRightTrigger / 255.0,
                    int(g.wButtons),
                )
        self._index = None
        return _DISCONNECTED


def load_default_reader() -> GamepadReader:
    try:
        dll = ctypes.WinDLL("xinput1_4.dll")
    except OSError:
        dll = ctypes.WinDLL("xinput9_1_0.dll")
    return GamepadReader(dll)
