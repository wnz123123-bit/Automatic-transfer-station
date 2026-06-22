from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from .axis_calibration import AXIS_CALIBRATIONS, AXIS_LABELS, AxisCalibration
from .json_store import atomic_write_json, cleanup_tmp, now_utc
from .paths import RUNTIME_DIR

_audit = logging.getLogger("ats.audit")
_log = logging.getLogger("ats.calibration")


SCHEMA_VERSION = 1
DEFAULT_RUNTIME_CALIBRATION_PATH = RUNTIME_DIR / "axis_runtime_calibration.json"


def _coordinate_in_range(base: AxisCalibration, coordinate: float) -> bool:
    """Same soft-range guard that ``calibrate_axis_at_pulse`` enforces.

    The calibration defines the pulse<->coordinate mapping that every
    soft-limit / interference safety check trusts, so a persisted override whose
    zero_coordinate falls outside the axis travel must never be applied.
    """
    if base.min_coordinate is not None and float(coordinate) < base.min_coordinate:
        return False
    if base.max_coordinate is not None and float(coordinate) > base.max_coordinate:
        return False
    return True


def _zero_pulse_in_band(base: AxisCalibration, zero_pulse: float) -> bool:
    """Reject a wildly shifted calibration origin.

    A valid runtime calibration only re-zeros the axis somewhere along its real
    travel, so the recorded ``zero_pulse`` cannot legitimately sit more than the
    full coordinate travel away from the built-in ``zero_pulse``. The accepted
    band is therefore the base ``zero_pulse`` +/- the pulse-equivalent of the
    full [min_coordinate, max_coordinate] span (i.e. travel * pulses_per_unit),
    which still allows generous slack for re-zeroing at either travel end while
    rejecting an absurd origin from a corrupted/hand-edited file.
    """
    if base.pulses_per_unit is None or base.zero_pulse is None:
        return True
    if base.min_coordinate is None or base.max_coordinate is None:
        return True
    travel = abs(base.max_coordinate - base.min_coordinate)
    band = travel * base.pulses_per_unit
    return abs(float(zero_pulse) - base.zero_pulse) <= band + 1.0


class RuntimeAxisCalibrationStore:
    def __init__(self, path: str | Path = DEFAULT_RUNTIME_CALIBRATION_PATH):
        self.path = Path(path)
        self.records: dict[str, dict[str, Any]] = {}
        self.calibrations: dict[int, AxisCalibration] = dict(AXIS_CALIBRATIONS)
        # Axes whose persisted override was rejected on load (out-of-range
        # coordinate or absurd zero_pulse) and fell back to the base calibration.
        self.skipped: list[int] = []
        # True when the file's schema_version was missing/newer than this build,
        # so the whole file was distrusted and defaults were used.
        self.schema_warning: bool = False

    def load(self) -> dict[int, AxisCalibration]:
        self.records = {}
        self.calibrations = dict(AXIS_CALIBRATIONS)
        self.skipped = []
        self.schema_warning = False
        cleanup_tmp(self.path)
        if not self.path.exists():
            return self.calibrations

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.calibrations

        if not isinstance(data, dict):
            return self.calibrations

        # Honor schema_version: a missing or future version means we cannot trust
        # the file's layout, so fall back to the built-in calibrations entirely.
        version = data.get("schema_version")
        if not isinstance(version, int) or version != SCHEMA_VERSION:
            self.schema_warning = True
            _audit.warning(
                "calibration store: schema_version mismatch (got %r, expected %s) in %s — using built-in defaults",
                version, SCHEMA_VERSION, self.path,
            )
            return self.calibrations

        axes = data.get("axes", {})
        if not isinstance(axes, dict):
            return self.calibrations

        for key, record in axes.items():
            if not isinstance(record, dict):
                continue
            try:
                axis = int(key)
                zero_pulse = float(record["zero_pulse"])
                zero_coordinate = float(record["zero_coordinate"])
            except (KeyError, TypeError, ValueError):
                continue

            base = AXIS_CALIBRATIONS.get(axis)
            if base is None or base.unit is None or base.pulses_per_unit is None or base.zero_pulse is None:
                continue

            # Validate the persisted override before trusting it: a corrupted or
            # hand-edited file must not silently shift the safety mapping.
            if not _coordinate_in_range(base, zero_coordinate) or not _zero_pulse_in_band(base, zero_pulse):
                self.skipped.append(axis)
                _audit.warning(
                    "calibration store: axis %s record skipped (out-of-range) "
                    "zero_pulse=%s zero_coordinate=%s in %s — using built-in default",
                    axis, zero_pulse, zero_coordinate, self.path,
                )
                continue

            normalized = dict(record)
            normalized["axis"] = axis
            normalized["label"] = AXIS_LABELS.get(axis, f"Axis{axis}")
            normalized["zero_pulse"] = zero_pulse
            normalized["zero_coordinate"] = zero_coordinate
            normalized["unit"] = base.unit
            self.records[str(axis)] = normalized
            self.calibrations[axis] = replace(base, zero_pulse=zero_pulse, zero_coordinate=zero_coordinate)

        return self.calibrations

    def calibrate_axis_at_pulse(self, axis: int, current_pulse: float, actual_coordinate: float) -> AxisCalibration:
        axis = int(axis)
        base = self.calibrations.get(axis) or AXIS_CALIBRATIONS[axis]
        if base.unit is None or base.pulses_per_unit is None or base.zero_pulse is None:
            raise ValueError(f"axis {axis} does not support absolute position calibration")
        if not _coordinate_in_range(base, float(actual_coordinate)):
            if base.min_coordinate is not None and float(actual_coordinate) < base.min_coordinate:
                raise ValueError(f"axis {axis} coordinate must be >= {base.min_coordinate:g} {base.unit}")
            raise ValueError(f"axis {axis} coordinate must be <= {base.max_coordinate:g} {base.unit}")

        old_zero_pulse = base.zero_pulse
        old_zero_coordinate = base.zero_coordinate
        updated = replace(base, zero_pulse=float(current_pulse), zero_coordinate=float(actual_coordinate))
        self.calibrations[axis] = updated
        now = now_utc()
        _audit.warning(
            "calibration overwrite: axis %s (%s) zero_pulse %s→%s  zero_coordinate %s→%s  at %s",
            axis,
            AXIS_LABELS.get(axis, f"Axis{axis}"),
            old_zero_pulse,
            float(current_pulse),
            old_zero_coordinate,
            float(actual_coordinate),
            now,
        )
        self.records[str(axis)] = {
            "axis": axis,
            "label": AXIS_LABELS.get(axis, f"Axis{axis}"),
            "unit": updated.unit,
            "zero_pulse": float(current_pulse),
            "zero_coordinate": float(actual_coordinate),
            "updated_at": now,
        }
        self._save(now)
        return updated

    def _save(self, updated_at: str) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": updated_at,
            "axes": {key: self.records[key] for key in sorted(self.records, key=int)},
        }
        atomic_write_json(self.path, payload)
