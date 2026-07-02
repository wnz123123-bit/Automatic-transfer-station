import os
import sys
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


class CameraImageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
