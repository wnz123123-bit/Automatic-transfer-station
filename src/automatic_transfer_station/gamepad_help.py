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

from .i18n import tr as _tr

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
    p.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 700" width="1000" height="700">')

    # Card background (keeps the figure legible regardless of dialog theme).
    p.append('<rect x="0" y="0" width="1000" height="700" rx="14" fill="#ffffff" stroke="#d0d7de" stroke-width="1.5"/>')

    # Header.
    p.append(_text(30, 40, _tr("Logitech F710 手柄按键说明"), size=22, weight="700"))
    p.append(_text(30, 63, _tr("MODE 拨到 X · 摇杆推得越多越快（比例速度）"), size=14, fill=_SUB))

    # Two views, matching the official layout: a top view for the face controls
    # and a front view for the shoulder buttons + MODE (which live on the front
    # edge and cannot be placed unambiguously on a top-down drawing).
    p.append(_text(40, 98, _tr("俯视图（正面）"), size=15, weight="700", fill=_SUB))
    p.append(_text(40, 410, _tr("前视图（肩键 / 扳机一侧）"), size=15, weight="700", fill=_SUB))

    _top_view(p)
    _front_view(p)

    # Footer notes.
    p.append(_text(500, 650,
                   _tr("可选轴：X3 / Y3 / X2 / Y2 / R / 载玻片Z / X1 / Y1（显微镜Z 由扳机 LT/RT 控制）"),
                   size=13, anchor="middle", fill=_SUB))
    p.append(_text(500, 674,
                   _tr("当前选中轴在左侧设备图横幅显示（含步长与坐标） · Space 键也是急停"),
                   size=13, anchor="middle", fill=_SUB))

    p.append('</svg>')
    return "".join(p)


def _top_view(p: list[str]) -> None:
    """Top-down view: D-pad, dual sticks, Y/A/X/B diamond, BACK/START."""
    # Grips first so the body overlaps their tops.
    p.append(f'<ellipse cx="415" cy="298" rx="44" ry="76" transform="rotate(-17 415 298)" '
             f'fill="{_BODY2}" stroke="{_EDGE}" stroke-width="2"/>')
    p.append(f'<ellipse cx="585" cy="298" rx="44" ry="76" transform="rotate(17 585 298)" '
             f'fill="{_BODY2}" stroke="{_EDGE}" stroke-width="2"/>')
    # Shoulder bumpers as a subtle hint at the top edge (named in the front view).
    p.append(f'<rect x="400" y="134" width="64" height="16" rx="8" fill="{_SHOULDER}" stroke="{_EDGE}" stroke-width="1.5"/>')
    p.append(f'<rect x="536" y="134" width="64" height="16" rx="8" fill="{_SHOULDER}" stroke="{_EDGE}" stroke-width="1.5"/>')
    # Main body.
    p.append(f'<rect x="380" y="150" width="240" height="116" rx="46" fill="{_BODY}" stroke="{_EDGE}" stroke-width="2.5"/>')

    # D-pad (upper-left).
    p.append(f'<rect x="432" y="178" width="16" height="40" rx="4" fill="{_DARK}"/>')
    p.append(f'<rect x="418" y="190" width="44" height="16" rx="4" fill="{_DARK}"/>')
    p.append(f'<circle cx="440" cy="198" r="5" fill="#444d58"/>')

    # Analog sticks.
    for sx in (464, 552):
        p.append(f'<circle cx="{sx}" cy="248" r="24" fill="{_SHOULDER}" stroke="{_EDGE}" stroke-width="2"/>')
        p.append(f'<circle cx="{sx}" cy="248" r="14" fill="#39424e"/>')
        p.append(f'<circle cx="{sx - 4}" cy="244" r="4" fill="#5a646f"/>')

    # Face-button diamond (Y top, A bottom, X left, B right).
    for cx, cy, col, lbl in ((568, 174, _COL_Y, "Y"), (568, 214, _COL_A, "A"),
                             (546, 194, _COL_X, "X"), (590, 194, _COL_B, "B")):
        p.append(f'<circle cx="{cx}" cy="{cy}" r="13" fill="{col}"/>')
        p.append(_text(cx, cy + 5, lbl, size=14, weight="700", anchor="middle", fill="#ffffff"))

    # Centre buttons: BACK, logo, START (MODE is on the front edge -> front view).
    p.append(f'<circle cx="500" cy="186" r="9" fill="#707a86"/>')
    p.append(f'<rect x="466" y="181" width="18" height="9" rx="4" fill="{_EDGE}"/>')   # BACK
    p.append(f'<rect x="516" y="181" width="18" height="9" rx="4" fill="{_EDGE}"/>')   # START

    # Callouts.
    p.append(_callout(250, 128, "end", "BACK", [_tr("急停（所有轴立即停止）")], (258, 122), (466, 185)))
    p.append(_callout(250, 196, "end", _tr("十字键"),
                      [_tr("↑ / ↓  选中轴 单步 ±"), _tr("← / →  步长档 减小 / 增大")],
                      (258, 190), (418, 198)))
    p.append(_callout(250, 280, "end", _tr("左摇杆"), [_tr("X3（左右）/ Y3（前后） 连续")], (258, 274), (440, 248)))

    p.append(_callout(750, 128, "start", "START", [_tr("启用 / 停用手柄")], (742, 122), (534, 185)))
    # Face-button callout with coloured Y/A/X/B chips on the name line.
    p.append(_leader(742, 190, 590, 194))
    p.append(
        f'<text x="750" y="196" font-family="{_FONT}" font-size="16" font-weight="700" text-anchor="start">'
        f'<tspan fill="{_INK}">{_esc(_tr("面键 "))}</tspan>'
        f'<tspan fill="{_COL_Y}">Y</tspan><tspan fill="{_INK}">/</tspan>'
        f'<tspan fill="{_COL_A}">A</tspan><tspan fill="{_INK}">/</tspan>'
        f'<tspan fill="{_COL_X}">X</tspan><tspan fill="{_INK}">/</tspan>'
        f'<tspan fill="{_COL_B}">B</tspan>'
        f'</text>'
    )
    p.append(_text(750, 214, _tr("选轴：Y=上  A=下  X=左  B=右"), size=13, fill=_SUB))
    p.append(_callout(750, 280, "start", _tr("右摇杆"), [_tr("X2（左右）/ Y2（前后） 连续")], (742, 274), (576, 248)))


def _front_view(p: list[str]) -> None:
    """Front view (the edge with the shoulder buttons): LB/LT, RB/RT and the
    MODE button dead-centre on the front edge."""
    dark = "#2c3138"
    # Grips (front-on, foreshortened) first.
    p.append(f'<ellipse cx="362" cy="548" rx="33" ry="40" fill="{dark}"/>')
    p.append(f'<ellipse cx="638" cy="548" rx="33" ry="40" fill="{dark}"/>')
    # Silver top edge + dark front face.
    p.append('<rect x="338" y="456" width="324" height="15" rx="7" fill="#cdd5df"/>')
    p.append(f'<rect x="338" y="469" width="324" height="66" rx="26" fill="{dark}" stroke="#1b1f24" stroke-width="2"/>')
    # Bumpers (LB/RB) on top of the front face.
    p.append('<rect x="368" y="447" width="62" height="19" rx="8" fill="#3a4049" stroke="#1b1f24" stroke-width="1.5"/>')
    p.append('<rect x="570" y="447" width="62" height="19" rx="8" fill="#3a4049" stroke="#1b1f24" stroke-width="1.5"/>')
    p.append(_text(399, 461, "LB", size=11, weight="700", anchor="middle", fill="#ffffff"))
    p.append(_text(601, 461, "RB", size=11, weight="700", anchor="middle", fill="#ffffff"))
    # Triggers (LT/RT) as paddles below the bumpers.
    p.append('<rect x="376" y="495" width="54" height="30" rx="12" fill="#1c2126" stroke="#11151a" stroke-width="1.5"/>')
    p.append('<rect x="570" y="495" width="54" height="30" rx="12" fill="#1c2126" stroke="#11151a" stroke-width="1.5"/>')
    p.append(_text(403, 514, "LT", size=11, weight="700", anchor="middle", fill="#ffffff"))
    p.append(_text(597, 514, "RT", size=11, weight="700", anchor="middle", fill="#ffffff"))
    # MODE button: centre of the front edge.
    p.append('<circle cx="500" cy="500" r="12" fill="#4a515a" stroke="#cdd5df" stroke-width="2"/>')
    p.append(f'<circle cx="500" cy="500" r="3" fill="{dark}"/>')

    # Callouts.
    p.append(_callout(250, 452, "end", "LB", [_tr("载玻片Z 下")], (258, 452), (368, 456)))
    p.append(_callout(250, 512, "end", "LT", [_tr("显微镜Z 下"), _tr("轻按单步 · 长按连续 ≤4mm/s")], (258, 512), (376, 510)))
    p.append(_callout(750, 452, "start", "RB", [_tr("载玻片Z 上")], (742, 452), (632, 456)))
    p.append(_callout(750, 512, "start", "RT", [_tr("显微镜Z 上"), _tr("轻按单步 · 长按连续 ≤4mm/s")], (742, 512), (624, 510)))
    p.append(_leader(500, 512, 500, 578))
    p.append(_text(500, 599, _tr("MODE 拨到 X 模式"), size=15, weight="700", anchor="middle"))


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
