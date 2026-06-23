import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIEW_DIR = PROJECT_ROOT / "docs" / "ui_preview"
HTML_PATH = PREVIEW_DIR / "stage_gui_preview.html"
IMAGE_PATH = PREVIEW_DIR / "assets" / "stage_overview.png"

EXPECTED_AXES = ["X1", "Y1", "MicroscopeZ", "Z", "X3", "Y3", "X2", "Y2", "R"]
EDITABLE_POSITION_AXES = ["X1", "Y1", "Z", "X3", "Y3", "X2", "Y2", "R"]
EXPECTED_GROUPS = ["吸附台", "载玻片侧", "整体位移", "显微镜调整", "温度控制"]


class UiPreviewTests(unittest.TestCase):
    def test_preview_html_and_image_asset_exist(self):
        self.assertTrue(HTML_PATH.exists(), HTML_PATH)
        self.assertTrue(IMAGE_PATH.exists(), IMAGE_PATH)
        self.assertGreater(IMAGE_PATH.stat().st_size, 1_000_000)

    def test_preview_contains_axis_hotspots_and_control_cards(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn("assets/stage_overview.png", html)
        for axis in EXPECTED_AXES:
            self.assertRegex(html, rf'class="axis-hotspot[^"]*"[^\n>]+data-axis="{re.escape(axis)}"')
            self.assertRegex(html, rf'class="axis-card[^"]*"[^\n>]+data-axis="{re.escape(axis)}"')

    def test_preview_contains_expected_groups_and_hover_linking_script(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        for group in EXPECTED_GROUPS:
            self.assertIn(group, html)
        self.assertIn("querySelectorAll('[data-axis]')", html)
        self.assertIn("active-axis", html)

    def test_preview_keeps_stage_hotspots_as_labels_without_motion_controls(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertNotIn("<marker", html)
        self.assertNotIn("marker-end", html)
        self.assertNotIn("axis-overlay", html)
        self.assertNotIn("axis-vector", html)
        self.assertNotIn("axis-arrow", html)
        self.assertNotIn('class="axis-direction"', html)
        self.assertNotIn(".axis-direction", html)
        self.assertIn('class="stage-figure"', html)
        self.assertEqual(9, html.count('class="axis-hotspot'))

    def test_preview_keeps_image_area_on_white_background(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn("background: #ffffff;", html)
        self.assertNotIn("background: var(--panel-2);\n      overflow: hidden;\n      display: flex;", html)

    def test_preview_motion_status_lives_in_dedicated_axis_status_bar(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertNotIn('<header class="topbar">', html)
        control_start = html.index('<aside class="control-panel">')
        status_start = html.index('<div class="axis-status-bar"', control_start)
        title_start = html.index('<div class="control-title">', control_start)
        layout_start = html.index('<div class="control-layout">', control_start)
        status_bar = html[status_start:title_start]
        control_header = html[title_start:layout_start]

        self.assertLess(status_start, title_start)
        self.assertLess(title_start, layout_start)
        self.assertIn('class="status-lamp"', html)
        self.assertIn('id="motionStatusText"', status_bar)
        self.assertIn('data-motion-state="off"', status_bar)
        self.assertNotIn('id="motionStatusText"', control_header)
        self.assertIn('data-motion-action="open">打开运动控制</button>', control_header)
        self.assertIn('data-motion-action="close">关闭运动控制</button>', control_header)
        self.assertIn('data-motion-action="reset">复位</button>', control_header)
        self.assertIn('motionMessages', html)
        self.assertIn('motionStatus.dataset.motionState = config.state', html)
        self.assertNotIn(">打开板卡</button>", html)
        self.assertNotIn(">关闭板卡</button>", html)
        self.assertNotIn(">复位板卡</button>", html)

    def test_preview_stage_panel_has_no_title_text(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        stage_start = html.index('<section class="stage-panel">')
        stage_end = html.index('<aside class="control-panel">', stage_start)
        stage_panel = html[stage_start:stage_end]
        self.assertNotIn("设备轴向图", stage_panel)
        self.assertNotIn("悬停标签或控制卡片可联动高亮", stage_panel)
        self.assertNotIn('class="stage-header"', stage_panel)

    def test_preview_uses_two_column_control_layout_with_full_width_overall_group(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: minmax(320px, 34%) minmax(460px, 42%) minmax(260px, 24%);", html)
        self.assertIn("width: min(100%, 620px);", html)
        self.assertIn(".stage-figure {", html)
        self.assertIn('class="control-layout"', html)
        self.assertIn(".control-layout {", html)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", html)
        self.assertIn(".axis-group.full-row", html)
        self.assertIn("grid-column: 1 / -1;", html)

        adsorb_index = html.index('<section class="axis-group" data-section="adsorption">')
        slide_index = html.index('<section class="axis-group" data-section="slide">')
        overall_index = html.index('<section class="axis-group full-row" data-section="overall">')
        self.assertLess(adsorb_index, slide_index)
        self.assertLess(slide_index, overall_index)

        self.assertIn('<h3>吸附台</h3>', html)
        self.assertIn('<h3>载玻片侧</h3>', html)
        self.assertIn('<h3>整体位移</h3>', html)

        control_start = html.index('<aside class="control-panel">')
        control_end = html.index('<aside class="temperature-panel">', control_start)
        control_panel = html[control_start:control_end]
        self.assertNotIn('data-section="microscope"', control_panel)

    def test_preview_places_microscope_controls_below_left_stage_image(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        stage_start = html.index('<section class="stage-panel">')
        stage_end = html.index('<aside class="control-panel">', stage_start)
        stage_panel = html[stage_start:stage_end]

        self.assertIn('<div class="stage-map"', stage_panel)
        self.assertIn('<section class="axis-group full-row stage-microscope-controls" data-section="microscope">', stage_panel)
        self.assertLess(stage_panel.index('<div class="stage-map"'), stage_panel.index('stage-microscope-controls'))
        self.assertIn('<h3>显微镜调整</h3>', stage_panel)
        self.assertIn('data-axis="MicroscopeZ"', stage_panel)
        self.assertIn('class="objective-selector scope-color"', stage_panel)
        self.assertLess(html.index(".axis-group h3 {"), html.index(".stage-microscope-controls h3 {"))
        self.assertIn(".stage-microscope-controls h3 {\n      color: #f6f8fb;", html)

    def test_preview_adds_temperature_panel_to_far_right(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        stage_index = html.index('<section class="stage-panel">')
        control_index = html.index('<aside class="control-panel">')
        temperature_index = html.index('<aside class="temperature-panel">')
        self.assertLess(stage_index, control_index)
        self.assertLess(control_index, temperature_index)

        self.assertIn("<h2>温度控制</h2>", html)
        self.assertIn('<section class="temperature-card" data-temperature-section="connection">', html)
        self.assertIn('<section class="temperature-card" data-temperature-section="status">', html)
        self.assertIn('<section class="temperature-card" data-temperature-section="control">', html)
        self.assertIn('<section class="temperature-card" data-temperature-section="plot">', html)
        for text in (
            "COM 端口",
            "刷新端口",
            "开始连接",
            "当前温度",
            "目标温度",
            "输出 (PWM)",
            "最大输出占空比",
            "快速设温",
            "按速率升温",
            "关闭输出",
            "温度曲线",
        ):
            self.assertIn(text, html)
        self.assertNotIn("自动 PID 档", html)
        self.assertIn("data-temperature-action", html)
        self.assertIn("temperatureStatusText", html)

    def test_preview_positions_are_editable_except_microscope_z_and_have_popup(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertEqual(8, html.count('class="axis-position-input"'))
        for axis in EDITABLE_POSITION_AXES:
            self.assertRegex(
                html,
                rf'class="axis-position-editor" data-position-axis="{re.escape(axis)}"[\s\S]*?'
                rf'class="axis-position-input" data-position-input="{re.escape(axis)}"[\s\S]*?'
                rf'data-position-open="{re.escape(axis)}"',
            )

        self.assertNotIn('data-position-axis="MicroscopeZ"', html)
        self.assertIn('<div class="axis-pos">pulse 0</div>', html)
        self.assertIn('<dialog class="position-dialog" id="positionDialog"', html)
        self.assertIn('id="positionDialogAxis"', html)
        self.assertIn('id="positionDialogCurrentInput"', html)
        self.assertIn('id="positionDialogActualInput"', html)
        self.assertIn('id="positionDialogApply"', html)
        self.assertIn("function openPositionDialog(axis, input)", html)
        self.assertIn("const currentFormatted = formatPositionValue(positionDialogCurrentInput.value);", html)
        self.assertIn("const actualFormatted = formatPositionValue(positionDialogActualInput.value);", html)
        self.assertIn("activePositionInput.value = actualFormatted;", html)

    def test_axis_card_header_keeps_role_text_horizontal_with_position_editor(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn('grid-template-areas: "name role" "position position";', html)
        self.assertIn("grid-template-columns: 44px minmax(0, 1fr);", html)
        self.assertIn("grid-area: role;", html)
        self.assertIn("word-break: keep-all;", html)
        self.assertIn("grid-area: position;", html)
        self.assertIn("width: 100%;", html)

    def test_microscope_header_keeps_long_axis_name_and_role_on_one_row(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn('.axis-card[data-axis="MicroscopeZ"] .axis-card-header {', html)
        self.assertIn("grid-template-columns: max-content minmax(0, 1fr);", html)
        self.assertIn('grid-template-areas: "name role" "position position";', html)
        self.assertIn('.axis-card[data-axis="MicroscopeZ"] .axis-name {', html)
        self.assertIn("white-space: nowrap;", html)

    def test_microscope_section_reserves_objective_selector(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn('class="objective-selector scope-color"', html)
        self.assertIn("<h4>物镜选择</h4>", html)
        self.assertEqual(5, html.count('class="objective-button"'))
        self.assertEqual(5, html.count("data-objective-power="))
        for power in ("5X", "10X", "20X", "40X", "50X"):
            self.assertIn(f'data-objective-power="{power}"', html)
            self.assertIn(f"选择 {power} 物镜", html)
        self.assertIn("function selectObjective(button)", html)
        self.assertIn(".objective-button.is-selected", html)

    def test_objective_selector_is_inside_microscope_section_after_z_control(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        microscope_start = html.index('<section class="axis-group full-row stage-microscope-controls" data-section="microscope">')
        microscope_end = html.index('<aside class="control-panel">', microscope_start)
        microscope_section = html[microscope_start:microscope_end]

        self.assertIn('data-axis="MicroscopeZ"', microscope_section)
        self.assertIn('class="objective-selector scope-color"', microscope_section)
        self.assertLess(
            microscope_section.index('data-axis="MicroscopeZ"'),
            microscope_section.index('class="objective-selector scope-color"'),
        )

    def test_preview_uses_more_compact_stage_image_and_buttons(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: minmax(320px, 34%) minmax(460px, 42%) minmax(260px, 24%);", html)
        self.assertIn("padding: 10px;", html)
        self.assertIn("gap: 10px;", html)
        self.assertIn("width: min(100%, 620px);", html)
        self.assertIn("height: auto;", html)
        self.assertIn("min-height: 28px;", html)
        self.assertIn("width: 26px;", html)
        self.assertIn("height: 20px;", html)
        self.assertIn("min-height: 30px;", html)

    def test_axis_role_text_is_bold_and_slide_labels_do_not_use_one_side(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertIn(".axis-role {", html)
        self.assertIn("font-weight: 700;", html)
        self.assertNotIn("载玻片一侧", html)
        self.assertIn('<div class="axis-role">载玻片左右移动</div>', html)
        self.assertIn('<div class="axis-role">载玻片前后移动</div>', html)
        self.assertIn('<div class="axis-role">载玻片升降</div>', html)

    def test_motion_controls_use_five_button_jog_stop_continuous_pad(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertEqual(9, html.count('class="axis-motion motion-pad"'))
        self.assertEqual(18, html.count('data-motion-mode="continuous"'))
        self.assertEqual(18, html.count('data-motion-mode="jog"'))
        self.assertEqual(9, html.count('data-motion-mode="stop"'))
        self.assertIn('class="motion-btn motion-continuous"', html)
        self.assertIn('class="motion-btn motion-jog"', html)
        self.assertIn('class="motion-btn motion-stop"', html)
        self.assertIn("function handleJog(button)", html)
        self.assertIn("function handleContinuousStart(button)", html)
        self.assertIn("function handleMotionStop(button)", html)
        self.assertIn("pointerdown", html)
        self.assertIn("pointerup", html)
        self.assertIn("pointercancel", html)
        self.assertIn("event.stopPropagation();", html)
        self.assertNotIn("按钮预览：", html)

    def test_motion_pad_uses_uniform_css_drawn_symbols(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertEqual(9, html.count('class="motion-symbol motion-symbol-continuous-negative"'))
        self.assertEqual(9, html.count('class="motion-symbol motion-symbol-jog-negative"'))
        self.assertEqual(9, html.count('class="motion-symbol motion-symbol-stop"'))
        self.assertEqual(9, html.count('class="motion-symbol motion-symbol-jog-positive"'))
        self.assertEqual(9, html.count('class="motion-symbol motion-symbol-continuous-positive"'))
        self.assertNotIn('class="motion-icon"', html)
        self.assertIn(".motion-symbol {", html)
        self.assertIn("width: 26px;", html)
        self.assertIn("height: 20px;", html)
        self.assertIn("display: block;", html)
        self.assertIn(".motion-symbol::before", html)
        self.assertIn(".motion-symbol-stop::before", html)
        self.assertIn("align-items: center;", html)
        self.assertIn("justify-items: center;", html)

    def test_motion_settings_show_speed_and_step_units(self):
        html = HTML_PATH.read_text(encoding="utf-8")

        self.assertEqual(8, html.count("速度 (um/s)<input"))
        self.assertEqual(8, html.count("步长 (um)<input"))
        self.assertEqual(1, html.count("速度 (deg/s)<input"))
        self.assertEqual(1, html.count("步长 (deg)<input"))
        self.assertNotIn("角度<input", html)


if __name__ == "__main__":
    unittest.main()
