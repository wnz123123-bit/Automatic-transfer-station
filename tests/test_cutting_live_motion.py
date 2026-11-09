"""Qualified coordinated gate tests: injected APIs only, never real DLL/hardware."""
import ctypes
from dataclasses import FrozenInstanceError, replace
import threading

import pytest

from automatic_transfer_station.controller_base import MotionControllerError
from automatic_transfer_station.gas_api import GasApi
from automatic_transfer_station.gas_controller import CuttingGateOutput, GasMotionController
from test_cutting_motion import CuttingApi, coordinate_dll
from test_gas_api import FakeDll, FakeFunction


class LiveApi(CuttingApi):
    supports_cutting_gate = True

    def __init__(self):
        super().__init__()
        self.outputs = {}
        self.fifo = []
        self.read_override = None
        self.occurrences = {}
        self.fail_at = None

    def record(self, name, *args):
        self.occurrences[name] = self.occurrences.get(name, 0) + 1
        result = super().record(name, *args)
        return -7 if self.fail_at == (name, self.occurrences[name]) else result

    def set_ext_do_bit(self, card, bit, level):
        result = self.record("set_ext_do_bit", card, bit, level)
        if not result:
            self.outputs[card, bit] = level
        return result

    def get_ext_do_bit(self, card, bit):
        if self.record("get_ext_do_bit", card, bit):
            raise RuntimeError("readback transport failure")
        return self.outputs.get((card, bit), 0) if self.read_override is None else self.read_override

    def buf_io(self, crd, kind, card, mask, value, fifo, segment):
        self.fifo.append(("io", card, mask, value))
        return self.record("buf_io", crd, kind, card, mask, value, fifo, segment)

    def line_xy(self, *args):
        result = super().line_xy(*args)
        self.fifo.append(("line", args[1], args[2]))
        return result

    def crd_clear(self, *args):
        result = super().crd_clear(*args)
        if not result:
            self.fifo.clear()
        return result

    def finish(self):
        for command in self.fifo:
            if command[0] == "line":
                self.positions[5], self.positions[6] = command[1:]
            else:
                _, card, mask, value = command
                self.outputs[card, mask.bit_length() - 1] = int(bool(value & mask))
        self.running = 0
        self.statuses[5] = self.statuses[6] = 0


@pytest.fixture
def rig():
    api = LiveApi()
    controller = GasMotionController(api, cutting_qualified=True)
    controller.is_open = True
    # These deliberately arbitrary values are TEST DATA, not a machine pin map.
    output = CuttingGateOutput(3, 7, 42, True)
    controller.configure_cutting_gate(output, qualified=True)
    owner = object()
    controller.acquire_cutting(owner)
    api.calls.clear()
    return controller, api, owner, output


def prepare(rig, **changes):
    c, _, owner, output = rig
    args = dict(cut_start_um=(300.0, 0.0), cut_end_um=(600.0, 0.0),
                lead_out_um=(900.0, 0.0), speed_um_s=1000.0, output=output)
    args.update(changes)
    c.prepare_cutting_line(owner, **args)


def writes(api):
    return [call for call in api.calls if call[0] not in
            ("get_status", "get_prf_pos", "crd_status", "get_ext_do_bit")]


def gate_dll():
    dll = coordinate_dll()
    for name in ("GA_BufIO", "GA_SetExtDoBit", "GA_GetExtDoBit"):
        setattr(dll, name, FakeFunction())
    return dll


def test_exact_header_io_signatures_and_return_marshalling():
    dll = gate_dll()
    api = GasApi(dll)
    assert api.supports_buffered_io and api.supports_external_do and api.supports_cutting_gate
    assert dll.GA_BufIO.argtypes == [ctypes.c_int16, ctypes.c_uint16, ctypes.c_uint16,
                                    ctypes.c_uint16, ctypes.c_uint16, ctypes.c_int16, ctypes.c_int32]
    assert dll.GA_SetExtDoBit.argtypes == [ctypes.c_int16, ctypes.c_int16, ctypes.c_uint16]
    assert dll.GA_GetExtDoBit.argtypes == [ctypes.c_int16, ctypes.c_int16, ctypes.POINTER(ctypes.c_uint16)]
    assert dll.GA_BufIO.restype == dll.GA_SetExtDoBit.restype == dll.GA_GetExtDoBit.restype == ctypes.c_int
    assert api.buf_io(1, 42, 3, 0x8000, 0x8000, 0, 9) == 0
    assert dll.GA_BufIO.calls == [(1, 42, 3, 0x8000, 0x8000, 0, 9)]
    assert api.set_ext_do_bit(3, 15, 1) == 0
    assert dll.GA_SetExtDoBit.calls == [(3, 15, 1)]

    def read(card, bit, result):
        result._obj.value = 1
        return 0
    dll.GA_GetExtDoBit.side_effect = read
    assert api.get_ext_do_bit(3, 15) == 1
    dll.GA_GetExtDoBit.side_effect = lambda *_: -7
    with pytest.raises(RuntimeError, match="-7"):
        api.get_ext_do_bit(3, 15)


@pytest.mark.parametrize("missing", ["GA_BufIO", "GA_SetExtDoBit", "GA_GetExtDoBit", "all"])
def test_optional_io_exports_do_not_break_legacy_or_motion_api(missing):
    dll = gate_dll() if missing != "all" else FakeDll()
    if missing != "all":
        delattr(dll, missing)
    api = GasApi(dll)
    assert not api.supports_cutting_gate
    assert api.stop(48) == 0
    if missing in ("GA_BufIO", "all"):
        with pytest.raises(RuntimeError, match="unavailable"):
            api.buf_io(1, 42, 3, 1, 0)
    else:
        with pytest.raises(RuntimeError, match="unavailable"):
            api.set_ext_do_bit(3, 7, 0)


@pytest.mark.parametrize("args", [(-1, 0, 12, True), (32768, 0, 12, True),
    (0, -1, 12, True), (0, 16, 12, True), (0, True, 12, True),
    (0, 0, -1, True), (0, 0, 65536, True), (0, 0, 12, 1)])
def test_explicit_gate_mapping_rejects_overflow_nonbool_and_multiple_bits(args):
    with pytest.raises(ValueError):
        CuttingGateOutput(*args)


def test_no_default_mapping_and_frozen_singlebit_profile():
    with pytest.raises(TypeError):
        CuttingGateOutput()
    for bit in (0, 15):
        output = CuttingGateOutput(2, bit, 99, False)
        assert output.mask == 1 << bit
        assert output.off_level == 1
        with pytest.raises(FrozenInstanceError):
            output.bit_index = 1


def test_wrappers_prevent_silent_width_wrap_and_invalid_logic_levels():
    api = GasApi(gate_dll())
    for call in (lambda: api.buf_io(1, 65536, 0, 1, 0),
                 lambda: api.buf_io(1, 12, 0, 65536, 0),
                 lambda: api.set_ext_do_bit(32768, 1, 0),
                 lambda: api.set_ext_do_bit(0, 1, 2),
                 lambda: api.get_ext_do_bit(-1, 0)):
        with pytest.raises(ValueError):
            call()


def test_gate_not_qualified_by_exports_or_injection_or_default_configuration():
    api = LiveApi()
    c = GasMotionController(api)
    c.is_open = True
    output = CuttingGateOutput(3, 7, 42, True)
    c.configure_cutting_gate(output)
    assert c.qualified_cutting_gate is None
    with pytest.raises(MotionControllerError):
        c.configure_cutting_gate(output, qualified=True)
    c.set_cutting_qualified(True)
    c.configure_cutting_gate(output)
    owner = object()
    c.acquire_cutting(owner)
    with pytest.raises(MotionControllerError, match="qualified"):
        c.prepare_cutting_line(owner, (300, 0), (600, 0), (900, 0), 1000, output)
    assert writes(api) == []


@pytest.mark.parametrize("method", ["buf_io", "set_ext_do_bit", "get_ext_do_bit"])
def test_missing_gate_capability_refuses_qualification_without_io(method):
    api = LiveApi()
    setattr(api, method, None)
    c = GasMotionController(api, cutting_qualified=True)
    c.is_open = True
    with pytest.raises(MotionControllerError, match="capability"):
        c.configure_cutting_gate(CuttingGateOutput(3, 7, 42, True), qualified=True)
    assert writes(api) == []


@pytest.mark.parametrize("high", [True, False])
def test_prepare_exact_fifo_order_constant_vector_velocity_and_no_start(rig, high):
    c, api, owner, original = rig
    c.release_cutting(owner)
    output = replace(original, active_high=high)
    c.configure_cutting_gate(output, qualified=True)
    owner = object()
    c.acquire_cutting(owner)
    api.outputs[3, 7] = 1 - output.off_level  # Initially active, for BOTH polarities.
    api.calls.clear()
    prepare((c, api, owner, output))
    off = output.mask if output.off_level else 0
    on = output.mask ^ off
    assert writes(api) == [
        ("set_ext_do_bit", 3, 7, output.off_level),
        ("crd_clear", 1, 0), ("set_crd_prm", 1),
        ("buf_io", 1, 42, 3, 128, off, 0, 1),
        ("line_xy", 1, 672898, -250000, 50.0, 0.1, 50.0, 0, 2),
        ("buf_io", 1, 42, 3, 128, on, 0, 3),
        ("line_xy", 1, 657898, -250000, 50.0, 0.1, 50.0, 0, 4),
        ("buf_io", 1, 42, 3, 128, off, 0, 5),
        ("line_xy", 1, 642898, -250000, 50.0, 0.1, 0.0, 0, 6),
    ]
    assert api.outputs[3, 7] == output.off_level
    assert list(api.prm.profile) == [5, 6, 0, 0, 0, 0, 0, 0]
    assert api.prm.setOriginFlag == 1 and list(api.prm.originPos) == [0] * 8
    assert c.get_cutting_lease_status(owner).state == "prepared"
    assert c.cutting_move_status(owner) is False
    assert api.running == 0
    c.start_prepared_cutting_line(owner)
    assert api.calls.count(("crd_start", 1, 0)) == 1
    assert not c.cutting_move_status(owner)
    with pytest.raises(MotionControllerError, match="no prepared"):
        c.start_prepared_cutting_line(owner)
    api.finish()
    assert c.cutting_move_status(owner)
    assert api.outputs[3, 7] == output.off_level
    assert c.get_cutting_pose() == pytest.approx((900, 0))
    c.release_cutting(owner)


def test_physical_acceleration_and_quantized_lead_distances(rig):
    c, api, owner, output = rig
    assert c.cutting_acceleration_um_s2 == pytest.approx(2000)
    assert 1000**2 / (2 * c.cutting_acceleration_um_s2) == 250
    with pytest.raises(MotionControllerError, match="too short"):
        prepare(rig, cut_start_um=(249, 0))
    assert writes(api) == []
    prepare(rig, cut_start_um=(250, 0), lead_out_um=(850, 0))


@pytest.mark.parametrize("direction", [(1, 1), (-1, 0), (0, -1)])
def test_live_vector_speed_is_not_multiplied_by_diagonal_axis_count(rig, direction):
    c, api, owner, _ = rig
    points = [tuple(distance * component for component in direction) for distance in (300, 600, 900)]
    prepare(rig, cut_start_um=points[0], cut_end_um=points[1], lead_out_um=points[2])
    lines = [call for call in api.calls if call[0] == "line_xy"]
    assert [call[4] for call in lines] == [50.0, 50.0, 50.0]
    assert [call[6] for call in lines] == [50.0, 50.0, 0.0]
    c.start_prepared_cutting_line(owner)
    api.finish()
    assert c.cutting_move_status(owner)
    assert c.get_cutting_pose() == pytest.approx(points[-1])


@pytest.mark.parametrize("changes", [
    {"cut_start_um": (300, 5)}, {"cut_end_um": (300, 0)},
    {"cut_start_um": (-300, 0)}, {"lead_out_um": (500, 0)},
    {"lead_out_um": (600, 0)}, {"lead_out_um": (849, 0)},
    {"cut_end_um": (20000, 0)}, {"lead_out_um": (12500, 0)},
    {"cut_start_um": (float("nan"), 0)}, {"lead_out_um": (float("inf"), 0)},
    {"speed_um_s": 0}, {"speed_um_s": -1}, {"speed_um_s": float("nan")},
    {"speed_um_s": 5001}, {"cut_start_um": (0, 0, 9)},
])
def test_complete_preflight_precedes_every_output_and_fifo_mutation(rig, changes):
    c, api, _, _ = rig
    with pytest.raises(MotionControllerError):
        prepare(rig, **changes)
    assert writes(api) == []
    assert not any(call[0] == "get_ext_do_bit" for call in api.calls)


@pytest.mark.parametrize("axis,value", [(5, float("nan")), (6, float("inf")), (7, -2_000_000)])
def test_preflight_rejects_unsafe_current_or_partner_position_before_io(rig, axis, value):
    _, api, _, _ = rig
    api.positions[axis] = value
    with pytest.raises(MotionControllerError):
        prepare(rig)
    assert writes(api) == []


@pytest.mark.parametrize("axis,status", [(5, 0x20), (6, 0x40), (7, 0x400), (9, 0x1000)])
def test_hard_limit_and_other_axis_motion_block_all_io(rig, axis, status):
    _, api, _, _ = rig
    api.statuses[axis] = status
    with pytest.raises(MotionControllerError):
        prepare(rig)
    assert writes(api) == []


@pytest.mark.parametrize("field,value", [("card_index", 4), ("bit_index", 8),
    ("buf_do_type", 12), ("active_high", False)])
def test_supplied_output_must_exactly_match_attested_mapping(rig, field, value):
    _, api, _, output = rig
    with pytest.raises(MotionControllerError, match="matching"):
        prepare(rig, output=replace(output, **{field: value}))
    assert writes(api) == []


@pytest.mark.parametrize("method,nth", [("set_ext_do_bit", 1), ("get_ext_do_bit", 1),
    ("crd_clear", 1), ("set_crd_prm", 1),
    ("buf_io", 1), ("buf_io", 2), ("buf_io", 3),
    ("line_xy", 1), ("line_xy", 2), ("line_xy", 3)])
def test_every_partial_prepare_failure_attempts_off_stop_clear_and_invalidates_owner(rig, method, nth):
    c, api, owner, output = rig
    api.fail_at = method, nth
    with pytest.raises(MotionControllerError):
        prepare(rig)
    assert ("stop", 48, 0) in api.calls
    assert ("crd_clear", 1, 0) in api.calls
    assert ("set_ext_do_bit", 3, 7, output.off_level) in api.calls
    assert not c.get_cutting_lease_status(owner).valid
    assert c.get_cutting_lease_status(owner).stop_confirmed
    assert not api.fifo
    assert not any(call[0] == "crd_start" for call in api.calls)


@pytest.mark.parametrize("failure", [-7, RuntimeError("IO link lost")])
def test_persistent_off_failure_preserves_recovery_interlock_and_trusted_mapping(rig, failure):
    c, api, owner, output = rig
    prepare(rig)
    api.failures["set_ext_do_bit"] = failure
    with pytest.raises(MotionControllerError):
        c.stop_cutting(owner)
    state = c.get_cutting_lease_status(owner)
    assert state.cleanup_required and state.can_retry_stop and not state.stop_confirmed
    with pytest.raises(MotionControllerError, match="recovery"):
        c.start_jog(3, 1)
    with pytest.raises(MotionControllerError, match="recovery"):
        c.configure_cutting_gate(replace(output, bit_index=8), qualified=True)
    api.failures.clear()
    c.stop_cutting(owner)
    assert c.get_cutting_lease_status(owner).stop_confirmed


@pytest.mark.parametrize("value", [1, 2, -1, None, True])
def test_bad_off_readback_aborts_preparation(rig, value):
    c, api, owner, _ = rig
    api.get_ext_do_bit = lambda *_: value
    with pytest.raises(MotionControllerError, match="OFF"):
        prepare(rig)
    assert not any(call[0] == "buf_io" for call in api.calls)
    assert c.get_cutting_lease_status(owner).cleanup_required


def test_prepared_state_blocks_normal_move_release_and_manual_mutations(rig):
    c, api, owner, output = rig
    prepare(rig)
    api.calls.clear()
    for action in (lambda: c.release_cutting(owner),
                   lambda: c.start_cutting_move(owner, (10, 0), 1),
                   lambda: prepare(rig), lambda: c.relative_move(9, 1, 1, 1),
                   lambda: c.configure_cutting_gate(output, qualified=True),
                   lambda: c.set_cutting_qualified(False), lambda: c.reset_board()):
        with pytest.raises(MotionControllerError):
            action()
    assert writes(api) == []
    assert c.get_cutting_lease_status(owner).valid


def test_cancel_prepared_line_clears_fifo_and_stale_owner_cannot_start_or_stop_next(rig):
    c, api, old, output = rig
    prepare(rig)
    c.stop_cutting(old)
    assert c.get_cutting_lease_status(old).stop_confirmed
    assert api.fifo == []
    new = object()
    c.acquire_cutting(new)
    prepare((c, api, new, output))
    api.calls.clear()
    for action in (lambda: c.start_prepared_cutting_line(old), lambda: c.stop_cutting(old)):
        with pytest.raises(MotionControllerError, match="stale"):
            action()
    assert writes(api) == []
    assert c.get_cutting_lease_status(new).state == "prepared"


@pytest.mark.parametrize("axis", range(1, 10))
def test_start_rechecks_all_axis_reference_after_follow_handshake(rig, axis):
    c, api, owner, _ = rig
    prepare(rig)
    api.positions[axis] = api.positions.get(axis, 0) + 5
    with pytest.raises(MotionControllerError, match="reference changed"):
        c.start_prepared_cutting_line(owner)
    assert not any(call[0] == "crd_start" for call in api.calls)
    assert not api.fifo
    assert c.get_cutting_lease_status(owner).stop_confirmed


def test_start_rechecks_output_off_and_final_completion_checks_off(rig):
    c, api, owner, output = rig
    prepare(rig)
    api.outputs[3, 7] = 1
    with pytest.raises(MotionControllerError, match="OFF"):
        c.start_prepared_cutting_line(owner)
    assert not any(call[0] == "crd_start" for call in api.calls)
    assert api.outputs[3, 7] == 0
    owner = object()
    c.acquire_cutting(owner)
    prepare((c, api, owner, output))
    c.start_prepared_cutting_line(owner)
    api.finish()
    api.outputs[3, 7] = 1  # Planner arrived but gate readback is unexpectedly active.
    with pytest.raises(MotionControllerError, match="OFF"):
        c.cutting_move_status(owner)
    assert api.outputs[3, 7] == 0
    assert not c.get_cutting_lease_status(owner).valid


def test_start_failure_cleans_queued_on_and_final_status_requires_final_target(rig):
    c, api, owner, output = rig
    prepare(rig)
    api.failures["crd_start"] = -7
    with pytest.raises(MotionControllerError):
        c.start_prepared_cutting_line(owner)
    assert api.fifo == [] and api.outputs[3, 7] == 0
    api.failures.clear()
    owner = object()
    c.acquire_cutting(owner)
    prepare((c, api, owner, output))
    c.start_prepared_cutting_line(owner)
    api.running = 0
    api.statuses[5] = api.statuses[6] = 0
    with pytest.raises(MotionControllerError, match="target"):
        c.cutting_move_status(owner)


@pytest.mark.parametrize("action", [lambda c: c.set_cutting_qualified(True),
    lambda c: c.set_cutting_qualified(False), lambda c: c.set_axis_calibrations(c.axis_calibrations),
    lambda c: c.reset_board(), lambda c: c.close_board(), lambda c: c.open_board()])
def test_gate_attestation_revoked_by_connection_and_configuration_operations(rig, action):
    c, _, owner, _ = rig
    c.release_cutting(owner)
    action(c)
    assert c.qualified_cutting_gate is None


def test_global_stop_and_close_attempt_off_even_when_stop_or_fifo_clear_fails(rig):
    c, api, owner, _ = rig
    prepare(rig)
    api.outputs[3, 7] = 1
    api.failures.update(stop=RuntimeError("stop failed"), crd_clear=RuntimeError("clear failed"))
    with pytest.raises(MotionControllerError):
        c.close_board()
    assert ("stop", 0xFFFF, 0) in api.calls and ("close",) in api.calls
    assert api.outputs[3, 7] == 0
    assert c.get_cutting_lease_status(owner).cleanup_required
    assert c.qualified_cutting_gate is None


def test_off_is_reasserted_after_stop_and_clear_to_defeat_queued_on_race(rig):
    c, api, owner, _ = rig
    prepare(rig)
    original_stop = api.stop

    def stop(*args):
        result = original_stop(*args)
        api.outputs[3, 7] = 1  # ON boundary raced the first direct OFF.
        return result
    api.stop = stop
    api.calls.clear()
    c.stop_cutting(owner)
    assert writes(api) == [("set_ext_do_bit", 3, 7, 0), ("stop", 48, 0),
                           ("crd_clear", 1, 0), ("set_ext_do_bit", 3, 7, 0)]
    assert api.outputs[3, 7] == 0


@pytest.mark.parametrize("failure", [-7, RuntimeError("partial reset")])
def test_partial_reset_failure_still_restores_active_low_off(rig, failure):
    c, api, owner, output = rig
    c.release_cutting(owner)
    output = replace(output, active_high=False)
    c.configure_cutting_gate(output, qualified=True)

    def reset():
        api.outputs[3, 7] = 0  # Reset default would activate this polarity.
        if isinstance(failure, Exception):
            raise failure
        return failure
    api.reset = reset
    with pytest.raises(MotionControllerError):
        c.reset_board()
    assert api.outputs[3, 7] == 1
    assert c.qualified_cutting_gate is None and not c.cutting_qualified


@pytest.mark.parametrize("fault", ["axis_alarm", "status_transport", "position_nan"])
def test_live_poll_fault_attempts_board_off_stop_and_fifo_cleanup(rig, fault):
    c, api, owner, _ = rig
    prepare(rig)
    c.start_prepared_cutting_line(owner)
    api.outputs[3, 7] = 1
    if fault == "axis_alarm":
        api.statuses[6] |= 2
    elif fault == "status_transport":
        def failed_status(_):
            raise RuntimeError("axis status lost")
        api.get_status = failed_status
    else:
        api.positions[5] = float("nan")
    with pytest.raises(MotionControllerError):
        c.cutting_move_status(owner)
    assert ("stop", 48, 0) in api.calls
    assert api.outputs[3, 7] == 0 and not api.fifo
    assert not c.get_cutting_lease_status(owner).valid


def test_prepare_and_cancel_are_serialized_with_no_host_sleep_or_start(rig):
    c, api, owner, _ = rig
    entered, proceed, stopped = threading.Event(), threading.Event(), threading.Event()
    original_buf = api.buf_io
    failures = []

    def buf(*args):
        entered.set()
        assert proceed.wait(2)
        return original_buf(*args)
    api.buf_io = buf

    def queue():
        try:
            prepare(rig)
        except Exception as exc:
            failures.append(exc)

    def cancel():
        try:
            c.stop_cutting(owner)
        except Exception as exc:
            failures.append(exc)
        finally:
            stopped.set()

    worker = threading.Thread(target=queue)
    worker.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=cancel)
    stopper.start()
    assert not stopped.wait(0.03)
    proceed.set()
    worker.join(2)
    stopper.join(2)
    assert not worker.is_alive() and not stopper.is_alive()
    assert failures == [] and not api.fifo
    assert not any(call[0] == "crd_start" for call in api.calls)
