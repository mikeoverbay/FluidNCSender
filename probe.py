"""
The surface probe ("the nano": an Arduino Leonardo that drives its own Z) for FluidSender's surface scan.

Python standard library only. The port is opened only by connect() (the page's Connect button). Like
FluidNC's port it is held exclusively, and refused while another program (UGS) has it open; FluidNC's
kind of port (/dev/ttyUSB*) and the pendant's (/dev/rfcomm*) are never opened here. Nothing is sent when
it connects: the probe moves only when the user homes it or runs a scan.

The protocol, as the user's UGS fork and ~/probe_check.py speak it: 115200 baud, raw; a command is a line
ending in "\\n" and gets one line back:

    H          -> "homed"         retract fully; the next touch reads 0.0
    ZR:0.200   -> "OK ZR=0.200"   how far it backs off after a touch (inches)
    P          -> "-0.0090"       lower until it touches: Z in inches, relative to the first touch after H;
                  "E" or "ERROR"  it went all the way down without touching (over travel)
    A          (no reply)         stop moving at once

Input is drained before each command (a late reply to an aborted command must not answer the next one)
and a reply that hasn't come within 9 s (as UGS) is a failure. One command at a time; abort() can come
from any thread at any time and wakes a command waiting for its reply.

Its tip may be down (state()["mayBeDown"]) from the moment a P or an H is sent, and after an abort, until an H
comes back "homed": so a scan Stop, an abort or a failed H leaves it on. That outlives the port (Disconnect, a lost
port, or Machine.release_extras letting go of it once no window is open), so a page opened later still warns
before X / Y moves; nothing here moves the probe to clear it (only the user's Home Probe, or a scan's H, does).
release_extras lets go of it with let_go(), which turns it on for a probe connected and not homed as well: the page
warned for that one too (just connected, its tip may still be down), and a release with no window open must not
take the warning away. The user's own Disconnect leaves it as it is.
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
from collections import deque
from decimal import ROUND_HALF_UP, Context, Decimal

from machine import (WRITE_TIMEOUT, MachineError, close_port, describe_port, open_port, other_holders,
                     port_kind, usb_info)

BAUD = termios.B115200
REPLY_TIMEOUT = 9.0     # UGS waits this long for every probe reply
SETTLE = 1.0            # the board needs this long after the port opens (probe_check.py)
QUIET = 0.05            # draining: the input is drained after this long without a byte ...
DRAIN_MAX = 0.5         # ... or after this long if it keeps talking
MAX_REPLY = 200         # characters kept of a reply line (a longer one is noise)
MAX_SAID = 20           # lines nobody asked for that are shown in the console, per connection
LEONARDO = ("2341", "8036")
NUMBER = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
NON_FINITE = re.compile(r"[+-]?(Infinity|NaN)")      # Java parses these (UGS: "Non-finite probe value")
NOT_READY = "Probe not ready. Home the probe first."
NO_HOME_REPLY = "No response from Auto Probe. Is it connected?"
MOVES = ("P", "H")      # the commands that move its tip: once one is sent, the tip may be down until H says "homed"
_WIDE = Context(prec=400)


class ProbeError(MachineError):
    """A probe command that failed. reply is what the probe said (None: nothing)."""

    def __init__(self, message, reply=None, status=409):
        super().__init__(message, status)
        self.reply = reply


class ProbeOverTravel(ProbeError):
    """P came back "E": the probe went all the way down without touching."""


class ProbeAborted(ProbeError):
    """abort() came while the command was waiting (or before it was sent)."""


def java_fixed(v, places):
    """String.format("%.<places>f", v) as Java writes it (UGS made the user's ZR commands and .xyz headers with it):
    Java rounds the shortest decimal form half up, Python's % rounds the exact binary value half to even, so
    0.1235 is "0.124" in Java and "0.123" in Python."""
    d = Decimal(repr(float(v))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP, context=_WIDE)
    return f"{d:f}"


def default_port(ports):
    """The probe port to offer from machine.ports()["ports"]: the Leonardo nobody has open, else None."""
    for p in ports:
        if p.get("kind") == "usb-acm" and (p.get("vid"), p.get("pid")) == LEONARDO and not p.get("inUseBy"):
            return p["path"]
    return None


class _Port:
    """One open probe port, its reader thread and the pipe that wakes it."""

    def __init__(self, fd, path):
        self.fd = fd
        self.path = path
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)
        self.thread = None


class Probe:
    """The probe link. connect / disconnect / state, and the commands home, set_retract, probe and abort.
    Refusals and failures raise MachineError (ProbeError for a command that failed) with the user's message.
    While claim()ed (by the scan) only its owner may send commands; abort() is always allowed.
    log(kind, text) is the console ('info' or 'error'); emit, if given, gets {"type": "probe", "probe": state()}
    after every change. Neither is ever called with a lock held."""

    def __init__(self, log, emit=None, test_ports=(), only_test_ports=False):
        self._log = log
        self.emit = emit
        self.test_ports = [p for p in test_ports if p]   # pseudo terminals that may be opened (tests)
        self.only_test_ports = only_test_ports           # tests: open nothing else
        self.lock = threading.Condition()   # guards the state below; notified when a line comes, on abort, on close
        self.cmd_lock = threading.Lock()    # held by the one command in progress
        self.wlock = threading.Lock()       # orders the writes (an abort never lands inside a command) and the close
        self.port = None                    # the open _Port
        self.path = None
        self.connecting = False
        self.connected = False              # open, settled, and not lost since
        self.ready = False                  # homed since connect, the last over travel, abort or failure
        self.down = False                   # its tip may be down: a P or H was sent, or an abort came, since the
                                            # last H that came back "homed" (or let_go() let go of it not homed).
                                            # Kept when the port closes (see the top)
        self.busy = None                    # the command waiting for its reply
        self.last = None                    # the last command and what came back, for the page
        self.owner = None                   # who has claimed the probe ('scan')
        self.lines = deque(maxlen=20)       # reply lines the command in progress hasn't taken yet
        self.rx = b""
        self.rx_at = 0.0                    # when the last byte came (the drain waits for quiet)
        self.aborts = 0                     # abort() calls: a command started before the last one gives up
        self.gone = None                    # why the port was closed
        self.said = 0                       # lines logged that nobody asked for, since connect

    # -------------------------------------------------- connection

    def state(self):
        """For the page. mayBeDown: its tip may be down (see down), also while it is not connected: the page warns
        before X / Y moves until it is connected and homed again."""
        with self.lock:
            return {"port": self.path if self.port or self.connecting else None, "connected": self.connected,
                    "ready": self.connected and self.ready, "busy": self.busy is not None, "last": self.last,
                    "connecting": self.connecting, "mayBeDown": self.down}

    def connect(self, port, force=False):
        path = port.strip() if isinstance(port, str) else ""
        if not path:
            raise MachineError("No probe port given")
        with self.lock:
            if self.port:
                raise MachineError(f"The probe is already connected on {self.path}. Disconnect it first.", 409)
            if self.connecting:
                raise MachineError("Already connecting to the probe", 409)
            self.connecting = True
            self.path = path
        self.changed()
        try:
            self._connect(path, force)
        except BaseException:
            with self.lock:
                if not self.port:
                    self.path = None
            raise
        finally:
            with self.lock:
                self.connecting = False
            self.changed()
        self.log("info", f"Probe connected on {path}. Home Probe before scanning.")
        return {"port": path}

    def check_port(self, path, force):
        """Raises unless path may be opened as the probe's port."""
        real = os.path.realpath(path)
        tests = {os.path.realpath(p) for p in self.test_ports}
        if self.only_test_ports and real not in tests:
            raise MachineError(f"{path} is not one of the test ports this server may open")
        if real in tests:
            return real
        kind = port_kind(real)
        if kind == "usb-serial":    # (never, even with force: that is FluidNC)
            raise MachineError(f"{path} is a USB serial port like FluidNC's, not the probe (Arduino Leonardo). "
                               "Not connected.")
        if kind == "bluetooth":
            raise MachineError(f"{path} is a Bluetooth port (the jog pendant), not the probe. Not connected.")
        if kind != "usb-acm":
            raise MachineError(f"{path} is not a USB serial port like the probe's (/dev/ttyACM...)")
        if not os.path.exists(real):
            raise MachineError(f"{path} is not there (unplugged?)")
        vid, pid, product = usb_info(os.path.basename(real))
        if (vid, pid) != LEONARDO and "leonardo" not in (product or "").lower() and not force:
            raise MachineError(f"{path} looks like {describe_port(real)['label']}, not the probe (Arduino Leonardo). "
                               "Not connected.")
        return real

    def _connect(self, path, force):
        real = self.check_port(path, force)

        def in_use(others):
            return MachineError(f"Port {path} is in use by {others}. Disconnect it there first.", 409)
        others = other_holders(real)
        if others:
            raise in_use(others)
        fd = open_port(real, BAUD)
        others = other_holders(real)        # someone may have opened it just before we locked it
        if others:
            close_port(fd)
            raise in_use(others)
        try:
            port = _Port(fd, path)
        except OSError as e:
            close_port(fd)
            raise MachineError(f"Could not connect to the probe: {e.strerror}")
        port.thread = threading.Thread(target=self.reader, args=(port,), name=f"probe {path}", daemon=True)
        with self.lock:
            self.port = port
            self.gone = None
            self.rx = b""
            self.lines.clear()
            self.ready = False
            self.last = None
            self.said = 0
        try:
            port.thread.start()
        except BaseException:
            with self.lock:
                self.port = None
            close_port(port.fd)
            os.close(port.wake_r)
            os.close(port.wake_w)
            raise
        end = time.monotonic() + SETTLE     # the board settles; what it says meanwhile is logged, not kept
        with self.lock:
            while self.port is port and time.monotonic() < end:
                self.lock.wait(end - time.monotonic())
            if self.port is not port:
                raise MachineError(self.gone or f"{path} was closed while connecting")
            self.connected = True
            self.rx = b""
            self.lines.clear()

    def disconnect(self, force=False):
        """Closes the port (a probe move in progress is aborted first). Refused while claimed, unless force."""
        with self.lock:
            if self.owner and not force:
                raise MachineError(f"The {self.owner} is using the probe: stop it first", 409)
            port, busy = self.port, self.busy
        if not port:
            return {}
        if busy:
            self.abort()        # its move must not go on with nobody waiting for it
        self.close(port, f"Probe disconnected from {port.path}", "info")
        return {}

    def let_go(self):
        """Machine.release_extras, no window open: disconnect(), and a probe connected but not homed keeps
        mayBeDown on from then (the page warned "Probe may be down" for it: just connected, its tip may still be
        down; a release must not take that away). Refused like disconnect() while claimed, and then changes nothing."""
        with self.lock:
            if self.connected and not self.ready and not self.owner:
                self.down = True
        return self.disconnect()

    def close(self, port, why, kind):
        with self.lock:
            if self.port is not port:
                return
            self.port = None
            self.connected = False
            self.ready = False              # (down stays: the tip is where it was)
            self.gone = why
            self.lock.notify_all()
        try:
            os.write(port.wake_w, b"x")
        except OSError:
            pass
        if port.thread is not threading.current_thread():
            port.thread.join(WRITE_TIMEOUT + 2)
        with self.wlock:                    # only once the reader has stopped: the fd number may be reused next
            close_port(port.fd)
            port.fd = None
        for w in (port.wake_r, port.wake_w):
            try:
                os.close(w)
            except OSError:
                pass
        if why:
            self.log(kind, why)
        self.changed()

    def reader(self, port):
        why = None
        try:
            while True:
                ready = select.select([port.fd, port.wake_r], [], [])[0]
                with self.lock:
                    if self.port is not port:
                        return
                if port.wake_r in ready:
                    return
                try:
                    data = os.read(port.fd, 1024)
                except BlockingIOError:
                    continue
                if not data:
                    raise OSError(errno.EIO, "the port was closed")
                self.received(data)
        except OSError as e:
            why = f"Lost the connection to the probe on {port.path} ({e.strerror or e}). Unplugged?"
        except Exception as e:              # a bug: close the port and say so, rather than hang the next command
            traceback.print_exc()
            why = f"Closed the probe port after an internal error in FluidSender ({e!r}). Connect again."
        self.close(port, why, "error")

    def received(self, data):
        said = []
        with self.lock:
            self.rx_at = time.monotonic()
            self.rx += data
            while b"\n" in self.rx:
                raw, self.rx = self.rx.split(b"\n", 1)
                line = raw.decode("ascii", "replace").strip()[:MAX_REPLY]
                if not line:
                    continue
                if self.busy is not None:
                    self.lines.append(line)
                    self.lock.notify_all()
                elif self.said < MAX_SAID:
                    self.said += 1
                    said.append(line if self.said < MAX_SAID else f"{line}  (more not shown)")
            if len(self.rx) > MAX_REPLY:    # no line end in sight: noise
                self.rx = b""
        for line in said:                   # nobody asked: boot text, or a late reply after an abort
            self.log("info", f"Probe says: {line}")

    def changed(self):
        if self.emit:
            try:
                self.emit({"type": "probe", "probe": self.state()})
            except Exception:               # (a command must still end cleanly)
                traceback.print_exc()

    def log(self, kind, text):
        try:
            self._log(kind, text)
        except Exception:
            traceback.print_exc()

    # -------------------------------------------------- claim (the scan has the probe to itself)

    def claim(self, owner):
        """From now on only owner's commands are taken. (command() checks the owner and sets busy under one
        lock: a command either started before the claim, which is then refused, or is refused itself.)"""
        with self.lock:
            if not self.connected:
                raise MachineError("The probe is not connected", 409)
            if self.owner not in (None, owner):
                raise MachineError(f"The {self.owner} is using the probe", 409)
            if self.busy is not None:
                raise MachineError("The probe is busy: wait until it is done", 409)
            self.owner = owner

    def release(self, owner):
        with self.lock:
            if self.owner == owner:
                self.owner = None

    # -------------------------------------------------- commands

    def write(self, data):
        """Writes all of data (wlock held)."""
        port = self.port
        if port is None or port.fd is None:
            raise OSError(errno.EBADF, "not connected")
        end = time.monotonic() + WRITE_TIMEOUT
        while data:
            try:
                n = os.write(port.fd, data)
            except BlockingIOError:
                n = 0
            data = data[n:]
            if data:
                if time.monotonic() > end:
                    raise OSError(errno.ETIMEDOUT, "the probe takes no more data")
                time.sleep(0.01)

    def command(self, text, owner=None, needs_ready=False):
        """Sends one command; returns (its reply line or None if none came within REPLY_TIMEOUT, the abort count
        it ran under). Raises ProbeAborted if abort() came first, ProbeError if the port is or gets lost."""
        if not self.cmd_lock.acquire(blocking=False):      # (refused, never queued for later)
            raise MachineError("The probe is busy: wait until it is done", 409)
        try:
            with self.lock:
                if not self.connected:
                    raise MachineError("The probe is not connected", 409)
                if self.owner not in (None, owner):
                    raise MachineError(f"The {self.owner} is using the probe: stop it first", 409)
                gen = self.aborts
                self.busy = text
            self.changed()
            try:
                self.drain(gen)
                with self.wlock:            # (abort takes wlock to write: it comes before this command or after it)
                    with self.lock:
                        self.alive(gen)
                        if needs_ready and not self.ready:
                            raise ProbeError(NOT_READY)
                        self.lines.clear()
                        self.rx = b""
                        if text in MOVES:   # (sent now: whatever comes of it, only a good H makes it up again)
                            self.down = True
                    try:
                        self.write((text + "\n").encode())
                    except OSError as e:
                        raise ProbeError(f"Could not write to the probe ({e.strerror or e})")
                reply = self.reply(gen)
                with self.lock:
                    self.last = f"{text} -> {reply if reply is not None else 'no reply'}"
                return reply, gen
            except ProbeAborted:
                with self.lock:
                    self.last = f"{text} -> aborted"
                raise
            finally:
                with self.lock:
                    self.busy = None
                self.changed()
        finally:
            self.cmd_lock.release()

    def alive(self, gen):
        """(lock held) Raises if abort() came since gen, or the port is gone."""
        if self.aborts != gen:
            raise ProbeAborted("Probe stopped")
        if not self.connected:
            raise ProbeError(self.gone or "The probe is not connected")

    def drain(self, gen):
        """Waits until the probe has said nothing for QUIET s (at most DRAIN_MAX) and the reader has taken
        everything the port has, so stale input can't answer the command (probe_check.py drains the same way)."""
        start = time.monotonic()
        end = start + DRAIN_MAX
        with self.lock:
            while True:
                self.alive(gen)
                now = time.monotonic()
                left = min(max(self.rx_at, start) + QUIET, end) - now
                if left <= 0 and (now >= end or not self.unread()):
                    return
                self.lock.wait(max(left, 0.002))

    def unread(self):
        """Bytes the port has that the reader hasn't taken yet."""
        try:
            return struct.unpack("i", fcntl.ioctl(self.port.fd, termios.FIONREAD, b"\0" * 4))[0]
        except (OSError, TypeError, AttributeError):
            return 0

    def reply(self, gen):
        end = time.monotonic() + REPLY_TIMEOUT
        with self.lock:
            while not self.lines:
                self.alive(gen)
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.lock.wait(left)
            return self.lines.popleft()

    def set_ready(self, gen, ready):
        with self.lock:
            if self.aborts == gen or not ready:     # an abort after the reply wins
                self.ready = ready
                if ready:                   # (only a good H makes it ready: its tip is up)
                    self.down = False
        self.changed()

    def home(self, owner=None):
        """H: retract fully (the page's "Home Probe"). The probe is ready afterwards, its tip up (mayBeDown off);
        an H that fails leaves mayBeDown on."""
        reply, gen = self.command("H", owner)
        r = (reply or "").strip()
        if reply is not None and r.lower() == "homed":
            self.set_ready(gen, True)
            message = "Auto Probe homed successfully."
            self.log("info", message)
            return {"message": message}
        self.set_ready(gen, False)
        if reply is None:
            message = NO_HOME_REPLY
        elif r.upper() in ("E", "ERROR"):
            message = "Probe error during homing. Check for overtravel."
        else:
            message = f"Unexpected probe response: {r}"
        self.log("error", message)
        raise ProbeError(message, reply)

    def set_retract(self, zr, owner=None):
        """ZR: how far the probe backs off after each touch (inches). Returns the reply ("OK ZR=0.200")."""
        if isinstance(zr, bool) or not isinstance(zr, (int, float)) or not math.isfinite(zr) or not 0 < zr <= 10:
            raise MachineError(f"Bad Z retract: {zr!r}")
        reply, gen = self.command("ZR:" + java_fixed(zr, 3), owner)
        r = (reply or "").strip()
        u = r.upper()
        if reply is None:
            message = "No response from probe to Z retract (ZR)."
        elif u in ("E", "ERROR") or u.startswith("ERR"):
            message = "Probe reported an error on Z retract (ZR)."
        elif not u.startswith("OK"):
            message = f"Unexpected probe response to Z retract (ZR): {r}"
        else:
            return r
        self.set_ready(gen, False)
        raise ProbeError(message, reply)

    def probe(self, owner=None):
        """P: lower until the probe touches. Returns Z in inches: 0.0 for the first touch after home(), the
        later ones relative to it. Raises ProbeOverTravel on "E"; refused unless ready."""
        reply, gen = self.command("P", owner, needs_ready=True)
        r = (reply or "").strip()
        z = None
        if reply is None:
            error = ProbeError("No response from probe to P.")
        elif r.upper() in ("E", "ERROR"):
            error = ProbeOverTravel("Probe reported over travel while probing.", reply)
        elif NON_FINITE.fullmatch(r):
            error = ProbeError(f"Non-finite probe value: {r}", reply)
        elif not NUMBER.fullmatch(r):
            error = ProbeError(f"Invalid probe numeric response: {r}", reply)
        else:
            z = float(r)
            error = None if math.isfinite(z) else ProbeError(f"Non-finite probe value: {r}", reply)
        if error:
            self.set_ready(gen, False)
            raise error
        return z + 0.0          # (as UGS: -0.0 is stored as 0.0)

    def abort(self):
        """A: the probe stops moving at once. Any thread, any time; a command waiting for its reply gives up
        (ProbeAborted), and one not sent yet isn't sent. The probe must be homed again before probing."""
        with self.lock:
            if not self.connected:
                return False
            self.aborts += 1
            self.ready = False
            self.down = True                # (it stops wherever it is)
            doing = self.busy
            self.lock.notify_all()
        try:
            with self.wlock:
                self.write(b"A\n")
        except OSError as e:
            self.log("error", f"Probe abort could not be sent ({e.strerror or e})")
            self.changed()
            return False
        self.log("info", f"Probe abort sent{f' (during {doing})' if doing else ''}")
        self.changed()
        return True
