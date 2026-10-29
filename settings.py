"""
FluidSender's own settings: a small JSON file, <FluidSender dir>/settings.json (the environment variable
FLUIDSENDER_SETTINGS names another file; the tests use a temporary one).

Python standard library only. Keys:
    "jog"          {"step": 0.1, "feed": 150}: the jog step (in) and speed (in/min) the page and the pendant share
    "scan"         the surface scan settings (inches): minX, minY, maxX, maxY, xPoints, yPoints, zRetract, and
                   probeOffX / probeOffY, where the probe sits from the spindle centerline (the user's: Y -3)
    "probePort"    the probe's port when it was last connected (or null)
    "pendantPort"  the pendant's port when it was last connected (or null)
    "zeros"        Undo Zero's records, oldest first: the last 5 zeros (page or pendant), each with the work
                   offset from before it (see machine.py). Kept over resets, reconnects and restarts, as FluidNC
                   keeps its work offsets
    "level"        the last auto-level's settings (leveling.py): {"zSurface": 0, "arcSegmentMm": 0.1}, the Z
                   surface (in) and the arc segment length (mm); not in the file until a leveling saves them
                   (get("level") is None until then: LEVEL is used)
The file is written at once when something changes, atomically (a temporary file, then os.replace), so a
crash never leaves half a file. A missing, unknown or bad value falls back to its default: a damaged file
never stops FluidSender (a damaged zero record is left out, the others kept).

    s = Settings()                  the file (Settings(None): in memory only, never written)
    s.get("jog")                    a copy of the value
    s.set("jog", {...})             checked (SettingsError with a message for the user), kept, written
    s.update("scan", {"xPoints": 9})    some fields of a dict value changed, the rest kept
"""

import copy
import json
import math
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
STEPS = (10, 1, 0.1, 0.01, 0.001)           # the jog steps there are buttons for (inches)
MAX_FEED = 10000.0                          # in/min: any jog speed above this is a mistake
JOG = {"step": 0.1, "feed": 150}            # the user's UGS settings: 0.1 in at 150 in/min
SCAN = {"minX": 0.5, "minY": 1.0, "maxX": 8.5, "maxY": 12.0, "xPoints": 15, "yPoints": 5, "zRetract": 0.2,
        "probeOffX": 0.0, "probeOffY": 0.0}
MAX_OFFSET = 12.0                           # in: the probe's offset from the spindle centerline (the user's: Y -3)
MAX_POINTS = 100                            # per axis
MAX_TOTAL = 2000                            # points in a scan
MAX_COORD = 100.0                           # in: a scan corner further out is a mistake
ZERO_KEEP = 5                               # zeros Undo Zero can go back through
ZERO_SOURCES = ("page", "pendant")          # where a zero came from
MAX_ZERO = 100.0                            # in: the largest work position a zero sets (as machine.py)
MAX_WCO = 100000.0                          # mm: a work offset further out than this is damage
LEVEL = {"zSurface": 0.0, "arcSegmentMm": 0.1}  # auto-level: the user's UGS Z surface; 0.1 mm arc pieces (leveling.py)
Z_SURFACE_MAX = 10.0                        # in
ARC_MIN, ARC_MAX = 0.01, 2.0                # mm
DEFAULTS = {"jog": JOG, "scan": SCAN, "probePort": None, "pendantPort": None, "zeros": []}   # ("level": see above)
_DEFAULT_FILE = object()


class SettingsError(ValueError):
    """A value that can't be kept; the message is for the user."""


def default_path():
    return os.environ.get("FLUIDSENDER_SETTINGS") or os.path.join(HERE, "settings.json")


def _number(v):
    """A JSON number as a finite float, else None (true/false, text, nan...)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        v = float(v)
    except OverflowError:
        return None
    return v if math.isfinite(v) else None


def _num(v):
    """150.0 -> 150 (as the page and the JSON file show it), 0.1 stays 0.1."""
    return int(v) if v == int(v) else v


def jog_step(v):
    """One of STEPS, else SettingsError."""
    n = _number(v)
    step = next((s for s in STEPS if n is not None and abs(n - s) < 1e-9), None)
    if step is None:
        raise SettingsError("Jog step: one of " + ", ".join(map(str, STEPS)) + " in")
    return step


def jog_feed(v):
    """A speed above 0 and up to MAX_FEED in/min, to 1 decimal, else SettingsError."""
    n = _number(v)
    if n is None or not 0 < n <= MAX_FEED:
        raise SettingsError(f"Jog speed: a number above 0 and up to {MAX_FEED:g} in/min")
    return _num(max(round(n, 1), 0.1))


def check_jog(value):
    if not isinstance(value, dict):
        raise SettingsError("Jog settings: give step and feed")
    return {"step": jog_step(value.get("step")), "feed": jog_feed(value.get("feed"))}


def check_scan(value):
    """The scan settings, checked: min <= max on each axis (equal only with 1 point there), 1 to 100 points
    per axis and at most 2000 in all, Z retract 0.01 to 2 in."""
    if not isinstance(value, dict):
        raise SettingsError("Scan settings: give minX, minY, maxX, maxY, xPoints, yPoints and zRetract")
    out = {}
    for key in ("minX", "minY", "maxX", "maxY"):
        v = _number(value.get(key))
        if v is None or abs(v) > MAX_COORD:
            raise SettingsError(f"Scan {key}: a number from -{MAX_COORD:g} to {MAX_COORD:g} in")
        out[key] = v                        # (as given: the grid is computed from these, as UGS does)
    for key in ("xPoints", "yPoints"):
        v = _number(value.get(key))
        if v is None or v != int(v) or not 1 <= v <= MAX_POINTS:
            raise SettingsError(f"Scan {key}: a whole number from 1 to {MAX_POINTS}")
        out[key] = int(v)
    v = _number(value.get("zRetract"))
    if v is None or not 0.01 <= v <= 2:
        raise SettingsError("Scan zRetract: a number from 0.01 to 2 in")
    out["zRetract"] = v
    for axis in "XY":
        lo, hi, n = out[f"min{axis}"], out[f"max{axis}"], out[f"{axis.lower()}Points"]
        if lo > hi or (lo == hi and n > 1):
            raise SettingsError(f"Scan: min {axis} must be below max {axis} (the same only with 1 point in {axis})")
    if out["xPoints"] * out["yPoints"] > MAX_TOTAL:
        raise SettingsError(f"Scan: {out['xPoints'] * out['yPoints']} points; at most {MAX_TOTAL}")
    for key in ("probeOffX", "probeOffY"):          # (0 when missing: files from before the probe offset)
        v = _number(value.get(key, 0.0))
        if v is None or abs(v) > MAX_OFFSET:
            raise SettingsError(f"Scan {key}: a number from -{MAX_OFFSET:g} to {MAX_OFFSET:g} in")
        out[key] = v
    return out


def check_port(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise SettingsError("Port: a device path such as /dev/rfcomm1")
    return value.strip()


def check_zero(r):
    """One zero record as machine.py keeps it, as a new dict, or None if it isn't one: "axes" ("XZ": X, Y, Z
    each once, in that order), "wcs" (1 = G54 ... 6 = G59), "wco" (the work offset X Y Z before the zero,
    machine coordinates in mm), "value" (in), "t" (unix time), "source" ("page", "pendant"), "text" (the work
    position before it, for the page)."""
    if not isinstance(r, dict):
        return None
    axes, wcs, wco, text = r.get("axes"), r.get("wcs"), r.get("wco"), r.get("text")
    value, t = _number(r.get("value")), _number(r.get("t"))
    if not (isinstance(axes, str) and axes and axes == "".join(a for a in "XYZ" if a in axes)):
        return None
    if isinstance(wcs, bool) or not isinstance(wcs, int) or not 1 <= wcs <= 6:
        return None
    if not isinstance(wco, list) or len(wco) != 3:
        return None
    wco = [_number(v) for v in wco]
    if any(v is None or abs(v) > MAX_WCO for v in wco):
        return None
    if value is None or abs(value) > MAX_ZERO or t is None or t < 0:
        return None
    if r.get("source") not in ZERO_SOURCES or not isinstance(text, str) or len(text) > 200:
        return None
    return {"axes": axes, "wcs": wcs, "wco": wco, "value": value, "t": t, "source": r["source"], "text": text}


def check_zeros(value):
    """Undo Zero's records, oldest first: the ones that aren't records are left out, the newest ZERO_KEEP kept."""
    if not isinstance(value, list):
        raise SettingsError("Zeros: a list of zero records")
    return [r for r in map(check_zero, value) if r is not None][-ZERO_KEEP:]


def check_level(value):
    """The auto-level settings, checked: zSurface from -10 to 10 in, arcSegmentMm from 0.01 to 2 mm (None: none
    saved)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SettingsError("Auto-level settings: give zSurface and arcSegmentMm")
    z, arc = _number(value.get("zSurface")), _number(value.get("arcSegmentMm"))
    if z is None or abs(z) > Z_SURFACE_MAX:
        raise SettingsError(f"Z surface: a number from -{Z_SURFACE_MAX:g} to {Z_SURFACE_MAX:g} in")
    if arc is None or not ARC_MIN <= arc <= ARC_MAX:
        raise SettingsError(f"Arc segment: a number from {ARC_MIN:g} to {ARC_MAX:g} mm")
    return {"zSurface": z + 0.0, "arcSegmentMm": arc}


CHECKS = {"jog": check_jog, "scan": check_scan, "probePort": check_port, "pendantPort": check_port,
          "zeros": check_zeros, "level": check_level}


class Settings:
    """The settings file, read once and written whenever a value changes. Thread-safe."""

    def __init__(self, path=_DEFAULT_FILE):
        self.path = default_path() if path is _DEFAULT_FILE else path
        self.lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        """Reads the file; what is missing, unknown or bad keeps its default."""
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return                              # no file yet, or not JSON: the defaults
        if not isinstance(saved, dict):
            return
        with self.lock:
            for key, check in CHECKS.items():
                if key not in saved:
                    continue
                try:
                    self.data[key] = check(saved[key])
                except SettingsError:
                    pass                        # (the default stays)

    def get(self, key, default=None):
        with self.lock:
            return copy.deepcopy(self.data[key]) if key in self.data else default

    def __getitem__(self, key):
        if key not in CHECKS:
            raise KeyError(key)
        return self.get(key)

    def __setitem__(self, key, value):
        self.set(key, value)

    def __contains__(self, key):
        return key in CHECKS

    def set(self, key, value):
        """Checks the value and keeps it (writing the file if it changed). Returns the value as kept."""
        check = CHECKS.get(key)
        if check is None:
            raise SettingsError(f"Unknown setting {key!r}")
        value = check(value)
        with self.lock:
            if key in self.data and value == self.data[key]:     # ("level" is there only once set)
                return copy.deepcopy(value)
            self.data[key] = value
            self._save()
            return copy.deepcopy(value)

    def update(self, key, fields):
        """Changes some fields of a dict setting ("jog", "scan"), keeping the others. Returns it as kept."""
        check = CHECKS.get(key)
        if check is None or not isinstance(DEFAULTS.get(key), dict):
            raise SettingsError(f"{key!r} is not a group of settings")
        if not isinstance(fields, dict):
            raise SettingsError(f"{key}: give the fields to change")
        with self.lock:                         # (read, change and write as one: two pages may change it)
            value = check(dict(self.data[key], **fields))
            if value != self.data[key]:
                self.data[key] = value
                self._save()
            return copy.deepcopy(value)

    def save(self):
        with self.lock:
            self._save()

    def _save(self):
        """Writes the file atomically (lock held). A failed write is printed, not raised: the settings are
        still used, only not kept for next time."""
        if not self.path:
            return
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            print(f"FluidSender: could not save the settings to {self.path}: {e}", flush=True)
