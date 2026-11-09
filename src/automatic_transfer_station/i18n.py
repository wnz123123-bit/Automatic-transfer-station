from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .paths import UI_SETTINGS_PATH
from .json_store import atomic_write_json


_log = logging.getLogger("ats.i18n")


TRANSLATIONS: dict[str, str] = {
    "定位与路径": "Reference and path",
    "实时图像": "Live view",
    "实时预览无法启动：{error}": "Could not start live preview: {error}",
    "实时图像 · 未连接": "Live view · Disconnected",
    "实时图像 · 等待图像": "Live view · Waiting for frames",
    "实时图像 · 实时": "Live view · Live",
    "实时图像 · 已暂停": "Live view · Paused",
    "实时图像 · 图像不可用": "Live view · Image unavailable",
    "区域配方": "Area recipe",
    '硬件诊断': 'Hardware diagnostics',
    '出光仍受阻：独立硬件切断与固件能力待作业层确认。': 'Emission remains blocked: the job must qualify independent inhibit and firmware capabilities.',
    '禁止出光：联锁与触发接线能力未确认。': 'Emission blocked: interlock/trigger wiring capabilities are unconfirmed.',

    # Compact cutting workspace
    '1 · 拍照': '1 · Capture',
    '2 · 路径与参数': '2 · Path and parameters',
    '3 · 预览与执行': '3 · Preview and run',
    '更多图像选项': 'Image options',
    '绘制路径': 'Draw path',
    '绘图帮助': 'Drawing help',
    '首次使用或硬件改变时，在这里完成设置。': 'Configure here on first use or after hardware changes.',
    '像素与位移标定': 'Image-to-stage calibration',
    '回程差补偿': 'Backlash compensation',
    '运动与激光设备': 'Motion and laser setup',
    '加工': 'Cutting',
    '设置': 'Setup',
    '相机：{camera} · 标定：{calibration}\n激光：{laser}': 'Camera: {camera} · Calibration: {calibration}\nLaser: {laser}',
    '已连接': 'Connected',
    '未连接': 'Disconnected',
    '故障': 'Fault',
    '待启动确认': 'Startup check needed',
    '待命': 'Armed',
    '作业中': 'Job active',
    '状态未知': 'Unknown',
    '已就绪': 'Ready',
    '未就绪': 'Needed',
    '作业进行中；可随时停止。': 'Job running. STOP remains available.',
    '激光状态异常，请在设置中检查设备。': 'Laser fault or unknown state. Check Setup.',
    '请先连接相机。': 'Connect the camera first.',
    '请先打开运动控制。': 'Open motion control first.',
    '拍照后选择 ROI 和切割路径。': 'Capture a photo, then select an ROI and path.',
    '请在设置中完成或加载标定。': 'Complete or load calibration in Setup.',
    '绘制路径后生成预览。': 'Draw a path, then generate its preview.',
    '激光未连接；空跑前请检查运动设置。': 'Laser disconnected. Check motion setup before a dry run.',
    '出光条件未满足，详情见设置。': 'Emission requirements unmet. See Setup.',
    '路径已就绪，请先关光空跑。': 'Path ready. Rehearse with the beam blocked first.',

    "激光切割工作台": "Laser cutting",
    "原始图像（未翻转）· 仅 X3 / Y3": "Raw image (unflipped) · X3 / Y3 only",
    "5 · Arduino 与硬件联锁": "5 · Arduino and interlock",
    "6 · 执行": "6 · Run",
    "虚线：关光移动与回差预加载；实线：切割路径。": "Dashed: dark travel and backlash take-up. Solid: cutting path.",
    "导出硬件调试记录模板": "Export commissioning template",
    "加载并确认硬件调试记录": "Load commissioning record",
    "真实出光需要独立关光联锁、运动卡触发和时序验证。": "Emission requires an independent inhibit, motion-card trigger and verified timing.",
    "执行激光切割": "Run laser cutting",
    "已包含关光加减速段；执行前将再次核对联锁和定位状态。": "Dark acceleration/deceleration included. Interlock and reference will be rechecked before execution.",
    "暂不可出光：{error}": "Emission unavailable: {error}",
    "确认本次连接的硬件调试": "Confirm hardware commissioning",
    "已实测独立关光联锁、断线关光、运动卡输出极性和触发延迟；记录与当前接线一致。": "Independent inhibit, disconnect response, output polarity and trigger latency have been measured. This record matches the current wiring.",
    "确认激光切割": "Confirm laser cutting",
    "本次操作将真实出光。已核对样品、路径、独立联锁和启动状态，并完成同路径关光空跑。": "This operation emits laser light. Verify the sample, path, independent inhibit and startup, and complete a dark rehearsal of the same path.",
    "正在执行激光切割；停止键始终可用。": "Laser cutting in progress. Stop remains available.",
    "切割完成；请重新拍摄定位照片。": "Cutting complete. Capture a new reference photo.",
    "切割进度：{step}/{total}": "Cutting progress: {step}/{total}",
    "绘制烧蚀区域": "Draw ablation area",
    "区域：依次点击顶点，Enter 或双击闭合，Backspace 撤销，Esc 取消。": "Area: click vertices; Enter/double-click closes, Backspace undoes, Esc cancels.",
    "用 ROI 作为烧蚀区域": "Use ROI as ablation area",
    "请先绘制闭合区域。": "Draw a closed area first.",
    "区域路径已生成：{count} 次加工，估计 {seconds:.1f} 秒。": "Area path ready: {count} exposures, estimated {seconds:.1f} s.",
    "请先绘制区域并选择扫描模式。": "Draw an area and select a raster mode first.",
    "保存加工配方": "Save cutting recipe", "加载加工配方": "Load cutting recipe",
    "加工配方已保存：{path}": "Cutting recipe saved: {path}",
    "配方的相机或物镜与当前定位照片不匹配。": "Recipe camera/objective does not match this reference photo.",
    "配方仅恢复图案和参数；请在新照片上重新对齐样品，再生成预览。": "Recipe restores pattern/settings only. Realign on the new photo, then generate a preview.",
    "请重新生成区域路径以核对出光条件。": "Generate the area path again to check emission requirements.",
    "加工模式": "Pattern mode", "单线": "Single line",
    "重试关光与停止": "Retry OFF and stop",
    "硬件设置已改变，请重新拍摄定位照片。": "Hardware configuration changed. Capture a new reference photo.",
    "停止状态未确认，请检查设备连接并重试停止。": "Stop is unconfirmed. Check device connections and retry cleanup.",
    "区域行扫描": "Area: continuous rows", "区域点阵": "Area: dots",
    "行距 (µm)": "Row pitch (µm)", "点距 (µm)": "Point pitch (µm)",
    "扫描角度 (°)": "Scan angle (°)", "到点稳定时间 (ms)": "Settling time (ms)",
    "曝光窗口 (ms)": "Exposure window (ms)",
    "保存区域配方": "Save area recipe", "加载区域配方": "Load area recipe",
    "配方只保存区域和参数；加载后需重新拍照并对齐。": "Recipes save area/settings only; use a fresh photo and realign after loading.",
    "单线使用画布上的切割线和路径速度。": "Single line uses the drawn line and path speed.",
    "行扫描按单向曝光，回程与预加载保持关光。": "Rows expose in one direction; returns and take-up remain dark.",
    "点阵移动全程关光；FIRE 设置毫秒曝光窗口，不代表单个皮秒脉冲。": "Dot travel is dark. FIRE sets a millisecond envelope, not a single picosecond pulse.",
    "尚未生成区域预览。": "No area preview generated.",
    "曝光数：{count} · 预计时间：{seconds:.2f} s": "Exposures: {count} · Estimated time: {seconds:.2f} s",
    "Arduino 已连接；请核对结构化状态并完成启动检查。": "Arduino connected. Check device status and complete startup checks.",
    "Comp HIGH/LOW 和串口 ACK 不是光功率测量。联锁感测不等于独立硬件切断。": "Comp levels and serial ACKs do not measure optical power. Interlock sensing is not an independent hardware inhibit.",
    "Comp 电平 ACK": "Comp level ACK",
    "OFF / 急停": "OFF / E-stop",
    "OFF 或断开尚未确认；保留面板并等待安全退出。": "OFF/disconnect pending. Waiting for workers before closing.",
    "OFF 请求已处理；ACK 不代表已测得光功率为零。": "OFF request processed. ACK does not prove zero optical power.",
    "{seconds:.1f} 秒前": "{seconds:.1f} s ago",
    "仅连接 Arduino 控制串口。激光电源必须关闭或光路已物理遮挡；打开串口可能触发 DTR 复位并将 Comp 置为 HIGH。": "Connect only the Arduino port. Power off the laser or physically block the beam; opening the port may trigger a DTR reset and drive Comp HIGH.",
    "关闭任务已结束，但 OFF 或串口断开未确认：{error}": "Cleanup ended; OFF or disconnect unconfirmed: {error}",
    "出光仍受阻：还需作业层确认独立硬件切断及固件能力。": "Emission remains blocked until independent inhibit and firmware capabilities are qualified for the job.",
    "刷新串口": "Refresh ports", "否": "No", "是": "Yes", "未确认": "Unconfirmed",
    "启动检查已完成": "Startup checks completed", "固件版本": "Firmware version",
    "已关闭激光电源或物理遮挡光路": "Laser powered off or beam physically blocked",
    "控制器状态": "Controller state", "故障信息": "Fault", "无已报告故障": "No reported fault",
    "最近应答": "Last reply", "激光控制已断开。": "Laser controller disconnected.",
    "激光控制操作失败：{error}": "Laser operation failed: {error}",
    "激光控制未连接；串口只连接 Arduino。": "Laser controller disconnected. Connect the Arduino port only.",
    "看门狗窗口 (ms)": "Watchdog (ms)",
    "确认激光器自检、温度稳定和 CDRH 启动延时均已完成；复位或故障后须重新确认。": "Confirm laser self-test, stable temperature and CDRH startup delay. Repeat after reset or fault.",
    "禁止出光：固件未确认联锁与触发接线能力；当前硬件缺少独立切断。": "Emission blocked: firmware has no qualified interlock/trigger wiring; an independent inhibit is required.",
    "联锁接线能力": "Interlock wiring", "联锁输入状态": "Interlock input",
    "触发接线能力": "Trigger wiring", "触发输入状态": "Trigger input",
    "请先选择 Arduino 串口。": "Select the Arduino port first.",
    "连接 Arduino 前确认": "Confirm before connecting Arduino",
    "选择 Arduino 串口": "Select Arduino port",
    "启动检查固件 ACK": "Startup check ACK",
    "启动检查已获固件确认；保持 OFF，出光仍受硬件与作业检查限制。": "Startup check acknowledged; output remains OFF. Emission still requires hardware and job checks.",
    "正在发送 OFF 并确认启动检查，请等待固件 ACK。": "Sending OFF and startup confirmation. Waiting for firmware ACK.",
}

CONTEXT_TRANSLATIONS: dict[tuple[str, str], str] = {
    ("objective", "连接"): "Connect",
    ("camera", "连接"): "Connect",
    ("camera", "断开"): "Disconnect",
}

TRANSLATIONS.update({
    # main_window — static widget text
    "显微镜调整": "Microscope adjust",
    "坐标标定": "Calibration",
    "二维材料转移平台": "2D Material Transfer Station",
    "轴控制面板": "Motion control",
    "镜Z": "Microscope Z",
    "停止移动": "Stop motion",
    "打开运动控制": "Open motion control",
    "关闭运动控制": "Close motion control",
    "复位": "Reset",
    "运动控制未打开 - 预览模式": "Motion control closed – preview",
    "R 轴回零等待超时": "R-axis homing timed out",
    "R 轴正在回零": "R axis is homing",
    "R 轴回零已取消": "R-axis homing cancelled",
    "角度回零": "Home",
    "连续控制": "Continuous",
    "目标": "Target",
    "移动": "Move",
    "标记零点": "Mark zero",
    "点位管理": "Point manager",
    "速度 ({speed_unit})": "Speed ({speed_unit})",
    "步长 ({step_unit})": "Step ({step_unit})",
    "速度\n({speed_unit})": "Speed\n({speed_unit})",
    "步长\n({step_unit})": "Step\n({step_unit})",
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
    # camera_panel — static widget text and dynamic messages
    "Basler 相机": "Basler camera",
    "相机预览": "Camera preview",
    "相机: 未连接": "Camera: disconnected",
    "相机: {model} ({serial})": "Camera: {model} ({serial})",
    "无图像": "No image",
    "图像缩放: {percent}%": "Image zoom: {percent}%",
    "鼠标滚轮缩放预览图像": "Use the mouse wheel to zoom the preview image",
    "预览帧率: -- FPS": "Preview FPS: --",
    "预览帧率: {fps:.1f} FPS": "Preview FPS: {fps:.1f}",
    "预览": "Preview",
    "拍照": "Capture",
    "开始预览": "Start preview",
    "停止预览": "Stop preview",
    "截图": "Snapshot",
    "采集": "Acquisition",
    "保存": "Save",
    "目录": "Directory",
    "浏览": "Browse",
    "文件名": "Filename",
    "格式": "Format",
    "清晰度": "Quality",
    "Live binning": "Live binning",
    "Snap binning": "Snap binning",
    "Live 像素": "Live pixels",
    "Snap 像素": "Snap pixels",
    "曝光/增益": "Exposure / gain",
    "自动曝光": "Auto exposure",
    "自动增益": "Auto gain",
    "锐化": "Sharpness",
    "比例尺": "Scale bar",
    "倍率": "Objective",
    "当前倍率": "Current objective",
    "已知长度": "Known length",
    "两点标定": "Two-point calibration",
    "标定...": "Calibrate...",
    "比例尺标定": "Scale calibration",
    "开始两点标定": "Start two-point calibration",
    "显示": "Display",
    "关闭": "Close",
    "水平翻转": "Flip horizontal",
    "垂直翻转": "Flip vertical",
    "测量": "Measurement",
    "十字线": "Crosshair",
    "中心十字线": "Center crosshair",
    "直线距离": "Line distance",
    "面积": "Area",
    "完成面积": "Finish area",
    "撤销": "Undo",
    "清空": "Clear",
    "截图包含标注": "Annotate snapshots",
    "测量: 就绪": "Measurement: ready",
    "测量结果": "Measurement results",
    "ID": "ID",
    "类型": "Type",
    "结果": "Result",
    "备注": "Note",
    "删除选中": "Delete selected",
    "未标定": "Uncalibrated",
    "测量: 直线距离，点击两个端点": "Measurement: line distance; click two endpoints",
    "测量: 面积，逐点点击后按完成面积": "Measurement: area; click points then finish area",
    "测量: 已记录第一个端点": "Measurement: first endpoint recorded",
    "测量: 面积点 {count}，继续点击或按完成面积": "Measurement: area point {count}; continue or finish area",
    "测量: 面积至少需要三个点": "Measurement: area needs at least three points",
    "测量: 当前点数 {count}": "Measurement: current point count {count}",
    "测量: 已撤销上一条结果": "Measurement: previous result undone",
    "测量: 已清空": "Measurement: cleared",
    "测量: M{id} {value}": "Measurement: M{id} {value}",
    "测量: 已删除选中结果": "Measurement: selected result deleted",
    "测量: 十字线关闭": "Measurement: center crosshair off",
    "十字线: X {x:.1f} px, Y {y:.1f} px (未标定)": "Crosshair: X {x:.1f} px, Y {y:.1f} px (uncalibrated)",
    "十字线: X {x:.1f} px / {x_um:.1f} um, Y {y:.1f} px / {y_um:.1f} um":
        "Crosshair: X {x:.1f} px / {x_um:.1f} um, Y {y:.1f} px / {y_um:.1f} um",
    "中心十字线: X {x:.1f} px, Y {y:.1f} px (未标定)":
        "Center crosshair: X {x:.1f} px, Y {y:.1f} px (uncalibrated)",
    "中心十字线: X {x:.1f} px / {x_um:.1f} um, Y {y:.1f} px / {y_um:.1f} um":
        "Center crosshair: X {x:.1f} px / {x_um:.1f} um, Y {y:.1f} px / {y_um:.1f} um",
    "测量: 坐标系变化，已清空测量": "Measurement: coordinate system changed; measurements cleared",
    "选择保存目录": "Select save directory",
    "未标定": "Uncalibrated",
    "Basler 相机未连接": "Basler camera disconnected",
    "正在连接相机...": "Connecting camera...",
    "相机未连接": "Camera disconnected",
    "相机预览中": "Camera preview running",
    "截图失败: {error}": "Snapshot failed: {error}",
    "截图已保存: {name}": "Snapshot saved: {name}",
    "没有可保存的相机图像": "No camera image to save",
    "预览停止超时，已取消拍照以保护相机状态": "Preview did not stop in time; snapshot canceled to protect camera state",
    "无法写入截图文件: {path}": "Cannot write snapshot file: {path}",
    "Basler 相机已连接": "Basler camera connected",
    "Basler 相机已断开": "Basler camera disconnected",
    "曝光设置已发送": "Exposure setting sent",
    "增益设置已发送": "Gain setting sent",
    "图像设置已发送": "Image setting sent",
    "预览失败: {error}": "Preview failed: {error}",
    "请在预览图像上点击标尺两端": "Click both ends of the scale reference in the preview",
    "已记录第一个标定点": "First calibration point recorded",
    "{objective}: 旧标定缺少采样倍率，请重新标定": "{objective}: previous calibration has no binning factor. Please recalibrate.",
    "无法确认图像采样倍率，无法标定": "Cannot calibrate without a verified image binning factor.",
    "{objective}: 未标定": "{objective}: uncalibrated",
    "{objective}: {value:.4g} um/pixel": "{objective}: {value:.4g} um/pixel",
})

TRANSLATIONS.update({
    # objective_panel — static widget text
    "物镜选择": "Objective selector",
    "物镜串口: 未连接": "Objective port: disconnected",
    "物镜串口: 已连接": "Objective port: connected",
    "当前物镜: 未读取": "Current objective: not read",
    "物镜 COM 设置": "Objective COM settings",
    "读取位置": "Read position",
    "物镜串口未连接": "Objective port not connected",
    # objective_panel — dynamic messages
    "端口已刷新": "Ports refreshed",
    "物镜串口已断开": "Objective port disconnected",
    "断开": "Disconnect",
    "已连接 {port}": "Connected {port}",
    "连接失败: {error}": "Connection failed: {error}",
    "未配置物镜串口工厂": "Objective port factory not configured",
    "正在连接，请稍候": "Connecting, please wait…",
    "操作进行中，请稍候": "Operation in progress, please wait…",
    "正在连接 {port} ...": "Connecting {port}…",
    "物镜切换中，请等待": "Switching objective, please wait…",
    "物镜切换中: {label} (孔位 {position})": "Switching objective: {label} (slot {position})",
    "当前物镜: {label} (孔位 {position})": "Current objective: {label} (slot {position})",
    "物镜切换失败: {error}": "Objective switch failed: {error}",
    "物镜位置读取失败: {error}": "Objective position read failed: {error}",
    # gamepad_panel — static widget text
    "手柄控制": "Gamepad control",
    "启用手柄": "Enable gamepad",
    "手柄已启用": "Gamepad armed",
    "START=启用/停用 · BACK=急停 · 点 ? 查看完整按键说明":
        "START=arm/disarm · BACK=e-stop · click ? for full key reference",
    "查看手柄按键映射": "View gamepad key mapping",
    "手柄按键说明 (Logitech F710)": "Gamepad Key Reference (Logitech F710)",
    "已启用": "Armed",
    "未启用": "Disarmed",
    "选中:": "Selected:",
    "步长:": "Step:",
    # gamepad_panel — GAMEPAD_MAPPING_TEXT (used as accessibleDescription / fallback text)
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
    "肩键 LB / RB：显微镜 Z 下 / 上（仅单步，步长取界面显微镜Z步长档）\n\n"
    "可选轴（面键 2D 选择）：X3 / Y3 / X2 / Y2 / R / 载玻片Z / X1 / Y1\n"
    "（显微镜Z 由 LT/RT 连续控制、LB/RB 单步控制，不在可选轴内）\n"
    "当前选中轴会在左侧设备图横幅与高亮上显示（含步长与当前坐标）。":
        "START: arm / disarm gamepad\n"
        "BACK: emergency stop (all axes stop immediately)\n\n"
        "Left stick: X3 (left-right) / Y3 (front-back) continuous\n"
        "Right stick: X2 (left-right) / Y2 (front-back) continuous\n"
        "(sticks are proportional speed — push further for faster)\n\n"
        "D-pad ↑ / ↓: selected axis step + / − (single step only, not continuous)\n"
        "D-pad ← / →: step size smaller / larger\n\n"
        "Face buttons select axis (by position on device diagram):\n"
        "  Y=up  A=down  X=left  B=right\n\n"
        "Triggers LT / RT: microscope Z down / up (tap=step, hold=continuous; capped at 4 mm/s proportional, independent of UI speed)\n"
        "Bumpers LB / RB: microscope Z down / up (single step only; step size follows the UI MicroscopeZ step notch)\n\n"
        "Selectable axes (face-button 2D selection): X3 / Y3 / X2 / Y2 / R / Slide Z / X1 / Y1\n"
        "(Microscope Z is controlled continuously by LT/RT and stepped by LB/RB, not in the selectable set)\n"
        "The currently selected axis is shown in the left-panel stage banner and highlight (including step and coordinate).",
    # gamepad_help — SVG labels
    "Logitech F710 手柄按键说明": "Logitech F710 Gamepad Key Reference",
    "MODE 拨到 X · 摇杆推得越多越快（比例速度）":
        "Set MODE to X · sticks are proportional speed (push further = faster)",
    "俯视图（正面）": "Top view (face controls)",
    "前视图（肩键 / 扳机一侧）": "Front view (shoulder / trigger edge)",
    "急停（所有轴立即停止）": "E-stop (all axes stop immediately)",
    "十字键": "D-pad",
    "↑ / ↓  选中轴 单步 ±": "↑ / ↓  selected axis step ±",
    "← / →  步长档 减小 / 增大": "← / →  step size smaller / larger",
    "左摇杆": "Left stick",
    "X3（左右）/ Y3（前后） 连续": "X3 (L-R) / Y3 (F-B) continuous",
    "启用 / 停用手柄": "Arm / disarm gamepad",
    "面键 ": "Face btn ",
    "选轴：Y=上  A=下  X=左  B=右": "Select axis: Y=up  A=down  X=left  B=right",
    "右摇杆": "Right stick",
    "X2（左右）/ Y2（前后） 连续": "X2 (L-R) / Y2 (F-B) continuous",
    "载玻片Z 下": "Slide Z down",
    "显微镜Z 下": "Scope Z down",
    "显微镜Z 下（单步）": "Scope Z down (step)",
    "轻按单步 · 长按连续 ≤4mm/s": "tap=step · hold=continuous ≤4mm/s",
    "载玻片Z 上": "Slide Z up",
    "显微镜Z 上": "Scope Z up",
    "显微镜Z 上（单步）": "Scope Z up (step)",
    "MODE 拨到 X 模式": "Set MODE to X",
    "可选轴：X3 / Y3 / X2 / Y2 / R / 载玻片Z / X1 / Y1（显微镜Z 由扳机 LT/RT 控制）":
        "Selectable axes: X3 / Y3 / X2 / Y2 / R / Slide Z / X1 / Y1 (Scope Z by triggers LT/RT)",
    "可选轴：X3 / Y3 / X2 / Y2 / R / 载玻片Z / X1 / Y1（显微镜Z 由 LT/RT 连续、LB/RB 单步控制）":
        "Selectable axes: X3 / Y3 / X2 / Y2 / R / Slide Z / X1 / Y1 (Scope Z continuous by LT/RT, step by LB/RB)",
    "当前选中轴在左侧设备图横幅显示（含步长与坐标） · Space 键也是急停":
        "Selected axis shown in stage banner (with step & coord) · Space key is also e-stop",
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

TRANSLATIONS.update({
    # rd105_controller — serial / comm errors (user-facing last_error)
    "串口 {port} 打开失败": "Serial port {port} failed to open",
    "连接失败: {exc}": "Connection failed: {exc}",
    "断开连接时关闭输出失败": "Disconnect: stop output failed",
    "断开连接时关闭输出失败: {exc}": "Disconnect: stop output failed: {exc}",
    "串口未连接": "Serial port not connected",
    "RD105 未返回数据": "RD105 returned no data",
    "第 {attempt} 次通信失败: {last_error}": "Comm attempt {attempt} failed: {last_error}",
    "串口通信异常: {exc}": "Serial comm exception: {exc}",
    "通信过程中发生异常: {exc}": "Exception during comm: {exc}",
    "RD105 回复格式异常: {response}": "RD105 reply format error: {response}",
    "RD105 写入 {name} 失败，回复: {response}": "RD105 write {name} failed, reply: {response}",
    "无": "N/A",
    "RD105 写入 {name} 后读回不一致: 写入 {expected_value}，读回 {actual_value}":
        "RD105 write {name} readback mismatch: wrote {expected_value}, read back {actual_value}",
    # rd105_controller — temperature guard / heating errors
    "超温/温度读数保护中：当前温度高于 {max_temp:.1f} °C 或读数无效，等待恢复后才能继续加热":
        "Over-temp / sensor-fault active: current temp above {max_temp:.1f} °C or reading invalid; heating blocked until resolved",
    "RD105 实时温度缺失: {response}": "RD105 live temperature missing: {response}",
    "RD105 实时温度读数无效": "RD105 live temperature reading invalid",
    "读取目标温度时出错: {exc}": "Error reading target temperature: {exc}",
    "RD105 输出使能读数格式异常: {raw_value}": "RD105 output-enable reading format error: {raw_value}",
    "读取输出使能时出错: {exc}": "Error reading output enable: {exc}",
    "RD105 最大输出占空比读数格式异常: {raw_value}": "RD105 max-duty reading format error: {raw_value}",
    "读取最大输出占空比时出错: {exc}": "Error reading max output duty: {exc}",
    "RD105 错误码读数格式异常: {raw_value}": "RD105 error-code reading format error: {raw_value}",
    "读取错误码时出错: {exc}": "Error reading error code: {exc}",
    "写入 PID 参数时出错: {exc}": "Error writing PID parameters: {exc}",
    "最大输出占空比必须在 {min_duty}~{max_duty} % 之间":
        "Max output duty must be between {min_duty}~{max_duty} %",
    "目标温度 {target_temp:.2f} °C 没有匹配的 PID 参数":
        "No matching PID profile for target {target_temp:.2f} °C",
    "软件斜坡 TG 写入失败": "Software ramp TG write failed",
    "读取实时数据时出错: {exc}": "Error reading live data: {exc}",
    "目标温度必须在 {min_temp:.1f}~{max_temp:.1f} °C 之间":
        "Target temperature must be between {min_temp:.1f}~{max_temp:.1f} °C",
    "控温速率必须大于 0 °C/小时": "Ramp rate must be greater than 0 °C/h",
    "控温速率不能高于 {max_rate:.1f} °C/分钟": "Ramp rate must not exceed {max_rate:.1f} °C/min",
    "快速设温失败: {exc}": "Quick set failed: {exc}",
    "按速率控温失败: {exc}": "Ramp set failed: {exc}",
    "关闭输出失败: {exc}": "Stop output failed: {exc}",
    # rd105_controller — watchdog messages
    "通信丢失或轮询停止——已强制关闭加热输出":
        "Comm lost or polling stopped — heater output force-disabled",
    "通信丢失或轮询停止——已强制关闭加热输出（关闭输出失败，将重试）":
        "Comm lost or polling stopped — heater output force-disable FAILED, will retry",
    "通信丢失或轮询停止——已强制关闭加热输出（关闭输出异常: {exc}，将重试）":
        "Comm lost or polling stopped — heater output force-disable raised {exc}, will retry",
    "看门狗检查异常: {exc}": "Watchdog check exception: {exc}",
    # gas_controller — MotionControllerError user messages
    "拒绝移动：该方向无安全行程 (axis {axis})":
        "Move refused: no safe travel in this direction (axis {axis})",
    "MicroscopeZ 目标脉冲超出 int32 范围，已拒绝":
        "MicroscopeZ target pulse out of int32 range; rejected",
    "MicroscopeZ 目标脉冲 {target} 超出合理范围 (±{bound})，已拒绝":
        "MicroscopeZ target pulse {target} out of sane range (±{bound}); rejected",
})


TRANSLATIONS.update({
    "激光切割": "Laser cutting",
    "预览 ROI": "Preview ROI",
    "固定轴位置缺失或已改变，请重新标定。": "Fixed-axis positions are missing or changed; recalibrate.",
    "运动控制未打开，无法确认插补调试。": "Open motion control before confirming commissioning.",
    "控制器状态已改变，请重新确认插补调试。": "Controller state changed; confirm commissioning again.",
    "等待切割任务停止；设备状态尚未确认": "Waiting for cutting to stop; device state unconfirmed",
    "其他设备操作尚未完成": "Another device operation is still running",
    "请先停用手柄控制": "Disarm the gamepad first",
    "激光切割工作台（仅暗运行）": "Laser cutting — dry run",
    "1 · 共享相机与定位照片": "1 · Camera and reference photo",
    "2 · 图像选择": "2 · Image selection",
    "3 · 像素与位移标定": "3 · Image-to-stage calibration",
    "4 · 路径与回差": "4 · Path and backlash",
    "5 · 仅暗运行": "5 · Dry run only",
    "X3 实测回差 (µm)": "X3 measured backlash (µm)",
    "Y3 实测回差 (µm)": "Y3 measured backlash (µm)",
    "X3 补偿比例 (%)": "X3 compensation (%)",
    "Y3 补偿比例 (%)": "Y3 compensation (%)",
    "仅预览 ROI（保留原图坐标）": "Preview ROI (keep full-frame coordinates)",
    "保存原图、ROI 和元数据": "Save full frame, ROI and metadata",
    "停止尚未确认，窗口保留并等待任务退出。": "Stop unconfirmed. Keeping this window until the task exits.",
    "允许最大误差 (µm)": "Maximum allowed error (µm)",
    "其他操作尚未结束，无法开始。": "Another operation is still running.",
    "加载匹配标定": "Load matching calibration",
    "取消 / 停止": "Cancel / Stop",
    "图像保存失败。": "Failed to save image.",
    "图像及元数据已保存：{path}": "Images and metadata saved: {path}",
    "在至少三个不共线的 X3/Y3 位置拍照，每次点击同一个固定特征；另行设置激光参考点。": "Capture at least three noncollinear X3/Y3 positions, clicking the same fixed landmark each time. Set the laser reference separately.",
    "定位照片已就绪；请选择 ROI、特征或线段。": "Reference photo ready. Select an ROI, landmark or line.",
    "定位照片或路径已改变，请重新预览并确认。": "Reference or path changed. Preview and confirm again.",
    "实时图像仅用于查看；选择前请拍摄定位照片。": "Live view only. Capture a reference photo before selecting.",
    "已在关激光条件下验证 X3/Y3 插补、方向、单位、限位和停止": "With the laser off, I have verified X3/Y3 interpolation, direction, units, limits and stopping on the actual board.",
    "开始预览": "Start preview",
    "我已完成上述检查": "I have completed these checks",
    "打开共享相机并开始预览，然后拍摄新的定位照片。": "Open the shared camera and start preview, then capture a fresh reference photo.",
    "打开相机窗口": "Open camera window",
    "拍摄定位照片": "Capture reference photo",
    "拖动选择 ROI 或线段；特征和激光参考点分别点击。": "Drag to select an ROI or line. Click to select a landmark or laser reference.",
    "拟合并保存标定": "Fit and save calibration",
    "插补确认后请重新拍摄定位照片。": "Capture a new reference after confirming interpolation.",
    "操作失败：{error}": "Operation failed: {error}",
    "操作已取消。": "Operation cancelled.",
    "暗运行": "Dry run (laser off)",
    "暗运行完成；请重新拍摄定位照片。": "Dry run complete. Capture a new reference photo.",
    "暗运行进度：{step}/{total}": "Dry run: {step}/{total}",
    "本次运行前，我已将激光电源关闭或物理遮挡光路。软件不验证实际出光状态。": "For this run, I have powered off the laser or physically blocked its beam. Software does not verify optical output.",
    "本次连接已确认插补调试；复位、重连或轴配置变化后需重新确认。": "Interpolation confirmed for this connection. Reset, reconnect or axis configuration changes require renewed confirmation.",
    "查看": "View",
    "标定已就绪，最大残差 {error:.4f} µm。": "Calibration ready. Maximum residual: {error:.4f} µm.",
    "标定的轴配置不匹配，请重新标定。": "Axis configuration differs from calibration. Calibrate again.",
    "正在取消，等待任务退出…": "Cancelling; waiting for the task to exit…",
    "正在拍摄新的定位照片…": "Capturing a fresh reference photo…",
    "正在暗运行；激光必须保持关闭或遮挡。": "Dry run in progress. Keep the laser off or blocked.",
    "每次暗运行前确认": "Confirm before each dry run",
    "生成路径预览": "Preview path",
    "相机或物镜已改变，请重新拍摄定位照片。": "Camera or objective changed. Capture a new reference photo.",
    "确认": "Confirm",
    "确认插补调试完成": "Confirm interpolation commissioning",
    "确认本次连接的插补调试": "Confirm interpolation for this connection",
    "绘制切割线": "Draw cutting line",
    "虚线：暗移动与回差预加载；实线：切割路径意图。此批次不控制激光出光。": "Dashed: dark travel and backlash take-up. Solid: intended cut. This mode does not control laser emission.",
    "记录同一特征": "Record the same landmark",
    "设置激光参考点": "Set laser reference",
    "请先完成匹配标定。": "Complete a matching calibration first.",
    "请先选择 ROI 和切割线。": "Select an ROI and cutting line first.",
    "路径速度 (µm/s)": "Path speed (µm/s)",
    "路径预览已生成；运行前仍需检查定位照片与运动状态。": "Path preview ready. Reference and motion state will be checked before execution.",
    "运动状态已改变，请重新拍摄定位照片。": "Motion state changed. Capture a new reference photo.",
    "运行结束后需重新拍摄定位照片。": "Capture a new reference photo after a run.",
    "返回实时图像": "Return to live view",
    "适应窗口": "Fit to window",
    "选择 ROI": "Select ROI",
    "重新记录标定": "Reset calibration samples",
})


class Translator:
    def __init__(
        self,
        lang: str = "en",
        table: dict[str, str] | None = None,
        ctx_table: dict[tuple[str, str], str] | None = None,
    ):
        self._lang = lang if lang in ("en", "zh") else "en"
        self._table = TRANSLATIONS if table is None else table
        self._ctx_table = CONTEXT_TRANSLATIONS if ctx_table is None else ctx_table
        self._observers: list[Callable[[], None]] = []
        self._saver: Callable[[str], None] | None = None

    @property
    def lang(self) -> str:
        return self._lang

    def tr(self, zh: str, context: str | None = None) -> str:
        if self._lang == "zh":
            return zh
        if context is not None:
            ctx = self._ctx_table.get((context, zh))
            if ctx is not None:
                return ctx
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
        # Isolate observers: one failing observer (e.g. a panel whose closure hits a
        # deleted widget) must NOT abort the loop and leave later panels stuck in the
        # old language. Log and continue so EVERY panel retranslates.
        for cb in list(self._observers):
            try:
                cb()
            except Exception:
                _log.exception("language-change observer failed; continuing")

    def add_observer(self, cb: Callable[[], None]) -> None:
        self._observers.append(cb)

    def clear_observers(self) -> None:
        self._observers.clear()

    def set_saver(self, saver: Callable[[str], None] | None) -> None:
        self._saver = saver


translator = Translator()


def tr(zh: str, context: str | None = None) -> str:
    return translator.tr(zh, context)


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
        self._messages: dict[object, tuple[str, dict]] = {}
        translator.add_observer(self.retranslate)

    def _add(self, fn: Callable[[], None]) -> None:
        fn()
        self._apply.append(fn)

    def text(self, widget, zh: str, context: str | None = None) -> None:
        self._add(lambda: widget.setText(self._translator.tr(zh, context)))

    def message(self, widget, zh: str, **values) -> str:
        """Keep the latest status template so language switches preserve its data."""
        self._messages[widget] = (zh, values)
        text = self._translator.tr(zh).format(**{
            key: value() if callable(value) else value for key, value in values.items()
        })
        widget.setText(text)
        return text

    def title(self, widget, zh: str, context: str | None = None) -> None:
        setter = getattr(widget, "setTitle", None) or widget.setWindowTitle
        self._add(lambda: setter(self._translator.tr(zh, context)))

    def tooltip(self, widget, zh: str, context: str | None = None) -> None:
        self._add(lambda: widget.setToolTip(self._translator.tr(zh, context)))

    def placeholder(self, widget, zh: str, context: str | None = None) -> None:
        self._add(lambda: widget.setPlaceholderText(self._translator.tr(zh, context)))

    def items(self, combo, zh_list: list[str], context: str | None = None) -> None:
        def apply():
            for i, zh in enumerate(zh_list):
                combo.setItemText(i, self._translator.tr(zh, context))
        self._add(apply)

    def custom(self, apply_fn: Callable[[], None]) -> None:
        self._add(apply_fn)

    def retranslate(self) -> None:
        for widget, (zh, values) in list(self._messages.items()):
            try:
                self.message(widget, zh, **values)
            except RuntimeError:
                # Dialogs can be destroyed while their owning panel stays alive.
                self._messages.pop(widget, None)
        # Isolate each closure: one failing widget (e.g. destroyed) must not stop the
        # rest of this panel's text from updating on a language switch.
        for fn in self._apply:
            try:
                fn()
            except Exception:
                _log.exception("retranslate closure failed; continuing")
