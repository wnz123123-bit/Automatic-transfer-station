import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from automatic_transfer_station.cutting_canvas import CuttingCanvas


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    return application


def make_image(width=400, height=200, color="#101010"):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


@pytest.fixture
def canvas(app):
    widget = CuttingCanvas()
    widget.resize(400, 400)
    widget.set_image(make_image())
    widget.show()
    app.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def drag(canvas, start, end, *, button=Qt.MouseButton.LeftButton):
    QTest.mousePress(canvas, button, Qt.KeyboardModifier.NoModifier, QPoint(*start))
    QTest.mouseMove(canvas, QPoint(*end))
    QTest.mouseRelease(canvas, button, Qt.KeyboardModifier.NoModifier, QPoint(*end))


def wheel(canvas, point, delta=120):
    local = QPointF(*point)
    event = QWheelEvent(
        local, canvas.mapToGlobal(local), QPoint(), QPoint(0, delta),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(canvas, event)
    return event


def fractional_mouse(canvas, event_type, point, *, pressed):
    local = QPointF(*point)
    event = QMouseEvent(
        event_type, local, canvas.mapToGlobal(local), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(canvas, event)


def rendered(canvas):
    # Read render assertions in logical coordinates even under QT_SCALE_FACTOR=2.
    image = canvas.grab().toImage()
    if image.size() != canvas.size():
        image = image.scaled(canvas.size())
    image.setDevicePixelRatio(1)
    return image


def test_letterbox_mapping_uses_contents_margins_and_fractional_points(canvas, app):
    canvas.resize(500, 400)
    canvas.setContentsMargins(10, 20, 30, 40)
    app.processEvents()
    assert canvas.image_rect() == QRectF(10, 75, 460, 230)
    for point in [(0.0, 0.0), (123.125, 87.625), (399.999, 199.999)]:
        mapped = canvas.image_to_widget(point)
        assert isinstance(mapped, QPointF)
        assert canvas.widget_to_image(mapped) == pytest.approx(point)
    assert canvas.widget_to_image(QPoint(240, 190)) == pytest.approx((200, 100))
    assert canvas.widget_to_image((9.999, 190)) is None
    assert canvas.widget_to_image((240, 74.999)) is None
    assert canvas.widget_to_image((470, 190)) is None
    assert canvas.widget_to_image((240, 305)) is None


def test_resize_and_portrait_letterbox_do_not_change_annotations(canvas, app):
    canvas.set_roi((40.25, 30.5, 200.125, 100.25))
    canvas.set_line(((70.125, 80.25), (290.5, 90.75)))
    roi, line = canvas.roi, canvas.line
    canvas.resize(800, 300)
    app.processEvents()
    assert canvas.image_rect() == QRectF(100, 0, 600, 300)
    assert canvas.widget_to_image((99.9, 100)) is None
    assert canvas.widget_to_image((700, 100)) is None
    assert canvas.widget_to_image(canvas.image_to_widget(line[0])) == pytest.approx(line[0])
    assert canvas.roi == roi
    assert canvas.line == line
    canvas.set_image(make_image(100, 200))
    assert canvas.image_rect() == QRectF(325, 0, 150, 300)
    assert canvas.widget_to_image((400, 150)) == pytest.approx((50, 100))


def test_empty_image_and_zero_contents_have_no_mapping(canvas):
    canvas.set_image(QImage())
    assert canvas.image_rect().isEmpty()
    assert canvas.widget_to_image((100, 100)) is None
    assert canvas.image_to_widget((100, 100)) is None
    canvas.set_image(make_image())
    canvas.setContentsMargins(200, 200, 200, 200)
    assert canvas.image_rect().isEmpty()
    assert canvas.widget_to_image((200, 200)) is None


@pytest.mark.parametrize("point", [(math.nan, 1), (1, math.inf), (-math.inf, 1)])
def test_nonfinite_mapping_is_rejected(canvas, point):
    assert canvas.widget_to_image(point) is None
    assert canvas.image_to_widget(point) is None


def test_qimage_device_pixel_ratio_does_not_change_raw_coordinates(canvas):
    image = make_image()
    image.setDevicePixelRatio(2.0)
    canvas.set_image(image)
    assert canvas.image_rect() == QRectF(0, 100, 400, 200)
    assert canvas.widget_to_image((300, 250)) == pytest.approx((300, 150))
    canvas.set_roi((10, 20, 40, 30))
    assert canvas.roi_image().width() == 40
    assert canvas.roi_image().devicePixelRatio() == 1
    assert image.devicePixelRatio() == 2


@pytest.mark.parametrize("mode", ["roi", "line"])
def test_drag_signals_are_full_frame_coordinates_at_fit_scale(canvas, mode):
    canvas.set_image(make_image(800, 400))
    canvas.set_mode(mode)
    roi_spy = QSignalSpy(canvas.roi_selected)
    line_spy = QSignalSpy(canvas.line_selected)
    point_spy = QSignalSpy(canvas.point_selected)
    drag(canvas, (310, 260), (60, 140))
    if mode == "roi":
        assert roi_spy.count() == 1
        assert roi_spy.at(0)[0] == pytest.approx((120, 80, 500, 240))
        assert canvas.roi == pytest.approx((120, 80, 500, 240))
        assert line_spy.count() == 0
    else:
        assert line_spy.count() == 1
        assert line_spy.at(0)[0] == ((620.0, 320.0), (120.0, 80.0))
        assert canvas.line == ((620.0, 320.0), (120.0, 80.0))
        assert roi_spy.count() == 0
    assert point_spy.count() == 0


@pytest.mark.parametrize("mode", ["roi", "line", "laser", "feature"])
def test_fractional_qt_event_positions_are_not_rounded(canvas, mode):
    canvas.set_image(make_image(800, 400))
    canvas.set_mode(mode)
    signal = canvas.roi_selected if mode == "roi" else (
        canvas.line_selected if mode == "line" else canvas.point_selected
    )
    spy = QSignalSpy(signal)
    fractional_mouse(canvas, QEvent.Type.MouseButtonPress, (50.125, 125.25), pressed=True)
    fractional_mouse(canvas, QEvent.Type.MouseButtonRelease, (180.375, 200.625), pressed=False)
    assert spy.count() == 1
    if mode == "roi":
        assert spy.at(0)[0] == pytest.approx((100.25, 50.5, 260.5, 150.75))
    elif mode == "line":
        assert spy.at(0)[0][0] == pytest.approx((100.25, 50.5))
        assert spy.at(0)[0][1] == pytest.approx((360.75, 201.25))
    else:
        assert spy.at(0)[0] == pytest.approx((360.75, 201.25))


@pytest.mark.parametrize("mode", ["laser", "feature"])
def test_point_click_signal_and_feature_does_not_move_reference_laser(canvas, mode):
    canvas.set_laser_point((25.5, 30.25))
    canvas.set_mode(mode)
    spy = QSignalSpy(canvas.point_selected)
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(120, 170))
    assert spy.count() == 1
    assert spy.at(0)[0] == (120.0, 70.0)
    expected = (120.0, 70.0) if mode == "laser" else (25.5, 30.25)
    assert canvas.laser_point == expected


@pytest.mark.parametrize("mode", ["roi", "line", "laser", "feature", "none"])
def test_outside_presses_releases_and_right_clicks_emit_nothing(canvas, mode):
    canvas.set_roi((20, 30, 40, 50))
    canvas.set_line(((10, 20), (30, 40)))
    canvas.set_laser_point((100, 100))
    previous = canvas.roi, canvas.line, canvas.laser_point
    canvas.set_mode(mode)
    spies = [QSignalSpy(signal) for signal in (canvas.roi_selected, canvas.line_selected, canvas.point_selected)]
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(20, 50))
    drag(canvas, (20, 50), (80, 180))
    drag(canvas, (80, 180), (20, 350))
    drag(canvas, (80, 180), (200, 220), button=Qt.MouseButton.RightButton)
    if mode == "none":
        drag(canvas, (80, 180), (200, 220))
    assert all(spy.count() == 0 for spy in spies)
    assert (canvas.roi, canvas.line, canvas.laser_point) == previous


@pytest.mark.parametrize("mode", ["roi", "line"])
def test_zero_size_drag_does_not_replace_selection(canvas, mode):
    canvas.set_mode(mode)
    canvas.set_roi((20, 30, 40, 50))
    canvas.set_line(((10, 20), (30, 40)))
    spies = [QSignalSpy(canvas.roi_selected), QSignalSpy(canvas.line_selected)]
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 150))
    if mode == "roi":
        drag(canvas, (100, 150), (100, 250))
    assert all(spy.count() == 0 for spy in spies)
    assert canvas.roi == (20, 30, 40, 50)
    assert canvas.line == ((10, 20), (30, 40))


@pytest.mark.parametrize("mode", ["roi", "line", "laser", "feature"])
def test_readonly_prevents_new_and_inflight_user_edits(canvas, mode):
    canvas.set_mode(mode)
    canvas.set_roi((20, 30, 40, 50))
    canvas.set_line(((10, 20), (30, 40)))
    canvas.set_laser_point((50, 60))
    previous = canvas.roi, canvas.line, canvas.laser_point
    spies = [QSignalSpy(signal) for signal in (canvas.roi_selected, canvas.line_selected, canvas.point_selected)]
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 180))
    canvas.set_editable(False)
    canvas.set_editable(True)
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(200, 220))
    canvas.set_editable(False)
    drag(canvas, (80, 180), (200, 220))
    assert all(spy.count() == 0 for spy in spies)
    assert (canvas.roi, canvas.line, canvas.laser_point) == previous
    canvas.set_laser_point((75, 80))  # Application-controlled overlays still update.
    assert canvas.laser_point == (75, 80)
    canvas.set_editable(True)
    drag(canvas, (80, 180), (200, 220))
    assert sum(spy.count() for spy in spies) == 1


def test_setters_clear_and_frame_changes_do_not_emit_selection_signals(canvas):
    spies = [QSignalSpy(signal) for signal in (canvas.roi_selected, canvas.line_selected, canvas.point_selected)]
    canvas.set_roi((20.25, 30.125, 40.5, 50.75))
    canvas.set_line(((10.125, 20), (30, 40.25)))
    canvas.set_laser_point((50.25, 60.5))
    segments = [((5, 6), (7, 8), False), ((7, 8), (10, 15), True)]
    canvas.set_path_preview(segments)
    segments.clear()
    assert len(canvas.path_preview) == 2
    previous = canvas.roi, canvas.line, canvas.laser_point, canvas.path_preview
    canvas.set_view_zoom(1.75)
    canvas.set_image(make_image(color="white"))
    assert (canvas.roi, canvas.line, canvas.laser_point, canvas.path_preview) == previous
    assert canvas.view_zoom == 1.75
    canvas.set_image(make_image(401, 200))
    assert (canvas.roi, canvas.line, canvas.laser_point, canvas.path_preview) == (None, None, None, ())
    assert canvas.view_zoom == 1.0
    canvas.set_roi((1, 2, 3, 4))
    canvas.clear_annotations()
    assert canvas.roi is None
    assert all(spy.count() == 0 for spy in spies)


@pytest.mark.parametrize("change", ["mode", "resize", "dimensions", "clear", "zoom", "crop", "hide"])
def test_view_and_state_changes_cancel_pending_selection(canvas, app, change):
    canvas.set_mode("line")
    spies = [QSignalSpy(canvas.line_selected), QSignalSpy(canvas.roi_selected)]
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 180))
    if change == "mode":
        canvas.set_mode("roi")
    elif change == "resize":
        canvas.resize(500, 400)
    elif change == "dimensions":
        canvas.set_image(make_image(800, 400))
    elif change == "clear":
        canvas.clear_annotations()
    elif change == "zoom":
        canvas.set_view_zoom(1.5)
    elif change == "crop":
        canvas.set_crop_preview(True)
    else:
        canvas.hide()
        canvas.show()
    app.processEvents()
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(200, 220))
    assert all(spy.count() == 0 for spy in spies)


def test_live_same_size_frame_does_not_interrupt_drag(canvas):
    canvas.set_mode("line")
    spy = QSignalSpy(canvas.line_selected)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 180))
    canvas.set_image(make_image(color="white"))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(200, 220))
    assert spy.count() == 1
    assert spy.at(0)[0] == ((80, 80), (200, 120))


def test_crop_preview_keeps_full_frame_offsets_and_restores_view(canvas):
    canvas.set_roi((100, 50, 200, 100))
    canvas.set_line(((110.125, 60.25), (290.75, 140.5)))
    canvas.set_crop_preview(True)
    assert canvas.image_rect() == QRectF(0, 100, 400, 200)
    assert canvas.widget_to_image((0, 100)) == pytest.approx((100, 50))
    assert canvas.widget_to_image((200, 200)) == pytest.approx((200, 100))
    for point in canvas.line:
        assert canvas.widget_to_image(canvas.image_to_widget(point)) == pytest.approx(point)
    assert canvas.widget_to_image((200, 99)) is None
    canvas.set_mode("line")
    line_spy = QSignalSpy(canvas.line_selected)
    drag(canvas, (40, 120), (360, 280))
    assert line_spy.at(0)[0] == ((120, 60), (280, 140))
    canvas.set_mode("roi")
    roi_spy = QSignalSpy(canvas.roi_selected)
    drag(canvas, (40, 120), (360, 280))
    assert roi_spy.at(0)[0] == pytest.approx((120, 60, 160, 80))
    assert canvas.line == ((120, 60), (280, 140))
    canvas.set_crop_preview(False)
    assert canvas.widget_to_image((40, 120)) == pytest.approx((40, 20))
    assert canvas.roi == (120, 60, 160, 80)


def test_roi_image_is_independent_unannotated_raw_copy_rounded_outward(canvas):
    image = make_image(color="#213547")
    image.setPixelColor(10, 20, QColor("#aabbcc"))
    canvas.set_image(image)
    canvas.set_roi((10.25, 20.5, 30.5, 40.25))
    canvas.set_laser_point((15, 25))
    canvas.set_line(((12, 23), (35, 55)))
    canvas.set_path_preview([((12, 23), (35, 55), True)])
    image.fill(QColor("white"))
    canvas.set_crop_preview(True)
    canvas.set_view_zoom(3.0)
    crop = canvas.roi_image()
    assert (crop.width(), crop.height()) == (31, 41)
    assert crop.pixelColor(0, 0) == QColor("#aabbcc")
    assert crop.pixelColor(5, 5) == QColor("#213547")
    crop.fill(QColor("black"))
    assert canvas.roi_image().pixelColor(0, 0) == QColor("#aabbcc")
    canvas.clear_annotations()
    assert canvas.roi_image().isNull()
    canvas.set_roi((0, 0, 400, 200))
    assert canvas.roi_image().size() == image.size()


def test_zoom_maps_visible_pixels_only_and_keeps_annotations(canvas):
    canvas.set_roi((100, 50, 200, 100))
    canvas.set_line(((150.125, 60.25), (290.5, 140.75)))
    before = canvas.roi, canvas.line
    canvas.set_view_zoom(2.0)
    assert canvas.image_rect() == QRectF(-200, 0, 800, 400)
    assert canvas.widget_to_image((0, 0)) == pytest.approx((100, 0))
    assert canvas.widget_to_image((-1, 100)) is None
    assert canvas.widget_to_image((400, 100)) is None
    assert canvas.widget_to_image(canvas.image_to_widget(canvas.line[0])) == pytest.approx(canvas.line[0])
    assert (canvas.roi, canvas.line) == before
    canvas.set_mode("line")
    spy = QSignalSpy(canvas.line_selected)
    drag(canvas, (100, 100), (300, 300))
    assert spy.at(0)[0] == ((150, 50), (250, 150))


def test_wheel_zoom_is_opt_in_view_only_and_ignores_letterbox(canvas):
    canvas.set_laser_point((200, 100))
    spy = QSignalSpy(canvas.point_selected)
    assert not wheel(canvas, (200, 200)).isAccepted()
    assert canvas.view_zoom == 1
    canvas.set_wheel_zoom_enabled(True)
    assert not wheel(canvas, (20, 50)).isAccepted()
    assert canvas.view_zoom == 1
    canvas.set_editable(False)
    assert wheel(canvas, (200, 200)).isAccepted()
    assert canvas.view_zoom == pytest.approx(1.2)
    assert wheel(canvas, (200, 200), -120).isAccepted()
    assert canvas.view_zoom == pytest.approx(1)
    assert canvas.laser_point == (200, 100)
    assert spy.count() == 0


@pytest.mark.parametrize("zoom", [0, -1, math.nan, math.inf, 33])
def test_invalid_zoom_is_rejected_without_changing_view(canvas, zoom):
    with pytest.raises(ValueError):
        canvas.set_view_zoom(zoom)
    assert canvas.view_zoom == 1


@pytest.mark.parametrize("roi", [(1, 2, 0, 4), (1, 2, 3, -4), (-1, 2, 3, 4), (0, 0, 401, 200), (math.nan, 1, 2, 3)])
def test_invalid_roi_is_rejected_without_replacing_existing_roi(canvas, roi):
    canvas.set_roi((10, 20, 30, 40))
    with pytest.raises(ValueError):
        canvas.set_roi(roi)
    assert canvas.roi == (10, 20, 30, 40)


def test_invalid_mode_and_points_do_not_replace_valid_annotations(canvas):
    canvas.set_line(((10, 20), (30, 40)))
    canvas.set_laser_point((10, 20))
    canvas.set_path_preview([((10, 20), (30, 40), True)])
    with pytest.raises(ValueError):
        canvas.set_mode("ellipse")
    with pytest.raises(ValueError):
        canvas.set_line(((10, 20), (math.nan, 40)))
    with pytest.raises(ValueError):
        canvas.set_laser_point((401, 20))
    with pytest.raises(ValueError):
        canvas.set_path_preview([((10, 20), (30, 40), True), ((30, 40), (40, math.inf), False)])
    assert canvas.line == ((10, 20), (30, 40))
    assert canvas.laser_point == (10, 20)
    assert canvas.path_preview == (((10, 20), (30, 40), True),)


def test_render_distinguishes_cut_and_travel_and_shows_line_endpoints(canvas):
    canvas.set_path_preview([((40, 40), (350, 40), True), ((40, 100), (350, 100), False)])
    canvas.set_line(((60, 150), (300, 150)))
    image = rendered(canvas)
    cut = [image.pixelColor(x, 140) for x in range(70, 300)]
    travel = [image.pixelColor(x, 200) for x in range(70, 300)]
    assert all(color.red() > 120 and color.red() > 1.5 * color.blue() for color in cut)
    assert any(color.blue() > 120 and color.blue() > color.red() for color in travel)
    assert any(color.red() < 30 and color.green() < 30 and color.blue() < 30 for color in travel)
    # Arrow wings and endpoint rings extend away from the central line.
    assert image.pixelColor(343, 137).red() > 120
    for x in (60, 300):
        assert image.pixelColor(x, 246).green() > 100
    assert image.pixelColor(290, 247).green() > 100


def test_render_reference_crosshair_and_annotations_are_clipped_to_image(canvas):
    base = rendered(canvas)
    canvas.set_laser_point((100, 100))
    image = rendered(canvas)
    for x, y in [(100, 191), (100, 209), (91, 200), (109, 200)]:
        color = image.pixelColor(x, y)
        assert color.red() > 150 and color.red() > color.green() * 2
    canvas.set_laser_point((100, 0))
    canvas.set_line(((30, 0), (300, 0)))
    canvas.set_roi((0, 0, 400, 200))
    image = rendered(canvas)
    assert image.copy(0, 0, 400, 100) == base.copy(0, 0, 400, 100)
    assert image.copy(0, 300, 400, 100) == base.copy(0, 300, 400, 100)


def test_drag_preview_is_visible_before_signal_and_restores_after_outside_release(canvas):
    canvas.set_mode("line")
    spy = QSignalSpy(canvas.line_selected)
    before = rendered(canvas)
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(60, 160))
    QTest.mouseMove(canvas, QPoint(250, 220))
    assert rendered(canvas) != before
    assert spy.count() == 0
    assert canvas.line is None
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(250, 350))
    assert rendered(canvas) == before
    assert spy.count() == 0


def polygon_click(canvas, point):
    widget_point = canvas.image_to_widget(point)
    fractional_mouse(canvas, QEvent.Type.MouseButtonPress, (widget_point.x(), widget_point.y()), pressed=True)
    fractional_mouse(canvas, QEvent.Type.MouseButtonRelease, (widget_point.x(), widget_point.y()), pressed=False)


@pytest.mark.parametrize("finish", ["enter", "return", "method", "doubleclick"])
def test_polygon_commits_raw_fractional_vertices_once(canvas, finish):
    canvas.set_mode("polygon")
    spy = QSignalSpy(canvas.polygon_selected)
    vertices = ((40.125, 20.25), (180.375, 40.5), (130.625, 170.75))
    for point in vertices:
        polygon_click(canvas, point)
    assert canvas.polygon is None and spy.count() == 0
    if finish == "method":
        assert canvas.finish_polygon()
    elif finish == "doubleclick":
        target = canvas.image_to_widget(vertices[-1])
        fractional_mouse(canvas, QEvent.Type.MouseButtonDblClick, (target.x(), target.y()), pressed=True)
    else:
        QTest.keyClick(canvas, Qt.Key.Key_Enter if finish == "enter" else Qt.Key.Key_Return)
    assert canvas.polygon == vertices
    assert spy.count() == 1 and spy.at(0)[0] == vertices
    assert isinstance(spy.at(0)[0], tuple)
    assert not canvas.finish_polygon() and spy.count() == 1


def test_polygon_native_doubleclick_closes_without_duplicate_first_vertex(canvas):
    canvas.set_mode("polygon")
    spy = QSignalSpy(canvas.polygon_selected)
    for point in ((40, 20), (180, 40), (130, 170)):
        polygon_click(canvas, point)
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(40, 120))
    assert canvas.polygon == ((40, 20), (180, 40), (130, 170))
    assert spy.count() == 1


def test_polygon_minimum_undo_abandon_and_outside_input(canvas):
    previous = ((10, 10), (20, 10), (20, 20))
    canvas.set_polygon(previous)
    canvas.set_mode("polygon")
    spy = QSignalSpy(canvas.polygon_selected)
    for point in ((40, 20), (180, 40)):
        polygon_click(canvas, point)
    QTest.keyClick(canvas, Qt.Key.Key_Return)
    assert not canvas.finish_polygon() and canvas.polygon == previous
    polygon_click(canvas, (130, 170))
    QTest.keyClick(canvas, Qt.Key.Key_Backspace)
    QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(30, 30))
    QTest.mouseClick(canvas, Qt.MouseButton.RightButton, pos=QPoint(130, 270))
    assert not canvas.finish_polygon()
    QTest.keyClick(canvas, Qt.Key.Key_Escape)
    assert canvas.polygon == previous
    polygon_click(canvas, (130, 170))
    assert not canvas.finish_polygon() and spy.count() == 0


def test_polygon_draft_survives_crop_zoom_resize_in_canonical_pixels(canvas, app):
    canvas.set_roi((100, 50, 200, 100))
    canvas.set_mode("polygon")
    polygon_click(canvas, (120.25, 60.5))
    canvas.set_crop_preview(True)
    canvas.set_view_zoom(1.5)
    polygon_click(canvas, (220.75, 90.125))
    canvas.resize(520, 420)
    app.processEvents()
    polygon_click(canvas, (180.5, 120.25))
    assert canvas.finish_polygon()
    for actual, expected in zip(canvas.polygon, ((120.25, 60.5), (220.75, 90.125), (180.5, 120.25))):
        assert actual == pytest.approx(expected)


@pytest.mark.parametrize("action", ["lock", "mode", "hide", "image", "clear"])
def test_polygon_draft_is_abandoned_by_state_change(canvas, action):
    canvas.set_mode("polygon")
    for point in ((40, 20), (180, 40), (130, 170)):
        polygon_click(canvas, point)
    spy = QSignalSpy(canvas.polygon_selected)
    if action == "lock":
        canvas.set_editable(False)
        assert not canvas.finish_polygon()
        polygon_click(canvas, (140, 180))
        QTest.keyClick(canvas, Qt.Key.Key_Return)
        canvas.set_editable(True)
    elif action == "mode":
        canvas.set_mode("line")
        canvas.set_mode("polygon")
    elif action == "hide":
        canvas.hide()
        canvas.show()
    elif action == "image":
        canvas.set_image(make_image())
    else:
        canvas.clear_annotations()
    assert not canvas.finish_polygon() and canvas.polygon is None
    assert spy.count() == 0


def test_polygon_click_limit_ignores_duplicates_and_129th_vertex(canvas):
    canvas.set_mode("polygon")
    vertices = tuple((20 + i, 80 + (i % 2)) for i in range(128))
    for point in vertices:
        polygon_click(canvas, point)
    polygon_click(canvas, vertices[0])
    polygon_click(canvas, (300, 100))
    assert canvas.finish_polygon() and canvas.polygon == vertices


@pytest.mark.parametrize("invalid", [((0, 0), (10, 10)),
    ((0, 0), (10, 10), (10, 10)), ((0, 0), (10, 10), (401, 20)),
    ((0, 0), (10, 10), (20, 201)), ((0, 0), (10, 10), (-1, 20)),
    ((0, 0), (10, 10), (20, math.nan)), tuple((i, 20) for i in range(129))])
def test_invalid_polygon_does_not_replace_previous(canvas, invalid):
    previous = ((10, 10), (20, 10), (20, 20))
    canvas.set_polygon(previous)
    with pytest.raises(ValueError):
        canvas.set_polygon(invalid)
    assert canvas.polygon == previous


def test_validate_polygon_is_pure_including_clear_error_and_inflight_draft(canvas):
    previous = ((10, 10), (20, 10), (20, 20))
    canvas.set_polygon(previous)
    canvas.set_exposure_points([(15, 15)])
    canvas.set_mode("polygon")
    draft = ((40.25, 20.5), (180.75, 40.25), (130.5, 170.75))
    for point in draft[:2]:
        polygon_click(canvas, point)
    before = rendered(canvas)
    spy = QSignalSpy(canvas.polygon_selected)
    contour = [(0, 0), (400, 0), (400, 200), (0, 200), (0, 0)]
    assert canvas.validate_polygon(contour) == tuple(contour[:-1])
    assert len(contour) == 5
    assert canvas.validate_polygon(None) is None
    assert canvas.validate_polygon([]) is None
    with pytest.raises(ValueError):
        canvas.validate_polygon(((0, 0), (401, 0), (400, 200)))
    assert canvas.polygon == previous and canvas.exposure_points == ((15, 15),)
    assert rendered(canvas) == before and spy.count() == 0
    polygon_click(canvas, draft[2])
    assert canvas.finish_polygon() and canvas.polygon == draft
    assert spy.count() == 1


def test_closed_frame_polygon_keeps_mouse_and_exposure_points_half_open(canvas):
    contour = ((0, 0), (400, 0), (400, 200), (0, 200))
    canvas.set_roi((50, 30, 100, 80))
    canvas.set_crop_preview(True)
    canvas.set_view_zoom(1.5)
    canvas.set_polygon(contour)
    assert canvas.polygon == contour  # Contour bounds use the full frame, not the crop.
    dots = ((0, 0), (math.nextafter(400., 0.), math.nextafter(200., 0.)))
    canvas.set_exposure_points(dots)
    for point in ((400, 0), (0, 200), (400, 200)):
        with pytest.raises(ValueError):
            canvas.set_exposure_points([point])
        assert canvas.exposure_points == dots
    canvas.set_crop_preview(False)
    canvas.set_mode("polygon")
    polygon_click(canvas, (10, 10))
    polygon_click(canvas, (40, 10))
    for point in ((400, 0), (0, 200), (400, 200)):
        assert canvas.widget_to_image(canvas.image_to_widget(point)) is None
        polygon_click(canvas, point)
    assert not canvas.finish_polygon() and canvas.polygon == contour
    polygon_click(canvas, (40, 40))
    assert canvas.finish_polygon()
    assert canvas.polygon == ((10, 10), (40, 10), (40, 40))


def test_polygon_and_dots_snapshot_setters_clear_on_every_new_image(canvas):
    spy = QSignalSpy(canvas.polygon_selected)
    vertices = [(10.25, 10.5), (20, 10), (20, 20), (10.25, 10.5)]
    dots = [(15.25, 15.5), (18.5, 16.25)]
    canvas.set_polygon(vertices)
    canvas.set_exposure_points(dots)
    vertices.clear()
    dots.clear()
    assert canvas.polygon == ((10.25, 10.5), (20, 10), (20, 20))
    assert canvas.exposure_points == ((15.25, 15.5), (18.5, 16.25))
    canvas.set_image(make_image())
    assert canvas.polygon is None and canvas.exposure_points == ()
    canvas.set_polygon(((0, 0), (10, 0), (10, 10)))
    canvas.set_exposure_points([(5, 5)])
    canvas.clear_annotations()
    assert canvas.polygon is None and canvas.exposure_points == ()
    assert spy.count() == 0


def test_exposure_points_limit_validation_and_no_silent_truncation(canvas):
    points = tuple((i % 400, i // 400) for i in range(10000))
    canvas.set_exposure_points(iter(points))
    assert canvas.exposure_points == points
    for invalid in (points + ((1, 1),), [(math.inf, 10)], [(10, 200)], [(-1, 10)]):
        with pytest.raises(ValueError):
            canvas.set_exposure_points(invalid)
        assert canvas.exposure_points == points


def test_polygon_dots_render_and_crop_clip_in_raw_coordinates(canvas):
    base = rendered(canvas)
    canvas.set_polygon(((50, 20), (350, 20), (350, 180), (50, 180)))
    canvas.set_exposure_points([(100.25, 70.25), (300, 150)])
    image = rendered(canvas)
    assert image.pixelColor(100, 170).red() > 180
    assert image.pixelColor(50, 200).green() > 120
    assert image.copy(0, 0, 400, 100) == base.copy(0, 0, 400, 100)
    canvas.set_roi((80, 40, 160, 80))
    before = canvas.polygon, canvas.exposure_points
    canvas.set_crop_preview(True)
    canvas.set_view_zoom(1.2)
    pixel = canvas.image_to_widget((100.25, 70.25)).toPoint()
    image = rendered(canvas)
    assert image.pixelColor(pixel).red() > 180
    assert (canvas.polygon, canvas.exposure_points) == before
