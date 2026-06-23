"""F710 gamepad button-mapping help: a self-contained SVG schematic plus a
renderer to a QPixmap.

The previous help was a ``QMessageBox`` of plain text; under the app's light
stylesheet its label inherited a near-white text colour and looked empty. An
SVG carries its own colours and is immune to the Qt stylesheet, so it always
renders legibly. The drawing is a stylised Logitech F710 (XInput) modelled on
the real layout -- D-pad upper-left, dual sticks, BACK/logo/START/MODE in the
centre, the Y/A/X/B diamond on the right, and the LB/RB/LT/RT shoulder row --
with leader-line callouts giving each control's name and function, the way
in-game control diagrams do.

``build_f710_help_svg()`` is pure (no Qt) so it can be unit-tested directly.
``render_help_pixmap()`` rasterises it via ``QSvgRenderer`` for display.
"""

from __future__ import annotations

# Face-button brand colours (Y darkened from bright yellow so it stays legible
# on white). Shared between the drawn buttons and the coloured name chips.
_COL_Y = "#b8860b"
_COL_A = "#2faf4e"
_COL_X = "#2f7fd6"
_COL_B = "#d83b3b"

_INK = "#1f2733"        # primary label text
_SUB = "#5b6776"        # secondary/description text
_LINE = "#8a93a0"       # leader lines
_EDGE = "#9aa4b2"       # control outlines
_BODY = "#eef2f6"       # controller body fill
_BODY2 = "#e6ebf1"      # grips
_SHOULDER = "#cdd5df"   # bumpers
_TRIGGER = "#b6bfca"    # triggers
_DARK = "#59636f"       # d-pad / stick caps

_FONT = "'Microsoft YaHei','Segoe UI','PingFang SC',sans-serif"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text(x, y, s, *, size=15, weight="400", anchor="start", fill=_INK):
    return (
        f'<text x="{x}" y="{y}" font-family="{_FONT}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}" fill="{fill}">{_esc(s)}</text>'
    )


def _leader(x1, y1, x2, y2):
    # Thin connector from a label to the control, with a small dot at the control.
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{_LINE}" stroke-width="1.6"/>'
        f'<circle cx="{x2}" cy="{y2}" r="3" fill="{_LINE}"/>'
    )


def _callout(label_x, label_y, anchor, name, desc, leader_from, leader_to, *, name_fill=_INK):
    """A name line + optional description line(s) with a leader to the control."""
    parts = [_leader(leader_from[0], leader_from[1], leader_to[0], leader_to[1])]
    parts.append(_text(label_x, label_y, name, size=16, weight="700", anchor=anchor, fill=name_fill))
    dy = label_y
    for line in desc:
        dy += 18
        parts.append(_text(label_x, dy, line, size=13, weight="400", anchor=anchor, fill=_SUB))
    return "".join(parts)


def build_f710_help_svg() -> str:
    """Return the standalone F710 button-mapping help as an SVG string."""
    p: list[str] = []
    p.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 600" width="1000" height="600">')

    # Card background (keeps the figure legible regardless of dialog theme).
    p.append('<rect x="0" y="0" width="1000" height="600" rx="14" fill="#ffffff" stroke="#d0d7de" stroke-width="1.5"/>')

    # Header.
    p.append(_text(30, 42, "Logitech F710 手柄按键说明", size=22, weight="700"))
    p.append(_text(30, 66, "MODE 拨到 X · 摇杆推得越多越快（比例速度）", size=14, fill=_SUB))

    # ---- Controller body ----------------------------------------------------
    # Grips drawn first so the body overlaps their tops.
    p.append(f'<ellipse cx="415" cy="378" rx="44" ry="82" transform="rotate(-17 415 378)" '
             f'fill="{_BODY2}" stroke="{_EDGE}" stroke-width="2"/>')
    p.append(f'<ellipse cx="585" cy="378" rx="44" ry="82" transform="rotate(17 585 378)" '
             f'fill="{_BODY2}" stroke="{_EDGE}" stroke-width="2"/>')
    # Shoulder bumpers (LB/RB) and triggers (LT/RT) peeking above the body top.
    p.append(f'<rect x="406" y="189" width="50" height="18" rx="7" fill="{_TRIGGER}" stroke="{_EDGE}" stroke-width="1.5"/>')
    p.append(f'<rect x="544" y="189" width="50" height="18" rx="7" fill="{_TRIGGER}" stroke="{_EDGE}" stroke-width="1.5"/>')
    p.append(f'<rect x="398" y="206" width="66" height="21" rx="9" fill="{_SHOULDER}" stroke="{_EDGE}" stroke-width="1.5"/>')
    p.append(f'<rect x="536" y="206" width="66" height="21" rx="9" fill="{_SHOULDER}" stroke="{_EDGE}" stroke-width="1.5"/>')
    p.append(_text(431, 202, "LT", size=11, weight="700", anchor="middle", fill="#ffffff"))
    p.append(_text(569, 202, "RT", size=11, weight="700", anchor="middle", fill="#ffffff"))
    p.append(_text(431, 221, "LB", size=11, weight="700", anchor="middle", fill="#33404e"))
    p.append(_text(569, 221, "RB", size=11, weight="700", anchor="middle", fill="#33404e"))
    # Main body.
    p.append(f'<rect x="380" y="220" width="240" height="122" rx="48" fill="{_BODY}" stroke="{_EDGE}" stroke-width="2.5"/>')

    # D-pad (upper-left).
    p.append(f'<rect x="430" y="250" width="16" height="44" rx="4" fill="{_DARK}"/>')
    p.append(f'<rect x="416" y="264" width="44" height="16" rx="4" fill="{_DARK}"/>')
    p.append(f'<circle cx="438" cy="272" r="5" fill="#444d58"/>')

    # Analog sticks.
    for sx in (462, 552):
        p.append(f'<circle cx="{sx}" cy="326" r="25" fill="{_SHOULDER}" stroke="{_EDGE}" stroke-width="2"/>')
        p.append(f'<circle cx="{sx}" cy="326" r="15" fill="#39424e"/>')
        p.append(f'<circle cx="{sx - 4}" cy="322" r="4" fill="#5a646f"/>')

    # Face-button diamond (Y top, A bottom, X left, B right).
    for cx, cy, col, lbl in ((566, 244, _COL_Y, "Y"), (566, 288, _COL_A, "A"),
                             (542, 266, _COL_X, "X"), (590, 266, _COL_B, "B")):
        p.append(f'<circle cx="{cx}" cy="{cy}" r="14" fill="{col}"/>')
        p.append(_text(cx, cy + 5, lbl, size=15, weight="700", anchor="middle", fill="#ffffff"))

    # Centre buttons: BACK, logo, START, MODE.
    p.append(f'<circle cx="500" cy="258" r="10" fill="#707a86"/>')
    p.append(f'<rect x="464" y="245" width="20" height="10" rx="4" fill="{_EDGE}"/>')   # BACK
    p.append(f'<rect x="516" y="245" width="20" height="10" rx="4" fill="{_EDGE}"/>')   # START
    p.append(f'<rect x="490" y="281" width="20" height="10" rx="4" fill="{_EDGE}"/>')   # MODE

    # ---- Callouts -----------------------------------------------------------
    # Top shoulders.
    p.append(_callout(360, 138, "end", "LT", ["显微镜Z ↓", "轻按单步 · 长按连续 ≤4mm/s"], (368, 132), (420, 197)))
    p.append(_callout(300, 168, "end", "LB", ["载玻片Z ↓ （轻按单步 · 长按连续）"], (308, 162), (412, 216)))
    p.append(_callout(640, 138, "start", "RT", ["显微镜Z ↑", "轻按单步 · 长按连续 ≤4mm/s"], (632, 132), (580, 197)))
    p.append(_callout(700, 168, "start", "RB", ["载玻片Z ↑ （轻按单步 · 长按连续）"], (692, 162), (588, 216)))

    # Left column.
    p.append(_callout(250, 250, "end", "BACK", ["急停（所有轴立即停止）"], (258, 244), (474, 250)))
    p.append(_callout(250, 322, "end", "十字键",
                      ["↑ / ↓  选中轴 单步 ± （仅单步）", "← / →  步长档 减小 / 增大"],
                      (258, 316), (416, 272)))
    p.append(_callout(250, 410, "end", "左摇杆", ["X3（左右）/ Y3（前后） 连续"], (258, 404), (438, 326)))

    # Right column.
    p.append(_callout(750, 250, "start", "START", ["启用 / 停用手柄"], (742, 244), (526, 250)))
    # Face-button callout with coloured Y/A/X/B chips on the name line.
    p.append(_leader(742, 320, 590, 266))
    face_name = (
        f'<text x="750" y="320" font-family="{_FONT}" font-size="16" font-weight="700" text-anchor="start">'
        f'<tspan fill="{_INK}">面键 </tspan>'
        f'<tspan fill="{_COL_Y}">Y</tspan><tspan fill="{_INK}">/</tspan>'
        f'<tspan fill="{_COL_A}">A</tspan><tspan fill="{_INK}">/</tspan>'
        f'<tspan fill="{_COL_X}">X</tspan><tspan fill="{_INK}">/</tspan>'
        f'<tspan fill="{_COL_B}">B</tspan>'
        f'</text>'
    )
    p.append(face_name)
    p.append(_text(750, 338, "选轴：Y=上  A=下  X=左  B=右", size=13, fill=_SUB))
    p.append(_callout(750, 410, "start", "右摇杆", ["X2（左右）/ Y2（前后） 连续"], (742, 404), (576, 326)))

    # MODE: on the front face, in the centre cluster (just below BACK/logo/START).
    # A short leader keeps the label tied to its real centre position rather than
    # appearing to sit at the front edge of the pad.
    p.append(_leader(500, 291, 500, 348))
    p.append(_text(500, 368, "MODE 拨到 X 模式", size=13, weight="700", anchor="middle"))

    # Footer notes.
    p.append(_text(500, 520, "可选轴：X3 / Y3 / X2 / Y2 / R / 载玻片Z / X1 / Y1（显微镜Z 由扳机 LT/RT 控制）",
                   size=13, anchor="middle", fill=_SUB))
    p.append(_text(500, 542, "当前选中轴在左侧设备图横幅显示（含步长与坐标） · Space 键也是急停",
                   size=13, anchor="middle", fill=_SUB))

    p.append('</svg>')
    return "".join(p)


def render_help_pixmap(scale: float = 2.0):
    """Rasterise the help SVG to a crisp QPixmap (Hi-DPI via device pixel ratio).

    Imports Qt lazily so the SVG builder stays usable without a GUI stack.
    Raises ImportError if the Qt SVG module is unavailable; callers fall back.
    """
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtSvg import QSvgRenderer

    svg = build_f710_help_svg()
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    size = renderer.defaultSize()  # 1000 x 600 from the viewBox
    pm = QPixmap(int(size.width() * scale), int(size.height() * scale))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    pm.setDevicePixelRatio(scale)
    return pm
