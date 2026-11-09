from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path

from .paths import GAS_DLL_PATH


DEFAULT_DLL_PATH = GAS_DLL_PATH

MC_LIMIT_POSITIVE = 0
MC_LIMIT_NEGATIVE = 1
MC_ARRIVE = 5

AXIS_STATUS_POS_HARD_LIMIT = 0x00000020
AXIS_STATUS_NEG_HARD_LIMIT = 0x00000040


@dataclass
class TrapPrm:
    acc: float
    dec: float
    vel_start: float = 0.0
    smooth_time: int = 0


@dataclass
class JogPrm:
    acc: float
    dec: float
    smooth: float = 0.0


class TCrdPrm(ctypes.Structure):
    """GAS_N.h TCrdPrm, #pragma pack(2), Windows LONG (always 32 bits).

    This declaration is header-backed, not a claim of board ABI qualification.
    Explicit widths also keep passive/fake-DLL tests portable to LP64 hosts.
    """

    _pack_ = 2
    _fields_ = [
        ("dimension", ctypes.c_int16),
        ("profile", ctypes.c_int16 * 8),
        ("synVelMax", ctypes.c_double),
        ("synAccMax", ctypes.c_double),
        ("evenTime", ctypes.c_int16),
        ("setOriginFlag", ctypes.c_int16),
        ("originPos", ctypes.c_int32 * 8),
    ]


class AxisHomePrm(ctypes.Structure):
    # Default (natural) struct packing is intentional. All fields actually used by
    # home_set_prm() lie at offsets identical under both default and #pragma
    # pack(2), and the only field that would move (ulHomeMaxDis) is always 0. The
    # vendor header GAS_N.h is outdated (it declares a 7-field struct and omits the
    # GA_HomeSetPrm* prototypes); the real 11-field layout was confirmed against the
    # original C# runtime log. Default packing also avoids a 2-byte over-read if the
    # real DLL turns out to use natural alignment, so it is the safer choice here.
    _fields_ = [
        ("nHomeMode", ctypes.c_short),
        ("nHomeDir", ctypes.c_short),
        ("lOffset", ctypes.c_int),
        ("dHomeRapidVel", ctypes.c_double),
        ("dHomeLocatVel", ctypes.c_double),
        ("dHomeIndexVel", ctypes.c_double),
        ("dHomeAcc", ctypes.c_double),
        ("ulHomeIndexDis", ctypes.c_int),
        ("ulHomeBackDis", ctypes.c_int),
        ("nDelayTimeBeforeZero", ctypes.c_short),
        ("ulHomeMaxDis", ctypes.c_int),
    ]


class GasApi:
    def __init__(self, dll: ctypes.CDLL):
        self.dll = dll
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        signatures = {
            "GA_Open": ([ctypes.c_short, ctypes.c_char_p], ctypes.c_int),
            "GA_Close": ([], ctypes.c_int),
            "GA_Reset": ([], ctypes.c_int),
            "GA_SetCardNo": ([ctypes.c_short], ctypes.c_int),
            "GA_AxisOn": ([ctypes.c_short], ctypes.c_int),
            "GA_AxisOff": ([ctypes.c_short], ctypes.c_int),
            "GA_ClrSts": ([ctypes.c_short, ctypes.c_short], ctypes.c_int),
            "GA_GetSts": (
                [ctypes.c_short, ctypes.POINTER(ctypes.c_long), ctypes.c_short, ctypes.POINTER(ctypes.c_ulong)],
                ctypes.c_int,
            ),
            "GA_SetHardLimP": (
                [ctypes.c_short, ctypes.c_short, ctypes.c_short, ctypes.c_short],
                ctypes.c_int,
            ),
            "GA_SetHardLimN": (
                [ctypes.c_short, ctypes.c_short, ctypes.c_short, ctypes.c_short],
                ctypes.c_int,
            ),
            # GA_SetLmtSnsSingle is not declared in GAS_N.h; argtypes inferred from
            # vendor C# runtime log (same approach as AxisHomePrm / GA_HomeSetPrm above).
            "GA_SetLmtSnsSingle": ([ctypes.c_short, ctypes.c_short, ctypes.c_short], ctypes.c_int),
            "GA_LmtsOn": ([ctypes.c_short, ctypes.c_short], ctypes.c_int),
            "GA_LmtsOff": ([ctypes.c_short, ctypes.c_short], ctypes.c_int),
            "GA_PrfTrap": ([ctypes.c_short], ctypes.c_int),
            "GA_SetTrapPrmSingle": (
                [ctypes.c_short, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_short],
                ctypes.c_int,
            ),
            "GA_PrfJog": ([ctypes.c_short], ctypes.c_int),
            "GA_SetJogPrmSingle": (
                [ctypes.c_short, ctypes.c_double, ctypes.c_double, ctypes.c_double],
                ctypes.c_int,
            ),
            "GA_SetPos": ([ctypes.c_short, ctypes.c_long], ctypes.c_int),
            "GA_SetVel": ([ctypes.c_short, ctypes.c_double], ctypes.c_int),
            "GA_Update": ([ctypes.c_long], ctypes.c_int),
            "GA_Stop": ([ctypes.c_long, ctypes.c_long], ctypes.c_int),
            "GA_GetPrfPos": (
                [
                    ctypes.c_short,
                    ctypes.POINTER(ctypes.c_double),
                    ctypes.c_short,
                    ctypes.POINTER(ctypes.c_ulong),
                ],
                ctypes.c_int,
            ),
            "GA_ZeroPos": ([ctypes.c_short, ctypes.c_short], ctypes.c_int),
            "GA_HomeSetPrm": ([ctypes.c_short, ctypes.POINTER(AxisHomePrm)], ctypes.c_int),
            "GA_HomeStart": ([ctypes.c_short], ctypes.c_int),
            "GA_GetExtDiBit": ([ctypes.c_short, ctypes.c_short, ctypes.POINTER(ctypes.c_ushort)], ctypes.c_int),
            "GA_GetExtDiValue": ([ctypes.c_short, ctypes.POINTER(ctypes.c_ulong), ctypes.c_short], ctypes.c_int),
        }

        for name, (argtypes, restype) in signatures.items():
            func = getattr(self.dll, name)
            func.argtypes = argtypes
            func.restype = restype

        # Optional: old DLLs and existing test doubles need not expose CRD APIs.
        # Export presence establishes capability only, never physical qualification.
        coordinate_signatures = {
            "GA_SetCrdPrm": [ctypes.c_int16, ctypes.POINTER(TCrdPrm)],
            "GA_LnXY": [ctypes.c_int16, ctypes.c_int32, ctypes.c_int32,
                        ctypes.c_double, ctypes.c_double, ctypes.c_double,
                        ctypes.c_int16, ctypes.c_int32],
            "GA_CrdStart": [ctypes.c_int16, ctypes.c_int16],
            "GA_CrdStatus": [ctypes.c_int16, ctypes.POINTER(ctypes.c_int16),
                             ctypes.POINTER(ctypes.c_int32), ctypes.c_int16],
            "GA_CrdClear": [ctypes.c_int16, ctypes.c_int16],
        }
        self.supports_coordinated_motion = all(
            callable(getattr(self.dll, name, None)) for name in coordinate_signatures
        )
        for name, argtypes in coordinate_signatures.items():
            func = getattr(self.dll, name, None)
            if func is not None:
                func.argtypes = argtypes
                func.restype = ctypes.c_int

        # GAS_N.h exact prototypes. MC_GPO == 12 does not establish that a
        # particular external output is addressed by that BufIO type/card pair.
        # That relationship must be explicitly commissioned by the caller.
        gate_signatures = {
            "GA_BufIO": [ctypes.c_int16, ctypes.c_uint16, ctypes.c_uint16,
                         ctypes.c_uint16, ctypes.c_uint16, ctypes.c_int16, ctypes.c_int32],
            "GA_SetExtDoBit": [ctypes.c_int16, ctypes.c_int16, ctypes.c_uint16],
            "GA_GetExtDoBit": [ctypes.c_int16, ctypes.c_int16, ctypes.POINTER(ctypes.c_uint16)],
        }
        self.supports_buffered_io = callable(getattr(self.dll, "GA_BufIO", None))
        self.supports_external_do = all(callable(getattr(self.dll, name, None))
                                       for name in ("GA_SetExtDoBit", "GA_GetExtDoBit"))
        self.supports_cutting_gate = self.supports_buffered_io and self.supports_external_do
        for name, argtypes in gate_signatures.items():
            func = getattr(self.dll, name, None)
            if func is not None:
                func.argtypes = argtypes
                func.restype = ctypes.c_int

    @staticmethod
    def _integer_range(value: int, maximum: int, label: str) -> None:
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"{label} must be an integer in 0..{maximum}")

    def buf_io(self, coordinate: int, do_type: int, card_index: int, mask: int,
               value: int, fifo: int = 0, segment: int = 0) -> int:
        if not self.supports_buffered_io:
            raise RuntimeError("GA_BufIO export is unavailable")
        for label, number in (("do_type", do_type), ("card_index", card_index),
                              ("mask", mask), ("value", value)):
            self._integer_range(number, 0xFFFF, label)
        self._integer_range(coordinate, 0x7FFF, "coordinate")
        self._integer_range(fifo, 0x7FFF, "fifo")
        self._integer_range(segment, 0x7FFFFFFF, "segment")
        return int(self.dll.GA_BufIO(coordinate, do_type, card_index, mask, value, fifo, segment))

    def set_ext_do_bit(self, card_index: int, bit_index: int, value: int) -> int:
        if not self.supports_external_do:
            raise RuntimeError("external digital output exports are unavailable")
        self._integer_range(card_index, 0x7FFF, "card_index")
        self._integer_range(bit_index, 0x7FFF, "bit_index")
        self._integer_range(value, 1, "digital output value")
        return int(self.dll.GA_SetExtDoBit(card_index, bit_index, value))

    def get_ext_do_bit(self, card_index: int, bit_index: int) -> int:
        if not self.supports_external_do:
            raise RuntimeError("external digital output exports are unavailable")
        self._integer_range(card_index, 0x7FFF, "card_index")
        self._integer_range(bit_index, 0x7FFF, "bit_index")
        value = ctypes.c_uint16()
        result = int(self.dll.GA_GetExtDoBit(card_index, bit_index, ctypes.byref(value)))
        if result != 0:
            raise RuntimeError(f"GA_GetExtDoBit({card_index}, {bit_index}) failed with {result}")
        return int(value.value)

    def _require_coordinates(self) -> None:
        if not self.supports_coordinated_motion:
            raise RuntimeError("coordinated motion DLL exports are unavailable")

    def set_crd_prm(self, coordinate: int, prm: TCrdPrm) -> int:
        self._require_coordinates()
        return int(self.dll.GA_SetCrdPrm(coordinate, ctypes.byref(prm)))

    def line_xy(self, coordinate: int, x: int, y: int, velocity: float,
                acceleration: float, end_velocity: float = 0.0,
                fifo: int = 0, segment: int = 0) -> int:
        self._require_coordinates()
        if any(not -(2 ** 31) <= value < 2 ** 31 for value in (x, y, segment)):
            raise ValueError("coordinate/segment exceeds Windows LONG range")
        return int(self.dll.GA_LnXY(coordinate, x, y, velocity, acceleration,
                                  end_velocity, fifo, segment))

    def crd_start(self, mask: int, option: int = 0) -> int:
        self._require_coordinates()
        return int(self.dll.GA_CrdStart(mask, option))

    def crd_status(self, coordinate: int, fifo: int = 0) -> tuple[int, int]:
        self._require_coordinates()
        running, segment = ctypes.c_int16(), ctypes.c_int32()
        result = int(self.dll.GA_CrdStatus(coordinate, ctypes.byref(running),
                                         ctypes.byref(segment), fifo))
        if result != 0:
            raise RuntimeError(f"GA_CrdStatus({coordinate}) failed with {result}")
        return int(running.value), int(segment.value)

    def crd_clear(self, coordinate: int, fifo: int = 0) -> int:
        self._require_coordinates()
        return int(self.dll.GA_CrdClear(coordinate, fifo))

    def open_network(self, ip_address: str, mode: int = 0) -> int:
        if not ip_address:
            raise ValueError("ip_address must not be empty")
        if "\x00" in ip_address:
            raise ValueError("ip_address must not contain NUL characters")
        try:
            encoded = ip_address.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(f"ip_address is not valid ASCII: {ip_address!r}") from exc
        return int(self.dll.GA_Open(mode, encoded))

    def close(self) -> int:
        return int(self.dll.GA_Close())

    def reset(self) -> int:
        return int(self.dll.GA_Reset())

    def set_card_no(self, card_number: int) -> int:
        return int(self.dll.GA_SetCardNo(card_number))

    def axis_on(self, axis: int) -> int:
        return int(self.dll.GA_AxisOn(axis))

    def axis_off(self, axis: int) -> int:
        return int(self.dll.GA_AxisOff(axis))

    def clear_status(self, axis: int, count: int = 1) -> int:
        return int(self.dll.GA_ClrSts(axis, count))

    def get_status(self, axis: int) -> int:
        value = ctypes.c_long(0)
        clock = ctypes.c_ulong(0)
        result = int(self.dll.GA_GetSts(axis, ctypes.byref(value), 1, ctypes.byref(clock)))
        if result != 0:
            raise RuntimeError(f"GA_GetSts({axis}) failed with {result}")
        return int(value.value)

    def set_hard_limit_positive(self, axis: int, limit_type: int, card_index: int, io_index: int) -> int:
        return int(self.dll.GA_SetHardLimP(axis, limit_type, card_index, io_index))

    def set_hard_limit_negative(self, axis: int, limit_type: int, card_index: int, io_index: int) -> int:
        return int(self.dll.GA_SetHardLimN(axis, limit_type, card_index, io_index))

    def set_limit_sense_single(self, axis: int, positive_sense: int, negative_sense: int) -> int:
        return int(self.dll.GA_SetLmtSnsSingle(axis, positive_sense, negative_sense))

    def limits_on(self, axis: int, limit_type: int = -1) -> int:
        return int(self.dll.GA_LmtsOn(axis, limit_type))

    def limits_off(self, axis: int, limit_type: int = -1) -> int:
        return int(self.dll.GA_LmtsOff(axis, limit_type))

    def profile_trap(self, axis: int) -> int:
        return int(self.dll.GA_PrfTrap(axis))

    def set_trap_prm_single(self, axis: int, acc: float, dec: float, vel_start: float, smooth_time: int) -> int:
        return int(self.dll.GA_SetTrapPrmSingle(axis, acc, dec, vel_start, smooth_time))

    def profile_jog(self, axis: int) -> int:
        return int(self.dll.GA_PrfJog(axis))

    def set_jog_prm_single(self, axis: int, acc: float, dec: float, smooth: float) -> int:
        return int(self.dll.GA_SetJogPrmSingle(axis, acc, dec, smooth))

    def set_position(self, axis: int, position: int) -> int:
        return int(self.dll.GA_SetPos(axis, position))

    def set_velocity(self, axis: int, velocity: float) -> int:
        return int(self.dll.GA_SetVel(axis, velocity))

    def update(self, mask: int) -> int:
        return int(self.dll.GA_Update(mask))

    def stop(self, mask: int, option: int = 0) -> int:
        return int(self.dll.GA_Stop(mask, option))

    def get_prf_pos(self, axis: int) -> float:
        value = ctypes.c_double(0.0)
        clock = ctypes.c_ulong(0)
        result = int(self.dll.GA_GetPrfPos(axis, ctypes.byref(value), 1, ctypes.byref(clock)))
        if result != 0:
            raise RuntimeError(f"GA_GetPrfPos({axis}) failed with {result}")
        return float(value.value)

    def get_ext_di_bit(self, card_index: int, bit_index: int) -> int:
        value = ctypes.c_ushort(0)
        result = int(self.dll.GA_GetExtDiBit(card_index, bit_index, ctypes.byref(value)))
        if result != 0:
            raise RuntimeError(f"GA_GetExtDiBit({card_index}, {bit_index}) failed with {result}")
        return int(value.value)

    def get_ext_di_value(self, card_index: int) -> int:
        value = ctypes.c_ulong(0)
        result = int(self.dll.GA_GetExtDiValue(card_index, ctypes.byref(value), 1))
        if result != 0:
            raise RuntimeError(f"GA_GetExtDiValue({card_index}) failed with {result}")
        return int(value.value)

    def zero_position(self, axis: int, count: int = 1) -> int:
        return int(self.dll.GA_ZeroPos(axis, count))

    def home_set_prm(
        self,
        axis: int,
        home_mode: int,
        home_direction: int,
        offset: int,
        rapid_velocity: float,
        locate_velocity: float,
        index_velocity: float,
        acceleration: float,
        delay_before_zero: int = 0,
        home_max_distance: int = 0,
    ) -> int:
        prm = AxisHomePrm()
        prm.nHomeMode = int(home_mode)
        prm.nHomeDir = int(home_direction)
        prm.lOffset = int(offset)
        prm.dHomeRapidVel = float(rapid_velocity)
        prm.dHomeLocatVel = float(locate_velocity)
        prm.dHomeIndexVel = float(index_velocity)
        prm.dHomeAcc = float(acceleration)
        prm.ulHomeIndexDis = 0
        prm.ulHomeBackDis = 0
        prm.nDelayTimeBeforeZero = int(delay_before_zero)
        prm.ulHomeMaxDis = int(home_max_distance)
        return int(self.dll.GA_HomeSetPrm(axis, ctypes.byref(prm)))

    def home_start(self, axis: int) -> int:
        return int(self.dll.GA_HomeStart(axis))


def configure_open_close_signatures(dll: ctypes.CDLL) -> None:
    """Configure GA_Open and GA_Close argtypes/restype on *dll*.

    This is the single authoritative declaration of the open/close calling
    convention. gas_network_probe imports and calls this function so both
    places use identical ctypes marshalling and cannot drift.
    """
    dll.GA_Open.argtypes = [ctypes.c_short, ctypes.c_char_p]
    dll.GA_Open.restype = ctypes.c_int
    dll.GA_Close.argtypes = []
    dll.GA_Close.restype = ctypes.c_int


def load_default_api(dll_path: Path | str = DEFAULT_DLL_PATH) -> GasApi:
    return GasApi(ctypes.CDLL(str(Path(dll_path).resolve())))
