from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QMainWindow

from .camera_panel import CameraPanel
from .paths import CAMERA_SNAPSHOT_DIR


class CameraWindow(QMainWindow):
    def __init__(
        self,
        register: Callable[[str, object], object] | None = None,
        controller_factory: Callable[[], object] | None = None,
        snapshot_dir: str | Path = CAMERA_SNAPSHOT_DIR,
    ):
        super().__init__()
        from .i18n import translator, Retranslator, tr

        self._tr = tr
        self._rt = Retranslator(translator)
        self._prepared_for_close = False
        self.close_guard = None
        self.setObjectName("cameraWindow")
        self._rt.custom(lambda: self.setWindowTitle(self._tr("Basler 相机")))
        self.resize(760, 620)
        self.setMinimumSize(760, 620)

        self.camera_panel = CameraPanel(
            register=register,
            controller_factory=controller_factory,
            snapshot_dir=snapshot_dir,
        )
        if register is not None:
            register("cameraPanel", self.camera_panel)
        self.setCentralWidget(self.camera_panel)

    def prepare_for_close(self, timeout_ms: int = 5000) -> bool:
        self._prepared_for_close = True
        return self.camera_panel.prepare_for_close(timeout_ms=timeout_ms)

    def close_after_prepare(self, timeout_ms: int = 5000) -> bool:
        idle = self.prepare_for_close(timeout_ms=timeout_ms)
        self.close()
        return idle

    def set_objective(self, label: str, position: int | None = None) -> None:
        self.camera_panel.set_objective(label, position)

    def closeEvent(self, event) -> None:
        if not self._prepared_for_close and self.close_guard is not None and not self.close_guard():
            event.ignore()
            return
        if not self._prepared_for_close:
            self.prepare_for_close(timeout_ms=2000)
        super().closeEvent(event)
