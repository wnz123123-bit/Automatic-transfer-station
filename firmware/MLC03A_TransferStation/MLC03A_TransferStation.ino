/* ATS-LASER-2.1: new firmware, not an edit of any NAS/original sketch.
 * Arduino Uno; no PWM/PRF commands. FIRE is a millisecond HIGH envelope.
 * BOOT Comp HIGH is REQUIRED for manufacturer startup. Independent physical
 * inhibit must protect reset/bootloader/startup/DTR; software cannot do so.
 * The 500 ms software watchdog is a bench-qualification target, not a promised
 * optical shutdown bound. No STATUS/HELLO command renews an emission lease.
 */
#include <Arduino.h>
#include <EEPROM.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static const char VERSION[] = "ATS-LASER-2.1";
static const uint8_t COMP_PIN = 9, LED_PIN = 13;
static const uint8_t INTERLOCK_PIN = 4, TRIGGER_PIN = 7;
// Change ONLY after qualifying a REAL independent inhibit + its sense contact.
// Pin 4 is sense only; it cannot independently inhibit the laser by itself.
static const bool INTERLOCK_WIRED = false;
static const bool TRIGGER_WIRED = false;
static const uint32_t WATCHDOG_MS = 500;
static const uint32_t MAX_WINDOW_MS = 60000;
static const uint32_t PARTIAL_COMMAND_MS = 100;

enum State { BOOT_HOLD, DISARMED, ARMED, FOLLOW, FIRING, FAULT };
static State state = BOOT_HOLD;
static bool compHigh = true, startupChecked = false;
static bool liveInterlock = false, liveTrigger = false;
static const char *fault = "NONE";
static uint32_t bootCounter = 0, lastSeq = 0, reportId = 0;
static uint32_t leaseAt = 0, windowAt = 0, windowMs = 0;
static uint32_t fireSeq = 0;
static bool fireCompleted = false;
static char bootNonce[9] = "00000000", session[33] = "";
// Never recycle a session nonce within this boot, including after takeover.
// Bounded history fails closed when full; resetting advances the EEPROM nonce.
static char usedSessions[8][33];
static uint8_t usedSessionCount = 0;
static bool nonceGood = false, reportDirty = false;
// One in-flight frame plus ONE queued ACK. EVENTs are coalesced until both
// drain. This covers a STATUS racing a short FIRE expiry EVENT without blocking
// safetyTick. A priority stop may replace the queued ACK (its host cancels it).
static char rx[128], tx[224], queuedTx[224];
static uint8_t rxUsed = 0;
static uint16_t txUsed = 0, txSent = 0;
static uint16_t queuedUsed = 0;
static bool dropping = false, carriageReturn = false;
static uint32_t rxStarted = 0;

static bool active() { return state == ARMED || state == FOLLOW || state == FIRING; }

static void setComp(bool high) {
  compHigh = high;
  digitalWrite(COMP_PIN, high ? HIGH : LOW);
  digitalWrite(LED_PIN, high ? HIGH : LOW);
}

static void stopDark(const char *why, bool isFault) {
  setComp(false);
  state = isFault ? FAULT : DISARMED;
  fault = isFault ? why : "NONE";
  windowMs = 0;
  fireSeq = 0;
  fireCompleted = false;
  if (isFault) startupChecked = false;
  reportDirty = true;
}

static void finishWindow() {
  // Capture only a natural FIRING expiry. OFF/fault/FOLLOW never earn this
  // receipt, even if they race the envelope deadline. stopDark invalidates it.
  uint32_t completed = state == FIRING ? fireSeq : 0;
  stopDark("NONE", false);
  fireSeq = completed;
  fireCompleted = completed != 0;
}

static const char *stateName() {
  switch (state) {
    case BOOT_HOLD: return "BOOT_HOLD";
    case DISARMED: return "DISARMED";
    case ARMED: return "ARMED";
    case FOLLOW: return "FOLLOW";
    case FIRING: return "FIRING";
    default: return "FAULT";
  }
}

static void safetyTick() {
  liveInterlock = INTERLOCK_WIRED && digitalRead(INTERLOCK_PIN) == LOW;
  liveTrigger = TRIGGER_WIRED && digitalRead(TRIGGER_PIN) == LOW;
  if (!active()) return;
  const uint32_t now = millis();
  if (!liveInterlock) { stopDark("INTERLOCK", true); return; }
  // Unsigned subtraction remains correct across millis() rollover.
  if ((uint32_t)(now - leaseAt) >= WATCHDOG_MS) { stopDark("WATCHDOG", true); return; }
  if ((state == FOLLOW || state == FIRING) && (uint32_t)(now - windowAt) >= windowMs) {
    finishWindow(); return; // Latched DISARMED, never ARMED.
  }
  if (state == FOLLOW) {
    if (!TRIGGER_WIRED) { stopDark("TRIGGER", true); return; }
    setComp(liveTrigger);
  } else if (state == FIRING) setComp(true);
  else setComp(false);
}

// Persist a monotonically increasing boot nonce before accepting any session.
// Eight 12-byte journal slots in EEPROM [0,96); commit magic is written last.
// A torn update leaves an older committed slot. A committed nonce is never reused.
static uint32_t read32(int address) {
  uint32_t value = 0;
  for (uint8_t i = 0; i < 4; ++i) value |= (uint32_t)EEPROM.read(address + i) << (8 * i);
  return value;
}
static void write32(int address, uint32_t value) {
  for (uint8_t i = 0; i < 4; ++i) EEPROM.update(address + i, (uint8_t)(value >> (8 * i)));
}
static bool nextBootNonce() {
  const uint32_t magic = 0x41545332UL;
  uint32_t largest = 0;
  int bestSlot = -1;
  bool erased = true;
  for (uint8_t slot = 0; slot < 8; ++slot) {
    int base = slot * 12;
    uint32_t count = read32(base), inverse = read32(base + 4), mark = read32(base + 8);
    if (count != 0xFFFFFFFFUL || inverse != 0xFFFFFFFFUL || mark != 0xFFFFFFFFUL) erased = false;
    if (mark == magic && count != 0 && inverse == ~count && count > largest) {
      largest = count; bestSlot = slot;
    }
  }
  if ((bestSlot < 0 && !erased) || largest == 0xFFFFFFFFUL) return false;
  int base = ((bestSlot + 1) % 8) * 12;
  bootCounter = largest + 1;
  write32(base + 8, 0); // invalidate destination before updating its payload
  write32(base, bootCounter);
  write32(base + 4, ~bootCounter);
  write32(base + 8, magic);
  if (read32(base) != bootCounter || read32(base + 4) != ~bootCounter || read32(base + 8) != magic) return false;
  snprintf(bootNonce, sizeof(bootNonce), "%08lx", (unsigned long)bootCounter);
  return true;
}

static void flushTx() {
  if (txSent == txUsed) {
    txSent = txUsed = 0;
    if (queuedUsed) {
      memcpy(tx, queuedTx, queuedUsed);
      txUsed = queuedUsed; queuedUsed = 0;
    } else return;
  }
  int room = Serial.availableForWrite();
  if (room <= 0) return;
  uint16_t count = txUsed - txSent;
  if (count > (uint16_t)room) count = room;
  // Never block on a full USB/UART buffer; keep returning to safetyTick.
  Serial.write((const uint8_t *)tx + txSent, count);
  txSent += count;
}

static void report(const char *kind, uint32_t sequence, const char *command, bool ok) {
  safetyTick();
  if (session[0] == '\0') { reportDirty = false; return; }
  const bool event = strcmp(kind, "EVENT") == 0;
  const bool busy = txUsed != txSent || queuedUsed;
  if (event && busy) return; // Preserve dirty state; never queue unsolicited telemetry.
  const bool priority = strcmp(command, "OFF") == 0 || strcmp(command, "ESTOP") == 0;
  if (busy && queuedUsed && !priority) {
    stopDark("TX_BUSY", true); // More than one outstanding ordinary ACK is a flood.
    return;
  }
  if (reportId == 0xFFFFFFFFUL) { stopDark("REPORT_OVERFLOW", true); return; }
  ++reportId;
  uint32_t remaining = 0;
  if (state == FOLLOW || state == FIRING) {
    uint32_t elapsed = (uint32_t)(millis() - windowAt);
    remaining = elapsed < windowMs ? windowMs - elapsed : 0;
    if (!remaining) {
      // Recheck inhibit/watchdog before assigning a natural completion cause.
      safetyTick();
      if (state == FOLLOW || state == FIRING) finishWindow();
    }
  }
  char *destination = busy ? queuedTx : tx;
  int n = snprintf(destination, sizeof(tx),
    "L2 %s %s %s %lu %s %s %lu %s %s %u %u %u %u %u %u %lu %lu %lu %s %lu %s\n",
    kind, bootNonce, session, (unsigned long)sequence, command, ok ? "OK" : "ERR",
    (unsigned long)reportId, stateName(), VERSION,
    (unsigned)INTERLOCK_WIRED, (unsigned)TRIGGER_WIRED, (unsigned)liveInterlock,
    (unsigned)liveTrigger, (unsigned)compHigh, (unsigned)startupChecked,
    (unsigned long)WATCHDOG_MS, (unsigned long)remaining, (unsigned long)MAX_WINDOW_MS, fault,
    (unsigned long)fireSeq, fireCompleted ? "FIRE_DONE" : "NONE");
  if (n <= 0 || n >= (int)sizeof(tx)) { stopDark("TX_OVERFLOW", true); return; }
  if (busy) queuedUsed = (uint16_t)n;
  else { txUsed = (uint16_t)n; txSent = 0; }
  reportDirty = false;
}

static bool hexToken(const char *text, uint8_t count) {
  if (strlen(text) != count) return false;
  for (uint8_t i = 0; i < count; ++i)
    if (!((text[i] >= '0' && text[i] <= '9') || (text[i] >= 'a' && text[i] <= 'f'))) return false;
  return true;
}

static bool unsignedToken(const char *text, uint32_t &result) {
  if (!*text || (text[0] == '0' && text[1] != '\0')) return false;
  result = 0;
  for (const char *p = text; *p; ++p) {
    if (*p < '0' || *p > '9') return false;
    uint8_t digit = *p - '0';
    if (result > (0xFFFFFFFFUL - digit) / 10UL) return false;
    result = result * 10UL + digit;
  }
  return true;
}

static void commandLine(char *line) {
  safetyTick();
  if (strcmp(line, "OFF") == 0 || strcmp(line, "ESTOP") == 0) {
    bool emergency = strcmp(line, "ESTOP") == 0;
    stopDark(emergency ? "ESTOP" : "NONE", emergency);
    return; // unsolicited state report, never a matched normal ACK
  }
  char *tokens[8]; uint8_t count = 0;
  char *cursor = line;
  while (*cursor && count < 8) {
    tokens[count++] = cursor;
    while (*cursor && *cursor != ' ') ++cursor;
    if (*cursor) { *cursor++ = '\0'; if (!*cursor || *cursor == ' ') { stopDark("PROTOCOL", true); return; } }
  }
  if (*cursor || count == 0) { stopDark("PROTOCOL", true); return; }
  if (count == 3 && strcmp(tokens[0], "L2") == 0 && strcmp(tokens[1], "HELLO") == 0) {
    if (!nonceGood || !hexToken(tokens[2], 32)) { stopDark("PROTOCOL", true); return; }
    for (uint8_t i = 0; i < usedSessionCount; ++i) {
      if (strcmp(usedSessions[i], tokens[2]) == 0) { stopDark("REPLAY", true); return; }
    }
    if (usedSessionCount == 8) { stopDark("SESSION_LIMIT", true); return; }
    // First handshake preserves manufacturer BOOT HIGH. Taking over an already
    // established session ALWAYS stops; it never inherits startup attestation.
    if (session[0] != '\0' || state != BOOT_HOLD) stopDark("NONE", false);
    startupChecked = false;
    strcpy(usedSessions[usedSessionCount++], tokens[2]);
    strcpy(session, tokens[2]); lastSeq = 0;
    report("ACK", 0, "HELLO", true);
    return;
  }
  if (count < 5 || count > 6 || strcmp(tokens[0], "L2") != 0) { stopDark("PROTOCOL", true); return; }
  const char *command = tokens[4];
  bool isOff = strcmp(command, "OFF") == 0, isEstop = strcmp(command, "ESTOP") == 0;
  // Safety commands pull LOW even when their nonce/sequence/argument is invalid.
  if (isOff || isEstop) stopDark(isEstop ? "ESTOP" : "NONE", isEstop);
  uint32_t sequence = 0;
  if (!nonceGood || session[0] == '\0' || strcmp(tokens[1], bootNonce) != 0
      || strcmp(tokens[2], session) != 0 || !unsignedToken(tokens[3], sequence)) {
    stopDark("SESSION", true); return;
  }
  if (sequence <= lastSeq) { stopDark("REPLAY", true); report("ACK", sequence, command, false); return; }
  lastSeq = sequence;
  bool windowCommand = strcmp(command, "FOLLOW") == 0 || strcmp(command, "FIRE") == 0;
  uint32_t duration = 0;
  if ((windowCommand && (count != 6 || !unsignedToken(tokens[5], duration)
                        || duration == 0 || duration > MAX_WINDOW_MS)) || (!windowCommand && count != 5)) {
    stopDark("PROTOCOL", true); report("ACK", sequence, command, false); return;
  }
  bool ok = true;
  if (isOff || isEstop || strcmp(command, "STATUS") == 0) {
    // STATUS never modifies leaseAt, startupChecked, or latched state.
  } else if (strcmp(command, "CHECK") == 0) {
    if (state != DISARMED) { stopDark("STATE", true); ok = false; }
    else if (!INTERLOCK_WIRED || !liveInterlock) { stopDark("INTERLOCK", true); ok = false; }
    else startupChecked = true; // Explicit human acknowledgement, not a sensor claim.
  } else if (strcmp(command, "ARM") == 0) {
    if (state != DISARMED) { stopDark("STATE", true); ok = false; }
    else if (!INTERLOCK_WIRED || !liveInterlock) { stopDark("INTERLOCK", true); ok = false; }
    else if (!startupChecked) { stopDark("SELF_CHECK", true); ok = false; }
    else {
      setComp(false); state = ARMED; leaseAt = millis(); fault = "NONE";
      fireSeq = 0; fireCompleted = false;
    }
  } else if (windowCommand) {
    if (state != ARMED) { stopDark("STATE", true); ok = false; }
    else if (!INTERLOCK_WIRED || !liveInterlock || !startupChecked) { stopDark("INTERLOCK", true); ok = false; }
    else if (strcmp(command, "FOLLOW") == 0 && !TRIGGER_WIRED) { stopDark("TRIGGER", true); ok = false; }
    // Read the physical input AGAIN at acceptance, not a host/poll snapshot.
    // A pre-existing LOW must never turn on Comp at a stationary starting pose.
    else if (strcmp(command, "FOLLOW") == 0 && (liveTrigger = digitalRead(TRIGGER_PIN) == LOW)) {
      stopDark("TRIGGER_ACTIVE", true); ok = false;
    }
    else {
      windowAt = millis(); windowMs = duration;
      state = strcmp(command, "FOLLOW") == 0 ? FOLLOW : FIRING;
      fireSeq = state == FIRING ? sequence : 0;
      fireCompleted = false;
      // FOLLOW/FIRE do not extend the lease established by ARM/KEEP.
      safetyTick();
    }
  } else if (strcmp(command, "KEEP") == 0) {
    if (state == DISARMED && fireCompleted && liveInterlock && startupChecked) {
      // An in-flight KEEP can arrive just after natural FIRE expiry. Report
      // that SAME receipt, without renewing a lease or changing latched LOW.
    }
    else if (!active() || !liveInterlock || !startupChecked) { stopDark("STATE", true); ok = false; }
    else leaseAt = millis(); // The ONLY renewal operation.
  } else { stopDark("PROTOCOL", true); ok = false; }
  report("ACK", sequence, command, ok);
}

void setup() {
  // Set output latch HIGH before enabling output: no blanket LOW at BOOT.
  digitalWrite(COMP_PIN, HIGH); pinMode(COMP_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH); pinMode(LED_PIN, OUTPUT);
  pinMode(INTERLOCK_PIN, INPUT_PULLUP); // open/disconnected = unsafe HIGH
  pinMode(TRIGGER_PIN, INPUT_PULLUP);   // open/disconnected = inactive HIGH
  nonceGood = nextBootNonce();
  if (!nonceGood) stopDark("BOOT_COUNTER", true);
  Serial.begin(115200);
  int n = snprintf(tx, sizeof(tx), "L2 BOOT %s %s\n", bootNonce, VERSION);
  txUsed = n > 0 && n < (int)sizeof(tx) ? (uint16_t)n : 0;
  safetyTick();
}

void loop() {
  safetyTick();
  flushTx();
  // Bounded work per pass; an RX flood cannot starve interlock/watchdog checks.
  for (uint8_t budget = 0; budget < 16 && Serial.available() > 0; ++budget) {
    safetyTick();
    int value = Serial.read();
    if (value < 0) break;
    char c = (char)value;
    if (c == '\n') {
      if (!dropping && rxUsed) { rx[rxUsed] = '\0'; commandLine(rx); }
      rxUsed = 0; dropping = false; carriageReturn = false;
    } else if (c == '\r') {
      // Only a terminating CRLF is legal, never an embedded/repeated CR.
      if (!dropping) {
        if (carriageReturn) { stopDark("PROTOCOL", true); dropping = true; rxUsed = 0; }
        else {
          if (rxUsed == 0) rxStarted = millis();
          carriageReturn = true;
        }
      }
    } else if (!dropping) {
      if (carriageReturn || c < 32 || c > 126 || rxUsed >= sizeof(rx) - 1) {
        stopDark("PROTOCOL", true); dropping = true; rxUsed = 0;
      } else {
        if (rxUsed == 0) rxStarted = millis();
        rx[rxUsed++] = c;
      }
    }
  }
  if (!dropping && (rxUsed || carriageReturn) && (uint32_t)(millis() - rxStarted) >= PARTIAL_COMMAND_MS) {
    stopDark("PARTIAL_TIMEOUT", true); dropping = true; rxUsed = 0; carriageReturn = false;
  }
  safetyTick();
  flushTx();
  if (reportDirty && txUsed == txSent && !queuedUsed) report("EVENT", lastSeq, "EVENT", state != FAULT);
}
