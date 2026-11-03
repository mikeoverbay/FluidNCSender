"""
The touch probe scan: the surface measured with the bit itself, through FluidNC's probe input, for people without
the auto probe (scan.py). One lead clips on the copper of a blank PCB (or any conductive stock), the other on the
bit; the spindle is OFF. The user's FluidNC has a probe input (probe: pin: gpio.2:pd, check_mode_start: true).

Completely apart from the auto probe: its own settings (settings.json "touchScan": the grid, clearance, depth and
feed; settings.py check_touch), its own Start / Pause / Resume / Stop, its own state (the page's Touch probe tab:
GET /api/machine/state's "touch", events of type "touch"); it never sends anything to the auto probe, not even on a
Stop. Only the last scan is shared: both kinds write the same .xyz (scans/last-scan.xyz, UGS's format with
FluidSender's setup line: scan.py), and auto-level's "last scan" is the newest of either kind (Machine.last_scan).

A grid of xPoints x yPoints from (minX, minY) to (maxX, maxY), in work coordinates and inches, with NO offset (the
bit touches where it is), visited in the auto probe's order (column by column in X, zigzag in Y). The bit first rises
to the clearance (work Z) if it is lower, then at each point:
    G20 G90 G0 X.. Y..                  at the clearance (the first point: at the start Z, if that is higher)
    G20 G90 G0 Z<clearance>             (only when not there yet)
    G20 G90 G38.2 Z<-depth> F<feed>     down until the bit touches the copper, at most to work Z -depth
    G20 G90 G0 Z<clearance>             back up
FluidNC answers the G38.2 with "[PRB:x,y,z:1]", the machine position where it touched; less the work offset that is
the work Z of the surface there (Machine.touch_line). So the heights in the file are the surface's work Z: 0 where
the copper is at work Z 0 (zero Z on the copper with the bit, as for the job). The auto probe's are relative to its
first touch, which the user zeroes Z on, so both mean the same with auto-level's Z surface 0 (leveled Z = Z + height
- Z surface: leveler.py): a touch scan's heights stay right even with Z zeroed somewhere else on the copper.

No contact (FluidNC's ALARM:5: the bit went down to -depth without touching) ends the scan at once, with the point
("No contact at point 3 (X0.5000 Y0.2500): is the clip on the copper?"); so does ALARM:4, the probe input closed
before a touch. If the spindle comes on during the scan (FluidNC's reports), the scan ends at once and the machine
is stopped (the hold, then the reset, which turns it off).

Start is refused (machine.touch_ready) while the probe input is already closed (FluidNC's report says Pn:P: "The
probe input is already closed: check the clip"), while the spindle is on or has a speed, and without a verified probe
check: FluidSender must have seen, in FluidNC's reports since connecting, the probe input open, then closed, then
open again (the user touches the clip to the bit and lets go: machine.Link.track_probe). Nothing else passes it: no
button. Each scan uses it up; a reset, an alarm, a disconnect and 10 minutes clear it. Once a touch scan is complete
the state says clipWarn until FluidNC is disconnected: the page's Run then warns to take the clip off the bit.

Pause (the page's Pause button) takes effect between points: once the current point's bit is back at the clearance
the scan waits there ("Paused at point n of N") until Resume, which goes on with the next point, or Stop. Stop
safe-stops the CNC (machine.stop: a hold, then a reset once it stands still), then raises Z all the way up
(machine.scan_raise: G53 to the top of the Z travel, never in an alarm); the machine's own Stop (the page's or the
pendant's) does the same. The points measured so far stay on screen.
"""

import math
import os
import threading
import time
import traceback

from leveling import setup_line
from machine import TOUCH, MachineError, ProbeFailed, SpindleOn, fixed
from scan import _real, _sentence, axis_values, probe_order, write_file, xyz_text
from settings import TOUCH as DEFAULTS
from settings import SettingsError, check_touch

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = "touchScan"           # its settings.json key
UNSET = "Touch scan area not set: Min / Max X and Y are all 0. Set them (or Use G-code area) first."
STOPPED = "Touch scan stopped."
RAPID = 50.0                # in/min: the slowest rapid to count with when FluidNC's max rates aren't known
MOVE_MIN = 15.0             # s: a move may take this long at least, or 3x its time at the rapid + 2 s
TOUCH_MORE = 15.0           # s: a touch may take its time at the feed (clearance to -depth) x 1.5, and this more


def check_settings(data=None, base=None):
    """The touch scan settings: data (all or some of DEFAULTS' keys) over base (the saved ones) over the defaults,
    checked (settings.check_touch). Returns all of them; raises MachineError (400) with what is wrong."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise MachineError("Touch scan settings must be an object")
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise MachineError(f"Unknown touch scan setting: {unknown[0]}")
    out = dict(DEFAULTS)
    if isinstance(base, dict):
        out.update((k, base[k]) for k in DEFAULTS if k in base)
    out.update(data)
    try:
        return check_touch(out)
    except SettingsError as e:
        raise MachineError(str(e)) from None


def load_settings(raw):
    """Saved settings as check_settings has them; the defaults if they are missing or don't pass."""
    if isinstance(raw, dict):
        try:
            return check_settings({k: raw[k] for k in DEFAULTS if k in raw})
        except MachineError:
            pass
    return dict(DEFAULTS)


def area_unset(s):
    """True while Min / Max X and Y are all 0 (a first run): no area to scan yet."""
    return all(s[k] == 0 for k in ("minX", "maxX", "minY", "maxY"))


class _Stop(Exception):
    """The user stopped the scan."""


class TouchScanner:
    """One touch scan at a time, in its own thread. start(settings) / pause() / resume() / stop() / state(); save(path)
    and save_settings(data) for the page; last_scan() for auto-level. machine provides begin_activity,
    end_activity, touch_line, scan_raise, stop() and status_now(), and, if it has them, snapshot() (the max rates
    the move timeouts count with), activity_stop (True once its own Stop came), work_setup() for the file's setup
    line and scan_complete(); settings is the settings store (get / set "touchScan"); emit gets {"type": "touch",
    "touch": state()} on every point and change; log(kind, text) is the console. No lock of this class is held while
    any of them is called."""

    def __init__(self, machine, settings, emit, log, scans_dir=None):
        self.machine = machine
        self.store = settings
        self.emit = emit
        self._log = log
        self.scans_dir = scans_dir or os.environ.get("FLUIDSENDER_SCANS") or os.path.join(HERE, "scans")
        self.lock = threading.Lock()        # guards the state below
        self.wake = threading.Condition(self.lock)     # a paused scan waits on it for Resume or Stop
        self.save_lock = threading.Lock()   # one file write at a time
        self.thread = None
        self.starting = False
        self.running = False
        self.ending = False                 # the scan's work is over (only the bookkeeping is left)
        self.stopping = False               # stop() came for the scan starting or running
        self.pausing = False                # Pause came: the scan pauses after the point it is at
        self.paused = False                 # it waits at the clearance for Resume
        self.phase = None                   # starting, moving, probing, retracting, paused, stopping;
                                            # then done, stopped or failed
        self.settings = None                # the running or last scan's settings
        self.xs = self.ys = None
        self.order = []
        self.z = {}                         # (x index, y index) -> the surface's work Z there
        self.at = None                      # the point being moved to or touched
        self.last_z = None
        self.text = None                    # the .xyz text, once every point is in
        self.error = None
        self.message = None
        self.saved_to = None
        self.started = self.finished = None
        self.clip = False                   # a scan completed since connecting: the page's Run warns to take the
                                            # clip off the bit first (until FluidNC is disconnected)
        self.pos = None                     # where the bit is (work X, Y, Z; for the next move's timeout)
        self.rate = RAPID

    # -------------------------------------------------- state and settings

    def saved_settings(self):
        try:
            raw = self.store.get(KEY) if self.store is not None else None
        except Exception:                   # (a broken store must not break the snapshot)
            traceback.print_exc()
            raw = None
        return load_settings(raw)

    def save_settings(self, data):
        """Checks and saves the touch scan settings (POST /api/touch/settings). Returns {"settings": all of them}."""
        settings = check_settings(data, self.saved_settings())
        self.remember(settings)
        self.changed()
        return {"settings": settings}

    def remember(self, settings):
        if hasattr(self.store, "set"):
            self.store.set(KEY, dict(settings))
        elif isinstance(self.store, dict):
            self.store[KEY] = dict(settings)

    def state(self):
        saved = self.saved_settings()
        with self.lock:
            result = None
            if self.xs is not None:
                points = [[x, y, self.z[i, j]] for i, x in enumerate(self.xs) for j, y in enumerate(self.ys)
                          if (i, j) in self.z]
                result = {"settings": dict(self.settings), "xs": list(self.xs), "ys": list(self.ys),
                          "points": points, "complete": self.text is not None, "text": self.text}
            settings = self.settings if self.running and self.settings else saved
            total = len(self.order) if self.order else saved["xPoints"] * saved["yPoints"]
            return {"running": self.running, "phase": self.phase, "point": len(self.z), "points": total,
                    "at": dict(self.at) if self.at else None, "lastZ": self.last_z, "settings": dict(settings),
                    "paused": self.paused, "pausing": self.pausing and not self.paused, "result": result,
                    "error": self.error, "message": self.message, "savedTo": self.saved_to, "started": self.started,
                    "finished": self.finished, "clipWarn": self.clip}

    def last_scan(self):
        """This session's complete touch scan: {"text" (the .xyz), "savedTo", "finished"}, or None."""
        with self.lock:
            if self.text is None:
                return None
            return {"text": self.text, "savedTo": self.saved_to, "finished": self.finished}

    def changed(self):
        if self.emit:
            try:
                self.emit({"type": "touch", "touch": self.state()})
            except Exception:               # (the scan must go on, and end cleanly)
                traceback.print_exc()

    def log(self, kind, text):
        try:
            self._log(kind, text)
        except Exception:
            traceback.print_exc()

    # -------------------------------------------------- start, pause, resume, stop

    def start(self, settings=None):
        """Starts a touch scan with these settings (over the saved ones). Refused (MachineError) if the settings are
        bad or set no area, a scan runs, the machine refuses it (not connected, not Idle, a job...), its work
        position isn't known yet, or the probe input is closed already."""
        settings = check_settings(settings, self.saved_settings())
        if area_unset(settings):
            raise MachineError(UNSET)
        with self.lock:
            if self.running or self.starting:
                raise MachineError("A touch scan is already running", 409)
            self.starting = True
            self.stopping = self.pausing = self.paused = False
        try:
            self.machine.begin_activity(TOUCH)
            try:                            # (the machine is the scan's now: nothing else can move it)
                pos = self.ready()
            except BaseException:
                self.end_activity()
                raise
            xs = axis_values(settings["minX"], settings["maxX"], settings["xPoints"])
            ys = axis_values(settings["minY"], settings["maxY"], settings["yPoints"])
            with self.lock:
                self.running = True
                self.ending = False
                self.phase = "stopping" if self.stopping else "starting"
                self.settings, self.xs, self.ys = settings, xs, ys
                self.order = probe_order(len(xs), len(ys))
                self.z = {}
                self.at = self.last_z = self.text = self.error = self.message = self.saved_to = None
                self.started, self.finished = time.time(), None
                self.pos = pos
                self.thread = threading.Thread(target=self.run, name="touch scan", daemon=True)
            try:
                self.thread.start()
            except BaseException:
                with self.lock:
                    self.running = False
                    self.phase = None
                self.end_activity()
                raise
        finally:
            with self.lock:
                self.starting = False
        try:
            self.remember(settings)         # (the settings a scan ran with are kept for the next one)
        except Exception as e:
            self.log("error", f"Could not save the touch scan settings ({e})")
        self.changed()
        return {"points": len(xs) * len(ys)}

    def ready(self):
        """The checks once the machine is the scan's (machine.touch_ready: the work position known, the spindle off,
        the probe input open, and a verified probe check, which it uses up). Returns the work position [x, y, z];
        else MachineError (409)."""
        wpos = self.machine.touch_ready()
        if not (isinstance(wpos, list) and len(wpos) >= 3 and all(_real(v) for v in wpos[:3])):
            raise MachineError("Work position not known yet; try again in a second", 409)
        return list(wpos[:3])

    def disconnected(self):
        """FluidNC's port closed (machine.link_closed): the remove-the-clip warning is over (see clip)."""
        with self.lock:
            if not self.clip:
                return
            self.clip = False
        self.changed()

    def pause(self):
        """Pause (POST /api/touch/pause): the scan stops after the point it is at, with the bit back at the
        clearance, and waits there for Resume. Refused when no scan runs, or it is stopping or paused already."""
        with self.lock:
            if not self.running or self.stopping or self.ending:
                raise MachineError("No touch scan is running", 409)
            if self.paused or self.pausing:
                raise MachineError("The touch scan is paused already" if self.paused
                                   else "The touch scan pauses after this point", 409)
            self.pausing = True
            n, total = len(self.z) + 1, len(self.order)
        self.log("info", f"Touch scan: pausing after point {min(n, total)} of {total}")
        self.changed()
        return {"paused": False, "pausing": True}

    def resume(self):
        """Resume (POST /api/touch/resume): a paused scan goes on with the next point; one that was only going to
        pause doesn't. Refused when it is neither."""
        with self.lock:
            if not self.running or self.stopping or not (self.paused or self.pausing):
                raise MachineError("The touch scan is not paused", 409)
            was = self.paused
            self.pausing = self.paused = False
            self.wake.notify_all()
        if was:
            self.log("info", "Touch scan resumed")
        self.changed()
        return {"resumed": True}

    def stop(self, stop_machine=True):
        """Stops the scan from any thread, at any time (also while paused): safe-stops the CNC (machine.stop();
        stop_machine=False when the caller is machine.stop itself); the scan thread then raises Z. Never sends
        anything to the auto probe. Returns False if no scan was starting or running (or it is stopping already)."""
        with self.lock:
            if not (self.running or self.starting) or self.stopping or self.ending:
                return False
            self.stopping = True
            if self.running:
                self.phase = "stopping"
            self.wake.notify_all()
        self.log("info", "Stopping the touch scan")
        self.changed()
        if stop_machine:
            try:
                self.machine.stop()
            except MachineError as e:       # (not connected, nothing moving ...)
                self.log("info", f"Touch scan stop: {e}")
            except Exception as e:          # a bug there: the scan still stops (its next line is refused or ends)
                traceback.print_exc()
                self.log("error", f"Touch scan stop: the machine could not be stopped ({e!r})")
        return True

    def shutdown(self, timeout=12.0):
        """The server is going away: stop a scan and wait for its thread (bounded)."""
        self.stop()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def save(self, path):
        """Writes the last complete touch scan to path (.xyz only; POST /api/touch/save). Returns {"path", "size"}."""
        path = os.path.abspath(os.path.expanduser(path if isinstance(path, str) else ""))
        if not path.lower().endswith(".xyz"):
            raise MachineError("Scans are saved as .xyz files")
        with self.lock:
            text = self.text
        if text is None:
            raise MachineError("No complete touch scan to save yet", 409)
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            raise MachineError(f"folder not found: {folder}", 404)
        with self.save_lock:
            try:
                write_file(path, text)
            except OSError as e:
                raise MachineError(f"Could not save {path}: {e.strerror or e}", 500)
        with self.lock:
            if self.text is text:
                self.saved_to = path
        self.log("info", f"Touch scan saved to {path}")
        self.changed()
        return {"path": path, "size": len(text)}

    def end_activity(self):
        try:
            self.machine.end_activity(TOUCH)
        except Exception as e:
            self.log("error", f"Touch scan: could not end the scan on the machine ({e})")

    # -------------------------------------------------- the scan thread

    def run(self):
        outcome = None
        try:
            outcome = self.scan()
        except BaseException as e:          # a bug: say so, never leave the machine taken
            traceback.print_exc()
            outcome = ("failed", f"Touch scan failed: internal error in FluidSender ({e!r}).")
        finally:
            with self.lock:
                self.ending = True
            phase, message = outcome or ("failed", "Touch scan failed")
            self.end_activity()
            with self.lock:
                self.running = self.stopping = self.ending = self.pausing = self.paused = False
                self.phase = phase
                self.at = None
                self.finished = time.time()
                if phase == "done":
                    self.message, self.error = message, None
                else:
                    self.message, self.error = None, message
            self.log("info" if phase in ("done", "stopped") else "error", message)
            self.changed()

    def check(self):
        with self.lock:
            if self.stopping:
                raise _Stop()

    def stopped(self, e):
        """Did e come from a stop: stop() here, or the machine's own Stop (machine.stop() sets activity_stop and
        wakes touch_line before it calls stop() here, so the line can fail first)?"""
        with self.lock:
            if self.stopping or isinstance(e, _Stop):
                return True
        return getattr(self.machine, "activity_stop", False) is True

    def set(self, **kw):
        with self.lock:
            if not self.stopping:
                for k, v in kw.items():
                    setattr(self, k, v)
        self.changed()

    def where(self):
        """The slowest axis max rate (in/min; Z moves too), from the machine: only the move timeouts depend on it."""
        try:
            snap = self.machine.snapshot()
            rates = [((snap.get("axes") or {}).get(a) or {}).get("maxRate") for a in "xyz"]
            rates = [r for r in rates if _real(r) and r > 0]
            return min(rates) if rates else RAPID
        except Exception:
            return RAPID

    def rapid(self):
        """FluidNC's rapid override now as a fraction (1.0 when not known): the moves are G0s, which it slows."""
        try:
            ov = (self.machine.status_now() or {}).get("ov")
            r = ov[1] if isinstance(ov, (list, tuple)) and len(ov) >= 2 else None
        except Exception:
            r = None
        return r / 100.0 if _real(r) and 0 < r <= 100 else 1.0

    def move(self, x=None, y=None, z=None):
        """A G0 to work X Y or Z (G20 G90), waited for until FluidNC stands there: 3x the expected time + 2 s,
        15 s at least."""
        to = list(self.pos)
        at = {}
        for i, v in enumerate((x, y, z)):
            if v is not None:
                to[i] = at[i] = v
        dist = math.dist(self.pos, to)
        timeout = max(MOVE_MIN, dist / (self.rate * self.rapid()) * 60.0 * 3.0 + 2.0)
        words = " ".join(f"{a}{fixed(v)}" for a, v in zip("XYZ", (x, y, z)) if v is not None)
        got = self.machine.touch_line(f"G20 G90 G0 {words}", timeout, at=at)
        self.pos = [got["x"], got["y"], got["z"]] if all(_real(got.get(a)) for a in "xyz") else to

    def scan(self):
        """The scan itself. Returns (phase, message) for the end."""
        s, xs, ys, order = self.settings, self.xs, self.ys, self.order
        clear, depth, feed = s["clearance"], s["depth"], s["feed"]
        total = len(order)
        self.rate = self.where()
        self.log("info", f"Touch scan: {s['xPoints']} x {s['yPoints']} = {total} points, X {s['minX']:.4f} to "
                         f"{s['maxX']:.4f}, Y {s['minY']:.4f} to {s['maxY']:.4f} in, clearance Z {clear:.3f} in, "
                         f"touches down to Z {-depth:.3f} at F{feed:g} in/min (the bit itself: no offset)")
        touch = f"G20 G90 G38.2 Z{fixed(-depth)} F{fixed(feed, 3)}"
        touch_time = (clear + depth) / feed * 60.0 * 1.5 + TOUCH_MORE
        n, x, y = 0, xs[0], ys[0]
        try:
            try:
                self.check()
                if self.pos[2] < clear:     # (up first, never down where it is: it may be over a clamp)
                    self.set(phase="moving", at={"n": 1, "x": x, "y": y})
                    self.move(z=clear)
                for n, (i, j) in enumerate(order, 1):
                    x, y = xs[i], ys[j]
                    self.check()
                    self.set(phase="moving", at={"n": n, "x": x, "y": y})
                    self.move(x, y)
                    if abs(self.pos[2] - clear) > 0.001:     # (the first point, from a start above the clearance)
                        self.check()
                        self.move(z=clear)
                    self.check()
                    self.set(phase="probing")
                    got = self.machine.touch_line(touch, touch_time, probe=True)
                    with self.lock:
                        self.z[i, j] = got["z"]
                        self.last_z = got["z"]
                    self.log("info", f"Touch scan point {n} of {total}: X{x:.4f} Y{y:.4f} Z{got['z']:.4f}")
                    self.changed()
                    self.pos[2] = got["z"]
                    self.check()
                    self.set(phase="retracting")
                    self.move(z=clear)
                    if n < total:
                        self.pause_here(n, total)
            except ProbeFailed as e:
                return "failed", self.probe_failed(e, n, x, y, depth)
            except SpindleOn:
                return "failed", self.spindle_on(n, total)
            except Exception as e:
                if self.stopped(e):
                    return "stopped", f"{STOPPED}{self.raise_head()}"
                return "failed", f"Touch scan failed at point {n} of {total}: {_sentence(e)}"

            # every point is in
            text = xyz_text(xs, ys, self.z, self.setup())
            with self.lock:
                self.text = text
                self.clip = True
            self.auto_save(text)
            self.complete()
            took = int(time.time() - self.started)
            return "done", f"Touch scan complete: {total} points in {took // 60}:{took % 60:02d}"
        finally:
            with self.lock:
                self.at = None

    def pause_here(self, n, total):
        """Between points (the bit at the clearance): if Pause came, wait here for Resume (or Stop)."""
        with self.lock:
            if not self.pausing or self.stopping:
                return
            self.paused = True
            self.phase = "paused"
        self.log("info", f"Touch scan paused at point {n} of {total} (Z at the clearance). Resume goes on with point "
                         f"{n + 1}.")
        self.changed()
        with self.lock:
            while self.paused and not self.stopping and getattr(self.machine, "activity_stop", False) is not True:
                self.wake.wait(0.2)
            stop = self.stopping or getattr(self.machine, "activity_stop", False) is True
            self.paused = self.pausing = False
            if not stop:
                self.phase = "moving"
        if stop:
            raise _Stop()

    def probe_failed(self, e, n, x, y, depth):
        """The message for a touch that failed: no contact (ALARM:5), or the input closed before it (ALARM:4)."""
        point = f"point {n} (X{x:.4f} Y{y:.4f})"
        if e.code == 4:
            return (f"Probe input already closed at {point}, before the touch: is the bit on the copper at the "
                    "clearance height, or the clip touching the bit? FluidNC is in ALARM:4 now: Unlock, then raise Z.")
        return (f"No contact at {point}: is the clip on the copper? The bit went down to Z {-depth:.4f} without "
                "touching. FluidNC is in ALARM:5 now: Unlock, then raise Z.")

    def spindle_on(self, n, total):
        """The spindle came on during the scan: the machine is stopped at once (machine.stop: the hold, then the reset,
        which turns the spindle off). Returns the message."""
        with self.lock:
            self.stopping = True            # (machine.stop calls stop() here: nothing more to do there)
        try:
            self.machine.stop()
        except Exception as e:
            self.log("error", f"Touch scan: the machine could not be stopped ({e})")
        return (f"Touch scan stopped at point {n} of {total}: the spindle came on. FluidNC was stopped (Hold, then "
                "Soft Reset, which turns it off). Never run a touch scan with the spindle on.")

    def raise_head(self):
        """After a Stop: Z all the way up (machine.scan_raise). Returns what to add to the message (with a space)."""
        up = getattr(self.machine, "scan_raise", None)
        if up is None:
            return ""
        try:
            return f" {up()}"
        except Exception as e:              # (a bug there: the scan still ends)
            traceback.print_exc()
            return f" Raise Z by hand: it could not be raised ({e!r})."

    def setup(self):
        """The setup line for the scan's file (scan.py): FluidNC's work coordinate system and offset now."""
        get = getattr(self.machine, "work_setup", None)
        if get is None:
            return None
        try:
            return setup_line(get(), time.time())
        except Exception:                   # (the scan's data matter more than this line)
            traceback.print_exc()
            return None

    def complete(self):
        """Tells the machine a new scan is complete (a leveled program goes back to its original)."""
        done = getattr(self.machine, "scan_complete", None)
        if done is None:
            return
        try:
            done()
        except Exception as e:
            traceback.print_exc()
            self.log("error", f"Touch scan: could not tell auto-level about the new scan ({e!r})")

    def auto_save(self, text):
        path = os.path.join(self.scans_dir, "last-scan.xyz")
        with self.save_lock:
            try:
                os.makedirs(self.scans_dir, exist_ok=True)
                write_file(path, text)
            except OSError as e:
                self.log("error", f"Could not save the touch scan to {path} ({e.strerror or e}); use Save Scan")
                return
        with self.lock:
            if self.text is text:
                self.saved_to = path
        self.log("info", f"Touch scan saved to {path}")
        self.changed()
