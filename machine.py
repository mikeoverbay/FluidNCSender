"""
FluidNC connection for FluidSender: one serial port, opened only when the user presses Connect.

Python standard library only (termios). A port is opened only if no other program has it open,
and is then held exclusively (TIOCEXCL), so FluidSender never fights UGS, the probe or the
pendant for a port. It never connects or reconnects by itself.

The only bytes FluidSender sends on its own: one ^L (0x0C) when it connects, before the first '?'
(it turns off FluidNC's UART echo mode, which an earlier terminal session can leave on); '?' every
200 ms (status; every 100 ms while a hold jog runs or a jog is being stopped, every 15 ms for a moment
after the vacuum or AUX switched or an override changed); $I, $G ($T too when it is in an alarm), the
settings it reads (never sets) when it connects: $/axes/<x|y|z>/max_rate_mm_per_min,
acceleration_mm_per_sec2 and soft_limits, $/planner_blocks, $/coolant/flood_pin, mist_pin and delay_ms,
and $SS (its startup log: the spindle type); a quiet $G after a vacuum/AUX toggle or at a job's end when
FluidNC's reports can't show those outputs (see Link.got_coolant_setting); G20 after connecting or after a
controller reset, once FluidNC is Idle; jog cancels
(0x85, repeated while a report shows the jog not slowing down) when a jog must stop; one cycle start
(~) if FluidNC takes no line after a jog cancel although it says Idle (see Link.unstick); and a feed
hold, then one more Soft Reset once it has stopped, when FluidNC runs a line left from before a reset
(a FluidNC quirk, 3.9.4 and 4.1.1: see Link.stray); and the rest of the spindle override steps the user
asked for, once a report shows the last ones (no override byte ever goes twice: see Link.ov_report). Until that
G20 has been answered, the user's G-code waits
for it, so nothing runs in millimeters. Everything else comes from the user: typed lines, the
realtime buttons, and the Jog, Zero, Home and Unlock buttons, whose lines ($J=, G10 L20 (G10 L2 for
Undo Zero), $H, $X; jogs always with G20, G10 with G20, or with G21 if the user has chosen
millimeters: a units word in a G10 stays in force) the console shows like typed ones; Go to XY/Z zero
(G20 G90 G0 ..., G53 G0 ... for machine zero); the vacuum and AUX switches (FluidNC's realtime toggles
0xA0 and 0xA1); the feed, rapid and spindle overrides (its realtime bytes 0x90-0x9D); and Run, Hold, Resume
and Stop. FluidNC 4.1.1 (the user's) and 3.9.4 both work.

A job (Run) streams the loaded program, after a G20 G90 of FluidSender's own (a program without
units runs in inches, as right after connecting; its own G21 or G91 still win): as many of its lines
as fit in 128 bytes are in FluidNC's hands at once (character counting, as UGS does on this machine),
and the replies answer them in order. On an error reply FluidSender writes '!' (feed hold) at once
and the job pauses there; Resume goes on with the next line. Resume writes '~' only once FluidNC
reports Hold:0 (it drops a '~' while still slowing down). Stop is the safe stop: '!', then once a
report shows FluidNC has stopped (or after 10 s), Ctrl-X, which keeps the position when nothing moves
and turns the spindle and coolant off; Stop does it for any move that may be under way, also one
whose ok has just come before a report shows it running. An alarm, a reset or a lost connection ends
a job at once; nothing more of it is sent. Disconnect (or stopping the server) during a job, a scan or
a move does the safe stop first and closes the port only then: FluidNC is never left holding, the
spindle on, with nobody watching. The vacuum and AUX switches follow M7/M8/M9 lines (and M2/M30) as
soon as FluidNC answers them, then its reports.

A hold jog (a jog button held down) is a stream of short $J pieces, the next one sent each time
FluidNC answers the last one (it answers once it has room in its planner; with a planner of more
than 16 blocks, at most 15 pieces not yet run are sent), and only while the page keeps saying the
button is held (every 100 ms; after 0.4 s without it the jog stops). So if the page, FluidSender
or the USB cable dies, the machine runs out only the short distance already planned. A hold jog
that FluidNC stopped by itself (a feed hold button, a soft limit) ends here too: it never starts
again without a new press. FluidNC reports millimeters; positions and feeds are converted to
inches here, so the page never sees millimeters.
"""

import errno
import fcntl
import json
import math
import os
import re
import select
import termios
import threading
import time
import traceback
from collections import deque

from job import WORD, WORDS, ProgramError, clock_text, coolant_after, coolant_words, parse_program
from leveling import Leveling, LevelingError, pre_leveled
from settings import ZERO_KEEP, Settings, SettingsError, jog_feed, jog_step

MM = 25.4
POLL = 0.2            # status request interval
NO_RESPONSE = 2.0     # no status report for this long: state "No response"
FIRST_STATUS = 2.0    # connecting gives up if '?' gets no answer this soon
LINE_TIMEOUT = 10.0   # wait this long for a line's ok/error (not counting time FluidNC is busy moving) ...
LONG_TIMEOUT = 120.0  # ... or this long for homing and probing, which answer when they finish
STALL = 5.0           # a line unanswered this long while status still answers: stalled
STATUS_MIN = 0.2      # at most 5 status events a second,
STATUS_EVERY = 2.0    # and at least one every 2 s
RELEASE_AFTER = 10.0  # close the port when no page has been open for this long
RESET_WAIT = 2.0      # after our Ctrl-X, lines wait this long at most for FluidNC's reset banner
WAIT_NOTE = 0.5       # a line still waiting this long is shown as waiting (else it looks lost, and is typed again)
WRITE_TIMEOUT = 3.0
TICK = 0.1            # the reader thread checks its timers at least this often
MAX_LINE = 254        # characters: FluidNC keeps 254 of a line and drops the rest without a word ($ lines) ...
MAX_GCODE = 127       # ... and FluidNC 4.1.1 refuses G-code lines (and $J= jogs) over 127 characters (error:14)
MAX_QUEUE = 100
INCH_TRIES = 3
CONSOLE_KEEP = 200
FAST_POLL = 0.1       # status request interval while a hold jog runs or a jog is being stopped
DEADMAN = 0.4         # a hold jog stops when the page hasn't said "still held" for this long ...
JOG_STALE = 0.5       # ... or when no status report has come for this long
JOG_SETTLE = 0.05     # FluidNC starts a jog just after its ok: only reports this much later show it
CANCEL_EVERY = 0.1    # a jog that still runs gets another 0x85 at most this often,
CANCEL_TRIES = 30     # and after this many it gives up, and says so
UNSTICK_AFTER = 1.5   # a line unanswered this long after a jog cancel while FluidNC says Idle: see Link.unstick
MAX_STEPS = 5         # step jogs waiting or still moving
MAX_STEP = 40.0       # in: the longest step jog
MAX_FEED = 10000.0    # in/min: any jog speed above this is a mistake
MAX_ZERO = 100.0      # in: the largest work position Zero sets
ACCEL = 50.0          # mm/s^2 for the hold-jog pieces when FluidNC's acceleration couldn't be read
BLOCKS = 16           # FluidNC's planner_blocks default; hold-jog pieces never count on a bigger planner
ALARM_PAUSE = 0.7     # FluidNC sleeps 0.5 s after printing ALARM:N, and its reports meanwhile show the old state
JOB_BUFFER = 128      # bytes of job lines in FluidNC's hands at most (its serial buffer is 256), as UGS does
JOB_EVERY = 0.5       # a running job's progress goes to the pages at most this often
SETTLE = 0.05         # FluidNC acts on a realtime byte or starts a move just after: only reports this much
                      # later show it
STOP_WAIT = 10.0      # the safe stop waits this long for FluidNC to stop after '!' before it resets anyway
RESUME_WAIT = 5.0     # Resume waits this long for FluidNC to finish slowing down (Hold:1 -> Hold:0)
BURST_POLL = 0.015    # after a vacuum/AUX toggle or override bytes, status requests this often ...
BURST_REPORTS = 11    # ... until a report says them (4.1.1: the next one; 3.9.4 every 10th: at most 11 reports, it
BURST_TIME = 1.0      # gives up while busy, when that is every 20th), or for this long at most (no answer)
SAFE_Z = 0.2          # in: Go to XY 0 lifts Z to this first when it is lower (the user's UGS safety height)
MAX_SCAN_XY = 100.0   # in: a scan point further out than this is a mistake
MOVE_SETTLE = 0.25    # Stop takes FluidNC as maybe moving until a report this long after the last ok to a line
                      # that can move it (it starts the move just after the ok; the rest is margin): see may_move

BAUDS = {b: getattr(termios, f"B{b}") for b in (9600, 19200, 38400, 57600, 115200, 230400, 460800,
                                                500000, 921600, 1000000, 1500000, 2000000)
         if hasattr(termios, f"B{b}")}
JOG_CANCEL = b"\x85"  # acts only while FluidNC jogs; never Ctrl-X for that (a reset while moving is ALARM:3)
REALTIME = {"hold": b"!", "resume": b"~", "reset": b"\x18", "status": b"?", "jogcancel": JOG_CANCEL}
# FluidNC's coolant toggles: at once, and only in Idle, Run or Hold. Flood (M8) is the user's vacuum,
# mist (M7) the AUX relay
COOLANT = {"vacuum": ("flood", b"\xa0", "Vacuum", "M8"), "aux": ("mist", b"\xa1", "AUX", "M7")}
COOLANT_STATES = ("Idle", "Run", "Hold")
# FluidNC's override bytes (realtime: they act at once, in any state; Config.h, RealtimeCmd.cpp). Ov reports them as
# feed, rapid, spindle (%). Feed and spindle: 100 % ("reset"), +-10, +-1, each clamped to 10..200; rapid: 100, 50 or
# 25 % (FluidNC's 5 %, 0x98, is not offered); 0x9E spindle stop isn't either
OV_NAMES = ("feed", "rapid", "spindle")
OV_TITLES = ("Feed", "Rapid", "Spindle")
OV_MIN, OV_MAX = 10, 200
OV_STEPS = {"feed": {"reset": 0x90, 10: 0x91, -10: 0x92, 1: 0x93, -1: 0x94},
            "spindle": {"reset": 0x99, 10: 0x9A, -10: 0x9B, 1: 0x9C, -1: 0x9D}}
RAPID_BYTES = {100: 0x95, 50: 0x96, 25: 0x97}
OV_RESET = bytes([0x90, 0x95, 0x99])
OV_GIVE_UP = 10.0       # s: FluidSender stops waiting for an override when FluidNC's value hasn't moved this long
                        # (a VFD getting to the program's speed can keep FluidNC from applying it for seconds); its
                        # bytes never go again (see Link.ov_report)
SPINDLE_STEPS = 2       # spindle override bytes per report at most: FluidNC sets the spindle at each (a VFD with speed
                        # feedback waits for its speed every time, ALARM:10 after 3 s), so FluidSender goes on only once
                        # a report shows the last ones took (as UGS does, one at a time)
JOB_RUNNING = "A job is running"
NO_ANSWER_WHY = "FluidNC stopped answering"     # (why a job paused when its line got no reply in time)
OUTPUTS_ON = "ALARM: the spindle and vacuum/AUX may still be on. Soft Reset turns them off."
# (release_extras let go of a probe that wasn't homed: nothing stops X / Y moves, the page only warns)
PROBE_LET_GO_DOWN = ("Released while not homed, the probe may be down: Home Probe before moving X or Y "
                     "(connect it first)")
RESET_ALARMS = (3, 6, 14)   # the alarms a reset prints (moving, homing, unhomed; 4.1.1: 3 from Sleep too): the reset
                            # turns everything off
STOPPING = "Stopping: FluidNC is being held, then reset; try again in a second"
NO_RESUME = "Stopping: FluidNC is being held, then reset"
RESETTING = "FluidNC is being reset: nothing to resume"
JUST_RESET = ("FluidNC has just been reset: nothing to resume, and a Resume now could run a line left from before the "
              "reset (if FluidNC stays held, Soft Reset)")
OLD_HOLD = "FluidNC is in Hold, probably with lines from before: Soft Reset clears them (Resume would run them)"
# FluidNC takes a feed hold in a critical alarm (a hard or soft limit, reported as Alarm): it holds, and the reset
# after it then lands in Idle with no alarm, unhomed (Protocol.cpp protocol_do_feedhold, protocol_do_rt_reset)
IN_ALARM = ("FluidNC is in ALARM: Feed Hold/Resume do nothing useful there, and after a hard or soft limit a Feed "
            "Hold would let FluidNC out of the alarm without homing. Soft Reset (critical) or Unlock/Home")
LEFTOVER = "FluidNC ran a line left from before the reset (a FluidNC quirk; 4.1.1 too); held it and resetting again"
LINE_EDIT_OFF = b"\x0c"     # ^L: turns off FluidNC's UART echo and editing mode (lineedit.cpp); else ignored
MOVING = ("Run", "Hold", "Jog", "Home", "Door")    # states in which Stop holds FluidNC first (see Link.may_move)
# the work coordinate systems: G54 = 1 (G10's P1) ... G59 = 6; FluidNC 4.1.1 adds G59.1-G59.3 (P7-P9)
WCS_CODES = {54.0: 1, 55.0: 2, 56.0: 3, 57.0: 4, 58.0: 5, 59.0: 6, 59.1: 7, 59.2: 8, 59.3: 9}
GOTO = {"z": ["G20 G90 G0 Z0"], "mz": ["G53 G0 Z0"], "my": ["G53 G0 Y0"], "mx": ["G53 G0 X0"]}
GOTO_NAMES = {"xy": "Go to XY 0", "z": "Go to Z 0", "zero": "Return to zero", "mx": "Machine X0", "my": "Machine Y0",
              "mz": "Machine Z0"}
# what FluidNC's error:N and ALARM:N mean (as the page's codes.js says them)
ERRORS = {
    1: "expected a command letter", 2: "bad number format", 3: "unknown $ command", 4: "negative value",
    5: "homing is not enabled", 6: "step pulse too short", 7: "settings read failed",
    8: "$ command only works when Idle", 9: "locked out during alarm or jog", 10: "soft limits need homing",
    11: "line too long", 12: "step rate too high", 13: "safety door is open",
    14: "line too long (FluidNC takes 127 characters of G-code)",
    15: "jog target is beyond the travel", 16: "invalid jog command", 17: "laser mode needs PWM",
    18: "no homing cycle defined in the settings", 19: "single axis homing not allowed",
    20: "unsupported or invalid G-code", 21: "two G-codes from the same group", 22: "feed rate not set",
    23: "needs a whole number", 24: "two G-codes both need the axis words", 25: "repeated G-code word",
    26: "axis words missing", 27: "line number out of range", 28: "P or L value missing",
    29: "unsupported coordinate system", 30: "G53 needs G0 or G1", 31: "axis words while G80 is active",
    32: "arc without axis words in the plane", 33: "invalid motion target", 34: "arc radius error",
    35: "arc offset (I/J/K) missing", 36: "unused words in the line", 37: "G43.1 on the wrong axis",
    38: "tool number too high", 39: "P value too big", 40: "a startup pin (door, control or limit) is on",
    120: ("FluidNC is busy with a macro or file of its own (after_reset/after_homing/after_unlock...); try again "
          "when it is done"),
    177: "loops (do/while/repeat) need a FluidNC file job: not over USB",
}
ALARMS = {
    1: "hard limit: position likely lost, home again", 2: "soft limit: target beyond the travel",
    3: "reset while moving: position likely lost, home again", 4: "probe fail: probe not in its start state",
    5: "probe fail: no contact", 6: "homing fail: reset during homing", 7: "homing fail: door opened",
    8: "homing fail: could not pull off the switch", 9: "homing fail: switch not found", 10: "spindle control",
    11: "an input pin (door, control or limit) was on", 12: "homing: ambiguous switch", 13: "hard stop",
    14: "unhomed: home the machine", 15: "init", 16: "I/O expander reset: reboot FluidNC",
    17: "G-code error in a FluidNC file job", 18: "probe touched during a move (probe hard limit)",
}
RISKY = {"usb-acm": "the Z probe (Arduino Leonardo)", "bluetooth": "the Bluetooth jog pendant"}
KNOWN_USB = {("1a86", "7523"): "CH340 USB serial (FluidNC?)", ("2341", "8036"): "Arduino Leonardo (Z probe)"}
SYS_TTY = "/sys/class/tty"
# FluidNC answers a line only when it is done, and lines wait behind moves (M3/M5/M8/M9, G4, G10, a full
# planner, M0 until resumed...): while it reports one of these states, a waiting line is not overdue
BUSY_STATES = ("Home", "Run", "Jog", "Hold", "Door")
RESTING = ("Idle", "Alarm")         # the only states the unwatched port is released in (an Alarm only with the
                                    # spindle and vacuum/AUX off: see Link.check_release)
NO_INCHES = ("FluidNC may still be in millimeters: G20 failed. Send G20 yourself first "
             "(or put G20 or G21 in the line)")
JOGGING = "Jogging: let go of the jog button first"
CRITICAL = "FluidNC needs a Soft Reset first ([MSG:Reset to continue])"
STATE_WHY = {           # why a button can't act in this state (the page's reasons, as far as they go)
    "Alarm": "ALARM: Unlock or Home first",
    "Run": "Running: wait until FluidNC is Idle",
    "Hold": "Hold: Resume or Soft Reset first",
    "Home": "Homing: wait until it is done",
    "Jog": "Jogging: wait until it has stopped",
    "Door": "The safety door is open",
    "Check": "Check mode ($C) is on",
    "Sleep": "FluidNC is asleep: Soft Reset first",
}
AXES = "xyz"
# read when connecting: FluidNC caps jog speeds at max_rate itself, but the page's speed slider and the
# hold-jog pieces need the numbers too. With soft_limits false (FluidNC's default) FluidNC doesn't keep jogs
# inside the travel, which the Jog panel then says. The coolant pins tell how the vacuum and AUX switches can
# know their state (see Link.got_setting), and delay_ms what switching one on in a job does (see R5 in the page)
SETTINGS = (tuple(f"$/axes/{a}/max_rate_mm_per_min" for a in AXES)
            + tuple(f"$/axes/{a}/acceleration_mm_per_sec2" for a in AXES) + ("$/planner_blocks",)
            + tuple(f"$/axes/{a}/soft_limits" for a in AXES)
            + ("$/coolant/flood_pin", "$/coolant/mist_pin", "$/coolant/delay_ms"))
SPINDLE_READ = "$SS"    # last of the reads: FluidNC's startup log, whose spindle line names the type (quiet)
READS = SETTINGS + (SPINDLE_READ,)
SPINDLE_TYPES = ("NoSpindle", "OnOff", "Relay", "PWM", "Laser", "10V", "BESC", "DAC", "HBridge", "PlasmaSpindle",
                 "Huanyang", "H2A", "H100", "YL620", "NowForever", "SiemensV20", "DanfossVLT2800", "ModbusVFD",
                 "DeltaMS300", "MollomG70", "FolinnBD600")
# "[MSG:INFO: PWM Spindle Ena:gpio.12 Out:...]", "[MSG:INFO: Laser Ena:...]", "[MSG:INFO: Huanyang Spindle Tx:...]"
SPINDLE_LINE = re.compile(r"^\[MSG:INFO: (" + "|".join(map(re.escape, SPINDLE_TYPES)) + r") (?:Spindle\b|Ena:)")
STATE_REPLY = re.compile(r"^State \d+ \((\w+)\)$")   # $T: "State 11 (Critical)" (4.1.1; 3.9.4: State 10)
COOLANT_OF = {"flood": (8, 9), "mist": (7, 9)}        # the M words that change each output
COOLANT_PIN_READS = {"$/coolant/flood_pin": "flood", "$/coolant/mist_pin": "mist"}
NO_PIN = "NO_PIN"

# "Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"; lines starting with "[" never count (see is_banner)
BANNER = re.compile(r"\bGrbl\s+\S+\s+\[.*for help\]")
ERROR = re.compile(r"error(?::|\s|$)")     # "error:20", or with verbose_errors "error: Bad number format"
LONG_LINE = re.compile(r"^\$H|G\s*38", re.I)
COMMENT = re.compile(r"\([^)]*\)|;.*")
GWORD = re.compile(r"G(\d+(?:\.\d+)?)")
MWORD = re.compile(r"M(\d+)")
PWORD = re.compile(r"P(\d+\.?\d*|\.\d+)")
SETTING = re.compile(r"^\$/([\w/]+)=(.*)$")   # "$/axes/x/max_rate_mm_per_min=5000.000"


class MachineError(Exception):
    """A refused call. The message is for the user; status is the HTTP status to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ parsing (no state)

def _numbers(val):
    """'1.5,-2,3' -> [1.5, -2.0, 3.0]; ValueError for anything but finite numbers (nan, inf, 1e999: line noise)."""
    nums = [float(v) for v in val.split(",")]
    if not all(map(math.isfinite, nums)):
        raise ValueError(f"not a finite number: {val!r}")
    return nums


def _reported_in_inches(val):
    """FluidNC prints X Y Z with 3 decimals in millimeters and with 4 in inches (report_inches on)."""
    return all(len(v.partition(".")[2]) == 4 for v in val.split(",")[:3])


def parse_status(line):
    """'<Idle|MPos:1.000,2.000,3.000|FS:0,0>' -> its fields in millimeters, or None if it isn't a status report.
    A report in inches (report_inches on) is converted to millimeters and marked 'inches': True."""
    if not (line.startswith("<") and line.endswith(">")):
        return None
    fields = line[1:-1].split("|")
    state, _, sub = fields[0].partition(":")
    if not state.isalpha():
        return None
    out = {"state": state, "sub": sub or None}
    for field in fields[1:]:
        key, _, val = field.partition(":")
        try:
            if key in ("MPos", "WPos", "WCO"):
                nums = _numbers(val)
                if _reported_in_inches(val):
                    nums[:3] = [v * MM for v in nums[:3]]     # (extra axes keep their own units)
                    out["inches"] = True
                out[key] = nums
            elif key == "FS":
                nums = _numbers(val)
                out["feed"] = nums[0]
                out["spindle"] = nums[1] if len(nums) > 1 else None
            elif key == "F":
                out["feed"] = _numbers(val)[0]
            elif key == "Ov":
                out["ov"] = [int(v) for v in _numbers(val)][:3]
            elif key == "A":
                out["acc"] = val                # spindle S (cw) / C (ccw), F flood, M mist; only with Ov
            elif key == "Pn":
                out["pins"] = val
            elif key == "Bf":
                out["buf"] = [int(v) for v in val.split(",")][:2]
            elif key == "Ln":
                out["line"] = int(val)
        except (ValueError, IndexError):
            continue  # a damaged field: skip it, keep the rest
    if "MPos" not in out and "WPos" not in out:
        return None
    if out.get("inches") and out.get("feed") is not None:
        out["feed"] *= MM                                     # reported in inches per minute
    return out


def _add(a, b, sign):
    n = max(len(a), len(b))
    a, b = a + [0.0] * (n - len(a)), b + [0.0] * (n - len(b))
    return [x + sign * y for x, y in zip(a, b)]


def _inches(v):
    return None if v is None else [round(x / MM, 6) for x in (v + [0.0, 0.0, 0.0])[:3]]


def _num(v):
    return int(v) if v is not None and math.isfinite(v) and v == int(v) else v


class StatusParser:
    """Turns status reports into status events in inches. FluidNC sends WCO (work offset) and Ov
    (overrides) only every few reports (every 10th, 30th / 20th while busy; FluidNC 4.1.1 puts Ov in the next
    one after any spindle, coolant or override change too), so both are remembered between reports. The spindle
    and coolant come only in the reports with Ov ("|Ov:...|A:SF", no A: all off): flood and mist are None until
    one came (or $G said them: see Link.handle_line). FluidNC builds A:'s F and M by reading its coolant pins
    back, and a plain ESP32 GPIO set up as an output reads 0: for an output on one (readback False, see
    Link.got_coolant_setting) A: says nothing, and FluidNC's M7/M8/M9 state as FluidSender follows it decides."""

    def __init__(self):
        self.wco = None
        self.ov = None              # [feed, rapid, spindle] %: reported, or expected after our override bytes ...
        self.ov_trust_after = None  # ... until a report with Ov received from then on (made after them) says it
        self.ov_told = False        # the last report was one (FluidNC's override values) ...
        self.ov_before = None       # ... and the values known just before it
        self.readback = {"flood": True, "mist": True}   # A:'s F / M say the output (see above)
        self.acc = None             # A:'s letters in the last trusted report with Ov ...
        self.before = None          # ... and (flood, mist) as they were before it
        self.mpos = None            # the last machine position X Y Z, in millimeters as reported (Undo Zero)
        self.inches = False         # the last report was in inches (FluidNC's report_inches is on)
        self.flood = None           # coolant flood (M8: the vacuum) and mist (M7: AUX): True, False, None
        self.mist = None
        self.spindle = None         # 'cw', 'ccw', 'off' or None
        self.trust_after = None     # reports with Ov received before this may be from before our toggle
        self.trusted = True         # the last report came after that (FluidNC made it after the toggle) ...
        self.told = False           # ... and it had Ov: it said the spindle and the coolant

    def reset(self):
        self.wco = None
        self.ov = None
        self.ov_trust_after = None

    def feed(self, line, now=None):
        p = parse_status(line)
        if p is None:
            return None
        self.inches = bool(p.get("inches"))
        if "WCO" in p:
            self.wco = p["WCO"]
        self.trusted = now is None or self.trust_after is None or now >= self.trust_after
        self.told = self.trusted and "ov" in p
        self.ov_told = "ov" in p and (now is None or self.ov_trust_after is None or now >= self.ov_trust_after)
        if "ov" in p:
            if self.ov_told:
                self.ov_before, self.ov = self.ov, p["ov"]
            if self.trusted:
                acc = self.acc = p.get("acc", "")
                self.before = (self.flood, self.mist)
                if self.readback["flood"]:
                    self.flood = "F" in acc
                if self.readback["mist"]:
                    self.mist = "M" in acc
                self.spindle = "cw" if "S" in acc else "ccw" if "C" in acc else "off"
        mpos, wpos = p.get("MPos"), p.get("WPos")
        if self.wco is not None:
            if mpos is None:
                mpos = _add(wpos, self.wco, 1)
            elif wpos is None:
                wpos = _add(mpos, self.wco, -1)
        if mpos is not None:
            self.mpos = (mpos + [0.0, 0.0, 0.0])[:3]
        feed = p.get("feed")
        return {
            "state": p["state"],
            "sub": p["sub"],
            "wpos": _inches(wpos),            # None until the first WCO arrives
            "mpos": _inches(mpos),
            "abc": [round(v, 4) for v in (wpos or [])[3:]],   # extra axes keep their own units
            "feed": None if feed is None else round(feed / MM, 3),
            "spindle": _num(p.get("spindle")),
            "ov": self.ov,
            "flood": self.flood,
            "mist": self.mist,
            "pins": p.get("pins", ""),
            "buf": p.get("buf"),
            "line": p.get("line"),
            "stalled": False,
            "raw": line,
        }


def is_banner(line):
    """FluidNC's welcome line, printed when it starts or resets.
    [MSG:...] lines never count, even with brackets inside."""
    return not line.startswith("[") and bool(BANNER.search(line))


def msg_ends(text, tail):
    """A whole [MSG:...] message that ends with tail: FluidNC 4.1.1 quotes the user's G-code inside messages
    ("[MSG:ERR: Bad GCode: ...]", "[MSG:INFO: GCode line: ...]"), so a comment there can't pass for one."""
    return text.startswith("[MSG:") and text.endswith(tail + "]")


def parse_gc(line):
    """'[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]' -> {'units': 'G21', 'distance': 'G90', 'gc': 'G0 G54 ...'}"""
    if not (line.startswith("[GC:") and line.endswith("]")):
        return None
    gc = line[4:-1].strip()
    words = gc.split()
    return {
        "units": next((w for w in words if w in ("G20", "G21")), None),
        "distance": next((w for w in words if w in ("G90", "G91")), None),
        "gc": gc,
    }


def gc_coolant(gc):
    """The coolant in $G's words: (flood, mist), e.g. 'M5 M9' -> (False, False), 'M7 M8' -> (True, True);
    None if it doesn't say."""
    words = gc.split() if gc else []
    if "M9" in words:
        return False, False
    if "M7" in words or "M8" in words:
        return "M8" in words, "M7" in words
    return None


def reply_meaning(reply):
    """'error:20' -> 'unsupported or invalid G-code'; 'ALARM:1' -> 'hard limit: ...'; None if not known."""
    kind, _, code = reply.partition(":")
    return (ERRORS if kind == "error" else ALARMS if kind == "ALARM" else {}).get(
        int(code) if code.strip().isdigit() else None)


def explain(reply):
    """'error:20' -> 'error:20 (unsupported or invalid G-code)'; 'ALARM:1' -> 'ALARM:1 (hard limit: ...)'"""
    text = reply_meaning(reply)
    return f"{reply} ({text})" if text else reply


def parse_ver(line):
    """'[VER:3.7 FluidNC v3.9.4:]' -> '3.7 FluidNC v3.9.4'"""
    if not (line.startswith("[VER:") and line.endswith("]")):
        return None
    return line[5:-1].strip().removesuffix(":").strip() or None


def _code(line):
    """A line as FluidNC reads it: upper case, without comments and spaces."""
    return COMMENT.sub("", line).upper().replace(" ", "")


def modal_words(line):
    """The units and distance-mode words of a G-code line, e.g. {'units': 'G20', 'distance': 'G91'}.
    $ commands (and $J= jogs, whose words apply to the jog only) change nothing."""
    if line.lstrip().startswith("$"):
        return {}
    code = _code(line)
    out = {}
    for g in GWORD.findall(code):
        v = float(g)
        if v in (20, 21):
            out["units"] = f"G{int(v)}"
        elif v in (90, 91):
            out["distance"] = f"G{int(v)}"
    if any(int(m) in (2, 30) for m in MWORD.findall(code)):
        out["distance"] = "G90"         # program end (M2/M30) goes back to G90 G1 G17 G94 G54 M5 M9; units stay
    return out


def uses_modal_units(line):
    """True if FluidNC reads the line's distances and feeds in its current units: G-code, and $J= jogs,
    without a G20 or G21 of their own. Other $ commands have none."""
    code = _code(line)
    if code.startswith("$J="):
        code = code[3:]
    elif code.startswith("$"):
        return False
    return not any(float(g) in (20, 21) for g in GWORD.findall(code))


def can_move(line):
    """True if FluidNC may move for this line (a Line): G-code (but Zero's G10 L20, Undo's G10 L2 and our own
    G20), $H and $J= jogs. $I, $G, the settings and the other $ commands never do."""
    if line.tag in ("zero", "undo") or line.auto == "inch":
        return False
    return line.jog or line.long or not _code(line.text).startswith("$")


def job_line_name(line):
    """'line 12' for a job's line (its line number in the file); FluidSender's own first line has none."""
    return f"line {line.no}" if line.no is not None else "FluidSender's first line"


def dwell_seconds(line):
    """The P seconds of a G4 dwell, else 0: FluidNC answers a G4 only after its dwell."""
    code = _code(line)
    if code.startswith("$") or not any(float(g) == 4 for g in GWORD.findall(code)):
        return 0.0
    m = PWORD.search(code)
    return float(m.group(1)) if m else 0.0


def patch_gc(gc, units, distance):
    """The $G text with the units/distance words replaced by the ones tracked since."""
    if not gc:
        return gc
    swap = {"G20": units, "G21": units, "G90": distance, "G91": distance}
    return " ".join(swap.get(w, w) for w in gc.split())


def check_line(line):
    """The line to send, stripped; MachineError if FluidNC must not get it."""
    if not isinstance(line, str):
        raise MachineError("No line given")
    text = line.strip()
    if not text:
        raise MachineError("Empty line")
    if len(text) > MAX_GCODE and (text[0] not in "$[" or text[:3].upper() == "$J="):
        raise MachineError(f"Line too long for FluidNC ({len(text)} characters; it refuses G-code lines over "
                           f"{MAX_GCODE}, spaces and comments included)")
    if len(text) > MAX_LINE:
        raise MachineError(f"Line too long ({len(text)} characters; FluidNC takes {MAX_LINE})")
    bad = next((c for c in text if not " " <= c <= "~"), None)
    if bad is not None:
        raise MachineError(f"Line not sent: it contains the control or non-ASCII character {ord(bad):#04x}, "
                           "which FluidNC could take as a realtime command")
    rt = next((c for c in text if c in "?!~"), None)
    if rt is not None:
        raise MachineError(f"Line not sent: FluidNC takes '{rt}' as a realtime command wherever it appears "
                           "(? status, ! feed hold, ~ resume). Use the buttons for those.")
    return text


# ------------------------------------------------------------------ jog and zero lines (no state)

def _number(v):
    """A JSON number as a finite float, else None (true/false, text, nan, 1e999...)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def _xyz(obj, what):
    """{'x': 0.1, 'z': -1} -> [0.1, 0.0, -1.0] (axes left out are 0); MachineError for anything else."""
    if not isinstance(obj, dict) or not obj or not set(obj) <= set(AXES):
        raise MachineError(f"{what}: give x, y and/or z")
    nums = [_number(obj.get(a, 0)) for a in AXES]
    if None in nums:
        raise MachineError(f"{what}: x, y and z must be numbers")
    return nums


def _jog_feed(v):
    feed = _number(v)
    if feed is None or not 0 < feed <= MAX_FEED:
        raise MachineError(f"Jog speed: a number above 0 and up to {MAX_FEED:g} in/min")
    return feed


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def fixed(v, places=4):
    """A number for a G-code line: fixed decimals (never 1e-05), trailing zeros cut, never '-0'.
    0.001 -> '0.001', 10 -> '10', -0.00001 -> '0'"""
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def feed_text(feed):
    """150.0 -> '150', 111.3 -> '111.3'"""
    return f"{feed:.1f}".removesuffix(".0")


def ov_after(value, seq, steps):
    """FluidNC's feed or spindle override after these override bytes: 100 for the reset byte, else each step
    added and clamped to 10..200 (Protocol.cpp protocol_do_feed_override...)."""
    step = {b: k for k, b in steps.items()}
    for b in seq:
        value = 100 if step[b] == "reset" else max(OV_MIN, min(OV_MAX, value + step[b]))
    return value


def ov_bytes(cur, target, steps):
    """The fewest override bytes that take a feed or spindle override from cur to target as FluidNC applies them:
    coarse (10) then fine (1) steps from cur, or the reset byte (100 %) first and then steps from 100. b"" when
    it is there already. (At most 11 bytes: 10 -> 200 is reset + 10 coarse.)"""
    best = None
    for start, prefix in ((cur, []), (100, [steps["reset"]])):
        diff = target - start
        for tens in {diff // 10, -(-diff // 10)}:
            ones = diff - 10 * tens
            seq = (prefix + [steps[10 if tens > 0 else -10]] * abs(tens)
                   + [steps[1 if ones > 0 else -1]] * abs(ones))
            if ov_after(cur, seq, steps) == target and (best is None or len(seq) < len(best)):
                best = seq
    return bytes(best or [])


def cap_feed(feed, unit, axes):
    """The jog feed (in/min) to 1 decimal, capped so that no moving axis goes over its max rate.
    unit: the direction (length 1); axes: max rates in mm/min, None if not read (then not capped
    here: FluidNC caps each axis itself, silently)."""
    caps = [axes[a]["maxRate"] / MM / abs(u) for a, u in zip(AXES, unit) if u and axes[a]["maxRate"]]
    feed = round(feed, 1)
    if caps and feed > min(caps):
        feed = math.floor(min(caps) * 10 + 1e-6) / 10      # rounded down: never above a max rate
    return max(feed, 0.1)


def step_line(dist, feed):
    """'$J=G20G91X0.1Y-1F150': inches and relative (G20 G91 apply to this jog only), only the axes that move."""
    words = "".join(f"{a}{fixed(v)}" for a, v in zip("XYZ", dist) if fixed(v) != "0")
    return f"$J=G20G91{words}F{feed_text(feed)}"


def segment_line(unit, feed, axes, blocks):
    """One piece of a hold jog, and its length in inches. FluidNC answers a $J once it is in its planner, and
    the next piece goes only then, so about `blocks` pieces are planned ahead (never more than BLOCKS: see
    Link.hold_room): they add up to d = stopping distance + 0.25 s of travel (at most 1 s). Smooth at speed,
    and if FluidSender dies the machine runs out d at most."""
    v = feed * MM / 60                                          # mm/s
    acc = [axes[a]["accel"] for a, u in zip(AXES, unit) if u and axes[a]["accel"]]
    a = min(acc) if acc else ACCEL
    d = min(v * v / (2 * a) + v * 0.25, v * 1.0)
    n = min(blocks or BLOCKS, BLOCKS)
    piece = min(max(d / max(n - 2, 1), v * 0.02), d / 2)       # mm
    inches = [u * piece / MM for u in unit]
    words = [fixed(c) for c in inches]
    if all(w == "0" for w in words):                            # (only below about 0.3 in/min)
        i = max(range(3), key=lambda k: abs(inches[k]))
        words[i] = "0.0001" if inches[i] > 0 else "-0.0001"
    line = "$J=G20G91" + "".join(a + w for a, w in zip("XYZ", words) if w != "0") + "F" + feed_text(feed)
    return line, math.sqrt(sum(float(w) ** 2 for w in words))


def zero_line(axes, value, units="G20"):
    """'G10 L20 P0 G20 X0 Y0': the work position of these axes in the active coordinate system (P0) becomes
    value (inches). The units word in the line applies first, and stays in force after it (unlike a $J's),
    so it is the one FluidNC is in: G21 (the value in mm) if the user has chosen millimeters."""
    if units == "G21":
        return "G10 L20 P0 G21 " + " ".join(f"{a}{fixed(float(fixed(value)) * MM, 5)}" for a in axes)
    return "G10 L20 P0 G20 " + " ".join(f"{a}{fixed(value)}" for a in axes)


def undo_line(axes, wco, wcs=1, units="G20"):
    """Undo Zero: 'G10 L2 P1 G20 X-5.511811': the work offset of each axis in that coordinate system (P1 = G54
    ... P6 = G59, P7-P9 = G59.1-G59.3 in FluidNC 4.1.1) set back to the WCO from before the zero (machine
    coordinates; mm in, inches out, or mm with G21 as in zero_line). G10 L2 sets the offset itself, wherever the
    machine is and whichever system is active. FluidNC's WCO counts a G92 offset and a tool length offset in too,
    so this is the old offset exactly while neither is on (FluidSender sets neither; UGS zeroes with G10 L20 too)."""
    words = []
    for a in axes:
        v = wco["XYZ".index(a)]
        if units == "G21":
            words.append(a + fixed(v, 5))                   # (inch reports converted have 5 decimals in mm)
            continue
        s = f"{v / MM:.6f}"
        words.append(a + ("0.000000" if s == "-0.000000" else s))
    return f"G10 L2 P{wcs} {'G21' if units == 'G21' else 'G20'} " + " ".join(words)


def wcs_word(line):
    """The work coordinate system a G-code line selects: 1 (G54) to 6 (G59), 7 to 9 (G59.1 to G59.3: FluidNC
    4.1.1), or None. Program end (M2, M30) goes back to G54 (after the line's other words). $ commands select
    none."""
    if line.lstrip().startswith("$"):
        return None
    code = _code(line)
    if any(int(m) in (2, 30) for m in MWORD.findall(code)):
        return 1
    found = [WCS_CODES[v] for v in map(float, GWORD.findall(code)) if v in WCS_CODES]
    return found[-1] if found else None


def gc_wcs(gc):
    """The work coordinate system in $G's words ('G0 G54 G17 ...' -> 1, 'G59.1' -> 7), or None. (Its M2/M30 is the
    last program's end, not a change.)"""
    words = gc.split() if gc else []
    return next((WCS_CODES[float(w[1:])] for w in words if w[:1] == "G" and w[1:].replace(".", "", 1).isdigit()
                 and float(w[1:]) in WCS_CODES), None)


def wcs_name(wcs):
    """1 -> 'G54' ... 6 -> 'G59', 7 -> 'G59.1' ... 9 -> 'G59.3'."""
    return f"G{53 + wcs}" if wcs <= 6 else f"G59.{wcs - 6}"


def _no_axes():
    return {a: {"maxRate": None, "accel": None, "softLimits": None} for a in AXES}


def _per_inch(v):
    return None if v is None else round(v / MM, 3)


# ------------------------------------------------------------------ ports (only /dev, /sys and /proc are read)

def port_kind(path):
    name = os.path.basename(path)
    if name.startswith("ttyUSB"):
        return "usb-serial"
    if name.startswith("ttyACM"):
        return "usb-acm"
    if name.startswith("rfcomm"):
        return "bluetooth"
    if path.startswith("/dev/pts/"):
        return "pty"
    return "other"


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace").strip()
    except OSError:
        return ""


def usb_info(name, sys_tty=SYS_TTY):
    """(vid, pid, product) of the USB device behind /dev/<name>, from /sys; Nones if it isn't USB."""
    d = os.path.realpath(os.path.join(sys_tty, name, "device"))
    for _ in range(4):            # tty -> interface -> USB device
        if os.path.isfile(os.path.join(d, "idVendor")):
            return (_read(os.path.join(d, "idVendor")) or None, _read(os.path.join(d, "idProduct")) or None,
                    _read(os.path.join(d, "product")) or None)
        d = os.path.dirname(d)
    return None, None, None


def describe_port(path, sys_tty=SYS_TTY):
    real = os.path.realpath(path)
    kind = port_kind(real)
    vid = pid = product = None
    if kind in ("usb-serial", "usb-acm"):   # (an rfcomm port's /sys parent is the Bluetooth adapter: not useful)
        vid, pid, product = usb_info(os.path.basename(real), sys_tty)
    label = KNOWN_USB.get((vid, pid))
    if not label:
        label = {"bluetooth": "Bluetooth (pendant)", "pty": "Pseudo terminal (simulator?)"}.get(kind)
    if not label:
        base = product or {"usb-serial": "USB serial", "usb-acm": "USB serial (ACM)"}.get(kind, "Serial port")
        label = f"{base} ({vid}:{pid})" if vid else base
    return {"path": path, "label": label, "kind": kind, "vid": vid, "pid": pid, "product": product, "inUseBy": None}


def scan_holders(paths):
    """{device path: [pid, ...]} of the processes that have these devices open, from /proc/*/fd.
    Other users' processes can't be read; those are caught when the open fails (EBUSY)."""
    want = set(paths)
    found = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{d}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{d}/fd/{fd}")
            except OSError:
                continue
            if target in want and int(d) not in found.setdefault(target, []):
                found[target].append(int(d))
    return found


def process_name(pid):
    """'UGS (java, pid 4242)', 'python3 (pid 77)'..."""
    comm = _read(f"/proc/{pid}/comm") or "?"
    args = [a for a in _read(f"/proc/{pid}/cmdline").split("\0") if a]
    if re.search(r"ugsplatform|universal-g-code-sender", " ".join(args), re.I):
        return f"UGS ({comm}, pid {pid})"
    if len(comm) == 15:        # the kernel cuts names to 15 characters: look for the whole one
        comm = next((os.path.basename(a) for a in args[:2] if os.path.basename(a).startswith(comm)), comm)
    return f"{comm} (pid {pid})"


def holders_text(pids):
    names = [process_name(p) for p in pids[:3]]
    names.sort(key=lambda n: not n.startswith("UGS"))
    return ", ".join(names)


def other_holders(real):
    """'UGS (java, pid 4242)' if another process has the device open, else ''."""
    return holders_text([p for p in scan_holders({real}).get(real, []) if p != os.getpid()])


def open_port(path, speed):
    """Opens a serial port exclusively, raw 8N1 at the given termios speed. Returns the fd (non-blocking)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.EBUSY:
            raise MachineError(f"Port {path} is busy: another program has it open exclusively. "
                               "Disconnect it there first.", 409)
        if e.errno == errno.EACCES:
            raise MachineError(f"No permission to open {path}. Add yourself to the dialout group "
                               "(sudo usermod -aG dialout $USER) and log in again.")
        raise MachineError(f"Could not open {path}: {e.strerror}")
    try:
        fcntl.ioctl(fd, termios.TIOCEXCL)   # nobody else can open it now
        a = termios.tcgetattr(fd)
        a[0] = 0                                                   # iflag: no flow control or CR/LF changes
        a[1] = 0                                                   # oflag: send bytes as they are
        a[2] = (a[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
                | termios.CS8 | termios.CREAD | termios.CLOCAL)   # 8N1, ignore modem lines (HUPCL left as it was)
        a[3] = 0                                                   # lflag: raw, no echo
        a[4] = a[5] = speed
        a[6][termios.VMIN] = 1
        a[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)                      # drop anything sent before we opened it
    except (OSError, termios.error) as e:
        close_port(fd)
        raise MachineError(f"{path} is not a usable serial port ({e.args[-1] if e.args else e})")
    return fd


def close_port(fd):
    try:
        fcntl.ioctl(fd, termios.TIOCNXCL)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ------------------------------------------------------------------ the connection

class Line:
    """A line waiting to be sent, or sent and waiting for its ok/error."""
    __slots__ = ("text", "auto", "quiet", "tag", "data", "work", "jog", "long", "timeout", "stall",
                 "needs_inches", "since", "sent", "queued", "noted", "no", "size", "reply", "done_at", "cool", "mark")

    def __init__(self, text, auto=None, quiet=False, tag=None, data=None, work=None):
        self.text = text
        self.auto = auto                    # None: the user's; 'connect': $I/$G/$T; 'inch': our G20;
                                            # 'settings': a setting read (or $SS); 'jog': a hold-jog piece;
                                            # 'job': a line of a job; 'coolant': a $G for the vacuum/AUX state
        self.quiet = quiet                  # not in the console, nor its ok (an error is, with the line)
        self.tag = tag                      # the button that made it: 'step', 'zero', 'undo', 'home', 'unlock',
                                            # 'goto'; 'scan': a scan move
        self.no = None                      # a job line's line number in the file
        self.size = len(text) + 1           # bytes it takes in FluidNC's serial buffer
        self.reply = None                   # 'ok', 'error:N', or 'dropped' (by a reset); once answered ...
        self.done_at = None                 # ... and when
        self.data = data                    # a zero's undo record; a hold-jog piece's HoldJog
        self.work = work                    # zero and undo: {axis index: work position in mm} it sets
        self.jog = _code(text).startswith("$J=")
        self.long = bool(LONG_LINE.search(text))
        dwell = dwell_seconds(text)
        self.timeout = (LONG_TIMEOUT if self.long else LINE_TIMEOUT) + dwell
        self.stall = STALL + dwell
        self.needs_inches = auto is None and uses_modal_units(text)   # would run in mm before our G20
        self.since = 0.0                    # sent at; moved on while FluidNC is busy (see BUSY_STATES)
        self.sent = 0.0                     # sent at
        self.queued = None                  # first seen waiting by the reader thread (its clock)
        self.noted = False                  # the console was told why it waits
        self.cool = coolant_words(_code(text)) if "M" in text or "m" in text else ()   # its M7/M8/M9 words
        self.mark = None                    # the vacuum/AUX toggles written before it was sent (see Link.gc_reply)


class HoldJog:
    """A jog that runs while the page holds a jog button down: the same short $J piece, again and again."""
    __slots__ = ("id", "dir", "feed", "segment", "piece", "gate", "kept", "start", "sent", "answered", "fast")

    def __init__(self, jog_id, direction, feed, segment, now, gate=False):
        self.id = jog_id
        self.dir = direction                # [x, y, z], each -1, 0 or 1
        self.feed = feed                    # in/min, capped by the max rates
        self.segment, self.piece = segment  # the $J piece, and its length (in)
        self.gate = gate                    # FluidNC's planner takes more than BLOCKS pieces (see Link.hold_room)
        self.kept = now                     # when the page last said the button is still held
        self.start = None                   # machine position (in) where its first piece went, if known
        self.sent = 0                       # pieces sent
        self.answered = None                # when FluidNC answered its first piece
        self.fast = False                   # a report has shown it at its speed

    def info(self):
        return {"id": self.id, "dir": dict(zip(AXES, self.dir)), "feed": self.feed}


class Link:
    """One open serial port and its reader thread, which also polls, keeps the timers and sends
    the queued lines one at a time. All its state is guarded by the Machine's lock."""

    def __init__(self, machine, fd, path, baud):
        self.machine = machine
        self.lock = machine.lock
        self.fd = fd
        self.path = path
        self.baud = baud
        self.wlock = threading.Lock()       # guards writes and closing the fds
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.thread = threading.Thread(target=self.run, name=f"fluidnc {path}", daemon=True)
        self.first = threading.Event()      # set by the first status report (or by closing)
        self.connected = False              # True once FluidNC has answered '?'
        self.connected_at = 0.0
        self.stopping = None                # (message, console kind) once closing
        self.closed = False
        self.out = [LINE_EDIT_OFF]          # bytes to write when the lock is released, in the order decided (also
                                            # the Machine's realtime bytes: a jog cancel never overtakes a $J);
                                            # first ^L, before the first '?' (FluidNC's echo mode off)
        self.rx = b""
        self.next_poll = 0.0
        self.queue = deque()
        self.inflight = None
        self.owed = None                    # a line that timed out: FluidNC may still answer it
        self.last_done = 0.0
        self.reset_at = None                # our Ctrl-X went out then; its banner hasn't come yet
        self.busy_at = 0.0                  # when FluidNC last wasn't at rest (for the release)
        self.parser = StatusParser()
        self.parser.readback = {"flood": None, "mist": None}    # (until the pins are read: see got_coolant_setting)
        self.status = None                  # the latest report, as StatusParser made it
        self.status_at = 0.0
        self.inch_reports = False           # FluidNC reports inches (report_inches on)
        self.sent_key = None                # the last status event sent (without raw), and when
        self.sent_at = 0.0
        self.firmware = None
        self.modal = None                   # {'units', 'distance', 'gc'} once known
        self.inch = False                   # G20 still to be sent ...
        self.inch_at = 0.0                  # ... once a report newer than this says Idle
        self.inch_tries = 0
        self.axes = _no_axes()              # read at connect: max rates mm/min, accelerations mm/s^2, soft limits
                                            # on/off (None: unread)
        self.unstick_at = None              # our last jog cancel, until FluidNC answers a line (see unstick)
        self.planner_blocks = None
        self.settings_left = []             # the settings reads not answered yet
        self.alarm = None                   # {'code': N or None, 'critical': bool, 'text': line} while in alarm
        self.alarm_at = -1e9                # when the last ALARM:N line came
        self.hold = None                    # the hold jog running (HoldJog)
        self.cancel = None                  # stopping jogs: {'tries', 'sent', 'gave_up', 'hold', 'why'}
        self.jog_mark = None                # the last $J reply or 0x85; None once a report made JOG_SETTLE
                                            # after it says FluidNC isn't jogging
        self.steps_done = 0                 # step jogs answered since that report
        self.wcs = None                     # the work coordinate system (1 = G54 ... 6 = G59) chosen since connecting:
                                            # by a line, or G54 by a reset; None: as $G said (see active_wcs)
        self.job = None                     # the job streamed here (job.Job), or the last one
        self.halt = None                    # the safe stop under way: {'at', 'until', 'why', 'job'} (see safe_stop)
        self.held_at = -1e9                 # when we last wrote '!' ...
        self.resumed_at = -1e9              # ... and '~'
        self.resume_at = None               # Resume waits for Hold:0 until then (see resume) ...
        self.resume_asked = 0.0             # ... asked for then
        self.resets = 0                     # resets (ours, or FluidNC's banner): a scan move must not span one
        self.burst = 0                      # reports to ask for quickly after a vacuum/AUX toggle (burst_report) ...
        self.burst_until = 0.0              # ... until then at most
        self.err_line = None                # (job, line) of a job error just answered: its [MSG:ERR: ...] may follow
        self.moved_at = -1e9                # FluidNC's last ok to a line that can move it (see may_move)
        self.after_reset = None             # FluidNC's reset banner came then, and no line has gone since: a reply
                                            # now answers a line left from before the reset (see stray) ...
        self.reset_clean = False            # ... until a report a moment after the banner shows it ran none
        self.strays = 0                     # such lines FluidNC ran since its last clean reset (see contain)
        self.old_hold = None                # connected while FluidNC holds, with lines from before: why Resume
                                            # is refused until a reset (see went_live)
        self.coolant_pins = {"flood": None, "mist": None}   # $/coolant/flood_pin, mist_pin: "gpio.25", "I2SO.4",
                                            # "NO_PIN"... (None: not read): see got_setting
        self.coolant_delay = None           # $/coolant/delay_ms (FluidNC dwells that long switching one on)
        self.coolant_sure = False           # FluidNC's $G (or its reset) said the coolant modes, none changed since
        self.coolant_said = False           # "FluidNC's reports don't show the coolant outputs" logged (once)
        self.toggles = 0                    # vacuum/AUX toggles written (a $G sent before one doesn't say its outcome)
        self.toggled_at = -1e9              # when the reader thread wrote the last one (None: in self.out still): a
                                            # line sent less than SETTLE after it may run before it (toggle_settled)
        self.spindle_type = None            # "PWM", "OnOff"...: FluidNC's spindle, from $SS
        self.programmed = {"S": None, "F": None}    # the S (rpm) and F (in/min) in force; None: unknown
        self.ov_target = [None, None, None]     # [feed, rapid, spindle] % FluidSender brings FluidNC to (see override)
        self.ov_sent_at = [0.0, 0.0, 0.0]   # when each one's last bytes went
        self.ov_seen = [None, None, None]   # the values FluidNC last reported (trusted reports) ...
        self.ov_moved_at = [0.0, 0.0, 0.0]  # ... and when each last changed
        self.ov_late = [None, None, None]   # a target FluidSender stopped waiting for (see ov_report)

    # -------------------------------------------------- thread and port

    def start(self):
        self.thread.start()

    def run(self):
        try:
            while True:
                with self.lock:
                    now = time.monotonic()
                    if not self.stopping:
                        self.tick(now)
                    stopping = self.stopping
                    out, self.out = self.out, []
                    wait = max(0.0, min(self.next_poll, now + TICK) - now)
                for data in out:
                    self.write(data)
                if stopping:                # (closing: what was decided before still went, e.g. the safe
                    break                   # stop's Soft Reset just before Disconnect closes the port)
                ready = select.select([self.fd, self.wake_r], [], [], wait)[0]
                if self.wake_r in ready:
                    try:
                        os.read(self.wake_r, 512)
                    except BlockingIOError:
                        pass
                if self.fd in ready:
                    try:
                        data = os.read(self.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not data:
                        raise OSError(errno.EIO, "the port was closed")
                    with self.lock:
                        self.received(data, time.monotonic())
        except OSError as e:
            with self.lock:
                if not self.stopping:
                    self.stopping = (f"Lost the connection to {self.path} ({e.strerror or e}). "
                                     "Unplugged or switched off?", "error")
        except Exception as e:              # a bug: close the port and say so, rather than leave the pages frozen
            traceback.print_exc()
            with self.lock:
                if not self.stopping:
                    self.stopping = (f"Closed {self.path} after an internal error in FluidSender ({e!r}). "
                                     "Connect again.", "error")
        finally:
            self.close()

    def close(self):
        with self.wlock:
            fd, self.fd = self.fd, None
            wake, self.wake_r, self.wake_w = (self.wake_r, self.wake_w), None, None
        if fd is not None:
            close_port(fd)
        for w in wake:
            if w is not None:
                os.close(w)
        self.closed = True
        self.first.set()
        self.machine.link_closed(self)

    def stop(self, message, kind="info"):
        """Closes the port. Waits for it unless called from the reader thread itself."""
        with self.lock:
            if not self.stopping:
                self.stopping = (message, kind)
        self.wake()
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(WRITE_TIMEOUT + 2)

    def wake(self):
        with self.wlock:
            if self.wake_w is not None:
                try:
                    os.write(self.wake_w, b"x")
                except BlockingIOError:
                    pass

    def write_realtime(self, byte):
        """A realtime byte, at once (FluidNC acts on it wherever it is in the stream). Only for Disconnect,
        without the lock (the port may make it wait): everything else goes through self.out, so it is
        written after what was decided before it (a $J piece written after a jog cancel would start a jog)."""
        self.write(byte)

    def write(self, data):
        """Writes all of data. Realtime bytes from other threads may slip in between the pieces:
        FluidNC picks those out of the stream wherever they are."""
        end = time.monotonic() + WRITE_TIMEOUT
        while data:
            with self.wlock:
                if self.fd is None:
                    raise OSError(errno.EBADF, "not connected")
                try:
                    n = os.write(self.fd, data)
                except BlockingIOError:
                    n = 0
            data = data[n:]
            if data:
                if time.monotonic() > end:
                    raise OSError(errno.ETIMEDOUT, "the port takes no more data")
                time.sleep(0.01)

    # -------------------------------------------------- timers (reader thread, lock held)

    def tick(self, now):
        if self.toggled_at is None:
            self.toggled_at = now           # (the vacuum/AUX toggle waiting in self.out goes out after this pass)
        if self.burst and now >= self.burst_until:
            self.burst = 0
        if now >= self.next_poll:
            self.out.append(b"?")
            self.next_poll = now + (BURST_POLL if self.burst else
                                    FAST_POLL if self.hold or self.cancel or self.halt or self.resume_at else POLL)
        if self.hold and now - self.hold.kept > DEADMAN:
            self.out.append(self.stop_jog(now, "the page stopped answering"))
        elif self.hold and now - self.status_at > JOG_STALE:
            self.out.append(self.stop_jog(now, f"no status from FluidNC for {JOG_STALE:g} s"))
        busy = self.is_busy(now)
        head = self.job_head()
        for line in (self.inflight, self.owed, head):
            if line and busy:
                line.since = now            # FluidNC answers when it is done: the wait starts over
        line = self.inflight
        if line and now - line.since > line.timeout:
            self.timed_out(line, now)
        elif line and self.unstick_at is not None:
            self.unstick(line, now)
        if head and now - head.since > head.timeout:
            self.job_timed_out(head, now)
        if self.halt and now >= self.halt["until"]:
            self.finish_halt(now, stopped=False)
        if self.resume_at is not None and now >= self.resume_at:
            self.resume_at = None
            self.machine.log("error", f"Resume not sent: FluidNC was still slowing down (Hold:1) after "
                             f"{RESUME_WAIT:g} s, and it ignores Resume until it has stopped. Press Resume again.")
        if self.connected:
            self.start_next(now)
            self.note_waiting(now)
            self.emit_status(now)
            self.emit_job(now)
            self.check_release(now)

    def unstick(self, line, now):
        """FluidNC (3.9 and 4.1) can be left suspended by a jog cancel that it reads just as the jog ends by itself: it
        handles the cancel after the jog, in Idle, and then keeps reporting Idle but takes no line until a
        cycle start (Protocol.cpp: the motion cancel lands behind the cycle stop). So when a line has gone
        unanswered for a while since our last jog cancel and FluidNC says Idle, send '~' once: in Idle with
        nothing planned it only clears that. Not for lines that take long by design ($H, G38, a G4 dwell)."""
        st = self.status
        if (line.long or not st or st["state"] != "Idle" or st["sub"] or now - self.status_at > NO_RESPONSE
                or self.status_at <= line.since or now - line.since < UNSTICK_AFTER + dwell_seconds(line.text)):
            return
        self.unstick_at = None
        self.cycle_start(now)
        self.machine.log("info", f"FluidNC took no line for {UNSTICK_AFTER:g} s after a jog cancel although it "
                         "says Idle: sent cycle start (~) to wake it up")

    def is_busy(self, now):
        """True while a recent report says FluidNC is moving, homing or holding."""
        st = self.status
        return bool(st) and now - self.status_at <= NO_RESPONSE and st["state"] in BUSY_STATES

    def fresh_state(self, now):
        """The state in a report newer than the last reply and the inch arming (so FluidNC made it after
        everything it was sent), or None."""
        st = self.status
        if not st or st["sub"] or now - self.status_at > NO_RESPONSE:
            return None
        return st["state"] if self.status_at > max(self.inch_at, self.last_done) else None

    def in_alarm(self, now):
        """FluidNC is in an alarm: the latest report (if recent) says Alarm, or an ALARM:N (or "Reset to continue")
        came that no report has cleared yet (see on_report; FluidNC 3.9.4 still reports the old state for 0.5 s)."""
        st = self.status
        return self.alarm is not None or bool(st and now - self.status_at <= NO_RESPONSE and st["state"] == "Alarm")

    def homing(self, now):
        """The latest report (if recent) says FluidNC homes."""
        st = self.status
        return bool(st and now - self.status_at <= NO_RESPONSE and st["state"] == "Home")

    def may_move(self, now):
        """True if FluidNC may be moving or about to: the last report says it moves, holds, homes or has its door
        open; a line of ours is in its hands (a job's too); no report has come for NO_RESPONSE; or none since
        the last ok to a line that can move it, MOVE_SETTLE or more after that ok. (The ok comes as soon as the
        move is planned, and only a report made after it shows Run: right after a G0's ok the last report still
        says Idle.) Stop then does the safe stop: a '!' to a FluidNC that stands still only gives Hold:0, and
        the reset after it keeps the position."""
        st = self.status
        if st is None or now - self.status_at > NO_RESPONSE:
            return True
        return (st["state"] in MOVING or bool(self.inflight or self.owed or self.job_head())
                or self.status_at < self.moved_at + MOVE_SETTLE)

    def went_live(self, now):
        """FluidNC answered the first '?': tell the pages, then ask for the firmware and the modes."""
        self.connected = True
        self.connected_at = now
        m = self.machine
        msg = f"Connected to {self.path} at {self.baud} baud"
        m.log("info", msg)
        m.emit({"type": "conn", "connected": True, "port": self.path, "firmware": self.firmware, "message": msg})
        if self.modal:                      # (only if FluidNC reset while we were connecting)
            m.emit(dict(self.modal, type="modal"))
        if m.zeros:                         # (Undo Zero outlives a disconnect: the pages forgot it meanwhile)
            m.emit({"type": "zero", "zeroUndo": m.zero_info()})
        if self.status["state"] == "Alarm":
            m.log("info", "Machine is in ALARM: home ($HZ) or unlock ($X) from the console")
        if self.status["state"] in ("Hold", "Door"):
            # it may hold lines from before (a job's, say): $I and the rest would wait behind them and get their
            # replies, and Resume would run them. Nothing goes until a reset (the reads go once a report shows
            # it ran nothing from before: see reset_report) or until FluidNC is Idle again (see on_report)
            self.old_hold = OLD_HOLD
            m.log("error", OLD_HOLD)
        else:
            self.connect_reads()
        self.arm_inches(now)
        self.emit_status(now, force=True)
        self.start_next(now)

    def connect_reads(self):
        """$I, $G, $T in an alarm, and the settings (and $SS): what FluidSender asks once connected. FluidNC 4.1.1
        answers $I in a critical alarm too (3.9.4 refused it, error:8): its $T ("State 11 (Critical)") says it."""
        self.queue.extend([Line("$I", "connect"), Line("$G", "connect")])
        if self.status and self.status["state"] == "Alarm":
            self.queue.append(Line("$T", "connect"))
        self.settings_left = list(READS)
        self.queue.extend(Line(q, "settings", quiet=True) for q in READS)

    def start_next(self, now):
        """Sends the next waiting line, one at a time. Inch-first: G20 goes out once FluidNC is Idle, and
        until it has been answered, lines whose distances FluidNC would read in its current units
        (millimeters after a reset) wait behind it. In Alarm or Check mode they go as they are: FluidNC
        refuses G-code in Alarm (error:9) and only checks it in Check mode, so nothing moves.
        While a hold jog runs and nothing else waits, its next piece goes. Lines other than $J wait while
        FluidNC may still jog (it would answer error:9), and nothing goes while a jog is being stopped.
        A job sends its own lines (send_job); once it is over, nothing goes until FluidNC has answered all
        of them (their replies would be taken for the next line's). After a reset banner nothing goes until
        a report a moment later shows FluidNC ran no line left from before the reset (see stray), and
        nothing while it holds lines from before FluidSender connected (see went_live). A $G for the coolant
        and a line with M7/M8/M9 wait until a vacuum/AUX toggle has settled (see toggle_settled)."""
        if not self.connected:
            return
        job = self.job
        if job is not None and job.active:
            self.send_job(now)
            return
        if self.inflight or self.owed or (job is not None and job.inflight):
            return
        if self.reset_at is not None:       # our Ctrl-X: nothing goes before FluidNC has reset
            if now - self.reset_at < RESET_WAIT:
                return
            self.reset_at = None            # no banner seen: its units may have been reset all the same
            self.machine.log("info", "No reset banner from FluidNC; assuming it was reset")
            self.arm_inches(now)
        if (self.after_reset is not None and not self.reset_clean) or self.old_hold:
            return
        if self.cancel is not None:         # a $J now would be cancelled too, or start a new jog after it
            return
        head = self.queue[0] if self.queue else None
        if head is None and self.hold is not None and self.hold_room():
            head = Line(self.hold.segment, "jog", quiet=True, data=self.hold)
            if not self.hold.sent:
                self.hold.start = self.status["mpos"] if self.status else None
            self.hold.sent += 1
            self.queue.append(head)
        if self.inch and (head is None or head.needs_inches):
            if self.jogging():
                return                      # (G20 is G-code too)
            if self.inch_tries >= INCH_TRIES:   # gave up: never send such a line in millimeters
                if head is not None:
                    self.queue.popleft()
                    self.machine.log("error", f"Not sent: {head.text}  ({NO_INCHES})")
                    self.start_next(now)
                return
            state = self.fresh_state(now)
            if state == "Idle":
                self.inch = False
                self.inch_tries += 1
                self.machine.log("info", "*** Setting units to inches (G20)")
                head = Line("G20", "inch")
                self.queue.appendleft(head)
            elif head is None or state not in ("Alarm", "Check"):
                return
        if head is None:
            return
        if not head.jog and self.jogging():
            return                          # until a report shows the jog has stopped (see on_report)
        if (head.auto == "coolant" or head.cool) and not self.toggle_settled(now):
            return                          # (a $G or an M7/M8/M9 waits a moment after a toggle: see there)
        self.queue.popleft()
        head.since = head.sent = now
        head.mark = self.toggles
        self.inflight = head
        self.after_reset = None             # (from now on a reply is this line's)
        self.out.append(head.text.encode() + b"\n")
        if not head.quiet:
            self.machine.log("tx", head.text)

    def send_job(self, now):
        """Sends the job's next lines while they fit: the lines FluidNC has not answered stay within JOB_BUFFER
        bytes, each with its "\\n" (character counting), so its serial buffer never overflows. (A program's line
        has 127 characters at most: see job.MAX_JOB_LINE. A longer one would go alone.) Nothing goes while the
        job is paused or stopping, or before our reset; a line with M7/M8/M9 waits until a vacuum/AUX toggle
        has settled (see toggle_settled).
        The job's first line is FluidSender's G20 G90 (job.first, no file line number: see job.Job)."""
        job = self.job
        if job.state != "running" or self.reset_at is not None:
            return
        program, chunk = job.program, []
        while job.first is not None or job.next < job.total:
            no, text = (None, job.first) if job.first is not None else program[job.next]
            if job.inflight and job.bytes + len(text) + 1 > JOB_BUFFER:
                break
            line = Line(text, "job", quiet=True, tag="job")
            if line.cool and not self.toggle_settled(now):
                break                       # (an M7/M8/M9 waits a moment after a toggle: see toggle_settled)
            line.no = no
            line.since = line.sent = now
            job.inflight.append(line)
            job.bytes += line.size
            if no is None:
                job.first = None
            else:
                job.next += 1
                job.sent += 1
            job.dirty = True
            chunk.append(text.encode() + b"\n")
        if chunk:
            self.after_reset = None
            self.out.append(b"".join(chunk))

    def job_head(self):
        """The oldest job line FluidNC has not answered: the next reply is its."""
        job = self.job
        return job.inflight[0] if job is not None and job.inflight else None

    def job_active(self):
        return self.job is not None and self.job.active

    def note_waiting(self, now):
        """Shows the user's lines that have been waiting a while, and what for."""
        ahead = self.inflight
        for line in self.queue:
            if line.queued is None:
                line.queued = now
            if line.auto is None and not line.noted and now - line.queued >= WAIT_NOTE:
                line.noted = True
                self.machine.log("info", f"Waiting to send: {line.text}  ({self.wait_why(line, ahead)})")
            ahead = line

    def job_owed(self):
        """Why nothing else may go while FluidNC still has lines of a job that is over, or None."""
        job = self.job
        if job is None or job.active or not job.inflight:
            return None
        n = len(job.inflight)
        return (f"FluidNC has not answered {n} line{'s' * (n != 1)} of the last job yet; Soft Reset drops "
                + ("them" if n != 1 else "it"))

    def wait_why(self, line, ahead):
        if self.job_owed():
            return self.job_owed()
        if self.owed:
            return f"FluidNC has not answered '{self.owed.text}' yet; Soft Reset drops it"
        if self.reset_at is not None:
            return "until FluidNC has reset"
        if self.old_hold:
            return "until FluidNC is reset: it holds lines from before"
        if self.after_reset is not None and not self.reset_clean:
            return "until FluidNC reports Idle after its reset"
        if (ahead is None or ahead.jog) and (self.cancel or (not line.jog and (ahead or self.jogging()))):
            return "until the jog has stopped"
        if ahead is None:
            return "until FluidNC is Idle and set to inches with G20"
        return f"after '{ahead.text}'"

    def timed_out(self, line, now):
        """No answer in time while FluidNC wasn't busy. It may still answer, and that answer belongs to this
        line, not to the next one: so no more lines go out until it comes, or a reset or Disconnect."""
        self.inflight = None
        self.owed = line
        n = len(self.queue)
        self.queue.clear()
        msg = f"No reply to '{line.text}' within {line.timeout:g} s"
        if n:
            msg += f"; dropped the {n} line{'s' * (n != 1)} waiting after it"
        self.machine.log("error", msg + ". FluidNC may still run it. No more lines are sent until it answers "
                         "(Soft Reset gives up).")
        if line.auto == "jog" and line.data is self.hold:
            self.out.append(self.stop_jog(now, "FluidNC did not answer a jog piece"))

    def job_timed_out(self, line, now):
        """The oldest job line got no answer in time while FluidNC wasn't busy: a running job pauses, and
        nothing more is sent (never a line again, never a reset by itself). FluidNC may still answer: the
        replies still go to these lines, in order. Said once per line."""
        job = self.job
        if job.timed_out:
            return
        job.timed_out = True
        n = len(job.inflight)
        msg = (f"No reply to {job_line_name(line)} of the job ({line.text}) within {line.timeout:g} s: FluidNC "
               "stopped answering" + (f" ({n} lines not answered)" if n > 1 else ""))
        if job.state == "running":
            self.pause_job(now, NO_ANSWER_WHY,
                           f"{job_line_name(line)}: {line.text}: FluidNC stopped answering")
            msg += ". The job is paused: nothing more is sent. Stop ends it; Resume goes on if FluidNC answers again."
        elif not job.active:
            msg += ". No more lines are sent until it answers (Soft Reset drops them)."
        self.machine.log("error", msg)

    def arm_inches(self, now):
        self.inch = True
        self.inch_at = now
        self.inch_tries = 0

    def inch_failed(self, now, why):
        self.inch = True                    # try again at the next Idle; after the last try, see start_next
        self.inch_at = now
        if self.inch_tries >= INCH_TRIES:
            self.machine.log("error", f"Could not set inches: G20 failed {INCH_TRIES} times ({why}). Send G20 "
                             "yourself: until then, G-code without G20 or G21 in it is not sent.")

    def check_release(self, now):
        """Closes the port when no page has been open for a while, and only while FluidNC is at rest
        (Idle or Alarm, nothing waiting): it is never let go of while it moves, nor during a job or a scan
        (a job runs on when the page is closed; a page opened later shows it), nor in an ALARM that may have left
        the spindle or the vacuum/AUX on (the alarm's "outputs": an alarm in a job, say a hard limit on its last
        move, or with one of them on). Only a Soft Reset turns them off, and after a hard or soft limit the pendant's
        Stop is that reset: so the port, and with it the pendant, stays until a reset (or a report with Ov says they
        are off: alarm_outputs). A connected pendant doesn't keep it otherwise: closing FluidSender's window lets go
        of everything at rest (the user: "should it not disconnet when I close the app?"); Machine.release_extras
        then lets go of the pendant and the probe."""
        m = self.machine
        st = self.status
        if not (st and now - self.status_at <= NO_RESPONSE and st["state"] in RESTING
                and not (self.inflight or self.owed or self.queue or self.job_head() or self.job_active()
                         or self.halt or m.activity or (self.alarm or {}).get("outputs"))):
            self.busy_at = now              # the count starts once it is at rest
        elif not m.subs and now - max(m.alone_since, self.connected_at, self.busy_at) >= m.release_after:
            self.stopping = ("Released the port because no FluidSender window is open", "info")

    # -------------------------------------------------- status events

    def current_status(self, now):
        """The status event: the latest report as StatusParser made it, with what FluidSender knows besides
        (see status_extras)."""
        st = self.status
        if st is None:
            return None
        extras = self.status_extras()
        if now - self.status_at > NO_RESPONSE:
            return dict(st, state="No response", sub=None, stalled=False, **extras)
        line = self.inflight or self.owed or self.job_head()
        if line and now - line.since > line.stall and st["state"] not in BUSY_STATES:
            return dict(st, stalled=True, **extras)
        return dict(st, **extras)

    def status_extras(self):
        """ovTarget: [feed, rapid, spindle] % FluidSender still brings FluidNC to (None each when not), or None;
        programmedS / programmedF: the S (rpm) and F (in/min) in force, None if not known; spindleType: from $SS;
        coolantBusy / coolantMissing: why a vacuum/AUX click is refused now (see Link.coolant_busy and
        coolant_missing); coolantPins: the pins FluidNC said; coolantFollows: "modal" when a switch follows
        FluidNC's M7/M8/M9 state (its reports can't show that pin), "report" when its reports; coolantDelayMs:
        FluidNC's coolant delay_ms."""
        target = self.ov_target
        return {
            "ovTarget": list(target) if any(v is not None for v in target) else None,
            "programmedS": self.programmed["S"],
            "programmedF": self.programmed["F"],
            "spindleType": self.spindle_type,
            "coolantBusy": {"vacuum": self.coolant_busy("flood"), "aux": self.coolant_busy("mist")},
            "coolantMissing": {"vacuum": self.coolant_missing("flood"), "aux": self.coolant_missing("mist")},
            "coolantPins": {"vacuum": self.coolant_pins["flood"], "aux": self.coolant_pins["mist"]},
            "coolantFollows": {"vacuum": "modal" if self.modal_coolant("flood") else "report",
                               "aux": "modal" if self.modal_coolant("mist") else "report"},
            "coolantDelayMs": self.coolant_delay,
        }

    def emit_status(self, now, force=False):
        """Sends a status event when something changed (at most 5 a second, but the vacuum or AUX switching at
        once: see burst_report) or 2 s after the last one."""
        cur = self.current_status(now)
        if cur is None:
            return
        key = {k: v for k, v in cur.items() if k != "raw"}
        since = now - self.sent_at
        sent = self.sent_key or {}
        force = force or (key["flood"], key["mist"]) != (sent.get("flood"), sent.get("mist"))
        if force or since >= STATUS_EVERY or (key != self.sent_key and since >= STATUS_MIN):
            self.sent_key, self.sent_at = key, now
            self.machine.emit(dict(cur, type="status"))

    # -------------------------------------------------- what FluidNC says (lock held)

    def received(self, data, now):
        # (a 0x11 an earlier terminal sent puts FluidNC's console in XON/XOFF mode until it reboots: it then puts
        # 0x13/0x11 into what it prints, and "\x13ok" isn't "ok". They are never part of its text)
        data = data.translate(None, b"\x11\x13")
        self.rx += data
        *lines, self.rx = self.rx.split(b"\n")
        if len(self.rx) > 4096:            # no line end in sight (wrong baud rate?): show it anyway
            lines.append(self.rx)
            self.rx = b""
        for raw in lines:
            shown = raw.decode("utf-8", "replace").rstrip()    # (indents kept: $CD's YAML needs them)
            text = shown.strip()
            if not text:
                continue
            try:
                self.handle(text, now, shown)
            except Exception as e:          # one garbled line must not end the connection
                traceback.print_exc()
                self.machine.log("error", f"Could not handle this line from FluidNC: {text!r} ({e!r})")

    def handle(self, text, now, shown=None):
        try:
            self.handle_line(text, now, shown)
        finally:
            self.machine.notify()           # (a scan move waits for replies and reports)

    def handle_line(self, text, now, shown):
        m = self.machine
        if text[0] == "<":
            st = self.parser.feed(text, now)
            if st is not None:
                self.status, self.status_at = st, now
                self.first.set()
                if self.parser.inches != self.inch_reports:
                    self.inch_reports = self.parser.inches
                    m.log("info", "FluidNC reports positions in inches (report_inches is on); read as inches"
                          if self.inch_reports else "FluidNC reports positions in millimeters again")
                self.burst_report()
                self.coolant_report()
                if self.parser.ov_told:
                    self.ov_report(now)
                self.on_report(st, now)
                if self.connected:
                    self.emit_status(now)
                return
        err, self.err_line = self.err_line, None
        line = self.job_head() or self.inflight
        reply = text == "ok" or bool(ERROR.match(text))
        setting = SETTING.match(text) if line and line.auto == "settings" else None
        if setting and setting.group(1).lower() != line.text[2:].lower():
            setting = None                  # (not the one asked for)
        # FluidSender's own questions are quiet: their answers and oks are not shown (an error is, with its line):
        # the settings, $SS's startup log (but an alarm's message that comes meanwhile), the $G for the coolant
        quiet = line is not None and line.quiet
        startup = (quiet and line.text == SPINDLE_READ and text.startswith("[MSG:")
                   and not (msg_ends(text, "Reset to continue") or msg_ends(text, "Reboot FluidNC")))
        modes = quiet and line.auto == "coolant" and text.startswith("[GC:")
        if not (quiet and (setting or text == "ok" or startup or modes)):
            if quiet and reply:
                m.log("tx", line.text)      # but an error is, with the line it answers
            m.log("rx", shown or text)
        if is_banner(text):
            self.controller_reset(text, now)
        elif reply:
            self.line_done(text, now)
        elif setting:
            self.got_setting(setting.group(1), setting.group(2))
        elif startup:
            self.startup_line(text)
        elif text.startswith("ALARM:"):
            self.alarm_line(text, now)
        elif msg_ends(text, "Reset to continue") or msg_ends(text, "Reboot FluidNC"):
            # (FluidNC's critical alarms: only a reset gets out of them; an I/O expander's, ALARM:16, a reboot)
            self.set_alarm(dict(self.alarm or {"code": None, "text": text, "outputs": self.outputs_on()},
                                critical=True))
            if self.hold:
                self.out.append(self.stop_jog(now, "FluidNC needs a Soft Reset"))
        elif err is not None and text.startswith("[MSG:ERR:") and not text.startswith("[MSG:ERR: Bad GCode:"):
            self.job_error_text(err, text, now)     # verbose_errors: what that error means (4.1.1 prints the
                                                    # next refused line itself before its error:N: not a meaning)
        elif msg_ends(text, "Caution: Unlocked"):
            self.set_alarm(None)
        elif msg_ends(text, "to unlock") and self.alarm is None:    # "'$H'|'$X' to unlock" without an ALARM:N
            self.set_alarm({"code": None, "critical": False, "text": text, "outputs": self.outputs_on()})
        elif line is not None and line.text == "$T" and STATE_REPLY.match(text):
            self.state_reply(STATE_REPLY.match(text).group(1), text)
        elif text.startswith("[VER:"):
            self.firmware = parse_ver(text) or self.firmware
            if self.connected:
                m.emit({"type": "conn", "connected": True, "port": self.path, "firmware": self.firmware, "message": ""})
        elif text.startswith("[GC:"):
            gc = parse_gc(text)
            if gc:
                old = self.modal or {}
                self.set_modal(gc["units"] or old.get("units"), gc["distance"] or old.get("distance"), gc["gc"])
                self.gc_reply(gc["gc"], line, now)
        # ALARM:N, [MSG:...] and the rest are only shown; an ALARM doesn't answer the line in flight

    def gc_reply(self, gc, line, now):
        """$G's answer: FluidNC's modal state. Its coolant modes are what the vacuum and AUX switches show when that
        $G was the line in flight, sent SETTLE or more after the last toggle (and none since): FluidNC switches an
        M7/M8/M9 before it answers it, so its modes are its outputs then (and for an output on a plain GPIO pin the
        only word on it: see StatusParser); a $G sent with a toggle can show the modes from before it (see
        toggle_settled). Otherwise only until something says them. And the S and F in force (F: mm/min in $G, also
        with report_inches)."""
        asked = line is not None and line is self.inflight and line.text.upper() == "$G"
        sure = (asked and line.mark == self.toggles and self.toggled_at is not None
                and line.sent >= self.toggled_at + SETTLE)
        coolant = gc_coolant(gc)
        if coolant is not None and (sure or self.parser.flood is None):
            self.set_coolant(coolant, now)
            self.coolant_sure = sure
        if asked:
            for word in gc.split():
                try:
                    if word[0] == "S":
                        self.programmed["S"] = _num(float(word[1:]))
                    elif word[0] == "F":
                        self.programmed["F"] = round(float(word[1:]) / MM, 3)
                except ValueError:
                    pass

    def state_reply(self, name, text):
        """$T's answer, asked when connected in an alarm: "State 11 (Critical)" is a hard or soft limit (FluidNC
        reports it as Alarm), which only a Soft Reset ends. FluidNC 4.1.1 answers $I then too (3.9.4 refused it:
        see line_done). The name counts, not the number (4.1.1 put Held into the list)."""
        if name != "Critical":
            return
        if not (self.alarm and self.alarm["critical"]):
            self.machine.log("info", "FluidNC is in a critical alarm (hard or soft limit): only a Soft Reset gets out "
                             "of it")
        self.set_alarm(dict(self.alarm or {"code": None, "text": f"$T: {text}", "outputs": self.outputs_on()},
                            critical=True))

    def startup_line(self, text):
        """A line of $SS's answer, FluidNC's startup log: the spindle's names its type ("[MSG:INFO: PWM Spindle
        Ena:gpio.12 Out:gpio.13 ...]", "[MSG:INFO: Laser Ena:...]", "[MSG:INFO: Huanyang Spindle Tx:...]")."""
        found = SPINDLE_LINE.match(text)
        if found and self.spindle_type is None:
            self.spindle_type = found.group(1)

    def track_modal(self, line):
        """A line got its ok: the units and distance mode it set. Returns its modal words."""
        words = modal_words(line.text)
        self.wcs = wcs_word(line.text) or self.wcs  # (which offsets a zero changes: see active_wcs)
        if words and self.modal:
            units = words.get("units", self.modal["units"])
            distance = words.get("distance", self.modal["distance"])
            self.set_modal(units, distance, patch_gc(self.modal["gc"], units, distance))
        return words

    def line_done(self, reply, now):
        if self.after_reset is not None:    # nothing sent since FluidNC's reset: it answers a line from before
            self.stray(reply, now)
            return
        if self.job_head():                 # the job's lines are answered first, in order
            self.job_reply(reply, now)
            return
        line = self.inflight
        if line is None:                    # nothing in flight: the late reply to a line that timed out?
            line, self.owed = self.owed, None
            if line is None:
                return                      # no: only shown
            self.machine.log("info", f"That was the late reply to '{line.text}'")
        self.inflight = None
        line.reply, line.done_at = reply, now
        if self.unstick_at is not None and line.sent > self.unstick_at:
            self.unstick_at = None          # FluidNC took a line sent after our jog cancel: it isn't stuck
        if line.tag not in ("zero", "undo"):    # (a G10 L20 doesn't move: the last report still says the state)
            self.last_done = now
        if line.jog:
            self.jog_mark = now             # it may move (or start moving) until a later report says otherwise
            if line.tag == "step":
                self.steps_done += 1
        if reply == "ok":
            words = self.track_modal(line)
            self.ran(line, now)
            if "units" in words and line.auto is None:
                self.inch = False           # the user chose the units: don't override them
            if line.tag == "zero":
                self.zeroed(line.data)
            elif line.tag == "undo":
                self.undone(line.data, now)
            if line.work:
                self.set_work(line.work, now)
            if line.auto == "jog" and line.data is self.hold and self.hold.answered is None:
                self.hold.answered = now
        elif line.auto == "inch":
            self.inch_failed(now, reply)
        elif line.auto == "jog" and line.data is self.hold:
            self.out.append(self.stop_jog(now, f"FluidNC answered {reply}"))
        elif line.tag == "goto":
            self.goto_refused(line, reply)
        elif line.auto == "connect" and line.text == "$I" and self.status and self.status["state"] == "Alarm":
            # FluidNC refuses $I in a critical alarm only (which it reports as Alarm): connected after a hard
            # or soft limit, when its "Reset to continue" was printed before we were here
            self.machine.log("info", "FluidNC refused $I: it is in a critical alarm (hard or soft limit), "
                             "so only a Soft Reset gets out of it")
            self.set_alarm(dict(self.alarm or {"code": None, "text": f"$I: {reply}", "outputs": self.outputs_on()},
                                critical=True))
        if line.auto == "settings" and line.text in COOLANT_PIN_READS:
            key = COOLANT_PIN_READS[line.text]
            if self.coolant_pins[key] is None:  # (refused, or an ok with no value: the pin is unknown, so its
                self.parser.readback[key] = True    # reports count as before; see coolant_report)
        if line.auto == "settings" and line.text in self.settings_left:
            self.settings_left.remove(line.text)
            if not self.settings_left:
                self.settings_done()
        self.start_next(now)

    def ran(self, line, now):
        """FluidNC answered ok to this line (a job's too): it has run it. A move may start now (FluidNC starts
        it just after the ok: see may_move), and an M7/M8/M9 (or M2/M30) has switched the vacuum or AUX already:
        FluidNC switches before it answers (GCode.cpp: it waits for the moves before it, then sets the
        outputs), so the switches show it at once (see expect_coolant)."""
        if can_move(line):
            self.moved_at = now
        code = _code(line.text)
        coolant = coolant_after(code)
        if coolant is not None:
            self.expect_coolant(coolant, now)
        if ("S" in code or "F" in code) and not code.startswith("$"):
            self.track_sf(code)
        if "M" in code and not code.startswith("$") and any(int(m) in (2, 30) for m in MWORD.findall(code)):
            self.ov_program_end(now)

    def track_sf(self, code):
        """The S and F a line FluidNC ran (comments and spaces taken out, upper case) left in force: F in the units
        it ran in (tracked: see track_modal), shown in in/min. Unknown after #parameters or expressions."""
        if not WORDS.fullmatch(code):
            for letter in "SF":
                if letter in code:
                    self.programmed[letter] = None
            return
        for letter, value in WORD.findall(code):
            if letter == "S":
                self.programmed["S"] = _num(float(value))
            elif letter == "F":
                self.programmed["F"] = round(float(value) / (MM if self.units() == "G21" else 1.0), 3)

    def goto_refused(self, line, reply):
        """A Go to line got an error: the rest of that Go to is dropped (an XY rapid after a Z lift FluidNC
        refused would run at the low Z, and a Z0 after a refused XY at the wrong place)."""
        rest = [q for q in self.queue if q.tag == "goto" and q.data is line.data]
        if not rest or not isinstance(line.data, dict):
            return
        self.queue = deque(q for q in self.queue if not any(q is r for r in rest))
        for q in rest:
            q.reply = "dropped"
        self.machine.log("error", f"{line.data['name']} stopped: {line.text} was refused ({reply})")

    # -------------------------------------------------- jobs (lock held)

    def job_reply(self, reply, now):
        """The reply to the oldest job line. An error pauses a running job at once: '!' goes out before
        anything else, and Resume goes on with the next line (FluidNC has skipped the bad one). An error while
        the job is paused (an M0, a Feed hold with room left in FluidNC's planner, no answer in time) is the job's
        error just the same, and cancels a Resume waiting for Hold:0: the Resume pressed before it must not go past
        that line. A second error while paused by an error is told (and cancels a waiting Resume too); the first
        one stays the job's."""
        job = self.job
        line = job.inflight.popleft()
        job.bytes -= line.size
        line.reply, line.done_at = reply, now
        if job.inflight:
            job.inflight[0].since = now     # (FluidNC works on one line at a time: its wait starts now)
        job.timed_out = False
        job.last_reply = self.last_done = now
        if self.unstick_at is not None and line.sent > self.unstick_at:
            self.unstick_at = None          # (it takes lines: not stuck after a jog cancel)
        if line.jog:
            self.jog_mark = now
        if job.active and line.no is not None:      # (FluidSender's first line isn't one of the program's)
            job.acked += 1
            job.line = line.no
            job.dirty = True
        if reply == "ok":
            self.track_modal(line)
            self.ran(line, now)
        elif job.active:
            name = job_line_name(line)
            text = f"{name}: {line.text}: {explain(reply)}"
            waiting = self.resume_at is not None
            again = " The Resume waiting for FluidNC to stop is cancelled: press Resume again to go on." * waiting
            if job.state == "running" or (job.state == "paused" and (job.error is None or job.why == NO_ANSWER_WHY)):
                self.feed_hold(now)         # (paused: FluidNC holds already, unless it didn't answer in time)
                self.pause_job(now, f"error on {name}", text)
                self.err_line = (job, line)
                said = reply_meaning(reply)
                self.machine.log("error", f"{name[0].upper()}{name[1:]} was refused by FluidNC ({reply}"
                                 f"{' ' + said if said else ''}) and won't run. Resume goes on with the next lines; "
                                 "Stop ends the job." + again)
            else:
                if job.state == "paused" and waiting:
                    self.resume_at = None   # (pressed for the first error: this one isn't known to the user yet)
                self.machine.log("error", f"Job: {text}" + (again if job.state == "paused" else ""))
        self.start_next(now)

    def job_error_text(self, err, text, now):
        """FluidNC's verbose_errors (on by default) follows each error:N with what it means, e.g. "[MSG:ERR: Bad
        GCode number format]" (not a reply: it answers no line). The job's error then says it in its words."""
        job, line = err
        meaning = text[len("[MSG:ERR:"):].removesuffix("]").strip()
        name = job_line_name(line)
        if meaning and job.error == f"{name}: {line.text}: {explain(line.reply)}":
            job.error = f"{name}: {line.text}: {line.reply} ({meaning})"
            self.emit_job(now, force=True)

    def pause_job(self, now, why, error=None):
        job = self.job
        job.state, job.why = "paused", why
        if error:
            job.error = error
        self.emit_job(now, force=True)

    def end_job(self, now, state, why, error=None):
        """The job is over: done, stopped or failed. Nothing more of it is sent."""
        job = self.job
        job.state, job.why, job.end = state, why, now
        if error:
            job.error = error
        if self.halt and self.halt["job"] is job:
            self.halt = None
        self.resume_at = None
        if (self.modal_coolant("flood") or self.modal_coolant("mist")) and self.reset_at is None \
                and self.after_reset is None:
            self.ask_coolant(behind=True)   # (the program's M7/M8/M9 were followed: $G says them for sure)
        took = clock_text(job.end - job.start)
        if state == "done":
            self.machine.log("info", f"Job done: {job.name}, {job.acked} lines in {took}")
        else:
            where = f" (up to line {job.line})" if job.line is not None else ""
            because = why[0].upper() + why[1:] + ("" if why.endswith(".") else ".")
            self.machine.log("info" if state == "stopped" else "error",
                             f"Job {state}: {job.name}, {job.acked} of {job.total} lines done in {took}{where}. "
                             f"{because}")
        self.emit_job(now, force=True)

    def emit_job(self, now, force=False):
        """A job event on every change of state; while it runs, at most every JOB_EVERY (and every second, for
        the time shown)."""
        job = self.job
        if job is None or not (force or job.active):
            return
        since = now - job.emitted
        if force or (job.dirty and since >= JOB_EVERY) or since >= 1.0:
            job.dirty = False
            job.emitted = now
            self.machine.emit({"type": "job", "job": job.info(now)})

    def job_report(self, st, now):
        """A new report, for the safe stop, a Resume waiting for Hold:0, and the job: FluidNC held it (its
        hold button, M0, the door), or it is done (every line answered, and Idle in a report made after the
        last reply: the moves after the last ok were still running until then)."""
        state, sub = st["state"], st["sub"]
        h = self.halt
        if h and now >= h["at"] + SETTLE and (state == "Idle" or (state == "Hold" and sub == "0")
                                              or (state == "Door" and sub in ("0", "1"))):
            self.finish_halt(now, stopped=True)     # stopped: a reset now keeps the position
            return
        if self.resume_at is not None and now >= max(self.resume_asked, self.held_at) + SETTLE:
            if not (state == "Hold" and sub == "1"):
                self.resume_at = None
                self.cycle_start(now)
                self.resumed(now)
        job = self.job
        if (job is not None and job.state == "paused" and state == "Run" and self.resume_at is None
                and now >= max(self.held_at, job.held_after) + SETTLE):
            self.machine.log("info", "FluidNC is running again (its cycle start button?): the job goes on")
            self.resumed(now)
            return
        if job is None or job.state != "running":
            return
        if state in ("Hold", "Door") and now >= job.held_after + SETTLE:
            head = self.job_head()
            if state == "Door":
                why = "the safety door is open"
            elif head and any(int(m) == 0 for m in MWORD.findall(_code(head.text))):   # (M1 does nothing in FluidNC)
                why = f"M0 in the program (line {head.no}): press Resume to go on"
            else:
                why = "Feed hold"
            self.pause_job(now, why)
        elif state == "Alarm":
            self.end_job(now, "failed", "FluidNC is in ALARM", "FluidNC is in ALARM")
        elif (state == "Idle" and job.next >= job.total and not job.inflight and job.last_reply is not None
              and now >= job.last_reply + SETTLE):
            self.end_job(now, "done", None)

    def burst_report(self):
        """A report after a vacuum/AUX toggle or override bytes. FluidNC 3.9.4 says the coolant and the spindle
        override only in every 10th report when polled with '?' (20th while busy), so after one the reports are asked
        for every BURST_POLL until one made after it does (at most BURST_REPORTS, or BURST_TIME), and the switch
        shows what FluidNC did within about 0.2 s, not 2 s. (FluidNC 4.1.1 puts Ov in the next report after any
        change: the burst ends after one.)"""
        p = self.parser
        if self.burst and p.trusted and (p.ov_trust_after is None or self.status_at >= p.ov_trust_after):
            self.burst = 0 if p.told else self.burst - 1    # (made after both: with Ov it said them)

    def feed_hold(self, now):
        """'!' at once (behind what was decided before it)."""
        self.held_at = now
        self.resume_at = None               # (a Resume still waiting would undo it)
        self.out.append(REALTIME["hold"])

    def hold_job(self, now):
        """Feed Hold pressed: a running job pauses (FluidNC holds it)."""
        if self.job_active() and self.job.state == "running":
            self.pause_job(now, "Feed hold")

    def resume(self, now):
        """Resume: '~', but only once FluidNC has stopped (Hold:0): it drops a '~' while still slowing down
        (Hold:1). Then the '~' goes with the first report that no longer says Hold:1 (job_report), for up to
        RESUME_WAIT. Returns True if '~' went now. Refused during the safe stop (a '~' would let the move run on,
        and the reset then hit it moving), during our reset and after FluidNC's banner until a report shows it
        ran nothing from before it (a '~' would run a line left from before the reset: see stray; the page's
        Resume, a typed '~' and the pendant all come here), and while FluidNC holds lines from before connecting
        (see went_live)."""
        job = self.job
        if job is not None and job.state == "stopping":
            raise MachineError("The job is being stopped", 409)
        if self.halt is not None:
            raise MachineError(NO_RESUME, 409)
        if self.reset_at is not None:
            raise MachineError(RESETTING, 409)
        if self.after_reset is not None and not self.reset_clean:
            raise MachineError(JUST_RESET, 409)
        if self.old_hold:
            raise MachineError(self.old_hold, 409)
        st = self.status
        slowing = st is not None and st["state"] == "Hold" and st["sub"] == "1"
        if slowing or (st is not None and self.status_at < self.held_at + SETTLE):
            self.resume_at = now + RESUME_WAIT
            self.resume_asked = now
            self.next_poll = min(self.next_poll, now + FAST_POLL)
            self.machine.log("info", "Resume: FluidNC is still slowing down; Resume goes once it has stopped")
            return False
        self.resume_at = None
        self.cycle_start(now)
        self.resumed(now)
        return True

    def cycle_start(self, now):
        """'~' at once (behind what was decided before it)."""
        self.resumed_at = now
        self.out.append(REALTIME["resume"])

    def resumed(self, now):
        job = self.job
        if job is not None and job.state == "paused":
            job.state, job.why, job.error, job.timed_out = "running", None, None, False
            job.held_after = now            # (reports from before the '~' still say Hold)
            if job.inflight:
                job.inflight[0].since = now
            self.emit_job(now, force=True)
            self.start_next(now)

    def safe_stop(self, now, why):
        """The safe stop: '!' (unless FluidNC already holds), then Ctrl-X once a report made after it shows
        FluidNC has stopped (Hold:0, or Idle), or after STOP_WAIT anyway (finish_halt). A reset while moving
        loses the position (ALARM:3); once stopped it keeps it, and it drops the lines FluidNC still has and
        turns the spindle and coolant off. A job waits in 'stopping' meanwhile. "Already holds" only from a
        report made after our last '~': right after a Resume the last report still says Hold, but FluidNC
        runs again (a '!' while it holds changes nothing).
        In an alarm, and while homing, it is the Soft Reset at once, with no '!': nothing moves in an alarm, and a
        '!' in a critical one (a hard or soft limit, reported as Alarm) would put FluidNC in a Hold that the reset
        then leaves as Idle, unhomed and with no alarm (both versions; from the alarm the reset gives ALARM:14 when
        an axis is unhomed, which is right). Homing ignores '!' ("use Reset instead"): the reset stops it (ALARM:6),
        and it must be homed again anyway."""
        if self.in_alarm(now) or self.homing(now):
            what = "in ALARM" if self.in_alarm(now) else "homing (it ignores a feed hold then)"
            self.machine.log("info", f"Stop: FluidNC is {what}: Soft Reset at once, no Feed Hold")
            self.soft_reset(now, why)
            return
        st = self.status
        if not (st and st["state"] == "Hold" and self.status_at >= self.resumed_at + SETTLE):
            self.feed_hold(now)
        self.resume_at = None
        job = self.job if self.job_active() else None
        if self.halt is None:
            self.halt = {"at": now, "until": now + STOP_WAIT, "why": why, "job": job}
            self.machine.log("info", "Stop: Feed Hold, then Soft Reset once FluidNC has stopped")
        self.next_poll = min(self.next_poll, now + FAST_POLL)
        if job is not None and job.state != "stopping":
            job.state, job.why = "stopping", why
            self.emit_job(now, force=True)

    def finish_halt(self, now, stopped):
        h, self.halt = self.halt, None
        why = h["why"]
        if not stopped:
            self.machine.log("error", f"FluidNC did not stop within {STOP_WAIT:g} s of the feed hold: sent Soft "
                             "Reset anyway. The position may be lost: home before the next job.")
            why += f" (FluidNC did not stop within {STOP_WAIT:g} s: reset anyway, the position may be lost)"
        elif h["job"] is None:
            self.machine.log("info", "Stopped: FluidNC held, then Soft Reset (the position is kept)")
        self.soft_reset(now, why)

    def soft_reset(self, now, why):
        """Ctrl-X: FluidNC stops at once and restarts (in G21 again). Drops the lines waiting here (but the axis
        setting reads, which any state answers) and forgets the job lines FluidNC had (a reset empties its
        buffer: they are never answered); ends every jog and the job. Nothing goes until its banner."""
        keep = deque(line for line in self.queue if line.auto == "settings")
        dropped = [line for line in self.queue if line.auto != "settings"]
        if dropped:
            self.queue = keep
            n = len(dropped)
            self.machine.log("info", f"Soft reset: dropped {n} waiting line{'s' * (n != 1)}")
            for line in dropped:
                line.reply = "dropped"
        self.reset_at = now
        self.halt = None
        self.resume_at = None
        self.resets += 1
        self.end_jogs("Soft Reset")         # (a reset stops every move: no jog cancel needed)
        job = self.job
        if job is not None:
            job.inflight.clear()
            job.bytes = 0
            if job.active:
                self.end_job(now, "stopped", why)
        self.out.append(REALTIME["reset"])

    def set_coolant(self, coolant, now):
        """Flood and mist as known now ($G, a reset, our toggle): the status shows them at once."""
        self.parser.flood, self.parser.mist = coolant
        if self.status is not None:
            self.status = dict(self.status, flood=coolant[0], mist=coolant[1])
            if self.connected:
                self.emit_status(now, force=True)

    def expect_coolant(self, coolant, now):
        """The vacuum (flood) and AUX (mist) changed now, as FluidSender knows: its own toggle written, or the ok
        of a line that switches them (flood, mist: True, False, or None for unchanged). The switches show it at
        once, and the known state is the newer of that and a report with Ov made after it: reports from before
        don't count any more, and they are asked for quickly until one with Ov says what FluidNC did (see
        burst_report). If that one disagrees, it wins. A switch to the state known sends nothing, so a double
        click can't toggle it back. (FluidNC has no output on a NO_PIN pin: M8/M7 change nothing there.)"""
        p = self.parser
        p.trust_after = now + SETTLE
        flood, mist = (None if self.no_pin(key) else on for key, on in zip(("flood", "mist"), coolant))
        self.set_coolant((p.flood if flood is None else flood, p.mist if mist is None else mist), now)
        self.coolant_sure = False           # (until a $G says it: see gc_reply)
        self.next_poll = now
        self.burst, self.burst_until = BURST_REPORTS, now + BURST_TIME

    # -------------------------------------------------- overrides (lock held)

    def set_ov(self, ov, now):
        """The overrides as known now (expected after our bytes, or after a program's end): shown at once."""
        self.parser.ov = list(ov)
        if self.status is not None:
            self.status = dict(self.status, ov=list(ov))
            if self.connected:
                self.emit_status(now, force=True)

    def ov_sent(self, data, now):
        """Override bytes written now: reports made before FluidNC took them don't count, and the next one is asked
        for once it has (SETTLE): FluidNC 4.1.1 puts Ov in the first report after a change, which then counts (an
        earlier one would use it up), so a spindle's next steps go about every 50 ms, also during a job, where Ov
        comes only in every 20th report otherwise. Then quickly until one says them (3.9.4: see burst_report)."""
        self.out.append(data)
        self.parser.ov_trust_after = now + SETTLE
        self.next_poll = now + SETTLE
        self.burst, self.burst_until = BURST_REPORTS, now + BURST_TIME

    def set_override(self, i, target, now):
        """Brings override i (0 feed, 1 rapid, 2 spindle) to target %. Returns the bytes written now."""
        self.ov_target[i] = target
        self.ov_late[i] = None
        n = self.ov_send(i, now)
        if not n and self.parser.ov and self.parser.ov[i] == target:
            self.ov_target[i] = None            # (there already: nothing to do)
        return n

    def ov_send(self, i, now):
        """The bytes from the known value to the target: all of them for feed (FluidNC re-plans every block at once)
        and rapid (one absolute byte), the first SPINDLE_STEPS for the spindle (see ov_report). The known value
        is the one they give. Returns how many went."""
        p = self.parser
        target = self.ov_target[i]
        cur = p.ov[i] if p.ov else None
        if target is None:
            return 0
        if i == 1:
            data = bytes([RAPID_BYTES[target]]) if cur != target else b""
        elif cur is None:
            return 0
        else:
            data = ov_bytes(cur, target, OV_STEPS[OV_NAMES[i]])[:SPINDLE_STEPS if i == 2 else None]
        if not data:
            return 0
        self.ov_sent(data, now)
        self.ov_sent_at[i] = now
        if p.ov:
            ov = list(p.ov)
            ov[i] = target if i == 1 else ov_after(cur, data, OV_STEPS[OV_NAMES[i]])
            self.set_ov(ov, now)
        return len(data)

    def ov_reset(self, now):
        """Feed, rapid and spindle back to 100 %: three absolute bytes (known at once, even before FluidNC said
        them)."""
        self.ov_target = [100, 100, 100]
        self.ov_sent_at = [now] * 3
        self.ov_late = [None, None, None]
        self.ov_sent(OV_RESET, now)
        self.set_ov([100, 100, 100], now)
        return len(OV_RESET)

    def ov_report(self, now):
        """A report with Ov made after our override bytes (StatusParser.ov_told): what FluidNC has. A target it
        shows is reached (the console says it, once). When it shows what the last bytes gave (the known value),
        the spindle's next steps go, from there. Otherwise FluidNC hasn't applied them all yet: it takes override
        bytes as events and applies them once its protocol task is free, which a VFD spindle getting to a new
        speed (seconds: at every spindle step while it stands, and at the program's M3/S) or the alarm pause holds
        up, and it drops one only when its 50-deep event queue is full. So nothing goes again: steps, which are
        relative, would be applied on top of the ones still waiting (an overshoot), and even absolute bytes would
        only fill that queue while FluidNC is busy, which a Feed Hold or a Soft Reset (events in the same queue)
        then may not get into. The known value stays the expected one. Once FluidNC's value hasn't moved for
        OV_GIVE_UP since the last bytes, FluidSender stops waiting and says so, and its reports count; if it gets
        there later, the console says that too."""
        p = self.parser
        done, keep = [], list(p.ov)
        for i, target in enumerate(self.ov_target):
            have = p.ov[i]
            if have != self.ov_seen[i]:
                self.ov_seen[i], self.ov_moved_at[i] = have, now
            if self.ov_late[i] is not None and have == self.ov_late[i]:
                self.ov_late[i] = None
                self.machine.log("info", f"{OV_TITLES[i]} override {have}% (FluidNC took it late)")
            if target is None:
                continue
            if have == target:
                self.ov_target[i] = None
                done.append((i, have))
                continue
            expected = p.ov_before[i] if p.ov_before else None
            if have == expected:                # (the last bytes took: the spindle's next steps)
                self.ov_send(i, now)
                keep[i] = p.ov[i]
                continue
            # (FluidNC may not have applied them all yet: still expected; a rapid asked before FluidNC said its
            # overrides had no expected value: its target is)
            keep[i] = target if expected is None else expected
            if now - max(self.ov_sent_at[i], self.ov_moved_at[i]) >= OV_GIVE_UP:
                self.ov_target[i] = None
                self.ov_late[i] = target
                keep[i] = have
                self.machine.log("error", f"FluidNC has not taken the {OV_NAMES[i]} override: it still says {have}%, "
                                 f"{target}% was asked (is it busy? A VFD spindle getting to speed holds it up)")
        if keep != p.ov and None not in keep:
            self.set_ov(keep, now)
        if len(done) == 1:
            self.machine.log("info", f"{OV_TITLES[done[0][0]]} override {done[0][1]}%")
        elif done:
            self.machine.log("info", "Overrides: " + ", ".join(f"{OV_NAMES[i]} {v}%" for i, v in done))

    def ov_program_end(self, now):
        """M2/M30 answered: FluidNC put the overrides back to 100 % (RESTORE_OVERRIDES_AFTER_PROGRAM_END) before it
        answered; nothing of ours is pending any more."""
        self.ov_target = [None, None, None]
        self.ov_late = [None, None, None]
        if self.parser.ov is not None and self.parser.ov != [100, 100, 100]:
            self.set_ov([100, 100, 100], now)
            self.parser.ov_trust_after = now + SETTLE

    def controller_reset(self, banner, now):
        """FluidNC restarted: it dropped what it was given, and it is back in G21 G90, M9, F0 S0, overrides at 100 %.
        (FluidNC, 3.9.4 and 4.1.1, may still run one line it had taken during a hold: see stray.)"""
        m = self.machine
        m.log("info", "*** Controller reset")
        n = sum(1 for line in (*self.queue, self.inflight) if line and not line.quiet)
        if n:
            m.log("info", f"Dropped {n} line{'s' * (n != 1)} FluidNC had not answered")
        for line in (*self.queue, self.inflight, self.owed):
            if line:
                line.reply = "dropped"
        self.queue.clear()
        self.inflight = self.owed = self.reset_at = None
        self.last_done = now
        self.parser.ov_trust_after = None   # (FluidNC's reset puts the overrides back to 100 %; the work offsets stay)
        self.ov_target = [None, None, None]
        self.ov_late = [None, None, None]
        self.set_ov([100, 100, 100], now)
        self.parser.spindle = "off"
        self.set_coolant((False, False), now)   # (a reset turns the spindle and the coolant off)
        self.coolant_sure = True
        self.programmed = {"S": 0, "F": 0.0}    # (gc_init: F0 S0)
        self.halt = self.resume_at = None
        self.after_reset, self.reset_clean = now, False     # (until a line goes, a reply isn't ours: see stray)
        self.resets += 1
        job = self.job
        if job is not None:
            job.inflight.clear()            # (FluidNC dropped them: never answered)
            job.bytes = 0
            if job.state == "stopping":     # (not our reset: that ended it already)
                self.end_job(now, "stopped", f"{job.why}; FluidNC was reset")
            elif job.active:
                self.end_job(now, "failed", "FluidNC was reset", "FluidNC was reset (its reset button, or it "
                             "restarted)")
        self.end_jogs("FluidNC was reset")  # (a reset stops every move)
        if self.alarm:                      # a reset ends a critical alarm; the ALARM it printed before this
            self.set_alarm(dict(self.alarm, critical=False, outputs=False))     # banner stays until a report says
                                                                                # otherwise (and all is off now)
        self.wcs = 1                        # (back in G54. FluidNC keeps the work offsets, so Undo Zero stays)
        self.queue.extend(Line(q, "settings", quiet=True) for q in self.settings_left)   # (they stay the same)
        if not self.firmware:               # ($I got no answer: the banner names it too)
            self.firmware = banner
            if self.connected:
                m.emit({"type": "conn", "connected": True, "port": self.path, "firmware": banner, "message": ""})
        self.set_modal("G21", "G90", None)
        self.arm_inches(now)

    def stray(self, reply, now):
        """A reply after FluidNC's reset banner, before FluidSender has sent a line since: it can't be for a line
        of ours, so it answers one left from before the reset. FluidNC's poller hands the next line to the main
        loop whenever that is free, also during a hold (Hold:0, where the main loop waits): 3.9.4 through its
        activeLine, 4.1.1 through its one-slot cmd_queue (Protocol.cpp poll_once, cmd_queue_defer). The reset
        empties the input but not that line (protocol_do_soft_restart: nothing drains cmd_queue): the main loop
        runs it after the banner, in the state after the reset (G21, G0...). (The macros a reset runs answer
        nothing.) An error means FluidNC refused it, so nothing ran; an ok that it ran: see contain."""
        if reply != "ok":
            self.machine.log("info", f"FluidNC refused a line left from before the reset ({explain(reply)}): "
                             "nothing ran")
            return
        self.contain(now)

    def contain(self, now):
        """FluidNC runs a line left from before its reset (see stray), or moves or turns the spindle although
        FluidSender has sent nothing since: hold it at once ('!'), and reset again once it has stopped (the safe
        stop). That reset is clean (nothing is left to run), and it turns off an M3 or M8 that line ran. Only
        once a stop: if FluidNC does it again after that reset, it is held, and the user is told."""
        if self.halt is not None:
            return                          # (being held, then reset, already)
        self.feed_hold(now)
        self.strays += 1
        if self.strays == 1:
            self.machine.log("error", LEFTOVER)
            self.halt = {"at": now, "until": now + STOP_WAIT, "why": "a line left from before the reset ran",
                         "job": None}
            self.next_poll = min(self.next_poll, now + FAST_POLL)
        elif self.strays == 2:
            self.machine.log("error", "FluidNC ran a line left from before the reset again: held it. Press Soft "
                             "Reset when ready (FluidSender doesn't reset it again by itself)")

    def reset_report(self, st, now):
        """A report while nothing has gone since FluidNC's reset banner (see stray). It moves, or its spindle
        turns: a line from before the reset runs (see contain). It says Idle (or Alarm) with the spindle
        stopped, a moment after the banner, and no stop is under way: it ran none, and lines may go again
        (and the connect reads, if FluidSender connected while FluidNC held lines from before: see went_live).
        Not while our own reset is under way: its banner starts this over (a report before it is no answer)."""
        if self.after_reset is None or self.reset_clean or self.reset_at is not None:
            return
        if st["state"] in ("Run", "Jog") or (st["spindle"] or 0) > 0:
            self.contain(now)
        elif self.halt is None and now >= self.after_reset + SETTLE and st["state"] in ("Idle", "Alarm"):
            self.reset_clean = True
            self.strays = 0
            if self.old_hold:
                self.old_hold = None
                self.connect_reads()

    def set_modal(self, units, distance, gc):
        modal = {"units": units, "distance": distance, "gc": gc}
        if units and distance and modal != self.modal:
            self.modal = modal
            if self.connected:
                self.machine.emit(dict(modal, type="modal"))

    # -------------------------------------------------- jogs (lock held)

    def on_report(self, st, now):
        """Every new status report: ends a hold jog FluidNC has left or stopped, keeps cancelling a jog that
        still runs, and notes when jogging is over. Only reports made JOG_SETTLE after the last $J reply or
        0x85 count: FluidNC starts a jog just after its ok, so a report right after it may still say Idle.
        Also: after a reset, whether FluidNC runs a line from before it (reset_report); connected while it
        held lines from before, once it is Idle again (they ran: the connect reads go now)."""
        state = st["state"]
        if self.alarm and state != "Alarm" and now - self.alarm_at > ALARM_PAUSE:
            self.set_alarm(None)            # (not the old state FluidNC reports while it sleeps after an ALARM)
        elif self.alarm and self.parser.told:   # it says whether the spindle and coolant are still on
            self.alarm_outputs(self.outputs_on())
        self.reset_report(st, now)
        if self.old_hold and state == "Idle" and self.after_reset is None:
            self.old_hold = None
            self.connect_reads()
        self.job_report(st, now)
        why = self.hold and self.hold_why(st, now)
        if why:
            self.out.append(self.stop_jog(now, why))
        c = self.cancel
        prev = None
        if c:
            prev, c["feed"] = c["feed"], st["feed"]
        if self.jog_mark is None or now < self.jog_mark + JOG_SETTLE:
            return
        if state == "Jog":
            # Slowing down: it has taken the cancel. Another 0x85 then is no use, and one read just as the
            # slow-down ends can leave FluidNC stuck (it reports Idle but takes no more lines)
            slowing = st["feed"] is not None and (st["feed"] == 0 or (prev is not None and st["feed"] < prev))
            if c and not c["gave_up"] and not slowing and now - c["sent"] >= CANCEL_EVERY:
                if c["tries"] >= CANCEL_TRIES:      # (not '!': if the jog ended meanwhile, a '!' would leave
                    c["gave_up"] = True             # FluidNC in Hold, taking no lines until Resume)
                    self.machine.log("error", f"FluidNC still jogs after {CANCEL_TRIES} jog cancels. Press Feed "
                                     "Hold, or Soft Reset if it doesn't stop.")
                else:
                    c["tries"] += 1
                    self.out.append(JOG_CANCEL)
                    self.unstick_at = now
                c["sent"] = self.jog_mark = now
            return
        if state != "Alarm" and any(line and line.jog for line in (self.inflight, self.owed)):
            return                          # a $J not answered yet may still start a jog (never in Alarm)
        self.jog_mark = None
        self.steps_done = 0
        self.cancel = None
        if c and c["hold"]:
            self.log_jog_end(c["hold"], c["why"])

    def hold_why(self, st, now):
        """Why the hold jog must end, from a new report, or None. FluidNC can stop a jog by itself, and the next
        piece would start it again at once: a feed hold (a button on its feed_hold pin, Pn:H) cancels a jog
        without ever showing Hold, and at a soft limit every piece is cut to nothing and answered at once."""
        h, state, feed = self.hold, st["state"], st["feed"]
        if state not in ("Idle", "Jog"):
            return f"FluidNC went to {state}"
        if "H" in (st["pins"] or ""):
            return "FluidNC's feed hold input is on"
        if h.answered is None or now - h.answered < JOG_SETTLE:
            return None                     # (until its first piece runs, reports show what ran before it)
        if state == "Idle":
            return "FluidNC is not moving: at a soft limit?"
        if feed is not None and feed >= 0.9 * h.feed:
            h.fast = True
        elif h.fast and feed is not None and feed < 0.5 * h.feed:
            return "FluidNC slowed it down by itself: feed hold or soft limit?"
        return None

    def hold_room(self):
        """True if the next hold-jog piece may go. FluidNC answers a piece once its planner has room, so with
        up to BLOCKS planner blocks it keeps at most that many. A bigger planner would take far more, and if
        FluidSender died the machine would run them all out: then at most BLOCKS - 1 pieces not yet run are
        sent, counted from how far the machine has come since the first."""
        h = self.hold
        if not h.gate or not h.sent:
            return True
        pos = self.status["mpos"] if self.status else None
        done = 0.0
        if h.start and pos:
            done = sum((p - s) * u for p, s, u in zip(pos, h.start, _unit(h.dir))) / h.piece
        return h.sent - done < BLOCKS - 1

    def jogging(self):
        """FluidNC may still be jogging (or about to): lines other than $J must wait (they'd get error:9)."""
        return self.hold is not None or self.cancel is not None or self.jog_mark is not None

    def jog_active(self):
        """Like jogging(), but also with $J lines waiting or in FluidNC's hands, or a Jog report."""
        return (self.jogging() or (self.status is not None and self.status["state"] == "Jog")
                or any(line and line.jog for line in (self.owed, self.inflight, *self.queue)))

    def steps_pending(self):
        """Step jogs waiting, in FluidNC's hands, or answered but maybe still moving."""
        return self.steps_done + sum(1 for line in (self.owed, self.inflight, *self.queue)
                                     if line and line.tag == "step")

    def stop_jog(self, now, why=None):
        """Stops every jog at once: ends the hold jog, drops the $J lines not sent yet, and cancels (0x85) until
        a report says FluidNC isn't jogging any more (on_report). Returns the byte to write now: the reader
        thread (lock held) puts it in self.out, Machine calls write it once they let go of the lock."""
        hold = self.end_hold()
        steps = [line for line in self.queue if line.jog]
        if steps:
            self.queue = deque(line for line in self.queue if not line.jog)
            n = len(steps)
            self.machine.log("info", f"Dropped {n} jog line{'s' * (n != 1)} not sent yet")
        c = self.cancel
        if c is None:                       # (feed: the speed in the last report, to tell a slow-down by)
            c = self.cancel = {"tries": 0, "gave_up": False, "hold": None, "why": None,
                               "feed": self.status["feed"] if self.status else None}
        c["tries"] += 1
        c["sent"] = self.jog_mark = self.unstick_at = now
        if hold and hold.sent and c["hold"] is None:
            c["hold"], c["why"] = hold, why     # logged once it has stopped, with the distance
        elif hold:
            self.log_jog_end(hold, why)
        self.next_poll = min(self.next_poll, now + FAST_POLL)
        return JOG_CANCEL

    def end_hold(self):
        """The hold jog is over: no more pieces go. Returns it (None if none ran)."""
        hold, self.hold = self.hold, None
        if hold is not None and self.connected:
            self.machine.emit({"type": "jog", "jog": None})
        return hold

    def end_jogs(self, why):
        """FluidNC was reset or the port closed: every jog is over, and nothing is left to cancel."""
        hold = self.end_hold()
        if hold:
            self.log_jog_end(hold, why)
        c, self.cancel = self.cancel, None
        if c and c["hold"]:
            self.log_jog_end(c["hold"], c["why"])
        self.jog_mark = None
        self.steps_done = 0

    def log_jog_end(self, hold, why):
        text = "Jog stopped" + (f" ({why})" if why else "")
        pos = self.status["mpos"] if self.status else None
        if hold.sent and hold.start and pos:
            text += f": moved {fixed(math.dist(hold.start, pos))} in"
        self.machine.log("info", text)

    # -------------------------------------------------- alarms, settings, zero (lock held)

    def alarm_line(self, text, now):
        """'ALARM:1'. It never answers a line. A critical one (hard or soft limit) is followed at once by a
        message with 'Reset to continue' (ALARM:16, an I/O expander's reset: 'Reboot FluidNC'); one printed by a
        reset (ALARM:14 Unhomed, ALARM:3) comes just before the reset's banner (protocol_do_rt_reset), which
        keeps it. FluidNC sleeps 0.5 s after printing it (alarm_msg): 3.9.4 answers '?' meanwhile with the state
        from before (see on_report), 4.1.1 sets the state first and says Alarm. An alarm leaves the spindle and
        the coolant on (off_on_alarm is false; both versions): only a reset turns them off. The alarm's
        "outputs" says they may still be on: an alarm in a job (whose program may have started them), or when the
        last reports said one was on; the next report with Ov says it for sure.
        Not for the alarms a reset prints (ours, or FluidNC's own reset button): that reset turns them off.
        During a stop (the safe stop's hold, before its reset) the reset the user asked for still goes: nothing
        moves in an alarm, and the reset turns the spindle and the vacuum/AUX off."""
        code = text[6:].strip()
        code = int(code) if code.isdigit() else None
        self.alarm_at = now
        job = self.job_active()
        halt = self.halt if self.reset_at is None else None
        outputs = (halt is None and self.reset_at is None and code not in RESET_ALARMS
                   and (job or self.outputs_on()))
        self.set_alarm({"code": code, "critical": False, "text": text, "outputs": outputs})
        if self.hold:
            self.out.append(self.stop_jog(now, text))
        self.halt = self.resume_at = None   # (an alarm stops every move: nothing left to stop or resume)
        if job:                             # nothing more of it goes (FluidNC answers what it has: error:9)
            self.end_job(now, "failed", explain(text), explain(text))
        if halt is not None:
            self.machine.log("info", f"{text} during the stop: sent the Soft Reset anyway (nothing moves in ALARM, "
                             "and the reset turns the spindle and the vacuum/AUX off)")
            self.soft_reset(now, halt["why"])
        if outputs:
            self.machine.log("error", OUTPUTS_ON)

    def outputs_on(self):
        """The spindle, the vacuum (flood) or AUX (mist) is on, as far as FluidNC's reports say."""
        p, st = self.parser, self.status
        return bool(p.flood or p.mist or p.spindle in ("cw", "ccw") or (st and (st["spindle"] or 0) > 0))

    def alarm_outputs(self, on):
        """A report with Ov during an alarm: the spindle and coolant are on or off for sure now. (Not after the
        alarm of a reset: all is off then, and only G-code, refused in an alarm, could start them.)"""
        if self.alarm and self.alarm.get("code") not in RESET_ALARMS and bool(self.alarm.get("outputs")) != on:
            self.set_alarm(dict(self.alarm, outputs=on))

    def set_alarm(self, alarm):
        if alarm != self.alarm:
            self.alarm = alarm
            if self.connected:
                self.machine.emit({"type": "alarm", "alarm": alarm})

    def got_setting(self, name, value):
        """'axes/x/max_rate_mm_per_min', '5000.000'; 'axes/x/soft_limits', 'true'. Only finite numbers above 0,
        and true/false for soft_limits, are kept."""
        parts = name.lower().split("/")
        if parts[0] == "coolant":
            self.got_coolant_setting(parts[1:], value.strip())
            return
        if len(parts) == 3 and parts[0] == "axes" and parts[1] in self.axes and parts[2] == "soft_limits":
            flag = {"true": True, "false": False}.get(value.strip().lower())
            if flag is not None:
                self.axes[parts[1]]["softLimits"] = flag
            return
        try:
            v = float(value)
        except ValueError:
            return
        if not (math.isfinite(v) and v > 0):
            return
        if parts == ["planner_blocks"]:
            self.planner_blocks = int(v)
        elif len(parts) == 3 and parts[0] == "axes" and parts[1] in self.axes:
            key = {"max_rate_mm_per_min": "maxRate", "acceleration_mm_per_sec2": "accel"}.get(parts[2])
            if key:
                self.axes[parts[1]][key] = v

    def got_coolant_setting(self, parts, value):
        """'flood_pin', 'gpio.25' (or 'I2SO.4', 'NO_PIN'...); 'delay_ms', '0'. FluidNC builds A:'s F and M by reading
        the coolant pins back, and an ESP32 GPIO set up as an output only reads 0 (CoolantControl::get_state,
        gpio_get_level): its reports never show an output on a gpio pin. Its switch then follows FluidNC's M7/M8/M9
        state (as FluidSender tracks it: toggles, M7/M8/M9/M2/M30 at their ok, resets, and a quiet $G after a
        toggle and when a job ends); I2SO and expander pins read back their state, so their reports still count.
        Until the pins are known, reports don't change the switches ($G said them at connect)."""
        if parts in (["flood_pin"], ["mist_pin"]):
            key = parts[0][:-4]
            self.coolant_pins[key] = value
            self.parser.readback[key] = not value.lower().startswith("gpio.")
            if value.upper() == NO_PIN and getattr(self.parser, key):     # (never on: M8/M7 do nothing then)
                p = self.parser
                self.set_coolant((False, p.mist) if key == "flood" else (p.flood, False), self.status_at)
        elif parts == ["delay_ms"]:
            try:
                v = float(value)
            except ValueError:
                return
            if math.isfinite(v) and v >= 0:
                self.coolant_delay = int(v)

    def modal_coolant(self, key):
        """The switch follows FluidNC's M7/M8/M9 state, not its reports (see got_coolant_setting)."""
        return self.parser.readback[key] is False

    def no_pin(self, key):
        return (self.coolant_pins[key] or "").upper() == NO_PIN

    def coolant_missing(self, key):
        """Why the switch can't do anything: FluidNC has no such output (its pin is NO_PIN: M8/M7 and the toggles
        do nothing then), or None."""
        if not self.no_pin(key):
            return None
        return ("FluidNC has no flood_pin configured (the vacuum output)" if key == "flood"
                else "FluidNC has no mist_pin configured (the AUX output)")

    def coolant_busy(self, key):
        """An M7/M8/M9 of this output that FluidNC has not answered yet (a typed line, or a job's: in its hands),
        as the user's message, or None. FluidNC sets the coolant mode first, then waits for the moves before the
        line, then switches the output: a toggle meanwhile flips the mode already set, which cancels the line's M
        word (Protocol.cpp: the toggle acts on the parser state). (M2/M30 wait first and switch everything off
        only then: harmless.)"""
        nums = COOLANT_OF[key]
        job = self.job
        for line in (self.owed, self.inflight, *(job.inflight if job is not None else ())):
            for n, m in (line.cool if line is not None else ()):
                if n in nums:
                    word = f"M{n}" if m == 0 else f"M{n}.{m // 10}"
                    who = (f"The program's {word} (line {line.no})" if line.auto == "job"
                           else f"The {word} you sent")
                    return (f"{who} hasn't run yet: it waits for the moves before it, and a click now would cancel "
                            "it. Try again once it has run.")
        return None

    def toggle_settled(self, now):
        """The last vacuum/AUX toggle went out SETTLE ago or more, so FluidNC has acted on it before a line sent now.
        FluidNC (4.1.1 and 3.9.4) queues a toggle as an event, and its main loop runs a line that came in the same
        read before it handles the events (Protocol.cpp protocol_main_loop, RealtimeCmd.cpp): a $G sent with the
        toggle can show the modes from before it, and an M8 sent with it is then flipped by it (the M8 sets the mode,
        waits for the moves, and the toggle in that wait flips it: see coolant_busy). So a coolant $G and a line
        with M7/M8/M9 wait until then (start_next, send_job)."""
        return self.toggled_at is not None and now >= self.toggled_at + SETTLE

    def ask_coolant(self, behind=False):
        """A quiet $G, for FluidNC's coolant modes now: what a switch that follows them shows (see gc_reply). Not
        while a job or a scan runs, nor behind other lines (they may take a while; the tracking is right
        meanwhile), unless behind (a job's end: after its last lines). One sent before the last toggle is asked
        again; one waiting goes after it anyway, once it has settled (see toggle_settled)."""
        if self.job_active() or self.machine.activity or self.stopping or not self.connected:
            return
        if any(line.auto == "coolant" for line in self.queue):
            return
        if not behind and (self.job_owed() or any(line is not None and line.auto != "coolant"
                                                  for line in (self.inflight, self.owed, *self.queue))):
            return
        self.queue.append(Line("$G", "coolant", quiet=True))

    def coolant_report(self):
        """A report with Ov made after the last change (StatusParser.told): an output its reports don't show while
        FluidNC's modes, which its $G (or reset) said and nothing changed since, have it on: FluidNC can't read
        that pin back (see got_coolant_setting). Said once. If the pin wasn't known, its switch follows the modes
        from now on."""
        p = self.parser
        if not (p.told and self.coolant_sure and p.before):
            return
        for i, (key, letter) in enumerate((("flood", "F"), ("mist", "M"))):
            if not p.before[i] or letter in (p.acc or ""):
                continue
            if self.coolant_pins[key] is None and p.readback[key]:
                p.readback[key] = False         # (pin unknown, and it reads back nothing: follow the modes)
                self.set_coolant((True, p.mist) if key == "flood" else (p.flood, True), self.status_at)
            if p.readback[key] is False and not self.coolant_said:
                self.coolant_said = True
                self.machine.log("info", "FluidNC's reports don't show the coolant outputs; following its M7/M8/M9 "
                                 "state")

    def axes_info(self):
        """The axes for the page: max rates in in/min, accelerations in in/s^2, soft limits on or off; None if
        they couldn't be read."""
        return {a: {"maxRate": _per_inch(v["maxRate"]), "accel": _per_inch(v["accel"]), "softLimits": v["softLimits"]}
                for a, v in self.axes.items()}

    def settings_done(self):
        rates = [(a.upper(), v["maxRate"]) for a, v in self.axes.items()]
        known = [f"{a} {r / MM:.1f}" for a, r in rates if r]
        missing = [a for a, r in rates if not r]
        if not known:
            text = "Could not read FluidNC's max speeds: jog speeds are not capped here (FluidNC still caps them)"
        else:
            text = "FluidNC max speeds: " + " · ".join(known) + " in/min"
            if missing:
                text += f" ({' '.join(missing)}: could not be read)"
        self.machine.log("info", text)
        off = [a.upper() for a, v in self.axes.items() if v["softLimits"] is False]
        if off:
            self.machine.log("info", f"FluidNC's soft limits are off on {' '.join(off)}: it doesn't stop jogs or moves "
                             "at the end of the travel there")
        if self.connected:
            self.machine.emit({"type": "axes", "axes": self.axes_info(), "plannerBlocks": self.planner_blocks})

    def zeroed(self, record):
        """A zero's G10 got its ok: log the work position from before it, and keep the record for Undo Zero (the
        Machine keeps the last ZERO_KEEP, in settings.json)."""
        axes, wco, mpos = record["axes"], record["wco"], self.parser.mpos   # (FluidNC was still: mpos is where)
        before = ""
        if mpos:
            before = " ".join(f"{a} {fixed((mpos[i] - wco[i]) / MM)}" for a, i in zip(axes, map("XYZ".index, axes)))
        names = " ".join(axes)
        what = f"Zeroed {names}" if record["value"] == 0 else f"Set {names} to {fixed(record['value'])} in"
        pendant = record["source"] == "pendant"
        what += " from the pendant" if pendant else ""
        text = f"{what}. Work position before: {before} (in)" if before else what
        if pendant:                         # (its zero keys have no second chance of their own: this is it)
            text += ". Undo Zero on the page puts it back"
        self.machine.log("info", text)
        self.machine.keep_zero(dict(record, t=round(time.time(), 3), text=before))

    def undone(self, record, now):
        """Undo Zero's G10 L2 got its ok: that record is used up. In the active coordinate system the position
        shows it at once (FluidNC sends the WCO only in every 10th report, 30th while busy): the WCO is the offset
        put back."""
        p, names, wcs = self.parser, " ".join(record["axes"]), wcs_name(record["wcs"])
        text = f"Zero undone: the {wcs} work offset of {names} is back to what it was before that zero"
        if record["wcs"] != self.active_wcs():
            text += f" ({wcs} isn't the active one: the position shown doesn't change)"
        elif p.mpos is not None:
            idx = [("XYZ".index(a), a) for a in record["axes"]]
            self.set_work({i: p.mpos[i] - record["wco"][i] for i, _ in idx}, now)
            pos = " ".join(f"{a} {fixed((p.mpos[i] - record['wco'][i]) / MM)}" for i, a in idx)
            text += f". Work position now: {pos} (in)"
        self.machine.drop_zero(record)
        left = len(self.machine.zeros)
        self.machine.log("info", text + (f". {left} older zero{'s' * (left != 1)} can still be undone" if left else ""))

    def active_wcs(self):
        """The work coordinate system FluidNC is in, 1 (G54) to 9 (G59.3), as far as FluidSender knows: what lines
        chose since connecting (after a reset: G54), else what $G said, else G54 (FluidNC's default)."""
        if self.wcs is not None:
            return self.wcs
        return gc_wcs(self.modal["gc"] if self.modal else None) or 1

    def set_work(self, work, now):
        """A zero's G10 L20 (or an undo's G10 L2) got its ok: those axes' work positions are now work (mm).
        FluidNC sends the new WCO only in every 10th report (up to 2 s later; 30th while busy), so it is worked out
        here (for a zero exactly whatever G92 or tool length offsets are on top): WCO = MPos - work position. The
        position shows it at once, and the next zero's undo record starts from it."""
        p = self.parser
        if p.wco is None or p.mpos is None or self.status is None:
            return
        wco = (list(p.wco) + [0.0] * 3)[:max(3, len(p.wco))]
        for i, w in work.items():
            wco[i] = p.mpos[i] - w
        p.wco = wco
        self.status = dict(self.status, wpos=_inches([m - w for m, w in zip(p.mpos, wco)]))
        self.emit_status(now, force=True)

    # -------------------------------------------------- what the buttons may do now (lock held)

    def state_why(self, now, states, wrong=None):
        """Why a button can't act now, from FluidNC's state (a message for the user), or None.
        states: the ones it acts in; wrong: the message for the others."""
        st = self.status
        if self.hold:
            return JOGGING
        if self.reset_at is not None:
            return "FluidNC is being reset; try again in a second"
        if self.halt is not None:
            return STOPPING
        if not st or now - self.status_at > NO_RESPONSE:
            return "No response from FluidNC"
        if self.alarm and self.alarm["critical"]:
            return CRITICAL
        if self.old_hold:
            return self.old_hold
        if st["state"] not in states:
            return wrong or STATE_WHY.get(st["state"], f"Not while FluidNC is in {st['state']}")
        return None

    def units(self):
        """The units FluidNC reads G-code in now: 'G21' only if known to be millimeters."""
        return "G21" if self.modal and self.modal["units"] == "G21" else "G20"

    def line_why(self, jogs_too):
        """Why a button must wait for lines FluidNC hasn't done yet (jogs_too: $J lines count too), or None."""
        if self.job_owed():
            return self.job_owed()
        for line in (self.owed, self.inflight, *self.queue):
            if line and (jogs_too or not line.jog):
                if line is self.owed:
                    return f"FluidNC has not answered '{line.text}' yet; Soft Reset drops it"
                if line.jog:
                    return "Jogging: wait until it has stopped"
                if line.auto == "coolant":
                    return "Asking FluidNC for the vacuum/AUX state; try again in a second"
                if line.auto:
                    return "Still talking to FluidNC after connecting; try again in a second"
                return f"Waiting for FluidNC to finish '{line.text}'"
        return None


def _finite(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_finite(x) for x in v]
    return v


def encode(ev):
    """One line of the event stream. NaN and Infinity aren't JSON (the page would drop the event): null."""
    try:
        return (json.dumps(ev, separators=(",", ":"), allow_nan=False) + "\n").encode()
    except ValueError:
        return (json.dumps(_finite(ev), separators=(",", ":"), allow_nan=False) + "\n").encode()


class Subscriber:
    """One page's event stream: encoded events waiting to be written to it."""
    LIMIT = 5000

    def __init__(self, first):
        self.cond = threading.Condition()
        self.items = [first]
        self.closed = False

    def put(self, data):
        with self.cond:
            if self.closed:
                return
            if len(self.items) >= self.LIMIT:   # the page stopped reading: end its stream; it reopens with a snapshot
                self.closed = True
                self.items.clear()
            else:
                self.items.append(data)
            self.cond.notify()

    def get(self, timeout):
        """Everything waiting, as one bytes object; b'' if nothing came within timeout seconds."""
        with self.cond:
            if not self.items and not self.closed:
                self.cond.wait(timeout)
            data = b"".join(self.items)
            self.items.clear()
            return data

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify()


class Machine:
    """The FluidNC connection, shared by every open page. Opens a port only in connect().
    The page's calls: connect, disconnect, send (a typed line), realtime (hold, resume, reset, status,
    jogcancel), jog (step, or start/keep/stop a hold jog), zero (or undo it), home, unlock, coolant
    (the vacuum and AUX switches), goto, load_program, level (auto-level: leveling.py), run, stop (the safe
    stop) and set_jog_settings.
    The pendant uses the same calls (and pause_toggle, jog_settings, pendant_status); the scanner
    begin_activity, scan_move, end_activity, stop, work_setup and scan_complete. Refused calls raise
    MachineError (409, with the reason) when FluidNC is in the wrong state, a job or scan runs, or lines are
    still waiting."""

    EXTRAS = ("probe", "scan", "pendant")

    def __init__(self, extra_ports=(), only_extra=False, release_after=RELEASE_AFTER, open_only_extra=False,
                 settings=None):
        self.clock = time.monotonic                    # (the reader thread's clock; tests replace it)
        self.jog_ids = 0
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)  # notified on every line from FluidNC (scan_move waits)
        self.settings = settings if settings is not None else Settings(None)   # (None: in memory only)
        self.zeros = self.settings.get("zeros") or []  # Undo Zero's records, oldest first (see zero, keep_zero)
        self.program = None                            # the loaded program (job.Program), or None
        self.uploads = 0                               # program uploads begun ...
        self.loaded = 0                                # ... and the newest one kept
        self.job = None                                # the last job, also once its connection is gone
        self.activity = None                           # 'scan' while the scanner uses the machine
        self.activity_stop = False                     # Stop was pressed during it
        self.local = threading.local()
        self.probe = self.scanner = self.pendant = None     # attached by server.py (probe.py, scan.py, pendant.py)
        self.extra_seq = 0                             # events of theirs sent, and the last of each:
        self.extra_last = {}                           # {'probe': (number, its state)}
        self.extra = [p for p in extra_ports if p]     # more ports to list (tests: the simulator's pty)
        self.only_extra = only_extra                   # tests: list and open nothing else
        self.open_only_extra = only_extra or open_only_extra   # tests: list the real ports too, but never open one
        self.release_after = release_after
        self.link = None
        self.connecting = False
        self.subs = set()
        self.alone_since = time.monotonic()            # when the last page went away
        self.console = deque(maxlen=CONSOLE_KEEP)
        self.leveling = Leveling(self)                 # auto-level: the leveled program, if loaded (leveling.py)

    # -------------------------------------------------- events

    def emit(self, ev):
        data = encode(ev)
        with self.lock:
            kind = ev.get("type")
            if kind in self.EXTRAS:         # (a new page's snapshot starts from the last one: see extra_states)
                self.extra_seq += 1
                self.extra_last[kind] = (self.extra_seq, ev.get(kind))
            for sub in self.subs:
                sub.put(data)

    def notify(self):
        """Wakes the calls that wait for FluidNC (scan_move)."""
        with self.lock:
            self.changed.notify_all()

    def attach(self, probe=None, scanner=None, pendant=None):
        """The probe, scanner and pendant (server.py makes them): their state goes into the snapshot, and
        Stop stops a scan."""
        with self.lock:
            self.probe, self.scanner, self.pendant = probe, scanner, pendant

    def release_extras(self, now):
        """Lets go of the pendant's and the probe's ports once no FluidSender window has been open for
        release_after, FluidSender holds no FluidNC port (Link.check_release lets go of that one first, and never
        during a job, a scan or a move: the pendant stays then, its Stop and Pause work; nor in an ALARM that may
        have left the spindle or vacuum/AUX on: after a hard or soft limit the pendant's Stop is the Soft Reset
        that turns them off) and no scan runs: closing the window lets go of everything. A probe still connecting
        is left to finish (its connect waits SETTLE s on the open port): a later call lets go of it. server.py
        calls it every second.
        Returns the names of those it let go of (the pendant's disconnect(), the probe's let_go()), "pendant"
        before "probe" ([] for none). (A pendant whose reader takes longer to stop than its disconnect() waits,
        pendant.WRITE_TIMEOUT + 2 s: 2.5 s, closes its port a moment later.) A probe let go of while its tip may be
        down (not homed since a P, an abort or a failed H: a scan Stop, say; or connected and never homed, which
        the page warned for too: Probe.let_go) gets a line in the console too; its state()["mayBeDown"] outlives
        the port, so the page warns."""
        with self.lock:
            if self.subs or now - self.alone_since < self.release_after or self.link is not None or self.activity:
                return []
            extras = [(name, x) for name, x in (("pendant", self.pendant), ("probe", self.probe)) if x is not None]
        done, down = [], False
        for name, extra in extras:          # (not with the lock held: their disconnect waits for their reader)
            try:
                st = extra.state()
                if st.get("port") is None or st.get("connecting"):  # (a port: the pendant connected or waiting,
                    continue                # the probe connected or connecting; a connect is let finish first)
                (getattr(extra, "let_go", None) or extra.disconnect)()  # (the probe: let_go keeps its warning)
                done.append(name)
                down = down or (name == "probe" and extra.state().get("mayBeDown") is True)
            except MachineError:            # (the probe, claimed by something: it stays)
                pass
            except Exception:               # (a bug there must not stop the others, nor the caller's loop)
                traceback.print_exc()
        if done:
            self.log("info", f"Released the {' and the '.join(done)} because no FluidSender window is open")
        if down:
            self.log("error", PROBE_LET_GO_DOWN)
        return done

    def log(self, kind, text):
        """A console event: kind 'tx', 'rx', 'info' or 'error'."""
        ev = {"type": "console", "dir": kind, "text": text, "t": round(time.time(), 3)}
        with self.lock:
            self.console.append(ev)
            self.emit(ev)

    def subscribe(self):
        """A new event stream. Its first event is a snapshot; nothing is missed or repeated after it."""
        extras = self.extra_states()        # (they may take their own locks: never with ours held)
        with self.lock:
            sub = Subscriber(encode(dict(self.snapshot(extras), type="snapshot")))
            self.subs.add(sub)
            return sub

    def extra_states(self):
        """The probe's, scanner's and pendant's state() now, and how many of their events had gone out."""
        with self.lock:
            seq = self.extra_seq
            objs = {"probe": self.probe, "scan": self.scanner, "pendant": self.pendant}
        states = {}
        for kind, obj in objs.items():
            if obj is not None:
                try:
                    states[kind] = obj.state()
                except Exception:           # (a bug there must not stop the pages from opening)
                    traceback.print_exc()
                    states[kind] = None
        return seq, states

    def merge_extras(self, extras):
        """Their snapshot entries (lock held): an event of theirs sent after state() was asked is newer."""
        seq, states = extras
        out = {}
        for kind in self.EXTRAS:
            last = self.extra_last.get(kind)
            out[kind] = last[1] if last and last[0] > seq else states.get(kind)
        return out

    def unsubscribe(self, sub):
        with self.lock:
            if sub in self.subs:
                self.subs.discard(sub)
                if not self.subs:
                    self.alone_since = time.monotonic()

    def snapshot(self, extras=None):
        if extras is None:
            extras = self.extra_states()
        jog = self.settings.get("jog")
        with self.lock:
            link = self.link if self.link and self.link.connected else None
            now = self.clock()
            snap = {
                "connected": link is not None,
                "port": link.path if link else None,
                "firmware": link.firmware if link else None,
                "status": link.current_status(now) if link else None,
                "modal": link.modal if link else None,
                "axes": link.axes_info() if link else _no_axes(),
                "jog": link.hold.info() if link and link.hold else None,
                "zeroUndo": self.zero_info(),
                "alarm": link.alarm if link else None,
                "program": self.leveling.info(self.program),     # (with "leveled" when it is a leveled one)
                "job": self.job.info(now) if self.job else None,
                "jogSettings": jog,
            }
            snap.update(self.merge_extras(extras))
            self.leveling.snapshot(snap)    # ("level": the saved auto-level settings, once there are any)
            snap["console"] = list(self.console)
            return snap

    # -------------------------------------------------- calls from the page

    def ports(self):
        paths = []
        if not self.only_extra:
            for pattern in ("ttyUSB", "ttyACM", "rfcomm"):
                names = [n for n in os.listdir("/dev") if n.startswith(pattern) and n[len(pattern):].isdigit()]
                paths += ["/dev/" + n for n in sorted(names, key=lambda n: int(n[len(pattern):]))]
        paths += [p for p in self.extra if p not in paths and os.path.exists(p)]
        ports = [describe_port(p) for p in paths]
        holders = scan_holders({os.path.realpath(p) for p in paths})
        with self.lock:
            mine = self.link.path if self.link else None
        for p in ports:
            pids = holders.get(os.path.realpath(p["path"]), [])
            others = [pid for pid in pids if pid != os.getpid()]
            if others:
                p["inUseBy"] = holders_text(others)
            elif pids:
                p["inUseBy"] = "FluidSender (connected)" if p["path"] == mine else "FluidSender"
        default = next((p["path"] for p in ports if p["kind"] == "usb-serial" and not p["inUseBy"]), None)
        return {"ports": ports, "default": default}

    def connect(self, port, baud=115200, force=False):
        path = port.strip() if isinstance(port, str) else ""
        if not path:
            raise MachineError("No port given")
        try:
            baud = int(115200 if baud is None else baud)
        except (TypeError, ValueError):
            raise MachineError(f"Bad baud rate: {baud!r}")
        if baud not in BAUDS:
            raise MachineError(f"Unsupported baud rate {baud}")
        with self.lock:
            if self.link:
                raise MachineError(f"Already connected to {self.link.path}. Disconnect first.", 409)
            if self.connecting:
                raise MachineError("Already connecting", 409)
            self.connecting = True
        try:
            self._connect(path, baud, force)
        finally:
            with self.lock:
                self.connecting = False

    def _connect(self, path, baud, force):
        real = os.path.realpath(path)
        test_ports = {os.path.realpath(p) for p in self.extra}
        if self.open_only_extra and real not in test_ports:
            raise MachineError(f"{path} is not one of the test ports this server may open")
        kind = port_kind(real)
        if real not in test_ports and kind not in ("usb-serial", "usb-acm", "bluetooth"):
            raise MachineError(f"{path} is not a USB or Bluetooth serial port")
        if not os.path.exists(real):
            raise MachineError(f"{path} is not there (unplugged?)")
        if kind in RISKY and not force:
            raise MachineError(f"{path} looks like {RISKY[kind]}, not FluidNC. Not connected.")

        def in_use(others):
            return MachineError(f"Port {path} is in use by {others}. Disconnect it there first.", 409)
        others = other_holders(real)
        if others:
            raise in_use(others)
        fd = open_port(real, BAUDS[baud])
        others = other_holders(real)        # someone may have opened it just before we locked it
        if others:
            close_port(fd)
            raise in_use(others)
        link = Link(self, fd, path, baud)
        with self.lock:
            self.link = link
        link.start()
        answered = link.first.wait(FIRST_STATUS)    # set by the first status report, or by the port closing
        with self.lock:
            live = answered and not link.closed
            if live:
                link.went_live(time.monotonic())
                self.leveling.connected(link.path)      # (auto-level: another FluidNC turns it off)
        if not live:
            why = link.stopping[0] if link.closed and link.stopping else ""   # e.g. lost, or Disconnect pressed
            link.stop(None)
            raise MachineError(why or f"No answer from {path} within {FIRST_STATUS:g} s. Is it the FluidNC "
                               f"controller, switched on, at {baud} baud?")
        link.wake()

    def disconnect(self):
        """Closes FluidNC's port (also when the server stops). A jog gets its jog cancel first. During a job, a
        scan, a move that may be under way (Link.may_move) or a stop: the safe stop first (see stop: Feed
        Hold, then Soft Reset once FluidNC has stopped, STOP_WAIT at most), and the port closes only once that
        reset has gone out. So FluidNC is never left holding with the spindle on, nor with lines from before
        that a Resume (or a reset: see Link.stray) would run, with nobody watching."""
        with self.lock:
            link = self.link
            byte = None
            busy = False
            if link and link.connected and link.jog_active():
                byte = link.stop_jog(self.clock(), "disconnected")
            elif link and link.connected:
                busy = bool(link.halt or link.job_active() or self.activity or link.may_move(self.clock()))
        if busy:
            self.stop_first(link)
        if link:
            if byte:
                try:
                    link.write_realtime(byte)       # a jog must not run on with nobody watching
                except OSError:
                    pass
            link.stop(f"Disconnected from {link.path}")

    def stop_first(self, link):
        """Disconnect during a job, a scan or a move: the safe stop, then a wait until FluidNC has been reset
        and a report shows it ran no line left from before that reset (FluidNC can, 3.9.4 and 4.1.1: see
        Link.stray; that is then held and reset again while FluidSender still watches). In an alarm the safe stop
        is the reset at once (see Link.safe_stop): its banner is waited for the same way. At most STOP_WAIT and
        two RESET_WAITs, and a second more. The reader thread writes what was decided before it closes the port."""
        try:
            stopped = self.stop()["stopped"]
        except MachineError:
            return
        with self.lock:
            if link.halt is None and link.reset_at is None:
                return                      # (nothing to hold: a scan between its moves, say)
            # (in an alarm the stop is the reset at once, with no hold: its banner and a clean report are waited for
            # too. After a soft limit FluidNC resets to Idle, where a line left from before could run)
            if self.clock() - link.status_at > NO_RESPONSE:
                if link.halt is None:       # (in an alarm: the reset went at once)
                    self.log("info", "Disconnect: FluidNC doesn't answer, so it can't be seen to reset: sent Soft "
                             "Reset and closed the port. Connect again when it answers.")
                else:
                    self.log("info", "Disconnect: FluidNC doesn't answer, so it can't be seen to stop: sent Feed "
                             "Hold and closed the port. Connect again and Soft Reset when it answers.")
                return
            what = {"job": "the job", "scan": "the scan"}.get(stopped, "a move")
            self.log("info", f"Disconnect during {what}: the port closes once FluidNC has stopped and been reset")
            end = time.monotonic() + STOP_WAIT + 2 * RESET_WAIT + 1.0
            while ((link.halt is not None or link.reset_at is not None
                    or (link.after_reset is not None and not link.reset_clean))
                   and not link.closed and time.monotonic() < end):
                self.changed.wait(0.1)

    def live(self):
        link = self.link
        if not link or not link.connected:
            raise MachineError("Not connected", 409)
        return link

    def activity_why(self, link):
        """Why the user's lines and buttons must wait: a job or a scan has the machine. Or None."""
        if link.job_active():
            return JOB_RUNNING
        if self.activity:
            return f"A {self.activity} is running"
        return None

    def send(self, line):
        text = check_line(line)
        with self.lock:
            link = self.live()
            why = self.activity_why(link) or (JOGGING if link.hold else None) or (STOPPING if link.halt else None)
            if why:
                raise MachineError(why, 409)
            if len(link.queue) >= MAX_QUEUE:
                raise MachineError(f"{MAX_QUEUE} lines are already waiting for FluidNC; not sent", 409)
            item = Line(text)
            if item.needs_inches and link.inch and link.inch_tries >= INCH_TRIES:
                raise MachineError(f"Not sent: {NO_INCHES}", 409)
            link.queue.append(item)
        link.wake()

    def realtime(self, cmd):
        byte = REALTIME.get(cmd) if isinstance(cmd, str) else None
        if byte is None:
            raise MachineError(f"Unknown realtime command {cmd!r} (hold, resume, reset, status or jogcancel)")
        with self.lock:
            link = self.live()
            now = self.clock()
            if cmd in ("hold", "resume") and link.in_alarm(now):
                raise MachineError(IN_ALARM, 409)   # (a '!' in a critical alarm lets FluidNC out of it: see safe_stop)
            if cmd == "reset":              # (during a job too: it ends at once; Stop is the safe way)
                link.soft_reset(now, "Soft Reset")
            elif cmd == "resume":           # '~' only once FluidNC has stopped (Hold:0): see Link.resume
                link.resume(now)
            elif cmd == "hold":
                if link.jog_active():
                    link.stop_jog(now, "Feed Hold")     # FluidNC takes '!' while jogging as a jog cancel
                link.feed_hold(now)
                link.hold_job(now)
            else:
                if cmd == "jogcancel":
                    link.stop_jog(now)
                link.out.append(byte)
            link.wake()

    def write_now(self, link, byte):
        """A realtime byte (lock held): the reader thread writes it at once, never behind the queued lines,
        but after the bytes decided before it (see Link.out)."""
        link.out.append(byte)
        link.wake()

    # -------------------------------------------------- jog, zero, home, unlock

    def jog(self, data, max_steps=MAX_STEPS):
        """{"step": {"x": 0.1, "y": 0, "z": 0}, "feed": 150}: one step, inches (signed), in/min; refused while
        max_steps step jogs are still waiting or moving (the pendant asks for fewer: see pendant.py).
        {"start": {"x": 1, "y": -1, "z": 0}, "feed": 150}: a hold jog that way (each -1, 0 or 1), returns its id;
        then {"keep": id} every 100 ms while the button is held (else it stops after DEADMAN), and
        {"stop": id or "all"} when it is let go (an ended or unknown id is fine)."""
        calls = [k for k in ("step", "start", "keep", "stop") if k in data]
        if len(calls) != 1:
            raise MachineError("A jog call takes one of step, start, keep or stop")
        if calls[0] == "step":
            return self.jog_step(data["step"], data.get("feed"), max_steps)
        if calls[0] == "start":
            return self.jog_start(data["start"], data.get("feed"))
        if calls[0] == "keep":
            return self.jog_keep(data["keep"])
        return self.jog_stop(data["stop"])

    def jog_step(self, step, feed, max_steps=MAX_STEPS):
        dist = _xyz(step, "Jog step")
        if any(abs(v) > MAX_STEP for v in dist):
            raise MachineError(f"Jog step too long ({MAX_STEP:g} in at most)")
        dist = [float(fixed(v)) for v in dist]          # (what rounds to 0 doesn't move)
        if not any(dist):
            raise MachineError("Nothing to jog: every distance is 0")
        feed = _jog_feed(feed)
        with self.lock:
            link = self.live()
            why = self.activity_why(link) or link.state_why(self.clock(), ("Idle", "Jog")) or link.line_why(False)
            if not why and link.steps_pending() >= max_steps:
                why = f"{max_steps} jog steps are still waiting or moving; wait for them"
            if why:
                raise MachineError(why, 409)
            text = step_line(dist, cap_feed(feed, _unit(dist), link.axes))
            link.queue.append(Line(text, tag="step"))
        link.wake()
        return {"line": text}

    def jog_start(self, start, feed):
        d = _xyz(start, "Jog direction")
        if any(v not in (-1, 0, 1) for v in d) or not any(d):
            raise MachineError("Jog direction: x, y and z must each be -1, 0 or 1, and not all 0")
        feed = _jog_feed(feed)
        with self.lock:
            link = self.live()
            now = self.clock()
            if self.activity_why(link):
                raise MachineError(self.activity_why(link), 409)
            if link.hold:                   # another direction: the old jog stops first
                self.write_now(link, link.stop_jog(now))
            why = link.state_why(now, ("Idle", "Jog")) or link.line_why(False)
            if not why:
                d = [int(v) for v in d]
                unit = _unit(d)
                f = cap_feed(feed, unit, link.axes)
                self.jog_ids += 1
                blocks = link.planner_blocks or BLOCKS
                hold = HoldJog(self.jog_ids, d, f, segment_line(unit, f, link.axes, blocks), now, blocks > BLOCKS)
                link.hold = hold
                link.next_poll = min(link.next_poll, now + FAST_POLL)
                self.emit({"type": "jog", "jog": hold.info()})
                way = " ".join(f"{a}{'+' if v > 0 else '-'}" for a, v in zip("XYZ", d) if v)
                self.log("info", f"Jog {way} at {feed_text(f)} in/min (hold)")
        if why:
            raise MachineError(why, 409)
        link.wake()
        return {"id": hold.id}

    def jog_keep(self, jog_id):
        with self.lock:
            link = self.link
            hold = link.hold if link and link.connected else None
            if hold is None or isinstance(jog_id, bool) or jog_id != hold.id:
                raise MachineError("That jog has ended", 409)
            hold.kept = self.clock()
        return {}

    def jog_stop(self, which):
        with self.lock:
            link = self.link
            hold = link.hold if link and link.connected else None
            if hold is None or not (which == "all" or (not isinstance(which, bool) and which == hold.id)):
                return {}                   # (it has ended already)
            self.write_now(link, link.stop_jog(self.clock()))
        return {}

    def zero(self, axes=None, value=None, undo=False, source="page"):
        """{"axes": "XY", "value": 0}: those axes' work position becomes value (inches) in the active work
        coordinate system; {"undo": true} puts back the work offsets the last zero changed (and again for the
        one before, up to ZERO_KEEP zeros, from the page or the pendant (source), also after a reset or
        reconnect: see undo_zero). (The call's dict may also come whole, as the first argument.)"""
        if isinstance(axes, dict):
            axes, value, undo = axes.get("axes"), axes.get("value"), axes.get("undo") is True
        if undo:
            return self.undo_zero()
        letters = axes.upper() if isinstance(axes, str) else ""
        if not letters or set(letters) - set("XYZ") or len(set(letters)) != len(letters):
            raise MachineError("Zero which axes? X, Y and/or Z, each once")
        value = 0.0 if value is None else _number(value)
        if value is None or abs(value) > MAX_ZERO:
            raise MachineError(f"Work position: a number from -{MAX_ZERO:g} to {MAX_ZERO:g} in")
        letters = "".join(a for a in "XYZ" if a in letters)
        with self.lock:
            link = self.live()
            now = self.clock()
            # G10 L20 counts from where the lines before it leave the machine: none of the user's may wait, and
            # FluidNC must have said Idle (or Jog) since the last reply (a move just answered may not show yet).
            # Behind jogs it waits until they have stopped (see start_next).
            why = self.activity_why(link) or link.state_why(now, ("Idle", "Jog")) or link.line_why(False)
            behind_jogs = link.jogging() or link.steps_pending()
            if not why and link.fresh_state(now) not in ("Idle", "Jog") and not behind_jogs:
                why = "FluidNC is busy; try again in a second"
            if not why and link.parser.wco is None:
                why = "Work offset not known yet; try again in a second"
            if not why and len(link.queue) >= MAX_QUEUE:
                why = f"{MAX_QUEUE} lines are already waiting for FluidNC; not sent"
            if why:
                raise MachineError(why, 409)
            text = zero_line(letters, value, link.units())
            # Undo Zero's record (kept once FluidNC has answered ok: see Link.zeroed): the work offset before, as
            # machine coordinates (mm), and which coordinate system P0 is now
            record = {"axes": letters, "value": value, "wco": (list(link.parser.wco) + [0.0] * 3)[:3],
                      "wcs": link.active_wcs(), "source": "pendant" if source == "pendant" else "page"}
            work = {"XYZ".index(a): float(fixed(value)) * MM for a in letters}
            link.queue.append(Line(text, tag="zero", data=record, work=work))
        link.wake()
        return {"line": text}

    def undo_zero(self):
        """Undo Zero: the newest zero's axes get the work offset back that they had before it (G10 L2 P<its
        coordinate system>, in the units FluidNC is in: see undo_line); its ok uses the record up, and the next
        Undo Zero goes back one more. The records stay over resets, reconnects and restarts (settings.json),
        as FluidNC keeps the offsets themselves."""
        with self.lock:
            link = self.live()
            now = self.clock()
            why = self.activity_why(link) or (JOGGING if link.hold else None if self.zeros else "No zero to undo")
            why = why or link.state_why(now, ("Idle",)) or link.line_why(True)
            if not why and (link.jogging() or link.fresh_state(now) != "Idle"):
                why = "FluidNC is busy; try again in a second"      # (the position shown after it: where it stands)
            if why:
                raise MachineError(why, 409)
            record = self.zeros[-1]
            text = undo_line(record["axes"], record["wco"], record["wcs"], link.units())
            link.queue.append(Line(text, tag="undo", data=record))
        link.wake()
        return {"line": text}

    def zero_info(self):
        """What Undo Zero puts back, for the page: the newest zero as {"axes", "text" (the work position before
        it, in), "wcs" ("G54"), "source" ("page" or "pendant"), "t" (unix time), "count" (zeros that can be
        undone, this one too)}, or None."""
        with self.lock:
            if not self.zeros:
                return None
            r = self.zeros[-1]
            return {"axes": r["axes"], "text": r["text"], "wcs": wcs_name(r["wcs"]), "source": r["source"],
                    "t": r["t"], "count": len(self.zeros)}

    def keep_zero(self, record):
        """A zero was done (its ok came): its record is the newest; beyond ZERO_KEEP the oldest is forgotten."""
        with self.lock:
            self.zeros = (self.zeros + [record])[-ZERO_KEEP:]
            self.zeros_changed()

    def drop_zero(self, record):
        """Undo Zero put that record's offsets back: used up."""
        with self.lock:
            self.zeros = [r for r in self.zeros if r is not record]
            self.zeros_changed()

    def zeros_changed(self):
        """The records go to settings.json (FluidNC keeps its offsets over resets and power cycles, and so Undo
        Zero keeps them too: never forgotten on a reset or a disconnect) and to the pages."""
        try:
            self.settings.set("zeros", self.zeros)
        except Exception:                   # (a file that can't be written must not get in the zero's way)
            traceback.print_exc()
        self.emit({"type": "zero", "zeroUndo": self.zero_info()})

    def home(self, axes=""):
        """'' -> $H (all axes), 'X', 'Y' or 'Z' -> $HX, $HY or $HZ (or the call's dict: {"axes": ...})."""
        if isinstance(axes, dict):
            axes = axes.get("axes", "")
        axis = "" if axes is None else axes
        if not isinstance(axis, str) or axis.upper() not in ("", "X", "Y", "Z"):
            raise MachineError("Home which axis? '' for all, or one of X, Y, Z")
        return self.button_line("$H" + axis.upper(), "home", ("Idle", "Alarm"))

    def unlock(self):
        return self.button_line("$X", "unlock", ("Alarm",), "Not in ALARM: nothing to unlock")

    def button_line(self, text, tag, states, wrong=None):
        """Home and Unlock: only in those states, with FluidNC done with every line and jog."""
        with self.lock:
            link = self.live()
            why = self.activity_why(link) or link.state_why(self.clock(), states, wrong) or link.line_why(True)
            if not why and link.jogging():
                why = "Jogging: wait until it has stopped"
            if why:
                raise MachineError(why, 409)
            link.queue.append(Line(text, tag=tag))
        link.wake()
        return {"line": text}

    # -------------------------------------------------- vacuum and AUX, go to zero

    def coolant(self, which, on=None):
        """{"which": "vacuum" | "aux", "on": true | false}: the vacuum relay is FluidNC's flood (M8), AUX its
        mist (M7). FluidNC has a realtime toggle for each (0xA0, 0xA1), which acts at once (also during a job)
        but only in Idle, Run or Hold: it is sent only when the state known is not the one wanted. The state
        known follows the toggles sent, the M7/M8/M9 lines FluidNC has answered and its reports (see
        Link.expect_coolant), so a click right after the program's M9 doesn't turn the vacuum back on; for an
        output on a plain GPIO pin, which FluidNC's reports can't show, FluidNC's M7/M8/M9 state (a quiet $G
        after the toggle says it: see Link.got_coolant_setting). Refused (409) with FluidNC's NO_PIN for it (no
        such output), while a jog runs or starts (FluidNC ignores the toggle in Jog), and while an M7/M8/M9 of
        it waits in FluidNC for the moves before it: the toggle would cancel that (see Link.coolant_busy)."""
        if isinstance(which, dict):
            which, on = which.get("which"), which.get("on")
        if which not in COOLANT:
            raise MachineError('Which switch? "vacuum" (M8) or "aux" (M7)')
        if not isinstance(on, bool):
            raise MachineError("on: true or false")
        key, byte, name, mcode = COOLANT[which]
        with self.lock:
            link = self.live()
            now = self.clock()
            p = link.parser
            missing = link.coolant_missing(key)
            if missing:
                raise MachineError(missing, 409)
            current = getattr(p, key)
            if current is None:
                raise MachineError(f"{name} state not known yet", 409)
            if current == on:
                return {"sent": False}
            st = link.status
            why = None
            if link.reset_at is not None:
                why = "FluidNC is being reset; try again in a second"
            elif not st or now - link.status_at > NO_RESPONSE:
                why = "No response from FluidNC"
            elif st["state"] not in COOLANT_STATES:
                why = (f"{name} not switched: FluidNC ignores the switch in {st['state']} (it takes it only when "
                       "Idle, running or in Hold)")
            elif link.jog_active():
                why = f"{name} not switched: a jog is running or starting (FluidNC ignores the switch while it jogs)"
            else:
                why = link.coolant_busy(key)
            if why:
                raise MachineError(why, 409)
            link.out.append(byte)
            link.toggles += 1
            link.toggled_at = None          # (the reader thread's next pass writes it: see Link.toggle_settled)
            # FluidNC shows the change in a later report with Ov (4.1.1: the next one; 3.9.4: up to 10, or 20 while
            # busy): until then the page shows what was asked for, reports made before the toggle don't count, and
            # they are asked for quickly until one says what FluidNC did. An output on a plain GPIO pin (or one not
            # known) is never in them: a quiet $G says what FluidNC did
            link.expect_coolant((on, None) if key == "flood" else (None, on), now)
            if link.parser.readback[key] is not True or link.coolant_pins[key] is None:
                link.ask_coolant()
            self.log("info", f"{name} {'on' if on else 'off'} ({mcode} toggle)")
        link.wake()
        return {"sent": True}

    def override(self, which=None, to=None, step=None, reset=None):
        """{"which": "feed" | "spindle", "to": 10..200} or {"which": "feed" | "spindle", "step": 1 | -1 | 10 | -10};
        {"which": "rapid", "to": 100 | 50 | 25}; {"which": "all", "reset": true}: FluidNC's feed, rapid and spindle
        overrides (%). Only its realtime override bytes go (never a line), so they work during a job, a hold, a
        jog or a scan, and do no harm in an alarm: not refused for the state. Feed and rapid change at once (FluidNC
        re-plans every block); the spindle goes SPINDLE_STEPS steps a report (see Link.ov_report). Returns
        {"sent": bytes written now, "expect": [feed, rapid, spindle] as known after them (None: not known yet)}. A
        step or a "to" needs FluidNC to have said its overrides since connecting (after a reset they are 100 %:
        "Overrides not known yet"); reset and rapid are absolute. (The call's dict may also come whole, as the first
        argument.)"""
        if isinstance(which, dict):
            which, to, step, reset = which.get("which"), which.get("to"), which.get("step"), which.get("reset")
        value = _number(to) if to is not None else None
        whole = int(value) if value is not None and value == int(value) else None
        if which == "all":
            if reset is not True or to is not None or step is not None:
                raise MachineError('Overrides: {"which": "all", "reset": true} puts feed, rapid and spindle back to '
                                   '100%')
        elif which == "rapid":
            if whole not in RAPID_BYTES or step is not None or reset is not None:
                raise MachineError("Rapid override: to 100, 50 or 25 (%)")
        elif which in ("feed", "spindle"):
            ok_to = to is not None and step is None and whole is not None and OV_MIN <= whole <= OV_MAX
            ok_step = (to is None and step in (1, -1, 10, -10) and not isinstance(step, bool)
                       and isinstance(step, int))
            if reset is not None or not (ok_to or ok_step):
                raise MachineError(f"{which.capitalize()} override: to {OV_MIN}..{OV_MAX} (%), or step 1, -1, 10 "
                                   "or -10")
        else:
            raise MachineError('Which override? "feed", "spindle", "rapid" or "all"')
        with self.lock:
            link = self.live()
            now = self.clock()
            st = link.status
            if link.reset_at is not None:
                raise MachineError("FluidNC is being reset; try again in a second", 409)
            if not st or now - link.status_at > NO_RESPONSE:
                raise MachineError("No response from FluidNC", 409)
            known = link.parser.ov
            if which == "all":
                sent = link.ov_reset(now)
            elif which == "rapid":
                sent = link.set_override(1, whole, now)
            else:
                i = OV_NAMES.index(which)
                if known is None:
                    raise MachineError("Overrides not known yet: FluidNC hasn't reported them since connecting; try "
                                       "again in a second", 409)
                cur = known[i]
                if step is not None:
                    if (step > 0 and cur >= OV_MAX) or (step < 0 and cur <= OV_MIN):
                        raise MachineError(f"{OV_TITLES[i]} override already at {cur}%", 409)
                    target = max(OV_MIN, min(OV_MAX, cur + step))
                else:
                    target = whole
                sent = link.set_override(i, target, now)
            expect = list(link.parser.ov) if link.parser.ov else [None, whole if which == "rapid" else None, None]
        if sent:
            link.wake()
        return {"sent": sent, "expect": expect}

    def goto(self, to=None):
        """{"to": "xy"}: to the work XY zero (Z first up to SAFE_Z if it is lower); "z": work Z zero; "zero":
        both, Z last (the pendant's Return to zero); "mx", "my", "mz": that axis to machine zero (G53). If
        FluidNC refuses one of the lines, the rest of them don't go (see Link.goto_refused)."""
        if isinstance(to, dict):
            to = to.get("to")
        to = to.lower() if isinstance(to, str) else None
        if to not in ("xy", "z", "zero", "mx", "my", "mz"):
            raise MachineError('Go to where? "xy", "z" or "zero" (work zero), or "mx", "my" or "mz" (machine zero)')
        with self.lock:
            link = self.live()
            now = self.clock()
            why = self.activity_why(link) or link.state_why(now, ("Idle",)) or link.line_why(True)
            if not why and link.jogging():
                why = "Jogging: wait until it has stopped"
            if not why and link.fresh_state(now) != "Idle":
                why = "FluidNC is busy; try again in a second"
            lines = GOTO.get(to)
            if not why and lines is None:
                wpos = link.status["wpos"]
                if wpos is None:
                    why = "Work position not known yet; try again in a second"
                else:
                    lines = [f"G20 G90 G0 Z{fixed(SAFE_Z)}"] if round(wpos[2], 4) < SAFE_Z else []
                    lines.append("G20 G90 G0 X0 Y0")
                    if to == "zero":
                        lines.append("G20 G90 G0 Z0")
            if why:
                raise MachineError(why, 409)
            each = {"to": to, "name": GOTO_NAMES[to]}     # (one record for its lines: see Link.goto_refused)
            for text in lines:
                line = Line(text, tag="goto", data=each)
                if text.startswith("G53"):
                    line.needs_inches = False   # (machine zero is 0 in either unit)
                link.queue.append(line)
        link.wake()
        return {"lines": list(lines)}

    # -------------------------------------------------- jobs

    def load_program(self, name=None, text=None):
        """{"name": "file.nc", "text": "..."}: the G-code the page shows becomes the loaded program (checked;
        refused with the file's line number; lines FluidNC would run otherwise than they read come back as
        "warnings"). {"text": ""} unloads it. A job that runs keeps its own copy."""
        if isinstance(name, dict):
            name, text = name.get("name"), name.get("text")
        text = "" if text is None else text
        if not isinstance(text, str):
            raise MachineError("text: the G-code, as text")
        name = "" if name is None else name
        if not isinstance(name, str):
            raise MachineError("name: the file name")
        name = name.strip()[:200] or "untitled"
        with self.lock:
            self.uploads += 1
            seq = self.uploads
        program = error = None
        if text:
            try:                            # (not with the lock held: a big file takes a while)
                program = parse_program(name, text)
                program.pre_leveled = pre_leveled(name, text)
            except ProgramError as e:
                error = str(e)
        with self.lock:
            if seq > self.loaded:           # (an older upload that finishes last must not win)
                self.loaded = seq
                if program is not None or self.program is not None:
                    self.leveling.loaded(program, text)     # (a leveled program goes: "Auto-level off")
                    self.program = program
                    self.emit({"type": "program", "program": program.info() if program else None})
        if error:
            raise MachineError(f"Program not loaded: {error}")
        return {"lines": len(program) if program else 0, "total": program.total if program else 0,
                "warnings": list(program.warnings) if program else []}

    def level(self, data=None):
        """Auto-level (POST /api/machine/level; leveling.py): {"scan": "last" | {"path": "/abs/x.xyz"}, "zSurface": 0,
        "arcSegmentMm": 0.1} levels the loaded program with that scan, and the leveled program becomes the loaded one
        (Run runs exactly it) -> {"lines", "total", "warnings", "stats", "scan"}; {"off": true}: the original back.
        Refused while a job or a scan runs, and with the reason (the line, for a program leveler.py refuses)."""
        try:
            return self.leveling.level(data if data is not None else {})
        except LevelingError as e:
            raise MachineError(str(e), e.status) from None

    def work_setup(self):
        """FluidNC's work coordinate system and work offset (WCO: G54..G59.3's offset, plus G92 and the tool length
        offset, as FluidNC reports it) now, for the scan's setup line and auto-level's same-setup check:
        {"wcs": "G54", "wco": [x, y, z] in inches (None until FluidNC has reported it), "port"}; None when not
        connected."""
        with self.lock:
            link = self.link if self.link and self.link.connected else None
            if link is None:
                return None
            wco = link.parser.wco
            return {"wcs": wcs_name(link.active_wcs()), "port": link.path,
                    "wco": None if wco is None else [round(v / MM, 6) for v in (list(wco) + [0.0] * 3)[:3]]}

    def scan_complete(self):
        """The scanner has a new complete scan (scan.py): a leveled program goes back to its original."""
        with self.lock:
            self.leveling.scan_complete()

    def run(self):
        """Starts the loaded program, from FluidNC Idle only, with nothing else going on. G20 G90 goes first
        (see job.Job): a program without units runs in inches whatever was left in force before."""
        scanned = self.leveling.scan_exists()      # (auto-level: asks the scanner, never with our lock held)
        with self.lock:
            link = self.live()
            now = self.clock()
            program = self.program
            why = self.activity_why(link) or link.state_why(now, ("Idle",))
            if not why and program is None:
                why = "No program loaded: open a G-code file first"
            if not why and not len(program):
                why = "Nothing to run: the program has no G-code lines"
            if not why and link.inch:       # (inch-first: a program without units runs in inches)
                why = (f"Not started: {NO_INCHES}" if link.inch_tries >= INCH_TRIES
                       else "FluidNC is not set to inches (G20) yet; try again in a second")
            if not why and not (link.modal and link.modal["units"]):
                why = "FluidNC's units are not known yet; try again in a second"
            why = why or link.line_why(True)
            if not why and link.jogging():
                why = "Jogging: wait until it has stopped"
            if not why and link.fresh_state(now) != "Idle":
                why = "FluidNC is busy; try again in a second"
            why = why or self.leveling.run_why(program)     # (auto-level: still leveling, or another WCS than the scan's)
            if why:
                raise MachineError(why, 409)
            job = link.job = self.job = self.leveling.new_job(program, now, time.time(), scanned)  # (info: "leveled")
            for text in self.leveling.started(job, scanned):   # "Job started: x.nc, LEVELED with ..., 55,287 lines"
                self.log("info", text)
            self.log("info", "Run: inches (G20) and absolute (G90) first; the program's own G21/G91 still win")
            link.emit_job(now, force=True)
            info = job.info(now)
        link.wake()
        return {"job": info}

    def stop(self):
        """Stop (the page's Stop and Soft Reset during a job, the pendant's Stop): a scan stops (the scanner
        aborts the probe at once) and so does any move it made; a job gets the safe stop (Link.safe_stop); a
        jog the jog cancel; anything else that may move (Link.may_move: also a move whose ok has just come,
        before a report shows it running), or lines waiting, the safe stop too. Returns {"stopped": "scan" |
        "job" | "jog" | "motion" | None (nothing was going on)}."""
        with self.lock:
            scanning = self.activity == "scan"
            if scanning:
                self.activity_stop = True   # (a scan_move waiting raises at once)
                self.changed.notify_all()
            scanner = self.scanner
        stopped = "scan" if scanning else None
        if scanning and scanner is not None and not getattr(self.local, "stopping", False):
            self.local.stopping = True      # (in case the scanner's stop() calls this again)
            try:
                scanner.stop(stop_machine=False)    # (aborts the probe at once; the machine's part is below)
            except Exception as e:
                traceback.print_exc()
                self.log("error", f"Could not stop the scan ({e!r})")
            finally:
                self.local.stopping = False
        with self.lock:
            link = self.link if self.link and self.link.connected else None
            if link is None:
                if stopped:
                    return {"stopped": stopped}
                raise MachineError("Not connected", 409)
            now = self.clock()
            moving = link.may_move(now)
            if scanning:                    # (its move not sent yet: dropped, nothing to stop for it)
                for line in [line for line in link.queue if line.tag == "scan"]:
                    link.queue.remove(line)
                    line.reply = "dropped"
            if link.reset_at is not None:   # our reset is under way: it stops everything
                pass
            elif link.job_active():
                link.safe_stop(now, "Stopped by the user")
                stopped = stopped or "job"
            elif link.jog_active():
                link.out.append(link.stop_jog(now, "Stop"))
                stopped = stopped or "jog"
            elif link.halt or moving or link.queue:
                link.safe_stop(now, "Stopped by the user")
                stopped = stopped or "motion"
        link.wake()
        return {"stopped": stopped}

    def pause_toggle(self):
        """The pendant's Pause/Resume key: a running job (or, with no job, a move or a jog) holds; a paused
        job (or a Hold) resumes, unless a Resume already waits for FluidNC to stop (Hold:1 -> Hold:0): then it
        holds, which cancels that Resume (pressing again meant to hold). Returns {"did": "hold" | "resume"}.
        Refused during the safe stop; a resume also during a reset and just after it (see Link.resume)."""
        with self.lock:
            link = self.live()
            now = self.clock()
            st = link.status
            state = st["state"] if st and now - link.status_at <= NO_RESPONSE else None
            job = link.job if link.job_active() else None
            if job is not None and job.state == "stopping":
                raise MachineError("The job is being stopped", 409)
            if link.halt is not None:       # (a '~' now would let the move run on into the reset)
                raise MachineError(NO_RESUME, 409)
            if link.resume_at is not None:  # (a Resume waits for Hold:0: this press holds, cancelling it)
                did = "hold"
            elif job is not None:
                did = "hold" if job.state == "running" else "resume"
            elif state in ("Run", "Jog"):
                did = "hold"
            elif state == "Hold":
                did = "resume"
            else:
                raise MachineError("Nothing to hold or resume" + (f" (FluidNC is {state})" if state else ""), 409)
            self.realtime(did)
        return {"did": did}

    # -------------------------------------------------- shared jog settings, the pendant's numbers

    def jog_settings(self):
        """{"step": 0.1, "feed": 150}: the jog step (in) and speed (in/min) the page and the pendant share."""
        return self.settings.get("jog")

    def set_jog_settings(self, step=None, feed=None):
        """Changes the jog step (one of 10, 1, 0.1, 0.01, 0.001 in) and/or speed (in/min); every page and the
        pendant show it (event "jogsettings"). Kept in settings.json. Returns the settings."""
        if isinstance(step, dict):
            step, feed = step.get("step"), step.get("feed")
        if step is None and feed is None:
            raise MachineError("Jog settings: give step and/or feed")
        fields = {}
        try:
            if step is not None:
                fields["step"] = jog_step(step)
            if feed is not None:
                fields["feed"] = jog_feed(feed)
            old = self.settings.get("jog")
            new = self.settings.update("jog", fields)
        except SettingsError as e:
            raise MachineError(str(e)) from None
        if new != old:
            self.emit({"type": "jogsettings", "step": new["step"], "feed": new["feed"]})
        return new

    def pendant_status(self):
        """The numbers for the pendant's display: the work position (in), status (0 idle, 1 hold or paused,
        2 job running, 3 anything else), jog speed (feed) and step, job lines answered (line) of total, and
        the speed now (live, in/min). Unknown numbers are 0."""
        jog = self.settings.get("jog")
        with self.lock:
            link = self.link if self.link and self.link.connected else None
            now = self.clock()
            st = link.current_status(now) if link else None
            pos = (st["wpos"] if st else None) or [0.0, 0.0, 0.0]
            state = st["state"] if st else None
            job, program = self.job, self.program
            active = link is not None and link.job_active()
            if (active and job.state == "paused") or (not active and state == "Hold"):
                status = 1
            elif active:
                status = 2
            elif state == "Idle" and not self.activity:
                status = 0
            else:
                status = 3
            if job is not None and (active or job.program is program):
                line, total = job.acked, job.total
            else:
                line, total = 0, len(program) if program else 0
            live = st["feed"] if st and st["feed"] is not None else 0.0
        return {"x": pos[0], "y": pos[1], "z": pos[2], "status": status, "feed": jog["feed"], "line": line,
                "total": total, "step": jog["step"], "live": live}

    def status_now(self):
        """The latest status as the pages get it ({"state", "wpos" (in, None until known), ...}; state "No
        response" once it is old), or None when not connected: for the pendant's and the scanner's own checks
        (Home only in ALARM, the work Z). Hold the lock over the check and the call it allows."""
        with self.lock:
            link = self.link if self.link and self.link.connected else None
            st = link.current_status(self.clock()) if link else None
            return dict(st) if st else None

    # -------------------------------------------------- the scanner's calls

    def begin_activity(self, name):
        """The scanner takes the machine (name 'scan'): from Idle with nothing going on; then the user's lines,
        jogs, buttons and Run are refused ("A scan is running") until end_activity."""
        with self.lock:
            link = self.live()
            now = self.clock()
            why = self.activity_why(link) or link.state_why(now, ("Idle",)) or link.line_why(True)
            if not why and link.jogging():
                why = "Jogging: wait until it has stopped"
            if not why and link.fresh_state(now) != "Idle":
                why = "FluidNC is busy; try again in a second"
            if why:
                raise MachineError(why, 409)
            self.activity = name
            self.activity_stop = False
        return {}

    def end_activity(self, name):
        with self.lock:
            if self.activity == name:
                self.activity = None
                self.activity_stop = False
                self.changed.notify_all()
        return {}

    def scan_move(self, x, y, timeout=60.0):
        """Moves to work X Y ("G20 G90 G0 X.. Y..", shown in the console) and waits until FluidNC reports Idle
        there (within 0.002 in), in a report made after the move's ok. Raises MachineError on an alarm, an
        error reply, a reset, Stop, the scan ending, disconnect, or no arrival within timeout seconds.
        Returns the work position {"x", "y", "z"} (in). Only during the 'scan' activity; blocks (the
        scanner's own thread)."""
        x, y, limit = _number(x), _number(y), _number(timeout)
        if x is None or y is None or abs(x) > MAX_SCAN_XY or abs(y) > MAX_SCAN_XY:
            raise MachineError(f"Scan move: X and Y must be numbers from -{MAX_SCAN_XY:g} to {MAX_SCAN_XY:g} in")
        if limit is None or limit <= 0:
            raise MachineError("Scan move: the time limit must be a number of seconds above 0")
        text = f"G20 G90 G0 X{fixed(x)} Y{fixed(y)}"
        with self.lock:
            link = self.live()
            now = self.clock()
            if self.activity != "scan":
                raise MachineError("No scan is running", 409)
            why = "Stopped" if self.activity_stop else None
            why = why or link.state_why(now, ("Idle",)) or link.line_why(True)
            if not why and link.jogging():
                why = "Jogging: wait until it has stopped"
            if why:
                raise MachineError(why, 409)
            line = Line(text, tag="scan")
            link.queue.append(line)
            resets, end = link.resets, now + limit
            link.wake()
            while True:
                why = self.scan_why(link, line, resets, text)
                if why is None and self.clock() >= end:
                    why = f"The machine did not get to X{fixed(x)} Y{fixed(y)} within {limit:g} s"
                if why:
                    if line in link.queue:
                        link.queue.remove(line)
                    raise MachineError(why, 409)
                st = link.status
                if (line.reply == "ok" and st and st["state"] == "Idle" and link.status_at >= line.done_at + SETTLE
                        and st["wpos"] and abs(st["wpos"][0] - x) <= 0.002 and abs(st["wpos"][1] - y) <= 0.002):
                    link.moved_at = -1e9    # (it got there and stands still: Stop has no move to hold for it)
                    return {"x": st["wpos"][0], "y": st["wpos"][1], "z": st["wpos"][2]}
                self.changed.wait(0.05)

    def scan_why(self, link, line, resets, text):
        """Why a scan move can't go on (lock held), or None."""
        if self.link is not link or not link.connected or link.stopping:
            return "Disconnected during the scan"
        if self.activity_stop:
            return "Stopped"
        if self.activity != "scan":
            return "The scan has ended"
        if link.alarm:
            return f"{explain(link.alarm['text'])} during the scan"
        if link.resets != resets or line.reply == "dropped":
            return "FluidNC was reset during the scan"
        if line.reply not in (None, "ok"):
            return f"FluidNC answered {explain(line.reply)} to {text}"
        return None

    def check_port(self, path):
        """The probe's or the pendant's port, if this server may open it (in test mode only the test ports)."""
        path = path.strip() if isinstance(path, str) else ""
        if not path:
            raise MachineError("No port given")
        if self.open_only_extra and os.path.realpath(path) not in {os.path.realpath(p) for p in self.extra}:
            raise MachineError(f"{path} is not one of the test ports this server may open")
        return path

    # -------------------------------------------------- port closed / server stopping

    def link_closed(self, link):
        with self.lock:
            if self.link is link:
                self.link = None
            message, kind = link.stopping or ("", "info")
            self.changed.notify_all()       # (a scan move waiting gives up)
            why = "disconnected" if kind == "info" else "the connection was lost"
            if link.connected and message:
                self.log(kind, message)
                link.end_jogs(why)
            if link.connected and link.job_active():    # (nothing more of it can go)
                link.end_job(self.clock(), "failed", why, message or why)
            if link.connected and message:
                link.set_alarm(None)            # (Undo Zero's records stay: FluidNC keeps the offsets)
                if link.axes != _no_axes() or link.planner_blocks:
                    self.emit({"type": "axes", "axes": _no_axes(), "plannerBlocks": None})
                self.emit({"type": "conn", "connected": False, "port": None, "firmware": None, "message": message})

    def shutdown(self):
        """Stops a scan, lets go of the pendant and probe ports, closes FluidNC's port (after the safe stop when
        a job, a scan or a move is under way: see disconnect), then ends the event streams once they have sent
        what is waiting (up to 1 s)."""
        if self.activity == "scan" and self.scanner is not None:
            try:                            # (stops it, and waits a little for its thread)
                getattr(self.scanner, "shutdown", self.scanner.stop)()
            except Exception:
                traceback.print_exc()
        for extra in (self.pendant, self.probe):
            if extra is not None:
                try:
                    extra.disconnect()
                except Exception:
                    traceback.print_exc()
        self.disconnect()
        with self.lock:
            subs = list(self.subs)
        for sub in subs:
            sub.close()
        end = time.monotonic() + 1.0
        while self.subs and time.monotonic() < end:     # each stream unsubscribes when it is done
            time.sleep(0.02)
