"""Area workspace integration; all camera, motion and serial devices are fakes."""
import json

from automatic_transfer_station.cutting_raster import RasterPlan, RasterRecipe
from automatic_transfer_station.i18n import translator
from test_cutting_live import LiveMotion
from test_cutting_window import app, window, planned, confirm, wait_for


def area(window, mode="lines"):
    window.controller = LiveMotion()
    planned(window)
    window.pattern_panel.set_mode(mode)
    window.canvas.set_polygon(((20., 20.), (40., 20.), (40., 40.), (20., 40.)))
    window.make_plan()
    wait_for(lambda: not window.busy)
    assert isinstance(window.plan, RasterPlan), window.status_label.text()
    assert window._raster_route is not None
    return window.plan


def test_area_planning_is_async_and_current_hardware_stays_dark(window):
    plan = area(window)
    assert plan.settings.mode == "lines" and plan.exposure_count > 0
    assert window.run_button.isEnabled()
    assert not window.live_run_button.isEnabled()
    assert window.laser_panel.controller is None
    assert window.canvas.line is None


def test_roi_can_define_the_entire_ablation_polygon(window):
    planned(window)
    window.canvas.set_roi((10., 10., 50., 40.))
    window.use_roi_polygon()
    assert window.pattern_panel.pattern_mode == "lines"
    assert len(window.canvas.polygon) == 4
    assert all(10 <= x < 60 and 10 <= y < 50 for x, y in window.canvas.polygon)


def test_dot_dark_rehearsal_never_sends_fire_or_arm(window):
    from automatic_transfer_station.laser_controller import LaserController
    from test_laser_controller import FakeSerial
    peer = FakeSerial()
    driver = LaserController(serial_factory=lambda **_: peer)
    driver.connect("FAKE_ONLY")
    window.laser_panel.controller = driver
    window.laser_panel._accept_status((driver.status, True))
    area(window, "dots")
    window.request_run()
    confirm(window)
    wait_for(lambda: not window.busy, timeout=10)
    assert window.snapshot is None
    assert "release" in window.controller.calls, window.status_label.text()
    assert not any(command in {"ARM", "FIRE", "FOLLOW", "KEEP"} for command in peer.commands)
    assert not peer.comp


def test_recipe_load_restores_editable_parameters_but_never_an_executable_plan(window, tmp_path):
    plan = area(window, "dots")
    original = window.snapshot
    recipe = RasterRecipe(plan.context, plan.roi, plan.polygon_pixels, plan.settings, plan.backlash)
    path = tmp_path / "recipe.json"
    path.write_text(recipe.to_json(), encoding="utf-8")
    window._load_recipe_path(path)
    wait_for(lambda: not window.busy)
    assert window.snapshot is original
    assert window.plan is None and window._raster_route is None and window._live_route is None
    assert window.canvas.polygon == recipe.polygon_pixels
    assert window.pattern_panel.settings(window.speed_spin.value()) == recipe.settings
    assert not window.run_button.isEnabled() and not window.live_run_button.isEnabled()
    assert not any(isinstance(c, tuple) and c[0] == "move" for c in window.controller.calls)


def test_recipe_unknown_fields_are_rejected_without_movement(window, tmp_path):
    plan = area(window)
    payload = RasterRecipe(plan.context, plan.roi, plan.polygon_pixels, plan.settings, plan.backlash).to_dict()
    payload["armed"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    window._load_recipe_path(path)
    wait_for(lambda: not window.busy)
    assert "失败" in window.status_label.text()
    assert not any(isinstance(c, tuple) and c[0] == "move" for c in window.controller.calls)


def test_switching_pattern_mode_invalidates_the_old_area_route(window):
    area(window)
    window.pattern_panel.set_mode("dots")
    assert window.plan is None and window._raster_route is None
    window.pattern_panel.set_mode("line")
    assert window.mode_combo.currentData() == "line"
    assert window.canvas.polygon is None
    translator.set_language("en")
    assert window.roi_area_button.text() == "Use ROI as ablation area"
    assert window.pattern_panel.pattern_mode == "line"


def test_unconfirmed_cleanup_keeps_controls_locked_until_explicit_retry(window):
    import threading
    from types import SimpleNamespace
    transitions = []
    window.busy_changed.connect(transitions.append)
    failed = SimpleNamespace(cleanup_required=True, cancel=threading.Event(), abort=lambda: None)
    def retry():
        failed.cleanup_required = False
    failed.retry_cleanup = retry
    window._runner = failed
    def fault():
        raise RuntimeError("OFF unconfirmed")
    window._launch("run", fault, lambda _: None)
    wait_for(lambda: not window.busy)
    assert window.interlocked and window._failed_runner is failed
    assert not window.capture_button.isEnabled() and window.retry_cleanup_button.isVisible()
    window.capture()
    assert not window.busy and not window.camera_panel.capture_threads
    assert transitions[-1] is True
    window.retry_cleanup()
    wait_for(lambda: not window.busy)
    assert not window.interlocked and window._failed_runner is None
    assert transitions[-1] is False
    assert window.capture_button.isEnabled()


def test_full_frame_contour_recipe_loads_atomically_with_boundary_vertices(window, tmp_path):
    from dataclasses import replace
    plan = area(window)
    settings = replace(plan.settings, mode="dots", point_pitch_um=10, dwell_ms=10, speed_um_s=50)
    recipe = RasterRecipe(plan.context, plan.roi,
        ((0, 0), (plan.context.width, 0), (plan.context.width, plan.context.height), (0, plan.context.height)),
        settings, plan.backlash)
    path = tmp_path / "frame.json"
    path.write_text(recipe.to_json(), encoding="utf-8")
    window._load_recipe_path(path)
    wait_for(lambda: not window.busy)
    assert window.canvas.polygon == recipe.polygon_pixels, window.status_label.text()
    assert window.pattern_panel.pattern_mode == "dots" and window.speed_spin.value() == 50
    assert window.plan is None
