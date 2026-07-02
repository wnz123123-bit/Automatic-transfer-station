from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

from .camera_controller import CameraFrame
from .camera_settings import ScaleCalibration


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
        label = f"{label_um:g} um  {objective}"
        text_rect = painter.fontMetrics().boundingRect(label)
        bg = QRect(x - 8, y - text_rect.height() - 14, max(pixel_width, text_rect.width()) + 16, text_rect.height() + bar_height + 22)
        painter.fillRect(bg, QColor(0, 0, 0, 150))
        painter.setPen(QPen(QColor(255, 255, 255), bar_height))
        painter.drawLine(x, y, x + pixel_width, y)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(x, y - 10, label)
    finally:
        painter.end()
    return result
