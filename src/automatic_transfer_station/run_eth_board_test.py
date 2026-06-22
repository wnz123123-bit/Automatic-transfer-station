from __future__ import annotations

import argparse
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .controller_base import BOARD_IP
from .gamepad_xinput import load_default_reader
from .gas_api import DEFAULT_DLL_PATH, load_default_api
from .gas_controller import GasMotionController
from .logging_setup import configure_logging
from .main_window import MainWindow, ThreadedBoardExecutor
from .nosepiece_controller import (
    DEFAULT_NOSEPIECE_BAUDRATE,
    DisabledNosepieceController,
    SerialNosepieceController,
)
from .paths import RUNTIME_DIR
from .rd105_config import RD105_BAUDRATE


DEFAULT_NOSEPIECE_TRACE_FILE = RUNTIME_DIR / "nosepiece_trace.log"


def make_nosepiece_trace(path: str | Path):
    """Return a thread-safe sink that appends timestamped serial frames to a file."""
    path = Path(path)
    lock = threading.Lock()

    def trace(message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"{stamp} {message}\n"
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)

    return trace


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EthBoardTest Python GUI")
    parser.add_argument("--ip", default=BOARD_IP, help="Motion-card network address.")
    parser.add_argument("--dll", default=str(DEFAULT_DLL_PATH), help="Path to GAS.dll.")
    parser.add_argument("--nosepiece-port", default=None, help="LV-NCNT2 serial port, for example COM7.")
    parser.add_argument(
        "--nosepiece-baud",
        type=int,
        default=DEFAULT_NOSEPIECE_BAUDRATE,
        help="LV-NCNT2 baud rate. Factory default is 9600; S4-4 ON uses 19200.",
    )
    parser.add_argument("--temperature-port", default=None, help="RD105 serial port, for example COM5.")
    parser.add_argument(
        "--temperature-baud",
        type=int,
        default=RD105_BAUDRATE,
        help="RD105 baud rate. Current controller setting is 38400.",
    )
    parser.add_argument(
        "--nosepiece-trace",
        action="store_true",
        help="Log every LV-NCNT2 serial TX/RX frame for protocol diagnostics. Does not change move/verify behaviour.",
    )
    parser.add_argument(
        "--nosepiece-trace-file",
        default=str(DEFAULT_NOSEPIECE_TRACE_FILE),
        help="Where to write the nosepiece serial trace.",
    )
    parser.add_argument("--smoke-exit", action="store_true", help="Create the GUI and exit without starting event loop.")
    parser.add_argument("--no-gamepad", dest="gamepad", action="store_false",
                        help="Disable F710 gamepad control.")
    parser.set_defaults(gamepad=True)
    return parser.parse_args(argv)


def build_controller(args: argparse.Namespace):
    api = load_default_api(Path(args.dll))
    return GasMotionController(api=api, board_ip=args.ip)


def _nosepiece_trace(args: argparse.Namespace):
    return make_nosepiece_trace(args.nosepiece_trace_file) if getattr(args, "nosepiece_trace", False) else None


def build_nosepiece_controller(args: argparse.Namespace):
    if args.nosepiece_port:
        return SerialNosepieceController(
            port=args.nosepiece_port, baudrate=args.nosepiece_baud, trace=_nosepiece_trace(args)
        )
    return DisabledNosepieceController()


def build_nosepiece_controller_factory(args: argparse.Namespace):
    trace = _nosepiece_trace(args)
    return lambda port: SerialNosepieceController(port=port, baudrate=args.nosepiece_baud, trace=trace)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging()
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])

    gamepad_reader = None
    if args.gamepad:
        try:
            gamepad_reader = load_default_reader()
        except Exception as exc:  # platform/XInput availability
            print(f"手柄初始化失败，已禁用手柄控制：{exc}", file=sys.stderr)
            gamepad_reader = None

    try:
        controller = build_controller(args)
    except (OSError, AttributeError) as exc:
        # A missing / wrong-bitness / broken-dependency GAS.dll otherwise crashes
        # before any window appears with a raw traceback. Fail with a clear message.
        message = (
            f"无法加载运动卡 DLL（{args.dll}）：{exc}\n"
            "请确认 GAS.dll 路径正确，且与 Python 位数一致（64 位）。"
        )
        print(message, file=sys.stderr)
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "运动卡 DLL 加载失败", message)
        except Exception:
            pass
        return 1

    window = MainWindow(
        controller=controller,
        nosepiece_controller=build_nosepiece_controller(args),
        nosepiece_controller_factory=build_nosepiece_controller_factory(args),
        nosepiece_port=args.nosepiece_port,
        temperature_port=args.temperature_port,
        temperature_baud=args.temperature_baud,
        # Run the blocking GA_Open/GA_Close/GA_Reset DLL calls off the GUI thread
        # so an unreachable board (192.168.0.200) no longer freezes the window for
        # the ~21 s OS TCP timeout. Tests keep the default inline executor.
        board_action_executor=ThreadedBoardExecutor(),
        gamepad_reader=gamepad_reader,
    )
    if args.smoke_exit:
        window.close()
        return 0

    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
