"""
The Bluetooth jog pendant for FluidSender: the user's NANO_jogbox (an ESP32 with a 28-key pad and a small
screen, behind a Bluetooth serial module), driven the way the user's UGS plugin (serialControl.java) drives it.
Python standard library only.

Its port (/dev/rfcomm1, made by Blueman) is opened only by connect() (Connect Pendant), never by itself: 9600 8N1
raw, held exclusively (TIOCEXCL) like the FluidNC port. A port another program has open is refused (UGS's plugin
may have it), except by blueman-rfcomm-watcher: Blueman holds the port open with it to keep the Bluetooth link,
and never reads it. That fd is never touched; Disconnect only closes ours.

The protocol. The pendant's lines end in "\\r\\n":
    CONREQ   every 1 s until it reads the line "CONACK". The firmware compares the whole line, so the answer is
             exactly b"CONACK\\n" (no "\\r"), and it is given every time: the pendant asks again whenever it has
             lost the link (UGS answers only the first one).
    REQ      every 250 ms once connected (every 1 s while its screen says a job runs), and only once the SYNC for
             the last one has come: answered with a SYNC.
    KEY:<n>  a key, then REQ 30 ms later: the key is done (see below), then a SYNC goes out.
    FW:2     the fixed firmware, right after CONACK (the original sends no such line; UGS ignores it).
A SYNC is 44 bytes: b"SYNC" and 10 little-endian float32: x, y, z, a (work position in inches; a = 0), status
(what the screen should say: below), the jog speed (in/min), job lines answered, job lines, the jog step (in)
and the feed FluidNC reports (in/min). The firmware reads 44 bytes at a time and checks only the first four, so
one stray byte would garble every packet after it: nothing but CONACK and SYNCs, each in one piece, is ever
written to it (a SYNC is skipped while the last one hasn't gone out yet, as UGS does). Without a SYNC for 3 s it
goes back to CONREQ by itself.
Here the pendant is "connected" from the first CONREQ answered, and "waiting" again (the port stays open) when
no REQ has come for 10 s. While waiting, its REQs and keys get no answer (a key that comes before it has asked to
connect may be an old one), so if it still thinks it is connected it soon gives up and asks again.

The status (screen_status): 0 IDLE, 1 PAUSED (a job held or at an M0, or FluidNC in Hold, or at a safety door
closed again: Door:0), 2 RUNNING (a job), 3 BUSY (anything else), and FluidSender's own 4 ALARM, 5 STOPPING (the
safe stop, until FluidNC's reset banner and a report after it), 6 ERROR (a job paused on an error or on no answer:
it is told on the page), 7 OFFLINE (no FluidNC, or no answer), 8 JOG, 9 HOMING, 10 SCAN, 11 MOVING (a move outside
a job, also one just sent while FluidNC still says Idle), 12 DOOR (open), 13 LIMIT (a critical alarm: a hard or
soft limit, which only a reset ends). The original firmware shows IDLE, HOLD, RUN,
and BUSY for 3 and up; the fixed one (NANO_jogbox.ino of 2026-10) has a name for each. Both ask every 1 s in
RUNNING, else every 250 ms. Each code goes as code + 0.5 (MARK): both firmwares keep the whole number, and UGS
sends whole numbers only, so the fixed firmware knows FluidSender by the half. With UGS it keeps the original
firmware's Run flag (Run once until Stop): UGS says IDLE in the middle of its own job whenever FluidNC reports Idle,
and its Run (GUIBackend.send) then starts the file again from line 1.

Keys, as the user's UGS maps them (serialControl.java handleCode: 1 A-, 2 A+, 3 Z+; the firmware's own table
says 2 is Z+, a slip). The firmware makes 12-19, 22 and 23 double-tap, and repeats a held jog key every 600 ms:
    0 Z-  3 Z+  4 Y-  5 X+  6 Y+  7 X-   a step jog with the shared jog step (1 in at most: the page's 10 in step is
                                         too big for a key that repeats while held) and speed; refused while 2 step
                                         jogs are still waiting or moving, so a held key never stacks up moves: it
                                         runs at most one step ahead of the machine
    8 9 10 11                            jog step 0.001, 0.01, 0.1, 1 in (the shared jog settings)
    13 14 15                             Zero Z, Y, X (work zero). The console says the work position before it, and
                                         the page's Undo Zero puts it back (the last 5 zeros, also after a reset):
                                         the second chance for a key pressed by accident
    17 18 19                             G53 G0 Z0, Y0, X0: to machine zero (UGS calls these Home Z/Y/X). 18 and 19
                                         rapid at the Z the bit is at, so they are refused while the work Z is
                                         below SAFE_Z (0.2 in, Go to XY 0's lift height): lift Z first (17)
    22 Return to zero (X Y, then Z)      23 Home all ($H): only in ALARM (a stray double-tap must not send the
                                         machine homing; the page's Home asks first)
    25 Run: a paused job, or FluidNC in Hold (or at a safety door closed again), resumes (the page's Resume); a
       job or a scan running: nothing to do (but from then on the original firmware sends its Pause and Stop: see
       below); else the loaded program runs (the page's Run; the console names it)
    26 Pause: a job running (or a move, a jog, also one just sent) holds. A paused job (or a Hold) resumes only as
       the original firmware's toggle: after its own Pause, or once its screen has shown PAUSED for SEEN (else
       the press was meant to hold, seeing RUNNING: it holds); and a Resume still waiting for FluidNC to stop is
       cancelled. The fixed firmware's Pause only holds (its Run resumes)
    27 Stop (a scan, a job (the safe stop) or a jog); in a critical alarm (LIMIT) the Soft Reset, the only way out
    Neither 25 nor 26 resumes a job that an error (or no answer from FluidNC) paused: what went wrong is told on
    the page, so it is resumed there; nor into the safe stop or its reset. Stop works.
    1 2 12 16                            A-, A+, Zero A, Home A: no A axis here, so only a note
Each key is the Machine call the page's button makes, so it is refused, with the reason in the console, exactly
when the button would be (and 18, 19, 23 and a resume after an error also as above), and a refused key is never
done later. Keys run on their own thread, one at a time (the reader keeps answering REQ meanwhile); a key that
comes while the last one is still being done is refused, but a Stop or a Pause: it is done right after that one
(one of them at most: a Stop replaces a Pause waiting), so a Stop read with the jog key before it acts after it.

When 25-27 are sent is the firmware's business. The original NANO_jogbox.ino keeps a Run flag of its own, set by
its Run key and cleared by its Stop key, and sends Run only while it is off, Pause and Stop only while it is on.
So Pause and Stop do nothing for a job or a scan started on the page until Run is pressed on the pendant, and
after a job Run does nothing until Stop (a refused Run says so, and so does the end of a job). Its double-tap
has no time limit. The fixed firmware goes by the status in the last SYNC (if it is fresh: at most 2.5 s old):
Stop always, Pause unless it says IDLE or (from FluidSender, the + 0.5) PAUSED, Run in IDLE with a job loaded
(the SYNC's total, the loaded program's lines: 1 or more) and, from FluidSender, in PAUSED, where FluidSender
resumes. It takes a second tap only 0.12 to 1 s after the first (sooner is contact bounce), and any other key
cancels a first tap. (With UGS's whole-number statuses it also keeps the original Run flag and more: see the
firmware.) It says FW:2 after each CONACK; the original firmware shows in its keys too (_follow): it never sends
Run twice without a Stop between, nor Pause or Stop after a Stop. state()["stopPause"]: whether the firmware
sends its Pause and Stop keys now (True, False, or None: not known yet).
"""

import errno
import fcntl
import math
import os
import re
import select
import struct
import termios
import threading
import time
import traceback
from collections import namedtuple

from job import ACTIVE
from machine import (BAUDS, NO_RESUME, SAFE_Z, SETTLE, MachineError, can_move, close_port, fixed, holders_text,
                     open_port, port_kind, process_name, scan_holders)

BAUD = 9600
REQ_TIMEOUT = 10.0      # no REQ for this long: "waiting" (UGS's timeout; the pendant connects again by itself)
TICK = 0.1              # the reader checks that timer at least this often
WRITE_TIMEOUT = 0.5     # a packet the port takes no more of within this is dropped (the pendant asks again)
NOTE_EVERY = 10.0       # an unreadable line is told in the console at most this often
MAX_RX = 1024           # this many bytes without a line end: noise, dropped
DEFAULT_PORT = "/dev/rfcomm1"   # where Blueman puts the pendant
CONACK = b"CONACK\n"
KEY = re.compile(r"KEY:(\d{1,3})")
TRIM = bytes(range(33))         # what Java's trim() takes off a line (UGS reads the lines that way)
BLUEMAN = ("blueman-rfcomm-watcher (", "blueman-rfcomm- (")   # process_name(): its name, or the kernel's 15 letters

JOG_KEYS = {0: ("Z-", "z", -1), 3: ("Z+", "z", 1), 4: ("Y-", "y", -1), 5: ("X+", "x", 1), 6: ("Y+", "y", 1),
            7: ("X-", "x", -1)}
STEP_KEYS = {8: 0.001, 9: 0.01, 10: 0.1, 11: 1.0}
ZERO_KEYS = {13: "Z", 14: "Y", 15: "X"}
GOTO_KEYS = {17: ("mz", "Machine Z0 (G53 G0 Z0)"), 18: ("my", "Machine Y0 (G53 G0 Y0)"),
             19: ("mx", "Machine X0 (G53 G0 X0)"), 22: ("zero", "Return to zero")}
A_KEYS = {1: "A-", 2: "A+", 12: "Zero A", 16: "Home A"}
NAMES = {23: "Home all ($H)", 25: "Run", 26: "Pause/Resume", 27: "Stop"}
PAUSE_DID = {"hold": "Pause (feed hold)", "resume": "Resume"}      # Machine.pause_toggle()'s "did"
STOPPED = {"scan": "scan stopped", "job": "stopping the job (feed hold, then a reset once it stands still)",
           "jog": "jog cancelled", "motion": "stopping (feed hold, then a reset once it stands still)",
           None: "nothing to stop"}                                 # Machine.stop()'s "stopped"
RESET_NOW = {"job": "stopping the job (Soft Reset at once: FluidNC is in ALARM or homing, where a feed hold does "
                    "nothing)",
             "motion": "Soft Reset at once (FluidNC is in ALARM or homing, where a feed hold does nothing)"}
LIMIT_RESET = ("Soft Reset (FluidNC was in a critical alarm, a hard or soft limit: only a reset gets out of it. "
               "Then Home All if it says ALARM)")
RUN_AGAIN = ". The pendant sends Run again only after its Stop key"     # (the original firmware's Run flag)
RUN_KEYS = (25, 26, 27)     # Run, Pause, Stop: sent when the firmware's own rule says (see _follow)
AGAIN = ("Pendant: the job is over. If its Run key does nothing now, press its Stop key first, then Run (the "
         "original pendant firmware sends Run again only after Stop)")
FIXED = ("Pendant: its firmware sends Run, Pause and Stop by the status it shows (the fixed firmware): Stop always "
         "reaches FluidSender, Pause holds (and isn't sent while it shows IDLE or PAUSED), Run starts the job or "
         "resumes it")
FW = re.compile(r"FW:(\d{1,3})")    # the fixed firmware's line after CONACK: its version (2 and up: the fixed one)
SEEN = 0.3              # s: a SYNC sent less than this before a key wasn't on the pendant's screen when it was
                        # pressed (44 bytes at 9600 baud, Bluetooth, the redraw, and a person's reaction)
ON_ERROR = "The job stopped on an error ({}). See it on the page, then Resume there to go on, or Stop"
MAX_STEP = 1.0          # in: the biggest jog step a key moves (the pendant's own step keys go up to 1 in)
STEPS_AHEAD = 2         # a jog key is refused while this many step jogs are still waiting or moving
HOME_ONLY_IN_ALARM = "FluidNC isn't in ALARM. Use Home on the page if you mean it."
LOW_Z_KEYS = ("mx", "my")   # G53 G0 X0 / Y0: a rapid across the table at the Z the bit is at

# The status in the SYNC: what the pendant's screen should say (screen_status). The original firmware shows 0 IDLE,
# 1 HOLD, 2 RUN and BUSY for any other code; the fixed one has a name for each of these
IDLE, PAUSED, RUNNING, BUSY, ALARM, STOPPING, ERROR, OFFLINE, JOG, HOMING, SCAN, MOVING, DOOR, LIMIT = range(14)
MARK = 0.5              # added to every code in the SYNC: FluidSender's, not UGS's (it sends 0-3 as whole numbers)
SCREEN = ("IDLE", "PAUSED", "RUNNING", "BUSY", "ALARM", "STOPPING", "ERROR", "OFFLINE", "JOG", "HOMING", "SCAN",
          "MOVING", "DOOR", "LIMIT")
STATES = {"Idle": IDLE, "Hold": PAUSED, "Jog": JOG, "Home": HOMING, "Run": MOVING}     # FluidNC's, with no job

View = namedtuple("View", "state job error name scan stopping settling sub critical moving")    # see machine_view()


def key_name(key):
    """5 -> 'Jog X+', 8 -> 'Jog step 0.001', 13 -> 'Zero Z', 22 -> 'Return to zero', 99 -> 'Key 99'"""
    if key in JOG_KEYS:
        return "Jog " + JOG_KEYS[key][0]
    if key in STEP_KEYS:
        return "Jog step " + fixed(STEP_KEYS[key])
    if key in ZERO_KEYS:
        return "Zero " + ZERO_KEYS[key]
    if key in GOTO_KEYS:
        return GOTO_KEYS[key][1]
    return A_KEYS.get(key) or NAMES.get(key) or f"Key {key}"


def _f32(v):
    """A number for the packet: NaN, infinity, None, text and numbers too big for a float32 are 0 (as in UGS)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    try:
        struct.pack("<f", v)
    except OverflowError:
        return 0.0
    return float(v) if math.isfinite(v) else 0.0


def sync_packet(st):
    """The 44-byte SYNC from pendant_status()'s dict: b"SYNC" + x y z a status feed line total step live, each a
    little-endian float32 (a is 0: no A axis). A field left out is 0, and a status that isn't one of the screen's
    (0-13) is BUSY: the pendant then never says IDLE by mistake. The status goes as code + MARK."""
    st = st if isinstance(st, dict) else {}
    status = st.get("status")
    status = status if not isinstance(status, bool) and status in range(len(SCREEN)) else BUSY
    vals = (st.get("x"), st.get("y"), st.get("z"), 0.0, status + MARK, st.get("feed"), st.get("line"), st.get("total"),
            st.get("step"), st.get("live"))
    return b"SYNC" + struct.pack("<10f", *map(_f32, vals))


def machine_view(m):
    """What the keys and the screen go by, as a View (call it with the Machine's lock held: one moment): state,
    FluidNC's ("Idle", "Run", "Hold", "Jog", "Alarm", "Home", "Door" ..., "No response"; None: not connected), and
    sub, its sub-state ("0" in "Door:0": the door closed again, a cycle start goes on); job, the state of the job
    if one is active ("running", "paused" or "stopping"), with the error that paused it and its name; scan, a scan
    running; stopping, the safe stop under way, or its reset until FluidNC's banner; settling, FluidNC's banner
    has come but no report a moment after it yet (the last one, from before the reset, may still say Hold);
    critical, FluidNC is in a critical alarm (a hard or soft limit: only a reset gets out of it); moving, FluidNC
    says Idle but a move of ours was just sent (see just_sent). Machine.pendant_status() says only 0-3, so these
    come from the Machine's own fields (link.job, link.halt, link.reset_at, link.after_reset, link.reset_clean,
    link.alarm, activity, and just_sent's), with defaults."""
    st = m.status_now()
    state = st.get("state") if st else None
    link = getattr(m, "link", None) if st else None
    job = getattr(link, "job", None)
    if getattr(job, "state", None) not in ACTIVE:
        job = None
    stopping = link is not None and (getattr(link, "halt", None) is not None
                                     or getattr(link, "reset_at", None) is not None)
    settling = getattr(link, "after_reset", None) is not None and not getattr(link, "reset_clean", True)
    alarm = getattr(link, "alarm", None)
    return View(state, getattr(job, "state", None), getattr(job, "error", None), getattr(job, "name", None),
                getattr(m, "activity", None) == "scan", stopping, settling, st.get("sub") if st else None,
                isinstance(alarm, dict) and bool(alarm.get("critical")),
                state == "Idle" and link is not None and just_sent(link))


def just_sent(link):
    """A move of ours (a Go to, a jog, a typed G0: machine.can_move) is in FluidNC's hands or waits to go, or its
    ok has come and no report made SETTLE after it says otherwise yet (FluidNC starts it just after the ok, so
    the last report may still say Idle). Lock held. Tighter than Link.may_move, which takes MOVE_SETTLE for
    Stop's sake: the screen would say MOVING after every job's end and every reset."""
    lines = (getattr(link, "inflight", None), getattr(link, "owed", None), *getattr(link, "queue", ()))
    if any(line is not None and can_move(line) for line in lines):
        return True
    times = (getattr(link, "moved_at", None), getattr(link, "status_at", None))
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in times):
        return False
    return times[1] < times[0] + SETTLE


def screen_status(v):
    """The SYNC's status for a View: what the pendant's screen should say."""
    if v.state in (None, "No response"):
        return OFFLINE
    if v.stopping or v.job == "stopping":
        return STOPPING
    if v.state == "Alarm":
        return LIMIT if v.critical else ALARM
    if v.state == "Door" and v.sub != "0":  # (Door:0: closed again, ready to go on: PAUSED below)
        return DOOR
    if v.settling:                          # (still the stop: what FluidNC does after its reset isn't known yet)
        return STOPPING
    if v.job == "paused":
        return ERROR if v.error else PAUSED
    if v.job == "running":
        return RUNNING
    if v.scan:
        return SCAN
    if v.state == "Door":                   # (Door:0 with no job: a cycle start goes on, as from a Hold)
        return PAUSED
    if v.state == "Idle" and v.moving:      # (a Go to or a jog just sent: Pause holds it, Stop stops it)
        return MOVING
    return STATES.get(v.state, BUSY)


def is_blueman_watcher(pid):
    """Blueman's blueman-rfcomm-watcher: it holds the rfcomm port open so the Bluetooth link stays up, and never
    reads it, so the pendant can share the port with it."""
    return process_name(pid).startswith(BLUEMAN)


def other_programs(real):
    """'UGS (java, pid 4242)' if another program has the port open, else ''. Blueman's rfcomm watcher doesn't count."""
    pids = [p for p in scan_holders({real}).get(real, []) if p != os.getpid()]
    return holders_text([p for p in pids if not is_blueman_watcher(p)])


def _waiting_out(fd):
    """Bytes written to the port that haven't gone out yet (0 if the port can't say)."""
    try:
        return struct.unpack("i", fcntl.ioctl(fd, termios.TIOCOUTQ, b"\0" * 4))[0]
    except OSError:
        return 0


class Session:
    """One open pendant port: its fd, and the reader thread with its wake-up pipe and line buffer."""

    def __init__(self, path, fd):
        self.path = path
        self.fd = fd
        self.wlock = threading.Lock()       # guards writes and closing the fds: a packet goes out whole
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.thread = None
        self.stopping = None                # (message, console kind) once closing
        self.rx = b""
        self.noted = -1e9                   # when an unreadable line was last told
        self.status_failed = False          # pendant_status() raised something else than MachineError (printed once)

    def wake(self):
        with self.wlock:
            if self.wake_w is not None:
                try:
                    os.write(self.wake_w, b"x")
                except BlockingIOError:
                    pass

    def send(self, data, skip_busy=False):
        """Writes all of data; True if it went. False if the port is closed, if skip_busy and the last packet
        hasn't gone out yet (as UGS does: the pendant gets that one), or if the port takes no more within
        WRITE_TIMEOUT. Raises OSError if the port is gone."""
        end = time.monotonic() + WRITE_TIMEOUT
        with self.wlock:
            if self.fd is None:
                return False
            if skip_busy and _waiting_out(self.fd):
                return False
            while data:
                try:
                    n = os.write(self.fd, data)
                except BlockingIOError:
                    n = 0
                data = data[n:]
                if data:
                    if time.monotonic() > end:
                        return False        # (a packet cut short garbles the ones after it, until the pendant
                                            # gives up and connects again by itself)
                    time.sleep(0.005)
        return True


class PendantLink:
    """The pendant, shared by every open page. Opens a port only in connect(). Its state is guarded by the
    Machine's lock (so a snapshot sees it whole); nothing is written to the port while that lock is held, and the
    Machine's calls for the keys are made without it (but Home and Machine X0/Y0, which look at FluidNC's state
    or Z first: the check and the call, both quick, are one step under it)."""

    def __init__(self, machine, settings=None):
        self.machine = machine
        self.settings = settings            # remembers "pendantPort", the last port connected
        self.lock = machine.lock
        self.session = None                 # the open port (Session), or None
        self.connecting = False
        self.status = "off"                 # 'off' (no port open), 'waiting' or 'connected'
        self.since = time.time()            # when status last changed
        self.last_key = None                # {'key', 'name', 'ok' (True done, False refused, None nothing to do),
                                            #  'text' (the console line), 't'}
        self.heard = 0.0                    # (monotonic) the last REQ or key, or the CONREQ that connected it
        self.live = False                   # a REQ or key has come since the last CONREQ
        self.busy = None                    # the key being done (kept over a reconnect: the Machine is still at it)
        self.pending = None                 # a Stop or Pause that came meanwhile: done right after it (see _key)
        self.run_flag = None                # the original firmware's Run flag as its keys show it (see _follow):
                                            # None, not known (since connecting, or since it last asked to connect)
        self.fixed = False                  # the fixed firmware: it said FW:2, or its keys broke the original rule
        self.job_on = False                 # a job was active at the last SYNC (see _numbers)
        self.shown = None                   # the status in the last SYNC made (what its screen shows) ...
        self.shown_since = 0.0              # ... and since when (monotonic) it has said that
        self.pause_hold = False             # its Pause key made the hold that the SYNCs have shown since (see
                                            # _no_resume: the original firmware's Pause toggles)

    # -------------------------------------------------- calls from the page

    def state(self):
        with self.lock:
            s = self.session
            return {"port": s.path if s else None, "state": self.status, "since": round(self.since, 3),
                    "lastKey": self.last_key, "stopPause": self._stop_pause()}

    def ports(self):
        """machine.ports() for the Connect Pendant dialog: Blueman's rfcomm watcher doesn't count as a user, and
        the default is the last pendant port if it is listed, else /dev/rfcomm1, else the first Bluetooth port."""
        data = self.machine.ports()
        ports = data["ports"]
        with self.lock:
            mine = self.session.path if self.session else None
        holders = scan_holders({os.path.realpath(p["path"]) for p in ports})
        for p in ports:
            pids = [pid for pid in holders.get(os.path.realpath(p["path"]), []) if pid != os.getpid()]
            if p["path"] == mine:
                p["inUseBy"] = "FluidSender (pendant connected)"
            elif pids and all(is_blueman_watcher(pid) for pid in pids):
                p["inUseBy"] = None
        paths = [p["path"] for p in ports]
        last = self._remembered()
        data["default"] = next((p for p in (last, DEFAULT_PORT) if p and p in paths), None) or next(
            (p["path"] for p in ports if p["kind"] == "bluetooth"), None)
        return data

    def connect(self, port, force=False):
        """Opens the port and listens: 'waiting' until the pendant asks to connect. force: a USB ACM port too
        (that is the probe's kind); a USB serial port is FluidNC's kind and never opened here."""
        path = self.machine.check_port(port)            # (in test mode only the test ports)
        with self.lock:
            if self.session:
                raise MachineError(f"The pendant is already connected on {self.session.path}. Disconnect it first.",
                                   409)
            if self.connecting:
                raise MachineError("Already connecting the pendant", 409)
            self.connecting = True
        try:
            fd = self._open(path, bool(force))
            try:
                s = Session(path, fd)
            except OSError as e:
                close_port(fd)
                raise MachineError(f"Could not start the pendant's reader ({e.strerror or e})") from None
            s.thread = threading.Thread(target=self._run, args=(s,), name=f"pendant {path}", daemon=True)
            with self.lock:
                self.session = s
                self.live = False
                self.run_flag, self.fixed, self.job_on = None, False, False     # (maybe another pendant now)
                self.shown, self.pause_hold = None, False
                self._set("waiting")
                self.machine.log("info", f"Pendant: listening on {path}; waiting for the pendant to connect")
            s.thread.start()
        finally:
            with self.lock:
                self.connecting = False
        self._remember(path)
        return {"pendant": self.state()}

    def disconnect(self):
        """Closes our fd (and nothing else: Blueman keeps its own). Waits for it unless called from the reader."""
        with self.lock:
            s = self.session
            if s and not s.stopping:
                s.stopping = (f"Pendant: disconnected from {s.path}", "info")
        if s:
            s.wake()
            if s.thread.is_alive() and threading.current_thread() is not s.thread:
                s.thread.join(WRITE_TIMEOUT + 2)
        return {}

    def _open(self, path, force):
        m = self.machine
        real = os.path.realpath(path)
        if real not in {os.path.realpath(p) for p in m.extra}:
            kind = port_kind(real)
            if kind == "usb-serial":        # (never, even with force: opening it can reset FluidNC's ESP32)
                raise MachineError(f"{path} is a USB serial port like FluidNC's, not the pendant (Bluetooth, "
                                   "/dev/rfcomm...). Not connected.")
            if kind not in ("bluetooth", "usb-acm"):
                raise MachineError(f"{path} is not a Bluetooth serial port like the pendant's (/dev/rfcomm...)")
            if kind == "usb-acm" and not force:
                raise MachineError(f"{path} looks like the Z probe (Arduino Leonardo), not the pendant. "
                                   "Not connected.")
        if not os.path.exists(real):
            raise MachineError(f"{path} is not there. Is the pendant on, and its serial port connected in Blueman?")
        link = m.link
        if link is not None and os.path.realpath(link.path) == real:
            raise MachineError(f"{path} is the FluidNC port", 409)

        def in_use(others):
            return MachineError(f"Port {path} is in use by {others}. Disconnect it there first.", 409)
        others = other_programs(real)
        if others:
            raise in_use(others)
        fd = open_port(real, BAUDS[BAUD])
        others = other_programs(real)       # someone may have opened it just before we locked it
        if others:
            close_port(fd)
            raise in_use(others)
        return fd

    def _remembered(self):
        try:
            port = self.settings.get("pendantPort") if self.settings is not None else None
        except Exception:                   # (a convenience only: never in the way)
            traceback.print_exc()
            return None
        return port if isinstance(port, str) and port else None

    def _remember(self, path):
        try:
            if self.settings is not None and self._remembered() != path:
                self.settings.set("pendantPort", path)
        except Exception:
            traceback.print_exc()

    # -------------------------------------------------- state (lock held)

    def _set(self, status):
        if status != self.status:
            self.status = status
            self.since = time.time()
        self._emit()

    def _emit(self):
        self.machine.emit({"type": "pendant", "pendant": self.state()})

    def _stop_pause(self):
        """Whether the pendant's firmware sends its Pause and Stop keys now: True (the fixed firmware, or the
        original one with its Run flag set), False (the original one after its Stop key), None (not known)."""
        if self.fixed or self.run_flag:
            return True
        return False if self.run_flag is False else None

    def _follow(self, key):
        """Keys 25-27 against the original firmware's rule: it sends Run only while its own Run flag is off (and
        sets it), Pause and Stop only while it is on (Stop clears it). A key against that rule: the fixed
        firmware, which sends them by the status it shows."""
        if self.fixed:
            return
        if (key == 25 and self.run_flag is True) or (key != 25 and self.run_flag is False):
            self.fixed, self.run_flag = True, None
            self.machine.log("info", FIXED)
        else:
            self.run_flag = key != 27

    # -------------------------------------------------- the reader thread

    def _run(self, s):
        try:
            while True:
                with self.lock:
                    if s.stopping:
                        break
                    self._tick(s, time.monotonic())
                ready = select.select([s.fd, s.wake_r], [], [], TICK)[0]
                if s.wake_r in ready:
                    try:
                        os.read(s.wake_r, 512)
                    except BlockingIOError:
                        pass
                if s.fd in ready:
                    try:
                        data = os.read(s.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not data:
                        raise OSError(errno.EIO, "the port was closed")
                    self._received(s, data)
        except OSError as e:
            with self.lock:
                if not s.stopping:
                    s.stopping = (f"Pendant: lost {s.path} ({e.strerror or e}). Bluetooth link dropped? "
                                  "Connect it again.", "error")
        except Exception as e:              # a bug: close the port and say so, rather than leave the pendant hanging
            traceback.print_exc()
            with self.lock:
                if not s.stopping:
                    s.stopping = (f"Pendant: closed {s.path} after an internal error in FluidSender ({e!r}). "
                                  "Connect it again.", "error")
        finally:
            self._close(s)

    def _close(self, s):
        with s.wlock:
            fd, s.fd = s.fd, None
            wake, s.wake_r, s.wake_w = (s.wake_r, s.wake_w), None, None
        if fd is not None:
            close_port(fd)
        for w in wake:
            if w is not None:
                os.close(w)
        with self.lock:
            message, kind = s.stopping or ("", "info")
            if self.session is s:
                self.session = None
                self._set("off")
            if message:
                self.machine.log(kind, message)

    def _tick(self, s, now):
        if self.session is s and self.status == "connected" and now - self.heard > REQ_TIMEOUT:
            self._set("waiting")
            self.machine.log("info", f"Pendant: no word from it for {REQ_TIMEOUT:g} s (switched off, or out of "
                                     f"range?). Still listening on {s.path}: it connects again by itself.")

    def _received(self, s, data):
        s.rx += data
        lines = s.rx.split(b"\n")
        s.rx = lines.pop()
        if len(s.rx) > MAX_RX:
            s.rx = b""
            lines.append(b"(noise without line ends)")
        for raw in lines:
            text = raw.strip(TRIM).decode("ascii", "replace")
            now = time.monotonic()
            if text == "CONREQ":
                self._conreq(s, now)
            elif text == "REQ":
                self._req(s, now)
            elif KEY.fullmatch(text):
                self._key(s, int(text[4:]), now)
            elif FW.fullmatch(text):
                self._fw(s, int(text[3:]))
            elif text:
                self._unreadable(s, text, now)

    def _conreq(self, s, now):
        s.send(CONACK)                      # every time: it asks again whenever it has lost the link
        with self.lock:
            if self.session is not s:
                return
            again = self.status == "connected" and self.live
            self.heard = now
            self.live = False
            known = self._stop_pause()
            self.run_flag = None            # (it may have been switched off and on: its Run flag off again)
            self.shown, self.pause_hold = None, False   # (its screen waits for the next SYNC)
            if self.status != "connected":
                self._set("connected")
                self.machine.log("info", f"Pendant: connected on {s.path}")
            else:
                if again:
                    self.machine.log("info", "Pendant: connected again (it had heard nothing for 3 s, or was "
                                             "switched off and on)")
                if known != self._stop_pause():
                    self._emit()

    def _req(self, s, now):
        with self.lock:
            if self.session is not s or self.status != "connected":
                return                      # (waiting: it gives up and asks to connect again)
            self.heard = now
            self.live = True
        self._sync(s)

    def _key(self, s, key, now):
        with self.lock:
            if self.session is not s:
                return
            if self.status != "connected":
                self.machine.log("info", f"Pendant: {key_name(key)} ignored: the pendant hasn't asked to connect "
                                         "yet (it may be an old key)")
                return
            self.heard = now
            self.live = True
            if key in RUN_KEYS:
                self._follow(key)           # (the firmware sent it: its flag changed, whatever is done with it)
            busy, why = self.busy, None
            if busy is None:
                self.busy = key
            elif key == 27 and 27 in (busy, self.pending):
                why = (None, "Pendant: Stop: being done already")
            elif key == 27 or (key == 26 and self.pending is None and busy not in (26, 27)):
                if self.pending == 26:      # (a Stop replaces a Pause waiting: it holds too)
                    self.machine.log("info", "Pendant: Pause/Resume dropped: Stop came right after it")
                self.pending = key          # (two lines read at once can be a jog key and Stop: Stop then acts
                return                      # after the jog, never before it)
            else:
                why = (False, f"Pendant: {key_name(key)} refused: still doing {key_name(busy)}")
        if why is not None:
            self._result(key, *why)
            self._sync(s)
            return
        threading.Thread(target=self._do_key, args=(s, key), name=f"pendant key {key}", daemon=True).start()

    def _fw(self, s, version):
        """'FW:2', after CONACK: the fixed firmware says which it is (the original one sends no such line)."""
        with self.lock:
            if self.session is not s or version < 2 or self.fixed:
                return
            self.fixed, self.run_flag = True, None
            self.machine.log("info", FIXED)
            self._emit()

    def _unreadable(self, s, text, now):
        if now - s.noted >= NOTE_EVERY:
            s.noted = now
            self.machine.log("info", f"Pendant: unreadable line from {s.path}: {text[:40]!r}")

    def _sync(self, s, quiet=False):
        """A SYNC with the machine's state now. quiet: the port being gone is the reader's business."""
        try:
            st = self._numbers()
        except Exception as e:              # never leave the pendant without an answer: it would give up on us
            if not isinstance(e, MachineError) and not s.status_failed:
                s.status_failed = True
                traceback.print_exc()
            st = {"status": BUSY}
        try:
            s.send(sync_packet(st), skip_busy=True)
        except OSError:
            if not quiet:
                raise

    def _numbers(self):
        """pendant_status()'s numbers for the SYNC, with the status the screen should say; once a job is over,
        the note that the original firmware's Run needs its Stop key first (if its Run flag is set)."""
        m = self.machine
        with self.lock:                     # (the Machine's: one moment)
            st = dict(m.pendant_status() or {})
            v = machine_view(m)
            st["status"] = screen_status(v)
            on = v.job is not None
            if self.job_on and not on and self.run_flag and not self.fixed:
                m.log("info", AGAIN)
            self.job_on = on
            if st["status"] != self.shown:
                self.shown, self.shown_since = st["status"], time.monotonic()
            if st["status"] != PAUSED:
                self.pause_hold = False     # (no longer the hold its Pause made: resumed, stopped, ...)
        return st

    # -------------------------------------------------- keys (each on its own thread)

    def _do_key(self, s, key):
        """Does the key, then the Stop or Pause that came meanwhile (self.pending), each followed by a SYNC."""
        while key is not None:
            try:
                ok, text = self._action(key)
            except Exception as e:          # a bug, or a Machine call that doesn't fit: say so, never die
                traceback.print_exc()
                ok, text = False, f"Pendant: {key_name(key)} failed: internal error in FluidSender ({e!r})"
            try:
                self._result(key, ok, text)
            except Exception:
                traceback.print_exc()
            with self.lock:                 # (whatever happened: else every key after it would be refused)
                key, self.pending = self.pending, None
                self.busy = key
            self._sync(s, quiet=True)

    def _action(self, key):
        """Does one key with the Machine's calls; (ok, console line). ok: True done, False refused, None nothing
        to do. Refused calls queue nothing: the key is simply not done."""
        m = self.machine
        name = key_name(key)
        try:
            if key in JOG_KEYS:
                axis, sign = JOG_KEYS[key][1:]
                js = m.jog_settings()
                step = min(js["step"], MAX_STEP)
                m.jog({"step": {axis: sign * step}, "feed": js["feed"]}, max_steps=STEPS_AHEAD)
                if step < js["step"]:       # (said with every such key: the Jog panel's step is the one to change)
                    m.log("info", f"Pendant: step {fixed(js['step'])} in is too big for the pendant; "
                                  f"using {fixed(MAX_STEP)} in")
                return True, f"Pendant: {name} {fixed(step)} in"
            if key in STEP_KEYS:
                m.set_jog_settings(step=STEP_KEYS[key])
                return True, f"Pendant: {name} in"
            if key in ZERO_KEYS:
                m.zero(axes=ZERO_KEYS[key], value=0, source="pendant")
                return True, f"Pendant: {name}"
            if key in GOTO_KEYS:
                to = GOTO_KEYS[key][0]
                with m.lock:                # (checked and sent as one: the Z can't change in between)
                    if to in LOW_Z_KEYS:
                        self._clear_of_the_work(m.status_now())
                    m.goto(to=to)
                return True, f"Pendant: {name}"
            if key == 23:
                with m.lock:                # (likewise FluidNC's state)
                    st = m.status_now()
                    if st and st["state"] not in ("Alarm", "No response"):     # (else the Machine says why not)
                        raise MachineError(HOME_ONLY_IN_ALARM, 409)
                    m.home(axes="")
                return True, f"Pendant: {name}"
            if key == 25:
                return self._run_key(m)
            if key == 26:
                return self._pause_key(m)
            if key == 27:
                return self._stop_key(m)
        except MachineError as e:
            return False, f"Pendant: {name} refused: {e}{RUN_AGAIN if key == 25 and not self.fixed else ''}"
        if key in A_KEYS:
            return None, f"Pendant: {name} ignored: this machine has no A axis"
        return None, f"Pendant: key {key} does nothing"

    def _run_key(self, m):
        """Run (25): a paused job, or FluidNC in Hold, resumes (the page's Resume); a job or a scan running: nothing
        to do (the original firmware sends its Pause and Stop from now on); else the loaded program runs (the
        page's Run). The state is checked and the call made as one."""
        with m.lock:
            v = machine_view(m)
            if v.stopping or v.settling:    # (the safe stop's own Hold, or its reset: never a '~' then)
                raise MachineError(NO_RESUME, 409)
            if v.state == "Door" and v.sub != "0":      # (FluidNC goes on only once it is closed: Door:0)
                raise MachineError("The safety door is open: close it, then Run resumes", 409)
            if v.job == "paused" or (v.job is None and not v.scan and v.state in ("Hold", "Door")):
                self._no_resume_after_error(v)
                m.realtime("resume")
                if v.job:
                    return True, f"Pendant: Run: Resume {v.name}"
                where = "in Hold" if v.state == "Hold" else "held by the safety door, closed again"
                return True, f"Pendant: Run: Resume (FluidNC was {where})"
            if v.job == "running":
                return None, f"Pendant: Run: {v.name} is running already (the pendant's Pause and Stop work for it)"
            if v.job == "stopping":
                raise MachineError("The job is being stopped", 409)
            if v.scan:
                return None, "Pendant: Run: a scan is running (the pendant's Stop stops it)"
            job = (m.run() or {}).get("job") or {}
        n = job.get("total")
        if not isinstance(n, int) or not job.get("name"):
            return True, "Pendant: Run"
        return True, f"Pendant: Run {job['name']} ({n} line{'s' * (n != 1)})"

    def _pause_key(self, m):
        """Pause (26): a running job (or a move, a jog; also one just sent while FluidNC still says Idle) holds.
        A held one resumes (Machine.pause_toggle) only as the original firmware's toggle: its Pause key made that
        hold, or the pendant's screen has shown PAUSED for SEEN, and no Resume waits for FluidNC to stop (Pause
        then cancels it). Else it holds again (a '!' while FluidNC holds changes nothing): the press was meant to
        hold, seeing RUNNING (FluidNC held by itself: its button, an M0, the page). The fixed firmware's Pause
        only holds: Run resumes. Never during the safe stop or its reset, nor after an error. The state is checked
        and the call made as one."""
        now = time.monotonic()
        with m.lock:
            v = machine_view(m)
            if v.stopping or v.settling:    # (the safe stop's Hold, or its reset: a '~' would let a move run on)
                raise MachineError(NO_RESUME, 409)
            self._no_resume_after_error(v)
            held = v.job == "paused" or (v.job is None and v.state == "Hold")   # (pause_toggle would resume)
            sent_move = v.job is None and not v.scan and v.state == "Idle" and v.moving
            why = self._no_resume(m, v, now) if held else None
            if why or sent_move:
                m.realtime("hold")
                did = "hold"
            else:
                did = (m.pause_toggle() or {}).get("did")
            self.pause_hold = did == "hold"     # (the original firmware's next Pause is its toggle back)
        return True, "Pendant: " + PAUSE_DID.get(did, "Pause/Resume") + (f": {why}" if why else "")

    def _no_resume(self, m, v, now):
        """Why a Pause doesn't resume what is held now (and holds instead), or None: it is the toggle back."""
        what = v.name or "FluidNC"
        if getattr(getattr(m, "link", None), "resume_at", None) is not None:
            return "the Resume waiting for FluidNC to stop is cancelled"
        if v.state == "Door" and v.sub != "0":
            return f"{what} is held by the safety door; close it, then the pendant's Run resumes it"
        if self.fixed:
            return f"{what} was held already; the pendant's Run resumes it"
        if self.pause_hold or (self.shown == PAUSED and now - self.shown_since >= SEEN):
            return None
        return f"{what} was held already (the pendant didn't show it yet); Pause again resumes it"

    def _stop_key(self, m):
        """Stop (27): Machine.stop() (a scan, the safe stop of a job or a move, a jog cancel). In a critical alarm
        (a hard or soft limit) a Soft Reset first: nothing moves there, a feed hold would let FluidNC out of it
        unhomed, and only a reset gets out of it (then ALARM:14 if an axis must be homed: Home All)."""
        with m.lock:
            v = machine_view(m)
            reset = v.critical and not v.stopping
            if reset:
                m.realtime("reset")
        stopped = (m.stop() or {}).get("stopped")
        if reset:
            return True, "Pendant: Stop: " + LIMIT_RESET + ("; scan stopped" if stopped == "scan" else "")
        if stopped in RESET_NOW:
            with m.lock:                    # (the safe stop in ALARM or homing: the reset at once, with no hold
                link = getattr(m, "link", None)     # to wait for: Link.safe_stop. Else its halt waits for Hold:0)
                if link is not None and getattr(link, "halt", None) is None:
                    return True, "Pendant: Stop: " + RESET_NOW[stopped]
        return (None if stopped is None else True), "Pendant: Stop: " + STOPPED.get(stopped, "stopping")

    @staticmethod
    def _no_resume_after_error(v):
        """A job that an error (or no answer from FluidNC) paused is resumed on the page, where the error is told."""
        if v.job == "paused" and v.error:
            raise MachineError(ON_ERROR.format(v.error), 409)

    @staticmethod
    def _clear_of_the_work(st):
        """Keys 18 and 19 rapid across the table at the Z the bit is at: refused (MachineError) while the work Z is
        below SAFE_Z. Only in Idle: in any other state the Machine refuses the move itself, and says why."""
        if not st or st["state"] != "Idle":
            return
        wpos = st.get("wpos")
        if not wpos:
            raise MachineError("Work position not known yet; try again in a second", 409)
        z = round(wpos[2], 4) + 0.0         # (no "-0.0000")
        if z < SAFE_Z:
            raise MachineError(f"Z is at {z:.4f} in (work). Lift Z first (Machine Z0 key).", 409)

    def _result(self, key, ok, text):
        with self.lock:
            self.last_key = {"key": key, "name": key_name(key), "ok": ok, "text": text, "t": round(time.time(), 3)}
            self.machine.log("error" if ok is False else "info", text)
            self._emit()
