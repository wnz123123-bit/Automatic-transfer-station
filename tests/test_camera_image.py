import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


class CameraImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def assertRedAnnotationPixel(self, image, x, y):  # noqa: N802 - unittest helper name
        from PySide6.QtGui import QColor

        color = QColor(image.pixel(x, y))
        self.assertGreater(color.red(), 120)
        self.assertLess(color.green(), 80)
        self.assertLess(color.blue(), 80)

    def assertRedAnnotationOnHorizontalLine(self, image, x1, x2, y):  # noqa: N802
        from PySide6.QtGui import QColor

        for x in range(x1, x2 + 1):
            color = QColor(image.pixel(x, y))
            if color.red() > 100 and color.red() > color.green() * 2 and color.red() > color.blue() * 2:
                return
        self.fail(f"No red annotation pixel found on y={y}, x={x1}..{x2}")

    def imageBytes(self, image):  # noqa: N802 - unittest helper name
        return bytes(image.constBits())

    def test_flip_frame_applies_horizontal_and_vertical_flip(self):
        from automatic_transfer_station.camera_controller import CameraFrame
        from automatic_transfer_station.camera_image import flip_frame

        # 2x2 RGB pixels labelled A, B, C, D by their red channel.
        frame = CameraFrame(
            width=2,
            height=2,
            rgb_bytes=bytes([
                1, 0, 0, 2, 0, 0,
                3, 0, 0, 4, 0, 0,
            ]),
        )

        flipped = flip_frame(frame, horizontal=True, vertical=True)

        self.assertEqual(
            bytes([
                4, 0, 0, 3, 0, 0,
                2, 0, 0, 1, 0, 0,
            ]),
            flipped.rgb_bytes,
        )

    def test_choose_scale_bar_prefers_readable_length(self):
        from automatic_transfer_station.camera_image import choose_scale_bar

        label_um, pixel_width = choose_scale_bar(image_width=1000, um_per_pixel=0.5)

        self.assertEqual(100, label_um)
        self.assertEqual(200, pixel_width)

    def test_draw_scale_bar_does_not_render_objective_text(self):
        from PySide6.QtGui import QColor, QImage
        from automatic_transfer_station.camera_image import draw_scale_bar
        from automatic_transfer_station.camera_settings import ScaleCalibration

        image = QImage(500, 200, QImage.Format_RGB888)
        image.fill(QColor(80, 80, 80))
        calibration = ScaleCalibration(
            objective="20X",
            um_per_pixel=0.5,
            known_um=100.0,
            pixel_distance=200.0,
            p1=(0.0, 0.0),
            p2=(200.0, 0.0),
            calibrated_at="2026-07-06T10:00:00",
        )

        with_20x = draw_scale_bar(image, calibration, "20X")
        with_long_objective = draw_scale_bar(image, calibration, "LONG_OBJECTIVE_LABEL")

        self.assertEqual(self.imageBytes(with_20x), self.imageBytes(with_long_objective))

    def test_draw_scale_bar_does_not_paint_black_background_panel(self):
        from PySide6.QtGui import QColor, QImage
        from automatic_transfer_station.camera_image import draw_scale_bar
        from automatic_transfer_station.camera_settings import ScaleCalibration

        base_color = QColor(80, 80, 80)
        image = QImage(500, 200, QImage.Format_RGB888)
        image.fill(base_color)
        calibration = ScaleCalibration(
            objective="20X",
            um_per_pixel=0.5,
            known_um=100.0,
            pixel_distance=200.0,
            p1=(0.0, 0.0),
            p2=(200.0, 0.0),
            calibrated_at="2026-07-06T10:00:00",
        )

        result = draw_scale_bar(image, calibration, "20X")

        self.assertEqual(base_color, QColor(result.pixel(12, 151)))
        self.assertRedAnnotationOnHorizontalLine(result, 18, 118, 176)

    def test_draw_measurement_overlay_draws_center_crosshair(self):
        from PySide6.QtGui import QColor, QImage
        from automatic_transfer_station.camera_image import draw_measurement_overlay

        image = QImage(40, 20, QImage.Format_RGB888)
        image.fill(QColor(0, 0, 0))

        result = draw_measurement_overlay(image, center_crosshair=True)

        self.assertRedAnnotationPixel(result, 20, 10)
        self.assertEqual(QColor(image.pixel(1, 1)), QColor(result.pixel(1, 1)))

    def test_draw_measurement_overlay_uses_red_for_completed_measurements(self):
        from PySide6.QtGui import QColor, QImage
        from automatic_transfer_station.camera_image import draw_measurement_overlay
        from automatic_transfer_station.camera_measurement import MeasurementObject

        image = QImage(80, 60, QImage.Format_RGB888)
        image.fill(QColor(0, 0, 0))
        measurement = MeasurementObject(
            id=1,
            kind="line",
            points=((10, 40), (70, 40)),
            objective="10X",
            frame_width=80,
            frame_height=60,
            um_per_pixel=None,
            created_at="2026-07-03T10:00:00",
        )

        result = draw_measurement_overlay(image, measurements=(measurement,))

        self.assertRedAnnotationOnHorizontalLine(result, 10, 70, 40)

    def test_draw_measurement_overlay_shows_active_preview_line_to_cursor(self):
        from PySide6.QtGui import QColor, QImage
        from automatic_transfer_station.camera_image import draw_measurement_overlay

        image = QImage(40, 20, QImage.Format_RGB888)
        image.fill(QColor(0, 0, 0))

        result = draw_measurement_overlay(
            image,
            active_points=((10, 10),),
            active_preview_point=(30, 10),
            active_kind="line",
            active_source_size=(40, 20),
        )

        self.assertRedAnnotationOnHorizontalLine(result, 10, 30, 10)


if __name__ == "__main__":
    unittest.main()
