"""Pure, immutable laser-cutting geometry for sample axes 5/6 (X3/Y3).

Stage points are absolute (X3, Y3) coordinates in micrometres, never pulses or
millimetres. Pixel points are in the raw, unflipped, uncropped full acquired
frame at the context's binning. Display transforms must be undone by callers.

Calibration observes ONE stationary sample landmark at several stage poses:
``pixel = affine_origin + J @ stage_um``. J includes the observed stage/image
sign, rotation and skew. A selection in a snapshot taken at s0 therefore needs
``s0 + inverse(J) @ (laser_pixel - selected_pixel)``. The fitted affine origin
belongs to the calibration landmark, NOT arbitrary selections in later frames.

There is no hardware, Qt, file I/O, identity fallback or laser control here.
``cut=True`` is geometric intent only; a dry executor must remain dark. Before
execution, callers must check current context, calibration, snapshot ownership,
all nine raw axis poses/motion revision, and limits/collisions for the ENTIRE
path (including dark travel). Geometry alone cannot certify those conditions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from numbers import Real
from uuid import uuid4


Point = tuple[float, float]
Matrix2 = tuple[Point, Point]
SAMPLE_AXES = (5, 6)

__all__ = [
    "Point", "Matrix2", "SAMPLE_AXES", "CalibrationContext", "CalibrationSample",
    "CuttingCalibration", "ROI", "BacklashSettings", "MoveSegment", "CuttingPlan",
    "fit_calibration", "plan_line",
]

# Dimensionless numerical identifiability bound, NOT a pixel/error tolerance.
# Both the centered stage design and J must have 2-norm condition <= this bound.
_MAX_CONDITION = 1e8
_SCHEMA_VERSION = 1


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number (not bool/text)")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} is not representable as a finite float") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    _number(value, name)
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    # Do not normalize identifiers: context equality is deliberately exact.
    return value


def _point(value: object, name: str) -> Point:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{name} must contain exactly two numbers")
    return (_number(value[0], name), _number(value[1], name))


def _matrix(value: object) -> Matrix2:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError("J must be a 2 by 2 matrix")
    return (_point(value[0], "J row"), _point(value[1], "J row"))


def _sum(values: Iterable[float]) -> float:
    try:
        return _number(math.fsum(values), "computed sum")
    except (OverflowError, ValueError) as exc:
        raise ValueError("geometry arithmetic overflowed or became nonfinite") from exc


def _dot(a: Iterable[float], b: Iterable[float]) -> float:
    return _sum(x * y for x, y in zip(a, b, strict=True))


def _matvec(matrix: Matrix2, point: Point) -> Point:
    return (_dot(matrix[0], point), _dot(matrix[1], point))


def _subtract(a: Point, b: Point) -> Point:
    return _point((a[0] - b[0], a[1] - b[1]), "coordinate difference")


def _add(a: Point, b: Point) -> Point:
    return _point((a[0] + b[0], a[1] + b[1]), "computed coordinate")


def _inverse(matrix: Matrix2) -> Matrix2:
    # Scale BEFORE determinant/singular-value calculations to avoid squaring
    # large or subnormal pixel-per-um scales. No absolute determinant cutoff.
    scale = max(abs(v) for row in matrix for v in row)
    if scale == 0:
        raise ValueError("matrix is singular; calibration is not identifiable")
    (a, b), (c, d) = tuple(tuple(v / scale for v in row) for row in matrix)
    det = _sum((a * d, -b * c))
    norm_squared = a * a + b * b + c * c + d * d
    largest_squared = (norm_squared + math.hypot(
        a * a + b * b - c * c - d * d, 2.0 * (a * c + b * d)
    )) / 2.0
    if det == 0 or largest_squared / abs(det) > _MAX_CONDITION:
        raise ValueError("matrix is singular or ill-conditioned")
    return _matrix(((d / det / scale, -b / det / scale),
                    (-c / det / scale, a / det / scale)))


def _record(data: object, keys: set[str], name: str) -> Mapping:
    if not isinstance(data, Mapping) or set(data) != keys:
        raise ValueError(f"{name} requires exactly the documented fields")
    return data


def _version(data: Mapping) -> None:
    if type(data["schema_version"]) is not int or data["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("unsupported geometry schema_version")


@dataclass(frozen=True, slots=True)
class CalibrationContext:
    """Exact acquisition identity; dimensions are full acquired-frame pixels.

    Unknown serial/objective must not be represented by a made-up identifier.
    The caller must supply the actual acquisition values. Binning is a positive
    integer; changes in any field invalidate a calibration for that acquisition.
    """

    camera_serial: str
    objective: str
    width: int
    height: int
    binning: int

    def __post_init__(self) -> None:
        _text(self.camera_serial, "camera_serial")
        _text(self.objective, "objective")
        for name in ("width", "height", "binning"):
            _positive_integer(getattr(self, name), name)

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in (
            "camera_serial", "objective", "width", "height", "binning"
        )}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CalibrationContext:
        data = _record(data, {"camera_serial", "objective", "width", "height", "binning"}, "context")
        return cls(**data)


def _context(value: object) -> CalibrationContext:
    if not isinstance(value, CalibrationContext):
        raise ValueError("a known CalibrationContext is required")
    return value


def _frame_pixel(value: object, context: CalibrationContext, name: str) -> Point:
    point = _point(value, name)
    if not (0 <= point[0] < context.width and 0 <= point[1] < context.height):
        raise ValueError(f"{name} lies outside the raw full frame")
    return point


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    """ONE tracked landmark's raw pixel position at an absolute X3/Y3 um pose."""

    stage_um: Point
    pixel: Point

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage_um", _point(self.stage_um, "stage_um"))
        object.__setattr__(self, "pixel", _point(self.pixel, "pixel"))


@dataclass(frozen=True, slots=True)
class CuttingCalibration:
    """Validated affine fit, with no implicit/default calibration.

    J's rows are (pixel-x, pixel-y), columns are (X3, Y3), in px/um.
    affine_origin is the fitted landmark pixel at stage (0, 0). Residuals are
    Euclidean lengths of inverse(J) @ pixel_residual, in um; max_error_um is
    the user's explicit inclusive bound on every sample (and hence on RMS).
    revision is an opaque nonempty fit identifier, preserved through JSON.
    Use fit_calibration for acquisition, from_dict for trusted saved fits.
    Serialized metadata records provenance, not cryptographic authenticity.
    """

    J: Matrix2
    affine_origin: Point
    laser_pixel: Point
    context: CalibrationContext
    rms_um: float
    max_residual_um: float
    max_error_um: float
    sample_count: int
    revision: str

    def __post_init__(self) -> None:
        _context(self.context)
        object.__setattr__(self, "J", _matrix(self.J))
        _inverse(self.J)
        object.__setattr__(self, "affine_origin", _point(self.affine_origin, "affine_origin"))
        object.__setattr__(self, "laser_pixel", _frame_pixel(self.laser_pixel, self.context, "laser_pixel"))
        for name in ("rms_um", "max_residual_um", "max_error_um"):
            value = _number(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
            object.__setattr__(self, name, value)
        if not self.rms_um <= self.max_residual_um <= self.max_error_um:
            raise ValueError("calibration residual exceeds max_error_um or has inconsistent RMS")
        if _positive_integer(self.sample_count, "sample_count") < 3:
            raise ValueError("calibration requires at least three samples")
        _text(self.revision, "revision")

    def is_valid_for(self, context: CalibrationContext) -> bool:
        """False for unknown context or ANY acquisition-identity difference."""
        return isinstance(context, CalibrationContext) and self.context == context

    def require_context(self, context: CalibrationContext) -> None:
        """Raise ValueError unless the current acquisition context matches exactly."""
        if not self.is_valid_for(context):
            raise ValueError("calibration context does not match the current acquisition")

    def stage_target(self, pixel: Point, reference_stage_um: Point) -> Point:
        """Align a snapshot selection to the laser: s0 + inv(J) @ (laser-p).

        pixel and reference_stage_um MUST belong to the same snapshot. This
        method checks the frame but cannot establish snapshot ownership/freshness.
        """
        pixel = _frame_pixel(pixel, self.context, "pixel")
        reference = _point(reference_stage_um, "reference_stage_um")
        offset = _matvec(_inverse(self.J), _subtract(self.laser_pixel, pixel))
        target = _add(reference, offset)
        roundoff = 32.0 * math.ulp(_number(math.hypot(*offset), "target offset"))
        if any(abs(offset[i]) > roundoff and target[i] == reference[i] for i in (0, 1)):
            raise ValueError("stage target offset is not numerically resolvable at this reference")
        return target

    def pixel_for_stage(self, stage_um: Point, reference_stage_um: Point) -> Point:
        """Project any path pose to the snapshot: laser - J @ (stage-s0).

        Intentionally permits results outside frame/ROI: dark preload and travel
        may leave the selected image area and must still be visible in preview.
        The controller remains responsible for their physical safety.
        """
        stage = _point(stage_um, "stage_um")
        reference = _point(reference_stage_um, "reference_stage_um")
        return _subtract(self.laser_pixel, _matvec(self.J, _subtract(stage, reference)))

    def to_dict(self) -> dict[str, object]:
        """Fresh JSON-safe schema v1 record; no implicit values on reload."""
        return {
            "schema_version": _SCHEMA_VERSION, "sample_axes": list(SAMPLE_AXES),
            "stage_units": "um", "pixel_coordinates": "raw_full_frame",
            "J": [list(row) for row in self.J], "affine_origin": list(self.affine_origin),
            "laser_pixel": list(self.laser_pixel), "context": self.context.to_dict(),
            "rms_um": self.rms_um, "max_residual_um": self.max_residual_um,
            "max_error_um": self.max_error_um, "sample_count": self.sample_count,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CuttingCalibration:
        """Validate a complete v1 record; unknown/partial/invalid fits raise ValueError."""
        data = _record(data, {
            "schema_version", "sample_axes", "stage_units", "pixel_coordinates", "J",
            "affine_origin", "laser_pixel", "context", "rms_um", "max_residual_um",
            "max_error_um", "sample_count", "revision",
        }, "calibration")
        _version(data)
        axes = data["sample_axes"]
        if (not isinstance(axes, (list, tuple)) or len(axes) != 2
                or any(type(axis) is not int for axis in axes) or tuple(axes) != SAMPLE_AXES):
            raise ValueError("only sample axes 5/6 (X3/Y3) are supported")
        if data["stage_units"] != "um" or data["pixel_coordinates"] != "raw_full_frame":
            raise ValueError("calibration coordinate provenance does not match")
        return cls(
            J=data["J"], affine_origin=data["affine_origin"], laser_pixel=data["laser_pixel"],
            context=CalibrationContext.from_dict(data["context"]), rms_um=data["rms_um"],
            max_residual_um=data["max_residual_um"], max_error_um=data["max_error_um"],
            sample_count=data["sample_count"], revision=data["revision"],
        )


def fit_calibration(
    samples: Iterable[CalibrationSample], laser_pixel: Point,
    context: CalibrationContext, max_error_um: float,
) -> CuttingCalibration:
    """Fit >=3 non-collinear poses of the SAME landmark by affine least squares.

    Stage design and J must be nonsingular with condition <= 1e8. The solver is
    centered/scaled QR (with reorthogonalization), not normal equations. Every
    residual is transformed through inverse(J) before applying max_error_um.
    The tolerance must be explicitly supplied, finite and nonnegative; zero
    accepts only numerically exact fits. No hidden pixel-error allowance exists.
    Feature identity must be ensured by the acquisition/session layer.
    """
    _context(context)
    laser = _frame_pixel(laser_pixel, context, "laser_pixel")
    tolerance = _number(max_error_um, "max_error_um")
    if tolerance < 0:
        raise ValueError("max_error_um must be nonnegative")
    try:
        samples = tuple(samples)
    except TypeError as exc:
        raise ValueError("samples must be an iterable of CalibrationSample") from exc
    if len(samples) < 3 or any(not isinstance(s, CalibrationSample) for s in samples):
        raise ValueError("at least three CalibrationSample objects are required")
    for sample in samples:
        _frame_pixel(sample.pixel, context, "sample pixel")

    # Subtract anchors first: large absolute stage origins must not swamp a
    # small, well-resolved calibration movement when computing its mean.
    stages = [_subtract(s.stage_um, samples[0].stage_um) for s in samples]
    pixels = [_subtract(s.pixel, samples[0].pixel) for s in samples]
    count = len(samples)
    stage_mean = tuple(_sum(p[i] / count for p in stages) for i in (0, 1))
    pixel_mean = tuple(_sum(p[i] / count for p in pixels) for i in (0, 1))
    stages = [_subtract(p, stage_mean) for p in stages]
    pixels = [_subtract(p, pixel_mean) for p in pixels]
    stage_scale = max(abs(v) for p in stages for v in p)
    pixel_scale = max(abs(v) for p in pixels for v in p)
    if stage_scale == 0 or pixel_scale == 0:
        raise ValueError("calibration poses/features do not span two dimensions")
    x = [p[0] / stage_scale for p in stages]
    y = [p[1] / stage_scale for p in stages]
    rx = math.sqrt(_dot(x, x))
    if rx == 0:
        raise ValueError("stage poses are collinear")
    qx = [v / rx for v in x]
    rxy = _dot(qx, y)
    perpendicular = [v - rxy * q for v, q in zip(y, qx)]
    correction = _dot(qx, perpendicular)
    perpendicular = [v - correction * q for v, q in zip(perpendicular, qx)]
    rxy += correction
    ry = math.sqrt(_dot(perpendicular, perpendicular))
    _inverse(((rx, rxy), (0.0, ry)))  # Reject collinear/ill-conditioned stage design.
    qy = [v / ry for v in perpendicular]
    rows = []
    for i in (0, 1):
        values = [p[i] / pixel_scale for p in pixels]
        b = _dot(qy, values) / ry
        a = (_dot(qx, values) - rxy * b) / rx
        rows.append((a * pixel_scale / stage_scale, b * pixel_scale / stage_scale))
    jacobian = _matrix(rows)
    inverse = _inverse(jacobian)
    origin = _subtract(
        _add(samples[0].pixel, pixel_mean),
        _matvec(jacobian, _add(samples[0].stage_um, stage_mean)),
    )
    errors = [
        _number(math.hypot(*_matvec(inverse, _subtract(p, _matvec(jacobian, s)))), "residual_um")
        for s, p in zip(stages, pixels)
    ]
    largest = max(errors)
    # Scale the RMS so finite physical errors do not overflow when squared.
    rms = largest * math.sqrt(_sum((e / largest) ** 2 / count for e in errors)) if largest else 0.0
    return CuttingCalibration(
        J=jacobian, affine_origin=origin, laser_pixel=laser, context=context,
        rms_um=rms, max_residual_um=largest, max_error_um=tolerance,
        sample_count=count, revision=uuid4().hex,
    )


@dataclass(frozen=True, slots=True)
class ROI:
    """Raw full-frame pixel rectangle, half-open [x,x+width) x [y,y+height).

    Positive extents and nonnegative origins are required. validate_for(context)
    additionally requires the complete ROI inside that full frame. Because this
    ROI is convex, containment of both line endpoints contains the whole cut.
    Dark travel/preload need not lie inside ROI (or even inside the frame).
    """

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if self.x < 0 or self.y < 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("ROI must have nonnegative origin and positive extents")
        right, bottom = _add((self.x, self.y), (self.width, self.height))
        if right <= self.x or bottom <= self.y:
            raise ValueError("ROI extents are not numerically resolvable")

    def contains(self, pixel: Point) -> bool:
        x, y = _point(pixel, "pixel")
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def validate_for(self, context: CalibrationContext) -> None:
        _context(context)
        if self.x + self.width > context.width or self.y + self.height > context.height:
            raise ValueError("ROI lies outside the raw full frame")


@dataclass(frozen=True, slots=True)
class BacklashSettings:
    """Measured nonnegative per-axis backlash in um, applied at 0..100 percent.

    effective_um = (x_um*x_percent/100, y_um*y_percent/100). Each planning call
    applies at most this one bounded offset per moving axis; it never modifies
    calibration/targets or accumulates offsets from previous plans.
    """

    x_um: float = 0.0
    y_um: float = 0.0
    x_percent: float = 100.0
    y_percent: float = 100.0

    def __post_init__(self) -> None:
        for name in ("x_um", "y_um", "x_percent", "y_percent"):
            value = _number(getattr(self, name), name)
            if value < 0 or (name.endswith("percent") and value > 100):
                raise ValueError(f"{name} is outside its allowed bounds")
            object.__setattr__(self, name, value)

    @property
    def effective_um(self) -> Point:
        return (self.x_um * (self.x_percent / 100.0), self.y_um * (self.y_percent / 100.0))

    def to_dict(self) -> dict[str, object]:
        return {"schema_version": _SCHEMA_VERSION, "x_um": self.x_um, "y_um": self.y_um,
                "x_percent": self.x_percent, "y_percent": self.y_percent}

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BacklashSettings:
        data = _record(data, {"schema_version", "x_um", "y_um", "x_percent", "y_percent"}, "backlash")
        _version(data)
        return cls(**{name: data[name] for name in ("x_um", "y_um", "x_percent", "y_percent")})


@dataclass(frozen=True, slots=True)
class MoveSegment:
    """A nonzero straight stage move. cut is intent, never a laser command."""

    start_um: Point
    end_um: Point
    cut: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_um", _point(self.start_um, "start_um"))
        object.__setattr__(self, "end_um", _point(self.end_um, "end_um"))
        if type(self.cut) is not bool:
            raise ValueError("cut must be a bool")
        length = _number(math.hypot(*_subtract(self.end_um, self.start_um)), "segment length")
        if length == 0:
            raise ValueError("zero-length moves are not permitted")


def _backlash_offset(delta: Point, backlash: BacklashSettings) -> Point:
    """Signed preload with one shared numerical stationary-axis criterion."""
    zero_roundoff = 32.0 * math.ulp(_number(math.hypot(*delta), "cut length"))
    return tuple(
        math.copysign(amount, movement) if abs(movement) > zero_roundoff else 0.0
        for movement, amount in zip(delta, backlash.effective_um)
    )


def _line_path(
    start_pixel: Point, end_pixel: Point, reference: Point,
    calibration: CuttingCalibration, backlash: BacklashSettings,
) -> tuple[Point, tuple[MoveSegment, ...]]:
    start = calibration.stage_target(start_pixel, reference)
    end = calibration.stage_target(end_pixel, reference)
    # Determine stage direction before adding a possibly large absolute origin.
    delta = _matvec(_inverse(calibration.J), _subtract(start_pixel, end_pixel))
    length = _number(math.hypot(*delta), "cut length")
    if length == 0 or start == end:
        raise ValueError("line must have nonzero, numerically resolvable length")
    # Ignore only floating-point roundoff in an otherwise stationary axis; this
    # is NOT a fit/measurement uncertainty setting. Avoid a full backlash move
    # on an axis whose theoretical zero acquired tiny QR/inversion roundoff.
    zero_roundoff = 32.0 * math.ulp(length)
    if any(abs(delta[i]) > zero_roundoff and start[i] == end[i] for i in (0, 1)):
        raise ValueError("line axis displacement is not numerically resolvable")
    offset = _backlash_offset(delta, backlash)
    preload = _subtract(start, offset)
    for i in (0, 1):
        if offset[i] != 0 and preload[i] == start[i]:
            raise ValueError("backlash offset is not numerically resolvable at this stage pose")
    segments = []
    for a, b in ((reference, preload), (preload, start)):
        if a != b:
            segments.append(MoveSegment(a, b, cut=False))
    segments.append(MoveSegment(start, end, cut=True))
    return preload, tuple(segments)


@dataclass(frozen=True, slots=True)
class CuttingPlan:
    """Immutable complete path and its pixel/calibration/reference provenance.

    Usually construct via plan_line. The path begins at reference_stage_um,
    travels DARK to preload and then DARK to the cut start, omitting zero moves,
    and ends with exactly one cut-intent segment. speed_um_s is the requested
    positive path speed; hardware speed/acceleration limits remain external.
    Storing the full calibration permits equality checks even if somebody edits
    a persisted fit without changing its revision. No snapshot ID is invented:
    the external cutting_session owns frame identity and all-axis motion state.
    """

    start_pixel: Point
    end_pixel: Point
    reference_stage_um: Point
    calibration: CuttingCalibration
    roi: ROI
    backlash: BacklashSettings
    speed_um_s: float
    segments: tuple[MoveSegment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.calibration, CuttingCalibration):
            raise ValueError("a fitted CuttingCalibration is required")
        if not isinstance(self.roi, ROI) or not isinstance(self.backlash, BacklashSettings):
            raise ValueError("ROI and BacklashSettings are required")
        self.roi.validate_for(self.context)
        for name in ("start_pixel", "end_pixel"):
            point = _frame_pixel(getattr(self, name), self.context, name)
            if not self.roi.contains(point):
                raise ValueError(f"{name} lies outside ROI")
            object.__setattr__(self, name, point)
        object.__setattr__(self, "reference_stage_um", _point(self.reference_stage_um, "reference_stage_um"))
        speed = _number(self.speed_um_s, "speed_um_s")
        if speed <= 0:
            raise ValueError("speed_um_s must be positive")
        object.__setattr__(self, "speed_um_s", speed)
        if not isinstance(self.segments, (tuple, list)) or any(
            not isinstance(s, MoveSegment) for s in self.segments
        ):
            raise ValueError("segments must contain MoveSegment objects")
        segments = tuple(self.segments)
        _, expected = _line_path(self.start_pixel, self.end_pixel, self.reference_stage_um,
                                 self.calibration, self.backlash)
        if segments != expected:
            raise ValueError("segments do not match the declared line/calibration/backlash")
        object.__setattr__(self, "segments", segments)

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
    def preload_um(self) -> Point:
        """Dark approach origin, equal to cut start if compensation is zero."""
        return _line_path(self.start_pixel, self.end_pixel, self.reference_stage_um,
                          self.calibration, self.backlash)[0]

    @property
    def backlash_offset_um(self) -> Point:
        """Signed preload shared by dark and live paths, before stage-origin rounding."""
        delta = _matvec(_inverse(self.calibration.J), _subtract(self.start_pixel, self.end_pixel))
        return _backlash_offset(delta, self.backlash)

    @property
    def path_endpoints_um(self) -> tuple[Point, ...]:
        """Reference plus every segment endpoint, including dark travel/preload."""
        return (self.reference_stage_um,) + tuple(s.end_um for s in self.segments)

    def validate_for(
        self, calibration: CuttingCalibration, context: CalibrationContext,
        reference_stage_um: Point,
    ) -> None:
        """Reject changed fit/context/reference; NOT a complete execution interlock.

        reference_stage_um is the snapshot pose, not a new arbitrary pose. Full
        snapshot ownership and current all-axis motion checks remain external.
        """
        self.calibration.require_context(context)
        if not isinstance(calibration, CuttingCalibration) or calibration != self.calibration:
            raise ValueError("plan calibration or revision is stale")
        if _point(reference_stage_um, "reference_stage_um") != self.reference_stage_um:
            raise ValueError("plan reference_stage_um does not match the snapshot")


def plan_line(
    start_pixel: Point, end_pixel: Point, reference_stage_um: Point,
    calibration: CuttingCalibration, roi: ROI, backlash: BacklashSettings,
    speed_um_s: float = 100.0,
) -> CuttingPlan:
    """Plan one ROI-contained line for X3/Y3, with bounded dark preload.

    Per moving stage axis, preload = cut_start - sign(cut_delta)*effective_um.
    Zero-motion axes get no offset. Reversing the line reverses the preload
    direction; endpoint targets never receive cumulative compensation. There is
    no retained direction state between plans. All invalid inputs raise ValueError.
    """
    if not isinstance(calibration, CuttingCalibration):
        raise ValueError("a fitted CuttingCalibration is required")
    if not isinstance(backlash, BacklashSettings) or not isinstance(roi, ROI):
        raise ValueError("ROI and BacklashSettings are required")
    start = _frame_pixel(start_pixel, calibration.context, "start_pixel")
    end = _frame_pixel(end_pixel, calibration.context, "end_pixel")
    reference = _point(reference_stage_um, "reference_stage_um")
    roi.validate_for(calibration.context)
    if not roi.contains(start) or not roi.contains(end):
        raise ValueError("line endpoints must lie inside ROI")
    _, segments = _line_path(start, end, reference, calibration, backlash)
    return CuttingPlan(start, end, reference, calibration, roi, backlash, speed_um_s, segments)
