from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QScrollArea,
    QPushButton, QVBoxLayout,
)

from .gamepad_controller import (
    GamepadController, StartContinuous, UpdateContinuous, StopContinuous,
    Jog, SelectAxis, StepDelta, StopAll, ToggleEnable, SELECTOR_AXES, GAMEPAD_AXES,
)
from .gamepad_help import render_help_pixmap
from .gamepad_xinput import load_default_reader

_audit = logging.getLogger("ats.audit")


# Full F710 (XInput) mapping, shown in-UI via the "?" help button so the operator
# never has to read source to learn that BACK is the emergency stop or that the
# triggers drive the microscope Z.
GAMEPAD_MAPPING_TEXT = (
    "START：启用 / 停用手柄\n"
    "BACK：急停（所有轴立即停止）\n\n"
    "左摇杆：X3（左右）/ Y3（前后）连续移动\n"
    "右摇杆：X2（左右）/ Y2（前后）连续移动\n"
    "（摇杆为比例速度，推得越多越快）\n\n"
    "方向键 ↑ / ↓：「当前选中轴」按步长单步 + / −（仅单步，不连续）\n"
    "方向键 ← / →：步长档 减小 / 增大\n\n"
    "四个面键 选轴（按设备图布局的上下左右方向）：\n"
    "  Y=上  A=下  X=左  B=右\n\n"
    "扳机 LT / RT：显微镜 Z 下 / 上（轻按单步，长按连续；固定上限 4 mm/s 比例，与界面速度无关）\n"
    "肩键 LB / RB：载玻片 Z 下 / 上（轻按单步，长按连续）\n\n"
    "可选轴（面键 2D 选择）：X3 / Y3 / X2 / Y2 / R / 载玻片Z / X1 / Y1\n"
    "（显微镜Z 由扳机 LT/RT 控制，不在可选轴内）\n"
    "当前选中轴会在左侧设备图横幅与高亮上显示（含步长与当前坐标）。"
)

_ENABLE_STYLE_ARMED = (
    "QPushButton{background:#16a34a;color:#ffffff;font-weight:700;"
    "border-radius:5px;padding:4px 12px;}"
)
_ENABLE_STYLE_IDLE = (
    "QPushButton{background:#e5e7eb;color:#111a22;"
    "border-radius:5px;padding:4px 12px;}"
)


class GamepadPanel(QGroupBox):
    def __init__(self, register, motion_queue_provider, motion_controller, stop_all, speed_fn,
                 step_fn, set_step_fn, direction_fn, highlight_fn, on_armed_changed=None,
                 step_display_fn=None, info_fn=None, reader=None, clock=time.monotonic,
                 controller=None, poll_ms: int = 30, parent=None):
        from .i18n import translator, Retranslator, tr
        self._tr = tr
        self._rt = Retranslator(translator)
        super().__init__(tr("手柄控制"), parent)
        self._rt.title(self, "手柄控制")
        self._register = register
        self._queue_provider = motion_queue_provider
        self._motion_controller = motion_controller
        self._on_armed_changed = on_armed_changed or (lambda armed: None)
        self._stop_all = stop_all
        self._speed_fn = speed_fn
        self._step_fn = step_fn
        self._set_step_fn = set_step_fn
        self._direction_fn = direction_fn
        self._highlight_fn = highlight_fn
        self._step_display_fn = step_display_fn or step_fn
        self._info_fn = info_fn  # axis -> operator-facing "步长/速度+单位" detail string
        self._cont_epoch = {}   # axis -> monotonically increasing; a stop bumps it to stale pending continuous
        self._reader = reader or load_default_reader()
        self._controller = controller or GamepadController(now=clock)
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    def _build_ui(self):
        row = QHBoxLayout(self)
        self._enable_btn = self._reg("btnGamepadEnable", QPushButton(self._tr("启用手柄")))
        self._enable_btn.setCheckable(True)
        self._rt.tooltip(self._enable_btn, "START=启用/停用 · BACK=急停 · 点 ? 查看完整按键说明")
        self._enable_btn.clicked.connect(self._toggle_from_ui)
        self._help_btn = self._reg("btnGamepadHelp", QPushButton("?"))
        self._help_btn.setFixedWidth(28)
        self._rt.tooltip(self._help_btn, "查看手柄按键映射")
        self._help_btn.clicked.connect(self._show_mapping_help)
        self._hud = self._reg("gamepadHud", QLabel())
        row.addWidget(self._enable_btn)
        row.addWidget(self._help_btn)
        row.addWidget(self._hud, 1)
        self._refresh_hud()

    def _show_mapping_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(self._tr("手柄按键说明 (Logitech F710)"))
        layout = QVBoxLayout(dlg)

        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        # The full text is kept as the accessible description so screen readers
        # and assistive tooling still expose the mapping behind the figure.
        image.setAccessibleDescription(self._tr(GAMEPAD_MAPPING_TEXT))
        try:
            image.setPixmap(render_help_pixmap())
        except Exception:
            # No Qt SVG module (or render failure): fall back to legible text.
            image.setPixmap(None)
            image.setText(GAMEPAD_MAPPING_TEXT)
            image.setWordWrap(True)
            image.setStyleSheet("QLabel{color:#111a22;background:#ffffff;padding:14px;}")

        scroll = QScrollArea(dlg)
        scroll.setWidget(image)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok, dlg)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.resize(1040, 680)
        dlg.exec()

    def _reg(self, name, w):
        w.setObjectName(name)
        if self._register:
            self._register(name, w)
        return w

    def _toggle_from_ui(self):
        self._controller.enabled = self._enable_btn.isChecked()
        self._controller.reset()
        if not self._controller.enabled:
            self._safe_stop_all()
        self._on_armed_changed(self._controller.enabled)
        self._sync_selection_highlight()
        self._refresh_hud()

    def disarm(self):
        if self._controller.enabled:
            self._controller.enabled = False
            self._controller.reset()
            self._enable_btn.setChecked(False)
            self._safe_stop_all()
            self._on_armed_changed(False)
            self._sync_selection_highlight()
            self._refresh_hud()

    def _current_selected_axis(self):
        return SELECTOR_AXES[self._controller.selected_idx]

    def _sync_selection_highlight(self):
        # Highlight the current selected axis (stage-map banner + hotspot + card)
        # whenever armed; clear it when disarmed. Keeps the "selected axis" cue in
        # sync on arm/disarm, not only when the operator presses the selector keys.
        self._highlight_fn(self._current_selected_axis() if self._controller.enabled else None)

    def _safe_stop_all(self):
        for ax in GAMEPAD_AXES:
            self._invalidate_axis(ax)
        try:
            self._stop_all()
        except Exception as exc:  # noqa: BLE001 - timer path must stay alive
            _audit.critical("gamepad stop_all failed: %s", exc, exc_info=True)

    def tick(self):
        snap = self._reader.read_state()
        for intent in self._controller.poll(snap):
            self._apply(intent)
        if self._enable_btn.isChecked() != self._controller.enabled:
            self._enable_btn.setChecked(self._controller.enabled)
            self._on_armed_changed(self._controller.enabled)
            self._sync_selection_highlight()
            self._refresh_hud()

    def _apply(self, intent):
        if isinstance(intent, StopAll):
            self._safe_stop_all(); return
        if isinstance(intent, ToggleEnable):
            self._sync_selection_highlight(); self._refresh_hud(); return
        if isinstance(intent, SelectAxis):
            self._highlight_fn(intent.axis); self._refresh_hud(); return
        if isinstance(intent, StepDelta):
            self._set_step_fn(intent.axis, intent.delta); self._refresh_hud(); return

        queue = self._queue_provider()
        if queue is None:
            return
        if isinstance(intent, (StartContinuous, UpdateContinuous)):
            axis, frac = intent.axis, intent.frac
            sign = 1 if frac >= 0 else -1
            speed = self._direction_fn(axis, sign) * abs(frac) * self._speed_fn(axis)
            look = 0.0
            partner = {5: 7, 7: 5}.get(axis)
            if partner is not None:
                pf = self._controller.last_frac(partner)
                if pf is not None and pf != 0.0:
                    look = abs(pf) * self._speed_fn(partner) * (self._controller.cfg.update_period_s * 1000.0)
            epoch = self._cont_epoch.setdefault(axis, 0)
            queue.submit(lambda a=axis, s=speed, lk=look, e=epoch: self._jog_if_current(a, s, lk, e), priority=5)
        elif isinstance(intent, StopContinuous):
            axis = intent.axis
            self._invalidate_axis(axis)
            ok = queue.submit(lambda a=axis: self._stop_axis_via_queue(a), priority=0)
            if not ok:
                self._stop_axis_direct(axis)
        elif isinstance(intent, Jog):
            axis, d = intent.axis, intent.direction
            cdir = self._direction_fn(axis, d)
            speed = self._speed_fn(axis); step = self._step_fn(axis)
            queue.submit(lambda a=axis, c=cdir, sp=speed, st=step: self._relative(a, c, sp, st), priority=10)

    def _invalidate_axis(self, axis):
        self._cont_epoch[axis] = self._cont_epoch.get(axis, 0) + 1

    def _jog_if_current(self, axis, speed, lookahead, epoch):
        # Skip a continuous command that a later stop has superseded (best-effort
        # cross-thread guard: the epoch int is written on the poll thread and read
        # on the queue worker; a race only degrades to today's behavior).
        if epoch != self._cont_epoch.get(axis, 0):
            return
        self._jog(axis, speed, lookahead)

    def _stop_axis_direct(self, axis):
        ctrl = self._motion()
        if getattr(ctrl, "is_open", False):
            try:
                ctrl.stop_axis(axis)
            except Exception:
                pass

    def _jog(self, axis, speed, lookahead=0.0):
        self._motion().start_jog(axis, speed, partner_lookahead_pulse=lookahead)

    def _stop_axis_via_queue(self, axis):
        self._motion().stop_axis(axis)

    def _relative(self, axis, d, sp, st):
        self._motion().relative_move(axis, d, sp, st)

    def _motion(self):
        return self._motion_controller

    def _refresh_hud(self):
        from .axis_calibration import AXIS_LABELS
        axis = SELECTOR_AXES[self._controller.selected_idx]
        armed = self._controller.enabled
        state = self._tr("已启用") if armed else self._tr("未启用")
        detail = None
        if self._info_fn is not None:
            try:
                detail = self._info_fn(axis)
            except Exception:
                detail = None
        if detail is None:
            detail = f"{self._tr('步长:')}{self._step_display_fn(axis)}"
        selected_label = self._tr("选中:")
        text = f"{state} · {selected_label}{AXIS_LABELS.get(axis, axis)} · {detail}"
        self._hud.setText(text)
        self._hud.setToolTip(text)  # full text always recoverable on hover if the row clips
        # Make the armed state unmistakable: green button when live, neutral when idle.
        self._enable_btn.setText(self._tr("手柄已启用") if armed else self._tr("启用手柄"))
        self._enable_btn.setStyleSheet(_ENABLE_STYLE_ARMED if armed else _ENABLE_STYLE_IDLE)
