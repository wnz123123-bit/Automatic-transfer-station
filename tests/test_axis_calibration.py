import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class AxisCalibrationTests(unittest.TestCase):
    def test_final_axis_coordinate_formulas_are_fixed_in_code(self):
        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS, coordinate_from_pulse, pulse_from_coordinate

        self.assertEqual("X1", AXIS_CALIBRATIONS[1].label)
        self.assertAlmostEqual(50.0, coordinate_from_pulse(1, 0.0))
        self.assertAlmostEqual(25.0, coordinate_from_pulse(2, 0.0))
        self.assertAlmostEqual(5.0, coordinate_from_pulse(4, 81506.0))
        self.assertAlmostEqual(0.0, coordinate_from_pulse(5, 687898.0))
        self.assertAlmostEqual(0.0, coordinate_from_pulse(6, -250000.0))
        self.assertAlmostEqual(0.0, coordinate_from_pulse(7, 131790.0))
        self.assertAlmostEqual(0.0, coordinate_from_pulse(8, -85000.0))
        self.assertAlmostEqual(180.0, coordinate_from_pulse(9, 750000.0))

        self.assertEqual(12500, pulse_from_coordinate(1, 51.0))
        self.assertEqual(625398, pulse_from_coordinate(5, 1.25))
        self.assertEqual(187500, pulse_from_coordinate(9, 45.0))

    def test_y1_and_axis3_hard_limit_configuration(self):
        from automatic_transfer_station.axis_calibration import AXIS_HARD_LIMITS

        self.assertEqual(3, AXIS_HARD_LIMITS[2].positive_io)
        self.assertEqual(2, AXIS_HARD_LIMITS[2].negative_io)
        self.assertIsNone(AXIS_HARD_LIMITS[3].positive_io)
        self.assertIsNone(AXIS_HARD_LIMITS[3].negative_io)

    def test_soft_range_and_x2_x3_interference_are_checked_in_pulse_space(self):
        from automatic_transfer_station.axis_calibration import is_target_safe

        self.assertTrue(is_target_safe(2, 312500.0, {2: 0.0}).ok)
        self.assertFalse(is_target_safe(2, 400000.0, {2: 0.0}).ok)

        safe_positions = {5: 200000.0, 7: -900000.0}
        self.assertTrue(is_target_safe(7, -900000.0, safe_positions).ok)
        self.assertFalse(is_target_safe(7, -1100000.0, safe_positions).ok)

    def test_microscope_axis_without_absolute_zero_skips_coordinate_value(self):
        from automatic_transfer_station.axis_calibration import coordinate_from_pulse

        self.assertIsNone(coordinate_from_pulse(3, 0.0))

    def test_fixed_physical_speed_options_convert_to_controller_velocity(self):
        from automatic_transfer_station.axis_calibration import (
            ANGULAR_SPEED_OPTIONS_DEG_S,
            AXIS9_TWIST_SPEED_DEG_S,
            DEFAULT_ANGULAR_SPEED_DEG_S,
            DEFAULT_LINEAR_SPEED_UM_S,
            LINEAR_SPEED_OPTIONS_UM_S,
            speed_to_pulse_per_ms,
            speed_options_for_axis,
            step_to_pulse_delta,
        )

        self.assertEqual((1.0, 10.0, 100.0, 1000.0, 5000.0), LINEAR_SPEED_OPTIONS_UM_S)
        self.assertEqual((0.1, 1.0, 10.0, 100.0, 1000.0, 5000.0), speed_options_for_axis(4))
        self.assertEqual((1.0, 10.0, 100.0, 1000.0, 5000.0), speed_options_for_axis(5))
        # MicroscopeZ (axis 3) is capped at 4000 um/s: the focus stepper stalls at
        # 5000 (confirmed on hardware), so 5000 is NOT offered. Finer high-end
        # notches (2000/3000/4000) replace the old 1000->5000 cliff. This also
        # bounds the gamepad LT/RT triggers, which scale by this per-axis setting.
        self.assertEqual(
            (1.0, 10.0, 100.0, 1000.0, 2000.0, 3000.0, 4000.0),
            speed_options_for_axis(3),
        )
        self.assertNotIn(5000.0, speed_options_for_axis(3))
        self.assertEqual((0.1, 0.3, 1.0, 2.0, 3.0), ANGULAR_SPEED_OPTIONS_DEG_S)
        self.assertEqual(0.1, AXIS9_TWIST_SPEED_DEG_S)
        self.assertEqual(1000.0, DEFAULT_LINEAR_SPEED_UM_S)
        self.assertEqual(1.0, DEFAULT_ANGULAR_SPEED_DEG_S)

        self.assertAlmostEqual(0.0125, speed_to_pulse_per_ms(1, 1.0))
        self.assertAlmostEqual(62.5, speed_to_pulse_per_ms(1, 5000.0))
        self.assertAlmostEqual(0.05, speed_to_pulse_per_ms(3, 1.0))
        self.assertAlmostEqual(0.005, speed_to_pulse_per_ms(4, 0.1))
        self.assertAlmostEqual(250.0, speed_to_pulse_per_ms(5, 5000.0))
        self.assertAlmostEqual(0.4166666667, speed_to_pulse_per_ms(9, 0.1))
        self.assertAlmostEqual(12.5, speed_to_pulse_per_ms(9, 3.0))

        self.assertEqual(13, step_to_pulse_delta(1, 1.0))
        self.assertEqual(12500, step_to_pulse_delta(1, 1000.0))
        self.assertEqual(25000, step_to_pulse_delta(3, 500.0))
        self.assertEqual(25000, step_to_pulse_delta(5, 500.0))
        self.assertEqual(4167, step_to_pulse_delta(9, 1.0))

    def test_nonzero_physical_step_never_rounds_to_zero_pulse(self):
        from automatic_transfer_station.axis_calibration import step_to_pulse_delta

        self.assertEqual(1, step_to_pulse_delta(1, 0.01))
        self.assertEqual(1, step_to_pulse_delta(9, 0.0001))

    def test_safe_continuous_target_uses_soft_range_and_interference_boundary(self):
        from automatic_transfer_station.axis_calibration import safe_continuous_target

        self.assertEqual(625000, safe_continuous_target(1, 1, {1: 0.0}))
        self.assertEqual(-625000, safe_continuous_target(1, -1, {1: 0.0}))
        self.assertEqual(1520833, safe_continuous_target(9, 1, {9: 0.0}))

        # Axis 7 moving negative would cross the X2/X3 interference line before
        # reaching its own travel end, so it must be clipped one pulse inside it.
        # Expected value updated from -1051566 to -1041566 when X2_X3_INTERFERENCE_MARGIN_PULSE
        # was raised from 0 to 10000 (10000 pulse safety gap tightens the clamped target).
        self.assertEqual(-1041566, safe_continuous_target(7, -1, {5: 200000.0, 7: -900000.0}))

    def test_safe_continuous_target_refuses_reversal_inside_interference_zone(self):
        from automatic_transfer_station.axis_calibration import safe_continuous_target

        # X2 (axis 7) resting at its documented left-limit contact position with
        # axis 5 already inside the interference zone. A requested NEGATIVE jog of
        # axis 5 must NOT come back as a positive-direction target (the old bug
        # returned 281841 while current was 100000); it must refuse instead.
        self.assertIsNone(safe_continuous_target(5, -1, {5: 100000.0, 7: -1133407.0}))
        # The recovery direction (positive, away from interference) is still allowed.
        self.assertEqual(1312898, safe_continuous_target(5, 1, {5: 100000.0, 7: -1133407.0}))

    def test_fractional_rezero_does_not_self_reject_a_jog_direction(self):
        """CP2 Fix 1: after a runtime re-zero at a fractional coordinate, the
        axis's OWN travel-end target must still be judged in-range, so neither
        jog direction is silently disabled.

        Bug: ``safe_continuous_target`` builds the travel-end target from
        ``pulse_from_coordinate(min/max_coordinate)`` (half-up rounding), then
        the final ``is_pulse_in_range`` gate maps that pulse BACK via
        ``coordinate_from_pulse`` and tests ``min <= coord <= max``. The rounded
        min endpoint can map back ~half a pulse BELOW ``min_coordinate`` (and the
        max endpoint ~half a pulse ABOVE ``max_coordinate``), so the legal
        endpoint reads out-of-range -> the whole direction is refused.

        Concrete repro (axis 7, X2, pulse_sign=+1), re-zeroed at
        ``zero_coordinate = -25.302924``:
            pulse_from_coordinate(min_coordinate=-25.30394) -> 131739
            coordinate_from_pulse(131739)                   -> -25.303944
            -25.303944 < -25.30394  -> in_range False  -> negative jog refused.
        """
        from automatic_transfer_station.axis_calibration import (
            AxisCalibration, AXIS_CALIBRATIONS, safe_continuous_target,
        )

        base7 = AXIS_CALIBRATIONS[7]
        rezeroed7 = AxisCalibration(
            7, "X2", "mm", 50000.0, base7.zero_pulse,
            zero_coordinate=-25.302924, pulse_sign=1.0,
            min_coordinate=base7.min_coordinate, max_coordinate=base7.max_coordinate,
        )
        cals = dict(AXIS_CALIBRATIONS)
        cals[7] = rezeroed7

        # Both jog directions must return a non-None bounded target: neither the
        # min nor the max endpoint may self-reject. Place axis 7 mid-travel
        # (between its min endpoint ~131740 and max endpoint ~2671936) and axis 5
        # well clear of the interference boundary so only the soft-range endpoint
        # gate is under test.
        positions = {7: 1000000.0, 5: 700000.0}
        neg = safe_continuous_target(7, -1, positions, cals)
        pos = safe_continuous_target(7, +1, positions, cals)
        self.assertIsNotNone(neg, "negative jog was self-rejected at the min endpoint")
        self.assertIsNotNone(pos, "positive jog was self-rejected at the max endpoint")

        # The constructed endpoints must be provably inside the soft range and
        # must not loosen the limit by more than ~1 pulse (half-a-mm-pulse).
        half_pulse_mm = 1.0 / rezeroed7.pulses_per_unit
        self.assertGreaterEqual(rezeroed7.coordinate_from_pulse(neg), rezeroed7.min_coordinate)
        self.assertLessEqual(rezeroed7.coordinate_from_pulse(pos), rezeroed7.max_coordinate)
        # Not loosened: the min endpoint stays within one pulse of min_coordinate.
        self.assertLessEqual(
            rezeroed7.coordinate_from_pulse(neg) - rezeroed7.min_coordinate, half_pulse_mm
        )
        self.assertLessEqual(
            rezeroed7.max_coordinate - rezeroed7.coordinate_from_pulse(pos), half_pulse_mm
        )

    def test_fractional_rezero_covers_inverted_pulse_sign(self):
        """CP2 Fix 1 sign coverage: axis 5 (X3) has pulse_sign=-1, so the pulse
        that maps to max_coordinate is the SMALLER pulse and the one that maps to
        min_coordinate is the LARGER pulse. The directional nudge must account
        for the inverted pulse<->coordinate direction.

        zc=4.2500001 self-rejects the MAX endpoint; zc=3.1234567 self-rejects the
        MIN endpoint (verified numerically), covering both endpoints under sign=-1.
        """
        from automatic_transfer_station.axis_calibration import (
            AxisCalibration, AXIS_CALIBRATIONS, safe_continuous_target,
        )

        base5 = AXIS_CALIBRATIONS[5]
        for zc in (4.2500001, 3.1234567):
            rezeroed5 = AxisCalibration(
                5, "X3", "mm", 50000.0, base5.zero_pulse,
                zero_coordinate=zc, pulse_sign=-1.0,
                min_coordinate=base5.min_coordinate, max_coordinate=base5.max_coordinate,
            )
            cals = dict(AXIS_CALIBRATIONS)
            cals[5] = rezeroed5
            # Place axis 5 mid-travel (endpoints span ~[219071, 1525398]) and
            # partner X2 well clear of the interference boundary so only the
            # soft-range endpoint gate is exercised.
            positions = {5: 800000.0, 7: 100000.0}
            neg = safe_continuous_target(5, -1, positions, cals)
            pos = safe_continuous_target(5, +1, positions, cals)
            self.assertIsNotNone(neg, f"zc={zc}: negative jog self-rejected")
            self.assertIsNotNone(pos, f"zc={zc}: positive jog self-rejected")
            # Mapped coordinates of both endpoints stay inside [min, max].
            self.assertGreaterEqual(rezeroed5.coordinate_from_pulse(neg), rezeroed5.min_coordinate)
            self.assertLessEqual(rezeroed5.coordinate_from_pulse(neg), rezeroed5.max_coordinate)
            self.assertGreaterEqual(rezeroed5.coordinate_from_pulse(pos), rezeroed5.min_coordinate)
            self.assertLessEqual(rezeroed5.coordinate_from_pulse(pos), rezeroed5.max_coordinate)

    def test_safe_continuous_target_still_rejects_genuinely_out_of_range(self):
        """CP2 Fix 1 must not loosen the limit enough to admit a target a full
        pulse (or more) outside the soft range. The exact endpoint pulse stays
        in-range; one pulse PAST it must still be rejected by is_target_safe."""
        from automatic_transfer_station.axis_calibration import (
            is_target_safe, pulse_from_coordinate, AXIS_CALIBRATIONS,
        )

        cal2 = AXIS_CALIBRATIONS[2]  # Y1: [0, 50] mm, 12500 pulses/mm, zero_coordinate 25.0
        max_pulse = pulse_from_coordinate(2, cal2.max_coordinate)
        # The exact max-travel pulse is safe...
        self.assertTrue(is_target_safe(2, float(max_pulse), {2: 0.0}).ok)
        # ...but one whole pulse beyond it (away from range) is a genuine overshoot.
        self.assertFalse(is_target_safe(2, float(max_pulse + 1), {2: 0.0}).ok)
        min_pulse = pulse_from_coordinate(2, cal2.min_coordinate)
        self.assertTrue(is_target_safe(2, float(min_pulse), {2: 0.0}).ok)
        self.assertFalse(is_target_safe(2, float(min_pulse - 1), {2: 0.0}).ok)

    def test_runtime_calibration_store_persists_coordinate_override(self):
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            store = RuntimeAxisCalibrationStore(path)
            calibrations = store.load()
            self.assertAlmostEqual(0.0, calibrations[5].coordinate_from_pulse(687898.0))

            store.calibrate_axis_at_pulse(5, current_pulse=687898.0, actual_coordinate=4.25)

            reloaded = RuntimeAxisCalibrationStore(path)
            calibrations = reloaded.load()
            self.assertAlmostEqual(4.25, calibrations[5].coordinate_from_pulse(687898.0))
            self.assertEqual(687898, calibrations[5].pulse_from_coordinate(4.25))

    def test_safe_continuous_target_applies_margin_and_partner_lookahead(self):
        from automatic_transfer_station.axis_calibration import (
            safe_continuous_target, X2_X3_INTERFERENCE_MARGIN_PULSE,
        )
        self.assertGreater(X2_X3_INTERFERENCE_MARGIN_PULSE, 0)
        # axis7 at -890000 is close enough to the boundary that lookahead=20000
        # pushes the effective partner position past the threshold while base stays
        # unclamped, so ahead (68434) > base (62898 = lower soft limit).
        positions = {5: 700000.0, 7: -890000.0}
        base = safe_continuous_target(5, -1, positions)
        ahead = safe_continuous_target(5, -1, positions, partner_lookahead_pulse=20000.0)
        self.assertIsNotNone(base)
        self.assertIsNotNone(ahead)
        # lookahead assumes the partner advances toward the boundary, so axis5 must
        # stop sooner -> a larger (less negative) clamped target.
        self.assertGreater(ahead, base)
        self.assertIsNone(
            safe_continuous_target(5, -1, {5: 100000.0, 7: -1133407.0}, partner_lookahead_pulse=20000.0)
        )

    def test_runtime_calibration_store_rejects_coordinate_outside_axis_range(self):
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            store = RuntimeAxisCalibrationStore(path)
            store.load()

            with self.assertRaises(ValueError):
                store.calibrate_axis_at_pulse(5, current_pulse=687898.0, actual_coordinate=50.0)

            self.assertFalse(path.exists())


    def test_load_rejects_override_with_out_of_range_coordinate(self):
        import json

        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            # axis 5 range is [-12.5, 12.5]; 50.0 is far outside it.
            payload = {
                "schema_version": 1,
                "axes": {
                    "5": {
                        "axis": 5,
                        "label": "X3",
                        "unit": "mm",
                        "zero_pulse": 687898.0,
                        "zero_coordinate": 50.0,
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = RuntimeAxisCalibrationStore(path)
            calibrations = store.load()

            # Bad override skipped -> axis 5 keeps the base built-in calibration.
            self.assertEqual(AXIS_CALIBRATIONS[5].zero_pulse, calibrations[5].zero_pulse)
            self.assertEqual(AXIS_CALIBRATIONS[5].zero_coordinate, calibrations[5].zero_coordinate)
            self.assertIn(5, store.skipped)

    def test_load_rejects_override_with_absurd_zero_pulse(self):
        import json

        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            # zero_coordinate is in range, but zero_pulse is wildly shifted far
            # outside any plausible band of the base zero_pulse (687898).
            payload = {
                "schema_version": 1,
                "axes": {
                    "5": {
                        "axis": 5,
                        "label": "X3",
                        "unit": "mm",
                        "zero_pulse": 999_999_999.0,
                        "zero_coordinate": 0.0,
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = RuntimeAxisCalibrationStore(path)
            calibrations = store.load()

            self.assertEqual(AXIS_CALIBRATIONS[5].zero_pulse, calibrations[5].zero_pulse)
            self.assertIn(5, store.skipped)

    def test_load_accepts_valid_override(self):
        import json

        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            payload = {
                "schema_version": 1,
                "axes": {
                    "5": {
                        "axis": 5,
                        "label": "X3",
                        "unit": "mm",
                        "zero_pulse": 687898.0,
                        "zero_coordinate": 4.25,
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = RuntimeAxisCalibrationStore(path)
            calibrations = store.load()

            self.assertAlmostEqual(4.25, calibrations[5].coordinate_from_pulse(687898.0))
            self.assertEqual([], store.skipped)

    def test_load_future_schema_version_falls_back_to_base_calibrations(self):
        import json

        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
        from automatic_transfer_station.axis_runtime_calibration_store import (
            RuntimeAxisCalibrationStore,
            SCHEMA_VERSION,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            payload = {
                "schema_version": SCHEMA_VERSION + 1,
                "axes": {
                    "5": {
                        "axis": 5,
                        "label": "X3",
                        "unit": "mm",
                        "zero_pulse": 687898.0,
                        "zero_coordinate": 4.25,
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = RuntimeAxisCalibrationStore(path)
            calibrations = store.load()

            # Future schema is not trusted -> built-in calibration for every axis.
            self.assertEqual(AXIS_CALIBRATIONS[5].zero_coordinate, calibrations[5].zero_coordinate)
            self.assertTrue(store.schema_warning)

    def test_load_cleans_up_stale_tmp_file(self):
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            stale = path.with_name(f"{path.name}.tmp")
            stale.write_text("partial garbage", encoding="utf-8")

            RuntimeAxisCalibrationStore(path).load()

            self.assertFalse(stale.exists())

    def test_axis4_rezero_at_max_travel_is_not_skipped_on_load(self):
        """Fix 1 regression: IEEE-754 rounding must not falsely reject a valid axis-4
        re-zero performed at max travel (9.95 mm).

        Axis 4 calibration:
            zero_pulse      = -168494.0
            zero_coordinate = 0.0
            pulses_per_unit = 50000.0
            pulse_sign      = 1.0
            max_coordinate  = 9.95

        The pulse that maps to coordinate 9.95 is:
            zero_pulse + (9.95 - 0.0) * 50000.0 = -168494 + 497500 = 329006.0

        The deviation |329006.0 - (-168494.0)| = 497500.0, while the raw band
        computed as travel * pulses_per_unit = 9.95 * 50000.0 is
        497499.99999999994 (IEEE-754), so without the +1.0 tolerance the valid
        calibration is silently rejected.  This test confirms it is accepted.
        """
        import json

        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        # Compute the exact zero_pulse for a re-zero at 9.95 mm, mirroring the
        # formula in AxisCalibration.pulse_from_coordinate.
        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS

        base4 = AXIS_CALIBRATIONS[4]
        # pulse = zero_pulse + (coordinate - zero_coordinate) * pulses_per_unit / pulse_sign
        max_travel_zero_pulse = base4.zero_pulse + (9.95 - base4.zero_coordinate) * base4.pulses_per_unit / base4.pulse_sign
        # Sanity: should be 329006.0
        self.assertAlmostEqual(329006.0, max_travel_zero_pulse)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            payload = {
                "schema_version": 1,
                "axes": {
                    "4": {
                        "axis": 4,
                        "label": "Z",
                        "unit": "mm",
                        "zero_pulse": max_travel_zero_pulse,
                        "zero_coordinate": 9.95,
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = RuntimeAxisCalibrationStore(path)
            calibrations = store.load()

            # The override must be applied, not skipped.
            self.assertNotIn(4, store.skipped,
                             "axis 4 re-zero at max travel was incorrectly rejected by _zero_pulse_in_band")
            self.assertAlmostEqual(9.95, calibrations[4].coordinate_from_pulse(max_travel_zero_pulse))

    def test_schema_version_zero_falls_back_to_defaults_with_warning(self):
        """Fix 3: v0 was never a real schema (there was only v1).
        A file with schema_version=0 must fall back to built-in calibrations
        and set schema_warning=True, the same as any other non-current version.
        """
        import json

        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_runtime_calibration.json"
            payload = {
                "schema_version": 0,
                "axes": {
                    "5": {
                        "axis": 5,
                        "label": "X3",
                        "unit": "mm",
                        "zero_pulse": 687898.0,
                        "zero_coordinate": 4.25,
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = RuntimeAxisCalibrationStore(path)
            calibrations = store.load()

            # schema_version=0 is not the current version → distrusted entirely.
            self.assertTrue(store.schema_warning,
                            "schema_version=0 should set schema_warning=True")
            self.assertEqual(AXIS_CALIBRATIONS[5].zero_coordinate, calibrations[5].zero_coordinate,
                             "schema_version=0 file must fall back to built-in calibration")
            self.assertEqual([], store.skipped,
                             "skipped list must be empty when schema is distrusted (whole file rejected)")

    def test_step_snap_tables_and_snap_step(self):
        from automatic_transfer_station.axis_calibration import STEP_SNAP, snap_step
        # 1,2 (X1/Y1) added with the 2026-06-22 gamepad selector remap.
        for axis in (1, 2, 5, 6, 7, 8, 9, 3, 4):
            self.assertIn(axis, STEP_SNAP)
            self.assertEqual(sorted(STEP_SNAP[axis]), list(STEP_SNAP[axis]))  # ascending
        snaps = STEP_SNAP[5]
        self.assertEqual(snap_step(5, snaps[0], -1), snaps[0])            # clamp low
        self.assertEqual(snap_step(5, snaps[-1], +1), snaps[-1])          # clamp high
        self.assertEqual(snap_step(5, snaps[0], +1), snaps[1])            # up one notch
        mid = (snaps[1] + snaps[2]) / 2.0
        self.assertEqual(snap_step(5, mid + 0.01, +1), snaps[2])          # snap to nearest then step

    def test_every_selectable_axis_has_step_snap_and_position(self):
        # Invariant that prevents the runtime KeyError: every gamepad-selectable axis
        # must have a STEP_SNAP entry (else _gamepad_set_step crashes) AND a stage-map
        # position (else the 2D directional selector can't place it).
        from automatic_transfer_station.axis_calibration import (
            STEP_SNAP, STAGE_MAP_AXIS_POSITIONS, STAGE_MAP_AXIS_LABEL_POSITIONS,
            SELECTOR_GRID_POSITIONS,
        )
        from automatic_transfer_station.gamepad_controller import SELECTOR_AXES
        for axis in SELECTOR_AXES:
            self.assertIn(axis, STEP_SNAP, f"axis {axis} missing STEP_SNAP -> snap_step KeyError")
            self.assertIn(axis, STAGE_MAP_AXIS_POSITIONS, f"axis {axis} missing stage-map position")
            self.assertIn(axis, SELECTOR_GRID_POSITIONS, f"axis {axis} missing selector grid position")
        # All nine axes have a stage-map tip AND a label position (the arrow needs
        # both); MicroscopeZ(3) is present but not selectable.
        self.assertEqual(set(range(1, 10)), set(STAGE_MAP_AXIS_POSITIONS))
        self.assertEqual(set(range(1, 10)), set(STAGE_MAP_AXIS_LABEL_POSITIONS))
        # The selector grid covers EXACTLY the selectable axes (no MicroscopeZ).
        self.assertEqual(set(SELECTOR_AXES), set(SELECTOR_GRID_POSITIONS))
        self.assertNotIn(3, SELECTOR_GRID_POSITIONS)
        self.assertNotIn(3, SELECTOR_AXES)

    def test_y3_y2_direction_reversed_pulse_sign_and_preserved_envelope(self):
        # 2026-06-22 real-hardware fix: Y3(6) and Y2(8) ran opposite to the software's
        # +pulse assumption, so pulse_sign was flipped to -1. This reverses the jog
        # direction AND the coordinate readout together.
        from automatic_transfer_station.axis_calibration import (
            AXIS_CALIBRATIONS, coordinate_from_pulse,
        )
        self.assertEqual(-1.0, AXIS_CALIBRATIONS[6].pulse_sign)
        self.assertEqual(-1.0, AXIS_CALIBRATIONS[8].pulse_sign)
        # Coordinate now INCREASES as the pulse DECREASES (direction flipped).
        self.assertLess(coordinate_from_pulse(6, 0.0), coordinate_from_pulse(6, -50000.0))
        self.assertLess(coordinate_from_pulse(8, 0.0), coordinate_from_pulse(8, -50000.0))
        # Y2 soft limits were swapped+negated so the PHYSICAL pulse envelope is unchanged.
        cal8 = AXIS_CALIBRATIONS[8]
        self.assertAlmostEqual(-25.280, cal8.min_coordinate)
        self.assertAlmostEqual(25.099, cal8.max_coordinate)
        # Pin the EXACT envelope: the new bounds map back to the SAME physical limit pulses
        # the old +1 calibration had (-1339950 / 1179000). Round-trip asserts catch the
        # "flipped sign but kept old limits" and "kept sign with new limits" regressions
        # that slack in-range probes would miss.
        self.assertEqual(1179000, cal8.pulse_from_coordinate(cal8.min_coordinate))
        self.assertEqual(-1339950, cal8.pulse_from_coordinate(cal8.max_coordinate))
        # Exact-boundary in-range: both physical endpoints inclusive, one pulse beyond out.
        self.assertTrue(cal8.is_pulse_in_range(-1339950))
        self.assertTrue(cal8.is_pulse_in_range(1179000))
        self.assertFalse(cal8.is_pulse_in_range(-1339951))
        self.assertFalse(cal8.is_pulse_in_range(1179001))

    # ---- Package 7 Test 1: unsafe_axes_for_positions unit test ----

    def test_unsafe_axes_for_positions_returns_both_x2_x3_when_interference_boundary_violated(self):
        """X2(7)+X3(5) violation must return {5,7}; safe pair returns empty."""
        from automatic_transfer_station.axis_calibration import (
            unsafe_axes_for_positions,
            X2_X3_INTERFERENCE_BOUNDARY_PULSE,
            X2_X3_INTERFERENCE_MARGIN_PULSE,
        )

        # Threshold: axis5 + axis7 must be > boundary + margin to be safe.
        threshold = X2_X3_INTERFERENCE_BOUNDARY_PULSE + X2_X3_INTERFERENCE_MARGIN_PULSE

        # Positions that VIOLATE the interference boundary: sum <= threshold.
        # Use positions well inside each axis's own soft range so soft-limit
        # checks don't add extra axes.
        # axis7 (X2): safe_range min_coordinate=-25.30394, zero_pulse=131790
        # at 0 mm -> pulse 131790; axis5 (X3): at 0 mm -> pulse 687898.
        # sum = 131790 + 687898 = 819688, which is > threshold (-841567), so safe.
        # We need a pair whose sum <= threshold (-841567):
        # e.g. axis7=-1000000, axis5=100000 -> sum=-900000 < -841567 -> unsafe.
        unsafe_pos = {5: 100000.0, 7: -1000000.0}
        check = unsafe_axes_for_positions(unsafe_pos)
        self.assertFalse(check.ok, "sum below interference threshold should be unsafe")
        self.assertIn(5, check.axes)
        self.assertIn(7, check.axes)
        self.assertEqual(frozenset(check.axes), frozenset([5, 7]))

        # Positions that are SAFE: sum > threshold.
        # axis7=131790 (X2 at 0mm), axis5=687898 (X3 at 0mm) -> sum=819688 >> threshold
        safe_pos = {5: 687898.0, 7: 131790.0}
        safe_check = unsafe_axes_for_positions(safe_pos)
        self.assertTrue(safe_check.ok, "sum well above interference threshold should be safe")
        self.assertEqual((), safe_check.axes)

    def test_unsafe_axes_for_positions_empty_dict_returns_safe(self):
        from automatic_transfer_station.axis_calibration import unsafe_axes_for_positions
        check = unsafe_axes_for_positions({})
        self.assertTrue(check.ok)
        self.assertEqual((), check.axes)

    def test_unsafe_axes_for_positions_x2_x3_partial_snapshot_is_unsafe(self):
        """CP5 contract change: when exactly ONE of X2(7)/X3(5) is in the snapshot
        the interference check CANNOT be completed, so the pair is treated as UNSAFE
        (fail-safe) rather than silently skipped.

        Previously (before CP5) a missing partner was silently treated as safe,
        which was a latent foot-gun: a partial position snapshot would let a move that
        might cross the interference boundary pass undetected.  The new contract is:
        - Both axes absent → interference not relevant, no error.
        - Both axes present → normal boundary check (unchanged).
        - Exactly ONE present → refuse (fail-safe: cannot evaluate interference).

        Production callers (_validate_target / _current_positions_for_safety) always
        supply both partner positions for axes 5 and 7, so they are unaffected.
        """
        from automatic_transfer_station.axis_calibration import unsafe_axes_for_positions

        # Both axes absent → no interference check, not relevant, safe.
        check_neither = unsafe_axes_for_positions({1: 0.0, 2: 0.0})
        self.assertTrue(check_neither.ok)

        # Exactly axis5 present, axis7 absent → fail-safe: treated as UNSAFE.
        check_only5 = unsafe_axes_for_positions({5: 687898.0})  # X3 at 0mm
        self.assertFalse(check_only5.ok,
                         "partial snapshot (only axis5) must be treated as unsafe (CP5 fail-safe)")
        self.assertIn(5, check_only5.axes)
        self.assertIn(7, check_only5.axes)

        # Exactly axis7 present, axis5 absent → fail-safe: treated as UNSAFE.
        check_only7 = unsafe_axes_for_positions({7: -1000000.0})
        self.assertFalse(check_only7.ok,
                         "partial snapshot (only axis7) must be treated as unsafe (CP5 fail-safe)")
        self.assertIn(5, check_only7.axes)
        self.assertIn(7, check_only7.axes)

    def test_is_target_safe_axis7_without_partner_axis5_returns_not_ok(self):
        """A1 (CP5): is_target_safe for an interference-pair axis (7) when the partner
        (5) is NOT in current_positions must return NOT ok rather than silently
        treating the move as safe.

        This is the explicit contract test requested in the CP5 spec:
            is_target_safe(7, <target>, {7: ...}) → not ok.
        """
        from automatic_transfer_station.axis_calibration import is_target_safe, AXIS_CALIBRATIONS

        # Choose a target for axis7 that is inside its soft range (so the
        # soft-limit gate does not fire first; we want to reach the interference check).
        # Axis7 zero_pulse ~ -1133407; target near 0 → coordinate ≈ -22.7 mm, inside range.
        target_pulse = -1130000.0
        cal7 = AXIS_CALIBRATIONS[7]
        self.assertTrue(cal7.is_pulse_in_range(target_pulse),
                        "pre-condition: target must be inside soft range so only the partner check fires")

        # Supply {7: target} — partner axis 5 intentionally absent.
        result = is_target_safe(7, target_pulse, {7: target_pulse})
        self.assertFalse(result.ok,
                         "is_target_safe for axis7 without partner axis5 must return NOT ok (CP5 fail-safe)")
        self.assertIn(5, result.axes)
        self.assertIn(7, result.axes)
        self.assertIn("干涉伙伴位置不可用", result.message)

    def test_is_target_safe_axis5_without_partner_axis7_returns_not_ok(self):
        """A1 (CP5): symmetric test for axis5 (X3) without partner axis7 (X2)."""
        from automatic_transfer_station.axis_calibration import is_target_safe, AXIS_CALIBRATIONS

        # Axis5 zero_pulse = 687898; target near zero → coordinate ≈ 0 mm, inside range.
        target_pulse = 687898.0
        cal5 = AXIS_CALIBRATIONS[5]
        self.assertTrue(cal5.is_pulse_in_range(target_pulse),
                        "pre-condition: target must be inside soft range")

        result = is_target_safe(5, target_pulse, {5: target_pulse})
        self.assertFalse(result.ok,
                         "is_target_safe for axis5 without partner axis7 must return NOT ok (CP5 fail-safe)")
        self.assertIn(5, result.axes)
        self.assertIn(7, result.axes)

    # ---- B2 (CP5): safe_continuous_target missing own-axis position ----

    def test_b2_safe_continuous_target_own_position_missing_with_clamp_returns_none(self):
        """B2: when the moving axis's OWN current position is absent from
        current_positions AND the interference clamp fires, safe_continuous_target
        must return None (refuse) rather than returning a potentially wrong target.

        This is a direct unit test for the public safety primitive. The branch is:
            current = positions.get(axis)  → None
            if clamped: return None

        Scenario (axis5/X3 going in direction -1 toward the interference boundary):
        - axis5's soft range in pulse space: lower ≈ 62898, upper ≈ 1312898
          (inverted coordinates — lower pulse = larger mm coordinate).
        - Direction -1 → target = lower ≈ 62898.
        - Clamp fires when: target + (axis7 - lookahead) ≤ threshold (-841567).
          With axis7 = -950000: 62898 + (-950000) = -887102 ≤ -841567 ✓
        - axis5's own position is NOT in current_positions → clamped + missing own
          → must return None.
        """
        from automatic_transfer_station.axis_calibration import (
            safe_continuous_target,
            AXIS_CALIBRATIONS,
            X2_X3_INTERFERENCE_BOUNDARY_PULSE,
            X2_X3_INTERFERENCE_MARGIN_PULSE,
        )

        threshold = X2_X3_INTERFERENCE_BOUNDARY_PULSE + X2_X3_INTERFERENCE_MARGIN_PULSE
        cal5 = AXIS_CALIBRATIONS[5]
        lower = min(
            cal5.soft_limit_endpoint_pulse(cal5.min_coordinate),
            cal5.soft_limit_endpoint_pulse(cal5.max_coordinate),
        )
        axis7_val = -950000.0

        # Verify the pre-condition: the clamp actually fires in this scenario.
        self.assertLessEqual(
            lower + axis7_val, threshold,
            "pre-condition: clamp must fire for this scenario to be meaningful"
        )

        # positions dict: axis7 present, axis5 (the moving axis) ABSENT.
        positions = {7: axis7_val}

        result = safe_continuous_target(
            axis=5, direction=-1, current_positions=positions, partner_lookahead_pulse=0.0
        )
        # The clamp fires and own position (axis5) is absent → must return None.
        self.assertIsNone(
            result,
            "safe_continuous_target must return None when own position is missing and clamp fired"
        )


if __name__ == "__main__":
    unittest.main()
