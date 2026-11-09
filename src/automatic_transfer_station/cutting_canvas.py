"""Qt-only cutting preview; every annotation uses raw full-frame pixel coordinates."""

from __future__ import annotations

import math
from typing import Literal

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import QWidget


Point = tuple[float, float]
PointInput = Point | QPoint | QPointF
ROI = tuple[float, float, float, float]
Line = tuple[Point, Point]
PathSegment = tuple[Point, Point, bool]
Mode = Literal["roi", "line", "polygon", "laser", "feature", "none"]
MAX_POLYGON_VERTICES = 128
MAX_EXPOSURE_POINTS = 10000


def _point(value: PointInput) -> Point:
    if isinstance(value, (QPoint, QPointF)):
        x, y = value.x(), value.y()
    else:
        x, y = value
    result = float(x), float(y)
    if not all(math.isfinite(coordinate) for coordinate in result):
        raise ValueError("Coordinates must be finite")
    return result


def _contains(rect: QRectF, point: Point) -> bool:
    # Continuous pixel coordinates, with right/bottom edges excluded for input.
    return rect.left() <= point[0] < rect.right() and rect.top() <= point[1] < rect.bottom()


class CuttingCanvas(QWidget):
    """Keep-aspect image view with full-frame, floating-point selection signals.

    Widget positions and pen widths are Qt logical pixels. Image coordinates are
    raw QImage pixels, independent of devicePixelRatio, zoom and ROI preview.
    Point selections lie in [0, width) x [0, height); ROI extents and programmatic
    polygon contours can reach the outer image edges. Exposure points remain
    strictly inside. Programmatic setters never emit selection signals.

    A same-size frame preserves legacy ROI/line annotations and the view;
    polygon/dot overlays and polygon drafts are cleared on every new image.
    A dimension change
    (including a null image) clears annotations, cancels gestures and resets the
    view. The caller must clear explicitly if a same-size image changes geometry.
    Wheel zoom is opt-in; set_view_zoom remains available while editing is locked.
    """

    roi_selected = Signal(object)
    line_selected = Signal(object)
    point_selected = Signal(object)
    polygon_selected = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._mode: Mode = "none"
        self._editable = True
        self._roi: ROI | None = None
        self._line: Line | None = None
        self._laser_point: Point | None = None
        self._feature_point: Point | None = None
        self._path_preview: tuple[PathSegment, ...] = ()
        self._polygon: tuple[Point, ...] | None = None
        self._polygon_draft: list[Point] = []
        self._polygon_hover: Point | None = None
        self._exposure_points: tuple[Point, ...] = ()
        self._view_zoom = 1.0
        self._wheel_zoom_enabled = False
        self._crop_preview = False
        self._drag_start: Point | None = None
        self._drag_end: Point | None = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def roi(self) -> ROI | None:
        return self._roi

    @property
    def line(self) -> Line | None:
        return self._line

    @property
    def laser_point(self) -> Point | None:
        return self._laser_point

    @property
    def path_preview(self) -> tuple[PathSegment, ...]:
        return self._path_preview

    @property
    def polygon(self) -> tuple[Point, ...] | None:
        """Completed full-frame vertices, without a repeated closing vertex."""
        return self._polygon

    @property
    def exposure_points(self) -> tuple[Point, ...]:
        return self._exposure_points

    @property
    def view_zoom(self) -> float:
        return self._view_zoom

    def set_image(self, image: QImage) -> None:
        """Own a raw-pixel snapshot, including when input wraps camera memory."""
        changed_size = image.size() != self._image.size()
        self._image = image.copy()
        self._image.setDevicePixelRatio(1.0)
        self._polygon = None
        self._exposure_points = ()
        self._cancel_polygon()
        if changed_size:
            self.clear_annotations()
            self._view_zoom = 1.0
        self.update()

    def set_mode(self, mode: Mode) -> None:
        if mode not in ("roi", "line", "polygon", "laser", "feature", "none"):
            raise ValueError(f"Unsupported canvas mode: {mode}")
        self._cancel_drag()
        self._cancel_polygon()
        self._mode = mode
        self._update_cursor()

    def _annotation_point(self, value: PointInput) -> Point:
        point = _point(value)
        if point[0] < 0 or point[1] < 0:
            raise ValueError("Image coordinates must be nonnegative")
        if not self._image.isNull() and (
            point[0] > self._image.width() or point[1] > self._image.height()
        ):
            raise ValueError("Annotation is outside the full frame")
        return point

    def set_roi(self, roi: ROI | None) -> None:
        if roi is not None:
            x, y, width, height = map(float, roi)
            if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
                raise ValueError("ROI must have finite coordinates and positive extents")
            self._annotation_point((x, y))
            self._annotation_point((x + width, y + height))
            roi = x, y, width, height
        self._cancel_drag()
        self._roi = roi
        self.update()

    def set_line(self, line: Line | None) -> None:
        self._line = None if line is None else (
            self._annotation_point(line[0]), self._annotation_point(line[1])
        )
        self._cancel_drag()
        self.update()

    def set_laser_point(self, point: Point | None) -> None:
        self._laser_point = None if point is None else self._annotation_point(point)
        self.update()

    def validate_polygon(self, vertices) -> tuple[Point, ...] | None:
        """Normalize a contour without changing annotations, drafts or signals.

        Geometry (area, intersections) is validated by the planning engine.
        Here 3..128 finite, unique vertices may touch the closed frame boundary
        [0,width] x [0,height]. A repeated closing vertex is normalized away;
        None/empty returns None. Mouse input and exposure points remain half-open.
        """
        points = []
        if vertices is not None:
            for vertex in vertices:
                if len(points) > MAX_POLYGON_VERTICES:
                    raise ValueError("Polygon exceeds 128 vertices")
                points.append(self._annotation_point(vertex))
        if len(points) > 1 and points[-1] == points[0]:
            points.pop()
        if points and not 3 <= len(points) <= MAX_POLYGON_VERTICES:
            raise ValueError("Polygon requires 3..128 vertices")
        if len(set(points)) != len(points):
            raise ValueError("Polygon vertices must be unique")
        return tuple(points) if points else None

    def set_polygon(self, vertices) -> None:
        """Replace a validated contour; None/empty clears. Never emits a signal."""
        self._polygon = self.validate_polygon(vertices)
        self._cancel_polygon()
        self.update()

    def finish_polygon(self) -> bool:
        """Commit the active draft, emitting once; refuse incomplete/locked input."""
        if not self._editable or self._mode != "polygon" or len(self._polygon_draft) < 3:
            return False
        self._polygon = tuple(self._polygon_draft)
        self._cancel_polygon()
        self.polygon_selected.emit(self._polygon)
        self.update()
        return True

    def set_exposure_points(self, points) -> None:
        """Snapshot at most 10000 raw-pixel exposure markers, with no signals."""
        snapshot = []
        for point in points:
            if len(snapshot) >= MAX_EXPOSURE_POINTS:
                raise ValueError("Exposure preview exceeds 10000 points")
            snapshot.append(self._bounded_pixel(point))
        self._exposure_points = tuple(snapshot)
        self.update()

    def _bounded_pixel(self, point: PointInput) -> Point:
        point = self._annotation_point(point)
        if not self._image.isNull() and not _contains(
            QRectF(0, 0, self._image.width(), self._image.height()), point
        ):
            raise ValueError("Point must be inside the full frame")
        return point

    def set_path_preview(self, segments: list[PathSegment]) -> None:
        """Snapshot (start_pixel, end_pixel, is_cut); false means dark travel.

        Dark approach/preload may project outside the frame. Preserve those
        finite coordinates and let Qt clip the preview, never clamp the route.
        """
        self._path_preview = tuple(
            (_point(start), _point(end), bool(is_cut))
            for start, end, is_cut in segments
        )
        self.update()

    def clear_annotations(self) -> None:
        self._cancel_drag()
        self._roi = self._line = self._laser_point = self._feature_point = None
        self._path_preview = ()
        self._polygon = None
        self._exposure_points = ()
        self._cancel_polygon()
        self._crop_preview = False
        self.update()

    def set_editable(self, editable: bool) -> None:
        """Lock user edits and cancel any pending gesture; setters still work."""
        self._editable = bool(editable)
        if not self._editable:
            self._cancel_drag()
            self._cancel_polygon()
        self._update_cursor()

    def set_view_zoom(self, zoom: float) -> None:
        """Set a view-only factor (1 = keep-aspect fit), centered in the widget."""
        zoom = float(zoom)
        if not math.isfinite(zoom) or not 0.1 <= zoom <= 32.0:
            raise ValueError("View zoom must be between 0.1 and 32")
        if zoom != self._view_zoom:
            self._cancel_drag()
            self._view_zoom = zoom
            self.update()

    def set_wheel_zoom_enabled(self, enabled: bool) -> None:
        self._wheel_zoom_enabled = bool(enabled)

    def set_crop_preview(self, enabled: bool) -> None:
        """Fit the selected ROI as a view, without cropping/rebasing annotations.

        Without an ROI, show the full frame. Turning preview off restores the
        full-frame view. This resets only the view zoom to 1.
        """
        self._cancel_drag()
        self._crop_preview = bool(enabled)
        self._view_zoom = 1.0
        self.update()

    def roi_image(self) -> QImage:
        """Copy unannotated ROI pixels, rounding its extents outward; null if absent."""
        if self._image.isNull() or self._roi is None:
            return QImage()
        x, y, width, height = self._roi
        left, top = math.floor(x), math.floor(y)
        right, bottom = math.ceil(x + width), math.ceil(y + height)
        return self._image.copy(QRect(left, top, right - left, bottom - top))

    def _source_rect(self) -> QRectF:
        if self._image.isNull():
            return QRectF()
        if self._crop_preview and self._roi is not None:
            return QRectF(*self._roi)
        return QRectF(0, 0, self._image.width(), self._image.height())

    def image_rect(self) -> QRectF:
        """Image destination in logical widget pixels, before viewport clipping."""
        source = self._source_rect()
        available = QRectF(self.contentsRect())
        if source.isEmpty() or available.isEmpty():
            return QRectF()
        scale = min(available.width() / source.width(), available.height() / source.height())
        width = source.width() * scale * self._view_zoom
        height = source.height() * scale * self._view_zoom
        center = available.center()
        return QRectF(center.x() - width / 2, center.y() - height / 2, width, height)

    def widget_to_image(self, point: PointInput) -> Point | None:
        """Map a visible image point to full-frame pixels; reject margins/outside."""
        try:
            x, y = _point(point)
        except ValueError:
            return None
        target = self.image_rect()
        if target.isEmpty() or not _contains(target, (x, y)) or not _contains(
            QRectF(self.contentsRect()), (x, y)
        ):
            return None
        source = self._source_rect()
        return (
            source.x() + (x - target.x()) * source.width() / target.width(),
            source.y() + (y - target.y()) * source.height() / target.height(),
        )

    def image_to_widget(self, point: PointInput) -> QPointF | None:
        """Transform full-frame pixels (including outer edges); null without a view.

        A point outside the crop/viewport is still transformed, so annotations
        can be clipped by Qt without changing their original geometry.
        """
        try:
            x, y = _point(point)
        except ValueError:
            return None
        target = self.image_rect()
        if target.isEmpty():
            return None
        source = self._source_rect()
        return QPointF(
            target.x() + (x - source.x()) * target.width() / source.width(),
            target.y() + (y - source.y()) * target.height() / source.height(),
        )

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(640, 480)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(160, 120)

    def _update_cursor(self) -> None:
        self.setCursor(
            Qt.CursorShape.CrossCursor if self._editable and self._mode != "none"
            else Qt.CursorShape.ArrowCursor
        )

    def _cancel_drag(self) -> None:
        self._drag_start = self._drag_end = None
        self.update()

    def _cancel_polygon(self) -> None:
        self._polygon_draft.clear()
        self._polygon_hover = None
        self.update()

    def _append_polygon_point(self, point: Point) -> None:
        if len(self._polygon_draft) < MAX_POLYGON_VERTICES and point not in self._polygon_draft:
            self._polygon_draft.append(point)
        self._polygon_hover = point
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or not self._editable or self._mode == "none":
            event.ignore()
            return
        point = self.widget_to_image(event.position())
        if point is None:
            self._cancel_drag()
            event.ignore()
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._mode == "polygon":
            self._append_polygon_point(point)
            event.accept()
            return
        self._drag_start = self._drag_end = point
        event.accept()
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._mode == "polygon" and self._editable and self._polygon_draft:
            self._polygon_hover = self.widget_to_image(event.position())
            self.update()
            event.accept()
            return
        if self._drag_start is None:
            event.ignore()
            return
        self._drag_end = self.widget_to_image(event.position())
        event.accept()
        self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        point = self.widget_to_image(event.position())
        if (self._mode == "polygon" and self._editable
                and event.button() == Qt.MouseButton.LeftButton and point is not None):
            # Qt sends a normal press before the double-click event. Ignore an
            # already-recorded vertex; synthetic double-clicks can supply it here.
            self._append_polygon_point(point)
            self.finish_polygon()
            event.accept()
        else:
            event.ignore()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._mode == "polygon" and self._editable:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.finish_polygon()
            elif event.key() == Qt.Key.Key_Backspace:
                if self._polygon_draft:
                    self._polygon_draft.pop()
                    self._polygon_hover = None
                    self.update()
            elif event.key() == Qt.Key.Key_Escape:
                self._cancel_polygon()
            else:
                super().keyPressEvent(event)
                return
            event.accept()
        else:
            super().keyPressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._drag_start is None:
            event.ignore()
            return
        start = self._drag_start
        end = self.widget_to_image(event.position())
        self._cancel_drag()
        if not self._editable or end is None:
            event.ignore()
            return
        event.accept()
        if self._mode == "roi":
            roi = self._rectangle(start, end)
            if roi[2] > 0 and roi[3] > 0:
                self._roi = roi
                self.roi_selected.emit(roi)
        elif self._mode == "line" and start != end:
            self._line = start, end
            self.line_selected.emit(self._line)
        elif self._mode in ("laser", "feature"):
            if self._mode == "laser":
                self._laser_point = end
            else:
                self._feature_point = end
            self.point_selected.emit(end)
        self.update()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y()
        if not self._wheel_zoom_enabled or not delta or self.widget_to_image(event.position()) is None:
            event.ignore()
            return
        # Bound the exponent too, so synthetic/high-resolution events cannot overflow.
        steps = max(-20.0, min(20.0, delta / 120.0))
        self.set_view_zoom(max(0.1, min(32.0, self._view_zoom * 1.2 ** steps)))
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._cancel_drag()
        super().resizeEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._cancel_drag()
        self._cancel_polygon()
        super().hideEvent(event)

    @staticmethod
    def _rectangle(start: Point, end: Point) -> ROI:
        return min(start[0], end[0]), min(start[1], end[1]), abs(end[0] - start[0]), abs(end[1] - start[1])

    def _draw_arrow(self, painter: QPainter, start: Point, end: Point, color: QColor, *, cut: bool) -> None:
        a, b = self.image_to_widget(start), self.image_to_widget(end)
        if a is None or b is None:
            return
        pen = QPen(color, 2.0, Qt.PenStyle.SolidLine if cut else Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(a, b)
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy)
        if length < 1:
            return
        ux, uy = dx / length, dy / length
        arrow_length = min(10.0, length * 0.4)
        base = QPointF(b.x() - ux * arrow_length, b.y() - uy * arrow_length)
        half_width = arrow_length * 0.45
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([
            b,
            QPointF(base.x() - uy * half_width, base.y() + ux * half_width),
            QPointF(base.x() + uy * half_width, base.y() - ux * half_width),
        ]))

    def _draw_roi(self, painter: QPainter, roi: ROI) -> None:
        x, y, width, height = roi
        a, b = self.image_to_widget((x, y)), self.image_to_widget((x + width, y + height))
        if a is not None and b is not None:
            painter.setPen(QPen(QColor("#ffd166"), 1.5, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(255, 209, 102, 24))
            painter.drawRect(QRectF(a, b))

    def _draw_polygon(self, painter: QPainter) -> None:
        vertices = self._polygon_draft or self._polygon
        if not vertices:
            return
        points = QPolygonF([self.image_to_widget(point) for point in vertices])
        painter.setPen(QPen(QColor("#ffd166"), 1.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._polygon_draft:
            painter.drawPolyline(points)
            if self._polygon_hover is not None:
                painter.setPen(QPen(QColor("#ffd166"), 1.0, Qt.PenStyle.DashLine))
                painter.drawLine(points[-1], self.image_to_widget(self._polygon_hover))
        else:
            painter.setBrush(QColor(255, 209, 102, 20))
            painter.drawPolygon(points)
        painter.setPen(QPen(QColor("#ffd166"), 1.5))
        painter.setBrush(QColor("#15251e"))
        for point in points:
            painter.drawEllipse(point, 3.0, 3.0)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), self.palette().window())
            target = self.image_rect()
            if target.isEmpty():
                return
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setClipRect(QRectF(self.contentsRect()).intersected(target))
            painter.drawImage(target, self._image, self._source_rect())
            if self._roi is not None:
                self._draw_roi(painter, self._roi)
            self._draw_polygon(painter)
            for start, end, is_cut in self._path_preview:
                self._draw_arrow(
                    painter, start, end, QColor("#ff584f" if is_cut else "#50b9dd"), cut=is_cut
                )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#ff584f"))
            for point in self._exposure_points:
                painter.drawEllipse(self.image_to_widget(point), 2.5, 2.5)
            active = self._drag_start is not None and self._drag_end is not None
            line = (self._drag_start, self._drag_end) if active and self._mode == "line" else self._line
            if line is not None:
                color = QColor("#67e8b0")
                self._draw_arrow(painter, *line, color, cut=True)
                painter.setPen(QPen(color, 1.5))
                painter.setBrush(QColor("#15251e"))
                for endpoint in line:
                    point = self.image_to_widget(endpoint)
                    painter.drawEllipse(point, 4.0, 4.0)
            if active and self._mode == "roi":
                self._draw_roi(painter, self._rectangle(self._drag_start, self._drag_end))
            if self._laser_point is not None:
                point = self.image_to_widget(self._laser_point)
                painter.setPen(QPen(QColor("#ff4040"), 2.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(point, 5.0, 5.0)
                painter.drawLine(point + QPointF(-11, 0), point + QPointF(11, 0))
                painter.drawLine(point + QPointF(0, -11), point + QPointF(0, 11))
            if self._feature_point is not None:
                point = self.image_to_widget(self._feature_point)
                painter.setPen(QPen(QColor("#cf9cff"), 2.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(point, 6.0, 6.0)
        finally:
            painter.end()
