"""
Auto-level in FluidSender: the loaded program leveled with a surface scan, as the user's UGS does it (POST
/api/machine/level). leveler.py does the leveling itself (pure, checked against UGS); this module keeps the state
and the checks around it. Python standard library only.

Machine (machine.py) keeps one Leveling (machine.leveling); its state is guarded by Machine.lock. Leveling is on
while the loaded program is a leveled one: the leveled text, parsed into a job.Program, replaces the loaded program,
so Run (the page's and the pendant's) sends exactly the leveled lines the page shows. The leveled program carries
what it was made from (Result: the scan, the settings, leveler's lines, warnings and stats, the program line each
leveled line came from, the original program and its text, the setup the scan was taken in). It is never leveled
again by itself: Leveling off (POST {"off": true}), a new scan completing and connecting to another FluidNC turn it
off (the original program back, and a console line says why); loading a program (or an edit) replaces it ("Auto-level
off: the G-code changed"). Leveling a 55,000-line program takes about 2 s: it runs outside Machine.lock, and one
that finds the program changed when it is done (or FluidSender connected to another FluidNC meanwhile) is dropped.

The same setup: scan.py adds setup_line() to every .xyz it writes, after UGS's four header lines:
"# wcs=G54 wcoX=-5.905512 wcoY=-3.937008 wcoZ=-1.181102 time=1760700000", FluidNC's work coordinate system and
its work offset (WCO, in inches) when the scan was taken (UGS reads only xSamples, ySamples and units from the "#"
lines: the file stays UGS's). When leveling and again at Run (check_setup), the G-code is refused only if FluidNC is
in another work coordinate system. An X/Y work offset that moved more than 0.0005 in since the scan is a warning,
never a refusal: the user's probe sits 3 in from the spindle, so they shift Y zero by that to scan and put it back
before leveling and running ("Dont block me from moving the table after a scan"); if it was not put back, the
leveling is off by that much, and the warning says by how much. A Z offset that changed is a warning too: the scan's
heights are relative to its first point (the probe's first touch is 0.0), so the leveled Z is right only with Z
zeroed on the surface there, and a tool change rightly changes it. A scan without that line (UGS's, or an older
FluidSender's) can't be checked: a warning says so.

Settings (settings.json "level", saved by each leveling): zSurface (UGS's "Gcode Z surface", in, 0 by default as in
the user's UGS) and arcSegmentMm (UGS's "Arc line segment length", 0.1 mm: UGS's 0.01 mm makes about 1,700 lines a
second at F40 on an arc, more than the serial line carries; CarveMaker writes G1 only), 0.01 to 2 mm.
"""

import math
import os
import re
import time
import traceback
import weakref

import leveler
from job import Job, ProgramError, parse_program
from leveler import LevelError, ScanError
from settings import LEVEL, SettingsError, check_level

LAST = "last-scan.xyz"          # the name of this session's scan (scan.py writes it as scans/last-scan.xyz)
XY_MOVED = 0.0005               # in: a work offset X or Y that moved more than this is another X/Y zero (a warning)
Z_MOVED = 0.0005                # in: likewise Z (a warning only)
MAX_SCAN_FILE = 8 * 1024 * 1024     # bytes: a .xyz bigger than this is not a scan (100 x 100 points: 0.6 MB)
SETUP_KEYS = ("wcs", "wcoX", "wcoY", "wcoZ", "time")
KEYS = ("scan", "zSurface", "arcSegmentMm")
JOB_RUNNING = "A job is running"
BUSY = "Already leveling the G-code; try again in a second"
CHANGED = "The G-code changed while it was being leveled: level it again"
ANOTHER = "FluidSender connected to another FluidNC while the G-code was being leveled: level it again"


class LevelingError(Exception):
    """A refused level call or Run: the message is for the user, status the HTTP status (Machine raises it as a
    MachineError)."""

    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _real(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num(v):
    """0 -> '0', 0.0100 -> '0.01', -0.00001 -> '0' (for the console)."""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _offset(v):
    """A work offset in inches for the setup line: 6 decimals, never '-0.000000'."""
    s = f"{v:.6f}"
    return "0.000000" if s == "-0.000000" else s


# ------------------------------------------------------------------ settings and the scan's setup (no state)

def check_settings(data, saved=None):
    """The settings a leveling uses: zSurface and arcSegmentMm from data where it has them, else the saved ones, else
    LEVEL (0 and 0.1). LevelingError (400) if one is not usable."""
    merged = dict(LEVEL)
    if isinstance(saved, dict):
        merged.update({k: saved[k] for k in LEVEL if k in saved})
    merged.update({k: data[k] for k in LEVEL if data.get(k) is not None})
    try:
        return check_level(merged)
    except SettingsError as e:
        raise LevelingError(str(e), 400) from None


def setup_line(setup, when):
    """The header line scan.py adds to a .xyz (see the top): setup is Machine.work_setup() ({"wcs": "G54", "wco":
    [x, y, z] in inches or None}), when the unix time. None if the setup isn't known (not connected). Without a work
    offset (none reported yet) the line has only wcs and time."""
    if not setup or setup.get("wcs") not in leveler.WCS:
        return None
    parts = [f"wcs={setup['wcs']}"]
    wco = setup.get("wco")
    if isinstance(wco, (list, tuple)) and len(wco) >= 3 and all(_real(v) for v in wco[:3]):
        parts += [f"wco{a}={_offset(v)}" for a, v in zip("XYZ", wco)]
    parts.append(f"time={int(when)}")
    return "# " + " ".join(parts)


def scan_setup(header):
    """The setup a scan was taken in, from its header words (leveler.Mesh.header): {"wcs": "G54" or None, "wco":
    [x, y, z] (in) or None, "time": unix or None}; None if it has none of these words (a UGS scan)."""
    if not isinstance(header, dict) or not any(k in header for k in SETUP_KEYS):
        return None
    wcs = str(header.get("wcs", "")).upper()
    try:
        wco = [float(header[f"wco{a}"]) for a in "XYZ"]
        if not all(map(math.isfinite, wco)):
            wco = None
    except (KeyError, ValueError):
        wco = None
    try:
        when = int(header["time"])
    except (KeyError, ValueError):
        when = None
    return {"wcs": wcs if wcs in leveler.WCS else None, "wco": wco, "time": when}


def check_setup(scan, now, first, at_run):
    """The same-setup check (see the top). scan: scan_setup() of the scan (None: it has no setup line); now:
    Machine.work_setup() (None: not connected); first: the scan's first point (x, y), where Z must be zeroed; at_run:
    Run's check (when leveling, a work offset not reported yet is checked at Run). Returns (why it is refused, or
    None; [warnings]). Only another work coordinate system refuses: a moved X/Y or Z zero, or one not reported yet,
    is a warning (see the top)."""
    where = f"X{first[0]:.4f} Y{first[1]:.4f}"
    z_zero = f"Z must be zeroed on the surface at the scan's first point ({where})"
    if now is not None and scan is not None and scan["wcs"] is not None and now["wcs"] != scan["wcs"]:
        return (f"The scan was taken in {scan['wcs']} but FluidNC is in {now['wcs']} now: switch to {scan['wcs']} "
                "(or scan again)"), []
    if scan is None or scan["wco"] is None:
        why = "it has no FluidSender setup line: a UGS scan?" if scan is None else "its setup line has no work offset"
        return None, [f"This scan's setup can't be checked ({why}): X/Y zero must be the G-code's zero (any shift "
                      f"for the probe put back), and {z_zero}"]
    if now is None:
        return None, ["Not connected to FluidNC: the scan's setup (work coordinate system and offsets) is checked "
                      "at Run"]
    if now["wco"] is None:
        if at_run:
            return None, ["FluidNC hasn't reported its work offset yet: the X/Y and Z zero couldn't be compared with "
                          "the scan's"]
        return None, ["FluidNC hasn't reported its work offset yet: the scan's setup is checked at Run"]
    dx, dy, dz = (n - s for n, s in zip(now["wco"], scan["wco"]))
    notes = []
    moved = [f"{a} by {d:+.4f} in" for a, d in (("X", dx), ("Y", dy)) if abs(d) > XY_MOVED]
    if moved:
        notes.append(f"X/Y zero moved since the scan ({', '.join(moved)}): fine if you shifted it for the probe and put "
                     "it back; otherwise the leveling is off by that much")
    if abs(dz) > Z_MOVED:
        notes.append(f"Z zero changed since the scan (by {dz:+.4f} in): {z_zero}")
    return None, notes


# ------------------------------------------------------------------ what a leveling made

class Result:
    """One leveling (the leveled job.Program's record; it holds no reference to that program)."""

    def __init__(self, scan, mesh, settings, wcs, setup, notes, leveled, original, original_text, port):
        self.scan = scan                    # LAST, or the .xyz file's path
        self.time = round(time.time(), 3)   # (the page tells one leveling from another by it)
        self.settings = settings            # {"zSurface", "arcSegmentMm"}
        self.wcs = wcs                      # the work coordinate system it is leveled for
        self.setup = setup                  # scan_setup() of the scan, or None
        self.first = (mesh.xs[0], mesh.ys[0])
        self.mesh = mesh.info()
        self.stats = leveled.stats
        self.warnings = list(notes) + list(leveled.warnings)    # (the setup's first: they are about safety)
        self.source = leveled.source        # array('I'): the program line (1-based) each leveled line came from
        self.text = leveled.text()          # what runs, byte for byte what UGS's Export writes
        self.original = original            # the program as loaded (job.Program) and its text
        self.original_text = original_text
        self.port = port                    # FluidNC's port when it was leveled (None: not connected then)

    def label(self):
        """'last-scan.xyz (15 x 5 points, Z surface 0)'"""
        return (f"{os.path.basename(self.scan)} ({self.mesh['xPoints']} x {self.mesh['yPoints']} points, "
                f"Z surface {_num(self.settings['zSurface'])})")

    def info(self):
        """The "leveled" of the program event, the snapshot and GET /api/machine/program."""
        return {"scan": self.scan, "time": self.time, "zSurface": self.settings["zSurface"],
                "arcSegmentMm": self.settings["arcSegmentMm"], "stats": self.stats, "warnings": list(self.warnings),
                "wcs": self.wcs, "scanned": self.setup["time"] if self.setup else None}


class LevelJob(Job):
    """A job.Job whose info() also says "leveled": True (its program is a leveled one) or False (it isn't, though a
    complete scan exists in this session); neither when there is no scan to level with (leveled None)."""

    def __init__(self, program, now, wall, leveled=None):
        super().__init__(program, now, wall)
        self.leveled = leveled

    def info(self, now):
        out = super().info(now)
        if self.leveled is not None:
            out["leveled"] = self.leveled
        return out


# ------------------------------------------------------------------ the state

class Leveling:
    """Machine's auto-level (see the top). machine: its lock, program, job, activity, scanner, settings, log(), emit()
    and work_setup() are used."""

    def __init__(self, machine):
        self.m = machine
        self.made = weakref.WeakKeyDictionary()     # leveled job.Program -> its Result (while that program lives)
        self.loaded_text = None                     # (program, text): the loaded program as the page sent it
        self.busy = False                           # a leveling is being worked out (outside the lock)

    def result(self, program):
        """The Result of a leveled program, None for any other (or None)."""
        return self.made.get(program) if program is not None else None

    # -------------------------------------------------- Machine's hooks (lock held)

    def loaded(self, program, text):
        """load_program is replacing the loaded program with program (None: unloaded), whose text is text: a leveled
        one goes (it says so)."""
        if self.result(self.m.program) is not None:
            self.m.log("info", "Auto-level off: the G-code changed")
        self.loaded_text = (program, text) if program is not None else None

    def info(self, program):
        """program.info() with "leveled" (Result.info) if it is a leveled program; None for None."""
        if program is None:
            return None
        out = program.info()
        r = self.result(program)
        if r is not None:
            out["leveled"] = r.info()
        return out

    def snapshot(self, snap):
        """The saved settings (settings.json "level") go into the snapshot as "level", once there are any."""
        saved = self.m.settings.get("level")
        if saved:
            snap["level"] = saved

    def off(self, why=None):
        """The original program back in place of the leveled one, if one is loaded; logs why. True if it was."""
        m = self.m
        r = self.result(m.program)
        if r is None:
            return False
        m.program = r.original
        m.log("info", f"Auto-level off: {why}" if why else
              f"Auto-level off: {r.original.name} runs as it is (not leveled) again")
        m.emit({"type": "program", "program": self.info(m.program)})
        return True

    def scan_complete(self):
        """A new scan is complete: the leveled program was made with another one."""
        self.off("a new scan was made (Level again to use it)")

    def connected(self, port):
        """FluidSender connected to FluidNC on port: a leveling made on another port goes off (another FluidNC); one
        made while not connected is this FluidNC's from now on."""
        r = self.result(self.m.program)
        if r is None:
            return
        if r.port is None:
            r.port = port
        elif r.port != port:
            self.off(f"connected to another FluidNC ({port}); level the G-code again for it")

    def run_why(self, program):
        """Why Run must not start this program now, or None: a leveling is being worked out, or (a leveled program)
        FluidNC is in another work coordinate system than the scan's (see check_setup: a moved zero only warns)."""
        if self.busy:
            return "Auto-level is still leveling the G-code; try again in a second"
        r = self.result(program)
        if r is None:
            return None
        now = self.m.work_setup()
        if now is None:
            return None                     # (not connected: Run refuses anyway)
        if now["wcs"] != r.wcs:
            return (f"The G-code was leveled for {r.wcs} but FluidNC is in {now['wcs']} now: switch to {r.wcs}, or "
                    "level it again")
        return check_setup(r.setup, now, r.first, at_run=True)[0]

    def new_job(self, program, now, wall, scanned):
        """Run's job (see LevelJob); scanned: scan_exists()."""
        leveled = True if self.result(program) is not None else (False if scanned else None)
        return LevelJob(program, now, wall, leveled)

    def started(self, job, scanned):
        """Run's console lines: "Job started: x.nc, LEVELED with last-scan.xyz (15 x 5 points, Z surface 0), 55,287
        lines" and the setup's warnings now, or "..., not leveled, ..." when a complete scan exists in this session
        (scanned: scan_exists())."""
        lines = f"{job.total:,} line{'s' * (job.total != 1)}"
        r = self.result(job.program)
        if r is not None:
            notes = check_setup(r.setup, self.m.work_setup(), r.first, at_run=True)[1]
            return [f"Job started: {job.name}, LEVELED with {r.label()}, {lines}"] + [f"Auto-level: {w}" for w in notes]
        if scanned:
            return [f"Job started: {job.name}, not leveled, {lines}"]
        return [f"Job started: {job.name}, {lines}"]

    def scan_exists(self):
        """A complete scan exists in this session (the scanner has one). Not with Machine.lock held: the scanner
        takes its own lock (Machine asks the scanner for its state only without its lock held, too)."""
        last = getattr(self.m.scanner, "last_scan", None)
        try:
            return bool(last and last())
        except Exception:                   # (a broken scanner must not stop a job)
            return False

    def why_not(self):
        """Why no leveling (or Leveling off) can be done now (lock held), or None."""
        m = self.m
        if m.job is not None and m.job.active:
            return JOB_RUNNING
        if m.activity:
            return f"A {m.activity} is running"
        if self.busy:
            return BUSY
        return None

    # -------------------------------------------------- GET /api/machine/program

    def program_fields(self, program, with_text):
        """What GET /api/machine/program says of a leveled program besides its info: "leveled" (Result.info, with
        "source", the program line of each leveled line, 1-based), "text" (the leveled text) and "original" (the
        program as loaded); with_text False: "leveled" only, without "source". None for any other program."""
        with self.m.lock:
            r = self.result(program)
        if r is None:
            return None
        out = {"leveled": r.info()}
        if with_text:
            out["leveled"]["source"] = list(r.source)
            out["text"] = r.text
            out["original"] = r.original_text
        return out

    # -------------------------------------------------- POST /api/machine/level

    def level(self, data):
        """{"scan": "last" | {"path": "/abs/x.xyz"}, "zSurface": 0, "arcSegmentMm": 0.1} (settings left out: the
        saved ones) levels the loaded program (its original, if it is leveled already) and loads the leveled one.
        Returns {"lines", "total" (the leveled program's), "warnings" (the leveling's), "stats", "scan" (the mesh)}.
        {"off": true}: the original program back; returns its {"lines", "total", "warnings"}. Raises LevelingError:
        400 for a bad call; 404 for a scan file that isn't there; 409 while a job or scan runs or another leveling is
        being worked out, without a program or a complete scan, for a scan or program leveler.py refuses (its text
        names the line), in another work coordinate system than the scan's (see check_setup: a moved zero only warns),
        and when the program changed or another
        FluidNC was connected while it was being leveled."""
        if not isinstance(data, dict):
            raise LevelingError("Auto-level: give the scan and the settings", 400)
        if data.get("off") is True:
            return self.turn_off()
        unknown = sorted(set(data) - set(KEYS) - {"off"})
        if unknown:
            raise LevelingError(f"Unknown auto-level setting: {unknown[0]}", 400)
        m = self.m
        settings = check_settings(data, m.settings.get("level"))
        source = self.source(data.get("scan", "last"))
        with m.lock:
            why = self.why_not()
            program = m.program
            if not why and program is None:
                why = "No program loaded: open a G-code file first"
            if why:
                raise LevelingError(why)
            r = self.result(program)
            if r is not None:               # (leveled already: its original is leveled again)
                original, text = r.original, r.original_text
            else:
                original = program
                text = self.loaded_text[1] if self.loaded_text and self.loaded_text[0] is program else None
            if text is None:
                raise LevelingError("FluidSender doesn't have this G-code's text: open it again")
            scanner, setup = m.scanner, m.work_setup()
            self.busy = True
        try:                                # (not with the lock held: 55,000 lines take 2 s)
            mesh, name = self.read(source, scanner)
            scan = scan_setup(mesh.header)
            refused, notes = check_setup(scan, setup, (mesh.xs[0], mesh.ys[0]), at_run=False)
            if refused:
                raise LevelingError(refused)
            wcs = (scan and scan["wcs"]) or (setup and setup["wcs"]) or "G54"
            try:
                leveled = leveler.level_text(text, mesh, {"z_surface": settings["zSurface"],
                                                          "arc_segment_mm": settings["arcSegmentMm"], "wcs": wcs})
            except LevelError as e:
                raise LevelingError(str(e)) from None
            result = Result(name, mesh, settings, wcs, scan, notes, leveled, original, text,
                            setup["port"] if setup else None)
            program_out = self.parse(original.name, result)
        except BaseException as e:
            with m.lock:
                self.busy = False
            if isinstance(e, Exception) and not isinstance(e, LevelingError):    # a bug: say so, keep serving
                traceback.print_exc()
                raise LevelingError(f"Internal error in FluidSender while leveling ({e!r})", 500) from None
            raise
        with m.lock:
            self.busy = False
            if m.program is not program:
                raise LevelingError(CHANGED)
            why = self.why_not()
            now = m.work_setup()
            if not why and setup is not None and now is not None and now["port"] != setup["port"]:
                why = ANOTHER               # (connected to another FluidNC meanwhile: see connected)
            if why:
                raise LevelingError(why)
            self.made[program_out] = result
            m.program = program_out
            m.emit({"type": "program", "program": self.info(program_out)})
            arcs = f", arcs in {_num(settings['arcSegmentMm'])} mm pieces" if result.stats.get("arcs") else ""
            m.log("info", f"Auto-level: {original.name} LEVELED with {result.label()}{arcs}: {len(program_out):,} "
                          f"lines to send (from {len(original):,})")
            for w in result.warnings:
                m.log("info", f"Auto-level: {w}")
        try:
            m.settings.set("level", settings)   # (the next leveling's, and every page's)
        except (SettingsError, OSError) as e:
            m.log("error", f"Could not save the auto-level settings ({e})")
        return {"lines": len(program_out), "total": program_out.total, "warnings": list(result.warnings),
                "stats": result.stats, "scan": result.mesh}

    def turn_off(self):
        """{"off": true}: the original program back (nothing to do if the loaded one isn't leveled)."""
        with self.m.lock:
            why = self.why_not()
            if why:
                raise LevelingError(why)
            self.off()
            p = self.m.program
            if p is None:
                return {"lines": 0, "total": 0, "warnings": []}
            return {"lines": len(p), "total": p.total, "warnings": list(p.warnings)}

    @staticmethod
    def source(scan):
        """("last", None) or ("file", absolute path) from the call's "scan"; LevelingError (400) if it is neither."""
        if scan is None or scan == "last":
            return "last", None
        path = scan.get("path") if isinstance(scan, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise LevelingError('scan: "last" or {"path": "/path/to/scan.xyz"}', 400)
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not path.lower().endswith(".xyz"):
            raise LevelingError(f"Not a scan file (.xyz): {path}", 400)
        return "file", path

    @staticmethod
    def read(source, scanner):
        """The scan's Mesh (read as UGS opens one: in inches) and its name."""
        kind, path = source
        if kind == "last":
            last = getattr(scanner, "last_scan", None)
            got = last() if last else None
            if not got:
                raise LevelingError("No complete scan in this session yet: scan first, or choose a .xyz file")
            text, name = got["text"], LAST
        else:
            try:
                if os.path.getsize(path) > MAX_SCAN_FILE:
                    raise LevelingError(f"{path} is too big for a scan file")
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                raise LevelingError(f"{path}: {e.strerror or e}", 404) from None
            name = path
        try:
            return leveler.parse_scan(text), name
        except ScanError as e:
            raise LevelingError(str(e)) from None

    @staticmethod
    def parse(name, result):
        """The leveled text as a job.Program. A line that became too long for FluidNC is named by its program line."""
        try:
            return parse_program(name, result.text)
        except ProgramError as e:
            found = re.match(r"Line (\d+)(.*)", str(e), re.S)
            n = int(found.group(1)) if found else 0
            if 0 < n <= len(result.source):
                raise LevelingError(f"Line {result.source[n - 1]} (leveled line {n}){found.group(2)}") from None
            raise LevelingError(f"The leveled G-code can't be run: {e}") from None
