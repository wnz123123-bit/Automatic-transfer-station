import os
import sys
import tempfile
import unittest
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


class FakeCameraHandle:
    def __init__(self, serial_number="25141247"):
        self.serial_number = serial_number
        self.calls = []
        self.opened = False
        self.grabbing = False
        self.closed = False
        self.next_frame = None

    def open(self):
        self.calls.append(("open",))
        self.opened = True

    def close(self):
        self.calls.append(("close",))
        self.closed = True
        self.opened = False

    def start_preview(self):
        self.calls.append(("start_preview",))
        self.grabbing = True

    def stop_preview(self):
        self.calls.append(("stop_preview",))
        self.grabbing = False

    def grab_rgb(self, timeout_ms=1000):
        self.calls.append(("grab_rgb", timeout_ms))
        return self.next_frame

    def set_exposure(self, auto, value_ms):
        self.calls.append(("set_exposure", auto, value_ms))

    def set_gain(self, auto, value):
        self.calls.append(("set_gain", auto, value))


class FakeBackend:
    def __init__(self, devices, handles):
        self.devices = devices
        self.handles = handles
        self.opened_serials = []

    def list_devices(self):
        return list(self.devices)

    def open_device(self, serial_number):
        self.opened_serials.append(serial_number)
        return self.handles[serial_number]


class CameraControllerTests(unittest.TestCase):
    def test_open_prefers_configured_basler_serial_when_multiple_devices_exist(self):
        from automatic_transfer_station.camera_controller import (
            BaslerCameraController,
            CameraDeviceInfo,
        )

        target = FakeCameraHandle()
        other = FakeCameraHandle(serial_number="OTHER")
        backend = FakeBackend(
            [
                CameraDeviceInfo(model_name="Other", serial_number="OTHER", device_class="BaslerUsb"),
                CameraDeviceInfo(model_name="puA2500-14ucMIC", serial_number="25141247", device_class="BaslerUsb"),
            ],
            {"25141247": target, "OTHER": other},
        )
        controller = BaslerCameraController(backend=backend)

        device = controller.open()

        self.assertEqual("25141247", device.serial_number)
        self.assertEqual(["25141247"], backend.opened_serials)
        self.assertTrue(target.opened)

    def test_open_reports_missing_camera_instead_of_crashing(self):
        from automatic_transfer_station.camera_controller import BaslerCameraController, CameraError

        controller = BaslerCameraController(backend=FakeBackend([], {}))

        with self.assertRaisesRegex(CameraError, "No Basler camera"):
            controller.open()

    def test_preview_and_manual_settings_delegate_to_open_handle(self):
        from automatic_transfer_station.camera_controller import (
            BaslerCameraController,
            CameraDeviceInfo,
            CameraFrame,
        )

        handle = FakeCameraHandle()
        frame = CameraFrame(width=2, height=1, rgb_bytes=b"\x00\x01\x02\x03\x04\x05")
        handle.next_frame = frame
        controller = BaslerCameraController(
            backend=FakeBackend(
                [CameraDeviceInfo("puA2500-14ucMIC", "25141247", "BaslerUsb")],
                {"25141247": handle},
            )
        )
        controller.open()

        controller.start_preview()
        controller.set_exposure(auto=False, value_ms=12.5)
        controller.set_gain(auto=False, value=3.0)
        grabbed = controller.grab_one(timeout_ms=250)
        controller.stop_preview()

        self.assertIs(grabbed, frame)
        self.assertIn(("start_preview",), handle.calls)
        self.assertIn(("set_exposure", False, 12.5), handle.calls)
        self.assertIn(("set_gain", False, 3.0), handle.calls)
        self.assertIn(("grab_rgb", 250), handle.calls)
        self.assertIn(("stop_preview",), handle.calls)

    def test_close_stops_preview_before_closing_camera(self):
        from automatic_transfer_station.camera_controller import (
            BaslerCameraController,
            CameraDeviceInfo,
        )

        handle = FakeCameraHandle()
        controller = BaslerCameraController(
            backend=FakeBackend(
                [CameraDeviceInfo("puA2500-14ucMIC", "25141247", "BaslerUsb")],
                {"25141247": handle},
            )
        )
        controller.open()
        controller.start_preview()

        controller.close()

        self.assertEqual(("stop_preview",), handle.calls[-2])
        self.assertEqual(("close",), handle.calls[-1])
        self.assertFalse(controller.is_open)
        self.assertFalse(controller.is_previewing)

    def test_snapshot_path_uses_runtime_snapshot_directory(self):
        from automatic_transfer_station.camera_controller import snapshot_path

        with tempfile.TemporaryDirectory() as tmp:
            path = snapshot_path(Path(tmp), now=lambda: "20260626-120102")

        self.assertEqual("camera-20260626-120102.png", path.name)
        self.assertEqual(Path(tmp), path.parent)


if __name__ == "__main__":
    unittest.main()
