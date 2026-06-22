import sys, ctypes
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


class FakeXInputDll:
    def __init__(self, *, connected=True, packet=1, lx=0, ly=0, rx=0, ry=0, lt=0, rt=0, buttons=0):
        self.cfg = dict(connected=connected, packet=packet, lx=lx, ly=ly, rx=rx, ry=ry, lt=lt, rt=rt, buttons=buttons)
        self.calls = []

    def XInputGetState(self, index, state_ptr):
        self.calls.append(int(index))
        if not self.cfg["connected"]:
            return 1167
        st = state_ptr._obj
        st.dwPacketNumber = self.cfg["packet"]
        g = st.Gamepad
        g.wButtons = self.cfg["buttons"]
        g.bLeftTrigger = self.cfg["lt"]; g.bRightTrigger = self.cfg["rt"]
        g.sThumbLX = self.cfg["lx"]; g.sThumbLY = self.cfg["ly"]
        g.sThumbRX = self.cfg["rx"]; g.sThumbRY = self.cfg["ry"]
        return 0


class GamepadXInputTests(unittest.TestCase):
    def test_button_constants_and_pressed(self):
        from automatic_transfer_station.gamepad_xinput import BUTTON, GamepadSnapshot
        self.assertEqual(BUTTON["A"], 0x1000)
        self.assertEqual(BUTTON["START"], 0x0010)
        self.assertEqual(BUTTON["BACK"], 0x0020)
        self.assertEqual(BUTTON["DPAD_LEFT"], 0x0004)
        snap = GamepadSnapshot(True, 1, 0, 0, 0, 0, 0, 0, BUTTON["A"] | BUTTON["START"])
        self.assertTrue(snap.pressed(BUTTON["A"]))
        self.assertTrue(snap.pressed(BUTTON["START"]))
        self.assertFalse(snap.pressed(BUTTON["B"]))

    def test_normalization_signs_and_ranges(self):
        from automatic_transfer_station.gamepad_xinput import GamepadReader
        reader = GamepadReader(FakeXInputDll(lx=-32768, ly=32767, rt=255, lt=128))
        snap = reader.read_state()
        self.assertAlmostEqual(snap.lx, -1.0, places=4)
        self.assertAlmostEqual(snap.ly, 1.0, places=4)
        self.assertAlmostEqual(snap.rt, 1.0, places=4)
        self.assertAlmostEqual(snap.lt, 128 / 255, places=4)
        self.assertTrue(snap.connected)

    def test_disconnected_returns_not_connected_snapshot(self):
        from automatic_transfer_station.gamepad_xinput import GamepadReader
        snap = GamepadReader(FakeXInputDll(connected=False)).read_state()
        self.assertFalse(snap.connected)
        self.assertEqual(snap.lx, 0.0)

    def test_polls_single_cached_index_after_connect(self):
        from automatic_transfer_station.gamepad_xinput import GamepadReader
        dll = FakeXInputDll(connected=True)
        orig = dll.XInputGetState
        def only2(index, ptr):
            if index != 2:
                dll.calls.append(int(index)); return 1167
            return orig(index, ptr)
        dll.XInputGetState = only2
        reader = GamepadReader(dll)
        reader.read_state()                 # scans 0,1,2 -> discovers index 2
        dll.calls.clear()
        reader.read_state()                 # must poll ONLY index 2 now
        self.assertEqual(dll.calls, [2])

    def test_cached_index_cleared_on_disconnect(self):
        from automatic_transfer_station.gamepad_xinput import GamepadReader
        dll = FakeXInputDll(connected=True)
        orig = dll.XInputGetState
        def only2(index, ptr):
            if index != 2:
                dll.calls.append(int(index)); return 1167
            return orig(index, ptr)
        dll.XInputGetState = only2
        reader = GamepadReader(dll)
        reader.read_state()                 # discovers index 2; _index == 2

        # Flip to disconnected
        dll.cfg["connected"] = False
        dll.XInputGetState = dll.__class__.XInputGetState.__get__(dll)  # restore to plain method

        snap = reader.read_state()          # cached slot now reports disconnected
        self.assertFalse(snap.connected)
        self.assertIsNone(reader._index)

        # Next no-arg call must rescan all 4 slots
        dll.calls.clear()
        reader.read_state()
        self.assertEqual(dll.calls, [0, 1, 2, 3])
