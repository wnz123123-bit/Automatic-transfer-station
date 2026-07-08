import os
import sys
import tempfile
import threading
import time
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

    def capabilities(self):
        from automatic_transfer_station.camera_controller import CameraCapabilities, FeatureRange

        self.calls.append(("capabilities",))
        return CameraCapabilities(
            width=FeatureRange(4, 2592, 2),
            height=FeatureRange(4, 1944, 2),
            binning=FeatureRange(1, 4, 1),
            exposure_ms=FeatureRange(0.01, 1000.0, None),
            gain=FeatureRange(0.0, 24.0, None),
            gamma=FeatureRange(0.25, 2.0, None),
            sharpness=FeatureRange(0.0, 1.0, None),
        )

    def apply_acquisition_settings(self, settings):
        self.calls.append(("apply_acquisition_settings", settings.width, settings.height, settings.binning))

    def set_image_adjustment(self, gamma=None, sharpness=None):
        self.calls.append(("set_image_adjustment", gamma, sharpness))


class BlockingGrabHandle(FakeCameraHandle):
    def __init__(self):
        super().__init__()
        from automatic_transfer_station.camera_controller import CameraFrame

        self.next_frame = CameraFrame(width=1, height=1, rgb_bytes=b"\x00\x00\x00")
        self.grab_entered = threading.Event()
        self.release_grab = threading.Event()
        self.exposure_started = threading.Event()
        self.overlapped_access = False
        self._inside_grab = False

    def grab_rgb(self, timeout_ms=1000):
        self.calls.append(("grab_rgb", timeout_ms))
        self._inside_grab = True
        self.grab_entered.set()
        self.release_grab.wait(timeout=2.0)
        self._inside_grab = False
        return self.next_frame

    def set_exposure(self, auto, value_ms):
        self.overlapped_access = self._inside_grab
        self.exposure_started.set()
        super().set_exposure(auto, value_ms)


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


class FakeFeature:
    def __init__(self, value, symbolics=None):
        self.value = value
        self.symbolics = list(symbolics or [])
        self.set_values = []

    def SetValue(self, value):
        self.value = value
        self.set_values.append(value)

    def GetSymbolics(self):
        return list(self.symbolics)


class RejectingFeature(FakeFeature):
    def SetValue(self, value):
        self.set_values.append(value)
        raise RuntimeError("simulated feature rejection")


class FakeTriggerModeFeature(FakeFeature):
    def __init__(self, camera):
        super().__init__("On")
        self.camera = camera

    def SetValue(self, value):
        super().SetValue(value)
        self.camera.trigger_modes[self.camera.TriggerSelector.value] = value


class FakeGrabArray:
    def tobytes(self):
        return b"\x00\x01\x02\x03\x04\x05"


class FakeConvertedImage:
    def GetArray(self):
        return FakeGrabArray()


class FakeImageFormatConverter:
    def Convert(self, result):
        return FakeConvertedImage()


class FakePylonModule:
    GrabStrategy_LatestImageOnly = 17
    TimeoutHandling_ThrowException = 23
    PixelType_RGB8packed = 1
    OutputBitAlignment_MsbAligned = 2

    def ImageFormatConverter(self):
        return FakeImageFormatConverter()


class FakeDeviceInfo:
    def GetSerialNumber(self):
        return "25141247"

    def GetModelName(self):
        return "puA2500-14ucMIC"


class FakeGrabResult:
    Width = 2
    Height = 1
    PixelType = 0
    ErrorCode = 0
    ErrorDescription = ""

    def __init__(self):
        self.released = False

    def GrabSucceeded(self):
        return True

    def Release(self):
        self.released = True


class FreeRunRequiredCamera:
    def __init__(self):
        self.AcquisitionMode = FakeFeature("SingleFrame")
        self.TriggerSelector = FakeFeature(
            "FrameStart",
            symbolics=["FrameStart", "FrameBurstStart", "ExposureStart"],
        )
        self.trigger_modes = {
            "FrameStart": "On",
            "FrameBurstStart": "On",
            "ExposureStart": "On",
        }
        self.TriggerMode = FakeTriggerModeFeature(self)
        self.calls = []
        self.opened = False
        self.grabbing = False

    def IsOpen(self):
        return self.opened

    def Open(self):
        self.calls.append(("Open",))
        self.opened = True

    def Close(self):
        self.calls.append(("Close",))
        self.opened = False

    def IsGrabbing(self):
        return self.grabbing

    def StartGrabbing(self, strategy):
        self.calls.append(("StartGrabbing", strategy))
        self._require_free_run()
        self.grabbing = True

    def StartGrabbingMax(self, count):
        self.calls.append(("StartGrabbingMax", count))
        self._require_free_run()
        self.grabbing = True

    def StopGrabbing(self):
        self.calls.append(("StopGrabbing",))
        self.grabbing = False

    def RetrieveResult(self, timeout_ms, timeout_handling):
        self.calls.append(("RetrieveResult", timeout_ms, timeout_handling))
        return FakeGrabResult()

    def GetDeviceInfo(self):
        return FakeDeviceInfo()

    def _require_free_run(self):
        if self.AcquisitionMode.value != "Continuous":
            raise RuntimeError("AcquisitionMode was not normalized before grabbing")
        enabled = [selector for selector, mode in self.trigger_modes.items() if mode != "Off"]
        if enabled:
            raise RuntimeError(f"TriggerMode was not disabled before grabbing: {enabled}")


class RetrieveTimeoutCamera(FreeRunRequiredCamera):
    def RetrieveResult(self, timeout_ms, timeout_handling):
        self.calls.append(("RetrieveResult", timeout_ms, timeout_handling))
        raise RuntimeError("simulated retrieve timeout")


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
            CameraAcquisitionSettings,
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
        controller.set_image_adjustment(gamma=1.2, sharpness=0.4)
        controller.apply_acquisition_settings(CameraAcquisitionSettings(width=1296, height=972, binning=2))
        grabbed = controller.grab_one(timeout_ms=250)
        controller.stop_preview()

        self.assertIs(grabbed, frame)
        self.assertIn(("start_preview",), handle.calls)
        self.assertIn(("set_exposure", False, 12.5), handle.calls)
        self.assertIn(("set_gain", False, 3.0), handle.calls)
        self.assertIn(("set_image_adjustment", 1.2, 0.4), handle.calls)
        self.assertIn(("apply_acquisition_settings", 1296, 972, 2), handle.calls)
        self.assertIn(("grab_rgb", 250), handle.calls)
        self.assertIn(("stop_preview",), handle.calls)

    def test_capabilities_and_snap_settings_restore_live_settings(self):
        from automatic_transfer_station.camera_controller import (
            BaslerCameraController,
            CameraAcquisitionSettings,
            CameraDeviceInfo,
            CameraFrame,
        )

        handle = FakeCameraHandle()
        handle.next_frame = CameraFrame(width=4, height=2, rgb_bytes=bytes(range(24)))
        controller = BaslerCameraController(
            backend=FakeBackend(
                [CameraDeviceInfo("puA2500-14ucMIC", "25141247", "BaslerUsb")],
                {"25141247": handle},
            )
        )
        controller.open()

        caps = controller.capabilities()
        frame = controller.grab_one_with_settings(
            snap_settings=CameraAcquisitionSettings(width=2592, height=1944, binning=1),
            restore_settings=CameraAcquisitionSettings(width=1296, height=972, binning=2),
            timeout_ms=500,
        )

        self.assertEqual(2592, caps.width.max)
        self.assertIs(frame, handle.next_frame)
        self.assertIn(("apply_acquisition_settings", 2592, 1944, 1), handle.calls)
        self.assertIn(("grab_rgb", 500), handle.calls)
        self.assertEqual(("apply_acquisition_settings", 1296, 972, 2), handle.calls[-1])

    def test_parameter_writes_wait_for_in_flight_grab(self):
        from automatic_transfer_station.camera_controller import BaslerCameraController, CameraDeviceInfo

        handle = BlockingGrabHandle()
        controller = BaslerCameraController(
            backend=FakeBackend(
                [CameraDeviceInfo("puA2500-14ucMIC", "25141247", "BaslerUsb")],
                {"25141247": handle},
            )
        )
        controller.open()

        grab_thread = threading.Thread(target=lambda: controller.grab_one(timeout_ms=250), daemon=True)
        grab_thread.start()
        self.assertTrue(handle.grab_entered.wait(timeout=1.0))

        set_thread = threading.Thread(target=lambda: controller.set_exposure(False, 12.5), daemon=True)
        set_thread.start()
        time.sleep(0.05)

        self.assertFalse(handle.exposure_started.is_set())
        handle.release_grab.set()
        grab_thread.join(timeout=1.0)
        set_thread.join(timeout=1.0)

        self.assertFalse(grab_thread.is_alive())
        self.assertFalse(set_thread.is_alive())
        self.assertTrue(handle.exposure_started.is_set())
        self.assertFalse(handle.overlapped_access)

    def test_live_parameter_writes_do_not_restart_preview(self):
        from automatic_transfer_station.camera_controller import BaslerCameraController, CameraDeviceInfo

        handle = FakeCameraHandle()
        controller = BaslerCameraController(
            backend=FakeBackend(
                [CameraDeviceInfo("puA2500-14ucMIC", "25141247", "BaslerUsb")],
                {"25141247": handle},
            )
        )
        controller.open()
        controller.start_preview()
        handle.calls.clear()

        controller.set_exposure(False, 12.5)
        controller.set_gain(False, 3.0)
        controller.set_image_adjustment(gamma=1.2, sharpness=0.4)

        self.assertEqual(
            [
                ("set_exposure", False, 12.5),
                ("set_gain", False, 3.0),
                ("set_image_adjustment", 1.2, 0.4),
            ],
            handle.calls,
        )
        self.assertTrue(controller.is_previewing)

    def test_acquisition_setting_write_pauses_preview_before_touching_camera_features(self):
        from automatic_transfer_station.camera_controller import (
            BaslerCameraController,
            CameraAcquisitionSettings,
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
        handle.calls.clear()

        controller.apply_acquisition_settings(CameraAcquisitionSettings(width=1296, height=972, binning=2))

        self.assertEqual(
            [
                ("stop_preview",),
                ("apply_acquisition_settings", 1296, 972, 2),
                ("start_preview",),
            ],
            handle.calls,
        )
        self.assertTrue(controller.is_previewing)

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

    def test_pypylon_start_preview_normalizes_free_run_before_grabbing(self):
        from automatic_transfer_station.camera_controller import PypylonCameraHandle

        camera = FreeRunRequiredCamera()
        handle = PypylonCameraHandle(FakePylonModule(), camera)

        handle.start_preview()

        start_index = camera.calls.index(("StartGrabbing", FakePylonModule.GrabStrategy_LatestImageOnly))
        self.assertEqual("Continuous", camera.AcquisitionMode.value)
        self.assertEqual("Off", camera.TriggerMode.value)
        self.assertLess(camera.AcquisitionMode.set_values.index("Continuous"), start_index)
        self.assertLess(camera.TriggerMode.set_values.index("Off"), start_index)

    def test_pypylon_single_grab_normalizes_free_run_before_grabbing(self):
        from automatic_transfer_station.camera_controller import PypylonCameraHandle

        camera = FreeRunRequiredCamera()
        handle = PypylonCameraHandle(FakePylonModule(), camera)

        frame = handle.grab_rgb(timeout_ms=250)

        start_index = camera.calls.index(("StartGrabbingMax", 1))
        self.assertEqual(2, frame.width)
        self.assertEqual(1, frame.height)
        self.assertEqual("Continuous", camera.AcquisitionMode.value)
        self.assertEqual("Off", camera.TriggerMode.value)
        self.assertLess(camera.AcquisitionMode.set_values.index("Continuous"), start_index)
        self.assertLess(camera.TriggerMode.set_values.index("Off"), start_index)

    def test_pypylon_single_grab_stops_camera_when_retrieve_times_out(self):
        from automatic_transfer_station.camera_controller import PypylonCameraHandle

        camera = RetrieveTimeoutCamera()
        handle = PypylonCameraHandle(FakePylonModule(), camera)

        with self.assertRaisesRegex(RuntimeError, "simulated retrieve timeout"):
            handle.grab_rgb(timeout_ms=250)

        self.assertIn(("StopGrabbing",), camera.calls)
        self.assertFalse(camera.grabbing)
        self.assertFalse(handle._single_grab_started)

    def test_pypylon_free_run_setting_failure_is_reported_before_grabbing(self):
        from automatic_transfer_station.camera_controller import CameraError, PypylonCameraHandle

        camera = FreeRunRequiredCamera()
        camera.TriggerMode = RejectingFeature("On")
        handle = PypylonCameraHandle(FakePylonModule(), camera)

        with self.assertRaisesRegex(CameraError, "TriggerMode"):
            handle.start_preview()

        self.assertNotIn(("StartGrabbing", FakePylonModule.GrabStrategy_LatestImageOnly), camera.calls)


if __name__ == "__main__":
    unittest.main()
