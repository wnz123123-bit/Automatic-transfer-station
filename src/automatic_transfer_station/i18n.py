from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .paths import UI_SETTINGS_PATH
from .json_store import atomic_write_json


TRANSLATIONS: dict[str, str] = {}

TRANSLATIONS.update({
    # main_window — static widget text
    "显微镜调整": "Microscope adjust",
    "坐标标定": "Coordinate calibration",
    "轴控制面板": "Axis control panel",
    "停止移动": "Stop motion",
    "打开运动控制": "Open motion control",
    "关闭运动控制": "Close motion control",
    "复位": "Reset",
    "运动控制未打开 - 预览模式": "Motion control closed – preview",
    "角度回零": "Home angle",
    "连续控制": "Continuous",
    "目标": "Target",
    "移动": "Move",
    "标记零点": "Mark zero",
    "点位管理": "Point manager",
    "速度 ({speed_unit})": "Speed ({speed_unit})",
    "步长 ({step_unit})": "Step ({step_unit})",
    # axis sections
    "吸附台": "Adsorption",
    "载玻片侧": "Slide",
    "整体位移": "Overall",
    # axis roles (card subtitles)
    "显微镜上下位移": "Microscope Z travel",
    "吸附台左右移动": "Adsorb left-right",
    "吸附台前后移动": "Adsorb front-back",
    "样品旋转": "Sample rotation",
    "载玻片左右移动": "Slide left-right",
    "载玻片前后移动": "Slide front-back",
    "载玻片升降": "Slide Z lift",
    "整体左右平移": "Overall left-right",
    "整体前后平移": "Overall front-back",
    # axis roles (stage hotspot)
    "整体左右": "Overall left-right",
    "整体前后": "Overall front-back",
    "显微镜上下": "Microscope up-down",
    "吸附台左右": "Adsorb left-right",
    "吸附台前后": "Adsorb front-back",
    "载玻片左右": "Slide left-right",
    "载玻片前后": "Slide front-back",
    # hotspot tooltip template
    "{axis} · {role}": "{axis} · {role}",
    # motion pad button tooltips
    "连续移动 · 坐标减小（按住）": "Continuous jog · coord decrease (hold)",
    "连续移动 · 坐标增大（按住）": "Continuous jog · coord increase (hold)",
    "单步移动 · 坐标减小": "Step jog · coord decrease",
    "单步移动 · 坐标增大": "Step jog · coord increase",
    "停止该轴": "Stop axis",
    "连续移动 坐标减小": "Continuous jog coord decrease",
    "连续移动 坐标增大": "Continuous jog coord increase",
    "单步移动 坐标减小": "Step jog coord decrease",
    "单步移动 坐标增大": "Step jog coord increase",
    # stop all tooltip
    "急停 (快捷键: Space)": "Emergency stop (shortcut: Space)",
    # board status messages
    "真实硬件模式 - 运动控制未打开": "Real hardware mode – motion control closed",
    "运动控制已打开": "Motion control opened",
    "运动控制已关闭": "Motion control closed",
    "运动控制已复位": "Motion control reset",
    "运动控制连接中…": "Motion control connecting…",
    "运动控制打开成功": "Motion control opened",
    "运动控制关闭中…": "Motion control closing…",
    "运动控制复位中…": "Motion control resetting…",
    "关闭运动控制失败，运动卡仍打开": "Close failed; board still open",
    "连续运动命令仍在执行，未关闭运动控制": "Continuous motion still running; control not closed",
    "关闭前停止失败: {exc}": "Stop before close failed: {exc}",
    "位置记录失败: {exc}": "Position record failed: {exc}",
    "运动控制操作超时（{kind}），运动卡无响应": "Motion control timeout ({kind}); board not responding",
    "操作超时": "Operation timeout",
    "操作失败": "Operation failed",
    # motion command messages
    "停止移动已执行": "Stop motion executed",
    "运动控制未打开，移动命令已忽略": "Motion control closed; move command ignored",
    "运动控制未打开，命令已忽略": "Motion control closed; command ignored",
    "运动命令失败: {message}": "Motion command failed: {message}",
    "{axis_name} 移动至 {target_text} {unit}": "{axis_name} moved to {target_text} {unit}",
    "目标位置无效": "Invalid target position",
    # gamepad
    "手柄选中: {detail} · 步长 {step} · 坐标 {coord}": "Gamepad: {detail} · step {step} · coord {coord}",
    "手柄已启用，屏幕轴按钮已禁用": "Gamepad armed; on-screen jog buttons disabled",
    "手柄已停用，屏幕轴按钮已恢复": "Gamepad disarmed; on-screen jog buttons restored",
    "手柄急停失败: {exc}": "Gamepad emergency stop failed: {exc}",
    "{axis} 已选中": "{axis} selected",
    # calibration dialog
    "轴": "Axis",
    "当前读数": "Current reading",
    "实际位置": "Actual position",
    "单位": "Unit",
    "取消": "Cancel",
    "应用标定": "Apply calibration",
    "标定失败": "Calibration failed",
    "请至少输入一个实际位置": "Enter at least one actual position",
    "{axis_name}: 请先打开运动控制或加载该轴的已记录位置": "{axis_name}: open motion control or load a cached position first",
    "{axis_name} 实际位置不能小于 {min_coord:g} {unit}": "{axis_name} actual position must not be less than {min_coord:g} {unit}",
    "{axis_name} 实际位置不能大于 {max_coord:g} {unit}": "{axis_name} actual position must not exceed {max_coord:g} {unit}",
    "确认标定覆盖": "Confirm calibration override",
    "即将覆盖轴 {axes_str} 的坐标标定，这将重定义该轴的坐标参考系并写入磁盘，"
    "错误的值可能导致错向/超程。确定覆盖？":
        "About to override coordinate calibration for axis {axes_str}. "
        "This redefines the axis reference frame and writes to disk. "
        "Wrong values may cause direction reversal or over-travel. Proceed?",
    "{axis_name} 标定：目前位置 {current_value} {unit}，实际位置 {actual_value} {unit}":
        "{axis_name} calibrated: current {current_value} {unit}, actual {actual_value} {unit}",
    "坐标标定：已应用 {axes}": "Coordinate calibration applied: {axes}",
    "位置数值无效: {text}": "Invalid position value: {text}",
    # axis3 position manager dialog
    "显微镜点位管理": "Microscope point manager",
    "名称": "Name",
    "点位名称": "Point name",
    "例如：安全高度 / 观察高度 / 接触前": "e.g. Safe height / View height / Before contact",
    "保存当前位置": "Save current position",
    "移动到选中点位": "Move to selected point",
    "重命名": "Rename",
    "删除": "Delete",
    "关闭": "Close",
    "保存失败": "Save failed",
    "点位名称不能为空": "Point name must not be empty",
    "运动失败": "Motion failed",
    "请先选择一个点位": "Select a point first",
    "重命名失败": "Rename failed",
    "删除失败": "Delete failed",
    "MicroscopeZ 已保存点位 {name}: {value:.2f} pulse": "MicroscopeZ saved point {name}: {value:.2f} pulse",
    "MicroscopeZ 移动到点位 {name}: {value:.2f} pulse": "MicroscopeZ moved to point {name}: {value:.2f} pulse",
    "MicroscopeZ 点位已重命名为 {name}": "MicroscopeZ point renamed to {name}",
    "MicroscopeZ 已删除点位 {name}": "MicroscopeZ deleted point {name}",
    # axis9 twist dialog direction combo
    "顺时针": "Clockwise",
    "逆时针": "Counter-clockwise",
    # Z continuous dialog direction combo
    "上升": "Up",
    "下降": "Down",
    # axis9 twist dialog
    "相对角度": "Relative angle",
    "方向": "Direction",
    "执行": "Apply",
    "R twist：{direction} {angle:.3f} deg，目标 {target:.3f} deg":
        "R twist: {direction} {angle:.3f} deg, target {target:.3f} deg",
    # Z continuous dialog
    "速度": "Speed",
    "距离": "Distance",
    "Z 连续控制：{direction} {distance:.3f} um，速度 {speed:g} um/s":
        "Z continuous: {direction} {distance:.3f} um, speed {speed:g} um/s",
    # R home confirm
    "确认 R 轴物理回零": "Confirm R-axis physical home",
    "R 轴将执行物理回零（归零行程），请确认样品和机械处于安全位置。\n\n确定执行？":
        "R axis will perform a physical home (homing travel). Confirm the sample and mechanism are in a safe position.\n\nProceed?",
    "轴3位置已清零": "Axis 3 position zeroed",
    "轴9开始物理回零": "Axis 9 homing started",
    # safety / watchdog messages
    "安全检查：所有轴在安全范围内": "Safety check: all axes in range",
    "安全停止轴 {axes}: {message}": "Safety stop axis {axes}: {message}",
    "位置读取无响应，运动安全扫描已暂停；请确认物理急停/硬件限位可用":
        "Position read unresponsive; motion safety scan paused. Check physical e-stop / hardware limits",
    "位置读取连续失败 {n} 次，已保护性停止；请确认物理急停/硬件限位可用":
        "Position read failed {n} times consecutively; protective stop issued. Check physical e-stop / hardware limits",
    # persistence warnings
    "轴标定文件中 {labels} 超出范围已忽略，改用内置标定":
        "Axis calibration file: {labels} out of range; using built-in calibration",
    "轴标定文件版本不受支持，改用内置标定": "Axis calibration file version unsupported; using built-in calibration",
    "位置缓存文件版本不受支持，已忽略": "Position cache file version unsupported; ignored",
    "显微镜点位文件版本不受支持，已忽略": "Microscope point file version unsupported; ignored",
    # close / exit
    "确认退出": "Confirm exit",
    "确认退出程序？\n退出后将关闭加热（ENABLE=0）并断开所有端口。":
        "Exit the program?\nThis will stop heating (ENABLE=0) and disconnect all ports.",
    "连续运动命令仍在执行，窗口关闭时未能完全停止队列":
        "Continuous motion still running; queue not fully stopped on close",
    "窗口关闭按钮已禁用：请点击右下角「退出 Exit」按钮退出程序":
        "Window close button disabled: use the Exit button at the bottom-right to quit",
    # Z continuous dialog title
    "Z 连续控制": "Z continuous control",
    # gamepad HUD detail
    "步长:{step_v:g} {step_unit} · 速度:{speed_v:g} {speed_unit}":
        "Step:{step_v:g} {step_unit} · Speed:{speed_v:g} {speed_unit}",
})

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
