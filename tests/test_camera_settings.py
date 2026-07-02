import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


class CameraSettingsTests(unittest.TestCase):
    def test_snapshot_template_uses_objective_and_unique_index(self):
        from automatic_transfer_station.camera_settings import build_snapshot_path

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            existing = directory / "20260701_120000_20X_001.tif"
            existing.write_bytes(b"old")

            path = build_snapshot_path(
                directory=directory,
                template="{date}_{time}_{objective}_{index}",
                objective="20X",
                image_format="TIFF",
                now=lambda: "20260701_120000",
            )

        self.assertEqual("20260701_120000_20X_002.tif", path.name)

    def test_scale_calibration_store_calculates_and_persists_um_per_pixel(self):
        from automatic_transfer_station.camera_settings import ScaleCalibrationStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scale.json"
            store = ScaleCalibrationStore(path)

            calibration = store.calibrate_from_points("20X", known_um=100.0, p1=(10, 20), p2=(110, 20))
            reloaded = ScaleCalibrationStore(path).load()

        self.assertAlmostEqual(1.0, calibration.um_per_pixel)
        self.assertAlmostEqual(1.0, reloaded["20X"].um_per_pixel)
        self.assertEqual(100.0, reloaded["20X"].known_um)


if __name__ == "__main__":
    unittest.main()
