"""
Tests for leveler.py: the scan file, the height function, what each kind of line becomes, what is refused, and
the user's own checker (/home/coffee/level_check.py, run as `python3 -I level_check.py orig leveled scan.xyz`)
on programs written here and on the user's real job (orig.nc + scan.xyz, which must come out byte for byte as
UGS's lev.gcode). No ports, no server.

    python3 -m unittest tests.test_leveler -v
"""

import math
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import leveler  # noqa: E402
from leveler import LevelError, Mesh, ScanError, fmt, level, level_text, parse_scan  # noqa: E402

CHECKER = "/home/coffee/level_check.py"
SAMPLES = os.environ.get("FLUIDSENDER_LEVEL_SAMPLES", os.path.dirname(ROOT))   # orig.nc, lev.gcode, scan.xyz ...

# The user's scan (scan.xyz, saved by their UGS fork): 15 x 5 points, X 0.5..8.5, Y 1..12 in
REAL_Z = [0.0, -0.009, -0.008, -0.006, -0.005, 0.01, 0.004, 0.002, 0.001, 0.002, 0.022, 0.018, 0.01, 0.006,
          0.006, 0.012, 0.004, 0.002, 0.004, 0.006, 0.004, -0.005, -0.006, -0.01, -0.001, -0.005, -0.014, -0.015,
          -0.014, -0.014, -0.014, -0.022, -0.024, -0.02, -0.017, -0.019, -0.031, -0.031, -0.026, -0.02, -0.026,
          -0.036, -0.037, -0.032, -0.024, -0.035, -0.042, -0.042, -0.037, -0.031, -0.041, -0.047, -0.047, -0.042,
          -0.035, -0.041, -0.047, -0.047, -0.043, -0.037, -0.04, -0.046, -0.045, -0.041, -0.036, -0.036, -0.041,
          -0.041, -0.037, -0.03, -0.027, -0.035, -0.036, -0.032, -0.026]
REAL_XS = [0.5 + (8.0 * i) / 14 for i in range(15)]
REAL_YS = [1.0 + (11.0 * j) / 4 for j in range(5)]

# /home/coffee/4.xyz, the scan the user's good.nc / bad.nc checker fixtures were leveled with
SCAN4 = """# minX=1.00000 maxX=31.87500
# minY=1.00000 maxY=18.50000
# xSamples=5 ySamples=4
# units=INCH
1.0 1.0 -0.11218136199108772
1.0 6.833333333333333 -0.11873666938224356
1.0 12.666666666666666 -0.12317275284125262
1.0 18.5 -0.1193354840958184
8.71875 1.0 -0.12074548923777945
8.71875 6.833333333333333 -0.09122425616459176
8.71875 12.666666666666666 -0.12769083742839057
8.71875 18.5 -0.12545095330021505
16.4375 1.0 -0.0883731081897488
16.4375 6.833333333333333 -0.10457206826651477
16.4375 12.666666666666666 -0.10786458213594129
16.4375 18.5 -0.1321512828809855
24.15625 1.0 -0.10798014927872622
24.15625 6.833333333333333 -0.10508709812000558
24.15625 12.666666666666666 -0.12458015004699052
24.15625 18.5 -0.09639321435779444
31.875 1.0 -0.08680772206886235
31.875 6.833333333333333 -0.09230636816964949
31.875 12.666666666666666 -0.11042113963995075
31.875 18.5 -0.12680126379995146
"""


def xyz_text(mesh, header_units=True):
    """A .xyz file for mesh, in the format UGS and scan.py write."""
    out = [f"# minX={mesh.xs[0]:.5f} maxX={mesh.xs[-1]:.5f}", f"# minY={mesh.ys[0]:.5f} maxY={mesh.ys[-1]:.5f}",
           f"# xSamples={len(mesh.xs)} ySamples={len(mesh.ys)}"]
    if header_units:
        out.append(f"# units={mesh.units}")
    out += [f"{x!r} {y!r} {mesh.z[i][j]!r}" for i, x in enumerate(mesh.xs) for j, y in enumerate(mesh.ys)]
    return "\n".join(out) + "\n"


def real_mesh():
    return Mesh(REAL_XS, REAL_YS, [REAL_Z[i * 5:i * 5 + 5] for i in range(15)])


def wavy_mesh(units="INCH", scale=1.0):
    """6 x 5 points over X 0..10, Y 0..8 (times scale), a surface bilinear blending can't fake (inches or mm)."""
    xs = [2.0 * i * scale for i in range(6)]
    ys = [2.0 * j * scale for j in range(5)]
    z = [[(0.02 * math.sin(x / scale / 3) + 0.01 * math.cos(y / scale / 2) - 0.005 + 0.003 * ((i * 7 + j * 3) % 4))
          * scale for j, y in enumerate(ys)] for i, x in enumerate(xs)]
    return Mesh(xs, ys, z, units)


def plane_mesh(a=0.01, b=0.02, c=0.0, units="INCH"):
    """z = a*x + b*y + c on X 0..10, Y 0..8: bilinear blending is exact on it."""
    xs, ys = [0.0, 2.5, 5.0, 7.5, 10.0], [0.0, 4.0, 8.0]
    return Mesh(xs, ys, [[a * x + b * y + c for y in ys] for x in xs], units)


def bilinear(mesh, x, y):
    """An independent height (the checker's way: find the cell by bisection), for comparison."""
    import bisect
    xs, ys = mesh.xs, mesh.ys
    x = min(max(x, xs[0]), xs[-1])
    y = min(max(y, ys[0]), ys[-1])
    i = min(max(bisect.bisect_right(xs, x) - 1, 0), len(xs) - 2)
    j = min(max(bisect.bisect_right(ys, y) - 1, 0), len(ys) - 2)
    tx = (x - xs[i]) / (xs[i + 1] - xs[i])
    ty = (y - ys[j]) / (ys[j + 1] - ys[j])
    z = mesh.z
    return ((1 - tx) * (1 - ty) * z[i][j] + tx * (1 - ty) * z[i + 1][j] + (1 - tx) * ty * z[i][j + 1]
            + tx * ty * z[i + 1][j + 1])


WORD = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))")


def words(line):
    return {k: float(v) for k, v in WORD.findall(re.sub(r"\(.*?\)|;.*", "", line).upper())}


def run_lines(program):
    """The leveled lines of a program given as text."""
    return level_text(program, MESH).lines


MESH = plane_mesh()


class FormatTest(unittest.TestCase):

    def test_numbers_as_java_decimalformat_writes_them(self):
        cases = [(0.0, "0"), (-0.0, "-0"), (-0.00004, "-0"), (0.00004, "0"), (12.0, "12"), (-1.5, "-1.5"),
                 (1.23456, "1.2346"),
                 (0.03125, "0.0312"),     # an exact half: to even
                 (0.00005, "0.0001"),     # (the double is just above a half)
                 (-0.0198, "-0.0198"), (0.25020000000000003, "0.2502"), (10.88430000001, "10.8843")]
        for v, text in cases:           # (a list: 0.0 and -0.0 are the same dict key)
            self.assertEqual(fmt(v), text, v)


class ScanTest(unittest.TestCase):

    def test_the_users_scan_with_both_headers(self):
        text = xyz_text(real_mesh())
        mesh = parse_scan(text)
        self.assertEqual((len(mesh.xs), len(mesh.ys), mesh.units), (15, 5, "INCH"))
        self.assertEqual(mesh.xs[1], 1.0714285714285714)
        self.assertEqual(mesh.z[1][0], 0.01)
        self.assertEqual(mesh.split_length(), (mesh.xs[1] - mesh.xs[0] + 2.75) / 2 / 4)
        log_style = text.replace("minX=0.50000 maxX=8.50000", "minX=0.5 maxX=8.5")     # (scan_from_log.xyz)
        self.assertEqual(parse_scan(log_style).z, mesh.z)
        self.assertEqual(mesh.header["ySamples"], "5")
        extra = parse_scan("# wcs=G54 wcoX=-12.5\n" + text)          # (keys of FluidSender's own: UGS skips them)
        self.assertEqual((extra.header["wcs"], extra.header["wcoX"], extra.z), ("G54", "-12.5", mesh.z))
        for name in ("scan.xyz", "scan_from_log.xyz"):
            path = os.path.join(SAMPLES, name)
            if os.path.exists(path):
                m = leveler.read_scan(path)
                self.assertEqual((m.xs, m.ys, m.z, m.units), (mesh.xs, mesh.ys, mesh.z, "INCH"), name)

    def test_units(self):
        """As UGS opens a scan (OpenScannedSurfaceAction.toUnits): the points go into the preferred units, inches
        for the user, whatever the file's units= says; a file without it is in those units already."""
        def rebuilt(lo, hi, n):                                          # (SurfaceScanner.reset)
            return [lo + ((hi - lo) * i) / (n - 1) for i in range(n)]
        mesh = wavy_mesh("MM", 25.4)
        k = 1.0 / 25.4                                                    # (UGS UnitUtils.scaleUnits(MM, INCH))
        inch = parse_scan(xyz_text(mesh))
        self.assertEqual(inch.units, "INCH")
        self.assertEqual(inch.xs, rebuilt(mesh.xs[0] * k, mesh.xs[-1] * k, 6))
        self.assertEqual(inch.ys, rebuilt(mesh.ys[0] * k, mesh.ys[-1] * k, 5))
        self.assertEqual(inch.z[3][2], mesh.z[3][2] * k)
        kept = parse_scan(xyz_text(mesh), units="MM")
        self.assertEqual((kept.units, kept.xs, kept.z), ("MM", rebuilt(mesh.xs[0], mesh.xs[-1], 6), mesh.z))
        no_units = xyz_text(mesh, header_units=False)
        self.assertEqual(parse_scan(no_units).xs, rebuilt(mesh.xs[0], mesh.xs[-1], 6))   # (as inches, as UGS)
        self.assertEqual(parse_scan(no_units).z, mesh.z)
        self.assertEqual(parse_scan(no_units, default_units="MM").xs, inch.xs)
        self.assertEqual(parse_scan(xyz_text(mesh).replace("units=MM", "units=mm")).xs, inch.xs)
        with self.assertRaisesRegex(ScanError, "units"):
            parse_scan(xyz_text(mesh).replace("units=MM", "units=FEET"))
        with self.assertRaisesRegex(ScanError, "units"):
            parse_scan(xyz_text(mesh), units="CM")

    def test_grid_rebuilt_from_its_corners_like_ugs(self):
        """UGS keeps only the corners and counts of an opened scan (SurfaceScanner.reset: min + range * i / (n - 1))
        and takes each point's Z from the file point within 0.01: rounded coordinates read the same."""
        text = xyz_text(real_mesh())
        rounded = re.sub(r"^([-\d.]+) ([-\d.]+)", lambda m: f"{float(m.group(1)):.5f} {float(m.group(2)):.5f}",
                         text, flags=re.M)
        self.assertIn("\n1.07143 1.00000 0.01\n", rounded)
        mesh = parse_scan(rounded)
        self.assertEqual(mesh.xs, [0.5 + (8.0 * i) / 14 for i in range(15)])
        self.assertEqual(mesh.z, real_mesh().z)
        nudged = rounded.replace("\n1.07143 1.00000 0.01\n", "\n1.07643 1.00000 0.01\n")    # 0.005 off: it
        self.assertEqual(parse_scan(nudged).z, real_mesh().z)
        off = rounded.replace("\n1.07143 1.00000 0.01\n", "\n1.09143 1.00000 0.01\n")       # 0.02 off: not it
        with self.assertRaisesRegex(ScanError, "X1.09143 Y1.00000 is not on an evenly spaced grid"):
            parse_scan(off)

    def test_points_in_any_order_and_spacing(self):
        lines = xyz_text(real_mesh()).splitlines()
        shuffled = lines[:4] + sorted(lines[4:], key=lambda s: hash(s) % 97)
        self.assertEqual(parse_scan("\n".join(shuffled)).z, real_mesh().z)
        self.assertEqual(parse_scan("0 0 1\n1 0 2\n0 1 3\n1 1 4\n").z, [[1.0, 3.0], [2.0, 4.0]])

    def test_bad_scans(self):
        good = xyz_text(real_mesh())
        bad = {
            "": "no points",
            "0 0 0\n1 0 0\n": "at least 2",
            "0 0 0\n1 0 0\n0 1 0\n": "not a full",
            "0 0 0\n1 0 0\n0 1 0\n1 1 0\n1 1 0\n": "5 points are not a full 2 x 2",
            "0 0 0\n1 0 0\n0 1 0\n0 1 0\n": "two points at X0.00000 Y1.00000",
            "0 0 0\n1 0 0\n0 1 0\n1 1 x\n": "not \"x y z\"",
            "0 0 0\n1 0 0\n0 1 0\n1 1\n": "not \"x y z\"",
            "0 0 0\n1 0 0\n0 1 0\n1 1 nan\n": "not a number",
            "0 0 0\n1 0 0\n3 0 0\n0 1 0\n1 1 0\n3 1 0\n": "X1.00000 Y0.00000 is not on an evenly spaced grid",
            good.replace("xSamples=15", "xSamples=14"): "xSamples=14",
        }
        for text, message in bad.items():
            with self.assertRaisesRegex(ScanError, message, msg=text[:30]):
                parse_scan(text)

    def test_height(self):
        mesh = wavy_mesh()
        for i, x in enumerate(mesh.xs):
            for j, y in enumerate(mesh.ys):
                self.assertAlmostEqual(mesh.height(x, y), mesh.z[i][j], places=15)
        rnd = random.Random(5)
        for _ in range(2000):
            x, y = rnd.uniform(-3, 13), rnd.uniform(-3, 11)
            self.assertAlmostEqual(mesh.height(x, y), bilinear(mesh, x, y), places=12)
        # outside: the nearest edge's height, not the edge's slope carried on
        self.assertAlmostEqual(mesh.height(-5, 3), mesh.height(0, 3), places=15)
        self.assertAlmostEqual(mesh.height(14, 20), mesh.z[-1][-1], places=15)
        self.assertAlmostEqual(plane_mesh().height(3.3, 5.1), 0.01 * 3.3 + 0.02 * 5.1, places=12)


class LevelLinesTest(unittest.TestCase):
    """G0/G1, comments, unknown positions, splitting, units, G91 (on z = 0.01x + 0.02y, where the expected Z is
    easy to work out)."""

    def h(self, x, y):
        return 0.01 * x + 0.02 * y

    def test_first_moves_and_z_words(self):
        out = run_lines("G20 G90\nG0 X1 Y1\nG0 Z0.25\nG1 Z-0.02 F10\nG1 X1.1 Y1 F40\ng1 x1.2 z-0.03\nM30\n")
        self.assertEqual(out[:2], ["G20 G90", "G0 X1 Y1"])                    # Z not known yet: as it is
        self.assertEqual(out[2], "G0 Z" + fmt(0.25 + self.h(1, 1)))
        self.assertEqual(out[3], "G1 Z" + fmt(-0.02 + self.h(1, 1)) + " F10")
        self.assertEqual(out[4], "G1 X1.1 Y1 F40Z" + fmt(-0.02 + self.h(1.1, 1)))   # (UGS appends it)
        self.assertEqual(out[5], "g1 x1.2 Z" + fmt(-0.03 + self.h(1.2, 1)))
        self.assertEqual(out[6], "M30")

    def test_comments_and_spaces(self):
        out = run_lines("G0 X1 Y1 Z0.2\nG1 X1.2 Y1 (cut here) F40 ; and here\nG1 X1.3 Z -0.01\n")
        self.assertEqual(out[1], "G1X1.2Y1F40Z" + fmt(0.2 + self.h(1.2, 1)))      # comments go, with the spaces
        self.assertEqual(out[2], "G1X1.3Z" + fmt(-0.01 + self.h(1.3, 1)))         # (a Z word with a space in it)

    def test_untouched_lines(self):
        program = "%\n\n(header)\nG20 G90 G17 G94 G40 G49 G54\nG0 G53 Z0 (retract)\nG54\nM8\nS16000 M3\nG4 P2\n"
        self.assertEqual(run_lines(program + "G0 X1 Y1 Z0.2\n")[:9], program.splitlines())

    def test_machine_coordinates_and_homing_forget_the_axes(self):
        out = run_lines("G0 X1 Y1 Z0.2\nG0 G53 Z0\nG0 X2 Y2\nG1 X3 Y2 F40\nG0 Z0.2\nG28 Z1\nG0 X2\nG0 Z0.1\n"
                        "G38.2 Z-1 F5\nG0 X1\nG0 Z0.3\n$H\nG0 X2 Y2\nG0 Z0.2\n")
        self.assertEqual(out[1:4], ["G0 G53 Z0", "G0 X2 Y2", "G1 X3 Y2 F40"])   # Z unknown after G53
        self.assertEqual(out[4], "G0 Z" + fmt(0.2 + self.h(3, 2)))
        self.assertEqual(out[5:7], ["G28 Z1", "G0 X2"])                         # G28 Z: only Z unknown
        self.assertEqual(out[7], "G0 Z" + fmt(0.1 + self.h(2, 2)))
        self.assertEqual(out[8:10], ["G38.2 Z-1 F5", "G0 X1"])                  # probing: Z unknown
        self.assertEqual(out[10], "G0 Z" + fmt(0.3 + self.h(1, 2)))
        self.assertEqual(out[11:13], ["$H", "G0 X2 Y2"])                        # homing: all unknown
        self.assertEqual(out[13], "G0 Z" + fmt(0.2 + self.h(2, 2)))

    def test_tool_change_forgets_m0_does_not(self):
        # (moves under MESH's split length, 0.8125 in, so they stay one line each)
        out = run_lines("G0 X1 Y1 Z0.2\nM0\nG1 X1.5 Y1 F40\nM6 T2\nG1 X2 Y1\nG0 Z0.2\nG1 X2.5\n")
        self.assertEqual(out[2], "G1 X1.5 Y1 F40Z" + fmt(0.2 + self.h(1.5, 1)))  # FluidNC can't jog while paused
        self.assertEqual(out[3:5], ["M6 T2", "G1 X2 Y1"])                       # the tool change: unknown
        self.assertEqual(out[5], "G0 Z" + fmt(0.2 + self.h(2, 1)))
        self.assertEqual(out[6], "G1 X2.5Z" + fmt(0.2 + self.h(2.5, 1)))
        result = level_text("G0 X1 Y1 Z0.2\nM6 T2\nG1 X3 Y1\nG1 Z0.1\n", MESH)
        self.assertEqual(result.stats["unleveledLines"], [3])
        self.assertTrue(any("lines 3" in w for w in result.warnings), result.warnings)

    def test_long_moves_are_split(self):
        result = level_text("G0 X1 Y1 Z0.2\nN7 G1 X4 Y1 Z-0.1 F40 S900 M30 (cut)\n", MESH)
        out, step = result.lines, MESH.split_length()      # (2.5 + 4) / 2 / 4 = 0.8125
        n = math.ceil(math.hypot(3, 0.3) / step)
        self.assertEqual(n, 4)
        self.assertEqual(out[1], "N7F40S900(cut)")                     # the line's other words first ...
        self.assertEqual(out[-1], "M30")                               # ... its program end last
        pieces = out[2:-1]
        self.assertEqual(len(pieces), n)
        for k, line in enumerate(pieces, 1):
            w = words(line)
            self.assertTrue(line.startswith("G1X"), line)
            self.assertAlmostEqual(w["X"], 1 + 3 * k / n, places=4)
            z = -0.1 * 0 + 0.2 + (-0.1 - 0.2) * k / n
            self.assertAlmostEqual(w["Z"], z + self.h(w["X"], 1), delta=0.00011)
        self.assertEqual(pieces[-1], "G1X4Y1Z" + fmt(-0.1 + self.h(4, 1)))
        self.assertEqual(list(result.source), [1] + [2] * (n + 2))
        self.assertEqual(result.stats["split"], 1)
        rapid = run_lines("G0 X1 Y1 Z0.2\nG0 X9 Y7\n")
        self.assertTrue(all(line.startswith("G0X") for line in rapid[1:]), rapid)

    def test_g91_follows_the_surface_without_drift(self):
        rnd = random.Random(3)
        lines = ["G90 G0 X1 Y1 Z0.1", "G91"]
        x = y = 1.0
        z = 0.1
        for _ in range(3000):                       # small moves with 5 decimals: each rounds to 4
            dx, dy, dz = rnd.choice((0.00013, -0.00007, 0.00211)), 0.00017, rnd.choice((0.0, 0.00003))
            lines.append(f"G1 X{dx:.5f} Y{dy:.5f} Z{dz:.5f} F40")
            x, y, z = x + dx, y + dy, z + dz
        out = level(lines, MESH).lines
        mx, my, mz = 1.0, 1.0, float(words(out[0])["Z"])
        self.assertAlmostEqual(mz, 0.1 + self.h(1, 1), places=4)
        px, py, pz = 1.0, 1.0, 0.1
        for src, line in zip(lines[2:], out[2:]):
            w, d = words(line), words(src)
            mx, my, mz = mx + w["X"], my + w["Y"], mz + w["Z"]
            px, py, pz = px + d["X"], py + d["Y"], pz + d["Z"]
            self.assertLess(abs(mz - (pz + self.h(px, py))), 0.0000501)    # never further than a rounding
        self.assertAlmostEqual(px, x, places=9)
        long = level(["G0 X1 Y1 Z0.1", "G91 G1 X3 Y1.5 Z-0.2 F30"], MESH).lines
        self.assertEqual(long[1], "G91F30")                  # (the line's G91 runs before the pieces, as UGS)
        total = [sum(words(line)[k] for line in long[2:]) for k in "XYZ"]
        self.assertAlmostEqual(total[0], 3, places=9)
        self.assertAlmostEqual(total[1], 1.5, places=9)
        self.assertAlmostEqual(total[2], -0.2 + self.h(4, 2.5) - self.h(1, 1), delta=0.00005)

    def test_g91_from_an_unknown_position_is_left(self):
        result = level_text("G91 G1 X1 Y1 F40\nG1 Z-0.1\nG90 G0 X1 Y1 Z0.2\n", MESH)
        self.assertEqual(result.lines[:2], ["G91 G1 X1 Y1 F40", "G1 Z-0.1"])
        self.assertEqual(result.stats["unleveledLines"], [1, 2])
        with self.assertRaisesRegex(LevelError, "Nothing in this program can be leveled"):
            level_text("G91 G1 X1 Y1 F40\nG1 Z-0.1\n", MESH)          # (said, not run as if leveled)

    def test_millimeter_program_on_an_inch_scan(self):
        # (mm moves under the split length, 0.8125 in = 20.6375 mm)
        out = run_lines("G21 G90\nG0 X25.4 Y50.8 Z5\nG1 X40 F500\nG20\nG1 X2\n")
        self.assertEqual(out[1], "G0 X25.4 Y50.8 Z" + fmt(5 + self.h(1, 2) * 25.4))
        self.assertEqual(out[2], "G1 X40 F500Z" + fmt(5 + self.h(40 / 25.4, 2) * 25.4))
        self.assertEqual(out[4], "G1 X2Z" + fmt(5 / 25.4 + self.h(2, 2)))         # (the position converts)

    def test_millimeter_scan(self):
        xs, ys = [0.0, 12.5, 25.0, 37.5, 50.0], [0.0, 25.0, 50.0]          # z = 0.01x + 0.02y, all in mm
        mesh = Mesh(xs, ys, [[0.01 * x + 0.02 * y for y in ys] for x in xs], "MM")
        out = level(["G20 G0 X1 Y1 Z0.1", "G21 G1 X27 Y24 Z-1"], mesh).lines   # (under its 4.6875 mm splits)
        self.assertEqual(out[0], "G20 G0 X1 Y1 Z" + fmt(0.1 + (0.01 * 25.4 + 0.02 * 25.4) / 25.4))
        self.assertEqual(out[1], "G21 G1 X27 Y24 Z" + fmt(-1 + 0.01 * 27 + 0.02 * 24))

    def test_z_surface(self):
        out = level(["G0 X1 Y1 Z0.2"], MESH, {"z_surface": 0.01}).lines
        self.assertEqual(out[0], "G0 X1 Y1 Z" + fmt(0.2 + self.h(1, 1) - 0.01))

    def test_outside_the_scan(self):
        result = level_text("G0 X1 Y1 Z0.2\nG1 X1 Y9 F40\nG1 X12 Y9\n", MESH)
        self.assertEqual(result.lines[-1], "G1X12Y9Z" + fmt(0.2 + self.h(10, 8)))    # the edge's height
        points = [words(line) for line in result.lines if "Z" in words(line)]
        outside = [w for w in points if w["X"] > 10 or w["Y"] > 8]
        self.assertEqual(result.stats["outside"], len(outside))
        self.assertEqual(result.stats["leveled"], len(points))
        self.assertGreater(len(outside), 10)
        for w in outside:                                            # clamped: the nearest edge point's height
            self.assertAlmostEqual(w["Z"], 0.2 + self.h(min(w["X"], 10), min(w["Y"], 8)), delta=0.00011)
        self.assertAlmostEqual(result.stats["maxOutside"], math.hypot(2, 1), places=9)
        self.assertTrue(any(f"{len(outside)} of {len(points)} leveled points are outside the scan" in w
                            for w in result.warnings), result.warnings)
        with self.assertRaisesRegex(LevelError, "doesn't cover this program"):
            level_text("G0 X20 Y20 Z0.2\nG1 X30\n", MESH)

    def test_nothing_to_level(self):
        with self.assertRaisesRegex(LevelError, "Nothing in this program can be leveled"):
            level_text("G0 X1 Y1\nG1 X2 Y2 F40\nG0 G53 Z0\n", MESH)

    def test_program_end_resets_modes(self):
        out = run_lines("G0 X1 Y1 Z0.2\nG91 G18\nM30\nX1.5\n")
        self.assertEqual(out[-1], "X1.5Z" + fmt(0.2 + self.h(1.5, 1)))   # absolute G1 again, as FluidNC resets

    def test_work_coordinate_systems(self):
        program = "G55\nG0 X1 Y1 Z0.2\nG1 X2\n"
        with self.assertRaisesRegex(LevelError, "Line 2: the program moves in G55 but the scan was taken in G54"):
            level_text(program, MESH)
        self.assertEqual(level_text(program, MESH, {"wcs": "G55"}).lines[1], "G0 X1 Y1 Z" + fmt(0.2 + self.h(1, 1)))
        out = run_lines("G0 X1 Y1 Z0.2\nG10 L2 P2 X0 Y0\nG1 X2\n")       # another system's offsets: fine
        self.assertEqual(out[1], "G10 L2 P2 X0 Y0")

    def test_refused(self):
        cases = [
            ("G0 X1 Y1 Z0.2\nG92 X0 Y0", 2, "changes the work coordinates"),
            ("G92.1\nG0 X1 Y1 Z0.2", 1, "changes the work coordinates"),
            ("G10 L20 P0 Z0\nG0 X1 Y1 Z0.2", 1, "changes the work coordinates of G54"),
            ("G10 L2 P1 X1\nG0 X1 Y1 Z0.2", 1, "changes the work coordinates of G54"),
            ("G10 L1 P1 X1", 1, "L2 or L20"),
            ("G93 G1 X1 Y1 Z0 F2", 1, "inverse time"),
            ("G0 X1 Y1 Z0.2\n#1=2", 2, "#parameters"),
            ("G0 X[1+2] Y1 Z0.2", 1, "expressions"),
            ("O100 sub", 1, "O-words"),
            ("G0 X1 Y1 Z0.2\nG1 A10", 2, "only knows the X, Y and Z"),
            ("G64 P0.01", 1, "G64 is not a code FluidNC runs"),
            ("G0 X1 Y1 Z0.2\nG90.1", 2, "G90.1 is not a code"),
            ("M60", 1, "M60 is not a code"),
            ("G0 X1 Y1 Z0.2\nG1 X1 X2", 2, "two X words"),
            ("G0 G1 X1", 1, "G0 and G1 on one line"),
            ("M7 M8", 1, "M7 and M8 on one line"),
            ("X1 Y1 Z0.2", 1, "before the program has set one"),
            ("G80\nX1 Y1 Z0", 2, "G80"),
            ("G0 X1 Y1 Z0.2\nG1 X2 S100:200:300", 2, "cluster"),
            ("$10=1\nG0 X1 Y1 Z0.2", 1, "FluidNC command"),
            ("G0 X1 Y1 Z0.2\nG28.1 X1", 2, "G28.1/G30.1 with axis words"),
            ("G0 X1 Y1 Z0.2\nG1 H1", 2, "doesn't take H"),
        ]
        for program, line, fragment in cases:
            with self.assertRaises(LevelError, msg=program) as cm:
                level_text(program + "\nG0 X2 Y2 Z0.2\n", MESH)
            self.assertEqual(cm.exception.line, line, (program, str(cm.exception)))
            self.assertIn(fragment, str(cm.exception), program)
            self.assertTrue(str(cm.exception).startswith(f"Line {line}: "), str(cm.exception))

    def test_settings_checked(self):
        for bad in ({"z_surface": "x"}, {"arc_segment_mm": 0}, {"wcs": "G60"}, {"max_lines": 0}):
            with self.assertRaises(LevelError, msg=bad):
                level(["G0 X1 Y1 Z0"], MESH, bad)

    def test_size_guard(self):
        with self.assertRaisesRegex(LevelError, "Line 2: the leveled program would be longer than 5 lines"):
            level(["G0 X0 Y0 Z0.1", "G1 X9 Y7 F40"], MESH, {"max_lines": 5})


class LevelArcsTest(unittest.TestCase):
    """G2/G3 become G1 lines on the arc FluidNC would cut, each leveled."""

    def h(self, x, y):
        return 0.01 * x + 0.02 * y

    def arc(self, program, settings=None):
        """The leveled G1 pieces of the last line of program, as (x, y, z unleveled) points."""
        result = level_text(program, MESH, settings)
        last = len(program.strip().splitlines())
        pts = []
        for line, src in zip(result.lines, result.source):
            if src == last and line.startswith("G1X"):
                w = words(line)
                pts.append((w["X"], w["Y"], w["Z"] - self.h(w["X"], w["Y"])))
        return result, pts

    def assert_on_circle(self, pts, cx, cy, r, tol=0.00008):
        for x, y, _ in pts:
            self.assertAlmostEqual(math.hypot(x - cx, y - cy), r, delta=tol)

    def angles(self, pts, cx, cy):
        return [math.atan2(y - cy, x - cx) for x, y, _ in pts]

    def test_quarter_arcs_both_directions(self):
        start = "G20 G90\nG0 X2 Y1 Z0.1\nG1 Z-0.02 F10\n"
        for code, end, sign in (("G2", "X1 Y0", -1), ("G3", "X1 Y2", 1)):
            result, pts = self.arc(start + f"{code} {end} I-1 J0 F40\n")
            n = math.ceil(math.pi / 2 * 1 * 25.4 / 0.01)               # UGS: arc length / 0.01 mm, rounded up
            self.assertEqual(len(pts), n)
            self.assert_on_circle(pts, 1, 1, 1)
            a = self.angles(pts, 1, 1)
            self.assertTrue(all(sign * (b - c) > 0 for c, b in zip(a, a[1:])), code)
            self.assertAlmostEqual(abs(a[-1] - 0), math.pi / 2, places=4)
            self.assertTrue(all(abs(z - -0.02) < 0.00011 for _, _, z in pts))
            self.assertEqual(result.lines[3], "F40")                     # (the arc's F first, as UGS)
            self.assertEqual(result.stats["arcs"], 1)

    def test_segment_length_setting(self):
        _, pts = self.arc("G0 X2 Y1 Z0\nG3 X1 Y2 I-1 J0 F40\n", {"arc_segment_mm": 1.0})
        self.assertEqual(len(pts), math.ceil(math.pi / 2 * 25.4))        # 40 lines of under 1 mm
        _, mm = self.arc("G21\nG0 X50 Y25 Z0\nG3 X25 Y50 I-25 J0 F40\n", {"arc_segment_mm": 1.0})
        self.assertEqual(len(mm), math.ceil(math.pi / 2 * 25))

    def test_full_and_helical_circles(self):
        _, pts = self.arc("G0 X2 Y1 Z0\nG2 X2 Y1 I-1 J0 Z-0.1 F40\n", {"arc_segment_mm": 0.5})
        self.assertEqual(len(pts), math.ceil(2 * math.pi * 25.4 / 0.5))
        self.assert_on_circle(pts, 1, 1, 1)
        zs = [z for _, _, z in pts]
        self.assertAlmostEqual(zs[len(zs) // 2], -0.05, delta=0.002)     # Z goes down evenly around the circle
        self.assertAlmostEqual(zs[-1], -0.1, delta=0.0001)
        _, two = self.arc("G0 X2 Y1 Z0\nG2 X2 Y1 I-1 J0 Z-0.1 P2 F40\n", {"arc_segment_mm": 0.5})
        self.assertEqual(len(two), math.ceil(4 * math.pi * 25.4 / 0.5))  # P2: one more turn (FluidNC)

    def test_r_arcs_match_their_ij_form(self):
        base = "G0 X2 Y2 Z0\n"
        for r_arc, ij_arc in (("G2 X3 Y3 R1", "G2 X3 Y3 I1 J0"), ("G2 X3 Y3 R-1", "G2 X3 Y3 I0 J1"),
                              ("G3 X3 Y3 R1", "G3 X3 Y3 I0 J1"), ("G3 X3 Y3 R-1", "G3 X3 Y3 I1 J0")):
            _, a = self.arc(base + r_arc + "\n", {"arc_segment_mm": 0.2})
            _, b = self.arc(base + ij_arc + "\n", {"arc_segment_mm": 0.2})
            self.assertEqual(len(a), len(b), r_arc)
            for p, q in zip(a, b):
                self.assertAlmostEqual(p[0], q[0], places=4)
                self.assertAlmostEqual(p[1], q[1], places=4)

    def test_other_planes(self):
        # G18 (ZX): Z is axis 0, X axis 1 (GCode.cpp:1184), I and K the center; Y moves linearly
        result = level_text("G0 X1 Y1 Z0\nG18 G2 X2 Z1 I0 K1 Y2 F40\n", MESH, {"arc_segment_mm": 0.5})
        self.assertEqual(result.lines[1], "G18F40")                   # (the line's other words first)
        pts = [words(line) for line, src in zip(result.lines, result.source) if src == 2 and line.startswith("G1X")]
        self.assertGreater(len(pts), 10)
        for w in pts:
            z = w["Z"] - self.h(w["X"], w["Y"])
            self.assertAlmostEqual(math.hypot(z - 1, w["X"] - 1), 1, delta=0.0002)
        self.assertGreater(max(w["X"] for w in pts), 1.9)              # CW seen from +Y: through X 2
        self.assertTrue(all(w["Y"] >= 1 - 1e-9 for w in pts))
        result = level_text("G0 X1 Y1 Z0\nG19 G3 Y2 Z1 J0 K1 F40\n", MESH, {"arc_segment_mm": 0.5})
        pts = [words(line) for line, src in zip(result.lines, result.source) if src == 2 and line.startswith("G1X")]
        self.assertGreater(len(pts), 10)
        for w in pts:
            z = w["Z"] - self.h(w["X"], w["Y"])
            self.assertAlmostEqual(math.hypot(w["Y"] - 1, z - 1), 1, delta=0.0002)
            self.assertAlmostEqual(w["X"], 1, places=9)

    def test_refused_arcs(self):
        cases = [("G2 X3 Y1 R0.5", "too small"), ("G2 X3 Y1 I0.8 J0", "not on its circle"),     # (0.4 in off)
                 ("G2 X1 Y1.000001 I1 J0", "ends a hair from its start"),   # FluidNC: full circle or nothing
                 ("G2 Z-1 I1", "needs X or Y"), ("G2 X3 Y1 R1 I1", "both R and I/J/K"),
                 ("G2 X3 Y1 I1 P1.5", "whole number"), ("G2 X1 Y1 R1", "ends where it starts"),
                 ("G2 X3 Y1", "needs I, J or K")]
        for arc, fragment in cases:
            with self.assertRaises(LevelError, msg=arc) as cm:
                level_text("G0 X1 Y1 Z0\n" + arc + "\n", MESH)
            self.assertEqual(cm.exception.line, 2, arc)
            self.assertIn(fragment, str(cm.exception), arc)
        with self.assertRaisesRegex(LevelError, "Line 2: this arc would become 3,19"):     # (before making them)
            level_text("G0 X1 Y1 Z0\nG2 X1 Y1 I1 P200 F40\n", MESH)                # 200 turns of 0.01 mm lines
        # an exact circle is not a hair off, though (x + I) - x is not quite I: its angle is rounding noise
        _, pts = self.arc("G0 X1.2345 Y2.3456 Z0\nG3 X1.2345 Y2.3456 I0.0633 J-0.6824 F40\n", {"arc_segment_mm": 1})
        self.assertEqual(len(pts), math.ceil(2 * math.pi * math.hypot(0.0633, 0.6824) * 25.4))

    def test_words_around_the_arc_as_ugs_keeps_them(self):
        """UGS extractMotion + splitProgramStops: the other words, comments where they were, on a line before the
        pieces, the program stop after them; the arc's P (turns) is not left on a line of its own (FluidNC would
        refuse a line with only P: unused word)."""
        result = level_text("G0 X2 Y1 Z0\nN5 (a) G2 X1 Y0 I-1 J0 F40 (b) S900 M3 M30\n", MESH)
        mine = [line for line, src in zip(result.lines, result.source) if src == 2]
        self.assertEqual(mine[0], "N5(a)F40(b)S900M3")
        self.assertEqual(mine[-1], "M30")
        self.assertTrue(all(line.startswith("G1X") for line in mine[1:-1]))
        two = level_text("G0 X2 Y1 Z0\nG2 X2 Y1 I-1 J0 P2 F40\n", MESH, {"arc_segment_mm": 1}).lines
        self.assertEqual(two[1], "F40")

    def test_arc_from_an_unknown_position(self):
        out = run_lines("G0 X0 Y0 Z0.1\nG2 X1 Y0 I0.5 J0 F40\nM6 T2\nX0 Y0 I-0.5 J0\nG0 Z0.1\n")
        self.assertEqual(out[-3:-1], ["M6 T2", "G2X0 Y0 I-0.5 J0"])      # (the machine is in G1 after the pieces)
        self.assertEqual(run_lines("G2 X1 Y0 I0.5 J0 F40\nG0 X1 Y1 Z0\n")[0], "G2 X1 Y0 I0.5 J0 F40")


def ugs_wavy_xyz(mm=False):
    """The scan the UGS runs below were made with: wavy_mesh() as a .xyz, in inches or (mm=True) in mm."""
    out = ["# minX=0.00000 maxX=10.00000", "# minY=0.00000 maxY=8.00000", "# xSamples=6 ySamples=5", "# units=INCH"]
    if mm:
        out = ["# minX=0.00000 maxX=254.00000", "# minY=0.00000 maxY=203.20000", "# xSamples=6 ySamples=5",
               "# units=MM"]
    for i in range(6):
        for j in range(5):
            x, y = 2.0 * i, 2.0 * j
            z = 0.02 * math.sin(x / 3) + 0.01 * math.cos(y / 2) - 0.005 + 0.003 * ((i * 7 + j * 3) % 4)
            if mm:
                x, y, z = x * 25.4, y * 25.4, z * 25.4
            out.append(f"{x!r} {y!r} {z!r}")
    return "\n".join(out) + "\n"


UGS_PROGRAMS = {
    "lines": "G20 G90 G17 G94 G54\nG0 G53 Z0\nG0 X1 Y1\nG0 Z0.25\nG1 Z-0.02 F15\nG1 X1.3 Y1.1 F40\nG1 X9 Y1\n"
             "G1 X9 Y7 (long) ; done\nG1 X8.8 Y6.9 Z-0.03\nN12 G1 X1 Y7 F35 S900 M3\nX1.2\nG0 Z0.25\nM30\n",
    "arcs": "G20 G90\nG0 X2 Y2 Z0.25\nG1 Z-0.02 F15\nG2 X3 Y3 I1 J0 F40\nG3 X4 Y4 I1 J0\nG2 X4 Y4 I1 J0 Z-0.04\n"
            "G3 X6 Y4 I1 J0 Z-0.02\nG1 X7 Y6\nG3 X5 Y6 I-1 J0\nG2 X7 Y6 I1 J0 Z-0.03\nX5 Y6 I-1 J0\nG0 Z0.25\n",
    "rarcs": "G20 G90\nG0 X2 Y2 Z0.2\nG1 Z-0.02 F15\nG2 X3 Y3 R1 F40\nG2 X4 Y4 R-1\nG3 X5 Y5 R1\n"
             "G3 X6 Y4 R-1.2\nG0 Z0.2\n",
    "planes": "G20 G90\nG0 X2 Y2 Z0.1\nG18 G2 X3 Z1.1 I0 K1 F40\nG19 G3 Y3 Z0.1 J0 K-1\nG17\nG0 Z0.2\n",
    "mm": "G21 G90\nG0 X25 Y25 Z6\nG1 Z-0.5 F400\nG1 X225 Y25 F1000\nG3 X250 Y50 I0 J25\nG1 X250 Y150 Z-0.8\n"
          "G2 X200 Y150 I-25 J0\nG0 Z6\n",
    "commentorder": "G20 G90\nG0 X1 Y1 Z0.2\nG1 (a) X9 F40 (b)\nG1 X1 (c) Y1 M30\n",
    "rapidsplit": "G20 G90\nG0 X1 Y1 Z0.2\nG0 X9 Y7\nG0 Z0.5\n",
    "arc2d": "G20 G90\nG0 X1 Y1\nG2 X2 Y2 I1 J0 F40\nG0 Z0.1\nG1 X3 Y3\n",
    "arcwords": "G20 G90\nG0 X1 Y1 Z0.2\nN5 G2 X3 Y1 I1 J0 F40 S1000 M3 (x)\nG3 X1 Y1 I-1 J0 M30\n",
    "tlo": "G20 G90\nG0 X1 Y1 Z0.2\nG43.1 Z0.1\nG1 X1.2 F30\nG0 Z0.3\nG1 X1.4\nG49\nG1 X1.5\n",
    "edge": "G20 G90\nG0 X-1 Y-1 Z0.2\nG1 X12 Y-1 F40\nG1 X12 Y10\nG1 X5 Y4\nG0 Z0.5\n",
    "unitsswitch": "G21 G90\nG0 X25.4 Y50.8 Z5\nG1 X50.8 F500\nG20\nG1 X2.5\nG21 G1 X30 Y30\n",
}
# SHA-256 of what UGS itself wrote for them: (scan in mm, Z surface, arc segment mm) -> program -> sum. Made with
# the user's UGS fork (ugs-core-2.0-SNAPSHOT.jar) run outside the GUI as its Export does it (ExportGcodeAction: a
# fresh GcodeParser with Utils.createCommandProcessor's ArcExpander, LineSplitter and MeshLeveler,
# GcodeParserUtils.processAndExport, SimpleGcodeStreamWriter), the scan opened as OpenScannedSurfaceAction opens it,
# preferred units inches. The same harness gives lev.gcode from orig.nc + scan.xyz byte for byte.
UGS_SHA256 = {
    (False, 0.0, 0.5): {
        "lines": "ff11159d3057ee3bee2f14290c6e13f8b6ab24e75a1330de81a34ea904ff5aca",
        "arcs": "df0b1ed45d0fcac66520d34fb17acd838002fc40278f0ef06194772287eb3105",
        "rarcs": "e7203993b04076ff4dcec84b649b725adbc49ec6772845fb2cd8cd785f47a5de",
        "planes": "d547b3a0e7e6c177b5e60ea6c0ba06ef28aa75cdfe45fa3b96e85b40f929ed65",
        "mm": "71330c57b656ade571d83fb4f31eba03123f8f3588065fd2a42e1683ead50fa7",
        "commentorder": "686bc5bd419ee453331c2c7c5b2084588caebe6a0b498da0b57506dd48948284",
        "arcwords": "e3326dd862182880974e65b4f3bc51421700bc4d9eb89805d9d9bda67df9952d",
        "arc2d": "d5374cf623debc3142da24d9a2035e717e11f02eed2103f3a6f9a9241bd5fce8",
        "unitsswitch": "84a6cdf647418b3b5c49a4b1f40b3e912b4d5850ec358edb4102d4def77b84e2",
        "edge": "5f27b6d133e9bcec9a4fb678d8631fc336d364230fbbc07306a86e24412d806b",
        "rapidsplit": "ffb9c392e09564c164d78c7e29392a6d53f262d10fb047fc8c35212cb12d0825",
        "tlo": "31961af85b854aa4229699a2f57f4a33dd8390e2c4c0b1c8cfcb8e23d7a3d534",
    },
    (False, 0.0, 0.01): {                       # the user's arc segment (63850 and 35265 lines)
        "arcs": "ca24c2392d4cd90a18437adf0241e26e320471059ca80d46f25596a6673a89f3",
        "rarcs": "65a36df8299c594960d9b5be406da5857429e2f2ea6bc894f175e24338a06b90",
    },
    (True, 0.01, 0.5): {                        # a scan saved in mm (UGS reads it into inches), Z surface 0.01 in
        "lines": "d39263ed989325eae1918fbae93370f335dfec156a1f051d5bfe9bfa08fc7ba5",
        "arcs": "e1a2ae9fc59722c217b2a74d910b1963a7f8ed7e13d18fea6c7201d837b9e90b",
        "rarcs": "41d522a4786e88158aba7aa0fdabd171529e897ac3626669de1b65f3b2738f48",
        "mm": "1db8500e47f1dfcc6506460f5127a84b8321db79d2097122ec3c59fe4fb424e1",
        "unitsswitch": "b95ec7c48c52666f6de3e0652dabd80bcb33379822e7c6cf27a10abfeeb270cb",
    },
}


class UgsTest(unittest.TestCase):
    """Byte for byte what UGS itself writes (G0/G1, splits, I/J and R arcs both ways, full and helical circles,
    G18/G19, G21 programs, unit switches, comments, N/F/S/M3/M30 around split moves, G43.1/G49, outside the
    scan, a mm scan file, a Z surface). Where leveler.py deliberately differs (G91, M0, "Z -0.5", arc P, G92 ...)
    the other tests say so."""

    def test_same_as_ugs(self):
        import hashlib
        for (mm, z_surface, segment), sums in UGS_SHA256.items():
            mesh = parse_scan(ugs_wavy_xyz(mm))
            self.assertEqual(mesh.units, "INCH")
            for name, digest in sums.items():
                result = level_text(UGS_PROGRAMS[name], mesh, {"z_surface": z_surface, "arc_segment_mm": segment})
                got = hashlib.sha256(result.text().encode()).hexdigest()
                self.assertEqual(got, digest, (name, mm, z_surface, segment))


@unittest.skipUnless(os.path.exists(CHECKER), "the user's checker is not here")
class CheckerTest(unittest.TestCase):
    """The user's own checker on our output: python3 -I level_check.py original leveled scan.xyz."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", newline="") as f:
            f.write(text)
        return path

    def check(self, original, leveled, scan, *extra):
        p = subprocess.run([sys.executable, "-I", CHECKER, original, leveled, scan, *extra],
                           capture_output=True, text=True, timeout=600)
        return p.returncode, p.stdout + p.stderr

    def assert_pass(self, program, mesh, settings=None, extra=(), original=None):
        """Levels program with mesh, and the checker must say PASS (original: the program it is checked
        against, if not program itself). Returns (result, checker output)."""
        result = level_text(program, mesh, settings)
        code, out = self.check(self.write("orig.nc", original or program), self.write("leveled.nc", result.text()),
                               self.write("scan.xyz", xyz_text(mesh)), *extra)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)
        checked = int(re.search(r"Checked (\d+) moves", out).group(1))
        self.assertGreater(checked, 0, out)
        return result, out

    def test_the_users_job_comes_out_as_ugs_wrote_it(self):
        paths = [os.path.join(SAMPLES, n) for n in ("orig.nc", "lev.gcode", "scan.xyz", "scan_from_log.xyz")]
        if not all(map(os.path.exists, paths)):
            self.skipTest("the sample files are not here")
        orig, ugs, scan, scan_log = paths
        with open(orig, newline="") as f:
            text = f.read()
        with open(ugs, newline="") as f:
            expected = f.read()
        result = level_text(text, leveler.read_scan(scan))
        self.assertEqual(result.text(), expected)                          # byte for byte UGS's lev.gcode
        self.assertEqual(level_text(text, leveler.read_scan(scan_log)).lines, result.lines)
        self.assertEqual(result.stats["split"], 40)
        self.assertEqual(result.stats["unleveled"], 0)
        self.assertTrue(any("outside the scan" in w for w in result.warnings))  # (Y 0.6 cut, scan from Y 1)
        code, out = self.check(orig, self.write("ours.nc", result.text()), scan)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)
        self.assertIn("Checked 55265 moves", out)

    def test_the_checkers_own_fixtures(self):
        """good.nc and bad.nc were leveled with 4.xyz from a program at Z-0.05 (rapids at 0.2): the checker must
        pass good.nc and fail bad.nc, and our leveling of that program must pass and agree with good.nc."""
        paths = [os.path.join(SAMPLES, n) for n in ("good.nc", "bad.nc")]
        if not all(map(os.path.exists, paths)):
            self.skipTest("the sample files are not here")
        mesh = parse_scan(SCAN4)
        scan = self.write("4.xyz", SCAN4)
        with open(paths[0]) as f:
            good = f.read().splitlines()
        original = []
        for line in good:
            w = words(line)
            if "X" in w and "Z" in w:
                z = round(w["Z"] - mesh.height(w["X"], w["Y"]), 4)
                original.append(re.sub(r"Z-?[\d.]+", f"Z{z:.4f}", line))
            else:
                original.append(line)
        self.assertEqual({words(line)["Z"] for line in original[3:-1]}, {-0.05})       # the cuts
        self.assertEqual({words(line)["Z"] for line in (original[2], original[-1])}, {0.2})   # the rapids
        orig = self.write("orig.nc", "\n".join(original) + "\n")
        code, out = self.check(orig, paths[0], scan)
        self.assertEqual(code, 0, out)
        code, out = self.check(orig, paths[1], scan)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL", out)
        result = level(original, mesh)
        code, out = self.check(orig, self.write("ours.nc", result.text()), scan)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)
        ends = {}                                   # program line -> the last output line it became (its end)
        for line, src in zip(result.lines, result.source):
            ends[src] = line
        for no in range(3, len(good) + 1):          # each move's end as good.nc has it
            self.assertAlmostEqual(words(ends[no])["Z"], words(good[no - 1])["Z"], delta=0.00011, msg=no)

    def test_lines_rapids_and_plunges_in_inches(self):
        program = """G20 G90 G17 G94 G54
G0 G53 Z0
G0 X1 Y1
G0 Z0.25
G0 Z0.05
G1 Z-0.02 F15
G1 X9 Y1 F40
G1 X9 Y7
G1 X1 Y7
G1 X1 Y1.5
G1 X5 Y4 Z-0.05
G1 X9 Y7.5 Z-0.01
G0 Z0.25
G0 X3 Y3
G1 Z-0.03 F15
G1 X3.2 Y3.1 F40
G1 X3.4 Y3.3 Z-0.035
G0 Z0.25
G0 G53 Z0
M30
"""
        result, _ = self.assert_pass(program, wavy_mesh())
        self.assertGreater(result.stats["split"], 5)

    def test_arcs_both_directions_in_inches(self):
        program = """G20 G90
G0 X2 Y2 Z0.25
G1 Z-0.02 F15
G2 X3 Y3 I1 J0 F40
G3 X4 Y4 I1 J0
G2 X4 Y4 I1 J0 Z-0.04
G3 X6 Y4 I1 J0 Z-0.02
G1 X7 Y6
G3 X5 Y6 I-1 J0
G2 X7 Y6 I1 J0 Z-0.03
G0 Z0.25
"""
        for segment in (0.25, 0.01):
            result, _ = self.assert_pass(program, wavy_mesh(), {"arc_segment_mm": segment})
            self.assertEqual(result.stats["arcs"], 6)

    def test_r_arcs_checked_against_their_ij_form(self):
        r_form = "G20 G90\nG0 X2 Y2 Z0.2\nG1 Z-0.02 F15\nG2 X3 Y3 R1 F40\nG2 X4 Y4 R-1\nG3 X5 Y5 R1\nG0 Z0.2\n"
        ij_form = r_form.replace("R1 F40", "I1 J0 F40").replace("R-1", "I0 J1").replace("X5 Y5 R1", "X5 Y5 I0 J1")
        self.assertNotIn("R", ij_form)
        self.assert_pass(r_form, wavy_mesh(), {"arc_segment_mm": 0.25}, original=ij_form)

    def test_millimeter_program_on_an_inch_scan(self):
        program = """G21 G90
G0 X25 Y25 Z6
G1 Z-0.5 F400
G1 X225 Y25 F1000
G3 X250 Y50 I0 J25
G1 X250 Y150 Z-0.8
G2 X200 Y150 I-25 J0
G0 Z6
"""
        self.assert_pass(program, wavy_mesh(), {"arc_segment_mm": 0.5})

    def test_millimeter_program_on_a_millimeter_scan(self):
        mesh = wavy_mesh("MM", 25.4)
        program = "G21 G90\nG0 X20 Y20 Z5\nG1 Z-0.5 F300\nG1 X230 Y30\nG2 X230 Y130 I0 J50 Z-1\nG1 X20 Y180\nG0 Z5\n"
        self.assert_pass(program, mesh, {"arc_segment_mm": 0.5})

    def test_z_surface(self):
        program = "G20 G90\nG0 X1 Y1 Z0.2\nG1 Z-0.02 F10\nG1 X9 Y7 F40\nG0 Z0.2\n"
        self.assert_pass(program, wavy_mesh(), {"z_surface": 0.01}, extra=("--zsurface", "0.01"))

    def test_the_checker_catches_a_wrong_z(self):
        program = "G20 G90\nG0 X1 Y1 Z0.2\nG1 Z-0.02 F10\nG1 X2 Y1.5 F40\nG1 X3 Y2\nG0 Z0.2\n"
        result, _ = self.assert_pass(program, wavy_mesh())
        lines = result.lines[:]
        i = max(k for k, src in enumerate(result.source) if src == 5)      # the end of "G1 X3 Y2" (split)
        self.assertTrue(lines[i].startswith("G1X3Y2Z"), lines[i])
        z = words(lines[i])["Z"]
        lines[i] = re.sub(r"Z-?[\d.]+$", "Z" + fmt(z - 0.002), lines[i])
        code, out = self.check(self.write("orig.nc", program), self.write("bad.nc", "\n".join(lines) + "\n"),
                               self.write("scan.xyz", xyz_text(wavy_mesh())))
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL: 1 moves", out)


if __name__ == "__main__":
    unittest.main()
