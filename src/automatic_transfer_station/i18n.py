from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .paths import UI_SETTINGS_PATH
from .json_store import atomic_write_json


TRANSLATIONS: dict[str, str] = {}

TRANSLATIONS.update({
    "温度控制": "Temperature Control",
    "连接": "Connection",
    "刷新端口": "Refresh ports",
    "开始连接": "Connect",
    "断开连接": "Disconnect",
    "COM 端口": "COM port",
    "连接状态": "Connection status",
    "当前状态": "Current status",
    "当前温度": "Current temp",
    "当前给定": "Current setpoint",
    "目标温度": "Target temp",
    "输出 (PWM)": "Output (PWM)",
    "最大输出占空比": "Max duty",
    "控制状态": "Control status",
    "错误码": "Error code",
    "控制": "Control",
    "快速设温": "Quick set",
    "按速率控温": "Ramp set",
    "关闭输出": "Stop output",
    "控温速率": "Ramp rate",
    "温度曲线": "Temperature curve",
    "等待实时读数": "Waiting for live data",
    "等待接入": "Waiting",
    "未知": "Unknown",
    "输出开启": "Output ON",
    "输出关闭": "Output OFF",
    "超温保护": "Over-temp protection",
    "超温保护-关闭输出失败": "Over-temp - disable FAILED",
    "⚠ 高温警告：加热时请勿触碰加热台，谨防烫伤；离开或退出前务必关闭输出（ENABLE=0）":
        "⚠ HIGH-TEMP WARNING: do not touch the heater stage while heating; always stop output (ENABLE=0) before leaving or exiting",
    "RD105 未连接": "RD105 not connected",
    "连接失败": "Connection failed",
    "已连接 {port}": "Connected {port}",
    "已断开": "Disconnected",
    "正在断开": "Disconnecting",
    "断开失败": "Disconnect failed",
    "已断开（关闭输出失败）": "Disconnected (stop-output failed)",
    "RD105 端口已刷新": "RD105 ports refreshed",
    "RD105 正在断开连接，请稍候": "RD105 disconnecting, please wait",
    "RD105 正在连接：{port}": "RD105 connecting: {port}",
    "RD105 已连接：{port}": "RD105 connected: {port}",
    "RD105 已断开": "RD105 disconnected",
    "RD105 正在断开连接（先关闭输出）": "RD105 disconnecting (stopping output first)",
    "RD105 断开失败：{message}": "RD105 disconnect failed: {message}",
    "RD105 通信丢失或读取卡死——温度轮询无响应，请检查连接（加热输出由看门狗保护）":
        "RD105 comms lost or read stalled - temperature polling unresponsive; check the connection (heater output protected by watchdog)",
    "读取失败": "read failed",
    "RD105 控温错误：{control_error}": "RD105 control error: {control_error}",
    "RD105 软件斜坡控温中：目标 {final_target}，速率 {rate} °C/h":
        "RD105 software ramp active: target {final_target}, rate {rate} °C/h",
    "RD105 实时读数已更新": "RD105 live data updated",
    "实时读数已更新": "live data updated",
    "确认高温设定": "Confirm high-temp setpoint",
    "目标温度 {target:.2f} °C 超过 {warn:g} °C，请确认操作安全后再发送。确定设温？":
        "Target {target:.2f} °C exceeds {warn:g} °C. Confirm it is safe before sending. Set temperature?",
    "RD105 正在发送快速设温": "RD105 sending quick set",
    "RD105 正在发送按速率控温": "RD105 sending ramp set",
    "RD105 正在关闭输出": "RD105 stopping output",
    "快速设温已发送": "Quick set sent",
    "按速率控温已发送": "Ramp set sent",
    "输出已关闭": "Output stopped",
    "RD105 输出已关闭": "RD105 output stopped",
    "RD105 {action}失败：{err}": "RD105 {action} failed: {err}",
    "RD105 {action}失败：{message}": "RD105 {action} failed: {message}",
    "RD105 未连接，不能发送温控命令": "RD105 not connected; cannot send a control command",
    "当前 {cur} / 给定 {sp}": "Current {cur} / setpoint {sp}",
})


class Translator:
    def __init__(self, lang: str = "en", table: dict[str, str] | None = None):
        self._lang = lang if lang in ("en", "zh") else "en"
        self._table = TRANSLATIONS if table is None else table
        self._observers: list[Callable[[], None]] = []
        self._saver: Callable[[str], None] | None = None

    @property
    def lang(self) -> str:
        return self._lang

    def tr(self, zh: str) -> str:
        if self._lang == "zh":
            return zh
        return self._table.get(zh, zh)

    def initialize(self, lang: str) -> None:
        if lang in ("en", "zh"):
            self._lang = lang

    def set_language(self, lang: str) -> None:
        if lang not in ("en", "zh") or lang == self._lang:
            return
        self._lang = lang
        if self._saver is not None:
            self._saver(lang)
        for cb in list(self._observers):
            cb()

    def add_observer(self, cb: Callable[[], None]) -> None:
        self._observers.append(cb)

    def clear_observers(self) -> None:
        self._observers.clear()

    def set_saver(self, saver: Callable[[str], None] | None) -> None:
        self._saver = saver


translator = Translator()


def tr(zh: str) -> str:
    return translator.tr(zh)


def load_language(path: Path | None = None, default: str = "en") -> str:
    path = UI_SETTINGS_PATH if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        lang = data.get("language")
    except (OSError, ValueError, AttributeError):
        return default
    return lang if lang in ("en", "zh") else default


def save_language(lang: str, path: Path | None = None) -> None:
    if lang not in ("en", "zh"):
        return
    atomic_write_json(UI_SETTINGS_PATH if path is None else path, {"language": lang})


class Retranslator:
    def __init__(self, translator: "Translator"):
        self._translator = translator
        self._apply: list[Callable[[], None]] = []
        translator.add_observer(self.retranslate)

    def _add(self, fn: Callable[[], None]) -> None:
        fn()
        self._apply.append(fn)

    def text(self, widget, zh: str) -> None:
        self._add(lambda: widget.setText(self._translator.tr(zh)))

    def title(self, widget, zh: str) -> None:
        setter = getattr(widget, "setTitle", None) or widget.setWindowTitle
        self._add(lambda: setter(self._translator.tr(zh)))

    def tooltip(self, widget, zh: str) -> None:
        self._add(lambda: widget.setToolTip(self._translator.tr(zh)))

    def placeholder(self, widget, zh: str) -> None:
        self._add(lambda: widget.setPlaceholderText(self._translator.tr(zh)))

    def items(self, combo, zh_list: list[str]) -> None:
        def apply():
            for i, zh in enumerate(zh_list):
                combo.setItemText(i, self._translator.tr(zh))
        self._add(apply)

    def custom(self, apply_fn: Callable[[], None]) -> None:
        self._add(apply_fn)

    def retranslate(self) -> None:
        for fn in self._apply:
            fn()
