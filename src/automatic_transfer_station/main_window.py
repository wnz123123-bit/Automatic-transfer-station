from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
import logging
from pathlib import Path
from queue import Empty, PriorityQueue, Queue
import threading
import time

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QBrush, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QApplication,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QKeySequence, QShortcut

from . import __version__
from .i18n import tr
from .axis_position_store import AxisPositionStore, DEFAULT_POSITION_STORE_PATH
from .axis_calibration import (
    AXIS_LABELS,
    AXIS9_TWIST_SPEED_DEG_S,
    GAMEPAD_MICROSCOPE_Z_SPEED_UM_S,
    MICROSCOPE_Z_STEP_OPTIONS_UM,
    STAGE_MAP_AXIS_POSITIONS,
    STAGE_MAP_AXIS_LABEL_POSITIONS,
    coordinate_from_pulse,
    default_speed_for_axis,
    speed_options_for_axis,
    speed_to_pulse_per_ms,
    step_to_pulse_delta,
    unsafe_axes_for_positions,
)
from .controller_base import ALL_AXES, AXIS9_NUM, MotionController, MotionControllerError, speed_axis_for
from .microscope_position_store import DEFAULT_MICROSCOPE_POSITION_STORE_PATH, MicroscopePositionStore
from .nosepiece_controller import DisabledNosepieceController, NosepieceController, NosepieceError
from .objective_panel import ObjectivePanel
from .axis_runtime_calibration_store import DEFAULT_RUNTIME_CALIBRATION_PATH, RuntimeAxisCalibrationStore
from .camera_window import CameraWindow
from .temperature_panel import TemperaturePanel
from .gamepad_controller import GAMEPAD_AXES
from .gamepad_panel import GamepadPanel
from .paths import CAMERA_SNAPSHOT_DIR, STAGE_OVERVIEW_IMAGE


_log = logging.getLogger("ats.motion")
_audit = logging.getLogger("ats.audit")

DEFAULT_STEP = 20.0

STAGE_COLOR = "#168a72"
ADSORB_COLOR = "#d07a1b"
SLIDE_COLOR = "#285fbd"
SCOPE_COLOR = "#b50083"
CALIBRATED_AXIS_ORDER = (1, 2, 4, 5, 6, 7, 8, 9)

# Mapping from axis number to its axis-card objectName, used by safety UI.
AXIS_CARD_NAMES: dict[int, str] = {
    1: "axisCardX1",
    2: "axisCardY1",
    3: "axisCardMicroscopeZ",
    4: "axisCardZ",
    5: "axisCardX3",
    6: "axisCardY3",
    7: "axisCardX2",
    8: "axisCardY2",
    9: "axisCardR",
}


class SpeedComboBox(QComboBox):
    def __init__(self, values: tuple[float, ...], default: float):
        super().__init__()
        self._values = tuple(float(value) for value in values)
        self.setEditable(False)
        for value in self._values:
            self.addItem(f"{value:g}", value)
        self.setValue(default)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def value(self) -> float:
        data = self.currentData()
        return float(data)

    def setValue(self, value: float) -> None:
        for index, option in enumerate(self._values):
            if abs(option - float(value)) < 1e-9:
                self.setCurrentIndex(index)
                return
        raise ValueError(f"{value:g} is not a configured option")

    def maximum(self) -> float:
        return max(self._values)


class _StageArrowOverlay(QWidget):
    """Transparent overlay that draws a leader-line arrow from each axis label chip
    to the real part on the photo. It sits above the image but below the chips and
    ignores mouse events, so the chips and photo stay interactive while the arrows
    keep each label visually tied to its motor/part regardless of where declutter
    nudges the chip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._segments: list[tuple[float, float, float, float, str]] = []

    def set_segments(self, segments) -> None:
        self._segments = list(segments)
        self.update()

    def paintEvent(self, event) -> None:
        if not self._segments:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        for x1, y1, x2, y2, color in self._segments:
            self._draw_arrow(painter, x1, y1, x2, y2, color)

    @staticmethod
    def _draw_arrow(painter, x1, y1, x2, y2, color) -> None:
        dx, dy = x2 - x1, y2 - y1
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1.0:
            return
        ux, uy = dx / length, dy / length
        px, py = -uy, ux  # perpendicular unit vector
        # White halo underlay first, so the colour line stays legible over the
        # busy photo (dark motors, bright tabletop) at any zoom.
        halo = QColor(255, 255, 255, 205)
        painter.setPen(QPen(halo, 4.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        pen = QPen(QColor(color), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        # Two arrowhead barbs at the tip (same style as the motion-pad chevrons).
        barb, half = 9.0, 4.5
        bx, by = x2 - ux * barb, y2 - uy * barb
        painter.drawLine(QPointF(x2, y2), QPointF(bx + px * half, by + py * half))
        painter.drawLine(QPointF(x2, y2), QPointF(bx - px * half, by - py * half))


class StageMapWidget(QFrame):
    REFERENCE_SIZE = QSize(620, 640)
    MAX_MAP_HEIGHT = 640
    MAP_PADDING = 6

    def __init__(self, image_path: Path):
        super().__init__()
        self._base_pixmap = QPixmap(str(image_path)) if image_path.exists() else QPixmap()
        # Each hotspot: (button, label_x, label_y, tip_x, tip_y, base_w, base_h, color)
        # where label_* is the chip anchor and tip_* is the part the arrow points at
        # (all fractions 0..1). See STAGE_MAP_AXIS_POSITIONS / *_LABEL_POSITIONS.
        self._hotspots: list[tuple] = []
        self._image_rect = QRect()

        self.setStyleSheet("background: #ffffff; border-radius: 8px;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(300, 300)
        self.setMaximumHeight(self.MAX_MAP_HEIGHT)

        self.image_label = QLabel(self)
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setScaledContents(False)
        if self._base_pixmap.isNull():
            self.image_label.setText("Missing stage image")
            self.image_label.setStyleSheet("color: #17212b; font-weight: 700;")
        else:
            self.image_label.setPixmap(
                self._base_pixmap.scaled(
                    self.REFERENCE_SIZE,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

        # Arrow overlay sits above the image but below the chips (created here so it
        # is stacked above image_label; the chips raise_() themselves on top in
        # add_hotspot). It draws the leader-line arrows each layout pass.
        self._arrow_overlay = _StageArrowOverlay(self)
        self._arrow_overlay.setGeometry(self.rect())

        # Overlay banner across the top of the image showing which axis the gamepad
        # currently has selected (amber to match the selected-hotspot highlight).
        self.selected_axis_label = QLabel(self)
        self.selected_axis_label.setObjectName("stageSelectedAxisLabel")
        self.selected_axis_label.setAlignment(Qt.AlignCenter)
        # Word-wrap so the (now longer) "轴 · 角色 · 步长 · 坐标" banner never spills
        # past the image edge — _position_selected_axis_label caps its width to the
        # image rect, and it wraps to a second line instead of overflowing.
        self.selected_axis_label.setWordWrap(True)
        self.selected_axis_label.setStyleSheet(
            "QLabel{color:#1a1206; background:rgba(245,158,11,238);"
            " border:1px solid #b4750a; border-radius:6px; padding:3px 12px;"
            " font-weight:800; font-size:13px;}"
        )
        self.selected_axis_label.hide()

    def set_selected_axis(self, text) -> None:
        """Show (or hide, when text is falsy) the gamepad-selected-axis banner."""
        if text:
            self.selected_axis_label.setText(text)
            self.selected_axis_label.show()
            self.selected_axis_label.raise_()
            self._position_selected_axis_label()
        else:
            self.selected_axis_label.hide()

    def _position_selected_axis_label(self) -> None:
        if not self.selected_axis_label.isVisible():
            return
        # Cap the banner width to the image area (minus a small margin) BEFORE
        # adjustSize() so word-wrap kicks in and the pill can never be wider than the
        # frame; it grows downward (extra line) instead of spilling off the right edge.
        max_w = max(120, self._image_rect.width() - 8)
        self.selected_axis_label.setMaximumWidth(max_w)
        self.selected_axis_label.adjustSize()
        w = min(self.selected_axis_label.width(), max_w)
        x = self._image_rect.left() + (self._image_rect.width() - w) // 2
        y = self._image_rect.top() + 6
        self.selected_axis_label.move(max(self._image_rect.left() + 4, x), y)

    def sizeHint(self) -> QSize:
        return QSize(self.REFERENCE_SIZE.width(), min(self.REFERENCE_SIZE.height(), self.MAX_MAP_HEIGHT))

    def minimumSizeHint(self) -> QSize:
        return QSize(300, 300)

    def add_hotspot(
        self,
        button: QPushButton,
        label_x_pct: float,
        label_y_pct: float,
        tip_x_pct: float,
        tip_y_pct: float,
        width: int,
        height: int,
        color: str,
    ) -> None:
        self._hotspots.append(
            (
                button,
                label_x_pct / 100.0,
                label_y_pct / 100.0,
                tip_x_pct / 100.0,
                tip_y_pct / 100.0,
                width,
                height,
                color,
            )
        )
        button.raise_()
        self._layout_contents()

    def image_rect(self) -> QRect:
        return QRect(self._image_rect)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout_contents()

    def _layout_contents(self) -> None:
        available = self.rect().adjusted(
            self.MAP_PADDING,
            self.MAP_PADDING,
            -self.MAP_PADDING,
            -self.MAP_PADDING,
        )
        if available.width() <= 0 or available.height() <= 0:
            return

        if self._base_pixmap.isNull():
            self._image_rect = QRect(available)
            self.image_label.setGeometry(self._image_rect)
        else:
            scaled = self._base_pixmap.scaled(
                available.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            x = available.x() + (available.width() - scaled.width()) // 2
            y = available.y() + (available.height() - scaled.height()) // 2
            self._image_rect = QRect(x, y, scaled.width(), scaled.height())
            self.image_label.setPixmap(scaled)
            self.image_label.setGeometry(self._image_rect)

        ref_scale = min(
            self._image_rect.width() / self.REFERENCE_SIZE.width(),
            self._image_rect.height() / self.REFERENCE_SIZE.height(),
        )
        label_scale = max(0.82, min(1.0, ref_scale))
        # First pass: size each chip and record its desired (label-anchor) centre.
        # tip_x/tip_y (the part the arrow points at) ride along untouched so the
        # final pass can draw the arrow even after declutter moves the chip.
        placed = []  # [button, width, height, cx, cy, tip_x, tip_y, color]
        for button, x_pct, y_pct, tip_x, tip_y, base_width, base_height, color in self._hotspots:
            font = button.font()
            font.setPointSizeF(max(8.5, 10.0 * label_scale))
            button.setFont(font)
            width = max(46, int(base_width * label_scale), button.fontMetrics().horizontalAdvance(button.text()) + 18)
            height = max(26, int(base_height * label_scale))
            button.setFixedSize(width, height)
            cx = self._image_rect.left() + self._image_rect.width() * x_pct
            cy = self._image_rect.top() + self._image_rect.height() * y_pct
            placed.append([button, width, height, float(cx), float(cy), tip_x, tip_y, color])

        # Second pass: declutter. The label anchors are hand-placed clear of one
        # another, but at the minimum map size the scaled chips can still touch; this
        # nudges any overlapping pair apart. The arrow (drawn next) keeps each chip
        # tied to its part regardless of how far declutter moves it.
        self._declutter_hotspots(placed)

        # Final pass: clamp the chip inside the image, move it, and record the arrow
        # segment from the chip's final centre to its part tip.
        segments = []
        for button, width, height, cx, cy, tip_x, tip_y, color in placed:
            x = int(cx - width / 2)
            y = int(cy - height / 2)
            x = max(self._image_rect.left(), min(x, self._image_rect.right() - width + 1))
            y = max(self._image_rect.top(), min(y, self._image_rect.bottom() - height + 1))
            button.move(x, y)
            chip_cx = x + width / 2.0
            chip_cy = y + height / 2.0
            tip_px = self._image_rect.left() + self._image_rect.width() * tip_x
            tip_py = self._image_rect.top() + self._image_rect.height() * tip_y
            segments.append((chip_cx, chip_cy, float(tip_px), float(tip_py), color))

        self._arrow_overlay.setGeometry(self.rect())
        self._arrow_overlay.lower()
        self.image_label.lower()
        self._arrow_overlay.set_segments(segments)

        self._position_selected_axis_label()

    def _declutter_hotspots(self, placed: list, gap: int = 4, iterations: int = 40) -> None:
        """Separate overlapping hotspot chips while keeping each near its anchor.

        Standard 2D label de-overlap: each iteration pushes any overlapping pair
        apart along their axis of least penetration, then clamps every centre back
        inside the image. Converges quickly for the ~9 chips.
        """
        if len(placed) < 2:
            return
        left, right = self._image_rect.left(), self._image_rect.right()
        top, bottom = self._image_rect.top(), self._image_rect.bottom()
        for _ in range(iterations):
            moved = False
            for i in range(len(placed)):
                for j in range(i + 1, len(placed)):
                    a, b = placed[i], placed[j]
                    min_dx = (a[1] + b[1]) / 2.0 + gap
                    min_dy = (a[2] + b[2]) / 2.0 + gap
                    dx, dy = b[3] - a[3], b[4] - a[4]
                    overlap_x = min_dx - abs(dx)
                    overlap_y = min_dy - abs(dy)
                    if overlap_x > 0 and overlap_y > 0:
                        if overlap_x <= overlap_y:
                            shift = overlap_x / 2.0 + 0.5
                            sign = 1.0 if dx >= 0 else -1.0
                            a[3] -= sign * shift
                            b[3] += sign * shift
                        else:
                            shift = overlap_y / 2.0 + 0.5
                            sign = 1.0 if dy >= 0 else -1.0
                            a[4] -= sign * shift
                            b[4] += sign * shift
                        moved = True
            for p in placed:
                half_w, half_h = p[1] / 2.0, p[2] / 2.0
                p[3] = min(max(p[3], left + half_w), right - half_w)
                p[4] = min(max(p[4], top + half_h), bottom - half_h)
            if not moved:
                break


class MotionPadButton(QPushButton):
    # Operator-facing direction text so an icon-only pad cannot be misread; the
    # arrows mean "displayed coordinate increases/decreases" for every axis (the
    # per-axis controller sign is applied downstream, so + is always coord-up).
    _TOOLTIPS = {
        "continuous-negative": "连续移动 · 坐标减小（按住）",
        "continuous-positive": "连续移动 · 坐标增大（按住）",
        "jog-negative": "单步移动 · 坐标减小",
        "jog-positive": "单步移动 · 坐标增大",
        "stop": "停止该轴",
    }
    _ACCESSIBLE = {
        "continuous-negative": "连续移动 坐标减小",
        "continuous-positive": "连续移动 坐标增大",
        "jog-negative": "单步移动 坐标减小",
        "jog-positive": "单步移动 坐标增大",
        "stop": "停止该轴",
    }

    def __init__(self, symbol: str):
        super().__init__("")
        self.symbol = symbol
        self.setProperty("motionSymbol", symbol)
        self.setAccessibleName(tr(self._ACCESSIBLE.get(symbol, symbol)))
        self.setToolTip(tr(self._TOOLTIPS.get(symbol, symbol)))

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        if not self.isEnabled():
            color = QColor("#9aa8b6")
        elif self.isDown():
            color = QColor("#ffffff")
        else:
            color = QColor("#0f1720")
        center = self.rect().center()
        cx = float(center.x())
        cy = float(center.y())

        if self.symbol == "stop":
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(QRectF(cx - 4.5, cy - 4.5, 9.0, 9.0), 1.2, 1.2)
            return

        if self.symbol.startswith("continuous"):
            painter.setPen(QPen(color, 3.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            if self.symbol.endswith("negative"):
                painter.drawLine(QPointF(cx + 11.0, cy), QPointF(cx - 10.0, cy))
                painter.drawLine(QPointF(cx - 10.0, cy), QPointF(cx - 2.0, cy - 7.0))
                painter.drawLine(QPointF(cx - 10.0, cy), QPointF(cx - 2.0, cy + 7.0))
            else:
                painter.drawLine(QPointF(cx - 11.0, cy), QPointF(cx + 10.0, cy))
                painter.drawLine(QPointF(cx + 10.0, cy), QPointF(cx + 2.0, cy - 7.0))
                painter.drawLine(QPointF(cx + 10.0, cy), QPointF(cx + 2.0, cy + 7.0))
            return

        painter.setPen(QPen(color, 4.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if self.symbol.endswith("negative"):
            painter.drawLine(QPointF(cx + 4.0, cy - 8.0), QPointF(cx - 4.0, cy))
            painter.drawLine(QPointF(cx - 4.0, cy), QPointF(cx + 4.0, cy + 8.0))
        else:
            painter.drawLine(QPointF(cx - 4.0, cy - 8.0), QPointF(cx + 4.0, cy))
            painter.drawLine(QPointF(cx + 4.0, cy), QPointF(cx - 4.0, cy + 8.0))


@dataclass(order=True)
class _QueuedMotionCommand:
    priority: int
    sequence: int
    action: Callable[[], object] | None = field(compare=False)
    kind: str = field(default="", compare=False)
    metadata: object = field(default=None, compare=False)


class MotionCommandQueue(QObject):
    completed = Signal(object)
    failed = Signal(object)
    position_ready = Signal(object)
    position_failed = Signal(object)

    def __init__(self, parent: QObject | None = None, *, inline: bool = False):
        super().__init__(parent)
        # inline mode runs each submitted action synchronously on the calling
        # thread and emits its result signal before submit() returns. It exists
        # for deterministic tests; production always uses the threaded worker so
        # blocking hardware calls never run on the GUI thread.
        self._inline = inline
        self._sequence = count()
        self._closed = False
        # Per-axis latest-wins coalescing for continuous-jog commands. A stalled
        # board lets the single worker fall behind while the GUI keeps enqueuing
        # continuous jogs; without coalescing the board would replay a backlog of
        # stale velocity commands on recovery. We keep only the most recent
        # pending continuous command per axis: older ones are tombstoned (their
        # sequence recorded in _superseded) and skipped by the worker. Stops and
        # discrete relative moves are NEVER coalesced.
        self._latest_continuous: dict[object, int] = {}
        self._superseded: set[int] = set()
        # Optional hook invoked (on the caller's thread) after cancel_pending has
        # drained the queue, so the owner can drop its own per-command callbacks
        # for the commands that were just cancelled.
        self.on_cancel_pending: Callable[[], None] | None = None
        if inline:
            self._queue = None
            self._state_lock = None
            self._thread = None
            return
        self._queue: PriorityQueue[_QueuedMotionCommand] = PriorityQueue()
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="motion-command-queue", daemon=True)
        self._thread.start()

    CONTINUOUS_JOG_KIND = "continuous_jog"

    def submit(
        self,
        action: Callable[[], object],
        *,
        priority: int = 10,
        kind: str = "",
        metadata: object = None,
    ) -> bool:
        if self._inline:
            if self._closed:
                return False
            self._dispatch(action, kind, metadata)
            return True
        with self._state_lock:
            if self._closed:
                return False
            sequence = next(self._sequence)
            if kind == self.CONTINUOUS_JOG_KIND:
                # Supersede any still-pending continuous jog for the same axis.
                previous = self._latest_continuous.get(metadata)
                if previous is not None:
                    self._superseded.add(previous)
                self._latest_continuous[metadata] = sequence
            self._queue.put(_QueuedMotionCommand(priority, sequence, action, kind, metadata))
            return True

    def cancel_pending(self) -> int:
        """Drain all not-yet-started commands and drop their callbacks.

        Used by the emergency-stop paths so discrete jogs/relative moves already
        queued behind a stalled worker never fire after the stop. The currently
        executing command cannot be cancelled (the direct stop_all halts the
        hardware regardless). The worker and queue stay alive and usable. No-op
        in inline mode (nothing is ever pending there). Returns the number of
        commands dropped.
        """
        if self._inline:
            if self.on_cancel_pending is not None:
                self.on_cancel_pending()
            return 0
        dropped = 0
        with self._state_lock:
            while True:
                try:
                    command = self._queue.get_nowait()
                except Empty:
                    break
                # The shutdown sentinel (action is None) must never be discarded
                # here; re-queue it so a concurrent shutdown still terminates.
                if command.action is None:
                    self._queue.put(command)
                    self._queue.task_done()
                    break
                self._queue.task_done()
                dropped += 1
            self._latest_continuous.clear()
            self._superseded.clear()
        # Run the owner hook outside the lock so it can touch GUI state without
        # any risk of deadlocking against the worker thread.
        if self.on_cancel_pending is not None:
            self.on_cancel_pending()
        return dropped

    def pending_count(self) -> int:
        """Number of live (non-superseded) commands waiting to be dispatched."""
        if self._inline:
            return 0
        with self._state_lock:
            return max(0, self._queue.qsize() - len(self._superseded))

    def shutdown(self, timeout: float | None = 1.0) -> bool:
        if self._inline:
            self._closed = True
            return True
        with self._state_lock:
            if not self._closed:
                self._closed = True
                while True:
                    try:
                        self._queue.get_nowait()
                    except Empty:
                        break
                    else:
                        self._queue.task_done()
                self._latest_continuous.clear()
                self._superseded.clear()
                self._queue.put(_QueuedMotionCommand(-1000, next(self._sequence), None, "shutdown"))
        # Always join outside the lock: the worker takes _state_lock after pulling
        # each command (coalescing check), so joining while holding the lock would
        # deadlock a second shutdown() call against an in-flight worker.
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            command = self._queue.get()
            try:
                if command.action is None:
                    return
                skip = False
                with self._state_lock:
                    if command.sequence in self._superseded:
                        # A newer continuous jog for the same axis arrived while
                        # this one sat in the queue; drop the stale velocity.
                        self._superseded.discard(command.sequence)
                        skip = True
                    elif command.kind == self.CONTINUOUS_JOG_KIND:
                        # This is the surviving command for its axis; clear the
                        # marker so a later submit starts a fresh coalescing chain.
                        if self._latest_continuous.get(command.metadata) == command.sequence:
                            self._latest_continuous.pop(command.metadata, None)
                if skip:
                    continue
                self._dispatch(command.action, command.kind, command.metadata)
            finally:
                self._queue.task_done()

    def _dispatch(self, action: Callable[[], object], kind: str, metadata: object) -> None:
        # Catch every exception, not just MotionControllerError/ValueError. The
        # DLL layer raises bare RuntimeError on a nonzero status code, and ctypes
        # can raise ArgumentError/OverflowError. If any of those escaped here the
        # worker thread would die silently while submit() kept returning True,
        # turning the queue into a black hole. Report and keep the worker alive.
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - intentional catch-all; see comment
            if kind == "position_refresh":
                self.position_failed.emit((metadata, str(exc)))
            else:
                # Include kind so handlers can distinguish continuous jogs
                # (which never register a callback) from discrete commands.
                self.failed.emit((kind, metadata, str(exc)))
            return
        if kind == "position_refresh":
            self.position_ready.emit(result)
        else:
            # Include kind so handlers can distinguish continuous jogs
            # (which never register a callback) from discrete commands.
            self.completed.emit((kind, metadata, result))


class InlineBoardExecutor:
    """Default board-action executor: runs the action synchronously.

    Each open/close/reset orchestration step hands its blocking DLL call here;
    InlineBoardExecutor runs it immediately on the calling (GUI) thread and
    invokes on_done/on_error before submit() returns. This keeps the default
    behavior byte-for-byte identical to the previous inline synchronous path, so
    every existing test that clicks open/close/reset and then asserts
    controller.is_open / recorded calls immediately still passes unchanged.
    Production injects ThreadedBoardExecutor instead so the blocking GA_Open /
    GA_Close / GA_Reset calls never freeze the GUI thread.
    """

    def submit(self, action: Callable[[], object], on_done, on_error) -> None:
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - mirror on_error signalling
            on_error(exc)
            return
        on_done(result)

    def wait_for_idle(self, timeout_ms: int = 5000) -> None:
        return None

    def is_busy(self) -> bool:
        return False


class ThreadedBoardExecutor(QObject):
    """Production board-action executor: dispatch blocking calls off the GUI thread.

    A clicked 打开/关闭/复位 returns immediately; the slow GA_* network transaction
    runs on a Python worker thread and the result is drained by a GUI-thread QTimer
    before calling on_done/on_error. This keeps UI writes on the main Qt thread
    without relying on QThread signal lifetime during application teardown.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._results: Queue[tuple[Callable[[object], None], object]] = Queue()
        self._result_timer = QTimer(self)
        self._result_timer.setInterval(0)
        self._result_timer.timeout.connect(self._drain_results)

    def submit(self, action: Callable[[], object], on_done, on_error) -> None:
        def run() -> None:
            try:
                result = action()
            except Exception as exc:  # noqa: BLE001 - mirror on_error signalling
                self._results.put((on_error, exc))
            else:
                self._results.put((on_done, result))
            finally:
                with self._threads_lock:
                    self._threads.discard(threading.current_thread())

        thread = threading.Thread(target=run, name="ATSBoardAction", daemon=True)
        with self._threads_lock:
            self._threads.add(thread)
        thread.start()
        if not self._result_timer.isActive():
            self._result_timer.start()

    def _drain_results(self) -> None:
        while True:
            try:
                callback, value = self._results.get_nowait()
            except Empty:
                break
            callback(value)
        if not self.is_busy() and self._results.empty():
            self._result_timer.stop()

    def wait_for_idle(self, timeout_ms: int = 5000) -> None:
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        while True:
            with self._threads_lock:
                pending = [thread for thread in self._threads if thread.is_alive()]
            if not pending:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for thread in pending:
                thread.join(timeout=max(0.0, remaining))
                if time.monotonic() >= deadline:
                    return

    def is_busy(self) -> bool:
        with self._threads_lock:
            return any(thread.is_alive() for thread in self._threads)


class MainWindow(QMainWindow):
    # Watchdog: if a board open/close/reset action runs longer than this (a wedged
    # DLL call that never returns), surface a timeout error and re-enable the
    # buttons instead of leaving them disabled forever. 25 s is just over the
    # ~21 s OS TCP connect timeout for the unreachable board, so a genuinely slow
    # but completing connect is not falsely flagged.
    BOARD_ACTION_TIMEOUT_MS = 25000

    # Bounded wait on window close for the RD105 heater-off (ENABLE=0) to actually
    # reach the controller before the process exits. The RD105 is a standalone
    # controller — if the PC exits without sending ENABLE=0 it keeps heating — so
    # the close path must give the disconnect worker a deterministic chance to
    # finish, while still bounding the wait so a wedged serial driver cannot hang
    # close forever.
    MOTION_CLOSE_TIMEOUT_MS = 2000
    HEATER_OFF_CLOSE_TIMEOUT_MS = 3000

    # CP1 — reactive motion-safety scan backstop.
    #
    # A2: position-refresh watchdog. The runtime interference / soft-range
    # auto-stop (_stop_unsafe_axes_if_needed) is driven ONLY by the 100 ms
    # position-refresh tick → async queue read → _handle_async_position_refresh.
    # _position_refresh_pending is a latch with no timeout: if a DLL read HANGS
    # (never returns AND never raises — a stuck network read), the worker never
    # emits position_ready/position_failed, the latch stays True forever, and
    # every later tick short-circuits at "if self._position_refresh_pending:
    # return" — the reactive scan is permanently and silently dead while axes may
    # still be moving. This bounded single-shot watchdog DETECTS that hang.
    #
    # 2000 ms is ~20× the 100 ms refresh cadence: comfortably longer than any
    # healthy read (which completes in milliseconds), short enough that a genuine
    # hang is caught within ~2 s. The board-action watchdog (25 s) covers the
    # slow OS TCP connect path; a per-tick position read has no such slow-but-OK
    # case, so a much tighter deadline is correct here.
    POSITION_REFRESH_WATCHDOG_MS = 2000

    # A3: read-failure fail-safe. _read_positions_from_controller aborts the
    # whole positions dict on the first MotionControllerError, so any axis read
    # failing fails the entire refresh — during which a concurrent X2/X3
    # interference approach would be unmonitored. We count CONSECUTIVE failures:
    # a single transient blip is logged but tolerated (no nuisance stop); reaching
    # this many in a row while the board is open is treated as a real loss of the
    # safety scan → protective stop + persistent alarm + audit CRITICAL. 3 reads
    # at the 100 ms cadence is ~0.3 s of sustained read trouble — long enough to
    # rule out a one-off blip, short enough to react before an interference
    # approach completes. A single successful read resets the counter.
    CONSECUTIVE_READ_FAILURE_LIMIT = 3

    def __init__(
        self,
        controller: MotionController,
        position_store_path: str | Path = DEFAULT_POSITION_STORE_PATH,
        runtime_calibration_path: str | Path = DEFAULT_RUNTIME_CALIBRATION_PATH,
        microscope_position_store_path: str | Path = DEFAULT_MICROSCOPE_POSITION_STORE_PATH,
        nosepiece_controller: NosepieceController | None = None,
        nosepiece_controller_factory: Callable[[str], NosepieceController] | None = None,
        nosepiece_port_lister: Callable[[], list[object]] | None = None,
        nosepiece_port: str | None = None,
        temperature_controller_factory: Callable[[str], object] | None = None,
        temperature_port: str | None = None,
        temperature_baud: int | None = None,
        motion_command_queue_factory: Callable[[QObject], "MotionCommandQueue"] | None = None,
        board_action_executor: object | None = None,
        gamepad_reader=None,
        camera_controller_factory: Callable[[], object] | None = None,
        camera_snapshot_dir: str | Path = CAMERA_SNAPSHOT_DIR,
    ):
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            app.setFont(QFont("Microsoft YaHei", 9))
        self.controller = controller
        self.nosepiece_controller = nosepiece_controller or DisabledNosepieceController()
        self._axis9_home_generation = 0
        self.controls = {}
        from .i18n import translator, Retranslator
        self._translator = translator
        self._retranslator = Retranslator(translator)
        self._motion_command_queue_factory = motion_command_queue_factory or (lambda parent: MotionCommandQueue(parent))
        self._motion_command_queue: MotionCommandQueue | None = None
        # Board open/close/reset blocking DLL calls run through this executor.
        # Default is INLINE (synchronous) so existing tests and current behavior
        # are unchanged; production injects ThreadedBoardExecutor so the ~21 s OS
        # TCP timeout for an unreachable board never freezes the GUI thread.
        self._board_action_executor = board_action_executor if board_action_executor is not None else InlineBoardExecutor()
        # True while a threaded board action is in flight (buttons disabled, a
        # watchdog timer armed). Stays False on the inline path.
        self._board_action_in_flight = False
        # Monotonic token for board open/close/reset callbacks. A timeout or
        # close invalidates the token so a late worker cannot overwrite the alarm.
        self._board_action_generation = 0
        self._board_action_watchdog: QTimer | None = None
        self._motion_command_callback_ids = count()
        self._motion_command_callbacks: dict[int, tuple[str | None, Callable[[object], None] | None, bool]] = {}
        self._position_refresh_pending = False
        self._position_refresh_generation = 0
        # CP1/A2: single-shot watchdog armed whenever a position read is in
        # flight; disarmed on normal completion (success OR failure). If it FIRES
        # the read hung — see _on_position_refresh_watchdog_timeout. Created lazily
        # so it is parented to the window (stopped in closeEvent).
        self._position_refresh_watchdog: QTimer | None = None
        # CP1/A3: number of CONSECUTIVE position-read failures; reset on any
        # successful read. Reaching CONSECUTIVE_READ_FAILURE_LIMIT trips a
        # protective stop + alarm.
        self._consecutive_read_failures = 0
        # Set once the window begins tearing down so the 100 ms refresh timer (if
        # it fires during processEvents() inside close/disconnect teardown) does
        # not enqueue a position_refresh concurrent with queue shutdown.
        self._closing = False
        # Tracks whether a safety alarm is currently active (set by
        # _stop_unsafe_axes_if_needed) so the lamp transitions cleanly.
        self._safety_alarm_active = False
        # Axis currently selected by the gamepad (None when disarmed/unselected).
        # Stored so the stage-map banner can be re-rendered with a live coordinate
        # on every 100 ms position tick, not only when the selection changes.
        self._gamepad_selected_axis: int | None = None
        self.runtime_calibration_store = RuntimeAxisCalibrationStore(runtime_calibration_path)
        self.axis_calibrations = self.runtime_calibration_store.load()
        self._apply_axis_calibrations_to_controller()
        self.position_store = AxisPositionStore(position_store_path, axis_calibrations=self.axis_calibrations)
        self.position_store.load()
        self.microscope_position_store = MicroscopePositionStore(microscope_position_store_path)
        self.microscope_position_store.load()

        self._retranslator.title(self, "二维材料转移平台")
        self.resize(1720, 860)
        self.setMinimumSize(1320, 760)

        # Set True only when the user confirms Exit; the window-manager X is refused
        # unless this is set (see closeEvent), so the program can only quit through
        # the deliberate Exit button — which guarantees the heater-off + port-close
        # shutdown runs.
        self._exit_requested = False

        # Version pinned to the bottom-right corner (status-bar permanent widget is
        # right-aligned, so it stays put regardless of transient status messages).
        version_label = self._register("versionLabel", QLabel(f"v{__version__}"))
        version_label.setStyleSheet("color:#8a96a3; padding:0 8px;")
        self.statusBar().addPermanentWidget(version_label)

        # Exit button, bottom-right next to the version. The OS window X is disabled
        # (closeEvent refuses a spontaneous close); this is the only way to quit, and
        # it always runs the heater-off + close-all-ports shutdown after confirmation.
        exit_button = self._register("exitButton", QPushButton("退出 Exit"))
        exit_button.setStyleSheet(
            "QPushButton{background:#b42318;color:#ffffff;font-weight:800;border:none;"
            "border-radius:5px;padding:3px 14px;margin:0 4px;}"
            "QPushButton:hover{background:#9a1d14;}"
            "QPushButton:pressed{background:#7a1711;}"
        )
        exit_button.setCursor(Qt.PointingHandCursor)
        exit_button.clicked.connect(self._request_exit)
        self.statusBar().addPermanentWidget(exit_button)
        # Status-bar language toggle. Shows the OTHER language; clicking flips it and
        # the whole UI retranslates live (Retranslator observer).
        lang_toggle = self._register("languageToggle", QPushButton())
        lang_toggle.setCursor(Qt.PointingHandCursor)
        lang_toggle.setStyleSheet(
            "QPushButton{background:#1f6f5b;color:#ffffff;font-weight:800;border:none;"
            "border-radius:5px;padding:3px 12px;margin:0 4px;}"
            "QPushButton:hover{background:#175d4c;}"
        )
        lang_toggle.clicked.connect(self._toggle_language)
        self.statusBar().addPermanentWidget(lang_toggle)
        self.cutting_window = None
        self._cutting_disabled_widgets = []
        laser_button = self._register("laserCuttingButton", QPushButton())
        self._retranslator.text(laser_button, "激光切割")
        laser_button.clicked.connect(self.show_cutting_window)
        self.statusBar().addPermanentWidget(laser_button)
        self._retranslator.custom(
            lambda b=lang_toggle: b.setText("中文" if self._translator.lang == "en" else "EN")
        )

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        self.objective_panel = ObjectivePanel(
            self.nosepiece_controller,
            register=self._register,
            status_callback=self.statusBar().showMessage,
            controller_factory=nosepiece_controller_factory,
            port_lister=nosepiece_port_lister,
            initial_port=nosepiece_port,
        )
        self.temperature_panel = TemperaturePanel(
            register=self._register,
            controller_factory=temperature_controller_factory,
            initial_port=temperature_port,
            baudrate=temperature_baud or 38400,
        )
        self.camera_window = self._register("cameraWindow", CameraWindow(
            register=self._register,
            controller_factory=camera_controller_factory,
            snapshot_dir=camera_snapshot_dir,
        ))
        self.objective_panel.objective_changed.connect(self.camera_window.set_objective)
        self.camera_window.close_guard = lambda: not self._cutting_busy()

        root.addWidget(self._build_stage_panel(), 30)
        root.addWidget(self._build_axis_control_panel(), 46)
        root.addWidget(self._register("temperaturePanel", self.temperature_panel), 24)

        self.gamepad_panel = None
        if gamepad_reader is not None:
            self.gamepad_panel = GamepadPanel(
                register=self._register,
                motion_queue_provider=self._active_motion_queue,
                motion_controller=self.controller,
                stop_all=self._gamepad_stop_all,
                speed_fn=self._gamepad_speed,
                step_fn=self._step,
                set_step_fn=self._gamepad_set_step,
                direction_fn=self._controller_direction_for_coordinate_direction,
                highlight_fn=self.highlight_axis,
                on_armed_changed=self.set_gamepad_armed,
                step_display_fn=lambda axis: float(self.controls[self._step_control_name(axis)].value()),
                info_fn=self._gamepad_hud_detail,
                reader=gamepad_reader,
            )
            self._stage_panel_layout.addWidget(self.gamepad_panel)
            # Disarm the gamepad whenever the WHOLE APPLICATION loses foreground, not
            # just when this window deactivates (changeEvent). The XInput reader keeps
            # delivering stick state while the app is backgrounded, so without this a
            # stick left deflected could keep an axis jogging after the operator
            # alt-tabbed away. applicationStateChanged fires immediately (not on the
            # 30 ms tick), and changeEvent remains as a secondary trigger.
            app = QApplication.instance()
            if app is not None:
                app.applicationStateChanged.connect(self._on_application_state_changed)

        initial_status = [""]

        def refresh_initial_status():
            bar = self.statusBar()
            if bar.currentMessage() == initial_status[0]:
                initial_status[0] = tr("真实硬件模式 - 运动控制未打开")
                bar.showMessage(initial_status[0])

        self._retranslator.custom(refresh_initial_status)
        self._report_persistence_warnings()
        self._load_cached_position_labels()

        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh_positions)
        self.timer.start()

    def _register(self, name: str, widget):
        widget.setObjectName(name)
        self.controls[name] = widget
        return widget

    def _report_persistence_warnings(self) -> None:
        """Surface any store-load problems (rejected/distrusted persisted state).

        A corrupted, hand-edited, or future-schema persistence file is silently
        ignored on load (falling back to the built-in calibration / empty
        state); show that in the status bar so the operator knows the safety
        mapping or saved positions did not come from disk as expected.
        """
        warnings: list[str] = []

        skipped = getattr(self.runtime_calibration_store, "skipped", None)
        if skipped:
            labels = ", ".join(AXIS_LABELS.get(axis, f"Axis{axis}") for axis in skipped)
            warnings.append(tr("轴标定文件中 {labels} 超出范围已忽略，改用内置标定").format(labels=labels))
        if getattr(self.runtime_calibration_store, "schema_warning", False):
            warnings.append(tr("轴标定文件版本不受支持，改用内置标定"))
        if getattr(self.position_store, "schema_warning", False):
            warnings.append(tr("位置缓存文件版本不受支持，已忽略"))
        if getattr(self.microscope_position_store, "schema_warning", False):
            warnings.append(tr("显微镜点位文件版本不受支持，已忽略"))

        if warnings:
            self.statusBar().showMessage("；".join(warnings))

    def _apply_axis_calibrations_to_controller(self) -> None:
        setter = getattr(self.controller, "set_axis_calibrations", None)
        if callable(setter):
            setter(self.axis_calibrations)

    def _refresh_axis_calibration_mapping(self) -> None:
        self.axis_calibrations = self.runtime_calibration_store.calibrations
        self.position_store.set_axis_calibrations(self.axis_calibrations)
        self._apply_axis_calibrations_to_controller()

    def _axis_for_label(self, axis_name: str) -> int:
        for axis, label in AXIS_LABELS.items():
            if label == axis_name:
                return int(axis)
        raise ValueError(f"unknown axis label: {axis_name}")

    def _close_existing_dialog(self, name: str) -> None:
        dialog = self.controls.get(name)
        if isinstance(dialog, QDialog):
            dialog.close()
            dialog.deleteLater()

    def _build_stage_panel(self) -> QWidget:
        panel = self._register("stagePanel", QFrame())
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setStyleSheet(
            """
            #stagePanel {
                background: #0e1720;
                border: 1px solid #1e2936;
                border-radius: 8px;
            }
            #stagePanel QLabel {
                color: #e8eef6;
            }
            """
        )
        root = QVBoxLayout(panel)
        self._stage_panel_layout = root
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        root.addWidget(self._build_stage_map(), 1)
        section = self._register("axisSectionMicroscope", QWidget())
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(8)
        title = self._register("stageMicroscopeTitle", QLabel())
        title.setMinimumHeight(18)
        title.setStyleSheet("color: #f6f8fb; font-weight: 800;")
        self._retranslator.text(title, "显微镜调整")
        section_layout.addWidget(title)
        section_layout.addWidget(
            self._build_axis_card(
                3,
                "axisCardMicroscopeZ",
                "MicroscopeZ",
                "显微镜上下位移",
                SCOPE_COLOR,
                position_text="pulse 0",
                unit="pulse",
                speed_unit="um/s",
                step_unit="um",
                step_value=500,
                position_editable=False,
            )
        )
        self._add_axis3_mark_controls(section_layout)
        root.addWidget(section)
        root.addWidget(self._register("objectivePanel", self.objective_panel))
        return panel

    def show_camera_window(self) -> None:
        self.camera_window.show()
        self.camera_window.raise_()
        self.camera_window.activateWindow()

    def _cutting_busy(self) -> bool:
        return self.cutting_window is not None and (
            self.cutting_window.busy or getattr(self.cutting_window, "interlocked", False) is True)

    def show_cutting_window(self) -> None:
        if self.cutting_window is None:
            from .cutting_window import CuttingWindow
            self.cutting_window = CuttingWindow(self.controller, self.camera_window.camera_panel)
            self.cutting_window.start_guard = self._check_cutting_start
            self.cutting_window.busy_changed.connect(self._set_cutting_busy)
        self.cutting_window.show()
        self.cutting_window.raise_()
        self.cutting_window.activateWindow()

    def _check_cutting_start(self) -> None:
        if self.gamepad_panel is not None and self.gamepad_panel._controller.enabled:
            raise MotionControllerError(tr("请先停用手柄控制"))
        if self._closing or self._board_action_in_flight or self._board_action_executor.is_busy():
            raise MotionControllerError(tr("其他设备操作尚未完成"))
        for name in ("_move_thread", "_connect_thread", "_read_thread"):
            worker = getattr(self.objective_panel, name, None)
            if worker is not None and worker.isRunning():
                raise MotionControllerError(tr("其他设备操作尚未完成"))
        camera = self.camera_window.camera_panel
        if camera._task_executor.is_busy() or camera._closing:
            raise MotionControllerError(tr("其他设备操作尚未完成"))

    def _set_cutting_busy(self, busy: bool) -> None:
        if busy:
            if self.gamepad_panel is not None:
                self.gamepad_panel.disarm()
            if self._motion_command_queue is not None:
                self._motion_command_queue.cancel_pending()
            widgets = list(self.controls["axisControlPanel"].findChildren(QWidget))
            # Microscope buttons live beside the stage image, outside axisControlPanel.
            widgets += [w for key, w in self.controls.items()
                        if key.endswith("3") and isinstance(w, (QPushButton, QAbstractSpinBox))]
            widgets += [self.objective_panel, self.camera_window.camera_panel]
            if self.gamepad_panel is not None:
                widgets.append(self.gamepad_panel)
            seen = set()
            recorded = {widget for widget, _ in self._cutting_disabled_widgets}
            for widget in widgets:
                if widget in seen or widget is self.controls.get("btnStopAll"):
                    continue
                seen.add(widget)
                if isinstance(widget, (QPushButton, QAbstractSpinBox, QComboBox)) or widget in (
                    self.objective_panel, self.camera_window.camera_panel, self.gamepad_panel
                ):
                    if widget not in recorded:
                        self._cutting_disabled_widgets.append((widget, widget.isEnabled()))
                    widget.setEnabled(False)
        else:
            for widget, enabled in self._cutting_disabled_widgets:
                widget.setEnabled(enabled)
            self._cutting_disabled_widgets.clear()

    def _abort_cutting(self) -> None:
        if self.cutting_window is not None:
            self.cutting_window.abort()

    def _build_stage_map(self) -> QFrame:
        stage_map = self._register("stageMap", StageMapWidget(STAGE_OVERVIEW_IMAGE))
        self._register("stageImageLabel", stage_map.image_label)
        # Roles/colours stay here (presentation); the part-tip and label anchors come
        # from STAGE_MAP_AXIS_POSITIONS / STAGE_MAP_AXIS_LABEL_POSITIONS. NOTE: those
        # photo tables feed ONLY these device-map hotspots — the gamepad's 2D
        # face-button selector navigates by the separate SELECTOR_GRID_POSITIONS (GUI
        # card grid), intentionally decoupled.
        for axis_num, role, color in (
            (1, "整体左右", STAGE_COLOR),
            (2, "整体前后", STAGE_COLOR),
            (3, "显微镜上下", SCOPE_COLOR),
            (4, "载玻片升降", SLIDE_COLOR),
            (5, "吸附台左右", ADSORB_COLOR),
            (6, "吸附台前后", ADSORB_COLOR),
            (7, "载玻片左右", SLIDE_COLOR),
            (8, "载玻片前后", SLIDE_COLOR),
            (9, "样品旋转", ADSORB_COLOR),
        ):
            tip_x, tip_y = STAGE_MAP_AXIS_POSITIONS[axis_num]
            label_x, label_y = STAGE_MAP_AXIS_LABEL_POSITIONS[axis_num]
            self._add_stage_hotspot(
                stage_map, AXIS_LABELS[axis_num], role, label_x, label_y, tip_x, tip_y, color
            )
        return stage_map

    def _add_stage_hotspot(
        self,
        parent: StageMapWidget,
        axis: str,
        role: str,
        label_x_pct: float,
        label_y_pct: float,
        tip_x_pct: float,
        tip_y_pct: float,
        color: str,
    ) -> None:
        # Show a short, single-line axis code on the chip and put the full
        # "轴 · 角色" in a tooltip. Two-line "MicroscopeZ\n显微镜上下" overflowed and
        # overlapped neighbours at the minimum window size, risking a wrong-axis
        # misclick; a compact single-line chip stays legible and packs tighter.
        short = {"MicroscopeZ": "镜Z"}.get(axis, axis)
        button = self._register(f"axisHotspot{axis}", QPushButton(parent))
        # Chip text: axis codes (X1, Y2, …) are identifiers and pass through tr
        # unchanged; only "镜Z" translates (-> "Microscope Z"). Registered so it
        # re-applies on a live language switch.
        self._retranslator.text(button, short)
        button.setCursor(Qt.PointingHandCursor)
        self._retranslator.custom(
            lambda b=button, a=axis, r=role: b.setToolTip(
                tr("{axis} · {role}").format(axis=a, role=tr(r))
            )
        )
        button.setStyleSheet(
            f"""
            QPushButton {{
                color: #111a22;
                background: rgba(255, 255, 255, 235);
                border: 2px solid {color};
                border-radius: 7px;
                padding: 2px 5px;
                font-weight: 700;
                text-align: center;
            }}
            QPushButton:hover {{
                background: #fffdf7;
            }}
            QPushButton:pressed {{
                background: #d3deea;
                border-color: #6f8194;
                padding-top: 3px;
                padding-bottom: 1px;
            }}
            QPushButton[selectedAxis="true"] {{
                border: 3px solid #f59e0b;
                background: #fff7e6;
            }}
            """
        )
        parent.add_hotspot(button, label_x_pct, label_y_pct, tip_x_pct, tip_y_pct, 54, 30, color)
        if axis == "MicroscopeZ":
            self._retranslator.custom(parent._layout_contents)
        button.clicked.connect(lambda checked=False, axis=axis: self._set_motion_status("{axis} 已选中", "off", axis=axis))

    def _build_axis_control_panel(self) -> QWidget:
        panel = self._register("axisControlPanel", QFrame())
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setStyleSheet(
            """
            #axisControlPanel {
                background: #f8fafc;
                border: 1px solid #dfe6ee;
                border-radius: 8px;
            }
            #axisControlPanel QPushButton:pressed {
                background: #d3deea;
                border-color: #90a4ba;
                padding-top: 4px;
                padding-bottom: 2px;
            }
            #axisControlPanel QPushButton:disabled {
                background: #e8ecf0;
                color: #9aa8b6;
                border-color: #cdd5dd;
            }
            """
        )
        root = QVBoxLayout(panel)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(10)

        title_row = QHBoxLayout()
        title = self._register("axisControlTitle", QLabel())
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #142033;")
        self._retranslator.text(title, "轴控制面板")
        calibration_button = self._register("btnCoordinateCalibration", QPushButton())
        self._retranslator.text(calibration_button, "坐标标定")
        calibration_button.setFixedHeight(28)
        calibration_button.setMinimumWidth(82)
        calibration_button.setStyleSheet(
            "border: 1px solid #cbd6e2; border-radius: 5px; "
            "background: #ffffff; color: #243140; font-weight: 800;"
        )
        calibration_button.clicked.connect(self._open_position_calibration_dialog)
        title_row.addWidget(title)
        title_row.addWidget(calibration_button)
        title_row.addStretch(1)
        title_row.addLayout(self._build_board_controls())
        root.addLayout(title_row)
        root.addWidget(self._build_axis_status_bar())

        scroll = self._register("axisScrollArea", QScrollArea())
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        content = QWidget()
        content.setMinimumWidth(520)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)
        content_layout.setAlignment(Qt.AlignTop)
        top_sections = self._register("axisTopSections", QWidget())
        top_layout = QHBoxLayout(top_sections)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(8)

        adsorb = self._build_axis_section("axisSectionAdsorption", "吸附台")
        adsorb.layout().addWidget(self._build_axis_card(5, "axisCardX3", "X3", "吸附台左右移动", ADSORB_COLOR))
        adsorb.layout().addWidget(self._build_axis_card(6, "axisCardY3", "Y3", "吸附台前后移动", ADSORB_COLOR))
        adsorb.layout().addWidget(self._build_axis_card(9, "axisCardR", "R", "样品旋转", ADSORB_COLOR, position_text="0.000 deg", unit="deg", speed_unit="deg/s", step_unit="deg", step_value=1.0))

        slide = self._build_axis_section("axisSectionSlide", "载玻片侧")
        slide.layout().addWidget(self._build_axis_card(7, "axisCardX2", "X2", "载玻片左右移动", SLIDE_COLOR))
        slide.layout().addWidget(self._build_axis_card(8, "axisCardY2", "Y2", "载玻片前后移动", SLIDE_COLOR))
        slide.layout().addWidget(self._build_axis_card(4, "axisCardZ", "Z", "载玻片升降", SLIDE_COLOR, position_text="5.000 mm"))

        overall = self._build_axis_section("axisSectionOverall", "整体位移")
        overall_cards = QHBoxLayout()
        overall_cards.setContentsMargins(0, 0, 0, 0)
        overall_cards.setSpacing(10)
        overall_cards.addWidget(self._build_axis_card(1, "axisCardX1", "X1", "整体左右平移", STAGE_COLOR, position_text="50.000 mm", step_value=1000))
        overall_cards.addWidget(self._build_axis_card(2, "axisCardY1", "Y1", "整体前后平移", STAGE_COLOR, position_text="25.000 mm", step_value=1000))
        overall.layout().addLayout(overall_cards)

        top_layout.addWidget(adsorb, 1)
        top_layout.addWidget(slide, 1)
        content_layout.addWidget(top_sections)
        content_layout.addWidget(overall)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        return panel

    def _build_axis_section(self, name: str, title: str) -> QWidget:
        section = self._register(name, QWidget())
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignTop)
        heading = QLabel()
        heading.setStyleSheet("font-size: 14px; font-weight: 800; color: #334154;")
        self._retranslator.text(heading, title)
        layout.addWidget(heading)
        return section

    def _build_axis_card(
        self,
        axis: int,
        card_name: str,
        axis_name: str,
        role: str,
        color: str,
        position_text: str = "0.000 mm",
        unit: str = "mm",
        speed_unit: str = "um/s",
        step_unit: str = "um",
        step_value: float = 500,
        position_editable: bool = True,
    ) -> QWidget:
        card = self._register(card_name, QFrame())
        card.setFrameShape(QFrame.StyledPanel)
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card.setMinimumWidth(250)
        card.setStyleSheet(
            f"""
            #{card_name} {{
                background: #ffffff;
                border: 1px solid #dde5ef;
                border-left: 5px solid {color};
                border-radius: 8px;
            }}
            #{card_name}[safetyAlarm="true"] {{
                border: 2px solid #dc2626;
                border-left: 5px solid #dc2626;
                background: #fff5f5;
            }}
            #{card_name}[selectedAxis="true"] {{
                border: 2px solid #f59e0b;
                border-left: 5px solid {color};
            }}
            """
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(3)

        header = QGridLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setHorizontalSpacing(8)
        header.setVerticalSpacing(2)
        name_label = QLabel("Microscope Z" if axis == 3 else axis_name)
        name_label.setStyleSheet(f"font-size: 18px; font-weight: 900; color: {color};")
        role_label = QLabel()
        role_label.setStyleSheet("font-size: 13px; font-weight: 800; color: #2e3a47;")
        self._retranslator.text(role_label, role)
        header.addWidget(name_label, 0, 0)
        header.addWidget(role_label, 0, 1)
        header_columns = 2
        if axis == 9:
            header_columns = 3
            btn_home = self._register("btnAxis9Home", QPushButton())
            self._retranslator.text(btn_home, "角度回零")
            btn_home.setFixedHeight(24)
            btn_home.setMinimumWidth(76)
            btn_home.setStyleSheet(
                "border: 1px solid #cbd6e2; border-radius: 5px; "
                "background: #ffffff; color: #243140; font-weight: 700;"
            )
            btn_home.clicked.connect(self._home_axis9)
            header.addWidget(btn_home, 0, 2, Qt.AlignRight | Qt.AlignVCenter)
        elif axis == 4:
            header_columns = 3
            continuous_button = self._register("btnZContinuousControl", QPushButton())
            self._retranslator.text(continuous_button, "连续控制")
            continuous_button.setFixedHeight(24)
            continuous_button.setMinimumWidth(76)
            continuous_button.setStyleSheet(
                "border: 1px solid #cbd6e2; border-radius: 5px; "
                "background: #ffffff; color: #243140; font-weight: 700;"
            )
            continuous_button.clicked.connect(self._open_z_continuous_dialog)
            header.addWidget(continuous_button, 0, 2, Qt.AlignRight | Qt.AlignVCenter)

        if position_editable:
            initial_position = position_text.replace(f" {unit}", "")
            position = QLineEdit(initial_position)
            position.setAlignment(Qt.AlignRight)
            position.setReadOnly(True)
            position.setFocusPolicy(Qt.NoFocus)
            position.setStyleSheet(
                "background: #f1f5f9; border: 0; border-radius: 5px; "
                "padding: 2px 7px; color: #244264;"
            )
            self._register(f"axisPosition{axis_name}", position)
            position_row = QHBoxLayout()
            position_row.setContentsMargins(0, 0, 0, 0)
            position_row.setSpacing(4)
            position_row.addWidget(position, 1)
            unit_label = QLabel(unit)
            unit_label.setStyleSheet("color: #526174;")
            position_row.addWidget(unit_label)
            if axis == 9:
                twist_button = self._register("btnAxis9Twist", QPushButton("twist"))
                twist_button.setFixedWidth(48)
                twist_button.setFixedHeight(24)
                twist_button.setStyleSheet(
                    "border: 1px solid #cbd6e2; border-radius: 5px; "
                    "background: #ffffff; color: #243140; font-weight: 700;"
                )
                twist_button.clicked.connect(self._open_axis9_twist_dialog)
                position_row.addWidget(twist_button)
            header.addLayout(position_row, 1, 0, 1, header_columns)

            target_label = QLabel()
            target_label.setStyleSheet("color: #526174; font-size: 12px; font-weight: 700;")
            self._retranslator.text(target_label, "目标")
            target = self._register(f"axisTarget{axis_name}", QLineEdit())
            target.setAlignment(Qt.AlignRight)
            target.setPlaceholderText(initial_position)
            target.setStyleSheet(
                "background: #ffffff; border: 1px solid #cbd6e2; border-radius: 5px; "
                "padding: 2px 7px; color: #12345a;"
            )
            move_button = self._register(f"btnMoveTo{axis_name}", QPushButton())
            self._retranslator.text(move_button, "移动")
            move_button.setFixedWidth(48)
            move_button.setFixedHeight(24)
            move_button.setStyleSheet(
                "border: 1px solid #cbd6e2; border-radius: 5px; "
                "background: #eef4fa; color: #142033; font-weight: 800;"
            )
            move_button.clicked.connect(
                lambda checked=False, axis=axis, axis_name=axis_name, unit=unit: self._move_axis_to_target(
                    axis,
                    axis_name,
                    unit,
                )
            )
            target_row = QHBoxLayout()
            target_row.setContentsMargins(0, 0, 0, 0)
            target_row.setSpacing(4)
            target_row.addWidget(target_label)
            target_row.addWidget(target, 1)
            target_unit = QLabel(unit)
            target_unit.setStyleSheet("color: #526174;")
            target_row.addWidget(target_unit)
            target_row.addWidget(move_button)
            header.addLayout(target_row, 2, 0, 1, header_columns)
        else:
            position_label = QLabel(position_text)
            position_label.setStyleSheet(
                "background: #f1f5f9; border-radius: 5px; padding: 4px 7px; color: #526174;"
            )
            if axis == 3:
                self._register("labelAxis3Pos", position_label)
            header.addWidget(position_label, 1, 0, 1, header_columns)
        header.setColumnStretch(1, 1)
        layout.addLayout(header)

        layout.addLayout(self._build_motion_pad(axis, color))
        layout.addLayout(self._build_axis_settings(axis, speed_unit, step_unit, step_value))

        return card

    def _build_motion_pad(self, axis: int, color: str) -> QHBoxLayout:
        pad = QHBoxLayout()
        pad.setContentsMargins(0, 0, 0, 0)
        pad.setSpacing(0)
        buttons = (
            (f"buttonback{axis}", "continuous-negative", lambda checked=False, axis=axis: self._start_jog(axis, -1)),
            (f"btnBack{axis}", "jog-negative", lambda checked=False, axis=axis: self._relative_move(axis, -1)),
            (f"btnStop{axis}", "stop", lambda checked=False, axis=axis: self._stop_axis(axis)),
            (f"btnFwd{axis}", "jog-positive", lambda checked=False, axis=axis: self._relative_move(axis, 1)),
            (f"buttonfwd{axis}", "continuous-positive", lambda checked=False, axis=axis: self._start_jog(axis, 1)),
        )
        for index, (name, symbol, slot) in enumerate(buttons):
            button = self._register(name, MotionPadButton(symbol))
            button.setFixedHeight(30)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"""
                QPushButton {{
                    border: 1px solid #707780;
                    border-left: 0;
                    border-radius: 0;
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #e4e6e9, stop:1 #cfd3d8);
                    color: #0f1720;
                }}
                QPushButton:pressed {{
                    background: {color};
                    color: #ffffff;
                }}
                """
            )
            if index == 0:
                button.setStyleSheet(button.styleSheet() + "QPushButton { border-left: 1px solid #707780; border-top-left-radius: 6px; border-bottom-left-radius: 6px; }")
            if index == 4:
                button.setStyleSheet(button.styleSheet() + "QPushButton { border-top-right-radius: 6px; border-bottom-right-radius: 6px; }")
            if name.startswith("button"):
                button.pressed.connect(slot)
                button.released.connect(lambda axis=axis: self._stop_continuous_jog(axis))
            else:
                button.clicked.connect(slot)
            pad.addWidget(button, 1)

        return pad

    def _build_axis_settings(self, axis: int, speed_unit: str, step_unit: str, step_value: float) -> QGridLayout:
        settings = QGridLayout()
        settings.setContentsMargins(0, 0, 0, 0)
        settings.setHorizontalSpacing(8)
        settings.setVerticalSpacing(0)
        speed_label = QLabel()
        step_label = QLabel()
        speed_label.setStyleSheet("color: #526174; font-size: 12px;")
        step_label.setStyleSheet("color: #526174; font-size: 12px;")
        self._retranslator.custom(lambda lbl=speed_label, u=speed_unit: lbl.setText(tr("速度\n({speed_unit})").format(speed_unit=u)))
        self._retranslator.custom(lambda lbl=step_label, u=step_unit: lbl.setText(tr("步长\n({step_unit})").format(step_unit=u)))
        settings.addWidget(speed_label, 0, 0)
        settings.addWidget(self._make_speed_combo(f"txtSpeed{axis}", axis), 0, 1)
        settings.addWidget(step_label, 0, 2)
        settings.addWidget(self._make_step_control(f"txtStep{axis}", axis, step_value), 0, 3)
        settings.setColumnStretch(1, 1)
        settings.setColumnStretch(3, 1)
        return settings

    def _add_axis3_mark_controls(self, layout: QVBoxLayout) -> None:
        row = QGridLayout()
        row.setContentsMargins(0, 0, 0, 0)
        btn_zero = self._register("btnAxis3Zero", QPushButton())
        self._retranslator.text(btn_zero, "标记零点")
        btn_positions = self._register("btnAxis3Positions", QPushButton())
        self._retranslator.text(btn_positions, "点位管理")
        btn_zero.clicked.connect(self._zero_axis3)
        btn_positions.clicked.connect(self._open_axis3_positions_dialog)
        row.addWidget(btn_zero, 0, 0)
        row.addWidget(btn_positions, 0, 1)
        layout.addLayout(row)

    def _build_axis_status_bar(self) -> QWidget:
        bar = self._register("axisStatusBar", QFrame())
        bar.setStyleSheet(
            """
            #axisStatusBar {
                background: #ffffff;
                border: 1px solid #d7e1ec;
                border-radius: 7px;
            }
            """
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)
        lamp = self._register("motionStatusLamp", QFrame())
        lamp.setFixedSize(11, 11)
        layout.addWidget(lamp)
        label = self._register("motionStatusText", QLabel())
        label.setStyleSheet("color: #526174; font-weight: 700;")
        layout.addWidget(label)
        layout.addStretch(1)
        self._set_motion_status("运动控制未打开 - 预览模式", "off")
        return bar

    def _build_board_controls(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        btn_stop_all = self._register("btnStopAll", QPushButton())
        self._retranslator.text(btn_stop_all, "停止移动")
        btn_open = self._register("btnOpen", QPushButton())
        self._retranslator.text(btn_open, "打开运动控制")
        btn_close = self._register("btnClose", QPushButton())
        self._retranslator.text(btn_close, "关闭运动控制")
        btn_reset = self._register("button4", QPushButton())
        self._retranslator.text(btn_reset, "复位")

        btn_stop_all.setStyleSheet(
            """
            QPushButton {
                background: #dc2626;
                color: #ffffff;
                border: 1px solid #b91c1c;
                border-radius: 6px;
                padding: 7px 16px;
                font-weight: 800;
                font-size: 14px;
            }
            QPushButton:pressed {
                background: #991b1b;
            }
            """
        )
        btn_stop_all.setMinimumSize(150, 56)
        font = btn_stop_all.font()
        font.setBold(True)
        font.setPointSize(max(font.pointSize(), 11))
        btn_stop_all.setFont(font)
        self._retranslator.tooltip(btn_stop_all, "急停 (快捷键: Space)")
        btn_stop_all.clicked.connect(self._stop_all_motion)

        # Global keyboard shortcut: Space → emergency stop
        stop_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        stop_shortcut.setContext(Qt.ApplicationShortcut)
        stop_shortcut.activated.connect(self._stop_all_motion)
        self._register("shortcutStopAll", stop_shortcut)

        btn_open.clicked.connect(lambda: self._run_board_action("open"))
        btn_close.clicked.connect(lambda: self._run_board_action("close"))
        btn_reset.clicked.connect(lambda: self._run_board_action("reset"))

        layout.addWidget(btn_stop_all)
        layout.addWidget(btn_open)
        layout.addWidget(btn_close)
        layout.addWidget(btn_reset)
        layout.addStretch(1)
        return layout

    # ---- Board open/close/reset --------------------------------------------
    #
    # Each public method below stays SYNCHRONOUS and self-contained: it runs the
    # blocking DLL call inline and emits its audit log + status on return. This is
    # what the audit-logging tests and any direct caller rely on, and it is what
    # the InlineBoardExecutor (default) runs unchanged. _run_board_action splits
    # each into (GUI-thread pre-step, blocking DLL call, GUI-thread finish) and
    # routes only the blocking DLL call through the executor, so the production
    # ThreadedBoardExecutor keeps the ~21 s board timeout off the GUI thread while
    # the inline default reproduces the old synchronous behavior byte-for-byte.

    def _open_motion_control(self) -> None:
        self.controller.open_board()
        self._open_motion_control_finish()

    def _open_motion_control_finish(self) -> None:
        _audit.info("motion board opened successfully")
        self._set_motion_status("运动控制已打开", "on")

    def _close_motion_control(self) -> None:
        self._axis9_home_generation += 1
        self._close_motion_control_prepare()
        self._close_motion_control_close_board()

    def _close_motion_control_prepare(self) -> None:
        """GUI-thread, must stay synchronous/ordered: tear the motion queue and
        position-refresh timer down BEFORE the board is closed.

        The motion-queue shutdown(timeout=2.0) is a bounded 2 s wait (not the
        21 s board-close freeze), so it stays on the GUI thread."""
        # Stop the position-refresh timer before tearing the queue down: the
        # teardown below calls processEvents() indirectly (e.g. temperature
        # disconnect), during which a pending 100 ms timeout could otherwise fire
        # refresh_positions and enqueue a position_refresh racing the shutdown.
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        # CP1/A2: disarm the position-refresh watchdog before the queue is torn
        # down so it cannot fire after the board is closed.
        self._disarm_position_refresh_watchdog()
        self._position_refresh_generation += 1
        motion_queue = self._motion_command_queue
        if motion_queue is not None:
            if not motion_queue.shutdown(timeout=2.0):
                raise MotionControllerError(tr("连续运动命令仍在执行，未关闭运动控制"))
            self._motion_command_queue = None
            self._motion_command_callbacks.clear()
            self._position_refresh_pending = False
        if self.controller.is_open:
            try:
                self.controller.stop_all()
            except MotionControllerError as exc:
                self.statusBar().showMessage(tr("关闭前停止失败: {exc}").format(exc=exc))

    def _close_motion_control_close_board(self) -> None:
        """Blocking GA_Close DLL call (routed through the executor) plus its
        success/failure status + audit handling."""
        try:
            self.controller.close_board()
        except MotionControllerError as exc:
            # GA_Close failed: the board may still report open. Reflect the real
            # state on the prominent status bar instead of leaving the previous
            # text, then re-raise so the failure dialog is shown. The queue was
            # already torn down but _active_motion_queue rebuilds it lazily while
            # is_open stays True, so motion remains recoverable.
            if self.controller.is_open:
                _audit.critical("motion board close FAILED (board still open): %s", exc)
                self._set_motion_status("关闭运动控制失败，运动卡仍打开", "on")
            raise
        _audit.info("motion board closed successfully")
        self._set_motion_status("运动控制已关闭", "off")

    def _reset_motion_control(self) -> None:
        self.controller.reset_board()
        self._reset_motion_control_finish()

    def _reset_motion_control_finish(self) -> None:
        _audit.info("motion board reset successfully")
        self._set_motion_status("运动控制已复位", "reset")

    # Per-kind orchestration metadata for _run_board_action: the prepare step
    # (GUI thread), the blocking DLL action (routed through the executor), the
    # finish step (GUI thread, on success), the in-flight status text, and the
    # success message shown on the bottom status bar.
    def _board_action_spec(self, kind: str) -> dict:
        return {
            "open": {
                "prepare": None,
                "action": self.controller.open_board,
                "finish": self._open_motion_control_finish,
                "busy_text": "运动控制连接中…",
                "success_message": tr("运动控制打开成功"),
            },
            "close": {
                "prepare": self._close_motion_control_prepare,
                "action": self.controller.close_board,
                "finish": self._close_motion_control_finish_after_executor,
                "busy_text": "运动控制关闭中…",
                "success_message": tr("运动控制已关闭"),
            },
            "reset": {
                "prepare": None,
                "action": self.controller.reset_board,
                "finish": self._reset_motion_control_finish,
                "busy_text": "运动控制复位中…",
                "success_message": tr("运动控制已复位"),
            },
        }[kind]

    def _close_motion_control_finish_after_executor(self) -> None:
        # On the executor path the blocking call is bare controller.close_board();
        # the GA_Close success status + audit live here (the failure branch is
        # handled by re-raising close_board's MotionControllerError, which the
        # executor delivers to on_error).
        _audit.info("motion board closed successfully")
        self._set_motion_status("运动控制已关闭", "off")

    def _run_board_action(self, kind: str) -> None:
        """Orchestrate a board open/close/reset through the board executor.

        Inline executor (default/tests): runs prepare + action + finish + status
        synchronously, identical to the old inline synchronous board-action
        path. Threaded executor (production): the prepare step runs on the GUI
        thread, the blocking DLL action runs on a worker thread (buttons disabled,
        watchdog armed), and the finish/status/error handling runs back on the GUI
        thread when the worker reports."""
        if self._closing or self._board_action_in_flight:
            return
        if kind in ("reset", "close"):
            self._axis9_home_generation += 1
        spec = self._board_action_spec(kind)
        prepare = spec["prepare"]
        if prepare is not None:
            try:
                prepare()
            except (MotionControllerError, ValueError) as exc:
                QMessageBox.warning(self, tr("操作失败"), str(exc))
                return

        # Give the operator feedback while the (possibly slow) DLL call runs:
        # disable the board-control buttons and show a busy status. This runs on
        # every executor; on a synchronous (inline) executor the busy state is set
        # then the finish step restores it within the same submit() call, so the
        # observable end state is unchanged. The watchdog is only meaningful for an
        # asynchronous executor (an inline action has already returned).
        self._board_action_generation += 1
        generation = self._board_action_generation
        self._set_board_controls_enabled(False)
        self._board_action_in_flight = True
        self._set_motion_status(spec["busy_text"], "reset")
        if self._board_executor_is_threaded():
            self._start_board_action_watchdog(kind)

        def on_done(_result, kind=kind, spec=spec, generation=generation):
            if generation != self._board_action_generation:
                return
            self._finish_board_action()
            try:
                spec["finish"]()
            except (MotionControllerError, ValueError) as exc:
                QMessageBox.warning(self, tr("操作失败"), str(exc))
                return
            self._after_board_action_success(spec)

        def on_error(exc, spec=spec, generation=generation):
            if generation != self._board_action_generation:
                return
            self._finish_board_action()
            QMessageBox.warning(self, tr("操作失败"), str(exc))

        self._board_action_executor.submit(spec["action"], on_done, on_error)

    def _after_board_action_success(self, spec: dict) -> None:
        if self.controller.is_open:
            try:
                positions, axis9_angle = self._record_positions_from_controller()
                self._update_axis_position_labels(positions, axis9_angle)
            except MotionControllerError as exc:
                self.statusBar().showMessage(tr("位置记录失败: {exc}").format(exc=exc))
                return
        if spec["success_message"]:
            self.statusBar().showMessage(spec["success_message"])

    def _board_executor_is_threaded(self) -> bool:
        # Inline executors complete synchronously inside submit(); only the
        # threaded path needs button-disable / busy-status / watchdog feedback.
        return not isinstance(self._board_action_executor, InlineBoardExecutor)

    def _set_board_controls_enabled(self, enabled: bool) -> None:
        enabled = enabled and not self._cutting_busy()
        for name in ("btnOpen", "btnClose", "button4"):
            widget = self.controls.get(name)
            if widget is not None:
                widget.setEnabled(enabled)

    def _start_board_action_watchdog(self, kind: str) -> None:
        watchdog = QTimer(self)
        watchdog.setSingleShot(True)
        watchdog.setInterval(self.BOARD_ACTION_TIMEOUT_MS)
        watchdog.timeout.connect(lambda kind=kind: self._on_board_action_timeout(kind))
        self._board_action_watchdog = watchdog
        watchdog.start()

    def _on_board_action_timeout(self, kind: str) -> None:
        if not self._board_action_in_flight:
            return
        # The worker thread is wedged in the DLL call; we cannot kill it, but we
        # surface the timeout and re-enable the buttons so the operator is not
        # stuck staring at a frozen-looking panel forever. A late completion still
        # runs its on_done/on_error harmlessly (in_flight is already cleared).
        self._board_action_generation += 1
        self._board_action_in_flight = False
        self._board_action_watchdog = None
        self._set_board_controls_enabled(True)
        message = self._set_motion_status("运动控制操作超时（{kind}），运动卡无响应", 'alarm', kind=kind)
        _audit.critical("board action TIMEOUT (kind=%s) after %d ms", kind, self.BOARD_ACTION_TIMEOUT_MS)
        QMessageBox.warning(self, tr("操作超时"), message)

    def _finish_board_action(self) -> None:
        if self._board_action_watchdog is not None:
            self._board_action_watchdog.stop()
            self._board_action_watchdog = None
        if self._board_action_in_flight:
            self._board_action_in_flight = False
            self._set_board_controls_enabled(True)

    def _stop_all_motion(self) -> None:
        self._abort_cutting()
        # "停止移动" means everything stops: cancel any discrete jog/relative moves
        # already queued (priority 10) BEFORE enqueuing the stop, so they cannot
        # run after the stop. cancel_pending never touches the not-yet-submitted
        # stop, and the stop goes in at priority 0 so it is dispatched ahead of
        # anything submitted concurrently.
        #
        # A2 (CP5): WHY does this path submit stop_all THROUGH the queue rather than
        # calling controller.stop_all() directly (as _gamepad_stop_all does)?
        #
        # The GUI / Space path is invoked from the Qt main thread.  The motion-queue
        # worker runs on a SEPARATE thread and holds _api_lock during every DLL call.
        # Calling controller.stop_all() directly from the main thread would race with
        # the worker: the _api_lock would serialise it (which is fine), but
        # _run_or_queue_motion_command checks controller.is_open and records callbacks
        # at submission time — logic that must stay on the main thread.  Submitting
        # at priority-0 guarantees the stop fires before any concurrently-submitted
        # lower-priority work WITHOUT requiring the main thread to block on the lock.
        #
        # The gamepad direct path (_gamepad_stop_all) is intentionally different:
        # gamepads issue stop_all from an already-serialised context (the gamepad
        # panel QTimer fires on the main thread after cancel_pending has already
        # drained the queue), and the extra priority-0 queue slot is not needed
        # because nothing is pending after cancel_pending.
        #
        # DO NOT "unify" these two paths: the queue-based path is correct for the
        # main thread; the direct path is correct for the gamepad context.
        self._axis9_home_generation += 1
        _audit.info("emergency stop all motion invoked (GUI button / Space key)")
        queue = self._motion_command_queue
        if queue is not None:
            queue.cancel_pending()
        self._run_or_queue_motion_command(
            self.controller.stop_all,
            success_message=tr("停止移动已执行"),
            record_positions=False,
            priority=0,
            on_success=lambda result: self._set_motion_status("停止移动已执行", "reset"),
        )

    def highlight_axis(self, axis: "int | None") -> None:
        from .axis_calibration import AXIS_LABELS
        for ax, label in AXIS_LABELS.items():
            btn = self.controls.get(f"axisHotspot{label}")
            selected = (axis is not None and int(ax) == int(axis))
            if btn is not None:
                if bool(btn.property("selectedAxis")) != selected:
                    btn.setProperty("selectedAxis", selected)
                    btn.style().unpolish(btn)
                    btn.style().polish(btn)
                # Non-color cue: prefix text with ▶ when selected
                short = tr({"MicroscopeZ": "镜Z"}.get(label, label))
                btn.setText(("▶ " + short) if selected else short)
            # Also mark the axis card so the operator can see it
            card_name = AXIS_CARD_NAMES.get(int(ax))
            if card_name is not None:
                card = self.controls.get(card_name)
                if card is not None and bool(card.property("selectedAxis")) != selected:
                    card.setProperty("selectedAxis", selected)
                    card.style().unpolish(card)
                    card.style().polish(card)

        # Prominent banner overlaid on the stage map showing the selected axis with
        # its live step size and current coordinate. Store the axis so the banner
        # can be re-rendered on every position tick (_update_axis_position_labels).
        self._gamepad_selected_axis = int(axis) if axis is not None else None
        self._update_gamepad_banner()

    def _update_gamepad_banner(self) -> None:
        stage_map = self.controls.get("stageMap")
        if stage_map is None or not hasattr(stage_map, "set_selected_axis"):
            return
        axis = self._gamepad_selected_axis
        if axis is None:
            stage_map.set_selected_axis(None)
            return
        label = AXIS_LABELS.get(axis, str(axis))
        hotspot = self.controls.get(f"axisHotspot{label}")
        detail = hotspot.toolTip() if hotspot is not None else label
        stage_map.set_selected_axis(
            tr("手柄选中: {detail} · 步长 {step} · 坐标 {coord}").format(
                detail=detail, step=self._axis_step_text(axis), coord=self._axis_coordinate_text(axis)
            )
        )

    def _axis_step_text(self, axis: int) -> str:
        ctrl = self.controls.get(self._step_control_name(axis))
        if ctrl is None:
            return "—"
        unit = "°" if axis == AXIS9_NUM else "µm"
        return f"{float(ctrl.value()):g} {unit}"

    def _axis_coordinate_text(self, axis: int) -> str:
        # MicroscopeZ (axis 3) has no calibrated coordinate — show its raw pulse.
        # It is not gamepad-selectable, so this branch is defensive only.
        if axis == 3:
            ctrl = self.controls.get("labelAxis3Pos")
            return f"{ctrl.text()} pulse" if ctrl is not None else "—"
        ctrl = self.controls.get(f"axisPosition{AXIS_LABELS.get(axis, str(axis))}")
        if ctrl is None:
            return "—"
        unit = "°" if axis == AXIS9_NUM else "mm"
        return f"{ctrl.text()} {unit}"

    def set_gamepad_armed(self, armed: bool) -> None:
        # Disable GUI jog widgets for EVERY gamepad-driven axis (incl. MicroscopeZ,
        # which is on the triggers, not the D-pad selector) while armed.
        for axis in GAMEPAD_AXES:
            for prefix in ("buttonfwd", "buttonback", "btnFwd", "btnBack", "btnStop"):
                widget = self.controls.get(f"{prefix}{axis}")
                if widget is not None:
                    widget.setEnabled(not armed)
        if armed:
            self._set_motion_status("手柄已启用，屏幕轴按钮已禁用", "on")
        else:
            self._set_motion_status("手柄已停用，屏幕轴按钮已恢复", "off")

    def _confirm(self, title: str, text: str) -> bool:
        """Show a Yes/No confirmation dialog. Monkeypatchable for tests."""
        result = QMessageBox.question(self, title, text)
        return result == QMessageBox.Yes

    def _set_motion_status(self, text: str, state: str = "off", **values) -> str:
        if "motionStatusText" in self.controls:
            text = self._retranslator.message(self.controls["motionStatusText"], text, **values)
        if "motionStatusLamp" not in self.controls:
            return text
        colors = {
            "on": ("#16a34a", "#bbf7d0"),
            "reset": ("#d97706", "#fed7aa"),
            "off": ("#9aa8b6", "#d7e1ec"),
            "alarm": ("#dc2626", "#fecaca"),
        }
        color, border = colors.get(state, colors["off"])
        self.controls["motionStatusLamp"].setStyleSheet(
            f"border-radius: 5px; background: {color}; border: 2px solid {border};"
        )
        return text

    def _open_position_calibration_dialog(self) -> None:
        self._close_existing_dialog("positionCalibrationDialog")

        dialog = self._register("positionCalibrationDialog", QDialog(self))
        dialog.setWindowTitle(tr("坐标标定"))
        dialog.setModal(False)
        dialog.setMinimumWidth(560)
        dialog.setStyleSheet(
            """
            QDialog {
                background: #f8fafc;
            }
            QLabel {
                color: #334154;
                font-weight: 700;
            }
            QLineEdit {
                background: #ffffff;
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                padding: 5px 7px;
                color: #12345a;
            }
            QPushButton {
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                background: #eef4fa;
                padding: 6px 12px;
                font-weight: 700;
                color: #142033;
            }
            QPushButton#calibrationApply {
                background: #0b8067;
                color: #ffffff;
                border-color: #0b8067;
            }
            """
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        form.addWidget(QLabel(tr("轴")), 0, 0)
        form.addWidget(QLabel(tr("当前读数")), 0, 1)
        form.addWidget(QLabel(tr("实际位置")), 0, 2)
        form.addWidget(QLabel(tr("单位")), 0, 3)
        first_actual = None
        for row, axis in enumerate(CALIBRATED_AXIS_ORDER, start=1):
            calibration = self.axis_calibrations[axis]
            axis_name = calibration.label
            unit = calibration.unit or ""
            current_value = self._format_position_text(self.controls[f"axisPosition{axis_name}"].text())
            current = self._register(f"calibrationCurrent{axis_name}", QLineEdit(current_value))
            current.setReadOnly(True)
            current.setFocusPolicy(Qt.NoFocus)
            actual = self._register(f"calibrationActual{axis_name}", QLineEdit())
            actual.setPlaceholderText(current_value)
            if first_actual is None:
                first_actual = actual
            form.addWidget(QLabel(axis_name), row, 0)
            form.addWidget(current, row, 1)
            form.addWidget(actual, row, 2)
            form.addWidget(QLabel(unit), row, 3)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = self._register("calibrationCancel", QPushButton(tr("取消")))
        apply = self._register("calibrationApply", QPushButton(tr("应用标定")))
        cancel.clicked.connect(dialog.close)
        apply.clicked.connect(lambda: self._apply_position_calibrations(dialog))
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

        dialog.open()
        if first_actual is not None:
            first_actual.setFocus()

    def _apply_position_calibrations(self, dialog: QDialog) -> None:
        pending = []
        for axis in CALIBRATED_AXIS_ORDER:
            calibration = self.axis_calibrations[axis]
            axis_name = calibration.label
            unit = calibration.unit or ""
            actual_widget = self.controls[f"calibrationActual{axis_name}"]
            if not actual_widget.text().strip():
                continue
            current_widget = self.controls[f"calibrationCurrent{axis_name}"]
            current_value = self._format_position_text(current_widget.text())
            try:
                actual_value = self._format_position_text(actual_widget.text())
            except ValueError as exc:
                QMessageBox.warning(self, tr("标定失败"), str(exc))
                return

            current_pulse = self._current_axis_pulse_for_calibration(axis)
            if current_pulse is None:
                QMessageBox.warning(self, tr("标定失败"), tr("{axis_name}: 请先打开运动控制或加载该轴的已记录位置").format(axis_name=axis_name))
                return

            actual_coordinate = float(actual_value)
            if calibration.min_coordinate is not None and actual_coordinate < calibration.min_coordinate:
                QMessageBox.warning(self, tr("标定失败"), tr("{axis_name} 实际位置不能小于 {min_coord:g} {unit}").format(axis_name=axis_name, min_coord=calibration.min_coordinate, unit=unit))
                return
            if calibration.max_coordinate is not None and actual_coordinate > calibration.max_coordinate:
                QMessageBox.warning(self, tr("标定失败"), tr("{axis_name} 实际位置不能大于 {max_coord:g} {unit}").format(axis_name=axis_name, max_coord=calibration.max_coordinate, unit=unit))
                return

            pending.append((axis, axis_name, unit, current_value, actual_value, current_pulse, actual_coordinate))

        if not pending:
            QMessageBox.warning(self, tr("标定失败"), tr("请至少输入一个实际位置"))
            return

        axes_str = "、".join(axis_name for _axis, axis_name, *_rest in pending)
        if not self._confirm(
            tr("确认标定覆盖"),
            tr("即将覆盖轴 {axes_str} 的坐标标定，这将重定义该轴的坐标参考系并写入磁盘，"
               "错误的值可能导致错向/超程。确定覆盖？").format(axes_str=axes_str),
        ):
            return

        for axis, axis_name, _unit, _current_value, actual_value, current_pulse, actual_coordinate in pending:
            self.runtime_calibration_store.calibrate_axis_at_pulse(axis, current_pulse, actual_coordinate)
        self._refresh_axis_calibration_mapping()
        for axis, axis_name, _unit, _current_value, actual_value, current_pulse, _actual_coordinate in pending:
            self.position_store.update_axis(axis, current_pulse, source="user_calibration")
            self.controls[f"axisPosition{axis_name}"].setText(actual_value)

        if len(pending) == 1:
            _axis, axis_name, unit, current_value, actual_value, _pulse, _coordinate = pending[0]
            message = self._set_motion_status("{axis_name} 标定：目前位置 {current_value} {unit}，实际位置 {actual_value} {unit}", 'reset', axis_name=axis_name, current_value=current_value, unit=unit, actual_value=actual_value)
        else:
            axes = "、".join(axis_name for _axis, axis_name, *_rest in pending)
            message = self._set_motion_status("坐标标定：已应用 {axes}", 'reset', axes=axes)
        self.statusBar().showMessage(message)
        dialog.close()

    def _current_axis_pulse_for_calibration(self, axis: int) -> float | None:
        if self.controller.is_open:
            return float(self.controller.get_axis_position(axis))

        cached = self.position_store.cached_pulse(axis)
        if cached is not None:
            return float(cached)

        return None

    def _format_position_text(self, text: str) -> str:
        try:
            value = float(text.strip())
        except ValueError as exc:
            raise ValueError(tr("位置数值无效: {text}").format(text=text)) from exc
        return f"{value:.3f}"

    def _open_axis3_positions_dialog(self) -> None:
        self._close_existing_dialog("axis3PositionsDialog")
        dialog = self._register("axis3PositionsDialog", QDialog(self))
        dialog.setWindowTitle(tr("显微镜点位管理"))
        dialog.setModal(False)
        dialog.setMinimumWidth(430)
        dialog.setStyleSheet(
            """
            QDialog {
                background: #f8fafc;
            }
            QLabel {
                color: #334154;
                font-weight: 700;
            }
            QLineEdit {
                background: #ffffff;
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                padding: 5px 7px;
                color: #12345a;
            }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #dbe4ee;
                border-radius: 6px;
                gridline-color: #e8eef6;
                color: #142033;
            }
            QPushButton {
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                background: #eef4fa;
                padding: 6px 10px;
                font-weight: 700;
                color: #142033;
            }
            QPushButton#btnAxis3MovePoint {
                background: #0b8067;
                color: #ffffff;
                border-color: #0b8067;
            }
            QPushButton#btnAxis3DeletePoint {
                background: #fff1f0;
                color: #b42318;
                border-color: #f4aaa4;
            }
            """
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        table = self._register("axis3PositionTable", QTableWidget(0, 2))
        table.setHorizontalHeaderLabels((tr("名称"), "pulse"))
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.itemSelectionChanged.connect(self._sync_axis3_selected_point_name)
        layout.addWidget(table)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel(tr("点位名称")))
        name_edit = self._register("axis3PointName", QLineEdit())
        name_edit.setPlaceholderText(tr("例如：安全高度 / 观察高度 / 接触前"))
        name_row.addWidget(name_edit, 1)
        layout.addLayout(name_row)

        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(8)
        save = self._register("btnAxis3SavePoint", QPushButton(tr("保存当前位置")))
        move = self._register("btnAxis3MovePoint", QPushButton(tr("移动到选中点位")))
        rename = self._register("btnAxis3RenamePoint", QPushButton(tr("重命名")))
        delete = self._register("btnAxis3DeletePoint", QPushButton(tr("删除")))
        close = self._register("btnAxis3ClosePositions", QPushButton(tr("关闭")))
        save.clicked.connect(self._save_current_axis3_point)
        move.clicked.connect(self._move_to_selected_axis3_point)
        rename.clicked.connect(self._rename_selected_axis3_point)
        delete.clicked.connect(self._delete_selected_axis3_point)
        close.clicked.connect(dialog.close)
        buttons.addWidget(save, 0, 0)
        buttons.addWidget(move, 0, 1)
        buttons.addWidget(rename, 1, 0)
        buttons.addWidget(delete, 1, 1)
        buttons.addWidget(close, 2, 0, 1, 2)
        layout.addLayout(buttons)

        self._refresh_axis3_position_table()
        dialog.open()

    def _refresh_axis3_position_table(self, selected_name: str | None = None) -> None:
        if "axis3PositionTable" not in self.controls:
            return
        table = self.controls["axis3PositionTable"]
        table.setRowCount(0)
        selected_row = -1
        for row, point in enumerate(self.microscope_position_store.points()):
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(point.name))
            table.setItem(row, 1, QTableWidgetItem(f"{point.pulse:.2f}"))
            if point.name == selected_name:
                selected_row = row
        if selected_row >= 0:
            table.setCurrentCell(selected_row, 0)

    def _sync_axis3_selected_point_name(self) -> None:
        point = self._selected_axis3_point()
        if point is not None and "axis3PointName" in self.controls:
            self.controls["axis3PointName"].setText(point.name)

    def _selected_axis3_point(self):
        if "axis3PositionTable" not in self.controls:
            return None
        table = self.controls["axis3PositionTable"]
        row = table.currentRow()
        if row < 0:
            return None
        name_item = table.item(row, 0)
        if name_item is None:
            return None
        return self.microscope_position_store.get_point(name_item.text())

    def _save_current_axis3_point(self) -> None:
        name = self.controls["axis3PointName"].text().strip()
        if not name:
            QMessageBox.warning(self, tr("保存失败"), tr("点位名称不能为空"))
            return

        def on_success(marked: object) -> None:
            marked_value = float(marked)
            try:
                self.microscope_position_store.save_point(name, marked_value)
            except ValueError as exc:
                QMessageBox.warning(self, tr("保存失败"), str(exc))
                return
            self._refresh_axis3_position_table(selected_name=name)
            self._set_motion_status("MicroscopeZ 已保存点位 {name}: {value:.2f} pulse", "reset", name=name, value=marked_value)

        self._run_or_queue_motion_command(
            self.controller.mark_axis3,
            record_positions=False,
            on_success=on_success,
        )

    def _move_to_selected_axis3_point(self) -> None:
        point = self._selected_axis3_point()
        if point is None:
            QMessageBox.warning(self, tr("运动失败"), tr("请先选择一个点位"))
            return

        def on_success(result: object) -> None:
            self.controls["labelAxis3Pos"].setText(f"{point.pulse:.2f}")
            self._set_motion_status("MicroscopeZ 移动到点位 {name}: {value:.2f} pulse", "on", name=point.name, value=point.pulse)

        self._run_or_queue_motion_command(
            lambda: self.controller.move_axis3_to_position(point.pulse),
            record_positions=False,
            on_success=on_success,
        )

    def _rename_selected_axis3_point(self) -> None:
        point = self._selected_axis3_point()
        if point is None:
            QMessageBox.warning(self, tr("重命名失败"), tr("请先选择一个点位"))
            return
        new_name = self.controls["axis3PointName"].text().strip()
        try:
            self.microscope_position_store.rename_point(point.name, new_name)
        except ValueError as exc:
            QMessageBox.warning(self, tr("重命名失败"), str(exc))
            return
        self._refresh_axis3_position_table(selected_name=new_name)
        self._set_motion_status("MicroscopeZ 点位已重命名为 {name}", "reset", name=new_name)

    def _delete_selected_axis3_point(self) -> None:
        point = self._selected_axis3_point()
        if point is None:
            QMessageBox.warning(self, tr("删除失败"), tr("请先选择一个点位"))
            return
        self.microscope_position_store.delete_point(point.name)
        self._refresh_axis3_position_table()
        self.controls["axis3PointName"].clear()
        self._set_motion_status("MicroscopeZ 已删除点位 {name}", "reset", name=point.name)

    def _open_axis9_twist_dialog(self) -> None:
        self._close_existing_dialog("axis9TwistDialog")
        dialog = self._register("axis9TwistDialog", QDialog(self))
        dialog.setWindowTitle("R twist")
        dialog.setModal(False)
        dialog.setMinimumWidth(280)
        dialog.setStyleSheet(
            """
            QDialog {
                background: #f8fafc;
            }
            QLabel {
                color: #334154;
                font-weight: 700;
            }
            QDoubleSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                padding: 5px 7px;
                color: #12345a;
            }
            QPushButton {
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                background: #eef4fa;
                padding: 6px 12px;
                font-weight: 700;
                color: #142033;
            }
            QPushButton#axis9TwistApply {
                background: #0b8067;
                color: #ffffff;
                border-color: #0b8067;
            }
            """
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        angle = self._register("axis9TwistAngle", QDoubleSpinBox())
        angle.setRange(0.0, 370.0)
        angle.setDecimals(3)
        angle.setSingleStep(1.0)
        angle.setValue(1.0)
        direction = self._register("axis9TwistDirection", QComboBox())
        direction.addItem(tr("顺时针"), 1)
        direction.addItem(tr("逆时针"), -1)
        form.addWidget(QLabel(tr("相对角度")), 0, 0)
        form.addWidget(angle, 0, 1)
        form.addWidget(QLabel("deg"), 0, 2)
        form.addWidget(QLabel(tr("方向")), 1, 0)
        form.addWidget(direction, 1, 1, 1, 2)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = self._register("axis9TwistCancel", QPushButton(tr("取消")))
        apply = self._register("axis9TwistApply", QPushButton(tr("执行")))
        cancel.clicked.connect(dialog.close)
        apply.clicked.connect(lambda: self._apply_axis9_twist(dialog, angle, direction))
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

        dialog.open()
        angle.setFocus()
        angle.selectAll()

    def _apply_axis9_twist(self, dialog: QDialog, angle_widget: QDoubleSpinBox, direction_widget: QComboBox) -> None:
        angle = float(angle_widget.value())
        sign = float(direction_widget.currentData())
        direction_text = lambda: tr("逆时针" if sign < 0 else "顺时针")

        def action() -> float:
            current_angle = self.controller.get_axis9_angle()
            target_angle = current_angle + sign * angle
            twist_speed = speed_to_pulse_per_ms(AXIS9_NUM, AXIS9_TWIST_SPEED_DEG_S)
            self.controller.move_axis9_to_angle(target_angle, twist_speed)
            return target_angle

        def on_success(result: object) -> None:
            target_angle = float(result)
            self.controls["axisPositionR"].setText(f"{target_angle:.3f}")
            message = self._set_motion_status("R twist：{direction} {angle:.3f} deg，目标 {target:.3f} deg", 'reset', direction=direction_text, angle=angle, target=target_angle)
            self.statusBar().showMessage(message)
            dialog.close()

        self._run_or_queue_motion_command(
            action,
            record_positions=False,
            on_success=on_success,
        )

    def _open_z_continuous_dialog(self) -> None:
        self._close_existing_dialog("zContinuousDialog")
        dialog = self._register("zContinuousDialog", QDialog(self))
        dialog.setWindowTitle(tr("Z 连续控制"))
        dialog.setModal(False)
        dialog.setMinimumWidth(300)
        dialog.setStyleSheet(
            """
            QDialog {
                background: #f8fafc;
            }
            QLabel {
                color: #334154;
                font-weight: 700;
            }
            QDoubleSpinBox, QComboBox {
                background: #ffffff;
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                padding: 5px 7px;
                color: #12345a;
            }
            QPushButton {
                border: 1px solid #cbd6e2;
                border-radius: 5px;
                background: #eef4fa;
                padding: 6px 12px;
                font-weight: 700;
                color: #142033;
            }
            QPushButton#zContinuousApply {
                background: #0b8067;
                color: #ffffff;
                border-color: #0b8067;
            }
            """
        )

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        form = QGridLayout()
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(8)
        speed = self._register("zContinuousSpeed", SpeedComboBox(speed_options_for_axis(4), 10.0))
        distance = self._register("zContinuousDistance", QDoubleSpinBox())
        distance.setRange(0.0, 10000.0)
        distance.setDecimals(3)
        distance.setSingleStep(10.0)
        distance.setValue(100.0)
        direction = self._register("zContinuousDirection", QComboBox())
        direction.addItem(tr("上升"), 1)
        direction.addItem(tr("下降"), -1)
        form.addWidget(QLabel(tr("速度")), 0, 0)
        form.addWidget(speed, 0, 1)
        form.addWidget(QLabel("um/s"), 0, 2)
        form.addWidget(QLabel(tr("距离")), 1, 0)
        form.addWidget(distance, 1, 1)
        form.addWidget(QLabel("um"), 1, 2)
        form.addWidget(QLabel(tr("方向")), 2, 0)
        form.addWidget(direction, 2, 1, 1, 2)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = self._register("zContinuousCancel", QPushButton(tr("取消")))
        apply = self._register("zContinuousApply", QPushButton(tr("执行")))
        cancel.clicked.connect(dialog.close)
        apply.clicked.connect(lambda: self._apply_z_continuous(dialog, speed, distance, direction))
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addLayout(buttons)

        dialog.open()
        distance.setFocus()
        distance.selectAll()

    def _apply_z_continuous(
        self,
        dialog: QDialog,
        speed_widget: SpeedComboBox,
        distance_widget: QDoubleSpinBox,
        direction_widget: QComboBox,
    ) -> None:
        speed_um_s = float(speed_widget.value())
        distance_um = float(distance_widget.value())
        direction = int(direction_widget.currentData())
        direction_text = lambda: tr("下降" if direction < 0 else "上升")
        speed = speed_to_pulse_per_ms(4, speed_um_s)
        step = step_to_pulse_delta(4, distance_um)

        def on_success(result: object) -> None:
            message = self._set_motion_status("Z 连续控制：{direction} {distance:.3f} um，速度 {speed:g} um/s", 'reset', direction=direction_text, distance=distance_um, speed=speed_um_s)
            self.statusBar().showMessage(message)
            dialog.close()

        self._run_or_queue_motion_command(
            lambda: self.controller.relative_move(4, direction, speed, step),
            record_positions=False,
            on_success=on_success,
        )

    def _make_spin(self, name: str, value: float, maximum: float = 5000.0) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, maximum)
        spin.setDecimals(2)
        spin.setSingleStep(1.0)
        spin.setButtonSymbols(QAbstractSpinBox.UpDownArrows)
        spin.setValue(value)
        spin.setMinimumWidth(80)
        spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return self._register(name, spin)

    def _make_speed_combo(self, name: str, axis: int) -> SpeedComboBox:
        combo = SpeedComboBox(speed_options_for_axis(axis), default_speed_for_axis(axis))
        return self._register(name, combo)

    def _make_step_control(self, name: str, axis: int, value: float):
        if int(axis) == 3:
            combo = SpeedComboBox(MICROSCOPE_Z_STEP_OPTIONS_UM, value)
            return self._register(name, combo)
        return self._make_spin(name, value)

    def _speed(self, axis: int) -> float:
        name = f"txtSpeed{axis}" if f"txtSpeed{axis}" in self.controls else f"txtSpeed{speed_axis_for(axis)}"
        return speed_to_pulse_per_ms(axis, float(self.controls[name].value()))

    def _gamepad_speed(self, axis: int) -> float:
        # MicroscopeZ (axis 3) uses a FIXED gamepad speed (4 mm/s = the stepper's
        # reliable max), independent of the GUI MicroscopeZ dropdown — the operator
        # wanted the LT/RT triggers to always top out at 4 mm/s no matter what the
        # on-screen setting shows. The proportional trigger still scales 0..this.
        # Every other axis follows its GUI speed control.
        if int(axis) == 3:
            return speed_to_pulse_per_ms(3, GAMEPAD_MICROSCOPE_Z_SPEED_UM_S)
        return self._speed(axis)

    def _step(self, axis: int) -> float:
        name = f"txtStep{axis}" if f"txtStep{axis}" in self.controls else f"txtStep{speed_axis_for(axis)}"
        return step_to_pulse_delta(axis, float(self.controls[name].value()))

    def _gamepad_stop_all(self) -> None:
        self._abort_cutting()
        # Direct emergency/release stop path for the gamepad: bypasses the motion
        # QUEUE so a release or Back press stops a merely-BUSY queue without waiting
        # behind pending work. NOTE: controller.stop_all() is still @_api_locked, so it
        # serialises on _api_lock and therefore CANNOT preempt a worker WEDGED inside a
        # blocking GA_* DLL call — that residual matches the A1 limit (see
        # _on_position_refresh_watchdog_timeout); a true preemptive halt would need an
        # unlocked stop entrypoint or a hardware e-stop line.
        # BACK/停止 means "everything stops": cancel any discrete jog/relative moves
        # already queued (priority 10, no epoch guard) BEFORE issuing the stop, so
        # they cannot execute on the board after the hardware has been halted.
        self._axis9_home_generation += 1
        _audit.info("gamepad emergency stop all motion invoked")
        queue = self._motion_command_queue
        if queue is not None:
            queue.cancel_pending()
        if not self.controller.is_open:
            return
        try:
            self.controller.stop_all()
        except MotionControllerError as exc:
            # This is a safety path; a failed stop must not be silently discarded.
            _audit.critical("gamepad emergency stop FAILED: %s", exc)
            self.statusBar().showMessage(tr("手柄急停失败: {exc}").format(exc=exc))
            self._set_motion_status("手柄急停失败: {exc}", "reset", exc=exc)

    def _step_control_name(self, axis: int) -> str:
        name = f"txtStep{axis}"
        return name if name in self.controls else f"txtStep{speed_axis_for(axis)}"

    def _speed_control_name(self, axis: int) -> str:
        name = f"txtSpeed{axis}"
        return name if name in self.controls else f"txtSpeed{speed_axis_for(axis)}"

    def _gamepad_hud_detail(self, axis: int) -> str:
        # Operator-facing HUD detail for the gamepad panel: the GUI step and speed
        # in their display units (R is angular, every other gamepad axis is linear).
        step_unit, speed_unit = ("°", "°/s") if int(axis) == 9 else ("µm", "µm/s")
        step_v = float(self.controls[self._step_control_name(axis)].value())
        speed_v = float(self.controls[self._speed_control_name(axis)].value())
        return tr("步长:{step_v:g} {step_unit} · 速度:{speed_v:g} {speed_unit}").format(
            step_v=step_v, step_unit=step_unit, speed_v=speed_v, speed_unit=speed_unit
        )

    def _gamepad_set_step(self, axis: int, delta: int) -> None:
        from .axis_calibration import snap_step
        ctrl = self.controls[self._step_control_name(axis)]
        ctrl.setValue(snap_step(axis, float(ctrl.value()), delta))

    def _controller_direction_for_coordinate_direction(self, axis: int, direction: int) -> int:
        coordinate_direction = 1 if direction > 0 else -1
        if int(axis) == 3:
            return -coordinate_direction
        calibration = self.axis_calibrations.get(int(axis))
        if calibration is not None and calibration.pulse_sign < 0:
            return -coordinate_direction
        return coordinate_direction

    def _move_axis_to_target(self, axis: int, axis_name: str, unit: str) -> None:
        target_widget = self.controls[f"axisTarget{axis_name}"]
        try:
            target_text = self._format_position_text(target_widget.text())
        except ValueError as exc:
            QMessageBox.warning(self, tr("目标位置无效"), str(exc))
            return

        target_coordinate = float(target_text)
        action = lambda: self.controller.move_axis_to_coordinate(axis, target_coordinate, self._speed(axis))
        queue = self._active_motion_queue()
        if queue is None or not queue.submit(action, priority=10):
            self.statusBar().showMessage(tr("运动控制未打开，移动命令已忽略"))
            self._set_motion_status("运动控制未打开，命令已忽略", "off")
            return

        target_widget.setText(target_text)
        message = self._set_motion_status("{axis_name} 移动至 {target_text} {unit}", 'reset', axis_name=axis_name, target_text=target_text, unit=unit)
        self.statusBar().showMessage(message)

    def _relative_move(self, axis: int, direction: int) -> None:
        controller_direction = self._controller_direction_for_coordinate_direction(axis, direction)
        action = lambda: self.controller.relative_move(axis, controller_direction, self._speed(axis), self._step(axis))
        queue = self._active_motion_queue()
        if queue is None or not queue.submit(action, priority=10):
            self.statusBar().showMessage(tr("运动控制未打开，移动命令已忽略"))
            self._set_motion_status("运动控制未打开，命令已忽略", "off")

    def _start_jog(self, axis: int, sign: int) -> None:
        queue = self._active_motion_queue()
        if queue is None:
            self.statusBar().showMessage(tr("运动控制未打开，移动命令已忽略"))
            self._set_motion_status("运动控制未打开，命令已忽略", "off")
            return
        speed = self._controller_direction_for_coordinate_direction(axis, sign) * self._speed(axis)
        queue.submit(
            lambda axis=axis, speed=speed: self.controller.start_jog(axis, speed),
            priority=0,
            kind=MotionCommandQueue.CONTINUOUS_JOG_KIND,
            metadata=axis,
        )

    def _stop_axis(self, axis: int) -> None:
        if axis == AXIS9_NUM:
            self._axis9_home_generation += 1
        self._run_or_queue_motion_command(
            lambda: self.controller.stop_axis(axis),
            record_positions=False,
            priority=0,
        )

    def _stop_continuous_jog(self, axis: int) -> None:
        queue = self._active_motion_queue()
        if queue is None:
            return
        queue.submit(lambda axis=axis: self.controller.stop_axis(axis), priority=0)

    def _active_motion_queue(self) -> MotionCommandQueue | None:
        # The motion queue exists only while the board is open. When the board is
        # closed (or has never been opened) we must NOT lazily create one: that
        # would resurrect a worker after close and spawn a leaked thread, breaking
        # the "closing motion control closes the queue" invariant. Motion handlers
        # treat a None return as "ignore the command".
        if not self.controller.is_open:
            return None
        if self._motion_command_queue is None:
            queue = self._motion_command_queue_factory(self)
            queue.completed.connect(self._handle_motion_command_completed)
            queue.failed.connect(self._show_motion_command_failure)
            queue.position_ready.connect(self._handle_async_position_refresh)
            queue.position_failed.connect(self._handle_async_position_refresh_failure)
            queue.on_cancel_pending = self._on_motion_queue_pending_cancelled
            self._motion_command_queue = queue
        return self._motion_command_queue

    def _on_motion_queue_pending_cancelled(self) -> None:
        # An emergency stop drained the queue: drop every pending per-command
        # callback so a never-arriving completion can't fire stale GUI logic, and
        # clear the in-flight position-refresh flag so a fresh refresh can be
        # requested once motion resumes. The currently-executing command (if any)
        # is already off the queue; its callback firing is harmless.
        #
        # A3 (CP5): also DISARM the position-refresh watchdog so that "pending
        # cleared ⇒ watchdog disarmed" holds as an invariant without depending on
        # the 100 ms refresh timer's continued liveness.  The watchdog would be
        # re-armed automatically the next time _request_async_position_refresh runs
        # (i.e. on the next 100 ms tick after motion resumes).
        self._motion_command_callbacks.clear()
        self._position_refresh_pending = False
        self._disarm_position_refresh_watchdog()

    def _run_or_queue_motion_command(
        self,
        action: Callable[[], object],
        *,
        success_message: str | None = None,
        record_positions: bool = True,
        priority: int = 10,
        on_success: Callable[[object], None] | None = None,
    ) -> bool:
        if self._closing:
            return False
        if self._cutting_busy() and priority > 0:
            return False
        queue = self._active_motion_queue()
        if queue is None:
            self.statusBar().showMessage(tr("运动控制未打开，命令已忽略"))
            self._set_motion_status("运动控制未打开，命令已忽略", "off")
            return False
        command_id = next(self._motion_command_callback_ids)
        self._motion_command_callbacks[command_id] = (success_message, on_success, record_positions)
        if not queue.submit(action, priority=priority, metadata=command_id):
            self._motion_command_callbacks.pop(command_id, None)
            return False
        return True

    def _handle_motion_command_completed(self, payload: object) -> None:
        try:
            kind, command_id, result = payload
        except (TypeError, ValueError):
            return
        # Continuous jogs complete on the worker but never register a callback;
        # returning here ensures their axis-number metadata can never accidentally
        # match a real discrete command_id that happens to share the same integer.
        if kind == MotionCommandQueue.CONTINUOUS_JOG_KIND:
            return
        try:
            command_id = int(command_id)
        except (TypeError, ValueError):
            return
        callback = self._motion_command_callbacks.pop(command_id, None)
        if callback is None:
            return
        success_message, on_success, record_positions = callback
        try:
            if on_success is not None:
                on_success(result)
        except (MotionControllerError, ValueError) as exc:
            QMessageBox.warning(self, tr("操作失败"), str(exc))
            return
        if self.controller.is_open and record_positions:
            self._request_async_position_refresh()
        if success_message:
            self.statusBar().showMessage(success_message)

    def _show_motion_command_failure(self, payload: object) -> None:
        try:
            kind, command_id, message = payload
        except (TypeError, ValueError):
            kind = ""
            command_id = None
            message = str(payload)
        # Continuous jogs never register a callback; early-return so their
        # axis-number metadata can't silently drop a discrete callback whose
        # command_id happens to equal that axis number.
        if kind == MotionCommandQueue.CONTINUOUS_JOG_KIND:
            _audit.critical("motion command FAILED (continuous jog kind=%s): %s", kind, message)
            self.statusBar().showMessage(tr("运动命令失败: {message}").format(message=message))
            self._set_motion_status("运动命令失败: {message}", "reset", message=message)
            return
        if command_id is not None:
            try:
                self._motion_command_callbacks.pop(int(command_id), None)
            except (TypeError, ValueError):
                pass
        _audit.critical("motion command FAILED (kind=%s, id=%s): %s", kind, command_id, message)
        self.statusBar().showMessage(tr("运动命令失败: {message}").format(message=message))
        # Hardware motion faults are at least as important as input-validation
        # errors, so surface them on the prominent motion-status bar too (amber),
        # not only the easily-missed bottom status bar.
        self._set_motion_status("运动命令失败: {message}", "reset", message=message)

    def _zero_axis3(self) -> None:
        self._run_or_queue_motion_command(
            self.controller.zero_axis3,
            success_message=tr("轴3位置已清零"),
        )

    def _home_axis9(self) -> None:
        if not self._confirm(
            tr("确认 R 轴物理回零"),
            tr("R 轴将执行物理回零（归零行程），请确认样品和机械处于安全位置。\n\n确定执行？"),
        ):
            return
        new_job = getattr(self.controller, "new_axis9_home_job", None)
        if new_job is not None:
            job = new_job()
            generation = self._axis9_home_generation

            def guarded_poll():
                if not self._closing and generation == self._axis9_home_generation:
                    return job.poll()
                return None

            def poll():
                if not self._closing and generation == self._axis9_home_generation:
                    self._run_or_queue_motion_command(guarded_poll, on_success=advance, record_positions=False,
                                                      priority=2)

            def advance(pulse):
                if generation != self._axis9_home_generation or self._closing:
                    return
                if pulse is None:
                    QTimer.singleShot(20, self, poll)
                else:
                    self._record_axis9_home_zero(pulse)

            poll()
            return
        self._run_or_queue_motion_command(
            self.controller.home_axis9,
            on_success=self._record_axis9_home_zero,
        )

    def _record_axis9_home_zero(self, pulse: float | None = None) -> None:
        current_pulse = float(self.controller.get_axis_position(AXIS9_NUM) if pulse is None else pulse)
        self.runtime_calibration_store.calibrate_axis_at_pulse(AXIS9_NUM, current_pulse, 0.0)
        self._refresh_axis_calibration_mapping()
        self.position_store.update_axis(
            AXIS9_NUM,
            current_pulse,
            source="axis9_home",
            angle_degrees=0.0,
        )
        self.controls["axisPositionR"].setText("0.000")
        message = self._set_motion_status("R 轴物理回零完成：当前位置已设为 0.000 deg", 'reset')
        self.statusBar().showMessage(message)

    def _load_cached_position_labels(self) -> None:
        cached_positions: dict[int, float] = {}
        for axis, record in self.position_store.axes.items():
            try:
                cached_positions[int(axis)] = float(record["pulse"])
            except (KeyError, TypeError, ValueError):
                continue
        self._update_axis_position_labels(cached_positions, self.position_store.cached_axis9_angle())

    def _read_positions_from_controller(self) -> tuple[dict[int, float], float]:
        positions = {axis: self.controller.get_axis_position(axis) for axis in ALL_AXES}
        axis9_angle = self.controller.get_axis9_angle()
        return positions, axis9_angle

    def _record_positions_from_controller(self) -> tuple[dict[int, float], float]:
        positions, axis9_angle = self._read_positions_from_controller()
        self.position_store.update_axes(
            positions,
            source="controller_readback",
            axis9_angle_degrees=axis9_angle,
        )
        return positions, axis9_angle

    def _update_axis_position_labels(self, positions: dict[int, float], axis9_angle: float | None = None) -> None:
        for axis, pulse in positions.items():
            axis = int(axis)
            if axis == 3:
                if "labelAxis3Pos" in self.controls:
                    self.controls["labelAxis3Pos"].setText(f"{float(pulse):.2f}")
                continue

            coordinate = (
                float(axis9_angle)
                if axis == AXIS9_NUM and axis9_angle is not None
                else coordinate_from_pulse(axis, pulse, self.axis_calibrations)
            )
            if coordinate is None:
                continue

            control_name = f"axisPosition{AXIS_LABELS.get(axis, f'Axis{axis}')}"
            if control_name in self.controls:
                self.controls[control_name].setText(f"{coordinate:.3f}")

        # Re-render the gamepad selected-axis banner so its coordinate stays live.
        if self._gamepad_selected_axis is not None:
            self._update_gamepad_banner()

    def refresh_positions(self) -> None:
        # Explicit closing/closed guard: even though _request_async_position_refresh
        # already bails when the board is closed (the queue is None), this stops a
        # timer tick that lands mid-teardown from doing any work at all.
        if self._closing or not self.controller.is_open:
            return
        self._request_async_position_refresh()

    def _request_async_position_refresh(self) -> None:
        if self._position_refresh_pending:
            return
        queue = self._active_motion_queue()
        if queue is None:
            return
        self._position_refresh_pending = True
        # A2: arm the watchdog BEFORE submitting so an inline queue (which runs the
        # read synchronously inside submit() and disarms via the completion handler)
        # leaves the watchdog disarmed, while a threaded queue keeps it armed until
        # the worker eventually reports. Arming first means a synchronous failure
        # that never sets pending=False is still covered.
        self._arm_position_refresh_watchdog()
        generation = self._position_refresh_generation
        # Priority 1: just below an e-stop (0) but ABOVE every jog/move (continuous=5,
        # discrete=10). The reactive safety scan rides on this read, so it must NOT be
        # starved behind continuous jogs — otherwise an interference/soft-limit is
        # detected late (axis over-travels) AND the alarm clears late after the operator
        # backs off. Running it each ~100 ms cycle even mid-motion keeps the scan live.
        submitted = queue.submit(
            lambda generation=generation: (generation, *self._read_positions_from_controller()),
            priority=1,
            kind="position_refresh",
            metadata=generation,
        )
        if not submitted:
            self._position_refresh_pending = False
            self._disarm_position_refresh_watchdog()

    def _arm_position_refresh_watchdog(self) -> None:
        """Start/restart the single-shot watchdog for the in-flight read.

        Deterministic-test note: the watchdog merely calls
        _on_position_refresh_watchdog_timeout when it fires. Tests drive that
        handler directly (and check _position_refresh_watchdog_active for
        arm/disarm) so no real 2 s wait is ever needed.
        """
        watchdog = self._position_refresh_watchdog
        if watchdog is None:
            watchdog = QTimer(self)
            watchdog.setSingleShot(True)
            watchdog.timeout.connect(self._on_position_refresh_watchdog_timeout)
            self._position_refresh_watchdog = watchdog
        watchdog.start(self.POSITION_REFRESH_WATCHDOG_MS)

    def _disarm_position_refresh_watchdog(self) -> None:
        watchdog = self._position_refresh_watchdog
        if watchdog is not None:
            watchdog.stop()

    def _position_refresh_watchdog_active(self) -> bool:
        watchdog = self._position_refresh_watchdog
        return watchdog is not None and watchdog.isActive()

    def _on_position_refresh_watchdog_timeout(self) -> None:
        """A2: the in-flight position read neither returned nor raised within the
        deadline — the DLL read is hung. Recover the reactive scan and ALARM.

        A1 residual (honest limitation): the single motion-queue worker plus the
        shared _api_lock mean a wedged blocking DLL read cannot be PREEMPTED in
        software — the protective stop_all submitted below may itself queue behind
        the wedged worker and not run until (if) the read unwedges. So this
        watchdog DETECTS + LOUDLY ALARMS + best-effort recovers; the ultimate
        backstop for a truly stuck controller is the operator + the hardware
        limits (for axes that have them) + the PREVENTIVE bounded-target clamp
        applied at jog-start (independent of this reactive scan). What it
        guarantees is: NO silent failure.
        """
        # Recovery: bump the generation so the eventually-returning stale read is
        # discarded by the existing generation guard, and clear the latch so the
        # NEXT 100 ms tick can attempt a fresh read.
        self._position_refresh_generation += 1
        self._position_refresh_pending = False
        self._disarm_position_refresh_watchdog()

        _audit.critical(
            "position read UNRESPONSIVE after %d ms — reactive motion-safety scan "
            "paused; attempting protective stop",
            self.POSITION_REFRESH_WATCHDOG_MS,
        )
        message = self._set_motion_status("位置读取无响应，运动安全扫描已暂停；请确认物理急停/硬件限位可用", 'alarm')
        self.statusBar().showMessage(message)
        # Best-effort protective stop. NOTE (A1): this may itself queue behind the
        # wedged worker; it is best-effort, not a guaranteed preemptive halt.
        self._attempt_protective_stop_all()

    def _attempt_protective_stop_all(self) -> None:
        """Best-effort priority-0 stop_all through the motion queue.

        Used by the A2 watchdog and the A3 consecutive-failure fail-safe. Submitted
        at priority 0 so it is dispatched ahead of anything queued concurrently; it
        cannot, however, preempt a worker already wedged in a blocking DLL call
        (see _on_position_refresh_watchdog_timeout for the residual limitation)."""
        self._abort_cutting()
        self._axis9_home_generation += 1
        queue = self._active_motion_queue()
        if queue is not None:
            queue.submit(self.controller.stop_all, priority=0)

    def _handle_async_position_refresh(self, result: object) -> None:
        try:
            generation, positions, axis9_angle = result
            if generation != self._position_refresh_generation or not self.controller.is_open:
                return
            self._position_refresh_pending = False
            self._disarm_position_refresh_watchdog()
            # A3: a successful read clears the consecutive-failure streak.
            self._consecutive_read_failures = 0
            self.position_store.update_axes(
                positions,
                source="controller_readback",
                axis9_angle_degrees=axis9_angle,
            )
            self._stop_unsafe_axes_if_needed(positions)
            self._update_axis_position_labels(positions, axis9_angle)
        except (MotionControllerError, TypeError, ValueError) as exc:
            self._position_refresh_pending = False
            self._disarm_position_refresh_watchdog()
            self.statusBar().showMessage(tr("位置记录失败: {exc}").format(exc=exc))

    def _handle_async_position_refresh_failure(self, payload: object) -> None:
        try:
            generation, message = payload
        except (TypeError, ValueError):
            generation = self._position_refresh_generation
            message = str(payload)
        if generation != self._position_refresh_generation or not self.controller.is_open:
            return
        self._position_refresh_pending = False
        self._disarm_position_refresh_watchdog()
        self.statusBar().showMessage(message)

        # A3: a failed read means the X2/X3 interference boundary was NOT checked
        # this tick. A single transient blip is tolerated; a sustained run of
        # failures while the board is open is a real loss of the reactive scan, so
        # protectively stop and raise a persistent alarm.
        self._consecutive_read_failures += 1
        if self._consecutive_read_failures < self.CONSECUTIVE_READ_FAILURE_LIMIT:
            return
        _audit.critical(
            "position read FAILED %d times consecutively — protective stop; "
            "last error: %s",
            self._consecutive_read_failures,
            message,
        )
        alarm_text = self._set_motion_status("位置读取连续失败 {n} 次，已保护性停止；请确认物理急停/硬件限位可用", 'alarm', n=self._consecutive_read_failures)
        # Mark the alarm latched so that once reads recover, the next all-safe scan in
        # _stop_unsafe_axes_if_needed clears the lamp. Without this the read-failure
        # alarm lamp would stay red after recovery (the clear path is gated on this flag).
        self._safety_alarm_active = True
        self.statusBar().showMessage(alarm_text)
        self._attempt_protective_stop_all()

    def _stop_unsafe_axes_if_needed(self, positions: dict[int, float]) -> None:
        # CP1/A1 — residual limitation of the REACTIVE motion-safety scan (read in
        # full; this is safety-critical):
        #
        # This interference / soft-range auto-stop runs only when a fresh position
        # read arrives via the 100 ms refresh tick. Both the per-tick read AND the
        # stop_axis it issues go through the SINGLE motion-queue worker behind the
        # shared _api_lock. Consequently, a blocking DLL read that WEDGES the worker
        # cannot be preempted in software: neither a fresh read nor a queued stop
        # can run until the worker unwedges. A fully-preemptive independent backstop
        # is not achievable without DLL-level timeout support.
        #
        # The realistic, layered mitigation therefore is:
        #   • PREVENTIVE (independent of this reactive scan): the bounded-target
        #     clamp applied at jog-start — it limits the commanded target up front,
        #     so it holds even if this reactive scan is degraded.
        #   • DETECT + ALARM (A2): the position-refresh watchdog
        #     (_on_position_refresh_watchdog_timeout) catches a hung read, recovers
        #     the latch, and raises a loud persistent alarm — NO silent failure.
        #   • FAIL-SAFE (A3): sustained read failures trip a protective stop +
        #     alarm in _handle_async_position_refresh_failure.
        #   • ULTIMATE BACKSTOP: the operator + physical e-stop + hardware limits
        #     (for axes that have them).
        check = unsafe_axes_for_positions(positions, self.axis_calibrations)

        # Update safety-alarm visual state on all axis cards: set on unsafe axes,
        # clear on axes that are now back in range.
        all_alarm_axes = set(check.axes) if not check.ok else set()
        for axis, card_name in AXIS_CARD_NAMES.items():
            card = self.controls.get(card_name)
            if card is None:
                continue
            alarm = axis in all_alarm_axes
            if bool(card.property("safetyAlarm")) != alarm:
                card.setProperty("safetyAlarm", alarm)
                card.style().unpolish(card)
                card.style().polish(card)

        if check.ok:
            # Clear lamp alarm state if it was previously in alarm. We detect this
            # by checking whether any card still has safetyAlarm (they were just
            # cleared above), which means we just transitioned from alarm to safe.
            if getattr(self, "_safety_alarm_active", False):
                self._safety_alarm_active = False
                self._set_motion_status("安全检查：所有轴在安全范围内", "on")
            return

        self._safety_alarm_active = True

        stopped_axes = []
        for axis in check.axes:
            # Always stop via the queue so the stop is serialised behind any
            # already-dispatching command and the worker thread is never raced.
            queue = self._active_motion_queue()
            if queue is not None:
                queue.submit(lambda axis=axis: self.controller.stop_axis(axis), priority=0)
            stopped_axes.append(str(axis))

        _audit.warning(
            "safety auto-stop: axes [%s] stopped — %s",
            ", ".join(stopped_axes),
            check.message,
        )
        self._set_motion_status("安全停止轴 {axes}: {message}", "alarm", axes=", ".join(stopped_axes), message=check.message)
        self.statusBar().showMessage(tr("安全停止轴 {axes}: {message}").format(axes=", ".join(stopped_axes), message=check.message))

    def changeEvent(self, event) -> None:
        from PySide6.QtCore import QEvent
        if event.type() in (QEvent.Type.ActivationChange, QEvent.Type.WindowStateChange):
            panel = getattr(self, "gamepad_panel", None)
            if panel is not None and not self.isActiveWindow():
                panel.disarm()
        super().changeEvent(event)

    def _on_application_state_changed(self, state) -> None:
        # Safety: disarm the gamepad the moment the whole app stops being the active
        # foreground application (alt-tab away, minimise to tray, etc.).
        if state != Qt.ApplicationState.ApplicationActive:
            panel = getattr(self, "gamepad_panel", None)
            if panel is not None:
                panel.disarm()

    def _prepare_for_close(self) -> None:
        # Stop the position-refresh timer first and mark the window closing so a
        # queued 100 ms timeout firing during the teardown below (which spins the
        # event loop via processEvents) cannot enqueue a position_refresh that
        # races queue shutdown.
        if self._closing:
            return
        self._closing = True
        self._abort_cutting()
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        # CP1/A2: stop the position-refresh watchdog so it can never fire into a
        # destroyed window during/after teardown.
        watchdog = getattr(self, "_position_refresh_watchdog", None)
        if watchdog is not None:
            watchdog.stop()
        panel = getattr(self, "gamepad_panel", None)
        if panel is not None:
            panel._timer.stop()
        temperature_panel = getattr(self, "temperature_panel", None)
        if temperature_panel is not None:
            # Start verified heater-off before waiting for motion or optical devices.
            if hasattr(temperature_panel, "prepare_for_close"):
                temperature_panel.prepare_for_close(timeout_ms=0)
            try:
                temperature_panel.disconnect_from_device()
            except Exception as exc:
                _audit.exception("RD105 disconnect could not start during exit")
                self.statusBar().showMessage(str(exc))
        if self._board_action_watchdog is not None:
            self._board_action_watchdog.stop()
            self._board_action_watchdog = None
        self._board_action_generation += 1
        self._board_action_in_flight = False
        board_executor = self._board_action_executor
        motion_queue = self._motion_command_queue
        if motion_queue is not None:
            self._position_refresh_generation += 1
            self._position_refresh_pending = False
            motion_queue.shutdown(timeout=0)
            self._motion_command_callbacks.clear()
        controller = self.controller

        def finish_motion():
            if motion_queue is not None:
                motion_queue.shutdown(timeout=None)
            # A late board-open must finish before we decide whether to close it.
            while board_executor.is_busy():
                board_executor.wait_for_idle()
            if controller.is_open:
                for action in (controller.stop_all, controller.close_board):
                    try:
                        action()
                    except Exception:
                        _audit.exception("Motion shutdown failed in %r", action)

        motion_cleanup = threading.Thread(target=finish_motion, name="ATSMotionClose", daemon=True)
        motion_cleanup.start()
        motion_cleanup.join(self.MOTION_CLOSE_TIMEOUT_MS / 1000)
        if motion_cleanup.is_alive():
            _audit.critical("Motion stop/close unconfirmed at shutdown deadline")
        if temperature_panel is not None and hasattr(temperature_panel, "prepare_for_close"):
            idle = temperature_panel.prepare_for_close(timeout_ms=self.HEATER_OFF_CLOSE_TIMEOUT_MS)
            heater = getattr(temperature_panel, "controller", None)
            if idle is False or getattr(heater, "disconnect_disable_failed", False):
                _audit.critical("RD105 heater-off unconfirmed during exit: %s", getattr(heater, "last_error", ""))
        objective_panel = getattr(self, "objective_panel", None)
        objective_idle = True
        if objective_panel is not None:
            # prepare_for_close marks the panel closing (so a late worker signal is
            # a no-op) and detaches any worker still running after the bounded wait
            # so Qt does not destroy a running QThread when the panel is torn down.
            if hasattr(objective_panel, "prepare_for_close"):
                objective_idle = objective_panel.prepare_for_close()
            else:  # pragma: no cover - fallback for older panel API
                objective_idle = objective_panel.wait_for_idle()
        current_nosepiece = getattr(getattr(self, "objective_panel", None), "nosepiece_controller", self.nosepiece_controller)
        # Only close the serial port if no objective worker is still mid-transfer.
        # Closing it under an active worker can corrupt the transfer / abort the
        # thread; if a worker is still running after the wait, leave the port to
        # the OS rather than yanking it.
        if objective_idle and getattr(current_nosepiece, "is_open", False):
            try:
                current_nosepiece.disconnect()
            except NosepieceError as exc:
                self.statusBar().showMessage(str(exc))
        camera_window = getattr(self, "camera_window", None)
        if self.cutting_window is not None:
            if not self.cutting_window.prepare_for_close(timeout_ms=1000):
                _audit.critical("Cutting stop unconfirmed during shutdown")
            self.cutting_window.hide()
        if camera_window is not None and hasattr(camera_window, "close_after_prepare"):
            if not camera_window.close_after_prepare(timeout_ms=2000):
                _audit.warning("Camera close unconfirmed at shutdown deadline")

    def _request_exit(self) -> None:
        """The only sanctioned way to quit: confirm, then close (which runs the
        heater-off + close-all-ports shutdown in closeEvent)."""
        reply = QMessageBox.question(
            self,
            tr("确认退出"),
            tr("确认退出程序？\n退出后将关闭加热（ENABLE=0）并断开所有端口。"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._exit_requested = True
            self.close()

    def _toggle_language(self) -> None:
        self._translator.set_language("zh" if self._translator.lang == "en" else "en")

    def closeEvent(self, event) -> None:
        # Refuse the OS window-manager X (a spontaneous close) unless the user went
        # through the Exit button. This keeps a stray click on the title-bar X from
        # quitting the app; a programmatic close() (Exit, smoke-exit, tests) is not
        # spontaneous and proceeds normally, so the shutdown path is unchanged for it.
        if event.spontaneous() and not self._exit_requested:
            event.ignore()
            self.statusBar().showMessage(tr("窗口关闭按钮已禁用：请点击右下角「退出 Exit」按钮退出程序"))
            return
        self._prepare_for_close()
        if self.cutting_window is not None and not self.cutting_window.prepare_for_close(timeout_ms=0):
            event.ignore()
            self.centralWidget().setEnabled(False)
            self.statusBar().showMessage(tr("等待切割任务停止；设备状态尚未确认"))
            if not hasattr(self, "_cutting_exit_poll"):
                self._cutting_exit_poll = QTimer(self)
                self._cutting_exit_poll.setInterval(100)
                self._cutting_exit_poll.timeout.connect(self._finish_cutting_exit)
                self._cutting_exit_poll.start()
            return
        super().closeEvent(event)

    def _finish_cutting_exit(self) -> None:
        if self.cutting_window.prepare_for_close(timeout_ms=0):
            self._cutting_exit_poll.stop()
            self.close()
