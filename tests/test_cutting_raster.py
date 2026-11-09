"""Pure planning/serialization tests; no Qt, serial, motion or optical actions."""

from dataclasses import FrozenInstanceError, replace
import json
import math
import random

import pytest

from automatic_transfer_station.cutting_geometry import (
    BacklashSettings, CalibrationContext, CuttingCalibration, ROI,
)
from automatic_transfer_station import cutting_raster as raster
from automatic_transfer_station.cutting_raster import (
    DotExposure, RasterLine, RasterMove, RasterPlan, RasterRecipe, RasterSettings, plan_raster,
)


CONTEXT = CalibrationContext("raster-camera", "20X", 1000, 800, 1)
CAL = CuttingCalibration(((-1, 0), (0, -1)), (200, 200), (100, 100), CONTEXT,
                         0, 0, 0.01, 4, "raster-fit")
FULL = ROI(0, 0, 1000, 800)
RECT = ((100, 100), (140, 100), (140, 130), (100, 130))
CONCAVE = ((100, 100), (160, 100), (160, 160), (140, 160),
           (140, 120), (120, 120), (120, 160), (100, 160))


def plan(polygon=RECT, *, reference=(0, 0), calibration=CAL, roi=FULL,
         backlash=BacklashSettings(), settings=RasterSettings()):
    return plan_raster(polygon, reference, calibration, roi, backlash, settings)


def dots(**kwargs):
    return RasterSettings(**dict(mode="dots", row_pitch_um=10, point_pitch_um=10,
                                 dwell_ms=1, **kwargs))


def check_path(p):
    current = p.reference_stage_um
    ends = [current]
    for action in p.actions:
        if isinstance(action, RasterMove):
            assert action.start_um == current
            assert action.start_um != action.end_um
            current = action.end_um
            ends.append(current)
        else:
            assert isinstance(action, DotExposure) and action.point_um == current
    assert p.path_endpoints_um == tuple(ends)


def test_lines_metadata_chain_and_nominal_time_include_dark_returns():
    p = plan()
    assert p.sample_axes == (5, 6) and p.context == CONTEXT
    assert p.calibration_revision == CAL.revision
    assert p.candidate_row_count == p.exposure_count == len(p.lines) == 3
    for i, line in enumerate(p.lines):
        assert line.row_index == i and line.span_index == 0
        assert line.start_um == (0, i * 10) and line.end_um == (40, i * 10)
        assert p.actions[line.action_index] == RasterMove(line.start_um, line.end_um, True)
    assert p.estimated_duration_s == pytest.approx((120 + 2 * math.hypot(40, 10)) / 100)
    check_path(p)
    p.verify_integrity()
    p.validate_for(CAL, CONTEXT, (0, 0))


def test_concave_spans_get_independent_preload_and_never_cut_across_notch():
    p = plan(CONCAVE, backlash=BacklashSettings(8, 99, 25, 100))
    # A +X scan preloads 8*25%=2 um only on X; there is no Y compensation.
    for line in p.lines:
        approach = p.actions[line.action_index - 1]
        assert approach == RasterMove((line.start_um[0] - 2, line.start_um[1]), line.start_um, False)
        if line.start_um[1] >= 20:
            assert line.end_um[0] <= 20 or line.start_um[0] >= 40
    assert any(not a.cut and a.end_um[0] < a.start_um[0] for a in p.actions if isinstance(a, RasterMove))
    assert min(x for x, y in p.path_endpoints_um) == -2  # Controller must preflight dark preload too.
    check_path(p)


@pytest.mark.parametrize("angle,delta", [(0, (2, 0)), (90, (0, 3)), (180, (-2, 0)), (270, (0, -3))])
def test_preload_reversal_stationary_axis_and_no_cumulative_offsets(angle, delta):
    p = plan(backlash=BacklashSettings(8, 6, 25, 50), settings=RasterSettings(angle_deg=angle))
    for line in p.lines:
        preload = p.actions[line.action_index - 1]
        assert not preload.cut and preload.end_um == line.start_um
        assert tuple(b - a for a, b in zip(preload.start_um, preload.end_um)) == delta
    # Compensation changes only dark approach, never the exposure geometry.
    uncompensated = plan(settings=p.settings)
    assert [(l.start_um, l.end_um) for l in p.lines] == [(l.start_um, l.end_um) for l in uncompensated.lines]


def test_dots_global_phase_across_rows_and_concave_gaps_settle_is_separate():
    settings = RasterSettings("dots", 10, 15, 0, 100, 50, 10)
    p = plan(CONCAVE, backlash=BacklashSettings(2, 30), settings=settings)
    assert p.lines == ()
    by_row = {}
    for a in p.actions:
        if isinstance(a, RasterMove):
            assert a.cut is False
        else:
            x, y = a.point_um
            assert x % 15 == 0 and y % 10 == 0
            assert (a.settle_ms, a.dwell_ms) == (50, 10)
            by_row.setdefault(y, []).append(x)
    assert by_row[30] == [0, 15, 45]  # Second span begins at 40, but phase does not restart there.
    for row, xs in by_row.items():
        index = next(i for i, a in enumerate(p.actions) if isinstance(a, DotExposure) and a.point_um == (xs[0], row))
        assert p.actions[index - 1] == RasterMove((xs[0] - 2, row), (xs[0], row), False)
    assert p.exposure_count == sum(map(len, by_row.values()))
    travel = sum(math.dist(a.start_um, a.end_um) / 100 for a in p.actions if isinstance(a, RasterMove))
    assert p.estimated_duration_s == pytest.approx(travel + p.exposure_count * .06)
    check_path(p)


def test_fixed_grid_is_relative_to_snapshot_not_each_polygon_minimum():
    polygon = ((103, 103), (137, 103), (137, 127), (103, 127))
    p = plan(polygon, reference=(500, -200), settings=dots())
    points = [a.point_um for a in p.actions if isinstance(a, DotExposure)]
    assert points == [(510, -190), (520, -190), (530, -190), (510, -180), (520, -180), (530, -180)]


@pytest.mark.parametrize("angle", [1e-8, 1e-12])
def test_near_axis_inset_does_not_require_the_minor_stage_axis_to_change(angle):
    # At a 20000 um reference, x=0.3 rounds just outside this contour. One
    # representable X step is enough. Requiring a Y step too formerly removed
    # 0.020844 um at EACH end (1e-8 degrees), or rejected the span (1e-12).
    polygon = ((100.3, 100), (100.4, 100), (100.4, 110), (100.3, 110))
    p = plan(polygon, reference=(20000, 20000), roi=ROI(100, 100, 1, 10),
             settings=RasterSettings(row_pitch_um=1, angle_deg=angle))
    assert len(p.lines) == 10
    for line in p.lines:
        start = CAL.pixel_for_stage(line.start_um, p.reference_stage_um)
        end = CAL.pixel_for_stage(line.end_um, p.reference_stage_um)
        assert start[0] == pytest.approx(100.3, abs=1e-9, rel=0)
        assert end[0] == pytest.approx(100.4, abs=1e-9, rel=0)
        for t in range(11):
            point = tuple(x + (y - x) * t / 10 for x, y in zip(line.start_um, line.end_um))
            pixel = CAL.pixel_for_stage(point, p.reference_stage_um)
            assert p.roi.contains(pixel) and 100.3 <= pixel[0] <= 100.4


@pytest.mark.parametrize("mode", ["lines", "dots"])
def test_roi_clipping_half_open_and_beam_center_only(mode):
    roi = ROI(110, 105, 20, 20)
    p = plan(settings=RasterSettings() if mode == "lines" else dots(), roi=roi)
    for a in p.actions:
        if isinstance(a, DotExposure):
            points = [a.point_um]
        elif a.cut:
            points = [tuple(x + (y - x) * t / 20 for x, y in zip(a.start_um, a.end_um)) for t in range(21)]
        else:
            continue
        assert all(roi.contains(CAL.pixel_for_stage(point, p.reference_stage_um)) for point in points)
    assert "footprint" in RasterRecipe.__doc__


def test_full_frame_closed_contour_vertices_never_expose_right_or_bottom():
    p = plan(((0, 0), (1000, 0), (1000, 800), (0, 800)),
             settings=RasterSettings("dots", 100, 100, dwell_ms=1))
    pixels = [CAL.pixel_for_stage(a.point_um, (0, 0)) for a in p.actions if isinstance(a, DotExposure)]
    assert len(pixels) == 80
    assert all(0 <= x < 1000 and 0 <= y < 800 for x, y in pixels)


@pytest.mark.parametrize("i", [2, 3, 7, 39])
def test_rotated_skew_reflected_concave_regression_and_upper_edge_interpolation(i):
    # Reduced cases from the independent seed-20261108 oracle. These exposed
    # near-zero intersections and interpolated centers just outside x=150/y=650.
    rng = random.Random(20261108)
    for _ in range(i + 1):
        a = rng.uniform(-math.pi, math.pi)
        sx = rng.choice((-1, 1)) * rng.uniform(.7, 3)
        sy, h = rng.uniform(.7, 3), rng.uniform(-.35, .35)
        j = ((math.cos(a) * sx, math.cos(a) * h - math.sin(a) * sy),
             (math.sin(a) * sx, math.sin(a) * h + math.cos(a) * sy))
        angle = rng.uniform(-180, 180)
        dot_angle = rng.uniform(-180, 180)
    cal = CuttingCalibration(j, (0, 0), (500, 400), CONTEXT, 0, 0, 1, 3, str(i))
    polygon = ((100, 100), (800, 100), (800, 300), (500, 300),
               (500, 600), (800, 600), (800, 700), (100, 700))
    roi, reference = ROI(150, 150, 600, 500), (1000, -500)
    for mode, direction in (("lines", angle), ("dots", dot_angle)):
        settings = RasterSettings(mode, 20, 20 if mode == "dots" else None, direction, 1000, 0,
                                  1 if mode == "dots" else 0)
        p = plan(polygon, reference=reference, calibration=cal, roi=roi,
                 backlash=BacklashSettings(2, 3), settings=settings)
        u = (math.cos(math.radians(direction)), math.sin(math.radians(direction)))
        for action in p.actions:
            if isinstance(action, DotExposure):
                points = [action.point_um]
                offset = tuple(x - y for x, y in zip(action.point_um, reference))
                assert sum(x * y for x, y in zip(offset, u)) / 20 == pytest.approx(round(sum(x * y for x, y in zip(offset, u)) / 20))
            elif action.cut:
                delta = tuple(y - x for x, y in zip(action.start_um, action.end_um))
                assert sum(x * y for x, y in zip(delta, u)) > 0
                assert delta[0] * u[1] - delta[1] * u[0] == pytest.approx(0, abs=1e-10)
                points = [tuple(x + (y - x) * t / 10 for x, y in zip(action.start_um, action.end_um)) for t in range(11)]
            else:
                continue
            for point in points:
                x, y = cal.pixel_for_stage(point, reference)
                assert roi.contains((x, y))
                assert not (x > 500 + 1e-8 and 300 + 1e-8 < y < 600 - 1e-8)
        check_path(p)


@pytest.mark.parametrize("polygon", [
    (), ((100, 100), (140, 100)),
    ((100, 100), (140, 130), (100, 130), (140, 100)),  # crossing
    ((100, 100), (140, 100), (120, 100), (120, 120)),  # backtrack
    ((100, 100), (140, 100), (140, 130), (120, 100), (100, 130)),  # self-touch
    ((100, 100), (110, 110), (120, 120)),
    ((100, 100), (140, 100), (140, 130), (100, 100), (100, 130)),
    ((-1, 0), (10, 0), (10, 10)),
    ((100, 100), (math.nan, 120), (140, 130)),
    ((100, 100), (True, 120), (140, 130)),
])
def test_invalid_polygon_is_rejected(polygon):
    with pytest.raises(ValueError):
        plan(polygon)


def test_polygon_normalization_winding_collinear_vertices_and_128_bound():
    base = plan()
    assert plan(tuple(reversed(RECT))).actions == base.actions
    assert plan(RECT + RECT[:1]).polygon_pixels == RECT
    assert plan(RECT[:1] + ((120, 100),) + RECT[1:]).actions == base.actions
    vertices = tuple((300 + 30 * math.cos(i * 2 * math.pi / 128), 300 + 30 * math.sin(i * 2 * math.pi / 128)) for i in range(128))
    assert len(RasterRecipe(CONTEXT, FULL, vertices, RasterSettings(), BacklashSettings()).polygon_pixels) == 128
    with pytest.raises(ValueError):
        plan(vertices + ((350, 350),))


@pytest.mark.parametrize("changes", [
    dict(mode="pulse"), dict(mode=True), dict(row_pitch_um=0), dict(row_pitch_um=-1),
    dict(row_pitch_um="10"), dict(row_pitch_um=True), dict(row_pitch_um=math.inf),
    dict(speed_um_s=math.nan), dict(speed_um_s=0), dict(angle_deg=math.inf),
    dict(point_pitch_um=1), dict(settle_ms=1), dict(dwell_ms=1),
    dict(mode="dots", point_pitch_um=1, dwell_ms=0),
    dict(mode="dots", point_pitch_um=0, dwell_ms=1),
    dict(mode="dots", point_pitch_um=1, dwell_ms=60001),
    dict(mode="dots", point_pitch_um=1, dwell_ms=True),
    dict(mode="dots", point_pitch_um=1, dwell_ms=1, settle_ms=1.0),
])
def test_settings_numeric_abuse_and_mode_separation(changes):
    with pytest.raises(ValueError):
        RasterSettings(**changes)


def test_canonical_angles_and_strict_settings_dict():
    s = RasterSettings(angle_deg=-30)
    assert s.angle_deg == 330 and RasterSettings.from_dict(s.to_dict()) == s
    with pytest.raises(ValueError):
        RasterSettings.from_dict({**s.to_dict(), "prf": 100})


@pytest.mark.parametrize("kw", [dict(calibration=None), dict(roi=ROI(900, 0, 200, 200)),
                               dict(reference=(math.inf, 0)), dict(reference=(1e100, 1e100))])
def test_unknown_out_of_frame_and_unresolvable_reference_rejected(kw):
    with pytest.raises(ValueError):
        plan(**kw)


def test_real_sub_ulp_move_preload_and_empty_intersections_are_rejected():
    with pytest.raises(ValueError):
        plan(reference=(1000, 0), backlash=BacklashSettings(1e-100, 0))
    tiny = replace(CAL, J=((-1e100, 0), (0, -1e100)))
    with pytest.raises(ValueError):
        plan(calibration=tiny, reference=(1000, -1000), settings=RasterSettings(row_pitch_um=1e-99))
    with pytest.raises(ValueError):
        plan(roi=ROI(700, 600, 100, 100))
    with pytest.raises(ValueError):
        plan(((101, 101), (102, 101), (102, 102), (101, 102)), settings=dots())


@pytest.mark.parametrize("settings", [RasterSettings(row_pitch_um=1e-30),
                                    RasterSettings("dots", 10, 1e-30, dwell_ms=1),
                                    RasterSettings(speed_um_s=1e-10)])
def test_candidate_and_duration_limits_reject_before_unbounded_work(settings):
    with pytest.raises(ValueError):
        plan(settings=settings)


def test_resource_boundary_equality_and_cumulative_exposures_actions_time(monkeypatch):
    assert (raster.MAX_EXPOSURES, raster.MAX_ACTIONS, raster.MAX_CANDIDATE_ROWS,
            raster.MAX_ESTIMATED_DURATION_S) == (10000, 30000, 10000, 3600)
    baseline = plan(settings=dots())
    monkeypatch.setattr(raster, "MAX_EXPOSURES", baseline.exposure_count)
    monkeypatch.setattr(raster, "MAX_ACTIONS", len(baseline.actions))
    monkeypatch.setattr(raster, "MAX_ESTIMATED_DURATION_S", baseline.estimated_duration_s)
    assert plan(settings=dots()) == baseline
    monkeypatch.setattr(raster, "MAX_EXPOSURES", baseline.exposure_count - 1)
    with pytest.raises(ValueError, match="exposures"):
        plan(settings=dots())
    monkeypatch.setattr(raster, "MAX_EXPOSURES", baseline.exposure_count)
    monkeypatch.setattr(raster, "MAX_ACTIONS", len(baseline.actions) - 1)
    with pytest.raises(ValueError, match="actions"):
        plan(settings=dots())
    monkeypatch.setattr(raster, "MAX_ACTIONS", len(baseline.actions))
    monkeypatch.setattr(raster, "MAX_ESTIMATED_DURATION_S", baseline.estimated_duration_s - .001)
    with pytest.raises(ValueError, match="duration"):
        plan(settings=dots())


def test_frozen_plan_copies_input_and_rebuild_rejects_tampering():
    polygon = [list(p) for p in RECT]
    p = plan(polygon)
    polygon[0][0] = 777
    assert p.polygon_pixels == RECT
    with pytest.raises(FrozenInstanceError):
        p.settings.speed_um_s = 1
    for changes in (dict(actions=p.actions[:-1]), dict(lines=()), dict(estimated_duration_s=1),
                    dict(candidate_row_count=4), dict(reference_stage_um=(1, 0)),
                    dict(settings=RasterSettings(speed_um_s=50))):
        with pytest.raises(ValueError):
            replace(p, **changes)
    # Even bypassing Python's frozen guard cannot pass full provenance rebuild.
    object.__setattr__(p, "actions", p.actions[:-1])
    with pytest.raises(ValueError):
        p.verify_integrity()


def test_validate_for_requires_exact_calibration_context_and_snapshot_reference():
    p = plan()
    for cal, ctx, ref in ((replace(CAL, revision="new-fit"), CONTEXT, (0, 0)),
                          (CAL, replace(CONTEXT, binning=2), (0, 0)),
                          (CAL, CONTEXT, (.01, 0)), (None, CONTEXT, (0, 0))):
        with pytest.raises(ValueError):
            p.validate_for(cal, ctx, ref)


def recipe():
    return RasterRecipe(CONTEXT, FULL, RECT, dots(), BacklashSettings(3, 4, 25, 50))


def test_recipe_json_roundtrip_copies_data_has_no_job_or_qualification():
    r = recipe()
    assert RasterRecipe.from_json(r.to_json()) == r
    data = r.to_dict()
    assert set(data) == {"schema_version", "coordinate_space", "sample_axes", "context", "roi", "polygon_pixels", "settings", "backlash"}
    data["polygon_pixels"][0][0] = 999
    assert r.polygon_pixels == RECT


@pytest.mark.parametrize("key,value", [("schema_version", True), ("schema_version", 2),
                                      ("sample_axes", [0, 1]), ("sample_axes", [5., 6.]),
                                      ("coordinate_space", "flipped_preview"), ("qualification", {}),
                                      ("ARM", True), ("actions", []), ("reference_stage_um", [0, 0])])
def test_recipe_rejects_wrong_provenance_unknown_keys_and_authorization(key, value):
    data = recipe().to_dict()
    data[key] = value
    with pytest.raises(ValueError):
        RasterRecipe.from_dict(data)


@pytest.mark.parametrize("section", ["context", "roi", "settings", "backlash"])
def test_recipe_nested_records_are_strict(section):
    data = recipe().to_dict()
    data[section]["unknown"] = 1
    with pytest.raises(ValueError):
        RasterRecipe.from_dict(data)


@pytest.mark.parametrize("text", ['{"schema_version":1,"schema_version":1}',
                                  '{"x":NaN}', '{"x":Infinity}', 'null', '[]',
                                  '[' * 2000 + ']' * 2000, '\ud800', b'{}'])
def test_json_rejects_duplicate_nonfinite_wrong_type_and_depth(text):
    with pytest.raises(ValueError):
        RasterRecipe.from_json(text)


def test_recipe_json_one_mib_utf8_bound():
    text = recipe().to_json()
    padding = raster.MAX_RECIPE_JSON_BYTES - len(text.encode("utf-8"))
    assert RasterRecipe.from_json(text + ' ' * padding) == recipe()
    with pytest.raises(ValueError):
        RasterRecipe.from_json(text + ' ' * (padding + 1))
    with pytest.raises(ValueError):
        RasterRecipe.from_json('"' + '\u4e2d' * 400000 + '"')


@pytest.mark.parametrize("factory", [lambda: RasterMove((0, 0), (0, 0), False),
                                     lambda: RasterMove((0, 0), (1, 0), 1),
                                     lambda: DotExposure((0, math.inf), 0, 1),
                                     lambda: DotExposure((0, 0), 0, 0),
                                     lambda: RasterLine(0, 0, (0, 0), (1, 0), -1)])
def test_action_records_validate_independently(factory):
    with pytest.raises(ValueError):
        factory()
