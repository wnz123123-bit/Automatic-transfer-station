import os, sys
from pathlib import Path
from xml.dom import minidom
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from automatic_transfer_station.gamepad_help import build_f710_help_svg, render_help_pixmap


class F710HelpSvgTests(unittest.TestCase):
    def setUp(self):
        self.svg = build_f710_help_svg()

    def test_is_well_formed_svg(self):
        # Must parse as XML and be a single <svg> root with a viewBox.
        doc = minidom.parseString(self.svg)
        self.assertEqual(doc.documentElement.tagName, "svg")
        self.assertEqual(doc.documentElement.getAttribute("viewBox"), "0 0 1000 700")

    def test_names_every_control(self):
        for token in ("START", "BACK", "LT", "RT", "LB", "RB", "MODE",
                      "左摇杆", "右摇杆", "十字键", "面键"):
            self.assertIn(token, self.svg, token)

    def test_describes_each_function(self):
        for token in ("显微镜Z", "载玻片Z", "X3", "Y3", "X2", "Y2", "急停",
                      "启用 / 停用", "步长档", "选轴", "Space"):
            self.assertIn(token, self.svg, token)

    def test_face_buttons_use_brand_colours(self):
        # Y/A/X/B chips are coloured so the diagram matches the physical pad.
        for colour in ("#b8860b", "#2faf4e", "#2f7fd6", "#d83b3b"):
            self.assertIn(colour, self.svg, colour)


class F710HelpRenderTests(unittest.TestCase):
    def test_renders_to_nonempty_pixmap(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        pm = render_help_pixmap()
        self.assertFalse(pm.isNull())
        self.assertGreater(pm.width(), 0)
        self.assertGreater(pm.height(), 0)


class F710HelpSvgI18nTests(unittest.TestCase):
    """Verify that the help diagram renders English labels when the translator
    is set to English."""

    def test_header_in_english(self):
        from automatic_transfer_station.i18n import translator

        translator.set_language("en")
        try:
            svg = build_f710_help_svg()
            self.assertIn("F710 Gamepad Key Reference", svg)
        finally:
            translator.initialize("zh")

    def test_callout_labels_in_english(self):
        from automatic_transfer_station.i18n import translator

        translator.set_language("en")
        try:
            svg = build_f710_help_svg()
            self.assertIn("Left stick", svg)
            self.assertIn("Right stick", svg)
        finally:
            translator.initialize("zh")


if __name__ == "__main__":
    unittest.main()
