"""
Auto-leveling: the program's Z follows the scanned surface, as the user's UGS fork does it (surfacescanner
Utils.createCommandProcessor: ArcExpander, then LineSplitter, then MeshLeveler, per line, in that order).

Python standard library only, pure functions: parse_scan(text) / read_scan(path) give a Mesh (the .xyz that
scan.py and UGS write, read as UGS opens one: in inches, the grid rebuilt from its corners), level(lines, mesh,
settings) gives the leveled program (Leveled: .lines, and .source, the program line each output line came from)
or raises LevelError naming the line it can't level safely. Checked against UGS itself (its export run on the
fork's ugs-core): byte for byte the same on the user's job and on G0/G1, G2/G3 (I/J, R, G18/G19, helical) and
G21 programs, except where noted below.

What it does to each line, as UGS does (leveled Z = Z + height(X, Y) - Z surface, height = the bilinear blend of
the 4 scan points around X, Y, the nearest edge's outside the scan):
- G0 and G1 are both leveled. A move is leveled when X, Y and Z are known where it ends (and, for G91, where it
  starts): the first moves of a program, and moves after G53, G28/G30, G38.x, M6, G43.1/G49, a change of work
  coordinate system or a $H/$J= line, stay as they are until a move has set all three again. A line without a Z
  word gets one ("G1 X1 Y2 F40Z-0.0198"); a Z word is rewritten in place; a line with a comment loses it and its
  spaces (UGS). Lines that are not leveled, and lines without motion, are copied as they are.
- A move longer than a quarter of the scan's mean spacing is split into equal pieces (written "G1X..Y..Z..") and
  each piece is leveled; the line's other words (F, S, M3 ...) go on a line before the pieces, M0/M1/M2/M30 after.
- G2/G3 (any plane, I/J/K or R, helical, P turns) become G1 lines of at most arc_segment_mm along the arc, each
  leveled; they keep the arc's F etc. like split lines. Arcs FluidNC would refuse are refused, not drawn.
- G91 moves get the height change from their start to their end; their rounding is carried over (UGS lets it add
  up), so the machine never drifts more than 0.00005 from the leveled path.
- Numbers are written as UGS writes them (Java DecimalFormat "0.####": up to 4 decimals, "-0" kept).
Where it differs from UGS (UGS would make a wrong or broken program): it refuses what UGS leaves silently
unleveled or garbles (G92/G10 frame changes, another work coordinate system, G93, #parameters, expressions,
O-words, A/B/C ...); M0/M1 don't forget the position (FluidNC 4.1.1 holds at M0, GCode.cpp:2017-2022, and
refuses $J= unless Idle or Jog, ProcessSettings.cpp:1144 + Settings.cpp:45-47; FluidSender refuses jogs and typed
lines during a job); G91 rounding is carried over; "Z -0.5" gets one Z word (UGS adds a second one); an arc's P
is not left on a line of its own; $H/$J= lines forget the position; and the program starts in inches and
absolute, as every FluidSender job does (job.FIRST), not in UGS's millimeters.
"""

import math
import re
from array import array

from job import COMMENTS, LINE_END, SPACES, mantissa

MM_PER_INCH = 25.4
INCH_PER_MM = 1.0 / MM_PER_INCH          # UGS UnitUtils.scaleUnits(MM, INCH)
UNITS = ("INCH", "MM")
DEFAULTS = {
    "z_surface": 0.0,          # UGS "Gcode Z surface", in the mesh's units: read_scan gives inches (UGS: the
                               # preferred units, inches for the user, whose UGS has 0.0)
    "arc_segment_mm": 0.01,    # UGS "Arc line segment length (mm)" (the user's UGS: 0.01, its default)
    "wcs": "G54",              # the work coordinate system the scan was taken in
    "max_lines": 2_000_000,    # refuse a leveled program longer than this
}
ARC_EPSILON = 5e-7             # FluidNC Config.h:136 ARC_ANGULAR_TRAVEL_EPSILON (radians)
AMBIGUOUS = 20 * ARC_EPSILON   # an arc this short (rad) may be a full circle or nothing in FluidNC's float math
EDGE = 1e-6                    # a point this close to the scan's edge is inside it (scan units)
SNAP = 0.01                    # a file point this close to its grid point is it (UGS OpenScannedSurfaceAction)
SHOW = 8                       # line numbers listed in a warning

WORDS = re.compile(r"(?:[A-Za-z][-+]?(?:\d+\.?\d*|\.\d+))+")
WORD = re.compile(r"([A-Za-z])([-+]?(?:\d+\.?\d*|\.\d+))")
ZWORD = re.compile(r"Z([-+]?[0-9.]+)", re.IGNORECASE)    # UGS GcodePreprocessorUtils.overridePosition, Z

# G and M codes FluidNC 4.1.1 runs (GCode.cpp:411-677 and 679-806), by (number, mantissa) as GCode.cpp reads them
G_CODES = {(0, 0): "G0", (1, 0): "G1", (2, 0): "G2", (3, 0): "G3", (4, 0): "G4", (10, 0): "G10",
           (17, 0): "G17", (18, 0): "G18", (19, 0): "G19", (20, 0): "G20", (21, 0): "G21",
           (28, 0): "G28", (28, 10): "G28.1", (30, 0): "G30", (30, 10): "G30.1",
           (38, 20): "G38.2", (38, 30): "G38.3", (38, 40): "G38.4", (38, 50): "G38.5",
           (38, 60): "G38.6", (38, 70): "G38.7", (38, 80): "G38.8", (38, 90): "G38.9",
           (40, 0): "G40", (43, 10): "G43.1", (49, 0): "G49", (53, 0): "G53",
           (54, 0): "G54", (55, 0): "G55", (56, 0): "G56", (57, 0): "G57", (58, 0): "G58", (59, 0): "G59",
           (59, 10): "G59.1", (59, 20): "G59.2", (59, 30): "G59.3", (61, 0): "G61", (80, 0): "G80",
           (90, 0): "G90", (91, 0): "G91", (91, 10): "G91.1", (92, 0): "G92", (92, 10): "G92.1",
           (93, 0): "G93", (94, 0): "G94"}
G_GROUP = {}                   # FluidNC's modal groups: two codes of one group on a line are an error
for _group, _codes in (("motion", "G0 G1 G2 G3 G38.2 G38.3 G38.4 G38.5 G38.6 G38.7 G38.8 G38.9 G80"),
                       ("nonmodal", "G4 G10 G28 G28.1 G30 G30.1 G53 G92 G92.1"), ("plane", "G17 G18 G19"),
                       ("units", "G20 G21"), ("distance", "G90 G91"), ("arcdistance", "G91.1"),
                       ("feed", "G93 G94"), ("cutter", "G40"), ("tlo", "G43.1 G49"), ("control", "G61"),
                       ("wcs", "G54 G55 G56 G57 G58 G59 G59.1 G59.2 G59.3")):
    G_GROUP.update(dict.fromkeys(_codes.split(), _group))
M_CODES = {(0, 0): "M0", (1, 0): "M1", (2, 0): "M2", (30, 0): "M30", (3, 0): "M3", (4, 0): "M4", (5, 0): "M5",
           (6, 0): "M6", (7, 0): "M7", (7, 10): "M7.1", (8, 0): "M8", (8, 10): "M8.1", (9, 0): "M9", (56, 0): "M56",
           (61, 0): "M61", (62, 0): "M62", (63, 0): "M63", (64, 0): "M64", (65, 0): "M65", (66, 0): "M66",
           (67, 0): "M67", (68, 0): "M68"}
M_GROUP = {}
for _group, _codes in (("stop", "M0 M1 M2 M30"), ("spindle", "M3 M4 M5"), ("tool", "M6 M61"),
                       ("coolant", "M7 M7.1 M8 M8.1 M9"), ("override", "M56"),
                       ("io", "M62 M63 M64 M65 M66 M67 M68")):
    M_GROUP.update(dict.fromkeys(_codes.split(), _group))
STOPS = {"M0", "M1", "M2", "M30"}      # run after the line's motion (UGS CoordinateRewriteGuard.splitProgramStops)
VALUES = set("XYZIJKRPLQEDNFST")       # the other letters FluidNC takes on a 3-axis machine
MOTION = {"G0", "G1", "G2", "G3"}
PROBES = {"G38.2", "G38.3", "G38.4", "G38.5", "G38.6", "G38.7", "G38.8", "G38.9"}
WCS = ("G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3")
PLANES = {"G17": (0, 1, 2), "G18": (2, 0, 1), "G19": (1, 2, 0)}    # axis_0, axis_1, axis_linear (GCode.cpp:1179-1194)
AXES = "XYZ"
NAN = float("nan")
MOTIONS = MOTION | PROBES | {"G80"}
MODES = {"G93", "G20", "G21", "G17", "G18", "G19", "G90", "G91", "G92", "G92.1", "G10"} | set(WCS)
WCS_SET = set(WCS)
TAKES_AXES = {"G10", "G28", "G30", "G28.1", "G30.1", "G43.1"}   # their X/Y/Z are not a move's target
PASS = {"G53", "G28", "G30", "G28.1", "G30.1", "G43.1", "G49", "G10"} | PROBES
_CODE = {}                         # ("G", "01") -> "G1", as read so far


def _code(no, letter, text):
    """The name of the G or M code letter+text as FluidNC reads it (number and mantissa, GCode.cpp:402-404)."""
    v = float(text)
    name = (G_CODES if letter == "G" else M_CODES).get((math.trunc(v), mantissa(v)))
    if name is None:
        raise LevelError(no, f"{letter}{text} is not a code FluidNC runs (it would refuse the line)")
    _CODE[letter, text] = name
    return name


class LevelError(ValueError):
    """A program the leveler refuses. line: the program line (1-based, as the editor counts), or None when it is
    about the whole program; reason: the message without "Line N: "."""

    def __init__(self, line, reason):
        self.line = line
        self.reason = reason
        super().__init__(f"Line {line}: {reason}" if line else reason)


class ScanError(ValueError):
    """A scan (.xyz) that can't be used for leveling."""


def fmt(v):
    """A number as UGS writes it (Java DecimalFormat "0.####", which rounds the exact binary value half to even,
    like Python's %): at most 4 decimals, no trailing zeros, "-0" for a small negative number."""
    s = "%.4f" % v
    return s.rstrip("0").rstrip(".") if "." in s else s


# ------------------------------------------------------------------ the scan

class Mesh:
    """The scanned surface: heights z[i][j] at (xs[i], ys[j]), in units ("INCH" or "MM")."""

    def __init__(self, xs, ys, z, units="INCH"):
        if units not in UNITS:
            raise ScanError(f"Unknown scan units {units!r} (INCH or MM)")
        xs, ys = [float(v) for v in xs], [float(v) for v in ys]
        if len(xs) < 2 or len(ys) < 2:
            raise ScanError("A scan needs at least 2 points along X and along Y")
        if any(b <= a for a, b in zip(xs, xs[1:])) or any(b <= a for a, b in zip(ys, ys[1:])):
            raise ScanError("The scan's X and Y positions must go up")
        if len(z) != len(xs) or any(len(col) != len(ys) for col in z):
            raise ScanError("The scan's heights are not a full grid")
        self.z = [[float(v) for v in col] for col in z]
        if not all(math.isfinite(v) for v in xs + ys + [v for col in self.z for v in col]):
            raise ScanError("The scan has a point that is not a number")
        self.xs, self.ys, self.units = xs, ys, units
        self.header = {}                    # the file's "# key=value" words (parse_scan), e.g. units, xSamples
        self.dx = xs[1] - xs[0]             # UGS MeshLeveler xResolution, yResolution
        self.dy = ys[1] - ys[0]
        for steps, d, name in ((xs, self.dx, "X"), (ys, self.dy, "Y")):
            if any(abs((b - a) - d) > 1e-6 * max(1.0, abs(d)) for a, b in zip(steps, steps[1:])):
                raise ScanError(f"The scan's points are not evenly spaced in {name}")

    def height(self, x, y):
        """The surface height at x, y (scan units), exactly as UGS MeshLeveler.surfaceHeightAt works it out:
        clamped to the scan's edge outside it, then the bilinear blend of the 4 points around it."""
        xs, ys, z = self.xs, self.ys, self.z
        x = max(xs[0], min(x, xs[-1]))
        y = max(ys[0], min(y, ys[-1]))
        xo, yo = x - xs[0], y - ys[0]
        i = 0 if xo == 0 else int(xo / self.dx)
        j = 0 if yo == 0 else int(yo / self.dy)
        i = max(min(i, len(xs) - 2), 0)
        j = max(min(j, len(ys) - 2), 0)
        x1, x2, y1, y2 = xs[i], xs[i + 1], ys[j], ys[j + 1]
        r1 = ((x2 - x) / (x2 - x1)) * z[i][j] + ((x - x1) / (x2 - x1)) * z[i + 1][j]
        r2 = ((x2 - x) / (x2 - x1)) * z[i][j + 1] + ((x - x1) / (x2 - x1)) * z[i + 1][j + 1]
        return ((y2 - y) / (y2 - y1)) * r1 + ((y - y1) / (y2 - y1)) * r2

    def split_length(self):
        """The longest move UGS leaves in one piece: a quarter of the mean scan spacing (scan units)."""
        return (self.dx + self.dy) / 2.0 / 4

    def bounds(self):
        return self.xs[0], self.xs[-1], self.ys[0], self.ys[-1]

    def info(self):
        flat = [v for col in self.z for v in col]
        return {"units": self.units, "xPoints": len(self.xs), "yPoints": len(self.ys),
                "minX": self.xs[0], "maxX": self.xs[-1], "minY": self.ys[0], "maxY": self.ys[-1],
                "minZ": min(flat), "maxZ": max(flat)}


def parse_scan(text, default_units="INCH", units="INCH"):
    """The Mesh in a .xyz text as UGS (SaveScannedSurfaceAction) and scan.py write it: "# key=value" header lines
    (units=INCH|MM, xSamples, ySamples), then one "x y z" line per point. Without a units line the numbers are in
    default_units. Read as UGS opens a scan (OpenScannedSurfaceAction, SurfaceScanner.reset): the points go into
    units (UGS: the preferred units, inches for the user; the Z surface is in these units too), the grid is
    min + (max - min) * i / (n - 1) along X and Y, and each grid point takes the Z of the file's point within 0.01
    of it. ScanError if the points aren't a full grid of at least 2 x 2 points."""
    if units not in UNITS:
        raise ScanError(f"Unknown scan units {units!r} (INCH or MM)")
    header, points = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            for part in line[1:].split():
                key, eq, value = part.partition("=")
                if eq:
                    header[key] = value
            continue
        parts = line.split()
        try:
            if len(parts) < 3:
                raise ValueError
            x, y, z = (float(v) for v in parts[:3])
        except ValueError:
            raise ScanError(f"Scan line {n} is not \"x y z\": {line[:40]}") from None
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise ScanError(f"Scan line {n} has a value that is not a number: {line[:40]}")
        points.append((x, y, z))
    file_units = header.get("units", default_units).upper()
    if file_units not in UNITS:
        raise ScanError(f"Unknown scan units {header.get('units')!r} (INCH or MM)")
    if not points:
        raise ScanError("The scan has no points")
    if file_units != units:              # UGS OpenScannedSurfaceAction.toUnits, Position.getPositionIn
        k = INCH_PER_MM if units == "INCH" else MM_PER_INCH
        points = [(x * k, y * k, z * k) for x, y, z in points]
    counts = []                          # (UGS: the header's counts, else the distinct values)
    for key, axis in (("xSamples", 0), ("ySamples", 1)):
        if key in header:
            try:
                counts.append(int(header[key]))
            except ValueError:
                raise ScanError(f"The scan's header has {key}={header[key]}: not a whole number") from None
        else:
            counts.append(len({round(p[axis], 6) for p in points}))
    nx, ny = counts
    if nx < 2 or ny < 2:
        raise ScanError("A scan needs at least 2 points along X and along Y")
    if len(points) != nx * ny:
        if "xSamples" in header or "ySamples" in header:
            raise ScanError(f"The scan has {len(points)} points but its header says xSamples={nx} ySamples={ny}")
        raise ScanError(f"The scan's {len(points)} points are not a full {nx} x {ny} grid")
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    if x1 == x0 or y1 == y0:             # (the header's counts say 2 or more: no grid step to divide by)
        raise ScanError(f"The scan's {len(points)} points all have the same {'X' if x1 == x0 else 'Y'}: a scan "
                        "needs at least 2 points along X and along Y")
    xs = [x0 + ((x1 - x0) * i) / (nx - 1) for i in range(nx)]     # (SurfaceScanner.reset, in this order)
    ys = [y0 + ((y1 - y0) * j) / (ny - 1) for j in range(ny)]
    dx, dy = xs[1] - xs[0], ys[1] - ys[0]
    z = [[None] * ny for _ in range(nx)]
    for x, y, h in points:
        i, j = round((x - x0) / dx), round((y - y0) / dy)
        if abs(x - xs[i]) > SNAP or abs(y - ys[j]) > SNAP:
            raise ScanError(f"The scan's point X{x:.5f} Y{y:.5f} is not on an evenly spaced grid")
        if z[i][j] is not None:
            raise ScanError(f"The scan has two points at X{xs[i]:.5f} Y{ys[j]:.5f}")
        z[i][j] = h
    mesh = Mesh(xs, ys, z, units)
    mesh.header = header                 # (UGS skips "#" lines, so FluidSender may add its own keys there)
    return mesh


def read_scan(path, default_units="INCH", units="INCH"):
    """parse_scan of the file at path."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse_scan(f.read(), default_units, units)


# ------------------------------------------------------------------ the program

class Leveled:
    """The leveled program: lines (the text to run and show), source[i] the program line (1-based) lines[i] came
    from, warnings (texts for the user), stats (numbers for the page)."""

    def __init__(self, lines, source, warnings, stats):
        self.lines = lines
        self.source = source
        self.warnings = warnings
        self.stats = stats

    def text(self):
        return "\n".join(self.lines) + "\n" if self.lines else ""


def settings_with(settings):
    """DEFAULTS with settings over them, checked. LevelError (line None) if a value is unusable."""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (settings or {}).items() if k in DEFAULTS})
    for key in ("z_surface", "arc_segment_mm"):
        v = s[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise LevelError(None, f"Leveling setting {key} must be a number")
        s[key] = float(v)
    if s["arc_segment_mm"] <= 0:
        raise LevelError(None, "The arc segment length must be more than 0 mm")
    if s["wcs"] not in WCS:
        raise LevelError(None, f"Unknown work coordinate system {s['wcs']!r}")
    if not isinstance(s["max_lines"], int) or s["max_lines"] < 1:
        raise LevelError(None, "max_lines must be a positive whole number")
    return s


def level(lines, mesh, settings=None):
    """The program lines (strings without line ends, as the editor shows them) leveled with mesh. settings: see
    DEFAULTS. Returns a Leveled; raises LevelError for a program, or a line of it, that can't be leveled safely."""
    s = settings_with(settings)
    leveler = _Leveler(mesh, s)
    count = 0
    for count, raw in enumerate(lines, 1):
        leveler.line(count, raw)
    result = leveler.result()
    result.stats["lines"] = count
    return result


def level_text(text, mesh, settings=None):
    """level() of a whole program text, split into lines as the editor and job.parse_program split it."""
    lines = LINE_END.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return level(lines, mesh, settings)


def _unreadable(code):
    """Why a line that is not plain words can't be leveled (FluidNC 4.1.1 runs #parameters, GCode.cpp:368-375,
    [expressions] and O-word flow control, GCode.cpp:381 and 878-884: the leveler can't follow them)."""
    if re.match(r"[Oo][-+]?\d", code):
        return f"{code[:20]}: O-words (subroutines, loops, if) can't be leveled: the leveler can't follow them"
    if "#" in code or "[" in code:
        return "has #parameters or [expressions]: the leveler can't work out where they move, so it can't level them"
    if re.search(r"[Ss][-+]?(?:\d+\.?\d*|\.\d+):", code):
        return "has an S..:.. spindle-speed cluster (laser): such a line can't be split for leveling"
    return f"can't be read as G-code words: {code[:40]}"


def _ugs_angle(c0, c1, p0, p1):
    """UGS GcodePreprocessorUtils.getAngle: the angle of center -> point in [0, 2 pi), worked out as UGS does."""
    dx, dy = p0 - c0, p1 - c1
    if dx != 0:
        if dx > 0 and dy >= 0:
            return math.atan(dy / dx)
        if dx < 0 and dy >= 0:
            return math.pi - abs(math.atan(dy / dx))
        if dx < 0 and dy < 0:
            return math.pi + abs(math.atan(dy / dx))
        if dx > 0 and dy < 0:
            return math.pi * 2 - abs(math.atan(dy / dx))
        return 0.0                                        # (dx is NaN)
    return math.pi / 2.0 if dy > 0 else math.pi * 3.0 / 2.0


def _ugs_sweep(begin, end, cw):
    """UGS GcodePreprocessorUtils.calculateSweep: the arc's angle (positive), a full circle when they are equal."""
    if begin == end:
        return math.pi * 2
    if end == 0:
        end = math.pi * 2
    if not cw and end < begin:
        return (math.pi * 2 - begin) + end
    if cw and end > begin:
        return (math.pi * 2 - end) + begin
    return abs(end - begin)


class _Leveler:
    """The pass over the program: the program's own state (units, plane, modes, where it is and which axes are
    known, in program units, never leveled, as UGS's GcodeState after each line), and the output."""

    def __init__(self, mesh, s):
        self.mesh = mesh
        self.s = s
        self.zs = s["z_surface"]
        if mesh.units == "INCH":          # UGS Utils.createCommandProcessor: Z surface -> mm -> mesh units
            self.zs = (self.zs * MM_PER_INCH) * INCH_PER_MM
        self.units = None
        self.set_units("INCH")            # (every FluidSender job starts with "G20 G90", job.FIRST)
        self.pos = [NAN, NAN, NAN]
        self.known = [False, False, False]
        self.err = [0.0, 0.0, 0.0]        # where the machine will be minus where it should be (program units)
        self.motion = None                # the program's modal motion (None: not set yet)
        self.machine_motion = None        # the machine's, which differs after an arc made into G1 lines
        self.plane = "G17"
        self.absolute = True
        self.wcs = s["wcs"]
        self.out = []
        self.source = array("I")
        self.leveled = self.split = self.arcs = self.outside = self.inside = 0
        self.max_outside = 0.0
        self.edges = mesh.bounds()
        self.box = [math.inf, -math.inf, math.inf, -math.inf]   # min x, max x, min y, max y leveled (scan units)
        self.offsets = [math.inf, -math.inf]                     # smallest and largest Z offset (program units)
        self.unleveled = []               # feed moves left as they are (position not known)

    # -------------------------------------------------- units and output

    def set_units(self, units):
        if units == self.units:
            return
        if self.units is not None:        # UGS GcodeParserUtils G20/G21: the position converts
            k = INCH_PER_MM if units == "INCH" else MM_PER_INCH
            self.pos = [v * k for v in self.pos]
            self.err = [v * k for v in self.err]
        self.units = units
        mm = MM_PER_INCH if units == "INCH" else 1.0
        self.to_mm = mm
        if self.mesh.units == units:
            self.to_mesh = self.from_mesh = 1.0
        elif self.mesh.units == "INCH":
            self.to_mesh, self.from_mesh = INCH_PER_MM, MM_PER_INCH
        else:
            self.to_mesh, self.from_mesh = MM_PER_INCH, INCH_PER_MM
        self.split_len = self.mesh.split_length() * self.from_mesh    # UGS LineSplitter maxLength, program units

    def emit(self, no, text):
        if len(self.out) >= self.s["max_lines"]:
            raise LevelError(no, f"the leveled program would be longer than {self.s['max_lines']:,} lines")
        self.out.append(text)
        self.source.append(no)

    def offset(self, x, y):
        """UGS MeshLeveler (absolute): the Z to add at x, y (program units), noting where it was."""
        mx, my = x * self.to_mesh, y * self.to_mesh
        off = (self.mesh.height(mx, my) - self.zs) * self.from_mesh
        self.note(mx, my, off)
        return off

    def note(self, mx, my, off):
        x0, x1, y0, y1 = self.edges
        dx = x0 - mx if mx < x0 else (mx - x1 if mx > x1 else 0.0)
        dy = y0 - my if my < y0 else (my - y1 if my > y1 else 0.0)
        if dx > EDGE or dy > EDGE:
            self.outside += 1
            d = math.hypot(dx, dy)
            if d > self.max_outside:
                self.max_outside = d
        else:
            self.inside += 1
        b = self.box
        if mx < b[0]:
            b[0] = mx
        if mx > b[1]:
            b[1] = mx
        if my < b[2]:
            b[2] = my
        if my > b[3]:
            b[3] = my
        o = self.offsets
        if off < o[0]:
            o[0] = off
        if off > o[1]:
            o[1] = off
        self.leveled += 1

    # -------------------------------------------------- one program line

    def line(self, no, raw):
        comment = "(" in raw or ";" in raw
        code = (COMMENTS.sub("", raw) if comment else raw).replace(" ", "")
        if "\t" in code or "\f" in code or "\v" in code:
            code = code.translate(SPACES)
        if not code.strip("%"):
            self.emit(no, raw)            # empty, comment or % (FluidSender doesn't send them)
            return
        if code[0] == "$":
            self.dollar(no, raw, code)
            return
        if not WORDS.fullmatch(code):
            raise LevelError(no, _unreadable(code))
        gs, ms, vals = [], [], {}
        for letter, text in WORD.findall(code):
            u = letter.upper()
            if u == "G":
                gs.append(_CODE.get(("G", text)) or _code(no, "G", text))
            elif u == "M":
                ms.append(_CODE.get(("M", text)) or _code(no, "M", text))
            elif u in VALUES:
                if u in vals:
                    raise LevelError(no, f"two {u} words on one line: FluidNC refuses it")
                vals[u] = float(text)
            elif u in "ABCUVW":
                raise LevelError(no, f"{letter}{text}: the leveler only knows the X, Y and Z axes")
            elif u == "O":
                raise LevelError(no, f"{letter}{text}: O-words (subroutines, loops, if) can't be leveled: the leveler "
                                     "can't follow them")
            else:
                raise LevelError(no, f"{letter}{text}: FluidNC doesn't take {u} words (it would refuse the line)")
        for names, groups in ((gs, G_GROUP), (ms, M_GROUP)):
            if len(names) > 1:
                seen = {}
                for name in names:
                    if groups[name] in seen:
                        raise LevelError(no, f"{seen[groups[name]]} and {name} on one line: FluidNC refuses it")
                    seen[groups[name]] = name
        g = set(gs)
        if not g.isdisjoint(MODES):       # modes first, wherever they are on the line (as GCode.cpp runs them)
            self.modes(no, g, vals)
        motion = None
        for c in gs:
            if c in MOTIONS:
                motion = self.motion = c
                break
        has_axes = "X" in vals or "Y" in vals or "Z" in vals
        takes_axes = not g.isdisjoint(TAKES_AXES)
        if has_axes and not takes_axes:
            m = motion or self.motion
            if m is None:
                raise LevelError(no, "X/Y/Z without G0, G1, G2 or G3 before the program has set one: add G0 or G1")
            if m == "G80":
                raise LevelError(no, "X/Y/Z after G80 (no motion mode): FluidNC refuses the line")
            if m in MOTION and self.wcs != self.s["wcs"] and "G53" not in g:
                raise LevelError(no, f"the program moves in {self.wcs} but the scan was taken in {self.s['wcs']}")
        if has_axes and not ms and not takes_axes and g.isdisjoint(PASS):
            m = motion or self.motion
            if m == "G0" or m == "G1":
                self.linear(no, raw, code, comment, vals, m)
                return
            if m == "G2" or m == "G3":
                self.arc(no, raw, code, vals, m, motion is not None)
                return
        self.other(no, raw, code, comment, g, ms, vals, motion, has_axes, takes_axes)

    def modes(self, no, g, vals):
        """The line's units, plane, distance mode and work coordinate system (and what the leveler refuses)."""
        if "G93" in g:
            raise LevelError(no, "G93 (inverse time feed) can't be leveled: split moves would take longer")
        if "G20" in g or "G21" in g:
            self.set_units("INCH" if "G20" in g else "MM")
        for p in ("G17", "G18", "G19"):
            if p in g:
                self.plane = p
        if "G90" in g or "G91" in g:
            self.absolute = "G90" in g
        for w in g & WCS_SET:
            if w != self.wcs:
                self.wcs = w
                self.known = [False, False, False]     # (UGS: every axis unknown in a new frame)
        if "G92" in g or "G92.1" in g:
            raise LevelError(no, f"{'G92' if 'G92' in g else 'G92.1'} changes the work coordinates: the scan would "
                                 "no longer line up with the program")
        if "G10" in g:
            self.g10(no, vals)

    def other(self, no, raw, code, comment, g, ms, vals, motion, has_axes, takes_axes):
        """A line with M codes or G codes other than motion and modes, or without X/Y/Z."""
        if "M6" in ms:                                  # a tool change: anything can happen to the position
            self.passthrough(no, raw, (0, 1, 2), motion)
        elif g & {"G28", "G30"}:                        # only the named axes go home, all if none (GCode.cpp:1352-1361)
            named = [a for a in range(3) if AXES[a] in vals]
            self.passthrough(no, raw, named or (0, 1, 2), motion)
        elif g & {"G28.1", "G30.1"} and has_axes:
            raise LevelError(no, "G28.1/G30.1 with axis words: FluidNC moves on them, the leveler can't follow")
        elif g & {"G43.1", "G49"}:                      # the tool length offset moves the work Z frame
            self.passthrough(no, raw, (2,), motion)
        elif not has_axes or takes_axes:
            self.passthrough(no, raw, (), motion)
        else:
            m = motion or self.motion
            if "G53" in g or m in PROBES:               # machine coordinates, or a probe that stops anywhere
                self.track(vals, True if "G53" in g else self.absolute)
                self.passthrough(no, raw, [a for a in range(3) if AXES[a] in vals], motion)
            elif m in ("G0", "G1"):
                self.linear(no, raw, code, comment, vals, m)
            else:
                self.arc(no, raw, code, vals, m, motion is not None)
        if "M2" in ms or "M30" in ms:                   # program end resets these (GCode.cpp:2035-2040)
            self.motion = self.machine_motion = "G1"
            self.plane = "G17"
            self.absolute = True
            if self.wcs != "G54":
                self.wcs = "G54"
                self.known = [False, False, False]

    def dollar(self, no, raw, code):
        up = code.upper()
        if up.startswith("$J=") or (up.startswith("$H") and "=" not in up):
            self.passthrough(no, raw, set(range(3)), None)    # homing and jogging: the position is unknown after
            return
        raise LevelError(no, f"{code[:30]} is a FluidNC command, not G-code: take it out of the program to level it")

    def g10(self, no, vals):
        """G10 L2/L20 on the active system changes the frame the scan is in (GCode.cpp:1247-1293)."""
        l, p = vals.get("L"), vals.get("P")
        if l not in (2, 20) or p is None or p != math.trunc(p) or not 0 <= p <= 9:
            raise LevelError(no, "G10 needs L2 or L20 and P0 to P9 (FluidNC refuses it otherwise)")
        if p == 0 or WCS[int(p) - 1] == self.wcs:
            raise LevelError(no, f"G10 L{int(l)} changes the work coordinates of {self.wcs}: the scan would no "
                                 "longer line up with the program")

    def track(self, vals, absolute):
        """The program's position after a line that is copied as it is (UGS GcodeParserUtils)."""
        for a in range(3):
            v = vals.get(AXES[a])
            if v is not None:
                if absolute or math.isnan(self.pos[a]):
                    self.pos[a] = v
                else:
                    self.pos[a] += v

    def passthrough(self, no, raw, unknown, motion):
        """The line as it is; the axes in unknown are not known after it; motion: its motion word, if any."""
        self.emit(no, raw)
        for a in unknown:
            self.known[a] = False
        if motion:
            self.machine_motion = motion

    # -------------------------------------------------- G0 / G1

    def end_of(self, vals):
        """(end, known after) of a move with these axis words, as UGS's parser works them out."""
        end, known = self.pos[:], self.known[:]
        for a in range(3):
            v = vals.get(AXES[a])
            if v is None:
                continue
            if self.absolute:
                end[a], known[a] = v, True
            elif math.isnan(end[a]):
                end[a], known[a] = v, False        # (a relative move from a position never set: unknown)
            else:
                end[a] += v
        return end, known

    def pieces(self, start, end):
        """UGS LineSplitter: the points after start up to end; [end] when the move is not split."""
        dx, dy, dz = start[0] - end[0], start[1] - end[1], start[2] - end[2]
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        top = self.split_len
        if not (top > 0) or not (length > top):          # (NaN: an axis never set)
            return [end]
        n = int(math.ceil(length / top))
        seg = length / math.ceil(length / top)
        points = []
        for i in range(1, n):
            k = 1 / (length / (i * seg))
            points.append([start[a] + k * (end[a] - start[a]) for a in range(3)])
        points.append(end)
        return points

    def linear(self, no, raw, code, comment, vals, m):
        start, known_start = self.pos[:], all(self.known)
        end, known = self.end_of(vals)
        known_end = all(known)
        absolute = self.absolute
        can_level = known_end if absolute else known_start
        points = self.pieces(start, end) if (not absolute or (known_start and known_end)) else [end]
        if len(points) == 1:
            if not can_level:
                if m == "G1":
                    self.unleveled.append(no)
                self.passthrough(no, raw, set(), m)
                self.track_err(vals, absolute)
            elif absolute:
                z = end[2] + self.offset(end[0], end[1])
                self.emit(no, self.with_z(raw, code, comment, "Z" in vals, z))
                self.err = [0.0 if AXES[a] in vals else self.err[a] for a in range(3)]
                self.err[2] = float(fmt(z)) - z
                self.machine_motion = self.motion
            else:
                want = end[2] - start[2] + self.offset(end[0], end[1]) - self.offset_quiet(start)
                d = want - self.err[2]
                text = fmt(d)
                self.emit(no, self.with_z(raw, code, comment, "Z" in vals, d, text))
                self.err[2] += float(text) - want
                self.machine_motion = self.motion
        else:
            self.split += 1
            before, after = self.rest(raw, m, arc=False)
            if before:
                self.emit(no, before)
            if absolute:
                prev = start
                for p in points:
                    self.absolute_piece(no, m, prev, p, True)
                    prev = p
            else:
                self.relative_pieces(no, m, start, points, can_level)
                if not can_level and m == "G1":
                    self.unleveled.append(no)
            if after:
                self.emit(no, after)
            self.machine_motion = m
        self.pos, self.known = end, known

    def absolute_piece(self, no, m, start, p, level_it):
        """One generated absolute G0/G1 line to p (UGS generateLineFromPoints, then MeshLeveler on its text)."""
        sx, sy, sz = fmt(p[0]), fmt(p[1]), fmt(p[2])
        if level_it:
            x, y, z = float(sx), float(sy), float(sz)
            nz = z + self.offset(x, y)
            self.emit(no, f"{m}X{sx}Y{sy}Z{fmt(nz)}")
            self.err = [x - p[0], y - p[1], float(fmt(nz)) - (p[2] + (nz - z))]
        else:
            self.emit(no, f"{m}X{sx}Y{sy}Z{sz}")
            self.err = [float(sx) - p[0], float(sy) - p[1], float(sz) - p[2]]

    def relative_pieces(self, no, m, start, points, level_it):
        """Generated G91 lines through points, each the move from where the machine is to where it should be."""
        prev = start
        prev_off = self.offset_quiet(start) if level_it else 0.0
        for p in points:
            off = self.offset(p[0], p[1]) if level_it else 0.0
            want = [p[0] - prev[0], p[1] - prev[1], (p[2] - prev[2]) + (off - prev_off)]
            parts = []
            for a in range(3):
                if math.isnan(want[a]):
                    continue
                text = fmt(want[a] - self.err[a])
                self.err[a] += float(text) - want[a]
                parts.append(AXES[a] + text)
            self.emit(no, m + "".join(parts))
            prev, prev_off = p, off

    def offset_quiet(self, p):
        return (self.mesh.height(p[0] * self.to_mesh, p[1] * self.to_mesh) - self.zs) * self.from_mesh

    def track_err(self, vals, absolute):
        if absolute:
            for a in range(3):
                if AXES[a] in vals:
                    self.err[a] = 0.0

    def with_z(self, raw, code, comment, has_z, z, text=None):
        """The line with its Z word set to z (UGS MeshLeveler: comments and their line's spaces go; a missing Z
        word is added at the end)."""
        new = "Z" + (text if text is not None else fmt(z))
        line = code if comment else raw
        if has_z:
            out, n = ZWORD.subn(lambda _m: new, line)
            if n == 1:
                return out
            out, n = ZWORD.subn(lambda _m: new, code)  # ("Z -0.5": the word only reads without its spaces)
            if n == 1:
                return out
            raise LevelError(None, "internal: Z word not found")      # (can't happen: WORDS read one)
        return line + new

    def rest(self, raw, m, arc):
        """UGS extractMotion + splitProgramStops: the line's words other than its motion, to run (before) and its
        M0/M1/M2/M30 (after), each "" if none; comments stay where they are among the words before. An arc's P
        (turns) goes too: UGS leaves it on a line of its own, which FluidNC refuses (unused word)."""
        before, after = [], []
        motion_seen = False
        tokens, at = [], 0
        for c in COMMENTS.finditer(raw):
            tokens += WORD.findall(raw[at:c.start()].translate(SPACES))
            tokens.append(c.group())
            at = c.end()
        tokens += WORD.findall(raw[at:].translate(SPACES))
        for token in tokens:
            if isinstance(token, str):                  # a comment
                before.append(token)
                continue
            letter, text = token
            word = letter + text
            letter = letter.upper()
            if letter in "XYZIJKR" or (arc and letter == "P"):
                continue
            if letter == "G" and not motion_seen:
                v = float(text)
                if G_CODES.get((math.trunc(v), mantissa(v))) == m:
                    motion_seen = True
                    continue
            if letter == "M":
                v = float(text)
                if M_CODES.get((math.trunc(v), mantissa(v))) in STOPS:
                    after.append(word)
                    continue
            before.append(word)
        return "".join(before), "".join(after)

    # -------------------------------------------------- G2 / G3

    def arc(self, no, raw, code, vals, m, explicit):
        a0, a1, lin = PLANES[self.plane]
        start, known_start = self.pos[:], self.known[:]
        end, known = self.end_of(vals)
        absolute = self.absolute
        expand = True
        if absolute:      # (UGS ArcExpander requireKnownPosition: the arc's start in its plane must be known)
            expand = known_start[a0] and known_start[a1]
            if expand and not known_start[lin] and AXES[lin] in vals:
                expand = False
        if math.isnan(start[a0]) or math.isnan(start[a1]):
            expand = False
        if not expand:                    # (UGS ArcExpander.passThrough: a modal arc line gets its G2/G3)
            self.unleveled.append(no)
            line = raw if explicit else m + raw
            self.emit(no, line)
            self.pos, self.known = end, known
            self.machine_motion = m
            return
        flat = absolute and not known_start[lin]                    # the linear axis is not known: leave it out
        points = self.arc_points(no, m == "G2", start, end, vals, (a0, a1, lin))
        self.arcs += 1
        before, after = self.rest(raw, m, arc=True)
        if before:
            self.emit(no, before)
        can_level = all(known) if absolute else all(known_start)
        if absolute:
            prev = start
            for p in points:
                r = [float(fmt(v)) for v in p]                     # (the G1 line's numbers, as UGS reads them back)
                if flat:
                    r[lin] = NAN
                    self.emit(no, "G1" + "".join(AXES[a] + fmt(p[a]) for a in range(3) if a != lin))
                    for a in (a0, a1):
                        self.err[a] = r[a] - p[a]
                else:
                    for q in self.pieces(prev, r):
                        self.absolute_piece(no, "G1", prev, q, can_level)
                        prev = q
                prev = r
            if flat or not can_level:
                self.unleveled.append(no)
        else:
            pts = []
            prev = start
            for p in points:
                pts += self.pieces(prev, p)
                prev = p
            self.relative_pieces(no, "G1", start, pts, can_level)
            if not can_level:
                self.unleveled.append(no)
        if after:
            self.emit(no, after)
        self.machine_motion = "G1"
        self.pos, self.known = end, known

    def arc_points(self, no, cw, start, end, vals, plane):
        """The points of the arc after start, end last, along the path FluidNC would cut (GCode.cpp:1425-1565,
        MotionControl.cpp:205-221); LevelError where FluidNC would refuse the arc."""
        a0, a1, lin = plane
        if AXES[a0] not in vals and AXES[a1] not in vals:
            raise LevelError(no, f"an arc needs {AXES[a0]} or {AXES[a1]} in its plane: FluidNC refuses it")
        p = vals.get("P")
        if p is not None and (p != math.trunc(p) or p < 0):
            raise LevelError(no, "an arc's P (turns) must be a whole number: FluidNC refuses it")
        turns = int(p) if p is not None else 1
        x, y = end[a0] - start[a0], end[a1] - start[a1]
        mm = self.to_mm
        if "R" in vals:
            if any(k in vals for k in "IJK"):
                raise LevelError(no, "an arc with both R and I/J/K: FluidNC refuses it")
            r = vals["R"]
            if x == 0 and y == 0:
                raise LevelError(no, "an R arc that ends where it starts: FluidNC can't run it (use I/J for a circle)")
            h = 4.0 * r * r - x * x - y * y
            if h < -1e-9 * 4.0 * r * r:
                raise LevelError(no, f"R{fmt(r)} is too small for this arc: FluidNC refuses it")
            h = -math.sqrt(max(h, 0.0)) / math.hypot(x, y)
            if not cw:
                h = -h
            if r < 0:
                h = -h
            off = (0.5 * (x - y * h), 0.5 * (y + x * h))
        else:
            ijk = {0: vals.get("I"), 1: vals.get("J"), 2: vals.get("K")}
            if ijk[a0] is None and ijk[a1] is None:
                raise LevelError(no, "an arc needs I, J or K in its plane (or R): FluidNC refuses it")
            off = (ijk[a0] or 0.0, ijk[a1] or 0.0)
            radius = math.hypot(*off)
            delta = abs(math.hypot(x - off[0], y - off[1]) - radius) * mm
            if delta > 0.005 and (delta > 0.5 or delta > 0.001 * radius * mm):
                raise LevelError(no, "the arc's end is not on its circle: FluidNC refuses it")
        c0, c1 = start[a0] + off[0], start[a1] + off[1]
        r0, r1 = -off[0], -off[1]                                # center -> start
        t0, t1 = end[a0] - c0, end[a1] - c1                      # center -> end
        travel = math.atan2(r0 * t1 - r1 * t0, r0 * t0 + r1 * t1)
        circle = start[a0] == end[a0] and start[a1] == end[a1]     # (its travel is rounding noise: a full circle)
        if not circle and abs(travel) <= AMBIGUOUS:
            raise LevelError(no, "the arc ends a hair from its start (under 0.00001 rad around the center): FluidNC "
                                 "works that out in single precision and may cut a full circle or almost nothing, so "
                                 "it can't be leveled safely (end a circle exactly where it starts)")
        if cw:
            if travel >= -ARC_EPSILON:
                travel -= 2 * math.pi
            travel -= (turns - 1) * 2 * math.pi if turns > 1 else 0
        else:
            if travel <= ARC_EPSILON:
                travel += 2 * math.pi
            travel += (turns - 1) * 2 * math.pi if turns > 1 else 0
        if turns <= 1:
            points = self.ugs_arc(no, cw, start, end, (c0, c1), vals.get("R"), travel, plane)
            if points is not None:
                return points
        radius = math.hypot(r0, r1)
        n = int(math.ceil(abs(travel * radius) * mm / self.s["arc_segment_mm"]))    # (UGS: ceil(length / segment))
        self.arc_size(no, n)
        n = max(n, 1)
        a = math.atan2(r1, r0)
        points = []
        for i in range(1, n):
            t = a + travel * i / n
            q = [0.0, 0.0, 0.0]
            q[a0] = c0 + radius * math.cos(t)
            q[a1] = c1 + radius * math.sin(t)
            q[lin] = start[lin] + (end[lin] - start[lin]) * i / n
            points.append(q)
        points.append(end[:])
        return points

    def arc_size(self, no, n):
        if n > self.s["max_lines"]:
            raise LevelError(no, f"this arc would become {n:,} lines of {self.s['arc_segment_mm']} mm: use a longer "
                                 "arc segment")

    def ugs_arc(self, no, cw, start, end, center, r_word, travel, plane):
        """The arc's points after start, end last, worked out as UGS's ArcExpander does it, to the last bit
        (GcodePreprocessorUtils.generatePointsAlongArcBDring:490-623, getAngle:676-705, calculateSweep:712-735;
        the radius as GcodeParserUtils.addArcPointSegment:235-271 has it), so that the output is UGS's. None when
        that is not the arc FluidNC cuts (travel: FluidNC's angle, MotionControl.cpp:205-221): an arc under
        ARC_EPSILON that FluidNC makes a full circle, or one with no length."""
        a0, a1, lin = plane
        c0, c1 = center
        s0, s1, e0, e1 = start[a0], start[a1], end[a0], end[a1]
        if r_word is None:
            d0, d1 = s0 - c0, s1 - c1
            radius = math.sqrt(d0 * d0 + d1 * d1)          # (Math.pow(d, 2.0) is d * d)
        else:
            radius = r_word
        r = radius
        if r == 0:
            return None
        begin = _ugs_angle(c0, c1, s0, s1)
        sweep = _ugs_sweep(begin, _ugs_angle(c0, c1, e0, e1), cw)
        if abs(sweep - abs(travel)) > 1e-6:
            return None
        if radius < 0:                                    # (MathUtils.normalizeAngle)
            begin = math.fmod(begin - math.pi, 2 * math.pi)
            if begin < 0:
                begin += 2 * math.pi
        n = int(math.ceil(abs(sweep * r * self.to_mm) / self.s["arc_segment_mm"]))
        self.arc_size(no, n)
        if n == 0:
            return None
        step = (end[lin] - start[lin]) / n
        at = start[lin]
        points = []
        for i in range(n):
            t = begin - i * sweep / n if cw else begin + i * sweep / n
            if t >= math.pi * 2:
                t = t - math.pi * 2
            q = [0.0, 0.0, 0.0]
            q[a0] = math.cos(t) * r + c0
            q[a1] = math.sin(t) * r + c1
            q[lin] = at
            at += step
            points.append(q)
        points.append(end[:])
        return points[1:]                                 # (the first is the start)

    # -------------------------------------------------- the end

    def result(self):
        if not self.leveled:
            raise LevelError(None, "Nothing in this program can be leveled: no move has X, Y and Z all known "
                                   "(it never sets Z, or only moves in machine coordinates)")
        if not self.inside:
            x0, x1, y0, y1 = self.mesh.bounds()
            bx0, bx1, by0, by1 = self.box
            raise LevelError(None, f"The scan (X {x0:.4f} to {x1:.4f}, Y {y0:.4f} to {y1:.4f}) doesn't cover this "
                                   f"program (X {bx0:.4f} to {bx1:.4f}, Y {by0:.4f} to {by1:.4f}): wrong scan or "
                                   "wrong zero?")
        u = "in" if self.mesh.units == "INCH" else "mm"
        warnings = []
        if self.outside:
            x0, x1, y0, y1 = self.mesh.bounds()
            warnings.append(f"{self.outside} of {self.leveled} leveled points are outside the scan (X {x0:.4f} to "
                            f"{x1:.4f}, Y {y0:.4f} to {y1:.4f}), up to {self.max_outside:.4f} {u} away: there the "
                            "height at the scan's nearest edge is used")
        if self.unleveled:
            shown = ", ".join(map(str, self.unleveled[:SHOW])) + (" ..." if len(self.unleveled) > SHOW else "")
            warnings.append(f"{len(self.unleveled)} feed moves are not leveled because X, Y or Z isn't known there "
                            f"(after G53, G28, G38, M6, $H, or before all three were set): lines {shown}")
        stats = {"lines": 0, "out": len(self.out), "leveled": self.leveled, "split": self.split,
                 "arcs": self.arcs, "outside": self.outside, "maxOutside": self.max_outside,
                 "unleveled": len(self.unleveled), "unleveledLines": self.unleveled[:SHOW],
                 "bounds": list(self.box), "offsets": list(self.offsets), "scan": self.mesh.info(),
                 "settings": dict(self.s)}
        return Leveled(self.out, self.source, warnings, stats)
