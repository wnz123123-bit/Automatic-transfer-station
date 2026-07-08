import ctypes
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class FakeFunction:
    def __init__(self, return_value=0, side_effect=None):
        self.return_value = return_value
        self.side_effect = side_effect
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        if self.side_effect is not None:
            return self.side_effect(*args)
        return self.return_value


class FakeDll:
    def __init__(self):
        names = [
            "GA_Open",
            "GA_Close",
            "GA_Reset",
            "GA_SetCardNo",
            "GA_AxisOn",
            "GA_AxisOff",
            "GA_ClrSts",
            "GA_GetSts",
            "GA_SetHardLimP",
            "GA_SetHardLimN",
            "GA_SetLmtSnsSingle",
            "GA_LmtsOn",
            "GA_LmtsOff",
            "GA_PrfTrap",
            "GA_SetTrapPrmSingle",
            "GA_PrfJog",
            "GA_SetJogPrmSingle",
            "GA_SetPos",
            "GA_SetVel",
            "GA_Update",
            "GA_Stop",
            "GA_GetPrfPos",
            "GA_ZeroPos",
            "GA_HomeSetPrm",
            "GA_HomeStart",
            "GA_GetExtDiBit",
            "GA_GetExtDiValue",
        ]
        for name in names:
            setattr(self, name, FakeFunction())


class GasApiTests(unittest.TestCase):
    def test_loads_expected_function_signatures_on_supplied_dll(self):
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        GasApi(dll=dll)

        self.assertEqual([ctypes.c_short, ctypes.c_char_p], dll.GA_Open.argtypes)
        self.assertEqual(ctypes.c_int, dll.GA_Open.restype)
        self.assertEqual([], dll.GA_Close.argtypes)
        self.assertEqual(ctypes.c_int, dll.GA_Close.restype)
        self.assertEqual([ctypes.c_short, ctypes.c_double], dll.GA_SetVel.argtypes)
        self.assertEqual(
            [
                ctypes.c_short,
                ctypes.POINTER(ctypes.c_long),
                ctypes.c_short,
                ctypes.POINTER(ctypes.c_ulong),
            ],
            dll.GA_GetSts.argtypes,
        )
        self.assertEqual(
            [ctypes.c_short, ctypes.c_short, ctypes.POINTER(ctypes.c_ushort)],
            dll.GA_GetExtDiBit.argtypes,
        )
        self.assertEqual(
            [ctypes.c_short, ctypes.POINTER(ctypes.c_ulong), ctypes.c_short],
            dll.GA_GetExtDiValue.argtypes,
        )

    def test_get_status_returns_status_word_from_sdk(self):
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()

        def fill_status(axis, status, count, clock):
            self.assertEqual(4, axis)
            self.assertEqual(1, count)
            status._obj.value = 0b100011
            clock._obj.value = 88
            return 0

        dll.GA_GetSts.side_effect = fill_status
        api = GasApi(dll=dll)

        self.assertEqual(0b100011, api.get_status(4))

    def test_open_network_encodes_ip_as_ascii(self):
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        api = GasApi(dll=dll)

        self.assertEqual(0, api.open_network("192.168.0.200", mode=0))
        self.assertEqual([(0, b"192.168.0.200")], dll.GA_Open.calls)

    def test_get_prf_pos_passes_axis_count_and_clock_pointer(self):
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()

        def fill_position(axis, value, count, clock):
            self.assertEqual(3, axis)
            self.assertEqual(1, count)
            value._obj.value = 12.5
            clock._obj.value = 99
            return 0

        dll.GA_GetPrfPos.side_effect = fill_position
        api = GasApi(dll=dll)

        self.assertEqual(12.5, api.get_prf_pos(3))

    def test_get_ext_di_bit_returns_single_input_bit(self):
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()

        def fill_input(card_index, bit_index, value):
            self.assertEqual(0, card_index)
            self.assertEqual(8, bit_index)
            value._obj.value = 1
            return 0

        dll.GA_GetExtDiBit.side_effect = fill_input
        api = GasApi(dll=dll)

        self.assertEqual(1, api.get_ext_di_bit(0, 8))

    def test_get_ext_di_value_returns_card_input_word(self):
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()

        def fill_input_word(card_index, value, count):
            self.assertEqual(0, card_index)
            self.assertEqual(1, count)
            value._obj.value = 0x100
            return 0

        dll.GA_GetExtDiValue.side_effect = fill_input_word
        api = GasApi(dll=dll)

        self.assertEqual(0x100, api.get_ext_di_value(0))

    # ---- Package 7 Test 3 (api part): nonzero DLL return raises RuntimeError ----

    def test_get_status_nonzero_return_raises_runtime_error(self):
        """GA_GetSts returning nonzero must cause get_status to raise RuntimeError."""
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        dll.GA_GetSts.return_value = -1  # nonzero failure code
        api = GasApi(dll=dll)

        with self.assertRaises(RuntimeError):
            api.get_status(4)

    def test_get_prf_pos_nonzero_return_raises_runtime_error(self):
        """GA_GetPrfPos returning nonzero must cause get_prf_pos to raise RuntimeError."""
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        dll.GA_GetPrfPos.return_value = -2  # nonzero failure code
        api = GasApi(dll=dll)

        with self.assertRaises(RuntimeError):
            api.get_prf_pos(3)

    # ---- Fix 2: IP address validation in open_network ----

    def test_open_network_rejects_empty_ip(self):
        """open_network with an empty string must raise ValueError before any DLL call."""
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        api = GasApi(dll=dll)

        with self.assertRaises(ValueError):
            api.open_network("")

        self.assertEqual([], dll.GA_Open.calls)

    def test_open_network_rejects_nul_in_ip(self):
        """open_network with a NUL character must raise ValueError before any DLL call."""
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        api = GasApi(dll=dll)

        with self.assertRaises(ValueError):
            api.open_network("192.168.0\x00.200")

        self.assertEqual([], dll.GA_Open.calls)

    def test_open_network_rejects_non_ascii_ip(self):
        """open_network with non-ASCII characters must raise ValueError before any DLL call."""
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        api = GasApi(dll=dll)

        with self.assertRaises(ValueError):
            api.open_network("192.168.0.2éé")

        self.assertEqual([], dll.GA_Open.calls)

    def test_home_set_prm_uses_axis_home_structure(self):
        from automatic_transfer_station.gas_api import GasApi

        dll = FakeDll()
        captured = {}

        def capture_home_prm(axis, prm):
            captured["axis"] = axis
            captured["prm"] = prm._obj
            return 0

        dll.GA_HomeSetPrm.side_effect = capture_home_prm
        api = GasApi(dll=dll)

        api.home_set_prm(
            9,
            home_mode=1,
            home_direction=1,
            offset=0,
            rapid_velocity=10.0,
            locate_velocity=2.0,
            index_velocity=1.0,
            acceleration=0.5,
            delay_before_zero=3000,
        )

        prm = captured["prm"]
        self.assertEqual(9, captured["axis"])
        self.assertEqual(1, prm.nHomeMode)
        self.assertEqual(1, prm.nHomeDir)
        self.assertEqual(0, prm.lOffset)
        self.assertEqual(10.0, prm.dHomeRapidVel)
        self.assertEqual(2.0, prm.dHomeLocatVel)
        self.assertEqual(1.0, prm.dHomeIndexVel)
        self.assertEqual(0.5, prm.dHomeAcc)
        self.assertEqual(0, prm.ulHomeIndexDis)
        self.assertEqual(0, prm.ulHomeBackDis)
        self.assertEqual(3000, prm.nDelayTimeBeforeZero)
        self.assertEqual(0, prm.ulHomeMaxDis)


if __name__ == "__main__":
    unittest.main()
