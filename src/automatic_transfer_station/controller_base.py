from __future__ import annotations

from typing import Protocol

from .axis_calibration import (
    AXIS9_DEGREES_PER_PULSE,
    AXIS9_MAX_DEGREES,
    AXIS9_MAX_VELOCITY,
    AXIS9_MIN_DEGREES,
    AXIS9_NUM,
    AXIS_HARD_LIMITS,
    LIMIT_TYPE_NEGATIVE,
    LIMIT_TYPE_POSITIVE,
)


BOARD_IP = "192.168.0.200"
OPEN_MODE = 0
ALL_AXES = range(1, 10)

AXES_WITHOUT_LIMITS = tuple(axis for axis, limits in AXIS_HARD_LIMITS.items() if not limits.has_any_limit)


class MotionControllerError(RuntimeError):
    pass


class MotionController(Protocol):
    is_open: bool

    def open_board(self) -> None:
        ...

    def close_board(self) -> None:
        ...

    def reset_board(self) -> None:
        ...

    def set_axis_calibrations(self, axis_calibrations) -> None:
        ...

    def relative_move(self, axis: int, direction: int, speed: float, step: float) -> None:
        ...

    def move_axis_to_coordinate(self, axis: int, coordinate: float, speed: float | None = None) -> None:
        ...

    def start_jog(self, axis: int, speed: float, partner_lookahead_pulse: float = 0.0) -> None:
        ...

    def stop_axis(self, axis: int) -> None:
        ...

    def stop_all(self) -> None:
        ...

    def get_axis_position(self, axis: int) -> float:
        ...

    def zero_axis3(self) -> None:
        ...

    def mark_axis3(self) -> float:
        ...

    def move_axis3_to_mark(self) -> None:
        ...

    def move_axis3_to_position(self, position: float) -> None:
        ...

    def home_axis9(self) -> None:
        ...

    def move_axis9_to_angle(self, angle: float, speed: float | None = None) -> None:
        ...

    def get_axis9_angle(self) -> float:
        ...


def axis_mask(axis: int) -> int:
    return 1 << (axis - 1)


def speed_axis_for(axis: int) -> int:
    if axis in (5, 6):
        return 5
    if axis in (7, 8):
        return 7
    return axis
