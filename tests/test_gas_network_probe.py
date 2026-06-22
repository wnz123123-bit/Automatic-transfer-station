import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class FakeFunction:
    def __init__(self, return_value=0):
        self.return_value = return_value
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.return_value


class FakeGasDll:
    def __init__(self, open_result=0, close_result=0):
        self.GA_Open = FakeFunction(open_result)
        self.GA_Close = FakeFunction(close_result)

    def __getattr__(self, name):
        if name.startswith("GA_"):
            raise AssertionError(f"unexpected hardware call: {name}")
        raise AttributeError(name)


class GasNetworkProbeTests(unittest.TestCase):
    # ---- Fix 2: IP validation in probe_open_close ----

    def test_probe_rejects_empty_ip(self):
        """probe_open_close with empty ip_address must raise ValueError before DLL call."""
        from automatic_transfer_station import gas_network_probe

        dll = FakeGasDll()

        with self.assertRaises(ValueError):
            gas_network_probe.probe_open_close(dll=dll, ip_address="")

        self.assertEqual([], dll.GA_Open.calls)

    def test_probe_rejects_nul_in_ip(self):
        """probe_open_close with NUL in ip_address must raise ValueError before DLL call."""
        from automatic_transfer_station import gas_network_probe

        dll = FakeGasDll()

        with self.assertRaises(ValueError):
            gas_network_probe.probe_open_close(dll=dll, ip_address="192.168.0\x00.200")

        self.assertEqual([], dll.GA_Open.calls)

    def test_probe_rejects_non_ascii_ip(self):
        """probe_open_close with non-ASCII ip_address must raise ValueError before DLL call."""
        from automatic_transfer_station import gas_network_probe

        dll = FakeGasDll()

        with self.assertRaises(ValueError):
            gas_network_probe.probe_open_close(dll=dll, ip_address="192.168.0.2éé")

        self.assertEqual([], dll.GA_Open.calls)

    # ---- Fix 3: probe must reuse gas_api's signature declarations ----

    def test_probe_uses_configure_open_close_signatures_from_gas_api(self):
        """gas_network_probe must import configure_open_close_signatures from gas_api,
        not re-declare it, so the two places share a single ctypes marshalling definition."""
        import automatic_transfer_station.gas_network_probe as probe_module
        import automatic_transfer_station.gas_api as api_module

        # The function object the probe module uses must be the same one from gas_api.
        self.assertIs(
            probe_module.configure_open_close_signatures,
            api_module.configure_open_close_signatures,
        )

    def test_probe_open_close_signatures_match_gas_api_signatures(self):
        """After probe_open_close runs, GA_Open and GA_Close argtypes/restype on the
        DLL must be identical to what GasApi._configure_signatures would set."""
        import ctypes
        from automatic_transfer_station import gas_network_probe

        dll = FakeGasDll()
        # Run probe so signatures get applied.
        gas_network_probe.probe_open_close(dll=dll, ip_address="192.168.0.200")

        self.assertEqual([ctypes.c_short, ctypes.c_char_p], dll.GA_Open.argtypes)
        self.assertEqual(ctypes.c_int, dll.GA_Open.restype)
        self.assertEqual([], dll.GA_Close.argtypes)
        self.assertEqual(ctypes.c_int, dll.GA_Close.restype)

    def test_successful_probe_only_opens_and_closes(self):
        from automatic_transfer_station import gas_network_probe

        dll = FakeGasDll()

        result = gas_network_probe.probe_open_close(
            dll=dll,
            ip_address="192.168.0.200",
            open_mode=0,
        )

        self.assertEqual(0, result.open_result)
        self.assertEqual(0, result.close_result)
        self.assertEqual([(0, b"192.168.0.200")], dll.GA_Open.calls)
        self.assertEqual([()], dll.GA_Close.calls)

    def test_failed_open_does_not_close(self):
        from automatic_transfer_station import gas_network_probe

        dll = FakeGasDll(open_result=-6)

        result = gas_network_probe.probe_open_close(
            dll=dll,
            ip_address="192.168.0.200",
            open_mode=0,
        )

        self.assertEqual(-6, result.open_result)
        self.assertIsNone(result.close_result)
        self.assertEqual([(0, b"192.168.0.200")], dll.GA_Open.calls)
        self.assertEqual([], dll.GA_Close.calls)


if __name__ == "__main__":
    unittest.main()
