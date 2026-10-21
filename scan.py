"""
The surface scan, as the user's UGS fork does it (SurfaceScanner.java), with the probe in probe.py.

A grid of xPoints x yPoints from (minX, minY) to (maxX, maxY), in work coordinates and inches. The CNC
visits it column by column in X, zigzag in Y (up the first column, down the next ...); at each point it
stands still while the probe, which has its own Z, backs off by Z retract (ZR) and touches (P). The CNC's
Z never moves, so a scan starts only with the bit at least 0.2 in (SAFE_Z) above the work zero: the moves
go at that height. The first touch after the probe was homed reads 0.0 and the others are relative to it,
so a scan needs a homed probe; when every point is in, the probe is homed (H) and the CNC goes back to the
first point.

Over travel (the probe found no surface) or any other failure ends the scan: the probe is aborted and
homed, and the message says what happened (UGS's words). Stop aborts the probe at once and safe-stops the
CNC. The result is written as soon as the last point is in, to scans/last-scan.xyz next to this file
(FLUIDSENDER_SCANS overrides the folder), in exactly the format UGS saves (SaveScannedSurfaceAction.java):
four "#" header lines, then "x y z" per point, by X then Y ascending, numbers as Java's Double.toString
writes them. FluidSender adds a fifth "#" line, the setup the scan was taken in, for auto-level's same-setup
check (leveling.py): "# wcs=G54 wcoX=-5.905512 wcoY=-3.937008 wcoZ=-1.181102 time=1760700000", FluidNC's work
coordinate system and work offset (in) and the unix time (UGS reads only its own keys from the "#" lines). Once a
scan is complete, a leveled program goes back to its original (machine.scan_complete).
"""

import math
import os
import re
import threading
import time
import traceback
from decimal import Decimal

from leveling import setup_line
from machine import SAFE_Z, MachineError, fixed
from probe import NO_HOME_REPLY, ProbeAborted, ProbeError, ProbeOverTravel, java_fixed

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS = {"minX": 0.5, "minY": 1.0, "maxX": 8.5, "maxY": 12.0, "xPoints": 15, "yPoints": 5, "zRetract": 0.2}
LABELS = {"minX": "Min X", "minY": "Min Y", "maxX": "Max X", "maxY": "Max Y", "xPoints": "Points X",
          "yPoints": "Points Y", "zRetract": "Z retract"}
MAX_AXIS_POINTS = 100
MAX_POINTS = 2000
ZR_MIN, ZR_MAX = 0.01, 2.0
MAX_COORD = 100.0       # in: no work position further out than this is meant
MOVE_MIN = 15.0         # s: a move to the next point may take this long at least (UGS) ...
RAPID = 50.0            # ... or 3x its time at this speed (in/min; FluidNC's max rates if known) + 2 s
FAR = 50.0              # in: the distance to assume when the start position isn't known
OWNER = "scan"
STOPPED = "Scan stopped. Home the probe before scanning again."
STOPPED_DONE = ("Scan complete, data kept. The final probe retract / return move was stopped. "
                "Home the probe before moving the machine.")
NUM = re.compile(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*")


# ------------------------------------------------------------------ settings, grid, file (no state)

def _number(v, key):
    if isinstance(v, str) and NUM.fullmatch(v):
        v = float(v)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise MachineError(f"{LABELS[key]} must be a number")
    return float(v)


def check_settings(data=None, base=None):
    """The scan settings: data (all or some of DEFAULTS' keys) over base (the saved ones) over the defaults,
    checked. Returns all of them; raises MachineError (400) with what is wrong."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise MachineError("Scan settings must be an object")
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise MachineError(f"Unknown scan setting: {unknown[0]}")
    out = dict(DEFAULTS)
    if isinstance(base, dict):
        out.update((k, base[k]) for k in DEFAULTS if k in base)
    out.update(data)
    for key in ("minX", "maxX", "minY", "maxY"):
        out[key] = _number(out[key], key)
        if abs(out[key]) > MAX_COORD:
            raise MachineError(f"{LABELS[key]} must be from -{MAX_COORD:g} to {MAX_COORD:g} in")
    for key, axis in (("xPoints", "X"), ("yPoints", "Y")):
        v = _number(out[key], key)
        if not v.is_integer() or not 1 <= v <= MAX_AXIS_POINTS:
            raise MachineError(f"Points {axis} must be a whole number from 1 to {MAX_AXIS_POINTS}")
        out[key] = int(v)
    for axis in "XY":
        lo, hi, n = out["min" + axis], out["max" + axis], out[axis.lower() + "Points"]
        if lo > hi:
            raise MachineError(f"Min {axis} ({lo:g}) is more than Max {axis} ({hi:g})")
        if lo == hi and n > 1:
            raise MachineError(f"Min {axis} and Max {axis} are the same: that takes 1 point in {axis}")
    total = out["xPoints"] * out["yPoints"]
    if total > MAX_POINTS:
        raise MachineError(f"Too many points: {out['xPoints']} x {out['yPoints']} = {total} ({MAX_POINTS} at most)")
    out["zRetract"] = _number(out["zRetract"], "zRetract")
    if not ZR_MIN <= out["zRetract"] <= ZR_MAX:
        raise MachineError(f"Z retract must be from {ZR_MIN:g} to {ZR_MAX:g} in")
    return out


def load_settings(raw):
    """Saved settings as check_settings has them; the defaults if they are missing or don't pass."""
    if isinstance(raw, dict):
        try:
            return check_settings({k: raw[k] for k in DEFAULTS if k in raw})
        except MachineError:
            pass
    return dict(DEFAULTS)


def axis_values(lo, hi, n):
    """The grid positions along one axis, computed as UGS does (so the file has the very same numbers)."""
    span = hi - lo
    return [lo + ((span * i) / (n - 1) if n > 1 else 0) for i in range(n)]


def probe_order(nx, ny):
    """(x index, y index) in the order the points are probed: column by column, zigzag in Y."""
    order, j, step = [], 0, 1
    for i in range(nx):
        while 0 <= j < ny:
            order.append((i, j))
            j += step
        step = -step
        j += step
    return order


def java_double(v):
    """Double.toString(v) as Java 17 writes it (UGS wrote the user's .xyz files with it): the shortest digits
    that read back as v, as Python's repr has them (Java before 19 differs only for rare huge or tiny values
    like 2e23, never for a scan's), plain from 0.001 up to 10^7 with at least one digit after the point
    ("12.0"), else like "5.0E-4"."""
    if not math.isfinite(v):
        return "NaN" if v != v else ("Infinity" if v > 0 else "-Infinity")
    if v == 0:
        return "-0.0" if math.copysign(1.0, v) < 0 else "0.0"
    sign = "-" if v < 0 else ""
    t = Decimal(repr(abs(v))).as_tuple()
    digits = "".join(map(str, t.digits)).rstrip("0") or "0"
    exp = len(t.digits) + t.exponent - 1        # the power of ten of the first digit
    if 1e-3 <= abs(v) < 1e7:
        if exp >= 0:
            return f"{sign}{digits[:exp + 1].ljust(exp + 1, '0')}.{digits[exp + 1:] or '0'}"
        return f"{sign}0.{'0' * (-exp - 1)}{digits}"
    return f"{sign}{digits[0]}.{digits[1:] or '0'}E{exp}"


def xyz_text(xs, ys, z, setup=None):
    """The .xyz file UGS writes for the grid xs x ys with z[(i, j)] (inches); setup: FluidSender's setup line
    (leveling.setup_line), after UGS's header lines, or None."""
    out = [f"# minX={java_fixed(xs[0], 5)} maxX={java_fixed(xs[-1], 5)}",
           f"# minY={java_fixed(ys[0], 5)} maxY={java_fixed(ys[-1], 5)}",
           f"# xSamples={len(xs)} ySamples={len(ys)}",
           "# units=INCH"]
    if setup:
        out.append(setup)
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            out.append(f"{java_double(x)} {java_double(y)} {java_double(z[i, j])}")
    return "\n".join(out) + "\n"


def write_file(path, text):
    """Writes text to path through a temporary file, so a failed write can't leave half a scan."""
    tmp = path + ".saving"
    try:
        with open(tmp, "w", encoding="ascii", newline="") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass                        # (not every file system has it: the share may not)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _real(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _sentence(e):
    text = str(e).strip() or repr(e)
    return text if text[-1] in ".!?" else text + "."


class _Stop(Exception):
    """The user stopped the scan."""


class _OverTravel(Exception):
    """The probe failed in a way UGS treats as over travel."""


# ------------------------------------------------------------------ the scanner

class Scanner:
    """One scan at a time, in its own thread. start(settings) / stop() / state(); save(path) and
    save_settings(data) for the page. machine provides begin_activity, end_activity, scan_move(x, y, timeout),
    stop() and status_now() (the work Z a scan starts at), and, if it has them, snapshot() for the max rates and
    position the move timeouts count with, activity_stop, True once its own Stop came, work_setup() for the file's
    setup line and scan_complete(), called once every point is in (auto-level, leveling.py); settings is the
    settings store (get("scan") / set("scan", value)); emit gets {"type": "scan", "scan": state()} on every
    point and change; log(kind, text) is the console. No lock of this class is held while any of them is
    called."""

    def __init__(self, machine, probe, settings, emit, log, scans_dir=None):
        self.machine = machine
        self.probe = probe
        self.store = settings
        self.emit = emit
        self._log = log
        self.scans_dir = scans_dir or os.environ.get("FLUIDSENDER_SCANS") or os.path.join(HERE, "scans")
        self.lock = threading.Lock()        # guards the state below
        self.save_lock = threading.Lock()   # one file write at a time
        self.thread = None
        self.starting = False
        self.running = False
        self.ending = False                 # the scan's work is over (only the bookkeeping is left)
        self.stopping = False               # stop() came for the scan starting or running
        self.phase = None                   # starting, moving, probing, homing, returning, stopping;
                                            # then done, stopped or failed
        self.settings = None                # the running or last scan's settings
        self.xs = self.ys = None
        self.order = []
        self.z = {}                         # (x index, y index) -> Z
        self.at = None                      # the point being moved to or probed
        self.last_z = None
        self.text = None                    # the .xyz text, once every point is in
        self.error = None
        self.message = None
        self.saved_to = None
        self.started = self.finished = None
        self.pos = None                     # where the machine is (for the next move's timeout)
        self.rate = RAPID                   # in/min: the slowest X/Y max rate (likewise)

    # -------------------------------------------------- state and settings

    def saved_settings(self):
        try:
            raw = self.store.get("scan") if self.store is not None else None
        except Exception:                   # (a broken store must not break the snapshot)
            traceback.print_exc()
            raw = None
        return load_settings(raw)

    def save_settings(self, data):
        """Checks and saves the scan settings (POST /api/scan/settings). Returns {"settings": all of them}."""
        settings = check_settings(data, self.saved_settings())
        self.remember(settings)
        self.changed()
        return {"settings": settings}

    def remember(self, settings):
        if hasattr(self.store, "set"):
            self.store.set("scan", dict(settings))
        elif isinstance(self.store, dict):
            self.store["scan"] = dict(settings)

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
                    "result": result, "error": self.error, "message": self.message, "savedTo": self.saved_to,
                    "started": self.started, "finished": self.finished}

    def last_scan(self):
        """This session's complete scan, for auto-level ("the last scan"): {"text" (the .xyz), "savedTo",
        "finished"}, or None if no scan is complete."""
        with self.lock:
            if self.text is None:
                return None
            return {"text": self.text, "savedTo": self.saved_to, "finished": self.finished}

    def changed(self):
        if self.emit:
            try:
                self.emit({"type": "scan", "scan": self.state()})
            except Exception:               # (the scan must go on, and end cleanly)
                traceback.print_exc()

    def log(self, kind, text):
        try:
            self._log(kind, text)
        except Exception:
            traceback.print_exc()

    # -------------------------------------------------- start and stop

    def start(self, settings=None):
        """Starts a scan with these settings (over the saved ones). Refused (MachineError) if the settings are
        bad, a scan runs, the probe isn't connected, busy or homed, the machine refuses the scan, or the bit is
        lower than SAFE_Z above the work zero (see clearance)."""
        settings = check_settings(settings, self.saved_settings())
        with self.lock:
            if self.running or self.starting:
                raise MachineError("A scan is already running", 409)
            self.starting = True
            self.stopping = False
        try:
            ps = self.probe.state()
            if not ps["connected"]:
                raise MachineError("The probe is not connected. Connect it first.", 409)
            if ps["busy"]:
                raise MachineError("The probe is busy: wait until it is done", 409)
            if not ps["ready"]:
                raise MachineError("Probe not ready. Home the probe before scanning.", 409)
            self.probe.claim(OWNER)
            try:
                self.machine.begin_activity(OWNER)
            except BaseException:
                self.probe.release(OWNER)
                raise
            try:                            # (the machine is the scan's now: nothing can move Z before it runs)
                self.clearance()
            except BaseException:
                self.probe.release(OWNER)
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
                self.thread = threading.Thread(target=self.run, name="scan", daemon=True)
            try:
                self.thread.start()
            except BaseException:
                with self.lock:
                    self.running = False
                    self.phase = None
                self.probe.release(OWNER)
                self.end_activity()
                raise
        finally:
            with self.lock:
                self.starting = False
        try:
            self.remember(settings)         # (the settings a scan ran with are kept for the next one)
        except Exception as e:
            self.log("error", f"Could not save the scan settings ({e})")
        self.changed()
        return {"points": len(xs) * len(ys)}

    def stop(self, stop_machine=True):
        """Stops the scan from any thread, at any time: aborts the probe at once, then safe-stops the CNC
        (machine.stop(); stop_machine=False when the caller is machine.stop itself). Returns False if no scan
        was starting or running (or it is stopping already)."""
        with self.lock:
            if not (self.running or self.starting) or self.stopping or self.ending:
                return False
            self.stopping = True
            if self.running:
                self.phase = "stopping"
        self.probe.abort()
        self.log("info", "Stopping the scan")
        self.changed()
        if stop_machine:
            try:
                self.machine.stop()
            except MachineError as e:       # (not connected, nothing moving ...)
                self.log("info", f"Scan stop: {e}")
            except Exception as e:          # a bug there: the scan still stops (its next move is refused or ends)
                traceback.print_exc()
                self.log("error", f"Scan stop: the machine could not be stopped ({e!r})")
        return True

    def shutdown(self, timeout=12.0):
        """The server is going away: stop a scan and wait for its thread (bounded)."""
        self.stop()
        thread = self.thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def save(self, path):
        """Writes the last complete scan to path (.xyz only; POST /api/scan/save). Returns {"path", "size"}."""
        path = os.path.abspath(os.path.expanduser(path if isinstance(path, str) else ""))
        if not path.lower().endswith(".xyz"):
            raise MachineError("Scans are saved as .xyz files")
        with self.lock:
            text = self.text
        if text is None:
            raise MachineError("No complete scan to save yet", 409)
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
        self.log("info", f"Scan saved to {path}")
        self.changed()
        return {"path": path, "size": len(text)}

    def end_activity(self):
        try:
            self.machine.end_activity(OWNER)
        except Exception as e:
            self.log("error", f"Scan: could not end the scan on the machine ({e})")

    def clearance(self):
        """The CNC's Z never moves during a scan, so every move (G0 X.. Y..) goes at the Z the bit is at now: it
        must be SAFE_Z (Go to XY 0's lift height) or more above the work zero. Else MachineError (409)."""
        wpos = (self.machine.status_now() or {}).get("wpos")
        if not (isinstance(wpos, list) and len(wpos) >= 3 and _real(wpos[2])):
            raise MachineError("Work position not known yet; try again in a second", 409)
        z = round(wpos[2], 4) + 0.0         # (no "-0.0000")
        if z < SAFE_Z:
            raise MachineError(f"Z is at {z:.4f} in (work): raise Z so the bit clears the work ({fixed(SAFE_Z)} in or "
                               "more above work zero), then Start Scan.", 409)

    # -------------------------------------------------- the scan thread

    def run(self):
        outcome = None
        try:
            outcome = self.scan()
        except BaseException as e:          # a bug: say so and stop the probe, never leave the machine taken
            traceback.print_exc()
            self.probe.abort()
            outcome = ("failed", f"Scan failed: internal error in FluidSender ({e!r}). Check the probe.")
        finally:
            with self.lock:
                self.ending = True
            phase, message = outcome or ("failed", "Scan failed")
            self.probe.release(OWNER)
            self.end_activity()
            with self.lock:
                self.running = self.stopping = self.ending = False
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
        """Did e come from a stop: stop() here, or the machine's own Stop? (machine.stop() sets activity_stop
        and wakes scan_move before it calls stop() here, so the move can fail first.)"""
        with self.lock:
            if self.stopping or isinstance(e, _Stop):
                return True
        if getattr(self.machine, "activity_stop", False) is True:
            self.probe.abort()          # (stop() may never come: the next scan must start from a homed probe)
            return True
        return False

    def set(self, **kw):
        with self.lock:
            if not self.stopping:
                for k, v in kw.items():
                    setattr(self, k, v)
        self.changed()

    def where(self):
        """The slowest X/Y max rate (in/min) and the work position now, from the machine (once a scan): only
        the move timeouts depend on them."""
        rate, pos = RAPID, None
        try:
            snap = self.machine.snapshot()
            rates = [((snap.get("axes") or {}).get(a) or {}).get("maxRate") for a in "xy"]
            rates = [r for r in rates if _real(r) and r > 0]
            rate = min(rates) if rates else RAPID
            wpos = (snap.get("status") or {}).get("wpos")
            if wpos and len(wpos) >= 2 and _real(wpos[0]) and _real(wpos[1]):
                pos = (wpos[0], wpos[1])
        except Exception:
            pass
        return rate, pos

    def rapid(self):
        """FluidNC's rapid override now as a fraction (1.0 when not known): the scan moves are G0s, which it slows
        down (25 % stays set after a job without M2/M30, say)."""
        try:
            ov = (self.machine.status_now() or {}).get("ov")
            r = ov[1] if isinstance(ov, (list, tuple)) and len(ov) >= 2 else None
        except Exception:
            r = None
        return r / 100.0 if _real(r) and 0 < r <= 100 else 1.0

    def move(self, x, y):
        """machine.scan_move to (x, y), with UGS's timeout: 3x the expected time + 2 s, 15 s at least. The time counts
        with FluidNC's rapid override (see rapid)."""
        dist = math.hypot(x - self.pos[0], y - self.pos[1]) if self.pos else FAR
        timeout = max(MOVE_MIN, dist / (self.rate * self.rapid()) * 60.0 * 3.0 + 2.0)
        got = self.machine.scan_move(x, y, timeout)
        if isinstance(got, dict) and _real(got.get("x")) and _real(got.get("y")):
            self.pos = (got["x"], got["y"])     # (where FluidNC says it is)
        else:
            self.pos = (x, y)

    def scan(self):
        """The scan itself. Returns (phase, message) for the end."""
        s, xs, ys, order = self.settings, self.xs, self.ys, self.order
        total = len(order)
        self.rate, self.pos = self.where()
        self.log("info", f"Scan: {s['xPoints']} x {s['yPoints']} = {total} points, X {s['minX']:.4f} to "
                         f"{s['maxX']:.4f}, Y {s['minY']:.4f} to {s['maxY']:.4f} in, Z retract {s['zRetract']:.3f} in")
        try:
            try:
                self.check()
                self.set(phase="moving", at={"n": 1, "x": xs[0], "y": ys[0]})
                self.move(xs[0], ys[0])         # to the first point (UGS: moveToSafeStartPoint)
                for n, (i, j) in enumerate(order, 1):
                    x, y = xs[i], ys[j]
                    self.check()
                    self.set(phase="moving", at={"n": n, "x": x, "y": y})
                    self.move(x, y)
                    self.check()
                    self.set(phase="probing")
                    try:
                        self.probe.set_retract(s["zRetract"], owner=OWNER)
                    except ProbeAborted:
                        raise
                    except ProbeError as e:     # (UGS: any ZR failure is an over travel)
                        raise _OverTravel(str(e))
                    self.check()
                    z = self.probe.probe(owner=OWNER)
                    with self.lock:
                        self.z[i, j] = z
                        self.last_z = z
                    self.log("info", f"Scan point {n} of {total}: X{x:.4f} Y{y:.4f} Z{z:.4f}")
                    self.changed()
            except Exception as e:
                if self.stopped(e):
                    return "stopped", STOPPED
                if isinstance(e, (ProbeOverTravel, _OverTravel)):
                    return "failed", self.over_travel(str(e))
                return "failed", f"Scan failed: {_sentence(e)} {self.retract()}"

            # every point is in: keep it whatever happens next (UGS keeps it too)
            text = xyz_text(xs, ys, self.z, self.setup())
            with self.lock:
                self.text = text
            self.auto_save(text)
            self.complete()
            try:
                self.check()
                self.set(phase="homing", at=None)
                try:
                    self.probe.home(owner=OWNER)
                except ProbeAborted:
                    raise
                except ProbeError as e:     # (UGS's words for the final H)
                    if e.reply is None:
                        raise RuntimeError("No response from probe on final retract (H)."
                                           if str(e) == NO_HOME_REPLY else str(e))
                    r = e.reply.strip()
                    if r.upper() in ("E", "ERROR"):
                        raise RuntimeError(f"Probe error on final retract (H): {r}")
                    raise RuntimeError(f"Unexpected probe response on final retract (H): {r}")
                self.check()
                self.set(phase="returning", at={"n": 1, "x": xs[0], "y": ys[0]})
                self.move(xs[0], ys[0])
            except Exception as e:
                if self.stopped(e):
                    return "stopped", STOPPED_DONE
                return "failed", ("Scan failed: the final probe retract / return did not finish (scan data was "
                                  f"kept): {_sentence(e)}")
            took = int(time.time() - self.started)
            return "done", f"Scan complete: {total} points in {took // 60}:{took % 60:02d}"
        finally:
            with self.lock:
                self.at = None

    def over_travel(self, reason):
        """UGS's over travel message, after homing the probe."""
        self.set(phase="homing", at=None)
        self.probe.abort()
        try:
            self.probe.home(owner=OWNER)
            return f"Over Travel Error: {_sentence(reason)} Probe retracted, scan cancelled."
        except MachineError as e:
            return (f"Over Travel Error: {_sentence(reason)} Probe did NOT confirm retract ({self.why(e)}). "
                    "Check the probe before moving the machine. Scan cancelled.")

    def retract(self):
        """After any other failure: abort and home the probe (when possible). Returns what to add to the message."""
        self.set(phase="homing", at=None)
        self.probe.abort()
        try:
            self.probe.home(owner=OWNER)
            return "Probe retracted."
        except MachineError as e:
            return f"Probe did NOT confirm retract ({self.why(e)}). Check the probe before moving the machine."

    @staticmethod
    def why(e):
        reply = getattr(e, "reply", None)
        return f"response: {reply.strip()}" if reply is not None else str(e).rstrip(".")

    def setup(self):
        """The setup line for the scan's file (see the top): FluidNC's work coordinate system and offset now
        (machine.work_setup; nothing can change them during the scan), or None if the machine doesn't say."""
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
            self.log("error", f"Scan: could not tell auto-level about the new scan ({e!r})")

    def auto_save(self, text):
        path = os.path.join(self.scans_dir, "last-scan.xyz")
        with self.save_lock:
            try:
                os.makedirs(self.scans_dir, exist_ok=True)
                write_file(path, text)
            except OSError as e:
                self.log("error", f"Could not save the scan to {path} ({e.strerror or e}); use Save Scan")
                return
        with self.lock:
            if self.text is text:
                self.saved_to = path
        self.log("info", f"Scan saved to {path}")
        self.changed()
