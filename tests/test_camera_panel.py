import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

_QT_APP = None


def get_app():
    global _QT_APP
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        _QT_APP = QApplication([])
        app = _QT_APP
    elif _QT_APP is None:
        _QT_APP = app
    app.setQuitOnLastWindowClosed(False)
    return app


def wait_until(predicate, timeout=2.0):
    app = get_app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


class FakeCameraController:
    def __init__(self):
        from automatic_transfer_station.camera_controller import CameraDeviceInfo, CameraFrame

        self.calls = []
        self.device = CameraDeviceInfo("puA2500-14ucMIC", "25141247", "BaslerUsb")
        self.frame = CameraFrame(width=2, height=1, rgb_bytes=b"\xff\x00\x00\x00\xff\x00")
        self.is_open = False
        self.is_previewing = False
        self.current_device = None

    def list_devices(self):
        self.calls.append(("list_devices",))
        return [self.device]

    def open(self, serial=None):
        self.calls.append(("open", serial))
        self.is_open = True
        self.current_device = self.device
        return self.device

    def close(self):
        self.calls.append(("close",))
        self.is_open = False
        self.current_device = None

    def start_preview(self):
        self.calls.append(("start_preview",))
        self.is_previewing = True

    def stop_preview(self):
        self.calls.append(("stop_preview",))
        self.is_previewing = False

    def set_exposure(self, auto, value_ms=None):
        self.calls.append(("set_exposure", auto, value_ms))

    def set_gain(self, auto, value=None):
        self.calls.append(("set_gain", auto, value))

    def grab_one(self, timeout_ms=1000):
        self.calls.append(("grab_one", timeout_ms))
        time.sleep(0.01)
        return self.frame


class CameraPanelTests(unittest.TestCase):
    def setUp(self):
        get_app()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.snapshot_dir = Path(self._tmpdir.name)
        self._panels = []

    def tearDown(self):
        app = get_app()
        for panel in reversed(self._panels):
            panel.prepare_for_close(timeout_ms=2000)
            panel.close()
            panel.deleteLater()
        app.processEvents()
        self._tmpdir.cleanup()

    def _make_panel(self, controller):
        from automatic_transfer_station.camera_panel import CameraPanel, InlineCameraExecutor

        controls = {}
        panel = CameraPanel(
            register=lambda name, widget: controls.setdefault(name, widget),
            controller_factory=lambda: controller,
            task_executor=InlineCameraExecutor(),
            snapshot_dir=self.snapshot_dir,
        )
        self._panels.append(panel)
        return panel, controls

    def test_connect_lists_and_opens_preferred_camera(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)

        panel.connect_camera()

        self.assertIn(("list_devices",), controller.calls)
        self.assertIn(("open", "25141247"), controller.calls)
        self.assertIn("puA2500-14ucMIC", controls["cameraDeviceLabel"].text())
        self.assertIn("25141247", controls["cameraDeviceLabel"].text())
        self.assertEqual("断开", controls["cameraConnect"].text())

    def test_controller_is_created_only_when_connecting(self):
        from automatic_transfer_station.camera_panel import CameraPanel, InlineCameraExecutor

        controller = FakeCameraController()
        created = []
        panel = CameraPanel(
            controller_factory=lambda: created.append(controller) or controller,
            task_executor=InlineCameraExecutor(),
            snapshot_dir=self.snapshot_dir,
        )
        self._panels.append(panel)

        self.assertEqual([], created)

        panel.connect_camera()

        self.assertEqual([controller], created)
        self.assertIn(("open", "25141247"), controller.calls)

    def test_exposure_and_gain_controls_delegate_to_controller(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        panel.connect_camera()

        controls["cameraExposureAuto"].setChecked(False)
        controls["cameraExposureSpin"].setValue(12.5)
        panel.apply_exposure()
        controls["cameraGainAuto"].setChecked(False)
        controls["cameraGainSpin"].setValue(3.0)
        panel.apply_gain()

        self.assertIn(("set_exposure", False, 12.5), controller.calls)
        self.assertIn(("set_gain", False, 3.0), controller.calls)

    def test_preview_updates_image_label_and_stops_cleanly(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        panel.connect_camera()

        panel.start_preview()

        self.assertTrue(wait_until(lambda: ("start_preview",) in controller.calls))
        self.assertTrue(wait_until(lambda: controls["cameraImageLabel"].pixmap() is not None))

        panel.stop_preview()
        self.assertTrue(wait_until(lambda: ("stop_preview",) in controller.calls))
        self.assertTrue(panel.prepare_for_close(timeout_ms=2000))

    def test_snapshot_saves_latest_frame_to_snapshot_directory(self):
        controller = FakeCameraController()
        panel, _controls = self._make_panel(controller)
        panel._last_frame = controller.frame

        path = panel.save_snapshot()

        self.assertEqual(self.snapshot_dir, path.parent)
        self.assertTrue(path.name.startswith("camera-"))
        self.assertEqual(".png", path.suffix)
        self.assertTrue(path.exists())

    def test_connect_failure_is_reported_without_raising_to_gui(self):
        from automatic_transfer_station.camera_controller import CameraError

        class FailingController(FakeCameraController):
            def list_devices(self):
                raise CameraError("pypylon is not installed")

        controller = FailingController()
        panel, controls = self._make_panel(controller)

        panel.connect_camera()

        self.assertIn("pypylon is not installed", controls["cameraStatusLabel"].text())
        self.assertEqual("连接", controls["cameraConnect"].text())

    def test_camera_panel_translates_to_english(self):
        from automatic_transfer_station.i18n import translator

        translator.set_language("en")
        try:
            controller = FakeCameraController()
            _panel, controls = self._make_panel(controller)

            self.assertEqual("Camera preview", controls["cameraTitleLabel"].text())
            self.assertEqual("Connect", controls["cameraConnect"].text())
            self.assertEqual("Start preview", controls["cameraPreview"].text())
            self.assertEqual("Snapshot", controls["cameraSnapshot"].text())
        finally:
            translator.initialize("zh")


if __name__ == "__main__":
    unittest.main()
