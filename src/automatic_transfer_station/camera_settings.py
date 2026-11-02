from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


DEFAULT_OBJECTIVE = "10X"
DEFAULT_FILENAME_TEMPLATE = "{date}_{time}_{objective}_{index}"
OBJECTIVE_LABELS = ("5X", "10X", "20X", "40X", "50X")


@dataclass(frozen=True)
class CameraSettings:
    save_dir: str
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    image_format: str = "TIFF"
    jpeg_quality: int = 95
    live_binning: int = 2
    snap_binning: int = 1
    flip_horizontal: bool = True
    flip_vertical: bool = True
    objective: str = DEFAULT_OBJECTIVE
    gamma: float = 1.0
    sharpness: float = 0.0


@dataclass(frozen=True)
class ScaleCalibration:
    objective: str
    um_per_pixel: float
    known_um: float
    pixel_distance: float
    p1: tuple[float, float]
    p2: tuple[float, float]
    calibrated_at: str
    binning: int | None = 1


def _format_parts(now: Callable[[], str] | None = None) -> tuple[str, str]:
    stamp = str(now()) if now is not None else datetime.now().strftime("%Y%m%d_%H%M%S")
    if "_" in stamp:
        date, time = stamp.split("_", 1)
    else:
        date, time = stamp[:8], stamp[8:] or "000000"
    return date, time


def extension_for_format(image_format: str) -> str:
    normalized = image_format.strip().upper()
    if normalized in {"TIF", "TIFF"}:
        return ".tif"
    if normalized in {"JPG", "JPEG"}:
        return ".jpg"
    return ".png"


def build_snapshot_path(
    directory: str | Path,
    template: str,
    objective: str,
    image_format: str,
    *,
    now: Callable[[], str] | None = None,
) -> Path:
    directory = Path(directory)
    date, time = _format_parts(now)
    extension = extension_for_format(image_format)
    safe_objective = str(objective).replace(" ", "")
    for index in range(1, 10000):
        stem = template.format(
            date=date,
            time=time,
            objective=safe_objective,
            index=f"{index:03d}",
        )
        path = directory / f"{stem}{extension}"
        if not path.exists():
            return path
    raise RuntimeError("Could not create a unique camera snapshot filename")


class CameraSettingsStore:
    def __init__(self, path: str | Path, default_save_dir: str | Path):
        self.path = Path(path)
        self.default_save_dir = Path(default_save_dir)

    def load(self) -> CameraSettings:
        if not self.path.exists():
            return CameraSettings(save_dir=str(self.default_save_dir))
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return CameraSettings(save_dir=str(self.default_save_dir))
        defaults = asdict(CameraSettings(save_dir=str(self.default_save_dir)))
        defaults.update({key: value for key, value in data.items() if key in defaults})
        return CameraSettings(**defaults)

    def save(self, settings: CameraSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(settings), indent=2, ensure_ascii=False), encoding="utf-8")


class ScaleCalibrationStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, ScaleCalibration]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        calibrations: dict[str, ScaleCalibration] = {}
        for objective, data in raw.items():
            try:
                calibrations[objective] = ScaleCalibration(
                    objective=str(data["objective"]),
                    um_per_pixel=float(data["um_per_pixel"]),
                    known_um=float(data["known_um"]),
                    pixel_distance=float(data["pixel_distance"]),
                    p1=tuple(data["p1"]),
                    p2=tuple(data["p2"]),
                    calibrated_at=str(data["calibrated_at"]),
                    binning=data.get("binning") if type(data.get("binning")) is int else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return calibrations

    def save(self, calibrations: dict[str, ScaleCalibration]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: asdict(value) for key, value in calibrations.items()}
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def calibrate_from_points(
        self,
        objective: str,
        known_um: float,
        p1: tuple[float, float],
        p2: tuple[float, float],
        *,
        binning: int = 1,
    ) -> ScaleCalibration:
        if binning < 1:
            raise ValueError("Calibration requires a positive binning factor")
        distance = math.dist(p1, p2)
        if distance <= 0:
            raise ValueError("Calibration points must not be identical")
        calibration = ScaleCalibration(
            objective=str(objective),
            um_per_pixel=float(known_um) / distance,
            known_um=float(known_um),
            pixel_distance=distance,
            p1=(float(p1[0]), float(p1[1])),
            p2=(float(p2[0]), float(p2[1])),
            calibrated_at=datetime.now().isoformat(timespec="seconds"),
            binning=binning,
        )
        calibrations = self.load()
        calibrations[str(objective)] = calibration
        self.save(calibrations)
        return calibration
