import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class MicroscopePositionStoreTests(unittest.TestCase):
    def test_store_persists_rename_and_delete_for_axis3_points(self):
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            store = MicroscopePositionStore(path)

            store.save_point("安全高度", 100.0)
            store.save_point("观察高度", 250.5)
            store.rename_point("观察高度", "接触前")
            store.delete_point("安全高度")

            reloaded = MicroscopePositionStore(path)
            reloaded.load()

            self.assertEqual([("接触前", 250.5)], [(point.name, point.pulse) for point in reloaded.points()])

    def test_save_round_trips_and_leaves_no_tmp_file(self):
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            store = MicroscopePositionStore(path)
            store.save_point("安全高度", 100.0)

            self.assertTrue(path.exists())
            self.assertEqual([], list(path.parent.glob("*.tmp")))

            reloaded = MicroscopePositionStore(path)
            reloaded.load()
            self.assertEqual([("安全高度", 100.0)], [(p.name, p.pulse) for p in reloaded.points()])

    def test_load_cleans_up_stale_tmp_file(self):
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            stale = path.with_name(f"{path.name}.tmp")
            stale.write_text("partial garbage", encoding="utf-8")

            MicroscopePositionStore(path).load()

            self.assertFalse(stale.exists())

    def test_future_schema_version_falls_back_to_defaults_and_reports(self):
        import json

        from automatic_transfer_station.microscope_position_store import (
            MicroscopePositionStore,
            SCHEMA_VERSION,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            payload = {
                "schema_version": SCHEMA_VERSION + 1,
                "points": [{"name": "安全高度", "pulse": 100.0}],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = MicroscopePositionStore(path)
            self.assertEqual([], store.load())
            self.assertTrue(store.schema_warning)


    def test_schema_version_zero_falls_back_to_no_points_with_warning(self):
        """Fix 3: schema_version=0 was never a real version; must be distrusted."""
        import json

        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            payload = {
                "schema_version": 0,
                "points": [{"name": "安全高度", "pulse": 100.0}],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            store = MicroscopePositionStore(path)
            result = store.load()
            self.assertEqual([], result)
            self.assertTrue(store.schema_warning)


    # ---- BP6 Fix 4: NaN/inf pulse guards ----

    def test_bp6_fix4_write_nan_pulse_is_rejected(self):
        """BP6 Fix 4: save_point with NaN pulse must raise ValueError."""
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            store = MicroscopePositionStore(path)
            with self.assertRaises(ValueError, msg="NaN pulse must raise ValueError"):
                store.save_point("测试", float("nan"))

    def test_bp6_fix4_write_inf_pulse_is_rejected(self):
        """BP6 Fix 4: save_point with +inf/-inf pulse must raise ValueError."""
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            store = MicroscopePositionStore(path)
            with self.assertRaises(ValueError):
                store.save_point("测试", float("inf"))
            with self.assertRaises(ValueError):
                store.save_point("测试", float("-inf"))

    def test_bp6_fix4_load_nonfinite_pulse_skips_record(self):
        """BP6 Fix 4: loading a file with a NaN pulse skips that record, keeps valid ones."""
        import json
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            payload = {
                "schema_version": 1,
                "updated_at": "2026-01-01T00:00:00Z",
                "points": [
                    {"name": "安全高度", "pulse": "NaN"},
                    {"name": "观察高度", "pulse": 250.5},
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = MicroscopePositionStore(path)
            result = store.load()
            names = [p.name for p in result]
            self.assertNotIn("安全高度", names, "NaN-pulse point must be skipped")
            self.assertIn("观察高度", names, "valid point must survive")

    def test_bp6_fix4_load_nan_does_not_crash(self):
        """BP6 Fix 4: load of all-NaN pulse records must not raise."""
        import json
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            payload = {
                "schema_version": 1,
                "updated_at": "2026-01-01T00:00:00Z",
                "points": [{"name": "安全高度", "pulse": "NaN"}],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = MicroscopePositionStore(path)
            result = store.load()
            self.assertEqual([], result)

    # ---- CP3: finite-but-absurd pulse sanity cap ----

    def test_cp3_save_over_cap_pulse_is_rejected(self):
        from automatic_transfer_station.axis_calibration import MICROSCOPE_Z_MAX_ABS_PULSE
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            store = MicroscopePositionStore(path)
            with self.assertRaises(ValueError):
                store.save_point("坏点", MICROSCOPE_Z_MAX_ABS_PULSE * 10)
            with self.assertRaises(ValueError):
                store.save_point("坏点", -MICROSCOPE_Z_MAX_ABS_PULSE * 10)

    def test_cp3_load_skips_over_cap_pulse_keeps_valid(self):
        import json
        from automatic_transfer_station.axis_calibration import MICROSCOPE_Z_MAX_ABS_PULSE
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            payload = {
                "schema_version": 1,
                "updated_at": "2026-01-01T00:00:00Z",
                "points": [
                    {"name": "损坏", "pulse": MICROSCOPE_Z_MAX_ABS_PULSE * 100},
                    {"name": "正常", "pulse": 250.5},
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = MicroscopePositionStore(path)
            names = [p.name for p in store.load()]
            self.assertNotIn("损坏", names)
            self.assertIn("正常", names)

    def test_cp3_normal_marked_pulse_round_trips_unaffected(self):
        from automatic_transfer_station.microscope_position_store import MicroscopePositionStore
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "microscope_z_positions.json"
            store = MicroscopePositionStore(path)
            store.save_point("正常", 81506.0)  # a realistic marked encoder reading
            reloaded = MicroscopePositionStore(path)
            reloaded.load()
            self.assertEqual([("正常", 81506.0)], [(p.name, p.pulse) for p in reloaded.points()])


if __name__ == "__main__":
    unittest.main()
