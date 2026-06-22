from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
from pathlib import Path

from .controller_base import BOARD_IP  # BP7: single source for the board IP
from .gas_api import configure_open_close_signatures  # Fix 3: single signature source
from .paths import GAS_DLL_PATH


# Re-export for backward compatibility so external callers can still reference
# gas_network_probe.DEFAULT_IP_ADDRESS, but the value now derives from the
# canonical single source in controller_base.
DEFAULT_IP_ADDRESS = BOARD_IP
DEFAULT_OPEN_MODE = 0


@dataclass(frozen=True)
class ProbeResult:
    open_result: int
    close_result: int | None


def load_dll(dll_path: Path) -> ctypes.CDLL:
    resolved = dll_path.resolve()
    return ctypes.CDLL(str(resolved))


def probe_open_close(
    dll: ctypes.CDLL,
    ip_address: str = DEFAULT_IP_ADDRESS,
    open_mode: int = DEFAULT_OPEN_MODE,
) -> ProbeResult:
    if not ip_address:
        raise ValueError("ip_address must not be empty")
    if "\x00" in ip_address:
        raise ValueError("ip_address must not contain NUL characters")
    try:
        encoded_ip = ip_address.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"ip_address is not valid ASCII: {ip_address!r}") from exc

    configure_open_close_signatures(dll)

    open_result = int(dll.GA_Open(open_mode, encoded_ip))
    if open_result != 0:
        return ProbeResult(open_result=open_result, close_result=None)

    close_result = int(dll.GA_Close())
    return ProbeResult(open_result=open_result, close_result=close_result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe GAS.dll network open/close without reset, axis enable, or motion.",
    )
    parser.add_argument(
        "--dll",
        default=str(GAS_DLL_PATH),
        help="Path to GAS.dll.",
    )
    parser.add_argument(
        "--ip",
        default=DEFAULT_IP_ADDRESS,
        help="Network address passed to GA_Open.",
    )
    parser.add_argument(
        "--mode",
        type=int,
        default=DEFAULT_OPEN_MODE,
        help="GA_Open connection mode.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dll = load_dll(Path(args.dll))
    result = probe_open_close(dll=dll, ip_address=args.ip, open_mode=args.mode)

    print(f"GA_Open({args.mode}, {args.ip!r}) -> {result.open_result}")
    if result.close_result is None:
        print("GA_Close skipped because GA_Open did not return 0")
    else:
        print(f"GA_Close() -> {result.close_result}")

    if result.open_result != 0:
        return 1
    if result.close_result != 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
