"""Bounded, pure X3/Y3 polygon raster planning and editable recipe JSON.

Pixels belong to one raw, unflipped full-frame snapshot. All stage coordinates,
pitches and speeds use physical um (axes 5/6 only). The scan basis is rotated
from +X3 stage motion, anchored at reference_stage_um: rows are v=j*row_pitch,
and dots are u=i*point_pitch, including across disconnected spans. Every row
travels in +u; returns/gaps/preloads are DARK. No automatic return follows the
last exposure. No hardware, Qt, file I/O, PRF, qualification or ARM exists here.

Containment concerns the BEAM CENTER / CENTERLINE ONLY. There is no spot
footprint erosion or optical margin. Polygon boundaries are included; ROI and
frame right/bottom boundaries are excluded. Imported recipes are editing data:
a fresh photo must be aligned and confirmed before a new plan can be made.

The plan contains every geometric travel/preload endpoint. A live executor that
adds lead-in/out must build and preflight its complete resulting route separately
and recheck counts/time. Estimated duration is nominal travel/speed + settle +
dwell, not an upper bound including acceleration/communication. Execution needs
its own deadline, interlocks, calibration/snapshot ownership and collision checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
import json
import math
from typing import Literal

from .cutting_geometry import (
    SAMPLE_AXES, BacklashSettings, CalibrationContext, CuttingCalibration, Point, ROI,
    _add, _inverse, _matvec, _number, _point, _record, _subtract, _sum,
)


MAX_POLYGON_VERTICES = 128
MAX_EXPOSURES = 10_000
MAX_ACTIONS = 30_000
MAX_CANDIDATE_ROWS = 10_000
MAX_ESTIMATED_DURATION_S = 3600.0
MAX_RECIPE_JSON_BYTES = 1024 * 1024
_MAX_CANDIDATE_POINTS = 30_000
_SCHEMA_VERSION = 1

__all__ = [
    "RasterSettings", "RasterMove", "DotExposure", "RasterLine", "RasterPlan",
    "RasterRecipe", "plan_raster", "MAX_POLYGON_VERTICES", "MAX_EXPOSURES",
    "MAX_ACTIONS", "MAX_CANDIDATE_ROWS", "MAX_ESTIMATED_DURATION_S",
    "MAX_RECIPE_JSON_BYTES",
]


def _integer(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def _orientation(a: Point, b: Point, c: Point) -> int:
    """Sign of orientation; exact float-rational fallback near cancellation.

    The fast-path bound is numerical roundoff, not a pixel/physical tolerance.
    No approximately collinear edge is silently declared safe.
    """
    left = (b[0] - a[0]) * (c[1] - a[1])
    right = (b[1] - a[1]) * (c[0] - a[0])
    determinant = left - right
    if math.isfinite(determinant) and abs(determinant) > 16 * math.ulp(1.0) * (abs(left) + abs(right)):
        return 1 if determinant > 0 else -1
    ax, ay, bx, by, cx, cy = map(Fraction, (*a, *b, *c))
    exact = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return (exact > 0) - (exact < 0)


def _on_segment(a: Point, b: Point, p: Point) -> bool:
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
            and _orientation(a, b, p) == 0)


def _edges(polygon: tuple[Point, ...]):
    return zip(polygon, polygon[1:] + polygon[:1])


def _touches(a: Point, b: Point, c: Point, d: Point) -> bool:
    if (max(a[0], b[0]) < min(c[0], d[0]) or max(c[0], d[0]) < min(a[0], b[0])
            or max(a[1], b[1]) < min(c[1], d[1]) or max(c[1], d[1]) < min(a[1], b[1])):
        return False
    ab_c, ab_d = _orientation(a, b, c), _orientation(a, b, d)
    cd_a, cd_b = _orientation(c, d, a), _orientation(c, d, b)
    return (ab_c * ab_d < 0 and cd_a * cd_b < 0
            or ab_c == 0 and _on_segment(a, b, c)
            or ab_d == 0 and _on_segment(a, b, d)
            or cd_a == 0 and _on_segment(c, d, a)
            or cd_b == 0 and _on_segment(c, d, b))


def _polygon(value: object, context: CalibrationContext) -> tuple[Point, ...]:
    if not isinstance(value, (tuple, list)) or not 3 <= len(value) <= MAX_POLYGON_VERTICES + 1:
        raise ValueError("polygon requires 3..128 vertices (one closing repeat is permitted)")
    polygon = tuple(_point(p, "polygon vertex") for p in value)
    if polygon[-1] == polygon[0]:
        polygon = polygon[:-1]
    if not 3 <= len(polygon) <= MAX_POLYGON_VERTICES or len(set(polygon)) != len(polygon):
        raise ValueError("polygon requires 3..128 distinct vertices")
    # Geometric contour vertices may lie on the frame edge. Exposure centers
    # still use strict half-open frame/ROI tests, even on those contours.
    if any(not (0 <= x <= context.width and 0 <= y <= context.height) for x, y in polygon):
        raise ValueError("polygon vertices must lie within the full-frame boundary")
    for i, (a, b) in enumerate(_edges(polygon)):
        c = polygon[(i + 2) % len(polygon)]
        if _on_segment(a, b, c) or _on_segment(b, c, a):
            raise ValueError("polygon has overlapping/backtracking adjacent edges")
        for j in range(i + 2, len(polygon)):
            if i == 0 and j == len(polygon) - 1:
                continue
            if _touches(a, b, polygon[j], polygon[(j + 1) % len(polygon)]):
                raise ValueError("polygon must be simple: no crossings, self-touch or holes")
    area2 = sum(Fraction(a[0]) * Fraction(b[1]) - Fraction(a[1]) * Fraction(b[0])
                for a, b in _edges(polygon))
    if area2 == 0:
        raise ValueError("polygon has zero area")
    return polygon


def _inside(p: Point, polygon: tuple[Point, ...]) -> bool:
    inside = False
    for a, b in _edges(polygon):
        if _on_segment(a, b, p):
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            sign = _orientation(a, b, p)
            if (sign > 0) == (b[1] > a[1]):
                inside = not inside
    return inside


def _segment_inside(a: Point, b: Point, polygon: tuple[Point, ...]) -> bool:
    """Check entire centerline, including a concave notch touching a vertex."""
    if not _inside(a, polygon) or not _inside(b, polygon):
        return False
    axis = 0 if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else 1
    denominator = b[axis] - a[axis]
    if denominator == 0:
        return False
    breaks = {0.0, 1.0}
    for c, d in _edges(polygon):
        if (_orientation(a, b, c) * _orientation(a, b, d) < 0
                and _orientation(c, d, a) * _orientation(c, d, b) < 0):
            return False
        for p in (c, d):
            if _on_segment(a, b, p):
                breaks.add((p[axis] - a[axis]) / denominator)
    ordered = sorted(breaks)
    for low, high in zip(ordered, ordered[1:]):
        t = (low + high) / 2
        if not _inside((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), polygon):
            return False
    return True


@dataclass(frozen=True, slots=True)
class RasterSettings:
    """Physical row spacing/direction and a discriminated exposure mode.

    angle_deg is stage-motion direction from +X3, canonicalized to [0,360).
    lines requires point_pitch_um=None and both times zero. dots requires a
    positive point pitch and integer dwell_ms 1..60000; settle_ms is 0..60000.
    Dwell means a FIRE exposure envelope, never PRF or one optical pulse.
    speed_um_s applies to every move; physical hardware limits are external.
    """

    mode: Literal["lines", "dots"] = "lines"
    row_pitch_um: float = 10.0
    point_pitch_um: float | None = None
    angle_deg: float = 0.0
    speed_um_s: float = 100.0
    settle_ms: int = 0
    dwell_ms: int = 0

    def __post_init__(self) -> None:
        if self.mode not in ("lines", "dots"):
            raise ValueError("mode must be 'lines' or 'dots'")
        for name in ("row_pitch_um", "speed_um_s"):
            value = _number(getattr(self, name), name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "angle_deg", _number(self.angle_deg, "angle_deg") % 360.0)
        _integer(self.settle_ms, "settle_ms", 0, 60000)
        _integer(self.dwell_ms, "dwell_ms", 0, 60000)
        if self.mode == "lines":
            if self.point_pitch_um is not None or self.settle_ms or self.dwell_ms:
                raise ValueError("lines cannot contain point pitch, settle or dwell parameters")
        else:
            pitch = _number(self.point_pitch_um, "point_pitch_um")
            if pitch <= 0 or self.dwell_ms == 0:
                raise ValueError("dots requires positive point pitch and dwell_ms")
            object.__setattr__(self, "point_pitch_um", pitch)

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in (
            "mode", "row_pitch_um", "point_pitch_um", "angle_deg", "speed_um_s", "settle_ms", "dwell_ms"
        )}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RasterSettings:
        data = _record(data, {"mode", "row_pitch_um", "point_pitch_um", "angle_deg",
                              "speed_um_s", "settle_ms", "dwell_ms"}, "raster settings")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class RasterMove:
    """Nonzero straight X3/Y3 move in um; cut is intent, never an enable command."""

    start_um: Point
    end_um: Point
    cut: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_um", _point(self.start_um, "start_um"))
        object.__setattr__(self, "end_um", _point(self.end_um, "end_um"))
        if type(self.cut) is not bool:
            raise ValueError("cut must be bool")
        if not 0 < _number(math.dist(self.start_um, self.end_um), "move length"):
            raise ValueError("zero/unresolvable moves are not permitted")


@dataclass(frozen=True, slots=True)
class DotExposure:
    """Stop/settle/expose at this center. Every move to/between dots is DARK."""

    point_um: Point
    settle_ms: int
    dwell_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "point_um", _point(self.point_um, "point_um"))
        _integer(self.settle_ms, "settle_ms", 0, 60000)
        _integer(self.dwell_ms, "dwell_ms", 1, 60000)


@dataclass(frozen=True, slots=True)
class RasterLine:
    """One continuous span; action_index identifies its cut RasterMove.

    row_index counts candidate rows from the first clipped candidate row (empty
    rows can leave gaps). span_index is the sorted clipped span within that row.
    The runner can construct a live lead route without flattening dwell actions.
    """

    row_index: int
    span_index: int
    start_um: Point
    end_um: Point
    action_index: int

    def __post_init__(self) -> None:
        _integer(self.row_index, "row_index", 0, MAX_CANDIDATE_ROWS - 1)
        _integer(self.span_index, "span_index", 0, MAX_POLYGON_VERTICES - 1)
        _integer(self.action_index, "action_index", 0, MAX_ACTIONS - 1)
        move = RasterMove(self.start_um, self.end_um, True)
        object.__setattr__(self, "start_um", move.start_um)
        object.__setattr__(self, "end_um", move.end_um)


def _validate_inputs(polygon, calibration, reference, roi, backlash, settings):
    if not isinstance(calibration, CuttingCalibration):
        raise ValueError("an explicit fitted CuttingCalibration is required")
    if not isinstance(roi, ROI) or not isinstance(backlash, BacklashSettings) or not isinstance(settings, RasterSettings):
        raise ValueError("ROI, BacklashSettings and RasterSettings are required")
    roi.validate_for(calibration.context)
    return _polygon(polygon, calibration.context), _point(reference, "reference_stage_um")


def _basis(angle: float) -> tuple[Point, Point]:
    cardinal = {0.0: (1.0, 0.0), 90.0: (0.0, 1.0), 180.0: (-1.0, 0.0), 270.0: (0.0, -1.0)}
    u = cardinal.get(angle)
    if u is None:
        radians = math.radians(angle)
        u = (math.cos(radians), math.sin(radians))
    return u, (-u[1], u[0])


def _scan_spans(polygon: tuple[Point, ...], v: float) -> tuple[tuple[float, float], ...]:
    crossings = []
    for a, b in _edges(polygon):
        # Half-open vertex ownership; ignore horizontal edges and zero tangencies.
        if a[1] <= v < b[1] or b[1] <= v < a[1]:
            fraction = (v - a[1]) / (b[1] - a[1])
            crossings.append(_number(a[0] + fraction * (b[0] - a[0]), "scan intersection"))
    crossings.sort()
    if len(crossings) % 2:
        raise ValueError("numerically ambiguous polygon/scan intersection")
    return tuple((a, b) for a, b in zip(crossings[::2], crossings[1::2]) if a < b)


def _grid(lower: float, upper: float, pitch: float, limit: int) -> tuple[int, ...]:
    """Fixed phase, bounded before iteration. One guard index handles division rounding."""
    if not lower < upper:
        return ()
    lo = _number(lower / pitch, "grid index")
    hi = _number(upper / pitch, "grid index")
    if max(abs(lo), abs(hi)) > 2**52:
        raise ValueError("grid index exceeds floating-point spatial resolution")
    first, stop = math.ceil(lo) - 1, math.ceil(hi) + 1
    if stop - first > limit + 2:
        raise ValueError("candidate grid count exceeds resource limit")
    result = tuple(i for i in range(first, stop) if lower <= i * pitch < upper)
    if len(result) > limit:
        raise ValueError("candidate grid count exceeds resource limit")
    return result


def _build(polygon, reference, calibration, roi, backlash, settings):
    u, n = _basis(settings.angle_deg)
    inverse = _inverse(calibration.J)

    def uv(pixel):
        offset = _matvec(inverse, _subtract(calibration.laser_pixel, pixel))
        return (_sum(a * b for a, b in zip(offset, u)), _sum(a * b for a, b in zip(offset, n)))

    def pose(x, y, *, require_resolvable=True, resolution_floor=0.0):
        offset = (_sum((u[0] * x, n[0] * y)), _sum((u[1] * x, n[1] * y)))
        target = _add(reference, offset)
        roundoff = max(resolution_floor, 32 * math.ulp(_number(math.hypot(*offset), "stage offset")))
        if require_resolvable and any(abs(offset[i]) > roundoff and target[i] == reference[i] for i in (0, 1)):
            raise ValueError("stage offset is not numerically resolvable")
        return target

    def pixel(point):
        return calibration.pixel_for_stage(point, reference)

    def contained(point):
        p = pixel(point)
        return roi.contains(p) and _inside(p, polygon)

    poly_uv = tuple(uv(p) for p in polygon)
    # Exclude upper pixel edges BEFORE affine transformation. All final exposed
    # centers are nevertheless checked again after round-trip projection.
    right = math.nextafter(roi.x + roi.width, roi.x)
    bottom = math.nextafter(roi.y + roi.height, roi.y)
    if settings.mode == "lines":
        # Affine projection of intermediate floating-point stage coordinates
        # can round one ULP past a valid endpoint. Keep exclusive upper edges
        # inward by an arithmetic error bound, not a physical spot/tolerance.
        corner_uv = tuple(uv(p) for p in ((roi.x, roi.y), (right, roi.y), (right, bottom), (roi.x, bottom)))
        radius = max(math.hypot(*p) for p in poly_uv + corner_uv)
        stage_ulps = tuple(math.ulp(_number(abs(s) + radius, "stage precision scale")) for s in reference)
        errors = tuple(16 * _sum((math.ulp(calibration.laser_pixel[i]), math.ulp(edge),
                                  *(abs(j) * step for j, step in zip(calibration.J[i], stage_ulps))))
                       for i, edge in enumerate((right, bottom)))
        right, bottom = right - errors[0], bottom - errors[1]
    if right <= roi.x or bottom <= roi.y:
        raise ValueError("ROI has no numerically resolvable interior")
    roi_uv = tuple(uv(p) for p in ((roi.x, roi.y), (right, roi.y), (right, bottom), (roi.x, bottom)))
    # Intersecting transformed edges can cancel to ~0 at the laser origin.
    # Bound that arithmetic error by the operands, not the near-zero result.
    # This is representation error, not a user calibration/containment tolerance.
    intersection_roundoff = 64 * math.ulp(max(abs(c) for p in poly_uv + roi_uv for c in p))
    low = max(min(p[1] for p in poly_uv), min(p[1] for p in roi_uv))
    high = min(max(p[1] for p in poly_uv), max(p[1] for p in roi_uv))
    rows = _grid(low, high, settings.row_pitch_um, MAX_CANDIDATE_ROWS)
    actions, lines, durations = [], [], []
    current = reference
    exposure_count = candidate_points = 0
    running_duration = 0.0
    seen_dots, seen_lines = set(), set()

    def append(action):
        nonlocal current, exposure_count, running_duration
        if len(actions) >= MAX_ACTIONS:
            raise ValueError("raster exceeds 30000 actions")
        if isinstance(action, RasterMove):
            if action.start_um != current:
                raise ValueError("discontinuous raster path")
            duration = _number(math.dist(action.start_um, action.end_um) / settings.speed_um_s, "move duration")
            current = action.end_um
            exposure_count += int(action.cut)
        else:
            if action.point_um != current:
                raise ValueError("dot exposure is not at the current path endpoint")
            duration = (action.settle_ms + action.dwell_ms) / 1000.0
            exposure_count += 1
        if exposure_count > MAX_EXPOSURES:
            raise ValueError("raster exceeds 10000 exposures")
        durations.append(duration)
        # fsum once at the end, but a nonnegative incremental sum bounds work.
        running_duration += duration
        if running_duration > MAX_ESTIMATED_DURATION_S and _sum(durations) > MAX_ESTIMATED_DURATION_S:
            raise ValueError("raster nominal duration exceeds 3600 seconds")
        actions.append(action)

    def dark_to(target):
        if current != target:
            append(RasterMove(current, target, False))

    offsets = tuple(math.copysign(amount, direction) if abs(direction) > 32 * math.ulp(1.0) else 0.0
                    for direction, amount in zip(u, backlash.effective_um))

    def approach(target):
        preload = _subtract(target, offsets)
        if any(offsets[i] != 0 and preload[i] == target[i] for i in (0, 1)):
            raise ValueError("backlash preload is not numerically resolvable")
        dark_to(preload)
        dark_to(target)

    def endpoint(x, other, v):
        point = pose(x, v, resolution_floor=intersection_roundoff)
        if contained(point):
            return point
        # Inset only in scan direction by bounded representational steps. Never
        # clamp a dot off its lattice or expand ROI with a pixel epsilon.
        # Try each axis's representable progress separately, smallest first.
        # Taking their maximum unnecessarily demands that a near-stationary
        # minor axis change, potentially discarding most of a narrow span.
        # Keep the established intersection roundoff margin: merely rounding
        # an adjusted endpoint onto an ROI edge can put an interpolated center
        # one ULP outside it after inverse projection.
        scales = (max(math.ulp(x), math.ulp(other), math.ulp(v), intersection_roundoff),
                  *(max(intersection_roundoff, math.ulp(reference[i]) / abs(u[i]))
                    for i in (0, 1) if u[i]))
        steps = sorted({scale * (2**power) for scale in scales for power in range(8)
                        if math.isfinite(scale * (2**power)) and 0 < scale * (2**power) < abs(other - x)})
        for step in steps:  # At most 24 probes; never a fractional-span fallback.
            adjusted = x + math.copysign(step, other - x)
            if not min(x, other) < adjusted < max(x, other):
                continue
            point = pose(adjusted, v, resolution_floor=intersection_roundoff)
            if contained(point):
                return point
        raise ValueError("cannot resolve an ROI/polygon-contained line endpoint")

    for row_index, j in enumerate(rows):
        v = j * settings.row_pitch_um
        spans = sorted((max(a, c), min(b, d))
                       for a, b in _scan_spans(poly_uv, v)
                       for c, d in _scan_spans(roi_uv, v)
                       if max(a, c) < min(b, d))
        first_dot = True
        for span_index, (a, b) in enumerate(spans):
            # A whole row exactly on an excluded ROI edge has no valid centers.
            # Keep the fixed phase; never move that row inward to create exposure.
            # This midpoint is only a containment probe, never a hardware pose.
            # A symmetric span may cancel to an offset below the reference ULP.
            middle = pose(a / 2 + b / 2, v, require_resolvable=False)
            if not contained(middle):
                continue
            if settings.mode == "lines":
                start, end = endpoint(a, b, v), endpoint(b, a, v)
                if start == end or not _segment_inside(pixel(start), pixel(end), polygon):
                    raise ValueError("line centerline is collapsed or leaves polygon")
                delta = _subtract(end, start)
                if _sum(x * y for x, y in zip(delta, u)) <= 0:
                    raise ValueError("line lost the required forward direction")
                if (start, end) in seen_lines:
                    raise ValueError("scan rows collapsed to a duplicate line")
                seen_lines.add((start, end))
                approach(start)
                index = len(actions)
                append(RasterMove(start, end, True))
                lines.append(RasterLine(row_index, span_index, start, end, index))
            else:
                indices = _grid(a, b, settings.point_pitch_um, MAX_EXPOSURES)
                candidate_points += len(indices)
                if candidate_points > _MAX_CANDIDATE_POINTS:
                    raise ValueError("candidate point count exceeds resource limit")
                for i in indices:
                    target = pose(i * settings.point_pitch_um, v)
                    if not contained(target):
                        continue
                    if target in seen_dots:
                        raise ValueError("dot lattice collapsed to a duplicate center")
                    seen_dots.add(target)
                    if first_dot:
                        approach(target)
                        first_dot = False
                    else:
                        dark_to(target)
                    append(DotExposure(target, settings.settle_ms, settings.dwell_ms))
    if exposure_count == 0:
        raise ValueError("polygon/ROI/grid intersection contains no exposures")
    duration = _sum(durations)
    if duration > MAX_ESTIMATED_DURATION_S:
        raise ValueError("raster nominal duration exceeds 3600 seconds")
    return tuple(actions), tuple(lines), duration, len(rows)


@dataclass(frozen=True, slots=True)
class RasterPlan:
    """Complete immutable geometric plan; actions are the ordered execution source.

    Never execute only path_endpoints_um: it intentionally omits dwell events.
    verify_integrity rebuilds all actions/metadata from saved inputs. Do that
    during preflight, not in a live keepalive loop. Additional live leads are not
    part of this geometric plan and require their own complete route preflight.
    """

    calibration: CuttingCalibration
    reference_stage_um: Point
    roi: ROI
    backlash: BacklashSettings
    settings: RasterSettings
    polygon_pixels: tuple[Point, ...]
    actions: tuple[RasterMove | DotExposure, ...]
    lines: tuple[RasterLine, ...]
    estimated_duration_s: float
    candidate_row_count: int

    def __post_init__(self) -> None:
        polygon, reference = _validate_inputs(self.polygon_pixels, self.calibration, self.reference_stage_um,
                                              self.roi, self.backlash, self.settings)
        object.__setattr__(self, "polygon_pixels", polygon)
        object.__setattr__(self, "reference_stage_um", reference)
        if (not isinstance(self.actions, (tuple, list)) or not 1 <= len(self.actions) <= MAX_ACTIONS
                or any(not isinstance(a, (RasterMove, DotExposure)) for a in self.actions)):
            raise ValueError("actions must be a bounded sequence of raster actions")
        if (not isinstance(self.lines, (tuple, list)) or len(self.lines) > MAX_EXPOSURES
                or any(not isinstance(line, RasterLine) for line in self.lines)):
            raise ValueError("lines must be bounded RasterLine metadata")
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "lines", tuple(self.lines))
        duration = _number(self.estimated_duration_s, "estimated_duration_s")
        if not 0 < duration <= MAX_ESTIMATED_DURATION_S:
            raise ValueError("estimated duration is outside 0..3600 seconds")
        object.__setattr__(self, "estimated_duration_s", duration)
        _integer(self.candidate_row_count, "candidate_row_count", 1, MAX_CANDIDATE_ROWS)
        self.verify_integrity()

    @property
    def context(self) -> CalibrationContext:
        return self.calibration.context

    @property
    def calibration_revision(self) -> str:
        return self.calibration.revision

    @property
    def sample_axes(self) -> tuple[int, int]:
        return SAMPLE_AXES

    @property
    def exposure_count(self) -> int:
        return sum(isinstance(a, DotExposure) or a.cut for a in self.actions)

    @property
    def path_endpoints_um(self) -> tuple[Point, ...]:
        """Initial reference and every movement endpoint, including all DARK moves."""
        return (self.reference_stage_um,) + tuple(a.end_um for a in self.actions if isinstance(a, RasterMove))

    def verify_integrity(self) -> None:
        """Reject any mismatch between provenance and a deterministic full rebuild."""
        polygon, reference = _validate_inputs(self.polygon_pixels, self.calibration, self.reference_stage_um,
                                              self.roi, self.backlash, self.settings)
        expected = _build(polygon, reference, self.calibration, self.roi, self.backlash, self.settings)
        if expected != (self.actions, self.lines, self.estimated_duration_s, self.candidate_row_count):
            raise ValueError("raster actions/metadata do not match rebuilt provenance")

    def validate_for(self, calibration: CuttingCalibration, context: CalibrationContext,
                     reference_stage_um: Point) -> None:
        """Full preflight integrity plus exact current calibration/context/reference.

        Snapshot/frame ownership and fresh-photo alignment are external checks.
        """
        self.calibration.require_context(context)
        if not isinstance(calibration, CuttingCalibration) or calibration != self.calibration:
            raise ValueError("raster calibration/revision is stale")
        if _point(reference_stage_um, "reference_stage_um") != self.reference_stage_um:
            raise ValueError("raster reference does not match the snapshot")
        self.verify_integrity()


def plan_raster(polygon_pixels, reference_stage_um: Point, calibration: CuttingCalibration,
                roi: ROI, backlash: BacklashSettings, settings: RasterSettings) -> RasterPlan:
    """Plan clipped physical rows/dots; all invalid/over-budget inputs raise ValueError.

    Each line span has its own DARK preload. Each dot row preloads only its first
    exposed point, then traverses increasing-u dots and gaps DARK. Points share a
    global lattice phase and are never snapped to span edges. Polygon vertices
    are allowed outside ROI; exposure centers/centerlines must remain inside.
    """
    polygon, reference = _validate_inputs(polygon_pixels, calibration, reference_stage_um, roi, backlash, settings)
    actions, lines, duration, rows = _build(polygon, reference, calibration, roi, backlash, settings)
    return RasterPlan(calibration, reference, roi, backlash, settings, polygon, actions, lines, duration, rows)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


@dataclass(frozen=True, slots=True)
class RasterRecipe:
    """Strict editable input ONLY: importing never restores a runnable job.

    Loading requires a fresh photo, alignment and explicit user confirmation in
    the application before calling plan_raster with a current calibration and
    reference. Context equality alone does not establish spatial alignment.
    No qualification, actions, saved stage reference, ARM or laser lease is stored.
    Containment checks refer only to beam centers, not a finite spot footprint.
    """

    context: CalibrationContext
    roi: ROI
    polygon_pixels: tuple[Point, ...]
    settings: RasterSettings
    backlash: BacklashSettings

    def __post_init__(self) -> None:
        if not isinstance(self.context, CalibrationContext) or not isinstance(self.roi, ROI):
            raise ValueError("recipe needs a known context and ROI")
        if not isinstance(self.settings, RasterSettings) or not isinstance(self.backlash, BacklashSettings):
            raise ValueError("recipe needs explicit settings and backlash")
        self.roi.validate_for(self.context)
        object.__setattr__(self, "polygon_pixels", _polygon(self.polygon_pixels, self.context))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION, "coordinate_space": "raw_full_frame",
            "sample_axes": list(SAMPLE_AXES), "context": self.context.to_dict(),
            "roi": {name: getattr(self.roi, name) for name in ("x", "y", "width", "height")},
            "polygon_pixels": [list(p) for p in self.polygon_pixels],
            "settings": self.settings.to_dict(), "backlash": self.backlash.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> RasterRecipe:
        data = _record(data, {"schema_version", "coordinate_space", "sample_axes", "context", "roi",
                              "polygon_pixels", "settings", "backlash"}, "raster recipe")
        if type(data["schema_version"]) is not int or data["schema_version"] != _SCHEMA_VERSION:
            raise ValueError("unsupported raster recipe schema_version")
        axes = data["sample_axes"]
        if (not isinstance(axes, (tuple, list)) or len(axes) != 2
                or any(type(axis) is not int for axis in axes) or tuple(axes) != SAMPLE_AXES
                or data["coordinate_space"] != "raw_full_frame"):
            raise ValueError("recipe coordinate provenance does not match X3/Y3 raw pixels")
        roi = _record(data["roi"], {"x", "y", "width", "height"}, "ROI")
        return cls(CalibrationContext.from_dict(data["context"]), ROI(**roi), data["polygon_pixels"],
                   RasterSettings.from_dict(data["settings"]), BacklashSettings.from_dict(data["backlash"]))

    def to_json(self) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_RECIPE_JSON_BYTES:
            raise ValueError("recipe JSON exceeds 1 MiB")
        return text

    @classmethod
    def from_json(cls, text: str) -> RasterRecipe:
        if not isinstance(text, str):
            raise ValueError("recipe JSON must be text")
        try:
            if len(text) > MAX_RECIPE_JSON_BYTES or len(text.encode("utf-8")) > MAX_RECIPE_JSON_BYTES:
                raise ValueError("recipe JSON exceeds 1 MiB")
            data = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            return cls.from_dict(data)
        except (RecursionError, UnicodeError, OverflowError) as exc:
            raise ValueError("invalid or excessively nested recipe JSON") from exc
