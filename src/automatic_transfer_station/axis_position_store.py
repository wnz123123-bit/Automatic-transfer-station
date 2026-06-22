from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from .axis_calibration import AXIS9_DEGREES_PER_PULSE, AXIS_CALIBRATIONS, AXIS_LABELS, axis_unit, coordinate_from_pulse
from .json_store import atomic_write_json, cleanup_tmp, now_utc
from .paths import RUNTIME_DIR

_audit = logging.getLogger("ats.audit")
_log = logging.getLogger("ats.position")


SCHEMA_VERSION = 1
DEFAULT_POSITION_STORE_PATH = RUNTIME_DIR / "axis_positions.json"


class AxisPositionStore:
    def __init__(self, path: str | Path = DEFAULT_POSITION_STORE_PATH, axis_calibrations=None):
        self.path = Path(path)
        self.axes: dict[str, dict[str, Any]] = {}
        self.axis_calibrations = axis_calibrations or AXIS_CALIBRATIONS
        # True when the loaded file's schema_version was missing/newer than this
        # build, so it was distrusted and defaults (empty state) were used.
        self.schema_warning: bool = False

    def set_axis_calibrations(self, axis_calibrations) -> None:
        self.axis_calibrations = axis_calibrations or AXIS_CALIBRATIONS

    def load(self) -> dict[str, dict[str, Any]]:
        self.schema_warning = False
        cleanup_tmp(self.path)
        if not self.path.exists():
            self.axes = {}
            return self.axes
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.axes = {}
            return self.axes

        if not isinstance(data, dict):
            self.axes = {}
            return self.axes

        # Honor schema_version: a missing or future version means the layout
        # cannot be trusted, so fall back to empty defaults and report it.
        version = data.get("schema_version")
        if not isinstance(version, int) or version != SCHEMA_VERSION:
            self.schema_warning = True
            self.axes = {}
            return self.axes

        axes = data.get("axes", {})
        if not isinstance(axes, dict):
            self.axes = {}
            return self.axes

        loaded: dict[str, dict[str, Any]] = {}
        for key, record in axes.items():
            if not isinstance(record, dict):
                continue
            try:
                axis = int(key)
                pulse = float(record["pulse"])
            except (KeyError, TypeError, ValueError):
                continue

            # BP6 Fix 4: reject non-finite pulse values (NaN/inf).  A persisted NaN
            # fed into coordinate_from_pulse / is_pulse_in_range makes range checks
            # silently False (nan <= x <= nan is False), corrupting safety limits.
            if not math.isfinite(pulse):
                _audit.warning(
                    "axis_position_store: axis %s record skipped (non-finite pulse=%r) "
                    "in %s — record dropped",
                    key, pulse, self.path,
                )
                continue

            normalized = dict(record)
            normalized["axis"] = axis
            normalized["label"] = str(record.get("label") or AXIS_LABELS.get(axis, f"Axis{axis}"))
            normalized["pulse"] = pulse
            if "angle_degrees" in normalized:
                try:
                    normalized["angle_degrees"] = float(normalized["angle_degrees"])
                except (TypeError, ValueError):
                    normalized.pop("angle_degrees", None)
            if "coordinate" in normalized:
                try:
                    normalized["coordinate"] = float(normalized["coordinate"])
                except (TypeError, ValueError):
                    normalized.pop("coordinate", None)
            loaded[str(axis)] = normalized

        self.axes = loaded
        return self.axes

    def update_axis(
        self,
        axis: int,
        pulse: float,
        source: str = "controller_readback",
        angle_degrees: float | None = None,
    ) -> None:
        self.update_axes({axis: pulse}, source=source, axis9_angle_degrees=angle_degrees if axis == 9 else None)

    def update_axes(
        self,
        positions: dict[int, float],
        source: str = "controller_readback",
        axis9_angle_degrees: float | None = None,
    ) -> None:
        if not positions:
            return

        # BP6 Fix 4: validate all pulse values up-front before mutating any state.
        for axis, pulse in positions.items():
            if not math.isfinite(float(pulse)):
                raise ValueError(
                    f"axis_position_store: non-finite pulse value {pulse!r} for axis {axis} "
                    "is not allowed (would corrupt range checks)"
                )

        now = now_utc()
        changed = False
        for axis, pulse in positions.items():
            record = {
                "axis": int(axis),
                "label": AXIS_LABELS.get(int(axis), f"Axis{axis}"),
                "pulse": float(pulse),
                "source": source,
                "updated_at": now,
            }
            if int(axis) == 9:
                if axis9_angle_degrees is None:
                    # Derive the angle through the (possibly runtime-overridden)
                    # calibration so it matches the stored `coordinate` field; the
                    # raw pulse*deg constant ignores the calibrated zero and would
                    # disagree with coordinate after a runtime R calibration.
                    calibrated = coordinate_from_pulse(9, float(pulse), self.axis_calibrations)
                    record["angle_degrees"] = (
                        float(calibrated) if calibrated is not None else float(pulse) * AXIS9_DEGREES_PER_PULSE
                    )
                else:
                    record["angle_degrees"] = float(axis9_angle_degrees)

            coordinate = coordinate_from_pulse(int(axis), float(pulse), self.axis_calibrations)
            unit = axis_unit(int(axis))
            if int(axis) == 9 and axis9_angle_degrees is not None:
                coordinate = float(axis9_angle_degrees)
            if coordinate is not None and unit is not None:
                record["coordinate"] = float(coordinate)
                record["unit"] = unit

            key = str(axis)
            previous = self.axes.get(key, {})
            comparable = {key_name: value for key_name, value in record.items() if key_name != "updated_at"}
            previous_comparable = {name: previous.get(name) for name in comparable}
            if comparable != previous_comparable:
                changed = True
            self.axes[key] = record

        if changed:
            self._save(now)

    def cached_pulse(self, axis: int) -> float | None:
        record = self.axes.get(str(axis))
        if not record:
            return None
        return float(record["pulse"])

    def cached_axis9_angle(self) -> float | None:
        record = self.axes.get("9")
        if not record:
            return None
        if "angle_degrees" in record:
            return float(record["angle_degrees"])
        calibrated = coordinate_from_pulse(9, float(record["pulse"]), self.axis_calibrations)
        if calibrated is not None:
            return float(calibrated)
        return float(record["pulse"]) * AXIS9_DEGREES_PER_PULSE

    def _save(self, updated_at: str) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": updated_at,
            "axes": {key: self.axes[key] for key in sorted(self.axes, key=int)},
        }
        atomic_write_json(self.path, payload)
