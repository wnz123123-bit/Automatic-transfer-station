from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF

from .camera_controller import CameraFrame
from .camera_measurement import MeasurementObject, evaluate_measurement
from .camera_settings import ScaleCalibration


ANNOTATION_COLOR = QColor(255, 40, 40)
ANNOTATION_FILL = QColor(255, 40, 40, 45)
ANNOTATION_TEXT_COLOR = QColor(255, 220, 220)


def flip_frame(frame: CameraFrame, *, horizontal: bool, vertical: bool) -> CameraFrame:
    row_stride = int(frame.width) * 3
    rows = [
        frame.rgb_bytes[offset : offset + row_stride]
        for offset in range(0, len(frame.rgb_bytes), row_stride)
    ]
    if vertical:
        rows = list(reversed(rows))
    if horizontal:
        flipped_rows = []
        for row in rows:
            pixels = [row[offset : offset + 3] for offset in range(0, len(row), 3)]
            flipped_rows.append(b"".join(reversed(pixels)))
        rows = flipped_rows
    return CameraFrame(
        width=frame.width,
        height=frame.height,
        rgb_bytes=b"".join(rows),
        metadata=dict(frame.metadata),
    )


def qimage_from_frame(frame: CameraFrame) -> QImage:
    stride = int(frame.width) * 3
    return QImage(frame.rgb_bytes, int(frame.width), int(frame.height), stride, QImage.Format_RGB888).copy()


def choose_scale_bar(image_width: int, um_per_pixel: float) -> tuple[int, int]:
    max_pixels = max(24, int(image_width * 0.22))
    max_um = max_pixels * float(um_per_pixel)
    candidates = []
    for power in range(-3, 7):
        base = 10**power
        for multiplier in (1, 2, 5):
            value = multiplier * base
            if value <= max_um:
                candidates.append(value)
    label_um = int(max(candidates or [max_um]))
    return label_um, max(1, int(round(label_um / float(um_per_pixel))))


def draw_scale_bar(image: QImage, calibration: ScaleCalibration, objective: str) -> QImage:
    result = image.copy()
    if calibration.um_per_pixel <= 0:
        return result
    label_um, pixel_width = choose_scale_bar(result.width(), calibration.um_per_pixel)
    margin = max(18, int(result.width() * 0.025))
    bar_height = max(6, int(result.height() * 0.006))
    x = margin
    y = result.height() - margin - bar_height

    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont("Microsoft YaHei", max(12, int(result.height() * 0.022)))
        font.setBold(True)
        painter.setFont(font)
        label = f"{label_um:g} um"
        painter.setPen(QPen(ANNOTATION_COLOR, bar_height))
        painter.drawLine(x, y, x + pixel_width, y)
        painter.setPen(ANNOTATION_COLOR)
        painter.drawText(x, y - 10, label)
    finally:
        painter.end()
    return result


def draw_measurement_overlay(
    image: QImage,
    *,
    measurements: list[MeasurementObject] | tuple[MeasurementObject, ...] = (),
    active_points: list[tuple[float, float]] | tuple[tuple[float, float], ...] = (),
    active_preview_point: tuple[float, float] | None = None,
    active_kind: str | None = None,
    active_source_size: tuple[int, int] | None = None,
    center_crosshair: bool = False,
) -> QImage:
    result = image.copy()
    if not measurements and not active_points and not center_crosshair:
        return result

    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont("Microsoft YaHei", max(12, int(result.height() * 0.018)))
        font.setBold(True)
        painter.setFont(font)

        for measurement in measurements:
            _draw_completed_measurement(painter, result, measurement)
        if active_points:
            _draw_active_measurement(
                painter,
                result,
                active_points,
                active_preview_point,
                active_kind,
                active_source_size or (result.width(), result.height()),
            )
        if center_crosshair:
            _draw_center_crosshair(painter, result)
    finally:
        painter.end()
    return result


def _draw_completed_measurement(painter: QPainter, image: QImage, measurement: MeasurementObject) -> None:
    points = [
        _scale_point(point, image.width(), image.height(), measurement.frame_width, measurement.frame_height)
        for point in measurement.points
    ]
    if len(points) < 2:
        return

    result = evaluate_measurement(measurement)
    label = f"M{measurement.id} {result['display']}"
    pen = QPen(ANNOTATION_COLOR, max(2, int(image.width() * 0.0025)))
    painter.setPen(pen)
    painter.setBrush(ANNOTATION_COLOR)

    if measurement.kind == "polygon" and len(points) >= 3:
        polygon = QPolygonF([QPointF(x, y) for x, y in points])
        painter.setBrush(ANNOTATION_FILL)
        painter.drawPolygon(polygon)
        painter.setBrush(ANNOTATION_COLOR)
        for index, (x1, y1) in enumerate(points):
            x2, y2 = points[(index + 1) % len(points)]
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    else:
        painter.drawLine(QPointF(points[0][0], points[0][1]), QPointF(points[1][0], points[1][1]))

    marker_radius = max(4, int(image.width() * 0.004))
    for x, y in points:
        painter.drawEllipse(QPointF(x, y), marker_radius, marker_radius)
    _draw_label(painter, image, points[0][0] + 10, points[0][1] - 10, label)


def _draw_active_measurement(
    painter: QPainter,
    image: QImage,
    active_points: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    active_preview_point: tuple[float, float] | None,
    active_kind: str | None,
    source_size: tuple[int, int],
) -> None:
    points = [_scale_point(point, image.width(), image.height(), source_size[0], source_size[1]) for point in active_points]
    if not points:
        return
    pen = QPen(ANNOTATION_COLOR, max(2, int(image.width() * 0.002)))
    pen.setStyle(Qt.DashLine)
    painter.setPen(pen)
    painter.setBrush(ANNOTATION_COLOR)
    marker_radius = max(4, int(image.width() * 0.004))
    for x, y in points:
        painter.drawEllipse(QPointF(x, y), marker_radius, marker_radius)
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
    if active_preview_point is not None:
        preview_x, preview_y = _scale_point(
            active_preview_point,
            image.width(),
            image.height(),
            source_size[0],
            source_size[1],
        )
        x1, y1 = points[-1]
        painter.drawLine(QPointF(x1, y1), QPointF(preview_x, preview_y))
    elif active_kind == "polygon" and len(points) > 2:
        x1, y1 = points[-1]
        x2, y2 = points[0]
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _draw_center_crosshair(painter: QPainter, image: QImage) -> None:
    x = image.width() / 2.0
    y = image.height() / 2.0
    pen = QPen(ANNOTATION_COLOR, max(1, int(image.width() * 0.0015)))
    painter.setPen(pen)
    painter.drawLine(QPointF(x, 0), QPointF(x, image.height()))
    painter.drawLine(QPointF(0, y), QPointF(image.width(), y))
    radius = max(6, int(image.width() * 0.006))
    painter.drawEllipse(QPointF(x, y), radius, radius)


def _draw_label(painter: QPainter, image: QImage, x: float, y: float, text: str) -> None:
    metrics = painter.fontMetrics()
    rect = metrics.boundingRect(text)
    left = max(4, min(int(x), image.width() - rect.width() - 12))
    top = max(4, min(int(y) - rect.height(), image.height() - rect.height() - 12))
    bg = QRect(left - 4, top - 3, rect.width() + 8, rect.height() + 6)
    painter.fillRect(bg, QColor(0, 0, 0, 150))
    painter.setPen(ANNOTATION_TEXT_COLOR)
    painter.drawText(left, top + rect.height(), text)


def _scale_point(
    point: tuple[float, float],
    target_width: int,
    target_height: int,
    source_width: int,
    source_height: int,
) -> tuple[float, float]:
    source_width = max(1, int(source_width))
    source_height = max(1, int(source_height))
    return (
        float(point[0]) * int(target_width) / source_width,
        float(point[1]) * int(target_height) / source_height,
    )
