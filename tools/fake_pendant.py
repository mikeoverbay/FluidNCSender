#!/usr/bin/env python3
"""
A pretend Bluetooth jog pendant (the user's NANO_jogbox firmware) on a pseudo terminal, to try and test
FluidSender's pendant link (pendant.py) without the pendant.

    python3 tools/fake_pendant.py [--log FILE] [--control PATH] [--no-ack-check] [--boot-delay S]
                                  [--firmware original|fixed]

The first line it prints is the port to connect to (e.g. /dev/pts/9). To use it from the page, make it a test
port (with the fake FluidNC's):

    FLUIDSENDER_EXTRA_PORTS=/dev/pts/7:/dev/pts/9 FLUIDSENDER_ONLY_EXTRA_PORTS=1 python3 server.py --port 8790

It does what NANO_jogbox.ino does, with its timing:
- After booting (2 s) it sends "CONREQ\\r\\n" every 1 s until it reads the line "CONACK": the bytes before a "\\n"
  must be exactly that ("CONACK\\r" doesn't count), and a part line is given up after 1 s (readStringUntil).
  Then "REQ\\r\\n" at once, and for 0.5 s it does nothing.
- Connected: "REQ\\r\\n" every 250 ms (every 1 s while the last SYNC said status 2, a job running), but only once
  the SYNC for the last REQ has come. Once 3 or more bytes have come it reads 44 (waiting up to 1 s for each byte,
  like readBytes) and takes them as a SYNC if they start with "SYNC". Anything else is lost, and shifts the
  packets after it.
- Nothing read for 3 s (no SYNC, nor any 3 bytes): back to CONREQ.
- Keys only while connected: "KEY:<n>\\r\\n", then "REQ\\r\\n" 30 ms later. A held jog key (0-7) repeats every
  600 ms. Keys 12-19, 22 and 23 are double-tap, and Run (25), Pause (26) and Stop (27) are gated, as the firmware
  given says:
  original  (NANO_jogbox.ino as it was) a first press only arms the key (keys 20, 21 and 24 disarm it), the next
            press of the same key sends it, however much later. Run is sent only while the firmware's own running
            flag is off (and sets it), Pause only while it is on, Stop only while running or paused (and clears
            both): so Pause and Stop do nothing until Run was pressed on the pendant, and Run is sent again only
            after Stop. The flags last until it is switched off.
  fixed     (the 2026-10 fix) "FW:2\\r\\n" right after CONACK (before its REQ). The second press must come 0.12
            to 1 s after the first, and any other key disarms it; the same key again sooner than 0.12 s after a
            press of it (a first or a second one) is contact bounce, no press (a first one still waits). With
            "fresh" a SYNC at most 2.5 s old, since it last connected: Run is sent only when a fresh one said IDLE
            (0) with a job loaded (total 1 or more); Pause unless a fresh one said IDLE; Stop always. From
            FluidSender (the status + 0.5) Run also goes in PAUSED (FluidSender resumes), and Pause doesn't (it
            only holds). A status as a whole number is UGS's: then Run also goes only once until it has seen the
            end of that job, or Stop was sent (a flag, till switched off; the end: see below), and not while the
            line count is part-way (1 <= line < total) unless Stop was sent since a SYNC said RUN or HOLD with no
            Stop under way; Pause only in HOLD or RUN, or in BUSY with total 0 right after RUN or HOLD (the job's
            last moves) until another key is sent, and after Stop only once a SYNC has said IDLE (UGS's Stop holds
            the job, then resets it: its Pause in that HOLD would resume it); Stop not again within 2 s while one
            is under way (no SYNC said IDLE since); and the screen shows the old names (IDLE, HOLD, RUN, BUSY).
            The end of the job its Run started: a SYNC from UGS that says IDLE with line under 1 (UGS's count
            drops to 0 once the last row is answered) right after one from UGS (since it last connected) that
            said BUSY with total under 1 (the job's last moves), RUN with line 1 or more, or IDLE with line 1 or
            more when the last status other than IDLE since the Run was RUN (no HOLD: no cancel); and only if
            that one before it came more than 0.5 s after the Run (UGS fills its count late: a SYNC that soon may
            predate the Run), as only a status that late counts as that last other one.
- Its screen's state name for the last SYNC's status goes in the log ("shown"): original 0 IDLE, 1 HOLD, 2 RUN,
  anything else BUSY; fixed the names of FluidSender's codes 0-13 (pendant.SCREEN), BUSY for others.

Controls, one per line, on stdin or on --control PATH (a FIFO, made if it isn't there, or a file it follows from
its start):
    key N          press key N once                key N double   press it twice, 0.2 s apart (a double tap;
                                                                  key N double S: S s apart)
    hold N S       hold key N down for S seconds   send TEXT      send TEXT + "\\r\\n" as it is, in any state
    deaf S         lose everything sent to it for S seconds (no SYNC for 3 s: it connects again)
    age S          take the last SYNC as S seconds older (the fixed firmware's Run and Pause go by its age)
    age run S      take the last Run sent as S seconds older (the fixed firmware with UGS: only SYNCs more than
                   0.5 s after it tell the end of its job)
    off            switched off: silent and deaf   on             switched on again (boots, then CONREQ)
    quit

    --log FILE      append one JSON object per line, {"t": <unix time>, "ev": ..., ...}, for what it sends and
                    receives: "sent" (line), "conack" (accepted), "line" (another line read while connecting:
                    text, part), "connected", "timeout", "sync" (x y z a status feed line total step live, the
                    floats decoded, but status: the whole number the firmware keeps; marked: it came + 0.5, from
                    FluidSender; shown: the state name on its screen), "short" (a SYNC cut short: n, then
                    the floats, zeros where bytes were missing), "bad" (a packet that doesn't start with SYNC:
                    hex), "key" (key, sent, why), "lost" (n bytes lost while deaf, off or booting), "control"
                    (text), "on", "off"
    --no-ack-check  never take CONACK: keep sending CONREQ (to see that each one is answered)
    --boot-delay S  seconds from the start (or "on") to the first CONREQ (2: the firmware's delay(2000))
    --firmware F    original (the default) or fixed: how keys 12-19, 22, 23 and 25-27 are sent (see above)

Python standard library only. Ctrl+C quits.
"""

import argparse
import errno
import json
import math
import os
import select
import stat
import struct
import sys
import time
import tty
from collections import deque

CONREQ_EVERY = 1.0      # RECONNECT_INTERVAL
ACK_TIMEOUT = 3.0       # nothing read for this long: back to CONREQ
REQ_EVERY = 0.25        # waitDelay; while the status says a job runs (2):
REQ_EVERY_RUNNING = 1.0
REPEAT = 0.6            # REPEAT_INTERVAL: a held jog key
READ_TIMEOUT = 1.0      # Arduino's Stream timeout (readStringUntil; readBytes, for each byte)
KEY_GAP = 0.03          # delay(30) between KEY and REQ
REQ_PAUSE = 0.1         # delay(100) after a REQ
CONACK_PAUSE = 0.5      # delay(500) after CONACK
PACKET = 44             # readBytes(readBuf, 44)
DOUBLE_TAP = (12, 13, 14, 15, 16, 17, 18, 19, 22, 23)
FIELDS = ("x", "y", "z", "a", "status", "feed", "line", "total", "step", "live")
TICK = 0.005
DOUBLE_TAP_WINDOW = 1.0     # the fixed firmware: DOUBLE_TAP_MS ...
DOUBLE_TAP_MIN = 0.12       # ... and DOUBLE_TAP_MIN_MS
DOUBLE_GAP = 0.2            # "key N double": the time between the two presses (a person's double tap)
STATUS_FRESH = 2.5          # STATUS_FRESH_MS: how old a SYNC Run trusts at most
STOP_AGAIN = 2.0            # STOP_AGAIN_MS: UGS, Stop not again this soon while the last one is under way
RUN_SETTLE = 0.5            # RUN_SETTLE_MS: UGS, SYNCs this soon after a Run may predate it: no job end from them
FW_LINE = "FW:2"            # the fixed firmware's line after CONACK
SHOWN = {"original": ("IDLE", "HOLD", "RUN"),
         "fixed": ("IDLE", "PAUSED", "RUNNING", "BUSY", "ALARM", "STOPPING", "ERROR", "OFFLINE", "JOG", "HOMING",
                   "SCAN", "MOVING", "DOOR", "LIMIT")}  # getState(); any other code: BUSY


class FakePendant:
    def __init__(self, fd, log=None, ack_check=True, boot_delay=2.0, loud=False, firmware="original"):
        self.fd = fd
        self.log = log
        self.ack_check = ack_check          # False: --no-ack-check
        self.boot_delay = boot_delay
        self.firmware = firmware            # "original" or "fixed"
        self.loud = loud                    # tell a person on stderr what happens
        self.shown = None                   # the last SYNC told on stderr
        self.deaf_until = 0.0
        self.power_on(time.monotonic(), quiet=True)

    def power_on(self, now, quiet=False):
        """setup(): everything starts over, the firmware's statics too."""
        if not quiet:
            self.note("on")
        self.on = True
        self.boot_at = now + self.boot_delay
        self.connected = False
        self.waiting = False                # waitingForSync
        self.last_ack = 0.0                 # lastAckTime
        self.last_counter = 0.0             # lastCounter (the REQ timer)
        self.last_conreq = -1e9             # lastReconnectAttempt
        self.status = 0                     # ps.status, from the last SYNC
        self.line = 0.0                     # ps.line
        self.total = 0.0                    # ps.total
        self.have_status = False            # fixed: a SYNC came since the last (re)connect ...
        self.sync_at = 0.0                  # ... then
        self.prev_cmd = -1                  # handleCommand's statics (fixed: armedKey)
        self.tap_key = -1                   # fixed: lastTapKey, the double-tap key pressed last ...
        self.tap_at = 0.0                   # ... and when (lastTapAt)
        self.running = self.paused = False
        self.marked = False                 # fixed: ps.fromFluidSender (the last status came as code + 0.5)
        self.run_locked = False             # fixed: runLocked (UGS: Run sent, and no end of its job seen since,
                                            # nor Stop sent) ...
        self.run_at = 0.0                   # ... runAt, when that Run was sent, and lastNonIdle, the last status
        self.last_non_idle = 0              # other than IDLE since then (RUN: no hold seen)
        self.ugs_stopping = False           # fixed: ugsStopping (Stop sent, no SYNC since said IDLE or was marked)
        self.stop_at = -1e9                 # fixed: lastStopAt
        self.stop_since = False             # fixed: stopSince (UGS: Stop sent since a SYNC said RUN or HOLD)
        self.ugs_drain = False              # fixed: ugsDrain (UGS: BUSY with total 0 right after RUN or HOLD)
        self.presses = deque()              # (key, held for s) pressed, not scanned yet; (None, s): s with no key
        self.quiet_until = 0.0              # (until then no key is pressed: the gap in a double tap)
        self.hold = None                    # [key, let go at, last sent] while a key is held
        self.busy_until = 0.0               # in a delay()
        self.later = deque()                # (at, line): the REQ after a KEY
        self.rx = bytearray()
        self.rx_at = 0.0                    # when the last byte came
        self.reading = None                 # readBytes() started then

    def note(self, ev, **fields):
        if self.log:
            self.log.write(json.dumps({"t": round(time.time(), 3), "ev": ev, **fields}) + "\n")
        if self.loud and not (ev == "sent" and fields.get("line") == "REQ"):
            if ev in ("sync", "short"):
                shown = tuple(round(fields[f], 4) for f in FIELDS)
                if shown == self.shown:
                    return
                self.shown = shown
            print(ev, " ".join(f"{k}={v}" for k, v in fields.items()), file=sys.stderr)

    def send_line(self, text):
        self.note("sent", line=text)
        try:
            os.write(self.fd, (text + "\r\n").encode())
        except OSError:
            pass                            # full buffer or nobody there: lost, like a real UART

    # -------------------------------------------------- input

    def received(self, data, now):
        if not self.on or now < self.boot_at or now < self.deaf_until:     # (booting: its UART isn't started yet)
            self.note("lost", n=len(data))
            return
        self.rx += data
        self.rx_at = now

    def control(self, text, now):
        """One control line; 'quit' to end."""
        words = text.split()
        if not words:
            return None
        self.note("control", text=text)
        cmd, args = words[0].lower(), words[1:]
        try:
            if cmd == "key":
                key = int(args[0])
                if args[1:2] == ["double"]:
                    gap = float(args[2]) if args[2:] else DOUBLE_GAP
                    self.presses.extend([(key, 0.0), (None, gap), (key, 0.0)])
                else:
                    self.presses.append((key, 0.0))
            elif cmd == "hold":
                self.presses.append((int(args[0]), float(args[1])))
            elif cmd == "send":
                self.send_line(text.split(None, 1)[1])
            elif cmd == "deaf":
                self.deaf_until = now + float(args[0])
            elif cmd == "age":
                if args[:1] == ["run"]:
                    self.run_at -= float(args[1])
                else:
                    self.sync_at -= float(args[0])
            elif cmd == "off":
                self.on = False
                self.note("off")
            elif cmd == "on":
                self.power_on(now)
            elif cmd == "quit":
                return "quit"
            else:
                raise ValueError("unknown control")
        except (ValueError, IndexError) as e:
            self.note("control", text=text, error=str(e))
        return None

    # -------------------------------------------------- the firmware

    def step(self, now):
        if not self.on or now < self.boot_at:
            return
        while self.later and self.later[0][0] <= now:
            self.send_line(self.later.popleft()[1])
        if now < self.busy_until:
            return
        if self.connected:
            self.loop(now)
        else:
            self.reconnect(now)

    def reconnect(self, now):
        """reconnect(): CONREQ every second until the line CONACK comes (keys aren't even scanned)."""
        while self.presses:
            key = self.presses.popleft()[0]
            if key is not None:
                self.note("key", key=key, sent=False, why="not connected")
        self.hold = None
        if self.firmware == "fixed":        # (reconnect(): the host's status not known again, nothing armed)
            self.have_status, self.prev_cmd, self.ugs_drain = False, -1, False
        if now - self.last_conreq >= CONREQ_EVERY:
            self.last_conreq = now
            self.send_line("CONREQ")
        while b"\n" in self.rx:
            i = self.rx.index(b"\n")
            line = bytes(self.rx[:i])
            del self.rx[:i + 1]
            if line != b"CONACK":
                self.note("line", text=line.decode(errors="replace"), part=False)
                continue
            self.note("conack", accepted=self.ack_check)
            if self.ack_check:
                self.connected = True
                self.waiting = True
                self.note("connected")
                if self.firmware == "fixed":
                    self.send_line(FW_LINE)
                self.send_line("REQ")
                self.busy_until = self.last_ack = now + CONACK_PAUSE
                return                      # (what came after the CONACK is read as packets)
        if self.rx and now - self.rx_at >= READ_TIMEOUT:
            self.note("line", text=self.rx.decode(errors="replace"), part=True)
            self.rx.clear()

    def loop(self, now):
        """loop(): keys, REQ when due, then a 44-byte read, then the 3 s timeout."""
        if self.reading is not None:        # inside readBytes(): nothing else happens meanwhile
            if self.read_packet(now):
                self.check_timeout(now)
            return
        self.scan(now)
        if now < self.busy_until:           # (a key's delay(30))
            return
        running = self.status == 2 and (self.have_status or self.firmware == "original")
        if now - self.last_counter >= (REQ_EVERY_RUNNING if running else REQ_EVERY):
            self.last_counter = now
            if not self.waiting:
                self.waiting = True
                self.send_line("REQ")
                self.busy_until = now + REQ_PAUSE
                return
        if len(self.rx) > 2:                # BTSerial.available() > 2
            self.reading = self.last_ack = now
            if not self.read_packet(now):
                return
        self.check_timeout(now)

    def read_packet(self, now):
        """readBytes(readBuf, 44): False while still waiting for bytes."""
        if len(self.rx) < PACKET and now - max(self.rx_at, self.reading) < READ_TIMEOUT:
            return False
        pkt = bytes(self.rx[:PACKET])
        del self.rx[:PACKET]
        self.reading = None
        if pkt[:4] != b"SYNC":
            self.note("bad", hex=pkt.hex())
            return True
        vals = struct.unpack("<10f", (pkt[4:] + bytes(PACKET))[:40])     # (readBuf was zeroed first)
        ok = math.isfinite(vals[4]) and 0 <= vals[4] < 256
        before, before_ugs = self.status, self.have_status and not self.marked
        before_line, before_total, before_at = self.line, self.total, self.sync_at      # (the last SYNC's too)
        self.status = int(vals[4]) if ok else 3 if self.firmware == "fixed" else 0
        self.marked = ok and vals[4] - int(vals[4]) >= 0.25
        if self.marked or self.status == 0:
            self.ugs_stopping = False
        if not self.marked and self.status in (1, 2) and not self.ugs_stopping:
            self.stop_since = False         # (a UGS job runs or holds: its line count is a live one again)
        self.line, self.total = vals[6], vals[7]
        # UGS: the job its Run started has ended (IDLE with line 0 right after its last moves, BUSY with total 0,
        # or right after RUN or an IDLE with rows answered, the job's last other state RUN), and the SYNC before
        # this one came more than 0.5 s after the Run: Run may go again without a Stop first
        if (self.run_locked and not self.marked and before_ugs and before_at - self.run_at > RUN_SETTLE
                and self.status == 0 and self.line < 1
                and ((before == 3 and before_total < 1) or (before == 2 and before_line >= 1)
                     or (before == 0 and before_line >= 1 and self.last_non_idle == 2))):
            self.run_locked = False
        if self.run_locked and not self.marked and self.status != 0 and now - self.run_at > RUN_SETTLE:
            self.last_non_idle = self.status
        self.ugs_drain = (not self.marked and self.status == 3 and self.total < 1 and before_ugs
                          and (before in (1, 2) or (before == 3 and self.ugs_drain)))    # (UGS's last moves of a job)
        self.have_status, self.sync_at = True, now
        self.waiting = False
        shown = self.shown_name()
        fields = dict(zip(FIELDS, vals), status=self.status, marked=self.marked)
        if len(pkt) == PACKET:
            self.note("sync", shown=shown, **fields)
        else:
            self.note("short", n=len(pkt), shown=shown, **fields)
        return True

    def shown_name(self):
        """getState(): the name on its screen for the last status (the fixed firmware: UGS's by the old names)."""
        names = SHOWN["fixed" if self.firmware == "fixed" and self.marked else "original"]
        return names[self.status] if self.status < len(names) else "BUSY"

    def check_timeout(self, now):
        if now - self.last_ack > ACK_TIMEOUT:
            self.connected = False
            self.hold = None
            self.note("timeout")

    def scan(self, now):
        """scanMatrix(): a new press, or the held jog key again every 600 ms."""
        if self.hold is not None:
            key, until, last = self.hold
            if now >= until:
                self.hold = None            # let go
            elif key <= 7 and now - last >= REPEAT:
                self.hold[2] = now
                self.key_out(key, now)
                return
        if self.presses and now >= self.quiet_until:
            key, held = self.presses.popleft()
            if key is None:                 # the gap between two taps
                self.quiet_until = now + held
                return
            self.hold = [key, now + held, now] if held else None
            if 0 <= key <= 27:
                self.command(key, now)
            else:
                self.note("key", key=key, sent=False, why="no such key (0-27)")

    def command(self, key, now):
        """handleCommand()"""
        if self.firmware == "fixed":
            self.command_fixed(key, now)
            return
        if key <= 11:
            self.key_out(key, now)
        elif key in DOUBLE_TAP:
            if self.prev_cmd == key:
                self.prev_cmd = -1
                self.key_out(key, now)
            else:
                self.prev_cmd = key
                self.note("key", key=key, sent=False, why="armed: a double-tap key, press it again")
        elif key == 25:
            if self.running:
                self.note("key", key=key, sent=False, why="running already: Run is sent again only after Stop")
            else:
                self.running = True
                self.key_out(key, now)
        elif key == 26:
            if self.running:
                self.paused = not self.paused
                self.key_out(key, now)
            else:
                self.note("key", key=key, sent=False, why="not running: Pause only works after Run on the pendant")
        elif key == 27:
            if self.running or self.paused:
                self.running = self.paused = False
                self.key_out(key, now)
            else:
                self.note("key", key=key, sent=False, why="not running: Stop only works after Run on the pendant")
        else:
            self.prev_cmd = -1
            self.note("key", key=key, sent=False, why="does nothing")

    def command_fixed(self, key, now):
        """handleCommand() of the fixed firmware: a double-tap window, and Run and Pause gated on the host's
        status."""
        if key in DOUBLE_TAP:
            since = now - self.tap_at
            if self.tap_key == key and since < DOUBLE_TAP_MIN:
                why = "the first tap still waits" if self.prev_cmd == key else "not a tap"
                self.note("key", key=key, sent=False, why="contact bounce: " + why)
                return
            self.tap_key, self.tap_at = key, now
            if self.prev_cmd == key and since <= DOUBLE_TAP_WINDOW:      # (since: from the first tap then)
                self.prev_cmd = -1
                self.key_out(key, now)
            else:
                self.prev_cmd = key
                self.note("key", key=key, sent=False, why="armed: press it again within 1 s")
            return
        self.prev_cmd = self.tap_key = -1   # (any other key disarms)
        if key == 27:
            if not self.marked and self.ugs_stopping and now - self.stop_at < STOP_AGAIN:
                self.note("key", key=key, sent=False, why="STOP under way")     # (UGS: one more cancel would wait)
                return
            self.stop_at = now
            self.run_locked = False         # (Stop lets Run go again)
            self.ugs_stopping = self.stop_since = True
        why = self.run_refused(now) if key == 25 else self.pause_refused(now) if key == 26 else None
        if why:
            self.note("key", key=key, sent=False, why=why)
        elif key <= 11 or key in (25, 26, 27):
            self.key_out(key, now)
        else:
            self.note("key", key=key, sent=False, why="does nothing")

    def fresh(self, now):
        """statusFresh()"""
        return self.have_status and now - self.sync_at <= STATUS_FRESH

    def run_refused(self, now):
        """runAllowed(): why Run isn't sent now (the note on its screen), or None."""
        if not self.fresh(now):
            return "RUN: no status yet"
        if self.status == 0:
            if self.total < 1:
                return "RUN: no job loaded"
            if self.marked:
                return None
            # UGS: its IDLE may be the middle of the job the last Run started (no end of it seen since, nor Stop
            # sent), or of one started in its window (the line count part-way, and no Stop since it last said RUN
            # or HOLD)
            if self.run_locked or (1 <= self.line < self.total and not self.stop_since):
                return "RUN: press STOP first"
            self.run_locked, self.run_at, self.last_non_idle = True, now, 0
            return None
        if self.status == 1:
            return None if self.marked else "PAUSE resumes"     # (FluidSender resumes; UGS would start again)
        if self.status == 2:
            return "RUN: job running"
        return "RUN: not in " + self.shown_name()

    def pause_refused(self, now):
        """pauseAllowed(): not sent after Stop until a SYNC says IDLE (or comes from FluidSender), nor when a fresh
        status says IDLE, nor FluidSender's PAUSED (Run resumes there), nor UGS's BUSY (a jog: its Pause would leave
        UGS paused) but its job's last moves."""
        if self.ugs_stopping:
            return "PAUSE: STOP under way"
        if not self.fresh(now):
            return None
        if self.status == 0:
            return "PAUSE:nothing running"
        if self.marked and self.status == 1:
            return "PAUSE: RUN resumes"
        return "PAUSE: use STOP" if not self.marked and self.status not in (1, 2) and not self.ugs_drain else None

    def key_out(self, key, now):
        """Send_To_Keyout(): the key, and 30 ms later a REQ."""
        if key != 26:
            self.ugs_drain = False          # (a move or a jog may start: not UGS's last moves of a job any more)
        self.note("key", key=key, sent=True)
        self.send_line(f"KEY:{key}")
        self.later.append((now + KEY_GAP, "REQ"))
        self.busy_until = now + KEY_GAP
        self.waiting = True


class Control:
    """The control lines: stdin, a FIFO or a file that grows."""

    def __init__(self, path):
        self.buf = b""
        self.done = False                   # stdin was closed: no more controls
        self.regular = False                # a file: read at every tick (select says nothing about files)
        if path is None:
            self.fd = sys.stdin.fileno()
            return
        if not os.path.exists(path):
            os.mkfifo(path)
        if stat.S_ISFIFO(os.stat(path).st_mode):
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            self.keep = os.open(path, os.O_WRONLY | os.O_NONBLOCK)  # so it never ends when a writer goes away
        else:
            self.fd = os.open(path, os.O_RDONLY)
            self.regular = True

    def fds(self):
        return [] if self.done or self.regular else [self.fd]

    def lines(self):
        if self.done:
            return []
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return []
        except OSError:
            data = b""
        if not data:
            self.done = not self.regular
            return []
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        return [line.decode(errors="replace").strip() for line in lines]


def main():
    ap = argparse.ArgumentParser(description="A pretend Bluetooth jog pendant (NANO_jogbox) on a pseudo terminal")
    ap.add_argument("--log", metavar="FILE", help="append what it sends and receives to FILE (JSON lines)")
    ap.add_argument("--control", metavar="PATH", help="read the controls from PATH (a FIFO, or a file) "
                                                       "instead of stdin")
    ap.add_argument("--no-ack-check", action="store_true", help="never take CONACK: keep sending CONREQ")
    ap.add_argument("--boot-delay", type=float, default=2.0, metavar="S", help="seconds before the first CONREQ")
    ap.add_argument("--firmware", choices=("original", "fixed"), default="original",
                    help="the firmware's key rules: original (as it was) or fixed (the 2026-10 fix)")
    args = ap.parse_args()

    master, slave = os.openpty()
    tty.setraw(slave)            # the port side: no echo, no line editing (kept after it is closed)
    path = os.ttyname(slave)
    os.close(slave)              # FluidSender won't open a port another program has open
    os.set_blocking(master, False)
    log = open(args.log, "a", buffering=1) if args.log else None
    control = Control(args.control)
    loud = sys.stderr.isatty()
    sim = FakePendant(master, log, not args.no_ack_check, args.boot_delay, loud, args.firmware)
    print(path, flush=True)
    if loud:
        print(f"Fake pendant ({args.firmware} firmware) on {path}  (type: key 5, key 13 double, hold 5 2, deaf 4, off, "
              "on, quit; Ctrl+C quits)", file=sys.stderr)
    try:
        while True:
            ready = select.select([master] + control.fds(), [], [], TICK)[0]
            now = time.monotonic()
            if master in ready:
                try:
                    data = os.read(master, 4096)
                except BlockingIOError:
                    data = b""
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    data = b""               # no one has the port open
                    time.sleep(TICK)
                if data:
                    sim.received(data, now)
            if control.regular or control.fd in ready:
                for line in control.lines():
                    if sim.control(line, now) == "quit":
                        return
            sim.step(time.monotonic())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
