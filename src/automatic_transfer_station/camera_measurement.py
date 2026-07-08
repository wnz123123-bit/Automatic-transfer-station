from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Literal


MEASUREMENT_SCHEMA_VERSION = 1
MeasurementKind = Literal["line", "polygon"]
Point = tuple[float, float]


@dataclass(frozen=True)
class MeasurementObject:
    id: int
    kind: MeasurementKind
    points: tuple[Point, ...]
    objective: str
    frame_width: int
    frame_height: int
    um_per_pixel: float | None
    created_at: str


def normalized_points(points: Iterable[tuple[float, float]]) -> tuple[Point, ...]:
    return tuple((float(x), float(y)) for x, y in points)


def evaluate_measurement(measurement: MeasurementObject) -> dict[str, object]:
    if measurement.kind == "line":
        return _evaluate_line(measurement)
    if measurement.kind == "polygon":
        return _evaluate_polygon(measurement)
    raise ValueError(f"Unsupported measurement kind: {measurement.kind}")


def _evaluate_line(measurement: MeasurementObject) -> dict[str, object]:
    if len(measurement.points) != 2:
        raise ValueError("Line measurement requires exactly two points")
    (x1, y1), (x2, y2) = measurement.points
    dx_px = float(x2 - x1)
    dy_px = float(y2 - y1)
    length_px = math.hypot(dx_px, dy_px)
    result: dict[str, object] = {
        "dx_px": dx_px,
        "dy_px": dy_px,
        "length_px": length_px,
    }
    if _has_calibration(measurement.um_per_pixel):
        scale = float(measurement.um_per_pixel)
        result.update(
            {
                "dx_um": dx_px * scale,
                "dy_um": dy_px * scale,
                "length_um": length_px * scale,
                "display": format_length_um(length_px * scale),
                "unit": "um",
            }
        )
    else:
        result.update({"display": f"{length_px:.1f} px", "unit": "px", "uncalibrated": True})
    return result


def _evaluate_polygon(measurement: MeasurementObject) -> dict[str, object]:
    if len(measurement.points) < 3:
        raise ValueError("Polygon measurement requires at least three points")
    area_px2 = abs(_signed_area(measurement.points))
    perimeter_px = _closed_perimeter(measurement.points)
    result: dict[str, object] = {
        "area_px2": area_px2,
        "perimeter_px": perimeter_px,
    }
    if _has_calibration(measurement.um_per_pixel):
        scale = float(measurement.um_per_pixel)
        area_um2 = area_px2 * scale * scale
        perimeter_um = perimeter_px * scale
        result.update(
            {
                "area_um2": area_um2,
                "perimeter_um": perimeter_um,
                "display": format_area_um2(area_um2),
                "unit": "um^2",
            }
        )
    else:
        result.update({"display": f"{area_px2:.1f} px^2", "unit": "px^2", "uncalibrated": True})
    return result


def format_length_um(value_um: float) -> str:
    value_um = float(value_um)
    if abs(value_um) < 1000.0:
        return f"{value_um:.1f} um"
    return f"{value_um / 1000.0:.3f} mm"


def format_area_um2(value_um2: float) -> str:
    value_um2 = float(value_um2)
    if abs(value_um2) < 1_000_000.0:
        return f"{value_um2:.1f} um^2"
    return f"{value_um2 / 1_000_000.0:.3f} mm^2"


def measurement_sidecar_payload(
    *,
    objective: str,
    frame_width: int,
    frame_height: int,
    flip_horizontal: bool,
    flip_vertical: bool,
    um_per_pixel: float | None,
    crosshair: Point | None,
    measurements: Iterable[MeasurementObject],
) -> dict[str, object]:
    return {
        "schema_version": MEASUREMENT_SCHEMA_VERSION,
        "objective": str(objective),
        "frame": {"width": int(frame_width), "height": int(frame_height)},
        "display": {
            "flip_horizontal": bool(flip_horizontal),
            "flip_vertical": bool(flip_vertical),
        },
        "um_per_pixel": None if not _has_calibration(um_per_pixel) else float(um_per_pixel),
        "crosshair": _point_payload(crosshair) if crosshair is not None else None,
        "measurements": [_measurement_payload(measurement) for measurement in measurements],
    }


def _measurement_payload(measurement: MeasurementObject) -> dict[str, object]:
    return {
        "id": int(measurement.id),
        "kind": measurement.kind,
        "objective": measurement.objective,
        "frame": {"width": int(measurement.frame_width), "height": int(measurement.frame_height)},
        "um_per_pixel": None if not _has_calibration(measurement.um_per_pixel) else float(measurement.um_per_pixel),
        "created_at": measurement.created_at,
        "points": [_point_payload(point) for point in measurement.points],
        "result": evaluate_measurement(measurement),
    }


def _point_payload(point: Point) -> dict[str, float]:
    return {"x": float(point[0]), "y": float(point[1])}


def _signed_area(points: tuple[Point, ...]) -> float:
    total = 0.0
    for index, (x1, y1) in enumerate(points):
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _closed_perimeter(points: tuple[Point, ...]) -> float:
    total = 0.0
    for index, p1 in enumerate(points):
        p2 = points[(index + 1) % len(points)]
        total += math.dist(p1, p2)
    return total


def _has_calibration(um_per_pixel: float | None) -> bool:
    return um_per_pixel is not None and float(um_per_pixel) > 0.0
