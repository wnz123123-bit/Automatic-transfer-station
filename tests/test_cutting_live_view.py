"""Offline checks for the independent, read-only cutting monitor."""
import math
import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from automatic_transfer_station.camera_controller import CameraFrame
from automatic_transfer_station import cutting_live_view as live
from automatic_transfer_station.i18n import TRANSLATIONS, translator


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr(live, "monotonic", lambda: value[0])
    return value


@pytest.fixture
def view(app, clock):
    widget = live.CuttingLiveView()
    widget.resize(400, 300)
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def frame(at=100.0, color=(10, 20, 30)):
    metadata = {} if at is None else {"capture_finished_monotonic": at}
    return CameraFrame(4, 3, bytes(color) * 12, metadata)


def assert_color(view, color):
    assert view.canvas._image.pixelColor(0, 0).getRgb()[:3] == color


def test_latest_pending_only_converts_at_refresh_and_preserves_input(view, clock, monkeypatch):
    converted = []
    original = live.qimage_from_frame

    def convert(item):
        converted.append(item)
        return original(item)

    monkeypatch.setattr(live, "qimage_from_frame", convert)
    earlier = frame(99.5)
    latest = frame(100, (40, 50, 60))
    view.set_frame(earlier)
    view.set_frame(latest)
    assert converted == []
    assert view.canvas.image_rect().isEmpty()
    view.refresh(True)
    assert len(converted) == 1 and converted[0] is latest
    assert_color(view, (40, 50, 60))
    assert latest.metadata == {"capture_finished_monotonic": 100}
    assert latest.rgb_bytes == bytes((40, 50, 60)) * 12
    view.refresh(True)
    assert len(converted) == 1
    assert view.status_label.text() == "实时图像 · 实时"


@pytest.mark.parametrize("mode", ["roi", "line", "polygon", "feature", "laser"])
def test_canvas_rejects_selection_but_allows_wheel_zoom(view, mode):
    view.set_frame(frame())
    view.refresh(True)
    canvas = view.canvas
    spies = [QSignalSpy(signal) for signal in (
        canvas.roi_selected, canvas.line_selected, canvas.polygon_selected, canvas.point_selected)]
    canvas.set_mode(mode)  # Even accidental mode changes cannot enable editing.
    start = canvas.image_to_widget((1, 1)).toPoint()
    end = canvas.image_to_widget((3, 2)).toPoint()
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(canvas, end)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end)
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=end)
    QTest.keyClick(canvas, Qt.Key.Key_Return)
    assert all(spy.count() == 0 for spy in spies)
    assert canvas.roi is canvas.line is canvas.polygon is canvas.laser_point is None
    point = QPointF(start)
    event = QWheelEvent(point, canvas.mapToGlobal(point), QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(canvas, event)
    assert event.isAccepted() and canvas.view_zoom > 1


def test_waiting_then_capture_age_not_receipt_age_drives_pause(view, clock):
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 等待图像"
    view.set_frame(frame(99.0))
    view.refresh(True, now=100.5)
    assert view.status_label.text() == "实时图像 · 实时"
    view.refresh(True, now=100.500001)
    assert view.status_label.text() == "实时图像 · 已暂停"
    assert_color(view, (10, 20, 30))
    clock[0] = 102
    view.set_frame(frame(102, (30, 40, 50)))
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 实时"


def test_initial_old_frame_is_marked_paused_never_live(view):
    view.set_frame(frame(95))
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 已暂停"
    assert_color(view, (10, 20, 30))


@pytest.mark.parametrize("duplicate_kind", ["same_object", "equal_timestamp", "older_timestamp"])
def test_duplicate_or_out_of_order_frames_do_not_revive_stalled_feed(view, clock, duplicate_kind):
    original = frame()
    view.set_frame(original)
    view.refresh(True)
    clock[0] = 103
    duplicate = {"same_object": original, "equal_timestamp": frame(100, (90, 80, 70)),
                 "older_timestamp": frame(99.9, (90, 80, 70))}[duplicate_kind]
    view.set_frame(duplicate)
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 已暂停"
    assert_color(view, (10, 20, 30))


def test_missing_timestamp_uses_first_receipt_of_frame_not_repeated_deliveries(view, clock):
    item = frame(None)
    view.set_frame(item)
    clock[0] = 100.8
    view.set_frame(item)  # Duplicate even before first conversion.
    view.refresh(True, now=101.6)
    assert view.status_label.text() == "实时图像 · 已暂停"
    clock[0] = 102
    view.set_frame(item)
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 已暂停"
    view.set_frame(frame(None))  # A new timestamp-less capture is allowed.
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 实时"


@pytest.mark.parametrize("timestamp", [None, True, "100", math.nan, math.inf, -math.inf,
                                       100.00001, 10**1000, [], {}])
def test_invalid_present_timestamp_never_falls_back_to_receipt_time(view, timestamp):
    invalid = replace(frame(), metadata={"capture_finished_monotonic": timestamp})
    view.set_frame(invalid)
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 等待图像"
    assert view.canvas.image_rect().isEmpty()


def test_zero_timestamp_is_valid_and_age_boundary_is_strict(view, clock):
    clock[0] = 0
    view.set_frame(frame(0))
    view.refresh(True, now=1.5)
    assert view.status_label.text() == "实时图像 · 实时"
    view.refresh(True, now=math.nextafter(1.5, math.inf))
    assert view.status_label.text() == "实时图像 · 已暂停"


def test_future_timestamp_relative_to_refresh_clock_is_not_displayed(view, clock):
    view.set_frame(frame(100))
    view.refresh(True, now=99.9)
    assert view.canvas.image_rect().isEmpty()
    assert view.status_label.text() == "实时图像 · 等待图像"
    view.refresh(True, now=100.1)
    assert view.canvas.image_rect().isEmpty()  # Rejected pending frame is not replayed.


@pytest.mark.parametrize("now", [math.nan, math.inf, -math.inf, "bad", True, 10**1000])
def test_invalid_refresh_clock_does_not_convert_or_raise(view, now, monkeypatch):
    view.set_frame(frame())
    monkeypatch.setattr(live, "qimage_from_frame", lambda _: pytest.fail("invalid clock converted image"))
    view.refresh(True, now=now)
    assert view.canvas.image_rect().isEmpty()


def test_disconnect_clears_display_pending_and_late_old_frames(view, clock):
    view.set_frame(frame())
    view.refresh(True)
    clock[0] = 100.2
    view.set_frame(frame(100.2, (50, 60, 70)))
    view.refresh(False)
    assert view.canvas.image_rect().isEmpty()
    assert view._pending_frame is None and view._latest_frame is None
    assert view.status_label.text() == "实时图像 · 未连接"
    clock[0] = 101
    view.set_frame(frame(101))  # Frames while disconnected are ignored.
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 等待图像"
    view.set_frame(frame(100.1))  # Delayed delivery from before disconnection.
    view.refresh(True)
    assert view.canvas.image_rect().isEmpty()
    view.set_frame(frame(101, (70, 80, 90)))
    view.refresh(True)
    assert_color(view, (70, 80, 90))


def test_reset_clears_image_zoom_and_pending_until_new_frame(view, clock):
    view.set_frame(frame())
    view.refresh(True)
    view.canvas.set_view_zoom(2)
    clock[0] = 101
    view.set_frame(frame(101))
    view.reset()
    assert view.canvas.image_rect().isEmpty() and view.canvas.view_zoom == 1
    assert view._pending_frame is None and view._latest_frame is None
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 等待图像"
    view.set_frame(frame(100))
    view.refresh(True)
    assert view.canvas.image_rect().isEmpty()
    clock[0] = 102
    view.set_frame(frame(102))
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 实时"


def test_hidden_monitor_converts_only_newest_frame_on_reshow(view, clock, app, monkeypatch):
    calls = []
    original = live.qimage_from_frame
    monkeypatch.setattr(live, "qimage_from_frame", lambda item: calls.append(item) or original(item))
    view.hide()
    view.set_frame(frame())
    view.refresh(True)
    clock[0] = 101
    latest = frame(101, (80, 90, 100))
    view.set_frame(latest)
    view.refresh(True)
    assert calls == [] and view.canvas.image_rect().isEmpty()
    view.show()
    app.processEvents()
    assert calls == []  # Showing alone performs no conversion either.
    view.refresh(True)
    assert len(calls) == 1 and calls[0] is latest
    assert_color(view, (80, 90, 100))


@pytest.mark.parametrize("invalid", [None, object(), replace(frame(), width=0),
    replace(frame(), height=-1), replace(frame(), width=4.0), replace(frame(), width=True),
    replace(frame(), width=2**31), replace(frame(), rgb_bytes=b"short"),
    replace(frame(), rgb_bytes=b"\x00" * 37), replace(frame(), rgb_bytes=bytearray(36)),
    replace(frame(), metadata=None)])
def test_malformed_frames_clear_monitor_at_refresh_without_conversion(view, invalid, monkeypatch):
    view.set_frame(frame())
    view.refresh(True)
    monkeypatch.setattr(live, "qimage_from_frame", lambda _: pytest.fail("malformed frame converted"))
    view.set_frame(invalid)
    assert not view.canvas.image_rect().isEmpty()  # Setter has no rendering work.
    view.refresh(True)
    assert view.canvas.image_rect().isEmpty()
    assert view.status_label.text() == "实时图像 · 图像不可用"


@pytest.mark.parametrize("failure", ["exception", "null"])
def test_conversion_failure_is_isolated_clears_image_and_recovers(view, clock, monkeypatch, failure):
    view.set_frame(frame())
    view.refresh(True)
    original = live.qimage_from_frame

    def fail(_):
        if failure == "exception":
            raise RuntimeError("conversion unavailable")
        return QImage()

    monkeypatch.setattr(live, "qimage_from_frame", fail)
    clock[0] = 101
    view.set_frame(frame(101))
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 图像不可用"
    assert view.canvas.image_rect().isEmpty()
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 图像不可用"
    monkeypatch.setattr(live, "qimage_from_frame", original)
    clock[0] = 102
    view.set_frame(frame(102, (40, 50, 60)))
    view.refresh(True)
    assert view.status_label.text() == "实时图像 · 实时"
    assert_color(view, (40, 50, 60))


@pytest.mark.parametrize("state", ["disconnected", "waiting", "live", "paused", "unavailable"])
def test_language_switch_updates_status_without_conversion_or_freshness_change(view, state, monkeypatch):
    # Main integration owns i18n.py; install its expected keys only for this test.
    expected = {"disconnected": "Disconnected", "waiting": "Waiting for frames",
                "live": "Live", "paused": "Paused", "unavailable": "Image unavailable"}
    for key, value in expected.items():
        monkeypatch.setitem(TRANSLATIONS, live.CuttingLiveView._STATUS[key], f"Live image · {value}")
    if state in ("live", "paused", "unavailable"):
        view.set_frame(frame())
    view.refresh(state != "disconnected", now=102 if state == "paused" else 100)
    if state == "unavailable":
        view.set_frame(None)
        view.refresh(True)
    before = view.canvas._image.copy()
    monkeypatch.setattr(live, "qimage_from_frame", lambda _: pytest.fail("translation converted pixels"))
    translator.set_language("en")
    assert view.status_label.text() == f"Live image · {expected[state]}"
    assert view.canvas._image == before
    translator.set_language("zh")
    assert view.status_label.text() == live.CuttingLiveView._STATUS[state]
    if state == "live":
        view.refresh(True, now=102)
        assert view.status_label.text() == "实时图像 · 已暂停"
