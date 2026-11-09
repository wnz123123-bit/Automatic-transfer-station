"""Small presentation helpers for the cutting workspace; no device operations."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSizePolicy, QToolButton, QVBoxLayout, QWidget

from .i18n import Retranslator, translator


class CuttingDisclosure(QWidget):
    """Keep infrequent controls available without expanding the daily workflow."""

    def __init__(self, title, parent=None, *, expanded=False):
        super().__init__(parent)
        self._rt = Retranslator(translator)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.toggle_button = QToolButton()
        self.toggle_button.setCheckable(True)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._rt.text(self.toggle_button, title)
        layout.addWidget(self.toggle_button)
        self.body = QWidget()
        self.content_layout = QVBoxLayout(self.body)
        self.content_layout.setContentsMargins(8, 0, 0, 4)
        self.content_layout.setSpacing(6)
        layout.addWidget(self.body)
        self.toggle_button.toggled.connect(self._show_body)
        self.set_expanded(expanded)

    def _show_body(self, expanded):
        self.body.setVisible(expanded)
        self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def set_expanded(self, expanded):
        self.toggle_button.setChecked(bool(expanded))
        self._show_body(bool(expanded))

    @property
    def is_expanded(self):
        return self.toggle_button.isChecked()
