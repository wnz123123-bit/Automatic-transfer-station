import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


class CameraMeasurementTests(unittest.TestCase):
    def test_line_measurement_reports_pixel_and_calibrated_distance(self):
        from automatic_transfer_station.camera_measurement import MeasurementObject, evaluate_measurement

        measurement = MeasurementObject(
            id=1,
            kind="line",
            points=((10, 20), (110, 70)),
            objective="10X",
            frame_width=200,
            frame_height=100,
            um_per_pixel=2.0,
            created_at="2026-07-02T10:00:00",
        )

        result = evaluate_measurement(measurement)

        self.assertAlmostEqual(100.0, result["dx_px"])
        self.assertAlmostEqual(50.0, result["dy_px"])
        self.assertAlmostEqual(111.8033989, result["length_px"])
        self.assertAlmostEqual(223.6067978, result["length_um"], places=6)
        self.assertEqual("223.6 um", result["display"])

    def test_line_measurement_uses_mm_for_long_calibrated_distance(self):
        from automatic_transfer_station.camera_measurement import MeasurementObject, evaluate_measurement

        measurement = MeasurementObject(
            id=1,
            kind="line",
            points=((0, 0), (600, 800)),
            objective="5X",
            frame_width=1000,
            frame_height=1000,
            um_per_pixel=2.0,
            created_at="2026-07-02T10:00:00",
        )

        result = evaluate_measurement(measurement)

        self.assertEqual("2.000 mm", result["display"])

    def test_polygon_measurement_reports_area_and_perimeter(self):
        from automatic_transfer_station.camera_measurement import MeasurementObject, evaluate_measurement

        measurement = MeasurementObject(
            id=2,
            kind="polygon",
            points=((0, 0), (40, 0), (40, 30), (0, 30)),
            objective="20X",
            frame_width=100,
            frame_height=100,
            um_per_pixel=5.0,
            created_at="2026-07-02T10:00:00",
        )

        result = evaluate_measurement(measurement)

        self.assertAlmostEqual(1200.0, result["area_px2"])
        self.assertAlmostEqual(140.0, result["perimeter_px"])
        self.assertAlmostEqual(30000.0, result["area_um2"])
        self.assertAlmostEqual(700.0, result["perimeter_um"])
        self.assertEqual("30000.0 um^2", result["display"])

    def test_polygon_measurement_uses_mm2_for_large_calibrated_area(self):
        from automatic_transfer_station.camera_measurement import MeasurementObject, evaluate_measurement

        measurement = MeasurementObject(
            id=2,
            kind="polygon",
            points=((0, 0), (1000, 0), (1000, 1000), (0, 1000)),
            objective="5X",
            frame_width=1200,
            frame_height=1200,
            um_per_pixel=2.0,
            created_at="2026-07-02T10:00:00",
        )

        result = evaluate_measurement(measurement)

        self.assertEqual("4.000 mm^2", result["display"])

    def test_uncalibrated_measurements_fall_back_to_pixels(self):
        from automatic_transfer_station.camera_measurement import MeasurementObject, evaluate_measurement

        line = MeasurementObject(
            id=1,
            kind="line",
            points=((0, 0), (3, 4)),
            objective="10X",
            frame_width=20,
            frame_height=20,
            um_per_pixel=None,
            created_at="2026-07-02T10:00:00",
        )
        polygon = MeasurementObject(
            id=2,
            kind="polygon",
            points=((0, 0), (10, 0), (10, 10), (0, 10)),
            objective="10X",
            frame_width=20,
            frame_height=20,
            um_per_pixel=None,
            created_at="2026-07-02T10:00:00",
        )

        self.assertEqual("5.0 px", evaluate_measurement(line)["display"])
        self.assertEqual("100.0 px^2", evaluate_measurement(polygon)["display"])

    def test_sidecar_payload_records_measurements_and_crosshair(self):
        from automatic_transfer_station.camera_measurement import (
            MeasurementObject,
            measurement_sidecar_payload,
        )

        measurement = MeasurementObject(
            id=1,
            kind="line",
            points=((10, 20), (20, 20)),
            objective="10X",
            frame_width=100,
            frame_height=80,
            um_per_pixel=1.5,
            created_at="2026-07-02T10:00:00",
        )

        payload = measurement_sidecar_payload(
            objective="10X",
            frame_width=100,
            frame_height=80,
            flip_horizontal=True,
            flip_vertical=False,
            um_per_pixel=1.5,
            crosshair=(30, 40),
            measurements=[measurement],
        )

        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("10X", payload["objective"])
        self.assertEqual({"x": 30.0, "y": 40.0}, payload["crosshair"])
        self.assertEqual(True, payload["display"]["flip_horizontal"])
        self.assertEqual(False, payload["display"]["flip_vertical"])
        self.assertEqual("line", payload["measurements"][0]["kind"])
        self.assertEqual("15.0 um", payload["measurements"][0]["result"]["display"])


if __name__ == "__main__":
    unittest.main()
