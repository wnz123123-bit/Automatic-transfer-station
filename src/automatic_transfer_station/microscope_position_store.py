from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from .axis_calibration import MICROSCOPE_Z_MAX_ABS_PULSE
from .json_store import atomic_write_json, cleanup_tmp, now_utc
from .paths import RUNTIME_DIR

_audit = logging.getLogger("ats.audit")
_log = logging.getLogger("ats.microscope_position")


SCHEMA_VERSION = 1
DEFAULT_MICROSCOPE_POSITION_STORE_PATH = RUNTIME_DIR / "microscope_z_positions.json"


@dataclass(frozen=True)
class MicroscopePosition:
    name: str
    pulse: float


class MicroscopePositionStore:
    def __init__(self, path: str | Path = DEFAULT_MICROSCOPE_POSITION_STORE_PATH):
        self.path = Path(path)
        self._points: list[MicroscopePosition] = []
        # True when the loaded file's schema_version was missing/newer than this
        # build, so it was distrusted and defaults (no points) were used.
        self.schema_warning: bool = False

    def load(self) -> list[MicroscopePosition]:
        self.schema_warning = False
        cleanup_tmp(self.path)
        if not self.path.exists():
            self._points = []
            return self._points
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._points = []
            return self._points

        if not isinstance(data, dict):
            self._points = []
            return self._points

        # Honor schema_version: a missing or future version means the layout
        # cannot be trusted, so fall back to no points and report it.
        version = data.get("schema_version")
        if not isinstance(version, int) or version != SCHEMA_VERSION:
            self.schema_warning = True
            self._points = []
            return self._points

        raw_points = data.get("points", [])
        if not isinstance(raw_points, list):
            self._points = []
            return self._points

        loaded = []
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            try:
                name = str(point["name"]).strip()
                pulse = float(point["pulse"])
            except (KeyError, TypeError, ValueError):
                continue
            # BP6 Fix 4: reject non-finite pulse values (NaN/inf).  A persisted NaN
            # fed into range checks makes is_pulse_in_range silently False, corrupting
            # safety limits.  Mirror axis_runtime_calibration_store's skip+warn pattern.
            if not math.isfinite(pulse):
                _audit.warning(
                    "microscope_position_store: point %r skipped (non-finite pulse=%r) "
                    "in %s — record dropped",
                    name, pulse, self.path,
                )
                continue
            # CP3: a corrupted/hand-edited finite-but-absurd pulse would otherwise drive
            # the unbounded MicroscopeZ axis directly. Skip records past the sanity cap.
            if abs(pulse) > MICROSCOPE_Z_MAX_ABS_PULSE:
                _audit.warning(
                    "microscope_position_store: point %r skipped (pulse=%r exceeds "
                    "sanity cap ±%d) in %s — record dropped",
                    name, pulse, int(MICROSCOPE_Z_MAX_ABS_PULSE), self.path,
                )
                continue
            if name:
                loaded.append(MicroscopePosition(name=name, pulse=pulse))
        self._points = loaded
        return self._points

    def points(self) -> list[MicroscopePosition]:
        return list(self._points)

    def save_point(self, name: str, pulse: float) -> None:
        # BP6 Fix 4: reject non-finite pulse values before persisting.
        pulse_f = float(pulse)
        if not math.isfinite(pulse_f):
            raise ValueError(
                f"microscope_position_store: non-finite pulse value {pulse!r} "
                "is not allowed (would corrupt range checks)"
            )
        # CP3: reject a finite-but-absurd pulse (axis 3 is unbounded; a saved point
        # drives it directly). The cap is deliberately generous (see MICROSCOPE_Z_MAX_ABS_PULSE).
        if abs(pulse_f) > MICROSCOPE_Z_MAX_ABS_PULSE:
            raise ValueError(
                f"microscope_position_store: pulse value {pulse!r} exceeds the sanity "
                f"cap (±{int(MICROSCOPE_Z_MAX_ABS_PULSE)})"
            )
        clean_name = self._clean_name(name)
        next_points = [point for point in self._points if point.name != clean_name]
        next_points.append(MicroscopePosition(clean_name, pulse_f))
        self._points = next_points
        self._save()

    def rename_point(self, old_name: str, new_name: str) -> None:
        old_clean = self._clean_name(old_name)
        new_clean = self._clean_name(new_name)
        target = self.get_point(old_clean)
        if target is None:
            raise ValueError(f"未找到点位: {old_clean}")
        next_points = [point for point in self._points if point.name not in (old_clean, new_clean)]
        next_points.append(MicroscopePosition(new_clean, target.pulse))
        self._points = next_points
        self._save()

    def delete_point(self, name: str) -> None:
        clean_name = self._clean_name(name)
        self._points = [point for point in self._points if point.name != clean_name]
        self._save()

    def get_point(self, name: str) -> MicroscopePosition | None:
        clean_name = self._clean_name(name)
        for point in self._points:
            if point.name == clean_name:
                return point
        return None

    def _save(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": now_utc(),
            "points": [{"name": point.name, "pulse": point.pulse} for point in self._points],
        }
        atomic_write_json(self.path, payload)

    @staticmethod
    def _clean_name(name: str) -> str:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("点位名称不能为空")
        return clean_name
