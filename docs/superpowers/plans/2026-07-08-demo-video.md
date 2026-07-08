# 转移台 Demo 视频制作计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 产出约 75 秒、1920×1080@30fps 的中文 demo 视频 `demo/transfer_station_demo.mp4`，风格对标 `E:\Measurix_Demo\Measurix_demo_1min.mp4`。

**Architecture:** 三条流水线汇合——①PIL 渲染卡片段（标题/总结为单帧+ffmpeg fade，LLM 对话为逐帧动画）；②GUI 预览模式 + Win32 真实鼠标自动化 + ffmpeg gdigrab 分段录屏；③ASS 字幕烧录 + concat 合成。所有中间产物在 `demo/clips/`，成品一个文件。

**Tech Stack:** Python 3.13（`py -3.13`，含 PIL/Pillow——若未装则 `py -3.13 -m pip install pillow`）、ffmpeg（已在 PATH：winget Gyan.FFmpeg）、ctypes Win32 API（无新依赖）。

## Global Constraints

- 一律 `py -3.13`（默认 python 是 3.14）。
- 视频参数：1920×1080、30 fps、H.264 yuv420p、约 75 s。
- 界面语言中文：录屏前确保 `runtime/ui_settings.json` = `{"language": "zh"}`。
- **安全红线：自动化绝不点击「打开运动控制/关闭运动控制/复位」按钮**；segments.json 中所有点击坐标必须通过禁区校验（Task 4 的 forbidden_rects），落禁区即拒绝运行。录制前截图校验界面，不符即中止。
- 卡片深藏青底 `#0D1B2A`，主字白、副字浅蓝 `#7FB3E8`、弱字 `#8899AA`；中文字体微软雅黑 `C:/Windows/Fonts/msyh.ttc`（粗体 `msyhbd.ttc`）。
- 字幕条：底部半透明黑条白字（ASS BorderStyle=3）。
- 版本号展示 v0.0.5；GitHub 地址 `github.com/wnz123123-bit/Automatic-transfer-station`。
- `demo/` 暂不提交 git（成片验收后由用户定）；本计划各任务的"提交"仅指必要时提交脚本，默认全部不提交。

## 目录结构

```
demo/
├── assets/        # flake 图片、卡片 PNG、LLM 帧序列
├── scripts/       # render_cards.py / render_llm_scene.py / record_gui.py / segments.json / subs/ / assemble.py
├── clips/         # seg1..seg8 各段 mp4
└── transfer_station_demo.mp4
```

---

### Task 1: 二维材料图片素材

**Files:**
- Create: `demo/assets/flake_1.jpg`（必需，LLM 段用）；`flake_2.jpg`（备用/相机段 fallback）

**Interfaces:**
- Produces: `demo/assets/flake_1.jpg` — 二维材料（MoS₂/石墨烯）光学显微照片，短边 ≥600px，衬度清晰。

- [ ] **Step 1: 检索下载**

用 WebSearch/WebFetch 检索 "MoS2 flake optical microscope image" / "graphene flake optical micrograph"（学术论文配图、大学课题组网页均可，内部演示无版权约束），下载 2 张到 `demo/assets/`。

- [ ] **Step 2: 视觉核验**

Read 查看两张图：确认是显微镜下的薄片照片（有衬度分层/基底背景），分辨率达标。不合格则换图重下。

---

### Task 2: 标题卡与总结卡（seg1 / seg8）

**Files:**
- Create: `demo/scripts/render_cards.py`
- Create: `demo/assets/card_title.png`, `demo/assets/card_summary.png`（运行产物）
- Create: `demo/clips/seg1_title.mp4`, `demo/clips/seg8_summary.mp4`（运行产物）

**Interfaces:**
- Produces: 两个 1920×1080@30fps、yuv420p 的 mp4；seg1 6s、seg8 9s，各自带 0.5s 淡入 / 0.5s 淡出（黑场淡入淡出即可衔接相邻段）。

- [ ] **Step 1: 写渲染脚本**

```python
# demo/scripts/render_cards.py
from PIL import Image, ImageDraw, ImageFont
import subprocess
from pathlib import Path

W, H = 1920, 1080
BG = (13, 27, 42)
WHITE = (240, 244, 248)
BLUE = (127, 179, 232)
GRAY = (136, 153, 170)
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
ROOT = Path(__file__).resolve().parents[1]


def _center(draw, text, y, font, fill):
    w = draw.textlength(text, font=font)
    draw.text(((W - w) / 2, y), text, font=font, fill=fill)


def title_card() -> Path:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _center(d, "Automatic Transfer Station", 380, ImageFont.truetype(FONT_B, 100), WHITE)
    _center(d, "二维材料自动转移平台", 560, ImageFont.truetype(FONT, 56), BLUE)
    _center(d, "v0.0.5 · WangLab", 700, ImageFont.truetype(FONT, 34), GRAY)
    out = ROOT / "assets/card_title.png"
    img.save(out)
    return out


def summary_card() -> Path:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _center(d, "Automatic Transfer Station", 150, ImageFont.truetype(FONT_B, 72), WHITE)
    rows = [
        ("九轴自动化", "吸附台 · 载玻片 · 显微镜 · 旋转轴，一套 GUI 全量控制"),
        ("安全体系", "软限位 · 干涉保护 · 看门狗 · 急停 · 高温联锁"),
        ("控温 + 显微测量", "RD105 精准控温 · Basler 相机标定测量"),
        ("发展方向", "LLM 驱动：自然语言 → 规划 → 视觉反馈 → 自主转移"),
    ]
    fk = ImageFont.truetype(FONT_B, 42)
    fv = ImageFont.truetype(FONT, 36)
    y = 360
    for k, v in rows:
        kw = d.textlength(k, font=fk)
        d.text((640 - kw, y), k, font=fk, fill=BLUE)      # 键右对齐到 x=640
        d.text((700, y + 4), v, font=fv, fill=WHITE)       # 值左对齐 x=700
        y += 110
    _center(d, "github.com/wnz123123-bit/Automatic-transfer-station", 880,
            ImageFont.truetype(FONT, 30), GRAY)
    out = ROOT / "assets/card_summary.png"
    img.save(out)
    return out


def encode(png: Path, seconds: float, out_name: str):
    out = ROOT / "clips" / out_name
    fade_out = seconds - 0.5
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-t", str(seconds), "-i", str(png),
        "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={fade_out}:d=0.5,format=yuv420p",
        "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(out),
    ], check=True)


if __name__ == "__main__":
    (ROOT / "clips").mkdir(exist_ok=True)
    encode(title_card(), 6.0, "seg1_title.mp4")
    encode(summary_card(), 9.0, "seg8_summary.mp4")
    print("done")
```

- [ ] **Step 2: 运行**

`cd "E:\Automatic transfer station" && py -3.13 demo/scripts/render_cards.py` → 输出 done，两个 mp4 生成。

- [ ] **Step 3: 视觉核验**

`ffmpeg -y -ss 3 -i demo/clips/seg1_title.mp4 -frames:v 1 <scratchpad>/t1.png`（seg8 同理取 4s 处），Read 查看：排版居中、无字体缺字、颜色符合规范。`ffprobe` 确认 1920×1080、30fps、时长 6s/9s。不满意就改布局参数重跑。

---

### Task 3: LLM 对话概念段（seg7）

**Files:**
- Create: `demo/scripts/render_llm_scene.py`
- Create: `demo/assets/llm_frames/f_0000.png ...`（480 帧，运行产物）
- Create: `demo/clips/seg7_llm.mp4`（16s，运行产物）

**Interfaces:**
- Consumes: `demo/assets/flake_1.jpg`（Task 1）。
- Produces: `demo/clips/seg7_llm.mp4`，1920×1080@30fps，16s，自带首尾 0.5s 黑场淡入淡出。

- [ ] **Step 1: 写动画脚本**

```python
# demo/scripts/render_llm_scene.py
from PIL import Image, ImageDraw, ImageFont
import subprocess
from pathlib import Path

W, H, FPS, SECONDS = 1920, 1080, 30, 16
BG = (13, 27, 42)
WHITE = (240, 244, 248)
BLUE = (127, 179, 232)
GRAY = (136, 153, 170)
USER_BG = (46, 90, 143)
AI_BG = (27, 43, 61)
AI_BORDER = (58, 82, 110)
FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
ROOT = Path(__file__).resolve().parents[1]

USER_TEXT = "帮我把这片 MoS₂ 转移到目标基底。"
AI_LINES = [
    "计划：",
    "① 扫描寻片 → 已定位目标薄片（见图）",
    "② 移动吸附台，对准目标位点",
    "③ 缓降接触 · 控温辅助贴合",
    "④ 缓升分离 → 转移完成",
    "安全预检通过，开始执行。",
]
FLOW = "自然语言  →  转移规划  →  视觉反馈  →  自主执行"


def rounded(d, box, radius, fill, outline=None):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)


def build_frame(t: float, flake: Image.Image) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_head = ImageFont.truetype(FONT_B, 54)
    f_sub = ImageFont.truetype(FONT, 28)
    f_body = ImageFont.truetype(FONT, 34)
    f_tag = ImageFont.truetype(FONT, 24)

    # 标题（0.3s 后常显）
    if t >= 0.3:
        tw = d.textlength("发展方向 · LLM 驱动的 AI 辅助自动转移", font=f_head)
        d.text(((W - tw) / 2, 70), "发展方向 · LLM 驱动的 AI 辅助自动转移", font=f_head, fill=WHITE)
        sw = d.textlength("概念演示 · 正在开发 —— 示例：MoS₂ 薄片转移", font=f_sub)
        d.text(((W - sw) / 2, 155), "概念演示 · 正在开发 —— 示例：MoS₂ 薄片转移", font=f_sub, fill=GRAY)

    # 用户气泡（右侧，1.2s 出现）
    if t >= 1.2:
        d.text((W - 260, 250), "操作者", font=f_tag, fill=GRAY)
        uw = d.textlength(USER_TEXT, font=f_body)
        box = (W - 260 - uw - 60, 290, W - 200, 360)
        rounded(d, box, 16, USER_BG)
        d.text((box[0] + 30, 305), USER_TEXT, font=f_body, fill=WHITE)

    # AI 气泡（左侧，3.0s 起打字机逐行）
    if t >= 3.0:
        d.text((200, 420), "Transfer Station · LLM", font=f_tag, fill=GRAY)
        # 打字机：每行 1.1s
        chars_total = sum(len(l) for l in AI_LINES)
        progress = min(1.0, (t - 3.2) / 6.6)
        chars_show = int(chars_total * max(0.0, progress))
        shown, budget = [], chars_show
        for line in AI_LINES:
            if budget <= 0:
                break
            shown.append(line[:budget])
            budget -= len(line)
        box_h = 60 + 52 * max(1, len(shown))
        rounded(d, (200, 455, 1080, 455 + box_h), 16, AI_BG, AI_BORDER)
        y = 480
        for line in shown:
            d.text((240, y), line, font=f_body, fill=WHITE)
            y += 52

    # 薄片图（6.0s 淡入，右下，作为“视觉反馈”）
    if t >= 6.0:
        alpha = min(1.0, (t - 6.0) / 0.8)
        fw = 460
        fh = int(flake.height * fw / flake.width)
        fimg = flake.resize((fw, fh))
        overlay = Image.new("RGB", (fw + 8, fh + 8), AI_BORDER)
        overlay.paste(fimg, (4, 4))
        if alpha < 1.0:
            base = img.crop((1200, 455, 1200 + fw + 8, 455 + fh + 8))
            overlay = Image.blend(base, overlay, alpha)
        img.paste(overlay, (1200, 455))
        d = ImageDraw.Draw(img)
        d.text((1200, 455 + fh + 20), "视觉反馈：已定位目标薄片", font=f_tag, fill=BLUE)

    # 底部流程行（11.5s 出现）
    if t >= 11.5:
        fw2 = d.textlength(FLOW, font=f_body)
        d.text(((W - fw2) / 2, 980), FLOW, font=f_body, fill=BLUE)
    return img


def main():
    flake = Image.open(ROOT / "assets/flake_1.jpg").convert("RGB")
    frames_dir = ROOT / "assets/llm_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    n = FPS * SECONDS
    for i in range(n):
        build_frame(i / FPS, flake).save(frames_dir / f"f_{i:04d}.png")
    out = ROOT / "clips/seg7_llm.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-framerate", str(FPS), "-i", str(frames_dir / "f_%04d.png"),
        "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={SECONDS-0.5}:d=0.5,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", str(out),
    ], check=True)
    print("done")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行** `py -3.13 demo/scripts/render_llm_scene.py` → done。

- [ ] **Step 3: 视觉核验**

抽 t=2s / 5s / 8s / 13s 四帧 Read 查看：气泡布局不重叠、打字机节奏合理、薄片图清晰入镜、流程行出现。布局问题改常量重跑（帧渲染约 1-2 分钟，可迭代）。

---

### Task 4: GUI 自动化录屏（seg2–seg6）

**Files:**
- Create: `demo/scripts/record_gui.py`
- Create: `demo/scripts/segments.json`（坐标标定数据，实施时填写）
- Create: `demo/clips/seg2_overview.mp4 ... seg6_lang.mp4`（运行产物）

**Interfaces:**
- Consumes: 应用 `run_transfer_station.py`（预览模式）。
- Produces: seg2(10s)/seg3(10s)/seg4(8s)/seg5(10s)/seg6(6s) 五段原始录屏（无字幕），1920×1080@30fps。

**实施流程（标定是核心）：**

- [ ] **Step 1: 环境预检**

`py -3.13 -c "import ctypes; u=ctypes.windll.user32; print(u.GetSystemMetrics(0), u.GetSystemMetrics(1))"` 记录屏幕分辨率。写 `runtime/ui_settings.json` = `{"language": "zh"}`。确认无其它程序占用屏幕角落。

- [ ] **Step 2: 写录制框架**

```python
# demo/scripts/record_gui.py
"""预览模式 GUI 自动化录屏。用法：
py -3.13 demo/scripts/record_gui.py calibrate   # 启动+摆位+截图（供人工标定坐标）
py -3.13 demo/scripts/record_gui.py record seg2 # 按 segments.json 录制指定段
"""
import ctypes, json, subprocess, sys, time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT.parent  # E:\Automatic transfer station
user32 = ctypes.windll.user32
WIN_TITLE = "EthBoardTest Python"
# 窗口摆位：左上角 (0,0)，尺寸在标定时按屏幕定（写入 segments.json 的 window 字段）

SEGJSON = ROOT / "scripts/segments.json"


def find_window(timeout=20):
    for _ in range(timeout * 10):
        hwnd = user32.FindWindowW(None, WIN_TITLE)
        if hwnd:
            return hwnd
        time.sleep(0.1)
    raise RuntimeError("window not found")


def launch():
    proc = subprocess.Popen([sys.executable, str(APP / "run_transfer_station.py")], cwd=str(APP))
    hwnd = find_window()
    cfg = json.loads(SEGJSON.read_text(encoding="utf-8"))
    wx, wy, ww, wh = cfg["window"]
    user32.MoveWindow(hwnd, wx, wy, ww, wh, True)
    user32.SetForegroundWindow(hwnd)
    time.sleep(2.0)
    return proc, hwnd


def screenshot(path):
    subprocess.run(["ffmpeg", "-y", "-f", "gdigrab", "-frames:v", "1",
                    "-i", "desktop", str(path)], check=True, capture_output=True)


def move_smooth(x1, y1, dur):
    pt = wintypes.POINT(); user32.GetCursorPos(ctypes.byref(pt))
    steps = max(2, int(dur * 60))
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)  # smoothstep
        user32.SetCursorPos(int(pt.x + (x1 - pt.x) * e), int(pt.y + (y1 - pt.y) * e))
        time.sleep(dur / steps)


def click():
    user32.mouse_event(0x0002, 0, 0, 0, 0); time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def in_rect(x, y, r):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def validate(seg, forbidden):
    for a in seg["actions"]:
        if a["type"] in ("move", "click_at"):
            for r in forbidden:
                if in_rect(a["x"], a["y"], r):
                    raise RuntimeError(f"SAFETY: action {a} in forbidden rect {r}")


def run_segment(name):
    cfg = json.loads(SEGJSON.read_text(encoding="utf-8"))
    seg = cfg["segments"][name]
    validate(seg, cfg["forbidden_rects"])
    proc, hwnd = launch()
    try:
        wx, wy, ww, wh = cfg["window"]
        rec = subprocess.Popen([
            "ffmpeg", "-y", "-f", "gdigrab", "-framerate", "30",
            "-offset_x", str(wx), "-offset_y", str(wy), "-video_size", f"{ww}x{wh}",
            "-i", "desktop", "-t", str(seg["seconds"]),
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                   "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0D1B2A,format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            str(ROOT / "clips" / seg["out"]),
        ])
        t0 = time.time()
        for a in seg["actions"]:
            while time.time() - t0 < a["t"]:
                time.sleep(0.01)
            if a["type"] == "move":
                move_smooth(a["x"], a["y"], a.get("dur", 0.6))
            elif a["type"] == "click_at":
                move_smooth(a["x"], a["y"], a.get("dur", 0.6)); time.sleep(0.15); click()
            elif a["type"] == "key_space":
                pass  # 不使用键盘急停演示，预留
        rec.wait(timeout=seg["seconds"] + 30)
    finally:
        proc.kill()


if __name__ == "__main__":
    if sys.argv[1] == "calibrate":
        cfg = json.loads(SEGJSON.read_text(encoding="utf-8"))
        proc, hwnd = launch()
        screenshot(ROOT / "assets/calibration.png")
        print("screenshot saved; window stays 20s for inspection")
        time.sleep(20)
        proc.kill()
    else:
        run_segment(sys.argv[2])
```

`segments.json` 初始骨架（坐标 Step 3 标定后填写）：

```json
{
  "window": [0, 0, 1720, 968],
  "forbidden_rects": [[0, 0, 0, 0]],
  "segments": {
    "seg2": {"seconds": 10, "out": "seg2_overview.mp4", "actions": []},
    "seg3": {"seconds": 10, "out": "seg3_axes.mp4", "actions": []},
    "seg4": {"seconds": 8,  "out": "seg4_gamepad.mp4", "actions": []},
    "seg5": {"seconds": 10, "out": "seg5_temp_camera.mp4", "actions": []},
    "seg6": {"seconds": 6,  "out": "seg6_lang.mp4", "actions": []}
  }
}
```

- [ ] **Step 3: 标定（人工闭环）**

`py -3.13 demo/scripts/record_gui.py calibrate` → Read 查看 `demo/assets/calibration.png`，从截图读出并填入 segments.json：
1. `forbidden_rects`：「打开运动控制/关闭运动控制/复位/停止移动」按钮行的包围盒（含 30px 余量）。
2. 各段动作坐标：seg2 鼠标沿设备图缓移（3 个 move 点）；seg3 依次 click_at 两个轴热点（如 Y2、R）；seg4 click_at 手柄「?」→ 停留看图 →（帮助对话框的关闭按钮坐标也标定）；seg5 move 到温度面板区域缓移（若相机窗口可用再加：点相机窗口任务栏/按钮的动作，先按 spec 规则试开相机窗口截图判断）；seg6 click_at 右下角语言按钮 → 等 2s → 再 click_at 切回。
3. 相机窗口判定：手动 `py -3.13 -c` 起一次 app 截图看相机窗口是否醒目报错，按 spec 规则决定 seg5 是否包含相机窗口镜头，并把结论记录在 segments.json 的注释字段 `"_camera_decision"`。

- [ ] **Step 4: 试录一段并核验**

`py -3.13 demo/scripts/record_gui.py record seg2` → 抽 3 帧 Read 查看：光标可见、界面中文、无报错弹窗、无禁区点击。逐段录完 seg2–seg6，每段核验。

**录制期间提醒用户勿动鼠标键盘（每次录制前在回复里声明）。**

---

### Task 5: 字幕烧录与成片合成

**Files:**
- Create: `demo/scripts/assemble.py`
- Create: `demo/scripts/subs/seg2.ass ... seg6.ass`（5 个字幕文件）
- Create: `demo/transfer_station_demo.mp4`（成品）

**Interfaces:**
- Consumes: `demo/clips/seg{1..8}*.mp4`（前序任务全部产物）。
- Produces: 成品 mp4。

- [ ] **Step 1: 写字幕与合成脚本**

字幕模板（每段一个 .ass，Text 各不同，Dialogue 时间覆盖整段减首尾 0.5s）：

```
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Microsoft YaHei,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,3,8,0,2,40,40,36,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:09.50,Cap,,0,0,0,,九轴电动平台：吸附台 · 载玻片 · 显微镜 · 旋转轴一体控制
```

五段字幕文案（照 spec 分镜表）：
- seg2: `九轴电动平台：吸附台 · 载玻片 · 显微镜 · 旋转轴一体控制`
- seg3: `实时坐标 · 软限位与干涉保护 · 一键急停`
- seg4: `Logitech F710 双摇杆操控，失焦/断连自动安全停止`
- seg5: `RD105 精准控温 + Basler 显微相机在线测量`
- seg6: `中英双语界面 · 即时切换`

```python
# demo/scripts/assemble.py
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIPS = ROOT / "clips"
SUBS = ROOT / "scripts/subs"
ORDER = [
    ("seg1_title.mp4", None),
    ("seg2_overview.mp4", "seg2.ass"),
    ("seg3_axes.mp4", "seg3.ass"),
    ("seg4_gamepad.mp4", "seg4.ass"),
    ("seg5_temp_camera.mp4", "seg5.ass"),
    ("seg6_lang.mp4", "seg6.ass"),
    ("seg7_llm.mp4", None),
    ("seg8_summary.mp4", None),
]


def burn(clip, ass):
    out = CLIPS / f"sub_{clip}"
    ass_path = str(SUBS / ass).replace("\\", "/").replace(":", "\\:")
    subprocess.run(["ffmpeg", "-y", "-i", str(CLIPS / clip),
                    "-vf", f"ass='{ass_path}'",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    str(out)], check=True)
    return out


def main():
    finals = []
    for clip, ass in ORDER:
        finals.append(burn(clip, ass) if ass else CLIPS / clip)
    lst = CLIPS / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in finals), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(ROOT / "transfer_station_demo.mp4")], check=True)
    print("done")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行合成** `py -3.13 demo/scripts/assemble.py` → done。

- [ ] **Step 3: 核验** `ffprobe` 时长≈75s、1920×1080；抽字幕段各 1 帧确认字幕条样式统一、文案无错字。

---

### Task 6: 终检验收

- [ ] **Step 1: 全片抽帧巡检**

每 5s 抽 1 帧（约 15 帧）逐一 Read 查看，对照 spec 验收标准：无「打开运动控制」被点击痕迹、无报错弹窗、字幕与画面对齐、卡片段与 Measurix 观感一致。

- [ ] **Step 2: 对照 Measurix 成片**

抽 Measurix 对应位置帧并排对比风格（标题卡/字幕条/LLM 段/总结卡）。

- [ ] **Step 3: 交付用户审片**

告知成品路径 `E:\Automatic transfer station\demo\transfer_station_demo.mp4`，请用户播放审片；按反馈迭代（改文案→只重跑受影响段+合成）。

---

## Self-Review

- Spec 覆盖：8 段分镜 ↔ Task 2(1,8)/3(7)/4(2-6)/5(合成)；安全红线 ↔ Task 4 forbidden_rects+校验；图片素材 ↔ Task 1；相机取舍规则 ↔ Task 4 Step 3.3。无缺口。
- 占位符：segments.json 坐标为**标定数据**（实施时人工闭环填写），非代码占位；其余代码完整。
- 一致性：clips 文件名在 Task 4 segments.json 与 Task 5 ORDER 一致；时长 6+10+10+8+10+6+16+9=75s ✓。
