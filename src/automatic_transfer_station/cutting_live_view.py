"""Read-only, latest-frame display; owns no camera, motion, or laser control."""

from __future__ import annotations

import math
from numbers import Real
from time import monotonic

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .camera_controller import CameraFrame
from .camera_image import qimage_from_frame
from .cutting_canvas import CuttingCanvas
from .i18n import Retranslator, translator


class CuttingLiveView(QWidget):
    """GUI-thread sink for shared camera frames, independent of cutting snapshots.

    Call ``set_frame`` from a queued GUI signal and ``refresh`` every 100 ms.
    Only refresh converts pixels, and only while visible. Missing capture times
    fall back to receipt time; present but invalid times are rejected. Equal or
    older times and repeated frame objects never renew freshness. An initially
    old frame may be displayed, but is explicitly marked paused.
    """

    STALE_AFTER = 1.5
    _STATUS = {
        "disconnected": "实时图像 · 未连接",
        "waiting": "实时图像 · 等待图像",
        "live": "实时图像 · 实时",
        "paused": "实时图像 · 已暂停",
        "unavailable": "实时图像 · 图像不可用",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rt = Retranslator(translator)
        self.canvas = CuttingCanvas(self)
        self.canvas.set_editable(False)
        self.canvas.set_wheel_zoom_enabled(True)
        self.status_label = QLabel(self)
        self.status_label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.status_label)
        self._connected: bool | None = None
        self._not_before: float | None = None
        self._latest_frame: CameraFrame | None = None
        self._pending_frame: CameraFrame | None = None
        self._latest_time: float | None = None
        self._displayed_time: float | None = None
        self._invalid_frame = False
        self._status: str | None = None
        self._show_status("disconnected")

    @staticmethod
    def _finite_time(value: object) -> bool:
        try:
            return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            return False

    def set_frame(self, frame: CameraFrame) -> None:
        """Retain one validated reference, without pixel copying or device I/O."""
        if self._connected is False or frame is self._latest_frame:
            return
        if not self.valid_image(frame):
            self._reject_image()
            return
        received = monotonic()
        captured = frame.metadata.get("capture_finished_monotonic", received)
        if not self._finite_time(captured) or captured > received:
            return
        if ((self._not_before is not None and captured <= self._not_before)
                or (self._latest_time is not None and captured <= self._latest_time)):
            return
        self._latest_frame = self._pending_frame = frame
        self._latest_time = float(captured)
        self._invalid_frame = False

    @staticmethod
    def valid_image(frame: object) -> bool:
        """Validate pixel metadata before it can be used as optical evidence."""
        return (isinstance(frame, CameraFrame)
                and all(type(size) is int and 0 < size <= 2**31 - 1
                        for size in (frame.width, frame.height))
                and type(frame.binning) is int and frame.binning > 0
                and isinstance(frame.rgb_bytes, bytes)
                and len(frame.rgb_bytes) == frame.width * frame.height * 3
                and isinstance(frame.metadata, dict))

    def _reject_image(self) -> None:
        self._pending_frame = None
        self._invalid_frame = True

    def refresh(self, connected: bool, *, now: float | None = None) -> None:
        """Update connection/freshness and paint at most the latest pending frame."""
        now = monotonic() if now is None else now
        if not self._finite_time(now):
            return
        if not connected:
            if self._connected is not False:
                self._clear(float(now))
            self._connected = False
            self._show_status("disconnected")
            return
        self._connected = True
        if self._invalid_frame:
            self._show_image_error()
            return
        # A caller-supplied clock must also agree with the capture timestamp.
        if self._latest_time is not None and self._latest_time > now:
            self._pending_frame = None
            self._show_status("paused" if self._displayed_time is not None else "waiting")
            return
        if self.isVisible() and self._pending_frame is not None:
            frame, self._pending_frame = self._pending_frame, None
            try:
                image = qimage_from_frame(frame)
                if not image.isNull():
                    self.canvas.set_image(image)
                    self._displayed_time = self._latest_time
                else:
                    self._reject_image()
            except Exception:
                # Display failure must never escape into a device/job callback.
                self._reject_image()
            if self._invalid_frame:
                self._show_image_error()
                return
        frame_time = self._latest_time if self._pending_frame is not None else self._displayed_time
        if frame_time is None:
            self._show_status("waiting")
        elif now - frame_time > self.STALE_AFTER:
            self._show_status("paused")
        else:
            self._show_status("live")

    def _show_image_error(self) -> None:
        if self._displayed_time is not None:
            self.canvas.set_image(QImage())
            self._displayed_time = None
        self._show_status("unavailable")

    def _clear(self, cutoff: float) -> None:
        self._latest_frame = self._pending_frame = None
        self._latest_time = self._displayed_time = None
        self._invalid_frame = False
        self._not_before = cutoff
        self.canvas.set_image(QImage())
        self.canvas.clear_annotations()
        self.canvas.set_view_zoom(1.0)

    def reset(self) -> None:
        """Drop all image state when closing/reopening the monitoring view."""
        self._clear(monotonic())
        self._connected = None
        self._show_status("disconnected")

    def _show_status(self, state: str) -> None:
        if state != self._status:
            self._status = state
            self._rt.message(self.status_label, self._STATUS[state])
