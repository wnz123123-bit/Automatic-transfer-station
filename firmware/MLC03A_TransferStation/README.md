# ATS-LASER-2.1 / MLC03A Transfer Station

This is a **new** matching host/firmware implementation. It does not modify NAS
originals or implement legacy PWM/PRF commands. No hardware connection/upload is
part of its software validation.

## Electrical and startup contract

| Pin | Role | Contract |
|---|---|---|
| 9 | Comp | **HIGH at BOOT**, as required for manufacturer startup; OFF/ESTOP LOW |
| 13 | LED | Mirrors commanded Comp GPIO, not optical feedback |
| 4 | Independent inhibit sense | `INPUT_PULLUP`; LOW = closed/qualified, disconnected HIGH = unsafe |
| 7 | Motion-board trigger | `INPUT_PULLUP`; active LOW, disconnected HIGH = inactive |

`INTERLOCK_WIRED=false` and `TRIGGER_WIRED=false` are shipped deliberately.
No ARM/FOLLOW/FIRE is possible with the current Comp-only wiring. Pin 4 senses
an **independent physical inhibit**; merely connecting pin 4 to ground does not
create one. Change capability constants only after qualifying the actual wiring
and its failure behavior. FOLLOW additionally requires qualified pin-7 wiring.

The Comp latch is set HIGH before pin 9 becomes an output. Bootloader/reset time
is outside sketch control. Opening or closing the serial port may assert DTR,
reset the Uno and restore startup HIGH. Even an acknowledged OFF before close
does not prove LOW afterward. Independent inhibit must cover this entire path.

An operator must observe the manufacturer's startup self-check, explicitly send
OFF, and invoke `confirm_startup_check()` before ARM. CHECK records a human
attestation; firmware does not measure successful self-check. A new session,
reset, ESTOP or fault clears the attestation. Ordinary OFF preserves it but always
requires explicit ARM again. Window completion also latches DISARMED.

## Public host calls

`LaserController(serial_factory=None)` exposes blocking **worker-thread** calls:

- `connect(port)`, `disconnect()`, `off()`, `emergency_off()`
- `confirm_startup_check()`, `arm()`, `follow(window_ms)`, `fire(duration_ms)`
- `keepalive()`, `poll()`, `check_ready(require_trigger=True)`
- `get_fire_completion(token=None)` (snapshot only; no serial I/O)

Calls return immutable `LaserStatus` or raise a `LaserError` subclass. Read
`.status` and `.connected`; never infer capabilities from display text. Status
includes state, version, wiring flags, interlock/trigger inputs, commanded Comp,
human confirmation, watchdog/window limits, boot/session nonces, sequence/report
IDs, monotonic last-seen time, fault and host stop generation.

`fire()` returns a status carrying `fire_token: LaserFireToken`, allocated with
the actual command sequence under the writer lock. Never predict the sequence
from a prior poll. Token fields are `fire_sequence`, `started_generation`,
`boot_nonce`, `session_nonce`. A short FIRE may already be DISARMED when its ACK
arrives; its explicit firmware completion evidence is required in that case.
An exception from `fire()` is not a successful receipt.

`get_fire_completion(token)` returns `LaserFireCompletion` or None. It contains
the token fields plus `completed_generation = started_generation + 1` and a
`.token` property. Only matched natural FIRE expiry creates a receipt. It must
still belong to a fresh connected DISARMED/LOW status and the current generation.
OFF/ESTOP, fault, disconnect/reset, or a new ARM/FOLLOW/FIRE invalidates it. A
caller may adopt the completed generation only after matching its returned FIRE
token and its own job ownership/cancellation checks. FOLLOW window expiry has
no FIRE receipt and remains an execution failure for continuous cutting.

ARM/FOLLOW/FIRE/KEEP accept the optional keyword `expected_stop_generation`.
An automated job **must** pass its expected generation: OFF/ESTOP cancels pending
requests and increments it. A queued old ARM is rejected atomically before its
serial write. New calls without this guard represent explicit new caller intent;
the driver cannot infer that they belong to an old motion job. The motion runner
must also cancel its job when the user presses OFF. A runner's intentional OFF
may adopt the generation returned by that OFF, while retaining its own job
cancellation/ownership check.

`check_ready` accepts fresh DISARMED/ARMED/FOLLOW/FIRING and checks physical
capabilities; it does not require ARMED or perform human confirmation. The caller
checks phase-specific state and profile qualification. **STATUS/poll/check_ready
never renew the emission lease.** There is no automatic heartbeat thread.

## Lease and timing

ARM starts a **500 ms** lease. Only KEEP renews it; FOLLOW/FIRE do not. All of
ARMED, FOLLOW and FIRING expire DARK/FAULT on lease loss or unsafe interlock.
FOLLOW gates Comp directly on active-LOW trigger while this lease and its bounded
window remain valid. FIRE is a bounded HIGH **millisecond envelope**, not one
picosecond optical pulse. Both accept integer 1..60000 ms. Neither changes PRF.

FOLLOW additionally requires an inactive trigger baseline. The host rejects a
cached active input; firmware re-reads pin 7 when accepting FOLLOW and rejects an
already active LOW with `TRIGGER_ACTIVE`/FAULT and Comp LOW. An independent
inhibit sense reporting good does not imply that the motion trigger is inactive.

The runner should send KEEP about every 75 ms (less than 100 ms), coupled to
successful motion/DLL polling. An unrelated keepalive timer could keep emission
enabled during a hung motion call and is not acceptable. Dark travel remains
DISARMED and polls STATUS only. A profile must compare `watchdog_ms` to its
commissioned maximum-loss budget and enforce the exposure window limit.

500 ms is the firmware's programmed watchdog and a **bench-qualification target**,
not a measured or guaranteed optical safety bound. GPIO scheduling, input/relay
response, reset behavior, laser shutdown latency and the independent inhibit need
separate qualification. Host serial reads/writes/ACK waits are bounded, but an
uncooperative OS/driver or hung MCU cannot be made electrically safe by Python.

## Wire protocol (ASCII, 115200 baud, LF or CRLF)

On boot: `L2 BOOT <boot8hex> ATS-LASER-2.1`

Handshake: `L2 HELLO <session32hex>`; ACK has sequence 0 and command HELLO.
Normal requests:

```text
L2 <boot8hex> <session32hex> <seq> <COMMAND> [integer_ms]
```

Commands: STATUS, OFF, ESTOP, CHECK, ARM, FOLLOW, FIRE, KEEP.
Seq is strictly increasing unsigned 32-bit, starting at 1, with no wrap/retry.
Only FOLLOW/FIRE have an argument. Unknown, malformed, partial, overflowing,
wrong-session and replayed requests fail DARK; no automatic resume occurs.
Bare OFF/ESTOP always work, even without a session. Priority host stops prepend
LF to terminate any partial frame; normal acknowledged OFF/ESTOP use nonces/seq.

ACK and unsolicited EVENT have exactly these 22 space-separated fields:

```text
L2 ACK|EVENT boot session seq command OK|ERR report_id state version iw tw io trigger comp checked watchdog_ms remaining_ms max_window_ms fault fire_seq completion_reason
```

Bits are 0/1; fault is NONE or a stable uppercase code. EVENT command is EVENT
and reports only a transition to DISARMED/FAULT. ACK must match boot, session,
sequence and command. Report IDs strictly increase for the boot. The host rejects
old firmware/unknown fields/states/inconsistent statuses instead of enabling a
legacy fallback. Late ACKs from explicitly cancelled requests do not update state
or freshness. `comp` is a commanded GPIO state, never optical power feedback.

`fire_seq` is nonzero only during FIRING or after its natural expiry.
`completion_reason` is `FIRE_DONE` only for natural FIRING expiry, otherwise
`NONE`. Natural expiry preserves the exact FIRE sequence while latching
DISARMED/LOW. OFF (including bare OFF), ESTOP, any fault and new ARM clear both
fields. Interlock and watchdog checks take precedence over expiry. STATUS does
not clear/renew the receipt or lease. Version 2.0 is rejected, with no fallback.

A KEEP already in transit when FIRE completes may acknowledge the same
DISARMED/FIRE_DONE receipt without renewing `leaseAt`, arming, or clearing startup
confirmation. This exception is limited to natural FIRE completion with good
inhibit and startup confirmation; an OFF/FOLLOW-expired/fault state cannot use it.

EVENT sequence is firmware `lastSeq`, which may trail a host command still in
transit. It must lie between the highest sequence in an accepted ACK/EVENT and
the latest host-issued sequence, inclusive. Boot/session and strictly increasing
report IDs are still required. A valid fault EVENT cancels pending work while
preserving the firmware fault reason instead of treating this race as link loss.

The firmware journals its boot counter in EEPROM bytes 0..95 (eight 12-byte slots,
commit marker written last). Corrupt/exhausted storage fails closed; no fallback
nonce is invented. Do not restore an old EEPROM image during service: doing so
would invalidate the boot-counter anti-replay assumption. Eight distinct session
nonces are remembered per boot; repeats and exhaustion fail closed. A controlled
reset under independent inhibit starts a new boot nonce. Session nonces provide
freshness, not cryptographic authentication against a hostile serial peer.

RX uses a fixed 128-byte buffer. Nonblocking TX uses one 224-byte active frame
and one 224-byte queued ACK. EVENTs coalesce until both slots are empty; a STATUS
ACK racing an in-flight short-FIRE EVENT therefore fits without TX_BUSY. OFF/
ESTOP pulls LOW before generating its ACK and may replace a queued ACK from work
the host cancelled. Further ordinary commands exceeding the single ACK slot fail
DARK/TX_BUSY. No dynamic Arduino String is used. The loop has
bounded RX work and a 100 ms partial-line timeout. Watchdog/window arithmetic uses
unsigned `millis()` differences across rollover. No serial query extends a lease.

## Software validation only

Host tests: `py -3.13 -m pytest tests/test_laser_controller.py -q` (fake serial).

Compile with installed AVR core 1.8.8, with all build output in a new TEMP folder:

```powershell
$laserBuildPath = Join-Path $env:TEMP ('ats-laser-build-' + [guid]::NewGuid().ToString('N'))
& 'C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe' compile --fqbn arduino:avr:uno --build-path $laserBuildPath firmware\MLC03A_TransferStation
```

Validation is UNO compilation plus a Python protocol peer, not dynamic execution
of the actual `.ino` watchdog/parser. Compilation and fake-serial tests do not validate electrical behavior, optical
shutdown, real USB reset timing or independent inhibit qualification. No upload,
port discovery or hardware probe is needed for these software checks.
