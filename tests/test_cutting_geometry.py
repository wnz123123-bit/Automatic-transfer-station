"""Pure geometry checks: no GUI creation, SDK calls, device connections or motion."""

from dataclasses import FrozenInstanceError, replace
import json
import math

import pytest

from automatic_transfer_station.cutting_geometry import (
    SAMPLE_AXES, BacklashSettings, CalibrationContext, CalibrationSample,
    CuttingCalibration, CuttingPlan, MoveSegment, ROI, fit_calibration, plan_line,
)


CONTEXT = CalibrationContext("camera-123", "20X", 1200, 900, 1)
LASER = (500.0, 400.0)
FULL_ROI = ROI(0, 0, CONTEXT.width, CONTEXT.height)
POSES = ((-20, -10), (20, -10), (-20, 10), (20, 10), (0, 0))


def samples_for(jacobian=((2.0, 0.0), (0.0, 3.0)), origin=(600.0, 450.0), poses=POSES):
    # Independent forward model: a single landmark moves in the acquired image.
    return tuple(CalibrationSample(
        (x, y),
        (origin[0] + jacobian[0][0] * x + jacobian[0][1] * y,
         origin[1] + jacobian[1][0] * x + jacobian[1][1] * y),
    ) for x, y in poses)


def calibration_for(jacobian=((2.0, 0.0), (0.0, 3.0))):
    return fit_calibration(samples_for(jacobian), LASER, CONTEXT, max_error_um=1e-8)


def make_plan(start=(600, 450), end=(620, 480), reference=(100, 200),
              calibration=None, roi=FULL_ROI, backlash=None, speed=100):
    return plan_line(start, end, reference,
                     calibration if calibration is not None else calibration_for(), roi,
                     backlash if backlash is not None else BacklashSettings(), speed)


def assert_matrix(actual, expected, abs=1e-10):
    for a, e in zip(actual, expected):
        assert a == pytest.approx(e, abs=abs)


def test_fit_and_inverse_use_observed_sign_and_same_snapshot_reference():
    calibration = calibration_for()
    assert_matrix(calibration.J, ((2, 0), (0, 3)))
    assert calibration.affine_origin == pytest.approx((600, 450))
    assert calibration.rms_um < 1e-10
    assert calibration.max_residual_um < 1e-10
    assert calibration.sample_count == len(POSES)
    # The point moves right/down for positive stage movement: bring this
    # selection to the laser by moving the stage left/up, relative to THIS pose.
    target = calibration.stage_target((520, 430), (1000, -2000))
    assert target == pytest.approx((990, -2010))
    assert calibration.stage_target(LASER, (1000, -2000)) == (1000, -2000)
    assert calibration.pixel_for_stage(target, (1000, -2000)) == pytest.approx((520, 430))
    assert calibration.pixel_for_stage((1000, -2000), (1000, -2000)) == LASER


@pytest.mark.parametrize("jacobian", [
    ((-2, 0), (0, -3)),         # reversed stage/image signs
    ((0, -2), (3, 0)),         # rotation and unequal scales
    ((1.5, -0.7), (0.4, 2.0)), # rotation plus skew
    ((2, 0.6), (0.5, -1.4)),   # reflection plus skew
])
def test_rotated_skewed_calibration_roundtrip_and_alignment(jacobian):
    calibration = calibration_for(jacobian)
    assert_matrix(calibration.J, jacobian)
    reference = (1020.0, -37.0)
    selected = (530.0, 417.0)
    target = calibration.stage_target(selected, reference)
    dx, dy = target[0] - reference[0], target[1] - reference[1]
    # Verify physical alignment by forward motion, independent of inverse code.
    aligned = (selected[0] + jacobian[0][0] * dx + jacobian[0][1] * dy,
               selected[1] + jacobian[1][0] * dx + jacobian[1][1] * dy)
    assert aligned == pytest.approx(LASER)
    assert calibration.pixel_for_stage(target, reference) == pytest.approx(selected)


def test_affine_landmark_origin_is_not_reused_for_later_snapshot_selection():
    first = fit_calibration(samples_for(origin=(600, 450)), LASER, CONTEXT, 1e-8)
    second = fit_calibration(samples_for(origin=(800, 600)), LASER, CONTEXT, 1e-8)
    assert first.affine_origin != second.affine_origin
    assert first.stage_target((540, 430), (123, 456)) == pytest.approx(
        second.stage_target((540, 430), (123, 456)))


def test_three_noncollinear_poses_and_generator_are_supported():
    samples = samples_for(poses=((0, 0), (10, 0), (0, 10)))
    calibration = fit_calibration((s for s in samples), LASER, CONTEXT, 1e-8)
    assert calibration.sample_count == 3
    assert_matrix(calibration.J, ((2, 0), (0, 3)))


def test_centered_fit_handles_large_absolute_stage_origin():
    base = (1e12, -2e12)
    jacobian = ((1.5, -0.5), (0.25, 2.0))
    samples = tuple(CalibrationSample(
        (base[0] + x, base[1] + y),
        (600 + jacobian[0][0] * x + jacobian[0][1] * y,
         450 + jacobian[1][0] * x + jacobian[1][1] * y),
    ) for x, y in POSES)
    calibration = fit_calibration(samples, LASER, CONTEXT, 1e-8)
    assert_matrix(calibration.J, jacobian)
    assert calibration.rms_um < 1e-10


@pytest.mark.parametrize("poses", [
    ((0, 0), (0, 0), (0, 0)),
    ((0, 0), (10, 10), (20, 20)),
    ((0, 0), (0, 10), (0, 20)),
    ((0, 0), (10, 0), (20, 1e-10)),
])
def test_collinear_duplicate_and_ill_conditioned_stage_poses_reject(poses):
    with pytest.raises(ValueError):
        fit_calibration(samples_for(poses=poses), LASER, CONTEXT, 100)


@pytest.mark.parametrize("jacobian", [
    ((0, 0), (0, 0)), ((1, 2), (2, 4)), ((1, 0), (0, 1e-10)),
])
def test_singular_and_ill_conditioned_image_response_reject(jacobian):
    with pytest.raises(ValueError):
        fit_calibration(samples_for(jacobian), LASER, CONTEXT, 100)


@pytest.mark.parametrize("scale", [1e-150, 1e150])
def test_matrix_conditioning_is_scale_relative_not_absolute_determinant(scale):
    # Tiny/huge units can be well-conditioned; representable inverse is required.
    calibration = replace(calibration_for(), J=((scale, 0), (0, -scale)))
    target = calibration.stage_target((501, 401), (0, 0))
    assert target == pytest.approx((-1 / scale, 1 / scale))
    assert calibration.pixel_for_stage(target, (0, 0)) == pytest.approx((501, 401))


def test_residuals_use_inverse_affine_metric_in_um_not_pixel_norm():
    jacobian = ((2.0, 0.8), (-0.3, 3.0))
    # This checkerboard noise is orthogonal to intercept/x/y design columns,
    # so the least squares fit has the exact original J. Physical noise has
    # norm 0.5 um even though its pixel displacement has a different norm.
    signs = (1, -1, -1, 1)
    poses = ((-10, -10), (10, -10), (-10, 10), (10, 10))
    samples = []
    for sample, sign in zip(samples_for(jacobian, poses=poses), signs):
        noise = (jacobian[0][0] * 0.3 + jacobian[0][1] * 0.4,
                 jacobian[1][0] * 0.3 + jacobian[1][1] * 0.4)
        samples.append(CalibrationSample(sample.stage_um, (
            sample.pixel[0] + sign * noise[0], sample.pixel[1] + sign * noise[1])))
    calibration = fit_calibration(samples, LASER, CONTEXT, 0.501)
    assert_matrix(calibration.J, jacobian)
    assert calibration.rms_um == pytest.approx(0.5, abs=1e-12)
    assert calibration.max_residual_um == pytest.approx(0.5, abs=1e-12)
    with pytest.raises(ValueError, match="max_error_um"):
        fit_calibration(samples, LASER, CONTEXT, 0.499)


def test_maximum_residual_bound_prevents_rms_hiding_one_bad_landmark_click():
    samples = list(samples_for())
    samples[-1] = replace(samples[-1], pixel=(samples[-1].pixel[0] + 2, samples[-1].pixel[1]))
    calibration = fit_calibration(samples, LASER, CONTEXT, 1)
    assert calibration.rms_um == pytest.approx(0.4)
    assert calibration.max_residual_um == pytest.approx(0.8)
    with pytest.raises(ValueError, match="max_error_um"):
        fit_calibration(samples, LASER, CONTEXT, 0.6)


def test_exact_zero_uncertainty_setting_is_allowed_without_hidden_pixel_tolerance():
    # Symmetric powers-of-two data keeps this fit exactly representable.
    samples = samples_for(((2, 0), (0, 2)), poses=((-8, -8), (8, -8), (-8, 8), (8, 8)))
    calibration = fit_calibration(samples, LASER, CONTEXT, 0)
    assert calibration.rms_um == calibration.max_residual_um == 0
    payload = calibration.to_dict()
    payload["max_residual_um"] = 1e-20
    with pytest.raises(ValueError):
        CuttingCalibration.from_dict(payload)


@pytest.mark.parametrize("changes", [
    {"camera_serial": "different"}, {"objective": "40X"}, {"objective": "20x"},
    {"width": 600}, {"height": 450}, {"binning": 2},
])
def test_each_context_change_invalidates_fit_and_plan(changes):
    calibration = calibration_for()
    changed = replace(CONTEXT, **changes)
    assert not calibration.is_valid_for(changed)
    with pytest.raises(ValueError, match="context"):
        calibration.require_context(changed)
    plan = make_plan(calibration=calibration)
    with pytest.raises(ValueError, match="context"):
        plan.validate_for(calibration, changed, plan.reference_stage_um)


def test_context_requires_exact_known_identity():
    calibration = calibration_for()
    assert calibration.is_valid_for(replace(CONTEXT))
    calibration.require_context(CONTEXT)
    for unknown in (None, {}, CONTEXT.to_dict()):
        assert not calibration.is_valid_for(unknown)
        with pytest.raises(ValueError):
            calibration.require_context(unknown)
    assert CalibrationContext.from_dict(json.loads(json.dumps(CONTEXT.to_dict()))) == CONTEXT


def test_calibration_json_roundtrip_is_complete_independent_and_preserves_revision():
    calibration = calibration_for(((1, -2), (3, 0.5)))
    payload = calibration.to_dict()
    restored = CuttingCalibration.from_dict(json.loads(json.dumps(payload, allow_nan=False)))
    assert restored == calibration
    assert restored.revision == calibration.revision
    assert payload["sample_axes"] == [5, 6]
    assert payload["stage_units"] == "um"
    assert payload["pixel_coordinates"] == "raw_full_frame"
    assert isinstance(restored.J, tuple) and isinstance(restored.J[0], tuple)
    assert restored.stage_target((570, 430), (12, 34)) == pytest.approx(
        calibration.stage_target((570, 430), (12, 34)))
    payload["J"][0][0] = 99
    payload["context"]["objective"] = "changed"
    assert calibration.J[0][0] != 99
    assert calibration.context == CONTEXT
    assert calibration_for().revision != calibration_for().revision


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True), ("sample_axes", [7, 8]),
    ("sample_axes", [5.0, 6.0]), ("stage_units", "mm"),
    ("pixel_coordinates", "flipped_preview"), ("J", [[1, 2], [2, 4]]),
    ("J", [[math.inf, 0], [0, 1]]), ("J", [[1, 0], [0, 1e-10]]),
    ("rms_um", -1), ("max_residual_um", -1), ("max_error_um", math.nan),
    ("sample_count", 2), ("sample_count", True), ("sample_count", 3.5),
    ("revision", ""), ("revision", None), ("context", None),
    ("laser_pixel", (1200, 200)), ("affine_origin", (math.nan, 0)),
])
def test_corrupt_calibration_records_reject(field, value):
    payload = calibration_for().to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        CuttingCalibration.from_dict(payload)


@pytest.mark.parametrize("kind", ["missing", "extra", "unknown", "bad_rms", "excess_error"])
def test_partial_unknown_or_inconsistent_calibration_never_falls_back(kind):
    payload = calibration_for().to_dict()
    if kind == "missing":
        del payload["J"]
    elif kind == "extra":
        payload["unknown"] = 1
    elif kind == "unknown":
        payload = None
    elif kind == "bad_rms":
        payload["rms_um"] = 1
    else:
        payload["max_residual_um"] = 100
    with pytest.raises(ValueError):
        CuttingCalibration.from_dict(payload)


def test_roi_half_open_boundaries_and_frame_containment():
    roi = ROI(20, 30, 100, 60)
    roi.validate_for(CONTEXT)
    assert roi.contains((20, 30))
    assert roi.contains((math.nextafter(120, 0), math.nextafter(90, 0)))
    for point in ((19.99, 40), (30, 29.99), (120, 40), (30, 90)):
        assert not roi.contains(point)
    FULL_ROI.validate_for(CONTEXT)
    assert FULL_ROI.contains((1199, 899))
    assert not FULL_ROI.contains((1200, 899))
    with pytest.raises(ValueError):
        ROI(1100, 0, 101, 100).validate_for(CONTEXT)
    with pytest.raises(ValueError):
        ROI(0, 850, 100, 51).validate_for(CONTEXT)


@pytest.mark.parametrize("values", [
    (-1, 0, 10, 10), (0, -1, 10, 10), (0, 0, 0, 10), (0, 0, 10, -1),
    (math.inf, 0, 10, 10), (0, 0, math.nan, 10), (0, 0, True, 10),
    (1e308, 0, 1e308, 10), (1e20, 0, 1, 10),
])
def test_bad_roi_geometry_rejects(values):
    with pytest.raises(ValueError):
        ROI(*values)


def test_line_rejects_invalid_roi_even_when_endpoints_happen_to_be_inside():
    with pytest.raises(ValueError, match="ROI"):
        make_plan(roi=ROI(0, 0, 1201, 900))


@pytest.mark.parametrize("point", [(90, 150), (200, 150), (150, 90), (150, 200)])
def test_either_line_endpoint_outside_roi_rejects(point):
    roi = ROI(100, 100, 100, 100)
    for start, end in ((point, (150, 150)), ((150, 150), point)):
        with pytest.raises(ValueError, match="ROI"):
            make_plan(start=start, end=end, roi=roi)


def test_percentage_is_per_axis_in_um_and_zero_can_disable_each_axis():
    settings = BacklashSettings(x_um=12, y_um=8, x_percent=25, y_percent=75)
    assert settings.effective_um == (3, 6)
    assert replace(settings, x_percent=0).effective_um == (0, 6)
    assert replace(settings, y_percent=0).effective_um == (3, 0)
    assert BacklashSettings().effective_um == (0, 0)
    assert BacklashSettings(1e308, 1e308).effective_um == (1e308, 1e308)
    assert BacklashSettings.from_dict(json.loads(json.dumps(settings.to_dict()))) == settings


@pytest.mark.parametrize("field,value", [
    ("x_um", -1), ("y_um", -1), ("x_percent", -0.01), ("y_percent", 100.01),
    ("x_um", math.inf), ("y_um", math.nan), ("x_percent", True), ("y_percent", "50"),
])
def test_invalid_backlash_rejects_in_constructor_and_json(field, value):
    with pytest.raises(ValueError):
        BacklashSettings(**{field: value})
    payload = BacklashSettings().to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        BacklashSettings.from_dict(payload)


@pytest.mark.parametrize("payload", [{}, None, {"schema_version": 1},
    {"schema_version": True, "x_um": 0, "y_um": 0, "x_percent": 100, "y_percent": 100}])
def test_unknown_or_partial_backlash_json_rejects(payload):
    with pytest.raises(ValueError):
        BacklashSettings.from_dict(payload)


def test_full_path_includes_dark_reposition_dark_preload_then_unchanged_cut():
    calibration = calibration_for()
    backlash = BacklashSettings(12, 8, 25, 75)
    plan = make_plan(calibration=calibration, backlash=backlash, speed=12.5)
    start, end = (50, 200 - 50 / 3), (40, 200 - 80 / 3)
    preload = (start[0] + 3, start[1] + 6)
    assert plan.preload_um == pytest.approx(preload)
    assert len(plan.segments) == 3
    assert [s.cut for s in plan.segments] == [False, False, True]
    assert plan.segments[0].start_um == plan.reference_stage_um
    assert plan.segments[0].end_um == pytest.approx(preload)
    assert plan.segments[1].start_um == pytest.approx(preload)
    assert plan.segments[1].end_um == pytest.approx(start)
    assert plan.segments[-1].start_um == pytest.approx(start)
    assert plan.segments[-1].end_um == pytest.approx(end)
    assert plan.speed_um_s == 12.5
    assert plan.sample_axes == SAMPLE_AXES == (5, 6)
    assert plan.context == CONTEXT
    assert plan.calibration_revision == calibration.revision
    assert plan.path_endpoints_um == (plan.reference_stage_um,) + tuple(s.end_um for s in plan.segments)
    for previous, following in zip(plan.segments, plan.segments[1:]):
        assert previous.end_um == following.start_um
    for i in range(11):
        t = i / 10
        pose = tuple(a + t * (b - a) for a, b in zip(start, end))
        assert calibration.pixel_for_stage(pose, plan.reference_stage_um) == pytest.approx(
            (600 + t * 20, 450 + t * 30))


def test_backlash_reversal_is_bounded_per_axis_and_never_accumulates():
    calibration = calibration_for()
    settings = BacklashSettings(10, 20, 50, 25)
    forward = make_plan(calibration=calibration, backlash=settings)
    reverse = make_plan(start=forward.end_pixel, end=forward.start_pixel,
                        calibration=calibration, backlash=settings)
    assert reverse.segments[-1].start_um == forward.segments[-1].end_um
    assert reverse.segments[-1].end_um == forward.segments[-1].start_um
    for plan, sign in ((forward, 1), (reverse, -1)):
        cut_start = plan.segments[-1].start_um
        assert tuple(p - s for p, s in zip(plan.preload_um, cut_start)) == pytest.approx((5 * sign, 5 * sign))
    for _ in range(5):
        assert make_plan(calibration=calibration, backlash=settings) == forward
    assert forward.backlash == settings


@pytest.mark.parametrize("start,end,stationary", [
    ((600, 450), (620, 450), 1), ((600, 450), (600, 480), 0),
])
def test_axis_aligned_cut_never_compensates_stationary_axis(start, end, stationary):
    plan = make_plan(start=start, end=end, backlash=BacklashSettings(10, 20))
    assert plan.preload_um[stationary] == plan.segments[-1].start_um[stationary]
    assert plan.segments[-1].start_um[stationary] == plan.segments[-1].end_um[stationary]
    moving = 1 - stationary
    assert abs(plan.preload_um[moving] - plan.segments[-1].start_um[moving]) == pytest.approx((10, 20)[moving])


def test_backlash_directions_follow_stage_axes_after_rotation_and_skew():
    calibration = calibration_for(((1.5, -0.7), (0.4, 2)))
    reference = (123, 456)
    # An X-only stage path appears diagonal in the raw camera image.
    start = calibration.pixel_for_stage((125, 459), reference)
    end = calibration.pixel_for_stage((135, 459), reference)
    plan = make_plan(start=start, end=end, reference=reference,
                     calibration=calibration, backlash=BacklashSettings(4, 20))
    assert plan.segments[-1].end_um == pytest.approx((135, 459))
    assert plan.preload_um == pytest.approx((121, 459))
    # Float roundoff in the theoretically zero Y delta must not cause 20 um preload.
    assert plan.preload_um[1] == plan.segments[-1].start_um[1]


def test_dark_preload_can_project_outside_roi_and_full_frame():
    calibration = calibration_for()
    plan = make_plan(start=(20, 30), end=(40, 30), reference=(0, 0),
                     calibration=calibration, roi=ROI(20, 30, 21, 1),
                     backlash=BacklashSettings(20, 100))
    projected = calibration.pixel_for_stage(plan.preload_um, plan.reference_stage_um)
    assert projected == pytest.approx((-20, 30))
    assert not plan.roi.contains(projected)
    assert plan.preload_um in plan.path_endpoints_um
    assert all(not segment.cut for segment in plan.segments[:-1])


def test_zero_backlash_omits_zero_moves_and_cut_start_can_equal_reference():
    plan = make_plan(start=LASER, end=(520, 400), reference=(0, 0))
    assert plan.preload_um == (0, 0)
    assert len(plan.segments) == 1
    assert plan.segments[0].cut is True
    assert plan.path_endpoints_um == ((0, 0), (-10, 0))
    plan = make_plan()
    assert len(plan.segments) == 2
    assert [s.cut for s in plan.segments] == [False, True]


def test_reference_already_at_preload_does_not_add_a_zero_reposition():
    # Identity J, start laser+10 -> target=-10; cut continues negative.
    # +10 um preload returns exactly to reference (0, 0).
    plan = make_plan(start=(510, 400), end=(520, 400), reference=(0, 0),
                     calibration=calibration_for(((1, 0), (0, 1))),
                     backlash=BacklashSettings(10, 0))
    assert plan.preload_um == (0, 0)
    assert len(plan.segments) == 2
    assert plan.segments[0] == MoveSegment((0, 0), (-10, 0), False)


def test_plan_validation_rejects_recalibration_edited_fit_and_wrong_reference():
    calibration = calibration_for()
    plan = make_plan(calibration=calibration)
    plan.validate_for(calibration, CONTEXT, plan.reference_stage_um)
    restored = CuttingCalibration.from_dict(calibration.to_dict())
    plan.validate_for(restored, CONTEXT, plan.reference_stage_um)
    for changed in (calibration_for(), replace(calibration, laser_pixel=(501, 400)),
                    replace(calibration, J=((2, 0.1), (0, 3))), None):
        with pytest.raises(ValueError, match="calibration"):
            plan.validate_for(changed, CONTEXT, plan.reference_stage_um)
    with pytest.raises(ValueError, match="reference"):
        plan.validate_for(calibration, CONTEXT, (101, 200))


def test_all_dataclasses_are_frozen_and_nested_inputs_are_copied_to_tuples():
    stage, pixel = [1, 2], [3, 4]
    sample = CalibrationSample(stage, pixel)
    stage[0] = 99
    pixel[0] = 99
    assert sample.stage_um == (1, 2) and sample.pixel == (3, 4)
    calibration = calibration_for()
    matrix = [list(row) for row in calibration.J]
    copied = replace(calibration, J=matrix)
    matrix[0][0] = 99
    assert copied.J == calibration.J
    plan = make_plan(calibration=calibration)
    segment_list = list(plan.segments)
    copied_plan = replace(plan, segments=segment_list, reference_stage_um=list(plan.reference_stage_um))
    segment_list.clear()
    assert copied_plan == plan
    objects_and_fields = ((CONTEXT, "width"), (sample, "pixel"), (calibration, "revision"),
                          (FULL_ROI, "width"), (BacklashSettings(), "x_um"),
                          (plan.segments[0], "cut"), (plan, "segments"))
    for obj, field in objects_and_fields:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
        hash(obj)


def test_manually_inconsistent_plan_cannot_bypass_path_invariants():
    plan = make_plan(backlash=BacklashSettings(10, 20))
    bad_segments = (MoveSegment(plan.reference_stage_um, (999, 999), True),)
    with pytest.raises(ValueError, match="segments"):
        replace(plan, segments=bad_segments)
    with pytest.raises(ValueError, match="segments"):
        replace(plan, segments=plan.segments[1:])
    with pytest.raises(ValueError, match="segments"):
        replace(plan, segments=())
    with pytest.raises(ValueError, match="segments"):
        replace(plan, segments=[None])


BAD_NUMBERS = [math.nan, math.inf, -math.inf, True, "2", None, 1 + 2j, 10**400]


@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_numeric_abuse_is_rejected_through_public_coordinate_entrypoints(bad):
    calibration = calibration_for()
    for call in (
        lambda: CalibrationSample((bad, 0), (10, 20)),
        lambda: CalibrationSample((0, 0), (10, bad)),
        lambda: calibration.stage_target((bad, 20), (0, 0)),
        lambda: calibration.stage_target((10, 20), (bad, 0)),
        lambda: calibration.pixel_for_stage((0, bad), (0, 0)),
        lambda: calibration.pixel_for_stage((0, 0), (0, bad)),
        lambda: make_plan(start=(bad, 0)),
        lambda: make_plan(reference=(0, bad)),
        lambda: make_plan(speed=bad),
        lambda: FULL_ROI.contains((bad, 0)),
        lambda: BacklashSettings(x_um=bad),
        lambda: fit_calibration(samples_for(), LASER, CONTEXT, bad),
        lambda: MoveSegment((0, 0), (bad, 1), False),
    ):
        with pytest.raises(ValueError):
            call()


@pytest.mark.parametrize("point", [None, (), (1,), (1, 2, 3), "12", {0: 1, 1: 2}])
def test_malformed_points_do_not_silently_coerce(point):
    with pytest.raises(ValueError):
        CalibrationSample(point, (0, 0))
    with pytest.raises(ValueError):
        calibration_for().stage_target(point, (0, 0))


@pytest.mark.parametrize("changes", [
    {"camera_serial": ""}, {"objective": " "}, {"objective": None},
    {"width": 0}, {"height": -1}, {"width": 1200.0},
    {"height": math.inf}, {"binning": 0}, {"binning": True}, {"width": 10**400},
])
def test_invalid_or_unknown_context_rejects(changes):
    with pytest.raises(ValueError):
        replace(CONTEXT, **changes)


@pytest.mark.parametrize("samples", [None, [], samples_for()[:2], [None, None, None]])
def test_missing_or_malformed_calibration_samples_reject(samples):
    with pytest.raises(ValueError):
        fit_calibration(samples, LASER, CONTEXT, 1)


def test_outside_frame_landmarks_or_laser_and_unknown_context_reject():
    samples = list(samples_for())
    samples[0] = replace(samples[0], pixel=(-1, 400))
    with pytest.raises(ValueError, match="frame"):
        fit_calibration(samples, LASER, CONTEXT, 1)
    with pytest.raises(ValueError, match="frame"):
        fit_calibration(samples_for(), (1200, 400), CONTEXT, 1)
    with pytest.raises(ValueError):
        fit_calibration(samples_for(), LASER, None, 1)
    with pytest.raises(ValueError):
        fit_calibration(samples_for(), LASER, CONTEXT, -1)


@pytest.mark.parametrize("field,value", [
    ("calibration", None), ("calibration", {}), ("roi", None), ("backlash", None),
])
def test_plan_line_requires_explicit_known_objects(field, value):
    kwargs = dict(start_pixel=(600, 450), end_pixel=(620, 480), reference_stage_um=(0, 0),
                  calibration=calibration_for(), roi=FULL_ROI, backlash=BacklashSettings())
    kwargs[field] = value
    with pytest.raises(ValueError):
        plan_line(**kwargs)


@pytest.mark.parametrize("speed", [0, -0.1])
def test_nonpositive_speed_rejects(speed):
    with pytest.raises(ValueError, match="speed"):
        make_plan(speed=speed)


def test_zero_and_unrepresentable_lines_and_backlash_reject():
    with pytest.raises(ValueError, match="line"):
        make_plan(start=(600, 450), end=(600, 450))
    with pytest.raises(ValueError, match="resolvable"):
        make_plan(reference=(1e308, 1e308))
    with pytest.raises(ValueError, match="backlash"):
        make_plan(backlash=BacklashSettings(1e-300, 1e-300))


def test_huge_reference_must_not_silently_drop_just_one_axis_of_target_or_cut():
    calibration = calibration_for()
    for reference in ((1e308, 0), (0, 1e308)):
        with pytest.raises(ValueError, match="resolvable"):
            calibration.stage_target((600, 450), reference)
        with pytest.raises(ValueError, match="resolvable"):
            make_plan(reference=reference, calibration=calibration)


def test_distinct_pixels_must_not_collapse_only_one_axis_of_the_cut():
    # Each endpoint's offset from the reference is representable, but their
    # small difference is swallowed in X by the large absolute X reference.
    with pytest.raises(ValueError, match="line axis"):
        make_plan(start=(600, 450), end=(600.001, 480), reference=(1e14, 0))


def test_finite_inputs_with_overflowing_computations_fail_closed():
    calibration = replace(calibration_for(), J=((1e-308, 0), (0, 1e-308)))
    with pytest.raises(ValueError):
        calibration.stage_target((100, 100), (0, 0))
    with pytest.raises(ValueError):
        calibration.pixel_for_stage((1e308, 0), (-1e308, 0))
    with pytest.raises(ValueError):
        replace(calibration, J=((5e-324, 0), (0, 5e-324)))
    with pytest.raises(ValueError):
        MoveSegment((-1e308, 0), (1e308, 0), False)
    samples = (CalibrationSample((-1e308, 0), (100, 100)),
               CalibrationSample((1e308, 0), (200, 100)),
               CalibrationSample((0, 1), (150, 200)))
    with pytest.raises(ValueError):
        fit_calibration(samples, LASER, CONTEXT, 1)


@pytest.mark.parametrize("cut", [0, 1, "false", None])
def test_segment_cut_intent_requires_real_bool(cut):
    with pytest.raises(ValueError):
        MoveSegment((0, 0), (1, 1), cut)


def test_zero_length_segment_rejects():
    with pytest.raises(ValueError):
        MoveSegment((1, 1), (1, 1), False)
