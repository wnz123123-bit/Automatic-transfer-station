"""Window integration tests use only in-memory camera/motion fakes."""
import json
import os
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QThread, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialogButtonBox, QWidget

from automatic_transfer_station.camera_controller import CameraDeviceInfo, CameraFrame
from automatic_transfer_station.cutting_geometry import CalibrationContext, CuttingCalibration
from automatic_transfer_station.cutting_session import FrameEnvelope
from automatic_transfer_station.cutting_window import CuttingWindow
from automatic_transfer_station.i18n import translator


class FakeMotion:
    def __init__(self):
        self.motion_revision = 0
        self.positions = {axis: 0.0 for axis in range(1, 10)}
        self.pose = (0.0, 0.0)
        self.owner = None
        self.qualified = False
        self.moves = []
        self.stops = 0
        self.hold_motion = False
        self.call_threads = []
        self.is_open = True

    def acquire_cutting(self, owner):
        self.call_threads.append(threading.get_ident())
        if self.owner is not None:
            raise RuntimeError("lease already held")
        self.owner = owner

    def release_cutting(self, owner):
        if self.owner is not owner:
            raise RuntimeError("stale owner")
        self.owner = None

    def get_cutting_reference(self):
        self.call_threads.append(threading.get_ident())
        return dict(self.positions)

    def get_cutting_pose(self):
        return self.pose

    def validate_cutting_path(self, owner, targets):
        assert self.owner is owner
        assert targets

    def start_cutting_move(self, owner, target, speed):
        assert self.owner is owner
        if not self.qualified:
            raise RuntimeError("coordinated cutting is not board-qualified")
        self.moves.append(target)
        self.motion_revision += 1
        self.pose = target
        self.positions[5], self.positions[6] = target

    def cutting_move_status(self, owner):
        if self.owner is not owner:
            raise RuntimeError("cancelled owner")
        return not self.hold_motion

    def stop_cutting(self, owner):
        if self.owner is owner:
            self.stops += 1
            self.owner = None
            self.motion_revision += 1

    def set_cutting_qualified(self, qualified):
        self.call_threads.append(threading.get_ident())
        self.qualified = qualified
        self.motion_revision += 1


class FakeCamera(QWidget):
    raw_frame_ready = Signal(object)

    def __init__(self):
        super().__init__()
        self.controller = SimpleNamespace(current_device=CameraDeviceInfo("Fake", "serial-1"))
        self._current_objective = "20X"
        self.cutting_reference_revision = 0
        self.capture_gate = None
        self.capture_entered = threading.Event()
        self.capture_threads = []
        self.preview_calls = 0

    def start_preview(self):
        self.preview_calls += 1

    def frame(self):
        return CameraFrame(100, 80, bytes([30, 40, 50]) * 8000,
                           {"capture_started_monotonic": time.monotonic()}, 1)

    def capture_cutting_reference(self):
        self.capture_threads.append(threading.get_ident())
        revision = self.cutting_reference_revision
        context = CalibrationContext("serial-1", self._current_objective, 100, 80, 1)
        self.capture_entered.set()
        if self.capture_gate is not None:
            assert self.capture_gate.wait(3), "test failed to release capture"
        return FrameEnvelope(self.frame(), context, time.monotonic(), revision)


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert predicate(), "Qt condition did not become true"


@pytest.fixture
def window(app, tmp_path):
    camera, controller = FakeCamera(), FakeMotion()
    widget = CuttingWindow(controller, camera, storage_dir=tmp_path / "cutting")
    widget.show()
    app.processEvents()
    yield widget
    if camera.capture_gate is not None:
        camera.capture_gate.set()
    widget.abort()
    wait_for(lambda: not widget.busy)
    wait_for(lambda: widget.prepare_for_close(50))
    widget.close()
    camera.close()
    widget.deleteLater()
    camera.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def capture(window):
    window.capture()
    wait_for(lambda: not window.busy)
    assert window.snapshot is not None, window.status_label.text()
    return window.snapshot


def select(window, mode, point):
    window.mode_combo.setCurrentIndex(window.mode_combo.findData(mode))
    if mode == "laser":
        window.canvas.set_laser_point(point)
    window.canvas.point_selected.emit(point)


def planned(window):
    snapshot = capture(window)
    calibration = CuttingCalibration(((1, 0), (0, 1)), (10, 10), (50, 40),
                                     snapshot.context, 0, 0, 1, 3, "test-fit")
    window._accept_calibration(calibration, snapshot)
    window.canvas.set_roi((0, 0, 100, 80))
    window.canvas.set_line(((20, 20), (40, 30)))
    window.make_plan()
    assert window.plan is not None, window.status_label.text()
    return window.plan


def confirm(window):
    dialog = window._confirmation
    assert dialog is not None
    checkbox = dialog.findChild(QCheckBox, "confirmationCheck")
    buttons = dialog.findChild(QDialogButtonBox)
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert not checkbox.isChecked()
    assert not ok.isEnabled()
    checkbox.setChecked(True)
    QTest.mouseClick(ok, Qt.MouseButton.LeftButton)


def test_empty_launch_shared_camera_and_guard_do_not_touch_hardware(window):
    assert not window.busy and window.snapshot is None
    assert window.controller.call_threads == []
    assert not window.storage_dir.exists()
    window.open_camera()
    assert window.camera_panel.isVisible()
    window.camera_panel.raw_frame_ready.emit(window.camera_panel.frame())
    window._refresh_live_view()
    assert not window.live_view.canvas.image_rect().isEmpty()
    assert window.canvas.image_rect().isEmpty()
    assert not window.mode_combo.isEnabled()
    window.start_guard = lambda: False
    window.capture()
    assert not window.busy and not window.camera_panel.capture_threads
    assert not window.controller.call_threads


def test_setup_navigation_and_disclosures_do_not_change_plan_or_authorize_devices(window):
    plan = planned(window)
    snapshot, calibration = window.snapshot, window.calibration
    calls = list(window.controller.call_threads)
    for language in ("en", "zh"):
        translator.set_language(language)
        for page in (1, 0):
            window.sidebar_tabs.setCurrentIndex(page)
            for section in (window.calibration_section, window.backlash_section, window.motion_section):
                section.set_expanded(True)
                section.set_expanded(False)
            QApplication.processEvents()
            assert window.plan is plan and window.snapshot is snapshot and window.calibration is calibration
            assert not window.laser_panel.startup_confirmed and window.qualification is None
            assert window.controller.call_threads == calls
            assert window.live_run_button.visibleRegion().boundingRect().size() == window.live_run_button.size()
            assert window.abort_button.isEnabled() and not window.live_run_button.isEnabled()


def test_visible_drawing_tools_follow_pattern_and_busy_guard(window):
    capture(window)
    window.roi_tool.click()
    assert window.mode_combo.currentData() == "roi" and window.roi_tool.isChecked()
    window.draw_tool.click()
    assert window.mode_combo.currentData() == "line" and window.draw_tool.isChecked()
    window.pattern_panel.set_mode("dots")
    window.roi_tool.click()
    window.draw_tool.click()
    assert window.mode_combo.currentData() == "polygon" and window.pattern_panel.pattern_mode == "dots"
    window.sidebar_tabs.setCurrentIndex(1)
    window.feature_tool.click()
    assert window.mode_combo.currentData() == "feature" and window.feature_tool.isChecked()
    window.sidebar_tabs.setCurrentIndex(0)
    assert window.mode_combo.currentData() == "none" and window.view_tool.isChecked()
    # The actual controls share the old snapshot guard; hiding setup cannot
    # leave a drawing shortcut active after its reference becomes stale.
    window._invalidate_snapshot("stale reference")
    for button in (*window.drawing_buttons.values(), window.draw_tool):
        assert not button.isEnabled()
    window.draw_tool.click()
    assert window.snapshot is None and not window.controller.moves


def test_sidebar_render_does_not_wait_for_controller_qualification(window, monkeypatch):
    planned(window)

    def locked_property(_):
        raise AssertionError("GUI must not read a lock-taking controller property")

    monkeypatch.setattr(FakeMotion, "cutting_qualified", property(locked_property), raising=False)
    window._refresh_sidebar_status()
    window._sync_controls()
    assert window.abort_button.isEnabled()
    assert not window.live_run_button.isEnabled()


def preview_frame(window, color):
    return replace(window.camera_panel.frame(), rgb_bytes=bytes(color) * 8000,
                   metadata={"capture_started_monotonic": time.monotonic(),
                             "capture_finished_monotonic": time.monotonic()})


def test_live_view_preserves_reference_photo_path_and_editing_controls(window):
    plan = planned(window)
    snapshot, calibration = window.snapshot, window.calibration
    reference = window.canvas._image.copy()
    annotations = (window.canvas.roi, window.canvas.line, window.canvas.path_preview)
    assert window.image_tabs.currentWidget() is window.canvas
    window.show_live()
    window.camera_panel.raw_frame_ready.emit(preview_frame(window, (150, 80, 20)))
    window._refresh_live_view()
    assert window.live_view.canvas._image.pixelColor(10, 10).getRgb()[:3] == (150, 80, 20)
    assert window.snapshot is snapshot and window.plan is plan and window.calibration is calibration
    assert window.canvas._image == reference
    assert (window.canvas.roi, window.canvas.line, window.canvas.path_preview) == annotations
    window.draw_tool.click()
    assert window.image_tabs.currentWidget() is window.canvas
    assert window.mode_combo.currentData() == "line"
    assert not window.controller.moves


def test_live_frames_continue_during_real_dark_runner_and_reference_remains_frozen(window):
    plan = planned(window)
    snapshot = window.snapshot
    reference = window.canvas._image.copy()
    window.controller.qualified = True
    window.controller.hold_motion = True
    window.request_run()
    confirm(window)
    wait_for(lambda: bool(window.controller.moves))
    assert window.image_tabs.currentWidget() is window.live_view
    assert window.camera_panel.preview_calls == 1
    # MainWindow disables camera configuration during processing; frame delivery
    # and monitoring must continue even while that configuration panel is locked.
    window.camera_panel.setEnabled(False)
    for color in ((90, 20, 30), (40, 130, 180)):
        window.camera_panel.raw_frame_ready.emit(preview_frame(window, color))
        window._refresh_live_view()
        assert window.live_view.canvas._image.pixelColor(10, 10).getRgb()[:3] == color
        assert window.snapshot is snapshot and window.plan is plan
        assert window.canvas._image == reference
    assert not window.canvas._editable and not window.live_view.canvas._editable
    assert window.abort_button.isEnabled()
    window.image_tabs.setCurrentWidget(window.canvas)
    assert window.busy and not window.canvas._editable
    window.show_live()
    assert window.image_tabs.currentWidget() is window.live_view
    window.abort()
    wait_for(lambda: not window.busy)
    assert window.controller.stops >= 1
    assert window.snapshot is None


@pytest.mark.parametrize("kind", ["live-run", "raster-run"])
def test_all_execution_kinds_select_live_view_without_unlocking_reference(window, kind):
    planned(window)
    release = threading.Event()
    window._launch(kind, lambda: release.wait(3), lambda _: None)
    try:
        assert window.busy and window.image_tabs.currentWidget() is window.live_view
        window.camera_panel.raw_frame_ready.emit(preview_frame(window, (70, 150, 210)))
        window._refresh_live_view()
        assert window.live_view.canvas._image.pixelColor(10, 10).getRgb()[:3] == (70, 150, 210)
        assert window.abort_button.isEnabled() and not window.mode_combo.isEnabled()
        assert not window.live_view.canvas._editable
    finally:
        release.set()
        wait_for(lambda: not window.busy)


def test_live_view_discards_old_camera_connection_frames_and_closing_updates(window):
    old = preview_frame(window, (40, 130, 180))
    old = replace(old, metadata={**old.metadata, "capture_context": {
        "camera_serial": "serial-1", "objective": "20X", "source_revision": 0}})
    window.camera_panel.raw_frame_ready.emit(old)
    window._refresh_live_view()
    assert not window.live_view.canvas._image.isNull()
    window.camera_panel.controller.current_device = None
    window._tick()
    assert window.live_view.canvas._image.isNull()
    window.camera_panel.cutting_reference_revision += 1
    window.camera_panel.controller.current_device = CameraDeviceInfo("Fake", "serial-1")
    window.camera_panel.raw_frame_ready.emit(old)
    window._refresh_live_view()
    assert window.live_view.canvas._image.isNull()
    window.camera_panel.raw_frame_ready.emit(preview_frame(window, (80, 90, 100)))
    window._refresh_live_view()
    assert not window.live_view.canvas._image.isNull()
    assert window.prepare_for_close(50)
    window.camera_panel.raw_frame_ready.emit(preview_frame(window, (10, 20, 30)))
    assert window.live_view.canvas._image.isNull()


def test_bad_preview_pixels_do_not_change_reference_but_valid_optics_change_aborts(window, monkeypatch):
    from unittest.mock import Mock
    plan = planned(window)
    snapshot = window.snapshot
    off = Mock()
    monkeypatch.setattr(window.laser_panel, "emergency_off", off)
    release = threading.Event()
    window._launch("live-run", lambda: release.wait(3), lambda _: None)
    try:
        good = preview_frame(window, (40, 130, 180))
        window.camera_panel.raw_frame_ready.emit(good)
        window._refresh_live_view()
        assert not window.live_view.canvas._image.isNull()
        for bad in (replace(good, width=0), replace(good, width=101, rgb_bytes=b"bad")):
            window.camera_panel.raw_frame_ready.emit(bad)
            window._refresh_live_view()
            assert window.live_view.canvas._image.isNull()
            assert "图像不可用" in window.live_view.status_label.text()
            assert window.snapshot is snapshot and window.plan is plan
            assert not window._cancel.is_set()
            off.assert_not_called()
        # A coherent new frame that really changes image geometry still invokes
        # the original optics/reference safety guard, including OFF and cancel.
        changed = replace(good, width=101, rgb_bytes=bytes([40, 130, 180]) * 101 * 80)
        window.camera_panel.raw_frame_ready.emit(changed)
        assert window.snapshot is None and window.plan is None
        assert window._cancel.is_set()
        off.assert_called_once()
    finally:
        release.set()
        wait_for(lambda: not window.busy)


def test_capture_worker_gui_callbacks_and_new_still_clear_annotations(window):
    callback_threads, busy_values = [], []
    window.busy_changed.connect(lambda value: (callback_threads.append(QThread.currentThread()), busy_values.append(value)))
    first = capture(window)
    window.canvas.set_roi((10, 10, 60, 40))
    window.canvas.set_line(((20, 20), (40, 30)))
    second = capture(window)
    assert first is not second
    assert window.canvas.roi is None and window.canvas.line is None
    assert busy_values == [True, False, True, False]
    assert all(thread == QApplication.instance().thread() for thread in callback_threads)
    assert all(thread != threading.get_ident() for thread in window.camera_panel.capture_threads)
    assert all(thread != threading.get_ident() for thread in window.controller.call_threads)
    assert window.mode_combo.currentData() == "roi"


def test_fresh_capture_accepts_new_dimensions_after_preview_stops(window, monkeypatch):
    camera = window.camera_panel
    window.start_preview()
    camera.raw_frame_ready.emit(camera.frame())
    assert window.frames.latest.context == CalibrationContext("serial-1", "20X", 100, 80, 1)

    # Preview stops here: no further preview signals arrive. Changing camera
    # mode leaves its last delivered frame at 100x80 until a fresh still arrives.
    camera.cutting_reference_revision += 1
    revision = camera.cutting_reference_revision
    context = CalibrationContext("serial-1", "20X", 200, 160, 1)
    worker_threads = []

    def fresh_frame():
        worker_threads.append(threading.get_ident())
        frame = replace(camera.frame(), width=200, height=160,
                        rgb_bytes=bytes([30, 40, 50]) * 200 * 160)
        # Construct freshness evidence inside the acquisition callback, not
        # before capture_snapshot records the request's monotonic timestamp.
        return FrameEnvelope(frame, context, time.monotonic(), revision)

    monkeypatch.setattr(camera, "capture_cutting_reference", fresh_frame)
    snapshot = capture(window)
    assert snapshot.context == context
    assert snapshot.envelope.source_revision == revision
    assert snapshot.envelope.frame.width == 200
    assert snapshot.envelope.frame.height == 160
    assert worker_threads and all(worker != threading.get_ident() for worker in worker_threads)
    assert window.frames.latest.context.width == 100  # Mailbox still contains the old preview.
    assert window.mode_combo.isEnabled()
    point = (175.25, 125.5)  # Outside the old frame, inside the fresh full frame.
    assert window.canvas.widget_to_image(window.canvas.image_to_widget(point)) == pytest.approx(point)
    window._tick()
    probe = window._probe_thread
    wait_for(lambda: not probe.is_alive())
    QApplication.processEvents()
    window._drain_workers()
    assert window.snapshot is snapshot, window.status_label.text()


def test_export_preserves_raw_full_frame_and_fractional_roi_metadata(window):
    snapshot = capture(window)
    window.canvas.set_roi((10.25, 20.5, 30.5, 40.25))
    window.canvas.set_line(((20, 25), (30, 50)))
    window.canvas.set_laser_point((20, 30))
    window.canvas.set_crop_preview(True)
    window.save_snapshot()
    wait_for(lambda: not window.busy)
    files = list(window.storage_dir.glob("*/metadata.json"))
    assert len(files) == 1, window.status_label.text()
    metadata = json.loads(files[0].read_text(encoding="utf-8"))
    full = QImage(str(files[0].parent / "full.png"))
    roi = QImage(str(files[0].parent / "roi.png"))
    assert (full.width(), full.height()) == (100, 80)
    assert (roi.width(), roi.height()) == (31, 41)
    assert full.pixelColor(20, 30).getRgb()[:3] == (30, 40, 50)
    assert metadata["roi_full_frame_pixels"] == [10.25, 20.5, 30.5, 40.25]
    assert metadata["source_revision"] == snapshot.envelope.source_revision
    assert set(metadata["raw_axis_positions"]) == set(map(str, range(1, 10)))


def fit_three_poses(window):
    for stage, pixel in [((0, 0), (10, 10)), ((10, 0), (20, 10)), ((0, 10), (10, 20))]:
        window.controller.pose = stage
        window.controller.positions[5], window.controller.positions[6] = stage
        window.controller.motion_revision += 1
        capture(window)
        select(window, "feature", pixel)
    select(window, "laser", (50, 40))
    window.fit()
    wait_for(lambda: not window.busy)
    assert window.calibration is not None, window.status_label.text()
    return window.calibration


def test_three_pose_same_landmark_fit_persist_and_load(window):
    fit_three_poses(window)
    assert window.calibration.J[0] == pytest.approx((1, 0), abs=1e-10)
    assert window.calibration.J[1] == pytest.approx((0, 1), abs=1e-10)
    fitted = window.calibration
    assert (window.storage_dir / "calibration.json").is_file()
    window.reset_calibration()
    window.load_calibration()
    wait_for(lambda: not window.busy)
    assert window.calibration == fitted


@pytest.mark.parametrize("axis", [3, 7, 9])
def test_fixed_axis_move_after_fit_invalidates_calibration_on_new_capture(window, axis):
    fit_three_poses(window)
    window.canvas.set_roi((0, 0, 100, 80))
    window.canvas.set_line(((20, 20), (40, 30)))
    window.make_plan()
    assert window.plan is not None
    window.controller.positions[axis] += 0.5001
    window.controller.motion_revision += 1
    snapshot = capture(window)
    assert dict(snapshot.positions)[axis] == pytest.approx(0.5001)
    assert window.calibration is None
    assert window.samples == []
    assert window.plan is None
    assert window.canvas.laser_point is None
    assert window.calibration_fixed_axis_positions is None
    assert not window.plan_button.isEnabled()


def test_xy_sample_motion_keeps_calibration_and_original_fixed_axis_reference(window):
    fitted = fit_three_poses(window)
    reference = window.calibration_fixed_axis_positions
    samples = tuple(window.samples)
    window.controller.pose = (25.0, 30.0)
    window.controller.positions[5], window.controller.positions[6] = window.controller.pose
    window.controller.motion_revision += 1
    capture(window)
    assert window.calibration is fitted
    assert window.calibration_fixed_axis_positions == reference
    assert tuple(window.samples) == samples
    window.canvas.set_roi((0, 0, 100, 80))
    window.canvas.set_line(((20, 20), (40, 30)))
    window.make_plan()
    assert window.plan is not None, window.status_label.text()
    assert window.plan.reference_stage_um == (25.0, 30.0)


def test_fixed_axis_tolerance_does_not_rebase_after_each_capture_or_load(window):
    fitted = fit_three_poses(window)
    path = window.storage_dir / "calibration.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["fixed_axis_positions"] == {str(axis): 0.0 for axis in (1, 2, 3, 4, 7, 8, 9)}
    original_reference = window.calibration_fixed_axis_positions
    window.controller.positions[3] = 0.5  # Inclusive raw-pulse tolerance.
    window.controller.motion_revision += 1
    capture(window)
    assert window.calibration is fitted
    assert window.calibration_fixed_axis_positions == original_reference
    window.reset_calibration()
    window.load_calibration()
    wait_for(lambda: not window.busy)
    assert window.calibration == fitted
    assert window.calibration_fixed_axis_positions == original_reference
    # Another small step must compare with the stored original, not the 0.5 pose.
    window.controller.positions[3] = 0.75
    window.controller.motion_revision += 1
    capture(window)
    assert window.calibration is None


@pytest.mark.parametrize("reason", ["changed", "missing"])
def test_saved_fit_load_rejects_changed_or_missing_fixed_axis_provenance(window, reason):
    fit_three_poses(window)
    path = window.storage_dir / "calibration.json"
    if reason == "changed":
        window.controller.positions[3] = 2.0
        window.controller.motion_revision += 1
        capture(window)
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["fixed_axis_positions"]
        path.write_text(json.dumps(payload), encoding="utf-8")
        window.reset_calibration()
    window.load_calibration()
    wait_for(lambda: not window.busy)
    assert window.calibration is None
    assert window.calibration_fixed_axis_positions is None
    assert "固定轴位置" in window.status_label.text()


def test_sample_collection_drops_prior_fixed_pose_and_fit_rejects_mixed_records(window):
    old = capture(window)
    select(window, "feature", (10, 10))
    previous_sample = window.samples[0]
    window.controller.positions[3] = 2.0
    window.controller.motion_revision += 1
    current = capture(window)
    assert window.samples == []
    select(window, "feature", (10, 10))
    assert len(window.samples) == 1
    assert window.samples[0][0] is current
    assert window.samples[0][0] is not old
    assert dict(window.calibration_fixed_axis_positions)[3] == 2.0
    # Even if an upstream caller restores a stale record, fit must reject it
    # before geometry or persistence, rather than trusting collection-time checks.
    window.samples.append(previous_sample)
    select(window, "laser", (50, 40))
    window.fit()
    wait_for(lambda: not window.busy)
    assert window.calibration is None
    assert "固定轴位置" in window.status_label.text()
    assert not (window.storage_dir / "calibration.json").exists()
    select(window, "feature", (11, 11))
    assert window.samples == []  # Point collection independently rejects the mixed set.


def test_plan_has_no_fallback_and_preload_preview_can_leave_frame(window):
    capture(window)
    window.canvas.set_roi((0, 0, 100, 80))
    window.canvas.set_line(((90, 20), (80, 30)))
    window.make_plan()
    assert window.plan is None
    planned(window)
    window.canvas.set_line(((90, 20), (80, 30)))
    window.x_backlash_spin.setValue(40)
    window.make_plan()
    assert window.plan is not None, window.status_label.text()
    assert any(point[0] > 100 for a, b, _ in window.canvas.path_preview for point in (a, b))
    assert window.plan.reference_stage_um == window.snapshot.stage_um
    window.speed_spin.setValue(200)
    assert window.plan is None


def test_optical_away_back_and_nonsample_axis_motion_invalidate_stills(window):
    planned(window)
    window.camera_panel.cutting_reference_revision += 2
    window._tick()
    assert window.snapshot is None and window.calibration is None and window.plan is None
    capture(window)
    window.controller.positions[9] += 3  # Also catches external position change without a revision bump.
    window._tick()
    wait_for(lambda: window.snapshot is None)
    assert not window.run_button.isEnabled()


def test_per_run_confirmation_and_unqualified_controller_fail_closed(window):
    planned(window)
    window.request_run()
    assert not window.busy and not window.controller.moves
    window._confirmation.reject()
    assert not window.controller.moves
    window.request_run()
    confirm(window)
    wait_for(lambda: not window.busy)
    assert not window.controller.moves
    assert "not board-qualified" in window.status_label.text()
    assert window.snapshot is None


def test_commissioning_attestation_and_successful_dark_run(window):
    window.qualify_controller()
    wait_for(lambda: not window.busy)
    assert not window.controller.qualified
    confirm(window)
    wait_for(lambda: not window.busy)
    assert window.controller.qualified
    plan = planned(window)
    window.request_run()
    confirm(window)
    wait_for(lambda: not window.busy)
    assert window.controller.moves == [s.end_um for s in plan.segments]
    assert window.controller.owner is None
    assert window.snapshot is None and window.plan is None
    # Qualification is never written to a file or restored from disk.
    assert not window.storage_dir.exists()


def test_commissioning_dialog_cannot_attest_after_connection_or_configuration_change(window):
    window.qualify_controller()
    wait_for(lambda: not window.busy)
    window.controller.motion_revision += 1
    confirm(window)
    wait_for(lambda: not window.busy)
    assert not window.controller.qualified


def test_source_change_during_motion_aborts_and_lock_blocks_edits(window):
    planned(window)
    window.controller.qualified = True
    window.controller.hold_motion = True
    window.request_run()
    confirm(window)
    wait_for(lambda: bool(window.controller.moves))
    assert window.busy and not window.mode_combo.isEnabled()
    assert not window.plan_button.isEnabled()
    window.camera_panel.cutting_reference_revision += 1
    wait_for(lambda: not window.busy)
    assert window.controller.stops >= 1
    assert window.snapshot is None


def test_cancel_close_timeout_retains_worker_and_discards_capture_result(window):
    window.camera_panel.capture_gate = threading.Event()
    window.capture()
    wait_for(window.camera_panel.capture_entered.is_set)
    started = time.monotonic()
    assert window.prepare_for_close(10) is False
    assert time.monotonic() - started < 0.1
    window.close()
    assert window.isVisible() and window.busy
    QTest.qWait(30)
    window._tick()
    assert window.isVisible()
    window.camera_panel.capture_gate.set()
    wait_for(lambda: not window.busy and not window.isVisible())
    assert window.snapshot is None
    assert window.prepare_for_close(10)


def test_abort_visible_at_both_scroll_extremes_and_space_cancels(window):
    window.resize(800, 560)
    planned(window)
    window.controller.qualified = True
    window.controller.hold_motion = True
    window.request_run()
    confirm(window)
    wait_for(lambda: bool(window.controller.moves))
    bar = window.scroll.verticalScrollBar()
    for value in (bar.minimum(), bar.maximum()):
        bar.setValue(value)
        QApplication.processEvents()
        assert window.abort_button.visibleRegion().boundingRect().size() == window.abort_button.size()
        assert window.abort_button.isEnabled()
    window.activateWindow()
    window.canvas.setFocus()
    QApplication.processEvents()
    QTest.keyClick(window.canvas, Qt.Key.Key_Space)
    wait_for(lambda: not window.busy)
    assert window.controller.stops >= 1


def test_language_switch_retains_modes_and_avoids_forced_horizontal_clipping(window):
    for language in ("zh", "en"):
        translator.set_language(language)
        window.resize(800, 560)
        QApplication.processEvents()
        assert [window.mode_combo.itemData(i) for i in range(5)] == ["none", "roi", "line", "feature", "laser"]
        assert window.windowTitle()
        # Fallback scrolling must remain available if a host font exceeds the panel width.
        assert window.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert window.scroll.width() >= 360
        assert window.abort_button.visibleRegion().boundingRect().size() == window.abort_button.size()


def test_current_hardware_and_incomplete_record_cannot_enable_emission(window, tmp_path):
    from automatic_transfer_station.cutting_live import LiveQualification
    planned(window)
    assert not window.live_run_button.isEnabled()
    assert window.laser_panel.controller is None
    path = tmp_path / 'commission.json'
    path.write_text(json.dumps(LiveQualification.template(window.controller)), encoding='utf-8')
    window._load_qualification(path)
    wait_for(lambda: not window.busy)
    assert window.qualification is None
    assert not window.live_run_button.isEnabled()


def test_live_preview_adds_dark_leads_and_same_points_are_used_for_rehearsal(window):
    from automatic_transfer_station.cutting_live import LiveQualification
    from automatic_transfer_station.gas_controller import CuttingGateOutput
    planned(window)
    snapshot = window.snapshot
    window.qualification = LiveQualification('test-bench', snapshot.axis_configuration,
        CuttingGateOutput(0, 7, 12, False), True, True, True, 1, 1, 10, 1000, 1000, 60000)
    window.laser_panel._status = SimpleNamespace(interlock_wired=True, trigger_wired=True,
        interlock_ok=True, watchdog_ms=500, state=SimpleNamespace(name='DISARMED'))
    window._refresh_live_route()
    route = window._live_route
    assert route is not None
    window.controller.qualified = True
    window.request_run()
    confirm(window)
    wait_for(lambda: not window.busy)
    assert window.controller.moves == [b for a,b in zip(route.endpoints, route.endpoints[1:]) if a != b]


def test_laser_labels_switch_with_workspace_language(window):
    translator.set_language('en')
    assert window.image_tabs.tabText(0) == 'Reference and path'
    assert window.image_tabs.tabText(1) == 'Live view'
    assert 'Camera: Connected' in window.connection_summary.text()
    assert 'Laser: Disconnected' in window.connection_summary.text()
    assert window.live_run_button.text() == 'Run laser cutting'
    assert window.laser_panel.off_button.text() == 'OFF / E-stop'
    assert window.windowTitle() == 'Laser cutting'
    translator.set_language('zh')
    assert window.live_run_button.text() == '执行激光切割'
    assert window.laser_panel.off_button.text() == 'OFF / 急停'


def test_lost_live_qualification_invalidates_preview_and_dark_run_instead_of_silent_fallback(window):
    from automatic_transfer_station.cutting_live import LiveQualification
    from automatic_transfer_station.gas_controller import CuttingGateOutput
    planned(window)
    window.qualification = LiveQualification('bench', window.snapshot.axis_configuration,
        CuttingGateOutput(0, 7, 12, False), True, True, True, 1, 1, 10, 1000, 1000, 60000)
    status = SimpleNamespace(interlock_wired=True, trigger_wired=True,
        interlock_ok=True, watchdog_ms=500, state=SimpleNamespace(name='DISARMED'))
    window.laser_panel._status = status
    window._refresh_live_route()
    assert window._live_route is not None
    status.interlock_ok = False
    window._laser_status_changed(status)
    assert window.plan is None and window._live_route is None
    assert not window.run_button.isEnabled() and not window.live_run_button.isEnabled()


def test_failed_commissioning_load_revokes_previous_record_and_preview(window, tmp_path):
    planned(window)
    window.qualification = object()
    old_gate = object()
    window.controller.qualified_cutting_gate = old_gate
    revoked = []
    window.controller.configure_cutting_gate = lambda gate, qualified: revoked.append((gate, qualified))
    bad = tmp_path / 'invalid-commissioning.json'
    bad.write_text('{}', encoding='utf-8')
    window._load_qualification(bad)
    wait_for(lambda: not window.busy)
    assert revoked == [(old_gate, False)]
    assert window.qualification is None and window.snapshot is None and window.plan is None
    assert not window.live_run_button.isEnabled()
