#!/usr/bin/env python3
"""
A pretend FluidNC 4.1.1 (--fw 3.9.4: FluidNC 3.9.4) on a pseudo terminal, to try and test FluidSender without
the machine.

    python3 tools/fake_fluidnc.py [--fw 4.1.1|3.9.4] [--alarm] [--soft-limits] [--inches] [--no-settings]
                                  [--stall-after N] [--banner-delay S] [--spinup S] [--log FILE]
                                  [--no-flood] [--no-mist] [--no-readback] [--coolant-delay MS]
                                  [--spindle pwm|onoff|vfd] [--vfd-sync S] [--rx-buffer N] [--ov-at-once]
                                  [--late-events] [--no-verbose-errors] [--clean-reset]

The first line it prints is the port to connect to (e.g. /dev/pts/7). To use it from the page:

    FLUIDSENDER_EXTRA_PORTS=/dev/pts/7 FLUIDSENDER_ONLY_EXTRA_PORTS=1 python3 server.py --port 8790

Like the real controller it boots in G21, reports millimeters (unless --inches) and prints its
welcome banner (after an empty line) at start and after Ctrl-X: "Grbl 4.1 [FluidNC v4.1.1
(esp32-noradio) '$' for help]", or 3.9.4's "Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"; lines
that come before that banner are lost (FluidNC empties its input when it restarts). Moves take real
time: G0 at 5000 mm/min, G1 and $J= at F, each slowed down so no axis goes faster than its max rate
(X Y 5000, Z 2000 mm/min).

Input: realtime bytes (? ! ~ Ctrl-X and every byte from 0x80 up) act as they arrive. FluidNC reads
the next line only once it has finished the last one, and finishes a line only when it may answer
it: a motion line once the planner has room for it (planner_blocks 16, which hold 15 moves); M3-M5,
M7-M9, G10, G28.1, G92, G92.1 once the moves before them are done; G4 P once the moves are done and
then its dwell (G4 P0 doesn't wait); M0 after ~; $H when homed. While it is free to read, its line
editor takes the other bytes as they come; while it is busy with a line, held (Hold:0), slowing
down a jog cancel or asleep after an ALARM, they wait in a receive buffer of 256 bytes (the ESP32
UART driver's; --rx-buffer), and a byte that comes while that buffer is full is lost (lines run
together, as on a real overflow): the --log file gets "RX OVERFLOW <n>" (n bytes lost in one read).
A character-counting sender that keeps at most 128 bytes unanswered never gets there. Like
FluidNC's UART line editor, each CR and each LF ends a line (an empty line is answered ok, so send
"\\n" only), a line keeps its first 254 characters, and spaces, (comments) (an unclosed one to the
end of the line) and ;comments are dropped; ^L (0x0C) turns off its echo mode (the sim never
echoes) and is dropped too. A '%': 3.9.4 drops it anywhere, 4.1.1 only a line that is nothing else
(a '%' inside a line is error:1). 4.1.1 refuses a G-code line over 127 characters, spaces and
comments included (error:14), and a $J= jog too. 4.1.1 prints "[MSG:ERR: Bad GCode: <line>]" before
every G-code error:N (not for error:9 in Alarm or Jog, nor for $ and $J= lines). Within a line it works
in FluidNC's order:
F, S/M3-M5, M7-M9, G4, units/distance, G43.1/G10/G28/G92, the move, M0/M2/M30.

Holds: ! during Run reports Hold:1 for 0.3 s (slowing down), then Hold:0; ~ resumes only from Hold:0
(during Hold:1 it is ignored, as FluidNC discards it). ! in Idle is Hold:0 at once. Once a hold is
complete (Hold:0) FluidNC waits in its suspend loop until ~ or Ctrl-X: it reads no line and finishes
none (a G4 dwell's clock stops; a spindle spin-up goes on); during Hold:1 it still reads lines.
A reset keeps one line, as FluidNC does: at Hold:0 its poller still hands the next complete line to
the main loop (3.9.4's activeLine, 4.1.1's one-slot cmd_queue; it runs it after ~), and the reset
empties the input but not that line, so the main loop runs it after the banner, in the state after
the reset (G21, G0...), and answers it (Protocol.cpp polling_loop / poll_once,
protocol_do_soft_restart). Here: Ctrl-X at Hold:0 with a complete line received meanwhile, and no
line being executed (a line waiting for planner room is dropped, as FluidNC's abort drops it), runs
that line after the banner; the --log file gets "LEFTOVER <text>" then. --clean-reset: a reset drops
every line. A '!' in a critical alarm (a hard or soft limit, reported as Alarm) is taken, as both
versions do: Hold:1 for ever (lines are read and their moves planned, none runs); Ctrl-X then gives
Idle with no ALARM:14 even with unhomed axes, and in 4.1.1 a '~' with nothing planned gives Idle
without a reset (Protocol.cpp protocol_do_feedhold). 0x9E in a complete hold with the spindle on
spins it down, until ~ or 0x9E again ("[MSG:INFO: Restoring spindle]").
M0 waits for the moves, then holds (Hold:0, as ! in Idle) and is answered ok after ~. M1 does
nothing (FluidNC has no optional stop). M2 / M30 wait for the moves, set G1 G90 M5 M9, turn
the spindle and coolant off, put the overrides back to 100 % and answer "[MSG:INFO: Program End]",
then ok. A bad line changes nothing and is answered error:N, then, as FluidNC's verbose_errors (on
by default) does, "[MSG:ERR: <meaning>]" (--no-verbose-errors: error:N only); the next line runs as
usual. Two words of one group (G0 G1, M3 M5, M7 M8, M0 M2...) are error:21, a word twice error:25,
G4 without P error:28, a character that isn't a letter where a word starts error:1, a letter
without a number error:2.

Coolant (M8 flood, M7 mist) and spindle: M8 / M7 turn one on and M9 both off, once the moves before
them are done; like FluidNC they take M7.1 / M8.1 (off) from the fraction of the line's last
word, so "M8 X0.1" turns flood off. 0xA0 toggles flood and 0xA1 mist at once, only in Idle, Run or
Hold (ignored in Alarm, Jog, Home). $/coolant/flood_pin and mist_pin say I2SO.4 and I2SO.5, pins
FluidNC reads back (A: shows them); --no-readback: gpio.25 and gpio.26, plain ESP32 GPIOs set up as
outputs only, which read 0, so A: never shows F or M ($G still shows the modes). $/coolant/delay_ms
says --coolant-delay (0). --no-flood / --no-mist: the machine has no such output (its pin: NO_PIN):
M8 / M7 are answered ok and change nothing ($G neither), its toggle does nothing, M9 does nothing if
neither exists ($I's [OPT:MPHS] loses its M without mist). $G shows the coolant modes like FluidNC: "M9", or
"M7", "M8" or "M7 M8". M3 / M4 / M5 always wait for the moves (FluidNC skips the wait when the
spindle doesn't change: the stage 2 tests count on an M5 waiting); FS shows the new speed then, A:
the new state after the spin-up (--spinup). FS's speed is S x the spindle override in whole numbers
(FluidNC's mapSpeed), also after M5 until a move runs with M5; 0 after a reset or M2/M30. $SS (the
startup log) names the spindle type: --spindle pwm (default), onoff (off, and no S in A:, when S x
override is below 1) or vfd (FS shows a new override's speed VFD_SYNC later). An ALARM leaves the
spindle and coolant on (FluidNC's spindle off_on_alarm is false by default); Ctrl-X turns them off.

Status reports (FluidNC's counters, both versions): WCO in the first report after a reset and every
10th after it (every 30th while busy: Run, Hold, Jog, Home); Ov in every 10th report as well (20th
while busy), never in a report with WCO (an Ov due then comes in the next report: normally reports
1, 11, 21... after a reset). A report with Ov adds "|A:" + S (M3) or C (M4) + F (flood on) + M (mist
on) when any is on: "Ov:100,100,100" alone means all off, and a report without Ov says nothing about
them. FluidNC 4.1.1 puts Ov in the next report after any spindle, coolant (M7/M8/M9 or a toggle) or
override change (gc_ovr_changed). Polled with ? (as FluidSender does), FluidNC 3.9.4 puts Ov in the
very next report only after a feed or rapid override change and after Ctrl-X; a spindle, coolant or
spindle override change shows with the next 10th report only (20th while busy); --ov-at-once: as
4.1.1 (and Grbl 1.1, and FluidNC on a channel with automatic reports). The overrides work like
FluidNC's: 0x90-0x94 feed 10-200 % (G1), 0x95-0x98 rapid 100/50/25/5 % (G0), 0x99-0x9D spindle
10-200 % (FS); jogs ignore them. 0x84 (door) and the macros are ignored.

FluidNC's protocol task handles the override bytes and the coolant toggles as events, in order, once it is free
(RealtimeCmd.cpp, Protocol.cpp protocol_handle_events), and its main loop runs a line handed over in the same
poll pass before the events queued with it (protocol_main_loop: the cmd_queue line first, then
protocol_execute_realtime). --late-events: those bytes act after the lines that came in the same read (a "$G"
written with 0xA0 shows the coolant modes from before the toggle; an "M8" with it is flipped by it). --vfd-sync S
(with --spindle vfd): every spindle override change while the spindle is on and not moving keeps that task busy
for S seconds (VFDSpindle::setState waits for the drive's speed, handling no event): the override bytes and
toggles that come meanwhile wait and then act in order, and no line is read or finished meanwhile; status
reports still answer (FluidNC's polling task). Otherwise these bytes act at once.

Jogs: $J= only in Idle or Jog (else error:8); its G20/G91/G53 apply to the jog only. The state
becomes Jog after the ok (FluidNC starts the cycle then), so a status report or a 0x85 that comes
right with the first $J still finds Idle. Jog cancel (0x85, or ! during a jog) acts only in Jog:
it slows down along the path at the move's acceleration (X Y 200, Z 100 mm/s^2) while reporting
Jog, then forgets every planned move and goes Idle. A $J waiting for planner room is dropped and
answered ok then; lines that come during the slow-down run after it.

Alarms come as FluidNC prints them: "[MSG:INFO: ALARM: <name>]" then "ALARM:N", and then it sleeps
half a second, reading no line: 3.9.4's reports still show the state from before, 4.1.1's say Alarm
(it sets the state first). A hard or soft limit is critical: after that pause "[MSG:ERR: Reset to
continue]", status says Alarm, lines get ok but nothing moves, $X answers ok and changes nothing, $H
still homes; $T says "State 11 (Critical)" (3.9.4: 10), and 3.9.4 refuses $I then (error:8), 4.1.1
doesn't. (4.1.1 soft limit: "[MSG:INFO: Soft limit exceeded on X axis: target ... mm, limit [..., ...]
mm]", the commanded mpos and the G-code line, then ok and ALARM:2; 3.9.4: "[MSG:INFO: Soft limit on
X target:...]".) 4.1.1: 0xB4 (an I/O expander's reset) is ALARM:16, critical, with "[MSG:ERR: Reboot
FluidNC]".
Ctrl-X then gives Alarm with ALARM:14 if an axis is unhomed (a hard limit unhomes all), else Idle;
the position is kept. Ctrl-X while moving is ALARM:3 (while homing ALARM:6). FluidNC prints a
reset's ALARM before the new banner, which then comes half a second later. A new work offset
(G10/G92/G43.1) shows only in the next 10th report's WCO, as FluidNC does when polled. 4.1.1's reset
clears the G43.1 tool length offset (3.9.4 kept it). Neither version prints "'$H'|'$X' to unlock"
after a banner.
--alarm boots with every axis unhomed (ALARM:14 after the banner);
$H homes all, $HX/$HY/$HZ one (the state stays Alarm while any axis is unhomed, as in FluidNC);
$X in a plain alarm marks every axis homed and prints "[MSG:INFO: Caution: Unlocked]".

Supported: ? ! ~ Ctrl-X 0x85 0xA0 0xA1 0x90-0x9E (0xB4), $I $G $T $SS $X $H $HX/$HY/$HZ $J=
(3.9.4 answers $I, $T and $SS only once the planner is empty; 4.1.1 at once),
$/axes/<x|y|z>/max_rate_mm_per_min, $/axes/<x|y|z>/acceleration_mm_per_sec2,
$/axes/<x|y|z>/soft_limits, max_travel_mm, homing/mpos_mm and homing/positive_direction, $/planner_blocks, $/coolant/flood_pin, $/coolant/mist_pin,
$/coolant/delay_ms (read only; other $/... answer error:3),
G0 G1 G4 G10 L2/L20 (P0 or P1) G17 G20 G21 G28 G28.1 G43.1 G49 G53 G54 G90 G91 G92 G92.1 G94,
M0 M1 M2 M30 M3 M4 M5 M7 M7.1 M8 M8.1 M9, F S T N. Any other G or M code (and any other letter)
answers error:20, any other $ or [ command error:3.

    --fw 4.1.1|3.9.4     the FluidNC version it acts as (default 4.1.1, the user's)
    --alarm              boot in Alarm with every axis unhomed (must home): G-code gets error:9 until $X or $H
    --soft-limits        soft limits on, travel X -800..0, Y -600..0, Z -100..0 mm (machine coordinates):
                         a jog stops at the limit, a G0/G1 beyond it is ALARM:2 (critical)
    --inches             report_inches on: MPos/WCO in inches with 4 decimals, FS feed in in/min, $G F x.x
    --no-settings        every $/... answers error:3 (the speeds are still capped)
    --stall-after N      answer N lines, then read no more lines (? still answers): a frozen controller
    --banner-delay S     wait S seconds before the banner, at start and after a reset
    --spinup S           M3/M4 answer S seconds after the moves before them are done (spindle spin-up delay)
    --no-flood           no flood (M8, vacuum) output: M8 and 0xA0 do nothing
    --no-mist            no mist (M7, AUX) output: M7 and 0xA1 do nothing
    --no-readback        the coolant pins are plain GPIOs (gpio.25, gpio.26): A: never shows F or M
    --coolant-delay MS   $/coolant/delay_ms (default 0)
    --spindle TYPE       the spindle $SS names: pwm (default), onoff, vfd
    --vfd-sync S         with --spindle vfd: a spindle override change while it stands keeps FluidNC busy S s
    --rx-buffer N        receive buffer bytes (default 256; 0: no limit, as FluidNC's own queue behind it)
    --ov-at-once         3.9.4: Ov in the next report after any spindle, coolant or override change (as 4.1.1)
    --late-events        override bytes and coolant toggles act after the lines that came in the same read
    --no-verbose-errors  error:N without the "[MSG:ERR: ...]" line after it
    --clean-reset        a reset drops every line (FluidNC runs the one it took during a hold)
    --log FILE           append what is received to FILE (the tests read it): "LINE <text>" for each line
                         (as it arrives), "RT <name>" for each realtime byte ("?", "!", "~", "^X",
                         "jog-cancel", "flood-toggle", "mist-toggle", "feed+10", "rapid=50", ...,
                         else "0xNN"), "CTRL ^L", "RX OVERFLOW <n>", "HARD-LIMIT", and "LEFTOVER <text>"
                         when it runs a line kept through a reset

kill -USR1 <pid> trips a hard limit: everything stops at once, ALARM:1, critical, all axes unhomed.
Python standard library only. Ctrl+C quits.
"""

import argparse
import errno
import math
import os
import re
import select
import signal
import struct
import sys
import time
import tty
from collections import deque

RAPID = 5000.0        # mm/min for G0 (then capped per axis like any move)
HOLD_DECEL = 0.3      # s from Hold:1 (slowing down) to Hold:0 (stopped)
HOME_TIME = 1.0       # s for a homing cycle
ALARM_PAUSE = 0.5     # s FluidNC sleeps after printing an ALARM, so a reset's banner comes this much later
TICK = 0.02
RX_SIZE = 256         # bytes: FluidNC's UART receive buffer (Uart.cpp: uart_driver_install(..., 256, ...))
LINE_MAX = 254        # characters of a line FluidNC keeps (its line editor drops the rest)
LINE_GCODE = 127      # FluidNC 4.1.1 refuses a longer G-code line or $J= jog (error:14: GCode.cpp gc_execute_line)
REPORT_EVERY = 10     # WCO, and Ov, in every 10th status report while idle (Config.h REPORT_*_REFRESH_IDLE_COUNT) ...
OV_BUSY = 20          # ... Ov in every 20th and WCO in every 30th while busy: Run, Hold, Jog, Home, Door
WCO_BUSY = 30         # (Config.h REPORT_OVR_REFRESH_BUSY_COUNT, REPORT_WCO_REFRESH_BUSY_COUNT; both versions)
BUSY = ("Run", "Hold", "Jog", "Home", "Door")
VFD_SYNC = 0.3        # s --spindle vfd takes to reach a new speed (FS shows the old one meanwhile)
AXES = "XYZ"
SETTINGS = {          # the config items FluidSender reads ($/name), as this machine has them
    "axes/x/max_rate_mm_per_min": 5000.0, "axes/y/max_rate_mm_per_min": 5000.0,
    "axes/z/max_rate_mm_per_min": 2000.0,
    "axes/x/acceleration_mm_per_sec2": 200.0, "axes/y/acceleration_mm_per_sec2": 200.0,
    "axes/z/acceleration_mm_per_sec2": 100.0,
    # the travel --soft-limits keeps (machine X -800..0, Y -600..0, Z -100..0 mm): homed toward + at 0
    "axes/x/max_travel_mm": 800.0, "axes/y/max_travel_mm": 600.0, "axes/z/max_travel_mm": 100.0,
    "axes/x/homing/mpos_mm": 0.0, "axes/y/homing/mpos_mm": 0.0, "axes/z/homing/mpos_mm": 0.0,
    "planner_blocks": 16,
}
# the coolant pins: I2SO ones FluidNC reads back (its A: shows them); --no-readback: plain ESP32 GPIOs, set up as
# output only, which read 0 (CoolantControl::get_state reads the pins: A: never shows F or M then)
PINS = {"flood": "I2SO.4", "mist": "I2SO.5"}
GPIO_PINS = {"flood": "gpio.25", "mist": "gpio.26"}
# $SS: the startup log FluidNC kept (the spindle's line names its type)
SPINDLE_LINES = {"pwm": "PWM Spindle Ena:NO_PIN Out:gpio.13 Dir:NO_PIN Freq:5000Hz Period:8191",
                 "onoff": "OnOff Spindle Ena:NO_PIN Out:gpio.13 Dir:NO_PIN",
                 "vfd": "Huanyang Spindle Tx:gpio.17 Rx:gpio.16 RTS:gpio.4 Baud:9600"}
MAX_RATE = [SETTINGS[f"axes/{a}/max_rate_mm_per_min"] for a in "xyz"]      # mm/min
ACCEL = [SETTINGS[f"axes/{a}/acceleration_mm_per_sec2"] for a in "xyz"]    # mm/s^2
PLANNER = SETTINGS["planner_blocks"] - 1     # moves the planner holds (its ring buffer keeps one block free)
TRAVEL = [(-800.0, 0.0), (-600.0, 0.0), (-100.0, 0.0)]   # --soft-limits, machine mm
ALARMS = {1: "Hard Limit", 2: "Soft Limit", 3: "Abort Cycle", 6: "Homing Fail Reset", 14: "Unhomed",
          16: "Expander Reset"}
ERRORS = {1: "Expected GCodecommand letter", 2: "Bad GCode number format",   # (Error.cpp: verbose_errors)
          3: "Invalid $ statement", 8: "Command requires idle state",
          9: "GCode cannot be executed in lock or alarm state", 14: "Line too long", 20: "Unsupported GCode command",
          21: "Gcode modal group violation", 22: "Gcode undefined feed rate", 25: "Gcode word repeated",
          28: "Gcode value word missing", 40: "Check startup pins"}
ERRORS_394 = {**ERRORS, 14: "Startup line too long", 40: "Check control pins"}    # (the texts 4.1.1 changed)
BANNERS = {"4.1.1": "Grbl 4.1 [FluidNC v4.1.1 (esp32-noradio) '$' for help]",
           "3.9.4": "Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"}
FIRMWARES = tuple(BANNERS)
# $T: "State <n> (<name>)", numbered as each version's State enum (4.1.1 put Held after Hold)
STATE_NUMBER = {"4.1.1": {"Idle": 0, "Alarm": 1, "Check": 2, "Home": 3, "Run": 4, "Hold": 5, "Jog": 7, "Door": 8,
                          "Sleep": 9, "Critical": 11},
                "3.9.4": {"Idle": 0, "Alarm": 1, "Check": 2, "Home": 3, "Run": 4, "Hold": 5, "Jog": 6, "Door": 7,
                          "Sleep": 8, "Critical": 10}}
STATE_NAME = {"Run": "Cycle", "Home": "Homing", "Door": "SafetyDoor", "Check": "CheckMode"}
NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)")
REALTIME_ASCII = (0x3F, 0x21, 0x7E, 0x18)    # ? ! ~ Ctrl-X; FluidNC takes every byte >= 0x80 as realtime too
REALTIME = {0x3F: "?", 0x21: "!", 0x7E: "~", 0x18: "^X", 0x84: "safety-door", 0x85: "jog-cancel",
            0x90: "feed=100", 0x91: "feed+10", 0x92: "feed-10", 0x93: "feed+1", 0x94: "feed-1",
            0x95: "rapid=100", 0x96: "rapid=50", 0x97: "rapid=25", 0x98: "rapid=5",
            0x99: "spindle=100", 0x9A: "spindle+10", 0x9B: "spindle-10", 0x9C: "spindle+1", 0x9D: "spindle-1",
            0x9E: "spindle-stop", 0xA0: "flood-toggle", 0xA1: "mist-toggle", 0xB4: "expander-reset"}
OVR_STEP = {0x91: 10, 0x92: -10, 0x93: 1, 0x94: -1, 0x9A: 10, 0x9B: -10, 0x9C: 1, 0x9D: -1}
EVENT_BYTES = frozenset(range(0x90, 0x9E)) | {0xA0, 0xA1}   # the overrides and toggles: events (see event)
RAPID_OVR = {0x95: 100, 0x96: 50, 0x97: 25, 0x98: 5}
G_GROUPS = {0: "motion", 1: "motion", 4: "non-modal", 10: "non-modal", 28: "non-modal", 28.1: "non-modal",
            53: "non-modal", 92: "non-modal", 92.1: "non-modal", 17: "plane", 20: "units", 21: "units",
            43.1: "tool length", 49: "tool length", 54: "coordinates", 90: "distance", 91: "distance",
            94: "feed mode"}
M_GROUPS = {0: "stop", 1: "stop", 2: "stop", 30: "stop", 3: "spindle", 4: "spindle", 5: "spindle",
            7: "coolant", 7.1: "coolant", 8: "coolant", 8.1: "coolant", 9: "coolant"}


def alarm_lines(code):
    return [f"[MSG:INFO: ALARM: {ALARMS[code]}]", f"ALARM:{code}"]


def collapse(line, v4=True):
    """FluidNC's collapseGCode: no white space, no (comments) (an unclosed one runs to the end of the
    line), nothing after ';', no stray ')'; upper case. A '%': FluidNC 3.9.4 drops it anywhere; 4.1.1 drops
    only a line that is nothing else (spaces aside, without a comment): elsewhere it stays, and the parser
    refuses it (error:1)."""
    out, comment, had = [], False, False
    for c in line:
        if c.isspace():
            continue
        if c in "()":
            comment = c == "("
            had = had or comment
        elif c == ";":
            return "".join(out)                 # (4.1.1 returns before its '%' check too)
        elif not comment and (v4 or c != "%"):
            out.append(c.upper())
    code = "".join(out)
    return "" if v4 and not had and code == "%" else code


def mantissa(value):
    """lroundf(100 * (value - truncf(value))) in single precision, as FluidNC's parser works it out for
    every word; M7/M8 read the last word's (10 means M7.1/M8.1: off)."""
    try:
        f = struct.unpack("f", struct.pack("f", value))[0]
        m = struct.unpack("f", struct.pack("f", 100 * (f - math.trunc(f))))[0]
    except OverflowError:
        return 0
    return math.floor(m + 0.5) if m >= 0 else math.ceil(m - 0.5)


class FakeFluidNC:
    def __init__(self, fd, alarm=False, stall_after=None, banner_delay=0.0, log=None, spinup=0.0,
                 inches=False, soft_limits=False, settings=True, flood=True, mist=True, rx_size=RX_SIZE,
                 ov_at_once=False, verbose_errors=True, keep_line=True, fw="4.1.1", readback=True, spindle="pwm",
                 coolant_delay=0, late_events=False, vfd_sync=0.0):
        self.fd = fd
        self.late_events = late_events       # override bytes and toggles act after the lines read with them
        self.vfd_sync = vfd_sync             # s the protocol task waits for a VFD at a spindle override change
        self.fw = fw                         # "4.1.1" or "3.9.4"
        self.v4 = fw == "4.1.1"
        self.readback = readback             # A: shows the coolant outputs (False: --no-readback, plain GPIOs)
        self.spindle_type = spindle          # pwm, onoff or vfd ($SS; onoff: on at S x override >= 1)
        self.coolant_delay = coolant_delay   # $/coolant/delay_ms
        self.keep_line = keep_line           # a reset at Hold:0 keeps the line taken meanwhile (False: --clean-reset)
        self.kept = None                     # that line, run after the banner
        self.stall_after = stall_after
        self.banner_delay = banner_delay
        self.spinup = spinup
        self.log = log
        self.inches = inches                 # report_inches
        self.soft_limits = soft_limits
        self.settings = settings             # False: no $/... can be read (--no-settings)
        self.has_flood, self.has_mist = flood, mist   # the machine has these coolant outputs
        self.rx_size = rx_size               # receive buffer bytes (0: no limit)
        self.ov_at_once = ov_at_once
        self.verbose = verbose_errors
        self.tripped = False                 # SIGUSR1 came: a hard limit (handled in the main loop)
        self.received = 0                    # lines received (for --stall-after)
        self.lost = 0                        # bytes lost to a full receive buffer
        self.ticks = 0
        self.now = time.monotonic()          # (for the line being executed: see proceed)
        self.buf = bytearray()               # the line coming in
        self.wco = [-150.0, -100.0, -30.0]   # G54 work zero in machine coordinates, mm
        self.tlo = 0.0                       # G43.1 tool length offset, mm (adds to the Z work offset; 3.9.4 keeps
                                             # it over a reset, until it reboots; 4.1.1's reset clears it)
        self.g28 = [0.0, 0.0, 0.0]           # G28 position (machine), set by G28.1
        self.pos = list(self.wco)            # machine position, mm
        self.homed = [not alarm] * 3         # must_home: an Alarm boot has every axis unhomed
        self.wco_counter = self.ovr_counter = 0   # FluidNC's report_wco_counter and report_ovr_counter
        self.reset("Alarm" if alarm else "Idle", self.now)
        if alarm:
            self.after_banner = alarm_lines(14)   # FluidNC's startup alarm comes right after its banner

    def reset(self, state, now, pause=0.0):
        """Power-up / Ctrl-X: everything but the position, the work and tool length offsets and the homed
        axes starts over; the spindle and coolant go off (and their modes to M5 M9)."""
        self.state = state
        self.critical = False                # a hard or soft limit alarm (status says Alarm): only a reset ends it
        self.fake_hold = False               # a '!' in that alarm: a Hold that never completes (see realtime)
        self.spindle_stop = False            # 0x9E in a hold: the spindle spun down until ~ (or 0x9E again)
        self.speed_zeroed = False            # a move ran with M5: FS says 0 (FluidNC's step generator)
        self.vfd_until, self.vfd_from = 0.0, 0   # --spindle vfd: FS shows vfd_from until then (see fs_speed)
        if self.v4:
            self.tlo = 0.0                   # (4.1.1's gc_init clears the tool length offset; 3.9.4 kept it)
        self.hold_at = 0.0                   # when a hold began (-inf: stopped at once, Hold:0)
        self.moves = deque()                 # (target mm, rate mm/min, is a jog, acceleration mm/s^2,
                                             #  programmed feed mm/min or None for a rapid or jog, max rate mm/min)
        self.target = list(self.pos)         # where the queued moves end
        self.start = None                    # tick count when a jog was queued in Idle: its cycle starts after it
        self.stop = None                     # [speed mm/s, deceleration mm/s^2] while a jog cancel slows down
        self.busy = None                     # the line being executed: a generator yielding what it waits for
        self.wait = None                     # ... the condition it waits for now: a function of the time
        self.waiting_jog = False             # busy is a $J waiting for room in the planner
        self.dwell = None                    # seconds of a G4 dwell still to go (counted while not held)
        self.backlog = deque()               # (line, its bytes) received but not read yet
        self.home = None                     # (axes, start, t0)
        self.g92 = [0.0, 0.0, 0.0]           # G92 offset (a reset clears it)
        self.units, self.distance, self.motion = "G21", "G90", "G0"
        self.spindle, self.feed, self.speed = "M5", 0.0, 0.0   # modes: M3/M4/M5, F (mm/min), S
        self.flood = self.mist = False       # coolant modes: M8, M7 ($G)
        self.spindle_cmd = "M5"              # the spindle as commanded (FS shows its speed) ...
        self.spindle_out = "M5"              # ... and as running, after the spin-up (A:)
        self.out_speed = 0.0                 # the S the spindle was set to
        self.pin_flood = self.pin_mist = False   # the coolant outputs (A:)
        self.f_ovr = self.r_ovr = self.s_ovr = 100   # overrides, percent (Ov)
        self.alarm_pause = None              # (until, state reported meanwhile, lines printed then) after an ALARM
        self.events = deque()                # override bytes and toggles waiting for the protocol task ...
        self.busy_until = 0.0                # ... which is busy until then (--vfd-sync)
        self.buf.clear()
        self.rx_used = 0                     # bytes waiting in the receive buffer (of the backlog and buf) ...
        self.buf_queued = 0                  # ... of them in buf, the line coming in
        self.banner_at = now + pause + self.banner_delay
        self.after_banner = []

    def offset(self):
        """The work offset FluidNC reports as WCO: G54 + G92 (+ tool length on Z)."""
        return [w + g + (self.tlo if i == 2 else 0.0) for i, (w, g) in enumerate(zip(self.wco, self.g92))]

    def note(self, text):
        if self.log:
            self.log.write(f"{time.time():.3f} {text}\n")

    def send(self, text):
        try:
            os.write(self.fd, (text + "\r\n").encode())
        except OSError:
            pass                             # full buffer or nobody there: lost, like a real UART

    def error(self, code):
        """FluidNC's answer to a bad line: error:N, then (verbose_errors, its default) what it means."""
        return [f"error:{code}"] + ([f"[MSG:ERR: {(ERRORS if self.v4 else ERRORS_394)[code]}]"] if self.verbose else [])

    def moving(self, now):
        return self.state in ("Run", "Jog") or (self.state == "Hold" and not self.fake_hold
                                                 and now - self.hold_at < HOLD_DECEL)

    def suspended(self, now):
        """A complete feed hold (Hold:0): FluidNC waits in its suspend loop until ~ or Ctrl-X, reading no
        line and finishing none."""
        return self.state == "Hold" and now - self.hold_at >= HOLD_DECEL

    def synced(self, now):
        """protocol_buffer_synchronize(): the moves before this line are done (and FluidNC isn't held)."""
        return not self.moves and not self.suspended(now)

    def ov_changed(self):
        """FluidNC's gc_ovr_changed(): 4.1.1 puts Ov in the next report (report_ovr_counter = 0). In 3.9.4 only a
        channel with automatic reports gets it; one polled with ? sees the change with the next 10th report (20th
        while busy). --ov-at-once: in the next report in 3.9.4 too."""
        if self.ov_at_once or self.v4:
            self.ovr_counter = 0

    # -------------------------------------------------- input

    def receive(self, data, now):
        """Bytes from the port. Realtime commands act at once (FluidNC picks them out as they come). The
        others go straight to the line editor while FluidNC is free to read a line; otherwise they wait in
        the receive buffer until their line is read, and are lost when it is full. (--late-events: the override
        bytes and toggles act after the lines that came with them: see event.)"""
        self.now = now
        lost = 0
        late = []
        for b in data:
            if self.late_events and b in EVENT_BYTES:
                self.note("RT " + REALTIME[b])
                late.append(b)
                continue
            if b in REALTIME_ASCII or b >= 0x80:
                self.realtime(b, now)
                continue
            if b == 0x0C:                    # ^L: FluidNC's line editor turns its echo off (lineedit.cpp), no more
                self.note("CTRL ^L")
                continue
            if self.backlog or not self.reading(now):
                if self.rx_size and self.rx_used >= self.rx_size:
                    lost += 1                # overflow: this byte is gone
                    continue
                self.rx_used += 1
                self.buf_queued += 1
            if b in (0x0A, 0x0D):            # (FluidNC's line editor ends a line at each of them)
                text = self.buf[:LINE_MAX].decode(errors="replace")
                size, self.buf_queued = self.buf_queued, 0
                self.buf.clear()
                self.line(text, size, now)
            else:
                self.buf.append(b)
        if lost:
            self.lost += lost
            self.note(f"RX OVERFLOW {lost}")
        for b in late:
            self.event(b, now)

    def event(self, b, now):
        """An override byte or a coolant toggle: FluidNC queues an event, which its protocol task handles in order
        once it is free (busy: --vfd-sync); otherwise at once."""
        if self.events or now < self.busy_until:
            self.events.append(b)
        else:
            self.apply_event(b, now)

    def run_events(self, now):
        """The protocol task free again: the events that waited, in order (a spindle one may keep it busy again)."""
        while self.events and now >= self.busy_until:
            self.apply_event(self.events.popleft(), now)

    def apply_event(self, b, now):
        if b in (0xA0, 0xA1):
            self.toggle("flood" if b == 0xA0 else "mist")
        else:
            self.override(b, now)

    def realtime(self, b, now):
        self.note("RT " + REALTIME.get(b, f"0x{b:02X}"))
        if b == 0x3F:
            self.report(now)
        elif b == 0x21:
            if self.state == "Alarm" and self.critical:
                # FluidNC ignores a feed hold in Alarm, but not in Critical (a hard or soft limit, reported as
                # Alarm): it goes to Hold, which never completes (Hold:1), reads lines and plans their moves but
                # runs none; Ctrl-X then gives Idle, no ALARM:14 (Protocol.cpp protocol_do_feedhold; both versions)
                self.state, self.hold_at, self.fake_hold, self.critical = "Hold", math.inf, True, False
            elif self.state == "Jog":        # like FluidNC: a feed hold cancels a jog, it never holds it
                self.cancel_jog()
            elif self.state == "Run":
                self.state, self.hold_at = "Hold", now
            elif self.state == "Idle":       # nothing to slow down: stopped at once (Hold:0)
                self.state, self.hold_at = "Hold", -math.inf
            elif self.state == "Home":
                self.send("[MSG:INFO: Feedhold ignored while homing; use Reset instead]")
        elif b == 0x7E:
            if self.fake_hold:               # 4.1.1 resumes a hold with nothing planned and nothing slowing down:
                if self.v4 and not self.moves:   # Idle, out of the alarm without a reset (3.9.4: nothing)
                    self.state, self.fake_hold = "Idle", False
            elif self.suspended(now):        # ignored while still slowing down (Hold:1)
                if self.spindle_stop:        # (a 0x9E spindle stop: restored first)
                    self.restore_spindle()
                self.state = ("Jog" if self.moves[0][2] else "Run") if self.moves else "Idle"
        elif b == 0x18:
            self.ctrl_x(now)
        elif b == 0x85:
            if self.state == "Jog":          # only in Jog: before a jog's cycle has started it does nothing
                self.cancel_jog()
        elif b in EVENT_BYTES:               # (0x90-0x9D, 0xA0, 0xA1)
            self.event(b, now)
        elif b == 0x9E:                      # spindle stop: only in a complete hold (Hold:0), with the spindle on
            if self.spindle_stop:
                self.restore_spindle()
            elif self.suspended(now) and self.spindle_cmd != "M5":
                self.spindle_stop = True
                self.ovr_counter = 0         # (FluidNC: report_ovr_counter = 0, both versions)
        elif b == 0xB4 and self.v4:          # 4.1.1: an I/O expander's reset (Channel.cpp PinRST): critical
            for text in self.critical_alarm(16, message="[MSG:ERR: Reboot FluidNC]"):
                self.send(text)
        # (0x84 safety door, the macros and the rest: ignored here)

    def restore_spindle(self):
        """The end of a 0x9E spindle stop: FluidNC says so and turns it on again (protocol_manage_spindle)."""
        self.spindle_stop = False
        self.send("[MSG:INFO: Restoring spindle]")
        self.ov_changed()

    def toggle(self, which):
        """0xA0 / 0xA1: FluidNC flips flood / mist at once, only in Idle, Run or Hold and only when the
        machine has that output. It flips the mode itself ($G shows it), so a toggle while an M8 waits
        for the moves cancels that M8."""
        if (self.has_flood if which == "flood" else self.has_mist) and self.state in ("Idle", "Run", "Hold"):
            setattr(self, which, not getattr(self, which))
            self.set_pins()
            self.ov_changed()

    def set_pins(self):
        """CoolantControl::set_state: the outputs follow the coolant modes."""
        self.pin_flood, self.pin_mist = self.flood and self.has_flood, self.mist and self.has_mist

    def override(self, b, now):
        """0x90-0x9D, as FluidNC changes them. A feed or rapid change puts Ov in the next report
        (update_velocities); a spindle one only flags it like a spindle change (see ov_changed). A VFD with the
        spindle on takes VFD_SYNC (--vfd-sync) to reach the new speed (FS shows the old one meanwhile); with
        --vfd-sync, while the spindle stands FluidNC's protocol task waits for it (protocol_do_spindle_override ->
        setState): busy until then (see event)."""
        vfd = self.spindle_type == "vfd" and self.spindle_cmd != "M5"
        if b > 0x98 and vfd:
            self.vfd_from, self.vfd_until = self.fs_speed(now), now + (self.vfd_sync or VFD_SYNC)
        if b <= 0x94:
            old, self.f_ovr = self.f_ovr, 100 if b == 0x90 else max(10, min(200, self.f_ovr + OVR_STEP[b]))
            if self.f_ovr != old:
                self.ovr_counter = 0
        elif b <= 0x98:
            old, self.r_ovr = self.r_ovr, RAPID_OVR[b]
            if self.r_ovr != old:
                self.ovr_counter = 0
        else:
            old, self.s_ovr = self.s_ovr, 100 if b == 0x99 else max(10, min(200, self.s_ovr + OVR_STEP[b]))
            if self.s_ovr != old:
                self.ov_changed()
                if self.vfd_sync and vfd and not self.moving(now):
                    self.busy_until = now + self.vfd_sync

    def ctrl_x(self, now):
        """FluidNC's reset. Its ALARM line comes before the new banner; until then it reports the old state
        (4.1.1: Alarm). From the Hold a '!' in a critical alarm gave: Idle, no ALARM:14 (see realtime)."""
        if self.state == "Home":
            code = 6                         # Homing Fail Reset
        elif self.moving(now):
            code = 3                         # Abort Cycle: stopped at once, the position may be lost
        elif self.critical and not all(self.homed):
            code = 14                        # Unhomed (a hard limit unhomes every axis)
        else:
            code = None
        for text in alarm_lines(code) if code else ():
            self.send(text)
        kept = None                          # FluidNC 3.9.4: the line its poller handed over during the hold
        if self.keep_line and self.suspended(now) and self.busy is None and self.backlog and self.backlog[0][0]:
            kept = self.backlog[0][0]
        alarm = code is not None or (self.state == "Alarm" and not self.critical)
        old = self.state
        self.reset("Alarm" if alarm else "Idle", now, ALARM_PAUSE if code else 0.0)
        self.kept = kept                     # (runs after the banner: see tick)
        self.ovr_counter = 0                 # (protocol_do_late_reset: the spindle stops, Ov in the next report)
        if code:
            self.alarm_pause = (now + ALARM_PAUSE, old, [])

    def hard_limit(self):
        """A limit switch (kill -USR1): FluidNC stops at once and needs a reset; no axis counts as homed."""
        self.note("HARD-LIMIT")
        if self.state in ("Idle", "Run", "Jog", "Hold"):   # ignored in an alarm; homing uses the switches
            for text in self.critical_alarm(1):
                self.send(text)
            self.homed = [False] * 3

    def critical_alarm(self, code, hang=True, message="[MSG:ERR: Reset to continue]"):
        """Stops everything until a reset (status says Alarm). Returns the lines FluidNC prints at once: like
        FluidNC (alarm_msg) it then sleeps ALARM_PAUSE, reading no line (3.9.4 reports the old state meanwhile,
        4.1.1 Alarm already), and only then says "Reset to continue". The spindle and coolant stay as they are."""
        self.moves.clear()
        self.target = list(self.pos)
        self.start = self.stop = self.dwell = None
        if self.busy and hang:               # FluidNC stays stuck on the line it executes: no answer until a reset
            self.busy, self.wait = self.hang(), None
        self.alarm_pause = (time.monotonic() + ALARM_PAUSE, self.state, [message])
        self.state, self.critical = "Alarm", True
        return alarm_lines(code)

    def line(self, text, size, now):
        """A line has come in (size: its bytes that wait in the receive buffer)."""
        self.note("LINE " + text)
        if self.banner_at is not None:
            self.rx_used -= size             # not started yet: FluidNC empties its input when it starts
            return
        self.received += 1
        if self.stall_after is not None and self.received > self.stall_after:
            text = None                      # frozen (--stall-after): FluidNC gets stuck on this line
        self.backlog.append((text, size))
        self.take(now)

    def reading(self, now):
        """FluidNC reads the next line once it has finished the last one, unless it is slowing down a jog
        cancel, asleep after an ALARM or held (Hold:0), nor while its protocol task is busy (--vfd-sync)."""
        return not (self.busy or self.stop is not None or self.alarm_pause or self.suspended(now)
                    or now < self.busy_until)

    def take(self, now):
        while self.backlog and self.reading(now):
            text, size = self.backlog.popleft()
            self.rx_used -= size
            if text is None:                 # stuck for good (until a reset): no answer, and what comes now
                self.busy, self.wait = self.hang(), None     # waits in the buffer
            else:
                self.run(text, now)

    def run(self, text, now):
        self.begin_jog()                     # FluidNC starts a planned jog before it reads the next line
        result = self.execute(text, now)
        if isinstance(result, list):
            for r in result:
                self.send(r)
        else:
            self.busy, self.wait = result, None
            self.proceed(now)

    def proceed(self, now):
        """Carries on with the line being executed (self.busy, a generator yielding the conditions it waits
        for and returning its replies) as far as it can now; sends the replies when it is done. Not while the
        protocol task is busy (--vfd-sync: see event)."""
        if now < self.busy_until:
            return
        while self.busy is not None:
            if self.wait is not None and not self.wait(now):
                return
            gen = self.busy
            try:
                wait = next(gen)
            except StopIteration as done:
                if self.busy is gen:
                    self.busy = self.wait = None
                    self.waiting_jog = False
                for r in done.value or ():
                    self.send(r)
                continue
            if self.busy is gen:
                self.wait = wait

    def later(self, cond, replies):
        """A line answered once cond(now) holds."""
        yield cond
        return replies

    def hang(self):
        """A line FluidNC never finishes (until a reset)."""
        yield lambda now: False

    # -------------------------------------------------- commands

    def execute(self, text, now):
        """The reply lines, or a generator when the line answers later (see proceed)."""
        if not text:
            return ["ok"]                    # FluidNC answers an empty line with ok
        text = text.strip()
        up = text.upper()
        if up.startswith("$/"):
            return self.setting(text)
        if up.startswith(("$", "[")):
            return self.dollar(up, now)
        if self.state == "Jog" or (self.state == "Alarm" and not self.critical):
            return self.error(9)             # like FluidNC: G-code is locked out during a jog or an alarm
        if self.v4 and len(text) > LINE_GCODE:   # 4.1.1: spaces and comments count (gc_execute_line)
            return [f"[MSG:ERR: Bad GCode: {text}]"] + self.error(14)
        result = self.gcode(up, now)
        if self.v4 and isinstance(result, list) and result[0].startswith("error:"):
            result = [f"[MSG:ERR: Bad GCode: {text}]"] + result     # (4.1.1 prints the line before the error)
        return result

    def setting(self, text):
        """$/name: a FluidNC config item, e.g. '$/axes/x/max_rate_mm_per_min=5000.000' (in mm)."""
        key = text[2:].strip()
        if self.settings and key.lower() in (f"axes/{a}/soft_limits" for a in "xyz"):
            return [f"$/{key}={'true' if self.soft_limits else 'false'}", "ok"]     # (FluidNC prints bools so)
        if self.settings and key.lower() in (f"axes/{a}/homing/positive_direction" for a in "xyz"):
            return [f"$/{key}=true", "ok"]
        if self.settings and key.lower() in ("coolant/flood_pin", "coolant/mist_pin"):
            return [f"$/{key}={self.pin(key.lower()[8:-4])}", "ok"]
        if self.settings and key.lower() == "coolant/delay_ms":
            return [f"$/{key}={self.coolant_delay}", "ok"]
        value = SETTINGS.get(key.lower()) if self.settings and "=" not in key else None
        if value is None:
            return self.error(3)
        return [f"$/{key}={value:.3f}" if isinstance(value, float) else f"$/{key}={value}", "ok"]

    def pin(self, which):
        """The coolant output's pin as $/coolant/<which>_pin prints it."""
        if not (self.has_flood if which == "flood" else self.has_mist):
            return "NO_PIN"
        return (PINS if self.readback else GPIO_PINS)[which]

    def synced_then(self, replies):
        """3.9.4 runs its $I, $T and $SS (UserCommands) only once the planner is empty (protocol_buffer_synchronize);
        4.1.1's are ReportCommands, which answer at once. replies: a function giving the reply lines then."""
        if self.v4:
            return replies()
        return self.answer_after(self.synced, replies)

    def answer_after(self, cond, replies):
        yield cond
        return replies()

    def info(self):
        """$I. 4.1.1 answers in any state; 3.9.4 refuses it outside Idle, Alarm and Door (notIdleOrAlarm: not in
        a critical alarm, which it reports as Alarm)."""
        opt = f"[OPT:{'M' if self.has_mist else ''}PHS]"
        if self.v4:
            return ["[VER:4.1 FluidNC v4.1.1 (esp32-noradio) :]", opt, "[CLUSTER:16]", "[MSG:Machine: Fake]", "ok"]
        if self.critical or self.state not in ("Idle", "Alarm", "Door"):
            return self.error(8)
        return ["[VER:3.7 FluidNC v3.9.4:]", opt, "ok"]

    def state_line(self):
        """$T: "State 11 (Critical)" (the number from each version's State enum: 4.1.1 put Held after Hold)."""
        name = "Critical" if self.state == "Alarm" and self.critical else self.state
        return [f"State {STATE_NUMBER[self.fw][name]} ({STATE_NAME.get(name, name)})", "ok"]

    def startup_log(self):
        """$SS: the messages of FluidNC's start, which name the spindle type and the coolant pins."""
        lines = [f"[MSG:INFO: FluidNC v{self.fw} https://github.com/bdring/FluidNC]", "[MSG:INFO: Machine Fake]",
                 "[MSG:INFO: Board Fake]", f"[MSG:INFO: {SPINDLE_LINES[self.spindle_type]}]"]
        lines += [f"[MSG:INFO: {legend} {self.pin(which)}]" for which, legend in (("flood", "Flood coolant"),
                                                                                  ("mist", "Mist coolant"))
                  if self.pin(which) != "NO_PIN"]
        return lines + ["ok"]

    def dollar(self, up, now):
        if up == "$I":
            return self.synced_then(self.info)
        if up == "$T":
            return self.synced_then(self.state_line)
        if up == "$SS":
            return self.synced_then(self.startup_log)
        if up == "$G":
            coolant = " ".join(m for m, on in (("M7", self.mist), ("M8", self.flood)) if on) or "M9"
            feed = f"{self.feed:.1f}" if self.inches else f"{self.feed:.0f}"     # (mm/min, as FluidNC prints it)
            return [f"[GC:{self.motion} G54 G17 {self.units} {self.distance} G94 {self.spindle} {coolant} "
                    f"T0 F{feed} S{int(self.speed)}]", "ok"]
        if up == "$X":
            if self.state == "Alarm" and not self.critical:   # a critical alarm needs a reset: $X changes nothing
                self.homed = [True] * 3      # soft limits now count from wherever the machine is
                self.state = "Idle"
                return ["[MSG:INFO: Caution: Unlocked]", "ok"]
            return ["ok"]
        m = re.fullmatch(r"\$H([XYZ]*)", up)
        if m:
            if self.state not in ("Idle", "Alarm") or self.moves:
                return self.error(8)
            self.home = (["XYZ".index(a) for a in m.group(1) or "XYZ"], list(self.pos), now)
            self.state, self.critical = "Home", False
            return self.later(lambda t: self.home is None, ["ok"])
        if up.startswith("$J="):
            if self.state not in ("Idle", "Jog"):
                return self.error(8)
            if self.v4 and len(up) > LINE_GCODE:     # (doJog hands "$J=..." to gc_execute_line: no Bad GCode)
                return self.error(14)
            return self.gcode(up[3:], now, jog=True)
        return self.error(3)

    def gcode(self, up, now, jog=False):
        """A G-code line, or a $J= line's words: the replies, or a generator when it answers later. Like
        FluidNC it checks everything first (a bad line changes nothing), then runs in FluidNC's order."""
        code = collapse(up, self.v4)
        if not code:
            return ["ok"]
        words, at = [], 0
        while at < len(code):                # letter, number, letter, number... (as FluidNC reads them)
            if not "A" <= code[at] <= "Z":
                return self.error(1)
            number = NUMBER.match(code, at + 1)
            if not number:
                return self.error(2)
            words.append((code[at], number.group()))
            at = number.end()
        units, distance, motion = self.units, self.distance, self.motion
        spindle, speed = self.spindle, self.speed
        axes, feed, p, l_word = {}, None, None, None
        special = flow = coolant = None      # a non-modal G code (G4, G10, G28, G53, G92...); M0-M30; M7-M9
        spindle_word = False
        groups, seen, last = set(), set(), 0
        for letter, value in words:
            v = float(value)
            last = 0 if letter == "G" else mantissa(v)   # (FluidNC keeps the last word's, M7/M8 read it below;
                                             # a valid G28.1/G43.1/G92.1 sets it back to 0)
            if letter == "G" or (letter == "M" and not jog):
                group = (G_GROUPS if letter == "G" else M_GROUPS).get(v)
                if group is None:
                    return self.error(20)
                if group in groups:
                    return self.error(21)    # two of one group on a line: G0 G1, M3 M5, M7 M8...
                groups.add(group)
            elif letter in seen:
                return self.error(25)        # a word twice: X1 X2
            seen.add(letter)
            if letter == "G":
                if v in (0, 1):
                    motion = f"G{v:g}"
                elif v in (20, 21):
                    units = f"G{v:g}"
                elif v in (90, 91):
                    distance = f"G{v:g}"
                elif v not in (17, 54, 94):
                    special = v
            elif letter == "M" and not jog:
                if v in (3, 4, 5):
                    spindle, spindle_word = f"M{v:g}", True
                elif v in (7, 7.1, 8, 8.1, 9):   # (FluidNC drops an M7/M8 without that output, M9 without both)
                    has = {7: self.has_mist, 8: self.has_flood, 9: self.has_mist or self.has_flood}[int(v)]
                    coolant = f"M{int(v)}" if has else None
                else:
                    flow = v                 # M0 M1 M2 M30
            elif letter in "XYZ":
                axes["XYZ".index(letter)] = v
            elif letter == "F":
                feed = v
            elif letter == "S" and not jog:
                speed = v
            elif letter == "P":
                p = v
            elif letter == "L":
                l_word = v
            elif letter not in "NT":
                return self.error(20)
        scale = 25.4 if units == "G20" else 1.0
        if jog:                              # a jog's G20/G91/G53/F apply to the jog only
            if not feed:
                return self.error(22)
            target = self.machine(axes, scale) if special == 53 else self.plan(axes, scale, distance)
            if self.soft_limits:
                target = self.constrain_jog(target)
            return self.jogging(target, feed * scale)
        if special == 10 and (l_word not in (2, 20) or p not in (0, 1)):
            return self.error(20)
        if special == 4 and p is None:
            return self.error(28)            # G4 without P
        new_feed = feed * scale if feed is not None else self.feed
        if axes and special in (None, 53) and motion == "G1" and not new_feed:
            return self.error(22)

        def block():
            self.feed = new_feed
            old = (self.spindle, self.speed)
            self.spindle, self.speed = spindle, speed
            if spindle_word or (speed != old[1] and old[0] != "M5"):
                # FluidNC waits for the moves when the spindle turns on, off or to a new speed (it skips an
                # M3/M5 that changes nothing; here every M3-M5 waits, which the stage 2 tests count on)
                yield self.synced
                if not self.critical:
                    self.spindle_cmd, self.out_speed = spindle, speed
                    self.speed_zeroed = False    # (FS: S x override, also with M5: see fs_speed)
                    if spindle != "M5" and (spindle, speed) != old and self.spinup:
                        t0 = self.now
                        yield lambda t: t - t0 >= self.spinup    # (a spin-up goes on during a hold)
                    self.spindle_out = spindle
                    self.ov_changed()
            if coolant:
                if coolant == "M9":
                    self.flood = self.mist = False
                else:                        # M7.1/M8.1, or a last word like X0.1: off (FluidNC)
                    setattr(self, "mist" if coolant == "M7" else "flood", last != 10)
                yield self.synced
                if not self.critical:
                    self.set_pins()
                    self.ov_changed()
            if special == 4 and int(p * 1000) > 0:   # a dwell (FluidNC: P0 neither waits for the moves nor dwells)
                yield self.synced
                self.dwell = int(p * 1000) / 1000.0
                yield lambda t: self.dwell <= 0
                self.dwell = None
            self.units, self.distance = units, distance
            here = [t - o for t, o in zip(self.target, self.offset())]    # work position where the moves end
            moves = []                       # (target, rate, rapid) this line plans
            if special in (43.1, 49):        # tool length offset (Z): no waiting
                self.tlo = axes.get(2, 0.0) * scale if special == 43.1 else 0.0
            elif special == 10:              # G10 L20: the current position gets these work coordinates (L2: G54's
                yield self.synced            # offsets are these), once the moves are done (FluidNC saves them then)
                for i, v in axes.items():
                    self.wco[i] = v * scale if l_word == 2 else self.wco[i] + here[i] - v * scale
            elif special == 92:              # G92 (and G92.1) change at once, then wait for the moves
                for i, v in axes.items():
                    self.g92[i] += here[i] - v * scale
                yield self.synced
            elif special == 92.1:
                self.g92 = [0.0, 0.0, 0.0]
                yield self.synced
            elif special == 28.1:
                self.g28 = list(self.target)
                yield self.synced
            elif special == 28:              # to the G28 position, through the given point if any
                if axes:
                    moves.append((self.plan(axes, scale, distance), RAPID, True))
                home = [g if not axes or i in axes else t for i, (g, t) in enumerate(zip(self.g28, self.target))]
                moves.append((home, RAPID, True))
            elif special == 53:              # machine coordinates, this line only
                if axes:
                    moves.append((self.machine(axes, scale), RAPID if motion == "G0" else self.feed, motion == "G0"))
            elif axes and special is None:
                moves.append((self.plan(axes, scale, distance), RAPID if motion == "G0" else self.feed, motion == "G0"))
            self.motion = motion
            if moves and not self.critical:  # in a critical alarm FluidNC answers ok but nothing moves
                beyond = self.beyond(moves)
                if beyond:                   # FluidNC doesn't run the line: once the moves before it are done
                    axis, coordinate, target = beyond    # it answers ok, then ALARM:2
                    if self.v4:              # (Limit.cpp limit_error, log_limit_context)
                        lo, hi = TRAVEL[axis]
                        self.send(f"[MSG:INFO: Soft limit exceeded on {AXES[axis]} axis: target {coordinate:.3f} mm, "
                                  f"limit [{lo:.3f}, {hi:.3f}] mm]")
                        self.send("[MSG:INFO: Commanded mpos: " + " ".join(f"{a}{v:.3f}" for a, v in zip(AXES, target))
                                  + "]")
                        self.send(f"[MSG:INFO: GCode line: {code}]")
                    else:
                        self.send(f"[MSG:INFO: Soft limit on {AXES[axis]} target:{coordinate:.3f}]")
                    yield lambda t: not self.moves
                    return ["ok"] + self.critical_alarm(2, hang=False)
                for target, rate, rapid in moves:
                    self.queue_move(target, rate, rapid=rapid)
                if len(self.moves) > PLANNER:
                    yield from self.room(False)
            if flow == 0 and not self.critical:   # M0: once the moves are done a feed hold (in Idle: Hold:0)
                yield self.synced
                self.state, self.hold_at = "Hold", -math.inf
                yield lambda t: self.state != "Hold"      # ok after ~
            elif flow in (2, 30):            # program end: like FluidNC, modes back to G1 G17 G90 G94 G54 M5 M9
                yield self.synced
                self.motion, self.distance, self.spindle, self.flood, self.mist = "G1", "G90", "M5", False, False
                self.f_ovr = self.r_ovr = self.s_ovr = 100
                if not self.critical:
                    self.spindle_cmd = self.spindle_out = "M5"
                    self.out_speed = 0.0
                    self.set_pins()
                    self.ov_changed()
                return ["[MSG:INFO: Program End]", "ok"]
            return ["ok"]

        return block()

    def jogging(self, target, rate):
        """A $J= line: its move is planned at once and the line answered once the planner has room for it."""
        self.queue_move(target, rate, jog=True)
        if len(self.moves) > PLANNER:
            yield from self.room(True)
        return ["ok"]

    def room(self, jog):
        """FluidNC answers a motion line only once it is planned: it waits for room in the planner."""
        self.waiting_jog = jog
        yield lambda t: len(self.moves) <= PLANNER
        self.waiting_jog = False

    def plan(self, axes, scale, distance):
        target, off = list(self.target), self.offset()
        for i, v in axes.items():
            target[i] = target[i] + v * scale if distance == "G91" else v * scale + off[i]
        return target

    def machine(self, axes, scale):
        return [axes[i] * scale if i in axes else t for i, t in enumerate(self.target)]

    def constrain_jog(self, target):
        """Like FluidNC's Cartesian::constrain_jog: a jog stops at the soft limits instead of failing."""
        target = list(target)
        for i, (lo, hi) in enumerate(TRAVEL):
            p, t = self.pos[i], target[i]
            if t == p:
                continue                     # this axis doesn't move
            if t < p < lo or t > p > hi:
                target[i] = p                # already outside: no going further out
            else:
                target[i] = min(max(t, lo), hi)
        return target

    def beyond(self, moves):
        """With --soft-limits, the first (axis, coordinate, target) a move would take outside the travel."""
        if self.soft_limits:
            for target, _, _ in moves:
                for i, (lo, hi) in enumerate(TRAVEL):
                    if not lo <= target[i] <= hi:
                        return i, target[i], target
        return None

    def queue_move(self, target, rate, jog=False, rapid=False):
        """Plans a move from where the queued moves end, slowed down so no axis exceeds its max rate."""
        delta = [t - s for t, s in zip(target, self.target)]
        length = math.hypot(*delta)
        self.target = list(target)
        if length < 1e-9:
            return                           # FluidNC drops a zero-length move (a jog held at a limit)
        unit = [abs(d) / length for d in delta]
        cap = min(m / u for m, u in zip(MAX_RATE, unit) if u)
        accel = min(a / u for a, u in zip(ACCEL, unit) if u)
        self.moves.append((target, min(rate, cap), jog, accel, None if rapid or jog else rate, cap))
        if self.state == "Idle":
            if not jog:
                self.state = "Run"
            elif self.start is None:
                self.start = self.ticks      # FluidNC starts a jog's cycle after its ok (see begin_jog)

    def speed_of(self, move):
        """mm/min a planned move runs at now: like FluidNC's planner, a rapid at the rapid override, a feed
        move at the feed override but no faster than the axes allow, a jog at neither."""
        _, rate, jog, _, feed, cap = move
        if jog:
            return rate
        if feed is None:
            return rate * self.r_ovr / 100.0
        return min(feed * self.f_ovr / 100.0, cap)

    def begin_jog(self):
        if self.start is not None:
            if self.state == "Idle" and self.moves:
                self.state = "Jog"
            self.start = None

    def cancel_jog(self):
        """FluidNC's jog cancel: slows down along the path at the move's acceleration, then Idle (advance)."""
        if self.stop is not None:
            return                           # already slowing down
        if self.waiting_jog:                 # parsed but not planned yet: dropped, answered ok once stopped
            self.moves.pop()
            self.waiting_jog = False
            self.busy, self.wait = self.later(lambda t: self.stop is None, ["ok"]), None
        self.stop = [self.moves[0][1] / 60.0, self.moves[0][3]] if self.moves else [0.0, 1.0]

    # -------------------------------------------------- time passing

    def tick(self, now, dt):
        self.now = now
        if self.banner_at is not None and now >= self.banner_at:
            self.banner_at = None
            self.wco_counter = self.ovr_counter = 0   # (system_reset: WCO in the next report, Ov in the one after)
            self.buf.clear()                 # FluidNC empties its input when it starts
            self.rx_used = self.buf_queued = 0
            self.send("")                    # (an empty line first: Report.cpp report_init_message, both versions)
            self.send(BANNERS[self.fw])
            for text in self.after_banner:   # (neither version prints "'$H'|'$X' to unlock" after it)
                self.send(text)
            if self.kept is not None:        # the main loop runs the line it was given before the reset
                text, self.kept = self.kept, None
                self.note("LEFTOVER " + text)
                self.run(text, now)
        if self.home:
            axes, start, t0 = self.home
            f = min(1.0, (now - t0) / HOME_TIME)
            for i in axes:
                self.pos[i] = start[i] * (1 - f) + 0.0   # machine zero is the home position (+0.0: no "-0.000")
            if f >= 1:
                self.home = None
                self.target = list(self.pos)
                for i in axes:
                    self.homed[i] = True
                self.state = "Idle" if all(self.homed) else "Alarm"   # FluidNC: Alarm while an axis is unhomed
                self.send("[MSG:Homed:" + "".join(AXES[i] for i in axes) + "]")
        elif self.state in ("Run", "Jog"):
            if self.spindle_cmd == "M5" and self.moves:
                self.speed_zeroed = True     # (a move with the spindle off: FS says 0 from then)
            self.advance(dt)
        if self.dwell is not None and not self.suspended(now) and now >= self.busy_until:
            self.dwell -= dt                 # (FluidNC's dwell counts its milliseconds only while not held)
        if self.start is not None and self.start < self.ticks:   # queued before this tick: the cycle starts
            self.begin_jog()
        self.run_events(now)
        self.proceed(now)
        if self.alarm_pause and now >= self.alarm_pause[0]:
            for text in self.alarm_pause[2]:
                self.send(text)
            self.alarm_pause = None
        self.take(now)
        self.ticks += 1

    def advance(self, dt):
        if self.stop is not None:            # a jog cancel: slow down along the path, then forget the rest
            v, a = self.stop
            t = min(dt, v / a)
            self.travel(v * t - a * t * t / 2)
            self.stop[0] = v - a * t
            if self.stop[0] > 1e-9 and self.moves:
                return
            self.moves.clear()               # stopped: FluidNC flushes the planner and syncs its position
            self.target = list(self.pos)
            self.stop = None
        while self.moves and dt > 0:
            target = self.moves[0][0]
            left = math.dist(self.pos, target)
            speed = self.speed_of(self.moves[0]) / 60.0   # mm/s
            if speed * dt >= left:
                self.pos = list(target)
                self.moves.popleft()
                dt -= left / speed if speed else dt
            else:
                f = speed * dt / left
                self.pos = [p + (t - p) * f for p, t in zip(self.pos, target)]
                dt = 0
        if not self.moves:
            self.state = "Idle"

    def travel(self, dist):
        """Moves dist mm along the queued moves."""
        while self.moves and dist > 0:
            target = self.moves[0][0]
            left = math.dist(self.pos, target)
            if dist >= left:
                self.pos = list(target)
                self.moves.popleft()
                dist -= left
            else:
                self.pos = [p + (t - p) * dist / left for p, t in zip(self.pos, target)]
                dist = 0

    def fs_speed(self, now):
        """FS's spindle speed, as FluidNC's mapSpeed sets it: the S times the spindle override, in whole numbers,
        also after M5 (s0_with_disable is off) until a move runs with M5; 0 after a reset, M2/M30 or a 0x9E spindle
        stop. --spindle vfd: the speed from before an override until the VFD has got there (VFD_SYNC)."""
        if self.spindle_stop or self.speed_zeroed:
            return 0
        if self.spindle_type == "vfd" and now < self.vfd_until:
            return self.vfd_from
        return int(self.out_speed) * self.s_ovr // 100

    def report(self, now):
        # (FluidNC asleep after an ALARM: 3.9.4 reports the state from before, 4.1.1 Alarm, set before the pause)
        state = self.alarm_pause[1] if self.alarm_pause and not self.v4 else self.state
        if state == "Hold":
            state += ":1" if now - self.hold_at < HOLD_DECEL else ":0"
        if self.stop is not None:
            rate = self.stop[0] * 60.0
        else:
            rate = self.speed_of(self.moves[0]) if self.state in ("Run", "Jog") and self.moves else 0
        rpm = self.fs_speed(now)
        k, d = (25.4, 4) if self.inches else (1.0, 3)     # report_inches: inches with 4 decimals
        parts = [state, "MPos:" + ",".join(f"{v / k:.{d}f}" for v in self.pos), f"FS:{rate / k:.0f},{rpm}"]
        busy = self.state in BUSY                # FluidNC's counters (Report.cpp): WCO in every 10th report (30th
        if self.wco_counter > 0:                 # while busy) ...
            self.wco_counter -= 1
        else:
            self.wco_counter = (WCO_BUSY if busy else REPORT_EVERY) - 1
            if self.ovr_counter == 0:
                self.ovr_counter = 1         # ... and an Ov due in that report comes in the next one
            parts.append("WCO:" + ",".join(f"{v / k:.{d}f}" for v in self.offset()))
        if self.ovr_counter > 0:             # ... Ov in every 10th (20th while busy), or the next after a change
            self.ovr_counter -= 1
        else:
            self.ovr_counter = (OV_BUSY if busy else REPORT_EVERY) - 1
            parts.append(f"Ov:{self.f_ovr},{self.r_ovr},{self.s_ovr}")
            turning = not self.spindle_stop and (self.spindle_type != "onoff" or rpm >= 1)   # (OnOff: off below S 1)
            on = {"M3": "S", "M4": "C"}.get(self.spindle_out, "") * turning
            if self.readback:                # (--no-readback: an output-only GPIO reads 0, so A: never says F or M)
                on += "F" * self.pin_flood + "M" * self.pin_mist
            if on:
                parts.append("A:" + on)
        self.send("<" + "|".join(parts) + ">")


def main():
    ap = argparse.ArgumentParser(description="A pretend FluidNC on a pseudo terminal")
    ap.add_argument("--fw", choices=FIRMWARES, default="4.1.1", help="the FluidNC version it acts as (4.1.1)")
    ap.add_argument("--alarm", action="store_true", help="boot in Alarm with every axis unhomed (must home)")
    ap.add_argument("--soft-limits", action="store_true", help="soft limits: X -800..0, Y -600..0, Z -100..0 mm")
    ap.add_argument("--inches", action="store_true", help="report_inches on: status in inches")
    ap.add_argument("--no-settings", action="store_true", help="every $/... setting query answers error:3")
    ap.add_argument("--stall-after", type=int, metavar="N", help="answer N lines, then no more lines")
    ap.add_argument("--banner-delay", type=float, default=0.0, metavar="S", help="seconds before the banner")
    ap.add_argument("--spinup", type=float, default=0.0, metavar="S", help="M3/M4 spin-up delay in seconds")
    ap.add_argument("--no-flood", action="store_true", help="no flood (M8) output: M8 and 0xA0 do nothing")
    ap.add_argument("--no-mist", action="store_true", help="no mist (M7) output: M7 and 0xA1 do nothing")
    ap.add_argument("--no-readback", action="store_true",
                    help="the coolant pins are plain GPIOs (gpio.25, gpio.26): A: never shows F or M")
    ap.add_argument("--coolant-delay", type=int, default=0, metavar="MS", help="$/coolant/delay_ms (0)")
    ap.add_argument("--spindle", choices=tuple(SPINDLE_LINES), default="pwm",
                    help="the spindle type $SS names (pwm; onoff: off below S 1 after the override; vfd: slow)")
    ap.add_argument("--vfd-sync", type=float, default=0.0, metavar="S",
                    help="--spindle vfd: a spindle override change while it stands keeps FluidNC busy S seconds")
    ap.add_argument("--late-events", action="store_true",
                    help="override bytes and coolant toggles act after the lines that came in the same read")
    ap.add_argument("--rx-buffer", type=int, default=RX_SIZE, metavar="N",
                    help=f"receive buffer bytes (default {RX_SIZE}; 0: no limit)")
    ap.add_argument("--ov-at-once", action="store_true",
                    help="Ov in the next report after any spindle, coolant or override change")
    ap.add_argument("--no-verbose-errors", action="store_true", help="error:N without [MSG:ERR: ...] after it")
    ap.add_argument("--clean-reset", action="store_true",
                    help="a reset drops every line (FluidNC 3.9.4 runs the one it took during a hold)")
    ap.add_argument("--log", metavar="FILE", help="append what is received to FILE")
    args = ap.parse_args()
    if args.rx_buffer < 0:
        ap.error("--rx-buffer must be 0 or more")
    if not (math.isfinite(args.vfd_sync) and 0 <= args.vfd_sync <= 3):
        ap.error("--vfd-sync must be 0 to 3 seconds (FluidNC alarms after 3 s without the speed)")

    master, slave = os.openpty()
    tty.setraw(slave)            # the port side: no echo, no line editing (kept after it is closed)
    path = os.ttyname(slave)
    os.close(slave)              # FluidSender won't open a port another program has open
    os.set_blocking(master, False)
    log = open(args.log, "a", buffering=1) if args.log else None
    sim = FakeFluidNC(master, args.alarm, args.stall_after, args.banner_delay, log, args.spinup,
                      args.inches, args.soft_limits, not args.no_settings, not args.no_flood, not args.no_mist,
                      args.rx_buffer, args.ov_at_once, not args.no_verbose_errors, not args.clean_reset,
                      fw=args.fw, readback=not args.no_readback, spindle=args.spindle,
                      coolant_delay=args.coolant_delay, late_events=args.late_events, vfd_sync=args.vfd_sync)
    signal.signal(signal.SIGUSR1, lambda *_: setattr(sim, "tripped", True))   # handled between ticks
    last = time.monotonic()
    sim.tick(last, 0.0)          # the boot banner goes out before anyone can open the port
    print(path, flush=True)
    if sys.stderr.isatty():
        print(f"Fake FluidNC {args.fw} on {path}  (Ctrl+C quits; kill -USR1 {os.getpid()} trips a hard limit)",
              file=sys.stderr)
    try:
        while True:
            ready = select.select([master], [], [], TICK)[0]
            now = time.monotonic()
            if ready:
                try:
                    data = os.read(master, 4096)
                except BlockingIOError:
                    data = b""
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    data = b""               # no one has the port open
                    time.sleep(TICK)
                sim.receive(data, now)
            if sim.tripped:
                sim.tripped = False
                sim.hard_limit()
            now = time.monotonic()
            sim.tick(now, now - last)
            last = now
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
