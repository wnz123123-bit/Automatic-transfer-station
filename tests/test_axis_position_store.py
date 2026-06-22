import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class AxisPositionStoreTests(unittest.TestCase):
    def test_update_axis_persists_position_metadata(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path)

            store.update_axis(7, -1133407, source="test")

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(1, data["schema_version"])
            self.assertEqual("X2", data["axes"]["7"]["label"])
            self.assertEqual(-1133407.0, data["axes"]["7"]["pulse"])
            self.assertAlmostEqual(-25.30394, data["axes"]["7"]["coordinate"])
            self.assertEqual("mm", data["axes"]["7"]["unit"])
            self.assertEqual("test", data["axes"]["7"]["source"])

    def test_axis9_angle_matches_calibrated_coordinate_under_runtime_override(self):
        from dataclasses import replace

        from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        # Runtime R calibration: pulse 1521782 maps to 360 deg (non-zero zero_pulse).
        calibrations = dict(AXIS_CALIBRATIONS)
        calibrations[9] = replace(calibrations[9], zero_pulse=1521782.0, zero_coordinate=360.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path, axis_calibrations=calibrations)

            store.update_axes({9: 1521782.0}, source="controller_readback")

            record = store.axes["9"]
            # angle_degrees must agree with the calibrated coordinate (360.0),
            # not the raw pulse*deg constant (~365.23) which ignores the zero.
            self.assertAlmostEqual(360.0, record["angle_degrees"])
            self.assertAlmostEqual(360.0, record["coordinate"])
            self.assertAlmostEqual(360.0, store.cached_axis9_angle())

    def test_load_missing_or_invalid_file_returns_empty_state(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.json"
            self.assertEqual({}, AxisPositionStore(missing).load())

            broken = Path(tmpdir) / "broken.json"
            broken.write_text("{bad json", encoding="utf-8")
            self.assertEqual({}, AxisPositionStore(broken).load())

    def test_axis9_angle_is_saved_with_pulse_position(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path)

            store.update_axis(9, 187500, source="test", angle_degrees=45.0)
            loaded = AxisPositionStore(path).load()

            self.assertEqual(187500.0, loaded["9"]["pulse"])
            self.assertEqual(45.0, loaded["9"]["angle_degrees"])
            self.assertEqual(45.0, loaded["9"]["coordinate"])
            self.assertEqual("deg", loaded["9"]["unit"])

    def test_axis3_position_keeps_pulse_without_unverified_absolute_coordinate(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path)

            store.update_axis(3, 123.0, source="test")
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(123.0, data["axes"]["3"]["pulse"])
            self.assertNotIn("coordinate", data["axes"]["3"])
            self.assertNotIn("unit", data["axes"]["3"])

    def test_update_axis_uses_runtime_calibration_mapping(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore
        from automatic_transfer_station.axis_runtime_calibration_store import RuntimeAxisCalibrationStore

        with tempfile.TemporaryDirectory() as tmpdir:
            calibration_path = Path(tmpdir) / "axis_runtime_calibration.json"
            calibration_store = RuntimeAxisCalibrationStore(calibration_path)
            calibration_store.load()
            calibration_store.calibrate_axis_at_pulse(5, current_pulse=687898.0, actual_coordinate=4.25)

            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path, axis_calibrations=calibration_store.calibrations)
            store.update_axis(5, 687898.0, source="test")

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(4.25, data["axes"]["5"]["coordinate"])

    def test_save_round_trips_and_leaves_no_tmp_file(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path)
            store.update_axis(7, -1133407, source="test")

            self.assertTrue(path.exists())
            self.assertEqual([], list(path.parent.glob("*.tmp")))

            reloaded = AxisPositionStore(path).load()
            self.assertEqual(-1133407.0, reloaded["7"]["pulse"])

    def test_load_cleans_up_stale_tmp_file(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            stale = path.with_name(f"{path.name}.tmp")
            stale.write_text("partial garbage", encoding="utf-8")

            AxisPositionStore(path).load()

            self.assertFalse(stale.exists())

    def test_future_schema_version_falls_back_to_defaults_and_reports(self):
        from automatic_transfer_station.axis_position_store import AxisPositionStore, SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            payload = {
                "schema_version": SCHEMA_VERSION + 1,
                "axes": {"7": {"axis": 7, "label": "X2", "pulse": -1133407.0}},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = AxisPositionStore(path)
            self.assertEqual({}, store.load())
            self.assertTrue(store.schema_warning)


    def test_schema_version_zero_falls_back_to_empty_state_with_warning(self):
        """Fix 3: schema_version=0 was never a real version; must be distrusted."""
        from automatic_transfer_station.axis_position_store import AxisPositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            payload = {
                "schema_version": 0,
                "axes": {"7": {"axis": 7, "label": "X2", "pulse": -1133407.0}},
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = AxisPositionStore(path)
            result = store.load()
            self.assertEqual({}, result)
            self.assertTrue(store.schema_warning)


    # ---- BP6 Fix 4: NaN/inf pulse guards ----

    def test_bp6_fix4_write_nan_pulse_is_rejected(self):
        """BP6 Fix 4: update_axis with NaN pulse must raise ValueError (not silently persist)."""
        from automatic_transfer_station.axis_position_store import AxisPositionStore
        import math
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path)
            with self.assertRaises(ValueError, msg="NaN pulse must raise ValueError"):
                store.update_axis(5, float("nan"))

    def test_bp6_fix4_write_inf_pulse_is_rejected(self):
        """BP6 Fix 4: update_axis with +inf/-inf pulse must raise ValueError."""
        from automatic_transfer_station.axis_position_store import AxisPositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            store = AxisPositionStore(path)
            with self.assertRaises(ValueError):
                store.update_axis(5, float("inf"))
            with self.assertRaises(ValueError):
                store.update_axis(5, float("-inf"))

    def test_bp6_fix4_load_nonfinite_pulse_skips_record(self):
        """BP6 Fix 4: A file containing a NaN pulse record must skip that record (not crash,
        not poison subsequent range checks). A valid record in the same file is kept."""
        import json
        from automatic_transfer_station.axis_position_store import AxisPositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            # Write a file with one NaN record and one valid record manually
            # (JSON spec forbids NaN, so we use a string that float() accepts but math.isfinite rejects)
            payload = {
                "schema_version": 1,
                "updated_at": "2026-01-01T00:00:00Z",
                "axes": {
                    "5": {"axis": 5, "label": "X1", "pulse": "NaN",  "source": "test"},
                    "7": {"axis": 7, "label": "X2", "pulse": -1133407.0, "source": "test"},
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = AxisPositionStore(path)
            result = store.load()

            # Axis 5 (NaN pulse) must be skipped
            self.assertNotIn("5", result, "NaN-pulse record must be skipped on load")
            # Axis 7 (valid) must be kept
            self.assertIn("7", result, "valid record must survive alongside a skipped NaN record")
            self.assertEqual(result["7"]["pulse"], -1133407.0)

    def test_bp6_fix4_load_nan_does_not_crash(self):
        """BP6 Fix 4: loading a file where float(pulse) would be NaN must not raise."""
        import json
        from automatic_transfer_station.axis_position_store import AxisPositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "axis_positions.json"
            payload = {
                "schema_version": 1,
                "updated_at": "2026-01-01T00:00:00Z",
                "axes": {
                    "5": {"axis": 5, "label": "X1", "pulse": "NaN", "source": "test"},
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = AxisPositionStore(path)
            # Must not raise
            result = store.load()
            self.assertEqual({}, result)


if __name__ == "__main__":
    unittest.main()
