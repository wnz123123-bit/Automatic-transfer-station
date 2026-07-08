import os
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


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
        from automatic_transfer_station.camera_controller import CameraCapabilities, CameraDeviceInfo, CameraFrame, FeatureRange

        self.calls = []
        self.device = CameraDeviceInfo("puA2500-14ucMIC", "25141247", "BaslerUsb")
        self.frame = CameraFrame(
            width=2,
            height=2,
            rgb_bytes=bytes([
                1, 0, 0, 2, 0, 0,
                3, 0, 0, 4, 0, 0,
            ]),
        )
        self.capabilities_value = CameraCapabilities(
            sensor_width=2592,
            sensor_height=1944,
            width=FeatureRange(4, 2592, 2),
            height=FeatureRange(4, 1944, 2),
            binning=FeatureRange(1, 4, 1),
            exposure_ms=FeatureRange(0.01, 1000.0, None),
            gain=FeatureRange(0.0, 24.0, None),
            gamma=FeatureRange(0.25, 2.0, None),
            sharpness=FeatureRange(0.0, 1.0, None),
        )
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

    def set_image_adjustment(self, gamma=None, sharpness=None):
        self.calls.append(("set_image_adjustment", gamma, sharpness))

    def apply_acquisition_settings(self, settings):
        self.calls.append(("apply_acquisition_settings", settings.width, settings.height, settings.binning))

    def grab_one_with_settings(self, snap_settings, restore_settings=None, timeout_ms=1000):
        self.calls.append(
            (
                "grab_one_with_settings",
                snap_settings.width,
                snap_settings.height,
                snap_settings.binning,
                getattr(restore_settings, "binning", None),
                timeout_ms,
            )
        )
        return self.frame

    def capabilities(self):
        self.calls.append(("capabilities",))
        return self.capabilities_value

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
            settings_path=self.snapshot_dir / "camera_settings.json",
            calibration_path=self.snapshot_dir / "camera_scale_calibration.json",
        )
        self._panels.append(panel)
        return panel, controls

    def test_connect_worker_does_not_read_qt_controls(self):
        from automatic_transfer_station.camera_panel import CameraPanel

        class DeferredExecutor:
            def __init__(self):
                self.action = None
                self.in_worker = False

            def submit(self, kind, action, on_done, on_error):
                self.action = action

            def wait_for_idle(self, timeout_ms=5000):
                return None

            def is_busy(self):
                return False

        controller = FakeCameraController()
        executor = DeferredExecutor()
        panel = CameraPanel(
            controller_factory=lambda: controller,
            task_executor=executor,
            snapshot_dir=self.snapshot_dir,
            settings_path=self.snapshot_dir / "camera_settings.json",
            calibration_path=self.snapshot_dir / "camera_scale_calibration.json",
        )
        self._panels.append(panel)
        original_binning_value = panel._binning_value

        def guarded_binning_value(name):
            if executor.in_worker:
                raise AssertionError("connect worker must not read Qt widgets")
            return original_binning_value(name)

        panel._binning_value = guarded_binning_value
        executor.in_worker = True
        try:
            executor.action()
        finally:
            executor.in_worker = False

    def test_auto_connect_lists_and_opens_preferred_camera(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)

        self.assertIn(("list_devices",), controller.calls)
        self.assertIn(("open", "25141247"), controller.calls)
        self.assertIn(("capabilities",), controller.calls)
        self.assertIn(("set_exposure", False, 10.0), controller.calls)
        self.assertIn("puA2500-14ucMIC", controls["cameraDeviceLabel"].text())
        self.assertIn("25141247", controls["cameraDeviceLabel"].text())
        self.assertNotIn("cameraConnect", controls)
        self.assertEqual("预览", controls["cameraPreview"].text())
        self.assertEqual("拍照", controls["cameraSnapshot"].text())
        self.assertFalse(controls["cameraExposureAuto"].isChecked())
        self.assertTrue(controls["cameraExposureSpin"].isEnabled())

    def test_controller_is_created_when_panel_starts(self):
        from automatic_transfer_station.camera_panel import CameraPanel, InlineCameraExecutor

        controller = FakeCameraController()
        created = []
        panel = CameraPanel(
            controller_factory=lambda: created.append(controller) or controller,
            task_executor=InlineCameraExecutor(),
            snapshot_dir=self.snapshot_dir,
            settings_path=self.snapshot_dir / "camera_settings.json",
            calibration_path=self.snapshot_dir / "camera_scale_calibration.json",
        )
        self._panels.append(panel)

        self.assertEqual([controller], created)
        self.assertIn(("open", "25141247"), controller.calls)

    def test_exposure_and_gain_controls_delegate_to_controller(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)

        controls["cameraExposureAuto"].setChecked(False)
        controls["cameraExposureSpin"].setValue(12.5)
        panel.apply_exposure()
        controls["cameraGainAuto"].setChecked(False)
        controls["cameraGainSpin"].setValue(3.0)
        panel.apply_gain()

        self.assertIn(("set_exposure", False, 12.5), controller.calls)
        self.assertIn(("set_gain", False, 3.0), controller.calls)

    def test_camera_panel_uses_workspace_layout_with_right_tool_groups(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)

        self.assertIn("cameraWorkspace", controls)
        self.assertIn("cameraPreviewPane", controls)
        self.assertIn("cameraControlPane", controls)
        self.assertTrue(controls["cameraImageLabel"].isAncestorOf(controls["cameraOverlayStatusLabel"]) is False)
        for name in (
            "cameraAcquireGroup",
            "cameraSaveGroup",
            "cameraQualityGroup",
            "cameraExposureGroup",
            "cameraScaleGroup",
            "cameraDisplayGroup",
            "cameraMeasurementGroup",
            "cameraMeasurementResultsGroup",
        ):
            self.assertIn(name, controls)
            self.assertTrue(controls["cameraControlPane"].isAncestorOf(controls[name]))

        for name in (
            "cameraCrosshairToggle",
            "cameraMeasureLine",
            "cameraMeasurePolygon",
            "cameraFinishPolygon",
            "cameraUndoMeasurement",
            "cameraClearMeasurements",
            "cameraDeleteMeasurement",
            "cameraIncludeAnnotations",
            "cameraMeasurementTable",
        ):
            self.assertIn(name, controls)
        self.assertTrue(controls["cameraIncludeAnnotations"].isChecked())

    def test_quality_controls_show_pixel_resolution_not_binning(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)

        self.assertIn("cameraLiveResolutionCombo", controls)
        self.assertIn("cameraSnapResolutionCombo", controls)
        self.assertEqual("1296 x 972 px", controls["cameraLiveResolutionCombo"].currentText())
        self.assertEqual("2592 x 1944 px", controls["cameraSnapResolutionCombo"].currentText())
        self.assertNotIn("cameraLiveBinningCombo", controls)
        self.assertNotIn("cameraSnapBinningCombo", controls)

    def test_resolution_options_use_sensor_size_when_width_range_is_binning_dependent(self):
        from automatic_transfer_station.camera_controller import CameraCapabilities, FeatureRange

        controller = FakeCameraController()
        controller.capabilities_value = CameraCapabilities(
            sensor_width=2592,
            sensor_height=1944,
            width=FeatureRange(4, 1296, 2),
            height=FeatureRange(4, 972, 2),
            binning=FeatureRange(1, 4, 1),
        )

        panel, controls = self._make_panel(controller)

        live_options = [controls["cameraLiveResolutionCombo"].itemText(i) for i in range(4)]
        self.assertEqual(
            ["2592 x 1944 px", "1296 x 972 px", "864 x 648 px", "648 x 486 px"],
            live_options,
        )

        controls["cameraSnapResolutionCombo"].setCurrentText("2592 x 1944 px")
        controls["cameraLiveResolutionCombo"].setCurrentText("1296 x 972 px")
        panel.save_snapshot(now=lambda: "20260701_120000")
        self.assertIn(("grab_one_with_settings", 2592, 1944, 1, 2, 5000), controller.calls)

    def test_scale_calibration_settings_are_opened_from_popup_button(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)

        self.assertIn("cameraOpenScaleCalibration", controls)
        self.assertNotIn("cameraCalibrationLengthSpin", controls)
        self.assertEqual("标定...", controls["cameraOpenScaleCalibration"].text())

        dialog = panel.create_scale_calibration_dialog()
        try:
            self.assertEqual("cameraScaleCalibrationDialog", dialog.objectName())
            self.assertIn("cameraCalibrationLengthSpin", controls)
            self.assertIn("cameraStartScaleCalibration", controls)
        finally:
            dialog.close()

    def test_snapshot_uses_template_tiff_and_snap_quality_settings(self):
        from PySide6.QtGui import QImage

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        controls["cameraSaveTemplateEdit"].setText("{date}_{time}_{objective}_{index}")
        controls["cameraFormatCombo"].setCurrentText("TIFF")
        controls["cameraObjectiveCombo"].setCurrentText("20X")
        controls["cameraSnapResolutionCombo"].setCurrentText("2592 x 1944 px")
        controls["cameraLiveResolutionCombo"].setCurrentText("1296 x 972 px")

        path = panel.save_snapshot(now=lambda: "20260701_120000")
        image = QImage(str(path))

        self.assertEqual("20260701_120000_20X_001.tif", path.name)
        self.assertTrue(path.exists())
        self.assertFalse(image.isNull())
        self.assertIn(("grab_one_with_settings", 2592, 1944, 1, 2, 5000), controller.calls)

    def test_snapshot_while_previewing_stops_and_restarts_preview_around_grab(self):
        from automatic_transfer_station.camera_panel import PREVIEW_STOP_TIMEOUT_MS

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        panel._preview_thread = object()

        def fake_stop_preview(timeout_ms=PREVIEW_STOP_TIMEOUT_MS):
            controller.calls.append(("stop_preview_for_snapshot", timeout_ms))
            panel._preview_thread = None
            return True

        def fake_start_preview():
            controller.calls.append(("restart_preview_after_snapshot",))

        panel.stop_preview = fake_stop_preview
        panel.start_preview = fake_start_preview
        path = panel.save_snapshot(now=lambda: "20260701_120000")

        self.assertTrue(path.exists())
        stop_index = controller.calls.index(("stop_preview_for_snapshot", PREVIEW_STOP_TIMEOUT_MS))
        grab_index = next(i for i, call in enumerate(controller.calls) if call[0] == "grab_one_with_settings")
        restart_index = controller.calls.index(("restart_preview_after_snapshot",))
        self.assertLess(stop_index, grab_index)
        self.assertLess(grab_index, restart_index)

    def test_preview_stop_timeout_exceeds_grab_timeout(self):
        from automatic_transfer_station.camera_controller import DEFAULT_CAMERA_GRAB_TIMEOUT_MS
        from automatic_transfer_station.camera_panel import PREVIEW_STOP_TIMEOUT_MS

        self.assertGreater(PREVIEW_STOP_TIMEOUT_MS, DEFAULT_CAMERA_GRAB_TIMEOUT_MS)

    def test_preview_updates_image_label_and_stops_cleanly(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)

        panel.start_preview()

        self.assertTrue(wait_until(lambda: ("start_preview",) in controller.calls))
        self.assertTrue(wait_until(lambda: ("grab_one", 5000) in controller.calls))
        self.assertTrue(wait_until(lambda: controls["cameraImageLabel"].pixmap() is not None))

        panel.stop_preview()
        self.assertTrue(wait_until(lambda: ("stop_preview",) in controller.calls))
        self.assertTrue(panel.prepare_for_close(timeout_ms=2000))

    def test_preview_thread_throttles_fast_camera_grabs(self):
        from automatic_transfer_station.camera_panel import CameraPreviewThread, DEFAULT_CAMERA_PREVIEW_FPS

        controller = FakeCameraController()
        thread = CameraPreviewThread(controller, timeout_ms=10)
        try:
            thread.start()
            time.sleep(0.25)
            thread.request_stop()
            self.assertTrue(thread.wait(2000))
        finally:
            if thread.isRunning():
                thread.request_stop()
                thread.wait(2000)

        grab_count = sum(1 for call in controller.calls if call == ("grab_one", 10))
        self.assertEqual(60, DEFAULT_CAMERA_PREVIEW_FPS)
        self.assertGreaterEqual(grab_count, 8)
        self.assertLessEqual(grab_count, 18)

    def test_preview_thread_emits_to_gui_at_render_rate(self):
        from automatic_transfer_station.camera_panel import CameraPreviewThread, DEFAULT_CAMERA_RENDER_FPS

        controller = FakeCameraController()
        thread = CameraPreviewThread(controller, timeout_ms=10, frame_interval_ms=1)
        emitted = []
        thread.frame_ready.connect(lambda frame, fps: emitted.append((frame, fps)))
        try:
            thread.start()
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                get_app().processEvents()
                time.sleep(0.005)
            thread.request_stop()
            self.assertTrue(thread.wait(2000))
            get_app().processEvents()
        finally:
            if thread.isRunning():
                thread.request_stop()
                thread.wait(2000)

        grab_count = sum(1 for call in controller.calls if call == ("grab_one", 10))
        self.assertEqual(30, DEFAULT_CAMERA_RENDER_FPS)
        self.assertGreater(grab_count, len(emitted))
        self.assertLessEqual(len(emitted), 10)

    def test_preview_header_shows_live_fps(self):
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        frame = CameraFrame(width=100, height=50, rgb_bytes=bytes([10, 20, 30]) * 5000)

        self.assertIn("cameraFpsLabel", controls)
        self.assertIn("--", controls["cameraFpsLabel"].text())

        panel._handle_frame(frame, 59.6)

        self.assertIn("FPS", controls["cameraFpsLabel"].text())
        self.assertIn("59.6", controls["cameraFpsLabel"].text())

    def test_preview_frames_are_coalesced_before_rendering(self):
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, _controls = self._make_panel(controller)
        frame1 = CameraFrame(width=1, height=1, rgb_bytes=b"\x01\x00\x00")
        frame2 = CameraFrame(width=1, height=1, rgb_bytes=b"\x02\x00\x00")
        rendered = []

        with patch.object(panel, "_render_frame", side_effect=lambda frame: rendered.append(frame)):
            panel._handle_frame(frame1, 30.0)
            panel._handle_frame(frame2, 60.0)
            self.assertEqual([], rendered)

            get_app().processEvents()

        self.assertEqual([frame2], rendered)
        self.assertIs(frame2, panel._last_frame)
        self.assertIn("60.0", panel.controls["cameraFpsLabel"].text())

    def test_preview_rendering_is_throttled_below_capture_rate(self):
        from automatic_transfer_station.camera_controller import CameraFrame
        from automatic_transfer_station.camera_panel import DEFAULT_CAMERA_RENDER_FPS

        controller = FakeCameraController()
        panel, _controls = self._make_panel(controller)
        frame1 = CameraFrame(width=1, height=1, rgb_bytes=b"\x01\x00\x00")
        frame2 = CameraFrame(width=1, height=1, rgb_bytes=b"\x02\x00\x00")
        rendered = []

        with patch.object(panel, "_render_frame", side_effect=lambda frame: rendered.append(frame)):
            panel._handle_frame(frame1, 60.0)
            self.assertTrue(wait_until(lambda: rendered == [frame1]))

            panel._handle_frame(frame2, 60.0)
            get_app().processEvents()

            self.assertEqual(30, DEFAULT_CAMERA_RENDER_FPS)
            self.assertEqual([frame1], rendered)
            self.assertTrue(wait_until(lambda: rendered == [frame1, frame2], timeout=1.0))

    def test_preview_zoom_label_and_mouse_wheel_adjust_display_scale(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        frame = CameraFrame(width=100, height=50, rgb_bytes=bytes([10, 20, 30]) * 5000)

        panel._handle_frame(frame)
        get_app().processEvents()

        self.assertIn("100%", controls["cameraZoomLabel"].text())
        self.assertEqual((200, 100), (label.pixmap().width(), label.pixmap().height()))

        wheel_up = QWheelEvent(
            QPointF(100, 100),
            QPointF(100, 100),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.ScrollUpdate,
            False,
        )
        get_app().sendEvent(label, wheel_up)

        self.assertIn("125%", controls["cameraZoomLabel"].text())
        self.assertEqual((250, 125), (label.pixmap().width(), label.pixmap().height()))

    def test_center_crosshair_toggle_does_not_track_mouse_position(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        frame = CameraFrame(width=100, height=50, rgb_bytes=bytes([0, 0, 0]) * 5000)

        panel.show()
        get_app().processEvents()
        panel._handle_frame(frame)
        self.assertTrue(wait_until(lambda: label.pixmap() is not None))

        self.assertEqual(False, controls["cameraCrosshairToggle"].isChecked())
        controls["cameraCrosshairToggle"].click()
        get_app().processEvents()
        first_status = controls["cameraMeasurementStatusLabel"].text()
        QTest.mouseMove(label, QPoint(150, 150), delay=1)
        get_app().processEvents()

        self.assertEqual(True, controls["cameraCrosshairToggle"].isChecked())
        self.assertEqual(first_status, controls["cameraMeasurementStatusLabel"].text())
        self.assertIn("X 50.0 px", first_status)
        self.assertIn("Y 25.0 px", first_status)

    def test_line_measurement_clicks_add_result_row(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame
        from automatic_transfer_station.camera_settings import ScaleCalibration

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        panel.scale_calibrations["10X"] = ScaleCalibration(
            objective="10X",
            um_per_pixel=2.0,
            known_um=100.0,
            pixel_distance=50.0,
            p1=(0, 0),
            p2=(50, 0),
            calibrated_at="2026-07-02T10:00:00",
        )
        panel._update_scale_status()
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        frame = CameraFrame(width=100, height=100, rgb_bytes=bytes([0, 0, 0]) * 10000)

        panel.show()
        get_app().processEvents()
        panel._handle_frame(frame)
        self.assertTrue(wait_until(lambda: label.pixmap() is not None))
        controls["cameraMeasureLine"].click()
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(120, 20))

        table = controls["cameraMeasurementTable"]
        self.assertEqual(1, table.rowCount())
        self.assertEqual("line", panel._measurements[0].kind)
        self.assertIn("100.0 um", table.item(0, 2).text())

    def test_line_measurement_tracks_cursor_preview_after_first_point(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        frame = CameraFrame(width=100, height=100, rgb_bytes=bytes([0, 0, 0]) * 10000)

        panel.show()
        get_app().processEvents()
        panel._handle_frame(frame)
        self.assertTrue(wait_until(lambda: label.pixmap() is not None))
        controls["cameraMeasureLine"].click()
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        QTest.mouseMove(label, QPoint(120, 20), delay=1)
        get_app().processEvents()

        self.assertEqual((60.0, 10.0), getattr(panel, "_active_measurement_preview_point", None))
        self.assertIsNone(getattr(panel, "_crosshair_point", None))

        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(120, 20))

        self.assertIsNone(getattr(panel, "_active_measurement_preview_point", None))

    def test_polygon_measurement_finish_updates_result_row(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame
        from automatic_transfer_station.camera_settings import ScaleCalibration

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        panel.scale_calibrations["10X"] = ScaleCalibration(
            objective="10X",
            um_per_pixel=1.0,
            known_um=100.0,
            pixel_distance=100.0,
            p1=(0, 0),
            p2=(100, 0),
            calibrated_at="2026-07-02T10:00:00",
        )
        panel._update_scale_status()
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        frame = CameraFrame(width=100, height=100, rgb_bytes=bytes([0, 0, 0]) * 10000)

        panel.show()
        get_app().processEvents()
        panel._handle_frame(frame)
        self.assertTrue(wait_until(lambda: label.pixmap() is not None))
        controls["cameraMeasurePolygon"].click()
        for point in (QPoint(20, 20), QPoint(120, 20), QPoint(120, 80), QPoint(20, 80)):
            QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, point)
        controls["cameraFinishPolygon"].click()

        table = controls["cameraMeasurementTable"]
        self.assertEqual(1, table.rowCount())
        self.assertEqual("polygon", panel._measurements[0].kind)
        self.assertIn("1500.0 um^2", table.item(0, 2).text())

    def test_measurement_undo_clear_and_delete_update_state(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        frame = CameraFrame(width=100, height=100, rgb_bytes=bytes([0, 0, 0]) * 10000)

        panel.show()
        get_app().processEvents()
        panel._handle_frame(frame)
        self.assertTrue(wait_until(lambda: label.pixmap() is not None))
        controls["cameraMeasurePolygon"].click()
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(120, 20))
        controls["cameraUndoMeasurement"].click()
        self.assertEqual([(10.0, 10.0)], panel._active_measurement_points)

        controls["cameraClearMeasurements"].click()
        self.assertEqual([], panel._active_measurement_points)
        self.assertEqual([], panel._measurements)

        controls["cameraMeasureLine"].click()
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(120, 20))
        controls["cameraMeasurementTable"].selectRow(0)
        controls["cameraDeleteMeasurement"].click()

        self.assertEqual([], panel._measurements)
        self.assertEqual(0, controls["cameraMeasurementTable"].rowCount())

    def test_snapshot_writes_measurement_sidecar(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame
        from automatic_transfer_station.camera_settings import ScaleCalibration

        controller = FakeCameraController()
        controller.frame = CameraFrame(width=100, height=100, rgb_bytes=bytes([0, 0, 0]) * 10000)
        panel, controls = self._make_panel(controller)
        panel.scale_calibrations["10X"] = ScaleCalibration(
            objective="10X",
            um_per_pixel=2.0,
            known_um=100.0,
            pixel_distance=50.0,
            p1=(0, 0),
            p2=(50, 0),
            calibrated_at="2026-07-02T10:00:00",
        )
        panel._update_scale_status()
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)

        panel.show()
        get_app().processEvents()
        panel._handle_frame(controller.frame)
        self.assertTrue(wait_until(lambda: label.pixmap() is not None))
        controls["cameraCrosshairToggle"].click()
        controls["cameraMeasureLine"].click()
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(120, 20))
        get_app().processEvents()

        path = panel.save_snapshot(now=lambda: "20260701_120000")
        sidecar = path.with_suffix(".measurements.json")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))

        self.assertTrue(sidecar.exists())
        self.assertEqual(1, payload["schema_version"])
        self.assertEqual("10X", payload["objective"])
        self.assertEqual("100.0 um", payload["measurements"][0]["result"]["display"])
        self.assertEqual({"x": 50.0, "y": 50.0}, payload["crosshair"])

    def test_snapshot_saves_latest_frame_to_snapshot_directory(self):
        controller = FakeCameraController()
        panel, _controls = self._make_panel(controller)
        panel._last_frame = controller.frame

        path = panel.save_snapshot()

        self.assertEqual(self.snapshot_dir, path.parent)
        self.assertTrue(path.name.endswith("_10X_001.tif"))
        self.assertEqual(".tif", path.suffix)
        self.assertTrue(path.exists())

    def test_calibration_workflow_saves_um_per_pixel_for_current_objective(self):
        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        controls["cameraObjectiveCombo"].setCurrentText("20X")
        dialog = panel.create_scale_calibration_dialog()
        self.addCleanup(dialog.close)
        controls["cameraCalibrationLengthSpin"].setValue(100.0)

        panel.start_scale_calibration()
        panel.record_calibration_point(10, 20)
        panel.record_calibration_point(110, 20)

        calibration = panel.scale_calibrations["20X"]
        self.assertAlmostEqual(1.0, calibration.um_per_pixel)
        self.assertIn("1", controls["cameraScaleStatusLabel"].text())

    def test_scale_calibration_accepts_preview_mouse_clicks(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        frame = CameraFrame(width=100, height=100, rgb_bytes=bytes([0, 0, 0]) * 10000)
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        controls["cameraObjectiveCombo"].setCurrentText("50X")
        dialog = panel.create_scale_calibration_dialog()
        self.addCleanup(dialog.close)
        controls["cameraCalibrationLengthSpin"].setValue(100.0)

        panel.show()
        get_app().processEvents()
        panel._handle_frame(frame)
        get_app().processEvents()
        panel.start_scale_calibration()
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(20, 20))
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(120, 20))

        calibration = panel.scale_calibrations["50X"]
        self.assertAlmostEqual(2.0, calibration.um_per_pixel)

    def test_preview_click_mapping_uses_zoomed_pixmap_geometry(self):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        frame = CameraFrame(width=100, height=50, rgb_bytes=bytes([0, 0, 0]) * 5000)
        label = controls["cameraImageLabel"]
        label.setFixedSize(200, 200)
        clicked = []
        label.image_clicked.connect(lambda x, y: clicked.append((x, y)))

        panel.show()
        get_app().processEvents()
        panel._handle_frame(frame)

        panel._set_image_zoom(1.25)
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(100, 100))
        self.assertAlmostEqual(50.0, clicked[-1][0], places=1)
        self.assertAlmostEqual(25.0, clicked[-1][1], places=1)

        panel._set_image_zoom(0.5)
        QTest.mouseClick(label, Qt.LeftButton, Qt.NoModifier, QPoint(100, 100))
        self.assertAlmostEqual(50.0, clicked[-1][0], places=1)
        self.assertAlmostEqual(25.0, clicked[-1][1], places=1)

    def test_processed_image_uses_qt_flip_path(self):
        from automatic_transfer_station.camera_controller import CameraFrame

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        controls["cameraFlipHorizontal"].setChecked(True)
        controls["cameraFlipVertical"].setChecked(True)
        frame = CameraFrame(
            width=2,
            height=2,
            rgb_bytes=bytes([
                1, 0, 0, 2, 0, 0,
                3, 0, 0, 4, 0, 0,
            ]),
        )

        with patch("automatic_transfer_station.camera_panel.flip_frame", side_effect=AssertionError("slow path")):
            image = panel._processed_image(frame, with_scale=False)

        self.assertEqual(4, image.pixelColor(0, 0).red())
        self.assertEqual(3, image.pixelColor(1, 0).red())
        self.assertEqual(2, image.pixelColor(0, 1).red())
        self.assertEqual(1, image.pixelColor(1, 1).red())

    def test_preview_scaling_uses_fast_qimage_transform_for_responsiveness(self):
        from PySide6.QtCore import QSize, Qt

        class FakeImage:
            def __init__(self):
                self.scaled_args = None

            def size(self):
                return QSize(400, 200)

            def scaled(self, width, height, aspect_mode, transform_mode):
                self.scaled_args = (width, height, aspect_mode, transform_mode)
                return self

        controller = FakeCameraController()
        panel, controls = self._make_panel(controller)
        controls["cameraImageLabel"].setFixedSize(200, 200)
        image = FakeImage()

        panel._scaled_image_for_label(image, controls["cameraImageLabel"])

        self.assertEqual((200, 100, Qt.KeepAspectRatio, Qt.FastTransformation), image.scaled_args)

    def test_connect_failure_is_reported_without_raising_to_gui(self):
        from automatic_transfer_station.camera_controller import CameraError

        class FailingController(FakeCameraController):
            def list_devices(self):
                raise CameraError("pypylon is not installed")

        controller = FailingController()
        panel, controls = self._make_panel(controller)

        self.assertIn("pypylon is not installed", controls["cameraStatusLabel"].text())
        self.assertNotIn("cameraConnect", controls)
        self.assertEqual("预览", controls["cameraPreview"].text())
        self.assertEqual("拍照", controls["cameraSnapshot"].text())

    def test_camera_panel_translates_to_english(self):
        from automatic_transfer_station.i18n import translator

        translator.set_language("en")
        try:
            controller = FakeCameraController()
            _panel, controls = self._make_panel(controller)

            self.assertEqual("Camera preview", controls["cameraTitleLabel"].text())
            self.assertEqual("Preview FPS: --", controls["cameraFpsLabel"].text())
            self.assertEqual("Image zoom: 100%", controls["cameraZoomLabel"].text())
            self.assertEqual("Measurement", controls["cameraMeasurementGroup"].title())
            self.assertEqual("Center crosshair", controls["cameraCrosshairToggle"].text())
            self.assertEqual("Line distance", controls["cameraMeasureLine"].text())
            self.assertEqual("Area", controls["cameraMeasurePolygon"].text())
            self.assertEqual("Measurement results", controls["cameraMeasurementResultsGroup"].title())
            self.assertNotIn("cameraConnect", controls)
            self.assertEqual("Preview", controls["cameraPreview"].text())
            self.assertEqual("Capture", controls["cameraSnapshot"].text())
        finally:
            translator.initialize("zh")


if __name__ == "__main__":
    unittest.main()
