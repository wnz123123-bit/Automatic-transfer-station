from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from automatic_transfer_station.main_window import MainWindow
from automatic_transfer_station.i18n import translator
from test_main_window import RecordingController


@pytest.fixture
def main(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(RecordingController(), position_store_path=tmp_path/'positions.json',
        runtime_calibration_path=tmp_path/'axis.json',
        microscope_position_store_path=tmp_path/'microscope.json',
        camera_snapshot_dir=tmp_path/'photos')
    window.timer.stop()
    yield window
    window.close()
    app.processEvents()


def test_entry_opens_one_shared_bilingual_workspace(main, tmp_path):
    main.controls['laserCuttingButton'].click()
    workspace = main.cutting_window
    workspace.storage_dir = tmp_path
    assert workspace.camera_panel is main.camera_window.camera_panel
    main.show_cutting_window()
    assert main.cutting_window is workspace
    translator.set_language('en')
    assert main.controls['laserCuttingButton'].text() == 'Laser cutting'
    assert workspace.capture_button.text() == 'Capture reference photo'
    translator.set_language('zh')
    assert workspace.capture_button.text() == '拍摄定位照片'


def test_competing_controls_locked_but_stop_remains_available(main):
    main._set_cutting_busy(True)
    assert main.controls['btnStopAll'].isEnabled()
    assert not main.controls['btnCoordinateCalibration'].isEnabled()
    assert not main.controls['btnOpen'].isEnabled()
    assert not main.objective_panel.isEnabled()
    assert not main.camera_window.camera_panel.isEnabled()
    main._set_cutting_busy(False)
    assert main.controls['btnCoordinateCalibration'].isEnabled()
    assert main.objective_panel.isEnabled()


def test_global_stop_and_shutdown_cancel_cutting_first(main):
    workspace = Mock(busy=True)
    workspace.prepare_for_close.return_value = True
    main.cutting_window = workspace
    main._stop_all_motion()
    workspace.abort.assert_called_once()
    main.close()
    assert workspace.abort.call_count >= 2
    workspace.prepare_for_close.assert_called()


def test_camera_close_refused_during_cutting(main):
    main.cutting_window = SimpleNamespace(busy=True, abort=lambda:None,
        prepare_for_close=lambda **_: True, hide=lambda:None)
    main.camera_window.show()
    main.camera_window.close()
    assert main.camera_window.isVisible()
    main.cutting_window.busy = False
    main.camera_window.close()
    assert not main.camera_window.isVisible()


def test_pending_board_callback_cannot_unlock_cutting_controls(main):
    main._board_action_in_flight = True
    with pytest.raises(Exception, match='其他设备'):
        main._check_cutting_start()
    main._board_action_in_flight = False
    main.cutting_window = Mock(busy=True)
    main.cutting_window.prepare_for_close.return_value = True
    main._set_board_controls_enabled(True)
    assert not main.controls['btnOpen'].isEnabled()


def test_exit_waits_for_cutting_worker_before_window_destruction(main):
    main.show()
    workspace = Mock(busy=True)
    workspace.prepare_for_close.return_value = False
    main.cutting_window = workspace
    assert main.close() is False
    assert main.isVisible()
    assert main._cutting_exit_poll.isActive()
    workspace.prepare_for_close.return_value = True
    main._finish_cutting_exit()
    assert not main.isVisible()
    assert not main._cutting_exit_poll.isActive()


def test_failed_cutting_cleanup_keeps_competing_controls_interlocked(main):
    main.cutting_window = SimpleNamespace(busy=False, interlocked=True, abort=lambda: None,
        prepare_for_close=lambda **_: True, hide=lambda: None)
    main._set_cutting_busy(True)
    main._set_board_controls_enabled(True)
    assert main._cutting_busy()
    assert not main.controls['btnOpen'].isEnabled()
    assert not main.objective_panel.isEnabled()
    assert main.controls['btnStopAll'].isEnabled()


def test_repeated_busy_during_cleanup_retry_restores_original_widget_states(main):
    before = {widget: widget.isEnabled() for widget in (
        main.controls['btnOpen'], main.controls['btnCoordinateCalibration'],
        main.objective_panel, main.camera_window.camera_panel)}
    for busy in (True, True, True, False):
        main._set_cutting_busy(busy)
    assert all(widget.isEnabled() == enabled for widget, enabled in before.items())
    assert not main._cutting_disabled_widgets


def test_shared_camera_thread_keeps_monitoring_while_main_locks_configuration(main):
    import threading
    from dataclasses import replace
    from test_camera_panel import FakeCameraController, wait_until

    camera = main.camera_window.camera_panel
    driver = FakeCameraController()
    driver.is_open = True
    driver.current_device = driver.device
    camera.controller = driver
    main.show_cutting_window()
    cutting = main.cutting_window
    release = threading.Event()
    cutting._launch("live-run", lambda: release.wait(5), lambda _: None)
    try:
        assert not camera.isEnabled()  # Configuration remains locked by MainWindow.
        assert wait_until(lambda: not cutting.live_view.canvas._image.isNull())
        shared_thread = camera._preview_thread
        assert shared_thread is not None and shared_thread.isRunning()
        driver.frame = replace(driver.frame, rgb_bytes=bytes([90, 150, 210]) * 4)
        assert wait_until(lambda: cutting.live_view.canvas._image.pixelColor(0, 0).getRgb()[:3] == (90, 150, 210))
        cutting.show_live()
        assert camera._preview_thread is shared_thread
        assert driver.calls.count(("start_preview",)) == 1
        assert not any(call[0] in {"open", "apply_acquisition_settings"} for call in driver.calls)
        assert cutting.abort_button.isEnabled() and main.controls['btnStopAll'].isEnabled()
    finally:
        release.set()
        assert wait_until(lambda: not cutting.busy)
        camera.stop_preview()
