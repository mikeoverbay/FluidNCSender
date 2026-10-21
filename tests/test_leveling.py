"""
Tests for auto-level (leveling.py, and its hooks in machine.py, scan.py and settings.py):
- unit: the settings, the scan's setup line and the same-setup check;
- the Machine with made-up time (test_machine's protocol harness, no port): Run sends exactly the leveled program,
  the program event / snapshot / GET fields, Leveling off and what else turns it off, the refusals, the setup checked
  when leveling and at Run, leveling outside the lock, the saved settings;
- a Machine connected to the FluidNC simulator (tools/fake_fluidnc.py on a pty) running a leveled job to the end.
Real serial ports are never opened.

    python3 -m unittest tests.test_leveling
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import leveler  # noqa: E402
import leveling  # noqa: E402
import machine  # noqa: E402
import settings  # noqa: E402
import test_jobs as tj  # noqa: E402
from job import parse_program  # noqa: E402
from leveling import LevelingError, check_settings, check_setup, scan_setup, setup_line  # noqa: E402
from machine import MachineError  # noqa: E402
from scan import axis_values, xyz_text  # noqa: E402

SIM = os.path.join(ROOT, "tools", "fake_fluidnc.py")
XS, YS = axis_values(0.0, 2.0, 3), axis_values(0.0, 1.0, 2)
HEIGHTS = {(0, 0): 0.0, (0, 1): 0.01, (1, 0): 0.02, (1, 1): -0.01, (2, 0): 0.0, (2, 1): -0.03}
ZERO = {"wcs": "G54", "wco": [0.0, 0.0, 0.0], "port": "/dev/fake"}     # the harness's FluidNC: WCO 0,0,0, G54
PROGRAM = ("(t.nc)\nG20 G90 G54\nG0 X0 Y0 Z0.1\nG1 Z-0.01 F20 (plunge)\nG1 X1 Y0.5 F40\nG1 X2 Y1\nG0 Z0.2\n"
           "M30\n")


def scan_text(setup=ZERO, when=1760000000):
    """The 3 x 2 scan (X 0..2, Y 0..1 in) as scan.py writes it, with its setup line (setup None: as UGS writes it)."""
    return xyz_text(XS, YS, HEIGHTS, setup_line(setup, when) if setup else None)


def leveled_text(program=PROGRAM, scan=None, z=0.0, arc=0.1, wcs="G54"):
    """What leveler.py makes of the program with the scan (the reference: test_leveler checks it against UGS)."""
    mesh = leveler.parse_scan(scan or scan_text())
    return leveler.level_text(program, mesh, {"z_surface": z, "arc_segment_mm": arc, "wcs": wcs}).text()


def lines_of(text, name="t.nc"):
    """The lines a job sends for this text (without comments and spaces)."""
    p = parse_program(name, text)
    return [p[i][1] for i in range(len(p))]


class FakeScanner:
    """scan.py's Scanner as far as the machine uses it: state() for the snapshot, last_scan() for "the last scan"."""

    def __init__(self, text=None):
        self.text = text

    def state(self):
        return {"running": False, "result": {"complete": self.text is not None}}

    def last_scan(self):
        return {"text": self.text, "savedTo": None, "finished": 1.0} if self.text is not None else None


# ------------------------------------------------------------------ unit

class Settings(unittest.TestCase):
    def test_defaults_saved_and_given(self):
        self.assertEqual(check_settings({}), {"zSurface": 0.0, "arcSegmentMm": 0.1})
        self.assertEqual(check_settings({}, {"zSurface": 0.01, "arcSegmentMm": 0.5}),
                         {"zSurface": 0.01, "arcSegmentMm": 0.5})
        self.assertEqual(check_settings({"arcSegmentMm": 0.01, "zSurface": None}, {"zSurface": -0.02}),
                         {"zSurface": -0.02, "arcSegmentMm": 0.01})
        for z, arc in ((-10, 0.01), (10, 2), (0, 1)):
            self.assertEqual(check_settings({"zSurface": z, "arcSegmentMm": arc}),
                             {"zSurface": float(z), "arcSegmentMm": arc})

    def test_refused(self):
        z_why = "Z surface: a number from -10 to 10 in"
        arc_why = "Arc segment: a number from 0.01 to 2 mm"
        for data, why in (({"zSurface": "0"}, z_why), ({"zSurface": True}, z_why), ({"zSurface": 10.01}, z_why),
                          ({"zSurface": float("nan")}, z_why), ({"arcSegmentMm": 0.009}, arc_why),
                          ({"arcSegmentMm": 2.01}, arc_why), ({"arcSegmentMm": 0}, arc_why),
                          ({"arcSegmentMm": "0.1"}, arc_why), ({"arcSegmentMm": float("inf")}, arc_why)):
            with self.assertRaises(LevelingError, msg=repr(data)) as cm:
                check_settings(data)
            self.assertEqual((cm.exception.status, str(cm.exception)), (400, why))

    def test_settings_file(self):
        """settings.json "level": not in the file until a leveling saves it (a file without it stays as it was)."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            s = settings.Settings(path)
            self.assertIsNone(s.get("level"))
            s.set("jog", {"step": 1, "feed": 99})
            with open(path) as f:
                self.assertNotIn("level", f.read())
            self.assertEqual(s.set("level", {"zSurface": 0, "arcSegmentMm": 0.25}),
                             {"zSurface": 0.0, "arcSegmentMm": 0.25})
            self.assertEqual(settings.Settings(path).get("level"), {"zSurface": 0.0, "arcSegmentMm": 0.25})
            with self.assertRaises(settings.SettingsError):
                s.set("level", {"zSurface": 0, "arcSegmentMm": 3})
            with open(path, "w") as f:
                f.write('{"level": {"zSurface": 99, "arcSegmentMm": 0.1}, "jog": {"step": 1, "feed": 99}}')
            again = settings.Settings(path)
            self.assertEqual((again.get("level"), again.get("jog")), (None, {"step": 1, "feed": 99}))  # (bad: none)


class SetupLine(unittest.TestCase):
    def test_line(self):
        setup = {"wcs": "G54", "wco": [-150 / 25.4, -100 / 25.4, -30 / 25.4], "port": "/dev/pts/3"}
        self.assertEqual(setup_line(setup, 1760700000.9),
                         "# wcs=G54 wcoX=-5.905512 wcoY=-3.937008 wcoZ=-1.181102 time=1760700000")
        self.assertEqual(setup_line({"wcs": "G59.1", "wco": [-1e-9, 0.0, 2.5]}, 5),
                         "# wcs=G59.1 wcoX=0.000000 wcoY=0.000000 wcoZ=2.500000 time=5")
        self.assertEqual(setup_line({"wcs": "G54", "wco": None}, 7), "# wcs=G54 time=7")   # (no WCO reported yet)
        for bad in (None, {}, {"wcs": "G53", "wco": [0, 0, 0]}):
            self.assertIsNone(setup_line(bad, 7))

    def test_read_back_and_ugs_reads_the_same_scan(self):
        """The line goes after UGS's four header lines; UGS reads only its own keys (xSamples, ySamples, units)
        there, and leveler.py reads the scan exactly as without it."""
        text, ugs = scan_text(), scan_text(None)
        self.assertEqual(text.splitlines()[:5], ugs.splitlines()[:4] + [
            "# wcs=G54 wcoX=0.000000 wcoY=0.000000 wcoZ=0.000000 time=1760000000"])
        self.assertEqual(text.splitlines()[5:], ugs.splitlines()[4:])
        a, b = leveler.parse_scan(text), leveler.parse_scan(ugs)
        self.assertEqual((a.xs, a.ys, a.z, a.units), (b.xs, b.ys, b.z, b.units))
        self.assertEqual(scan_setup(a.header), {"wcs": "G54", "wco": [0.0, 0.0, 0.0], "time": 1760000000})
        self.assertIsNone(scan_setup(b.header))
        self.assertEqual(scan_setup({"wcs": "g55", "wcoX": "1", "time": "x"}),
                         {"wcs": "G55", "wco": None, "time": None})
        self.assertEqual(scan_setup({"wcs": "G99", "wcoX": "1", "wcoY": "2", "wcoZ": "nan"}),
                         {"wcs": None, "wco": None, "time": None})


class ScanFiles(unittest.TestCase):
    def test_points_on_one_line_are_a_scan_error(self):
        """(leveler.py fix) A header that says 2 x 2 with every point at one X (or one Y) divided by a zero grid
        step: a ZeroDivisionError, which the level call doesn't answer. Now a ScanError (the call answers 409)."""
        for text, axis in (("# xSamples=2 ySamples=2\n0 0 0\n0 0 0\n0 1 0\n0 1 0\n", "X"),
                           ("# xSamples=2 ySamples=2\n0 0 0\n1 0 0\n0 0 0\n1 0 0\n", "Y")):
            with self.assertRaises(leveler.ScanError) as cm:
                leveler.parse_scan(text)
            self.assertEqual(str(cm.exception), f"The scan's 4 points all have the same {axis}: a scan needs at least "
                                                "2 points along X and along Y")


class SameSetup(unittest.TestCase):
    FIRST = (0.5, 1.0)

    def check(self, scan, now, at_run=False):
        return check_setup(scan, now, self.FIRST, at_run)

    def test_same(self):
        scan = {"wcs": "G54", "wco": [-5.905512, -3.937008, -1.181102], "time": 1}
        now = {"wcs": "G54", "wco": [-5.905512 + 0.0004, -3.937008 - 0.0004, -1.181102 + 0.0004], "port": "p"}
        self.assertEqual(self.check(scan, now), (None, []))
        self.assertEqual(self.check(scan, now, at_run=True), (None, []))

    def test_xy_zero_moved_is_only_a_warning(self):
        """The user shifts Y zero by the probe's offset (3 in) to scan and puts it back before leveling and running:
        never refused ("Dont block me from moving the table after a scan"), when leveling or at Run."""
        scan = {"wcs": "G54", "wco": [1.0, 2.0, 3.0], "time": 1}
        for wco, moved in (([1.0006, 2.0, 3.0], "X by +0.0006 in"),
                           ([1.0, 1.9, 3.0], "Y by -0.1000 in"),
                           ([1.0, -1.0, 3.0], "Y by -3.0000 in"),
                           ([1.5, -1.0, 3.0], "X by +0.5000 in, Y by -3.0000 in")):
            for at_run in (False, True):
                self.assertEqual(self.check(scan, {"wcs": "G54", "wco": wco}, at_run),
                                 (None, [f"X/Y zero moved since the scan ({moved}): fine if you shifted it for the "
                                         "probe and put it back; otherwise the leveling is off by that much"]))
        self.assertEqual(self.check(scan, {"wcs": "G54", "wco": [1.0, -1.0, 2.9]}, True),     # and Z: both said
                         (None, ["X/Y zero moved since the scan (Y by -3.0000 in): fine if you "
                                 "shifted it for the probe and put it back; otherwise the leveling is off by that much",
                                 "Z zero changed since the scan (by -0.1000 in): Z must be zeroed on the surface at "
                                 "the scan's first point (X0.5000 Y1.0000)"]))

    def test_another_work_coordinate_system_refuses(self):
        scan = {"wcs": "G54", "wco": [1.0, 2.0, 3.0], "time": 1}
        self.assertEqual(self.check(scan, {"wcs": "G55", "wco": [1.0, 2.0, 3.0]})[0],
                         "The scan was taken in G54 but FluidNC is in G55 now: switch to G54 (or scan again)")
        self.assertEqual(self.check({"wcs": "G54", "wco": None, "time": 1}, {"wcs": "G56", "wco": None})[0],
                         "The scan was taken in G54 but FluidNC is in G56 now: switch to G54 (or scan again)")

    def test_z_zero_moved_warns(self):
        scan = {"wcs": "G54", "wco": [1.0, 2.0, 3.0], "time": 1}
        self.assertEqual(self.check(scan, {"wcs": "G54", "wco": [1.0, 2.0, 2.9]}),
                         (None, ["Z zero changed since the scan (by -0.1000 in): Z must be zeroed on the surface at "
                                 "the scan's first point (X0.5000 Y1.0000)"]))

    def test_what_can_not_be_checked(self):
        why, notes = self.check(None, ZERO)
        self.assertIsNone(why)
        self.assertEqual(notes, ["This scan's setup can't be checked (it has no FluidSender setup line: a UGS scan?): "
                                 "X/Y zero must be the G-code's zero (any shift for the probe put back), and Z must be zeroed on the surface at "
                                 "the scan's first point (X0.5000 Y1.0000)"])
        self.assertEqual(self.check({"wcs": "G54", "wco": None, "time": 1}, ZERO),     # (a line without WCO)
                         (None, ["This scan's setup can't be checked (its setup line has no work offset): X/Y zero "
                                 "must be the G-code's zero (any shift for the probe put back), and Z must be zeroed on the surface at the scan's "
                                 "first point (X0.5000 Y1.0000)"]))
        scan = {"wcs": "G54", "wco": [0.0, 0.0, 0.0], "time": 1}
        self.assertEqual(self.check(scan, None), (None, ["Not connected to FluidNC: the scan's setup (work coordinate "
                                                         "system and offsets) is checked at Run"]))
        no_wco = {"wcs": "G54", "wco": None}
        self.assertEqual(self.check(scan, no_wco), (None, ["FluidNC hasn't reported its work offset yet: the scan's "
                                                           "setup is checked at Run"]))
        self.assertEqual(self.check(scan, no_wco, at_run=True),
                         (None, ["FluidNC hasn't reported its work offset yet: the X/Y and Z zero couldn't be compared "
                                 "with the scan's"]))


# ------------------------------------------------------------------ the Machine (made-up time, no port)

class Leveled(tj.Stage3):
    """The protocol harness (test_machine.Harness: WCO 0,0,0, G54), its Machine with a scanner that has the 3 x 2
    scan, and the program loaded."""

    def setUp(self):
        super().setUp()
        self.scanner = FakeScanner(scan_text())
        self.m.attach(scanner=self.scanner)
        self.ready()
        self.load(PROGRAM)

    def level(self, **data):
        return self.m.level({"scan": "last", **data})

    def refused_level(self, data, message, status=409):
        self.refused(lambda: self.m.level(data), message, status)

    def run_to_end(self):
        """Run, every line answered ok: the lines sent (FluidSender's G20 G90 first)."""
        self.m.run()
        sent = []
        for _ in range(10000):
            self.tick(0.01)
            new = self.sent()
            sent += new
            for _ in new:
                self.reply("ok")
            if not new and self.m.job.acked >= self.m.job.total:
                break
        self.reply(self.IDLE, dt=0.1)
        self.assertEqual(self.m.job.state, "done")
        return sent


class Levels(Leveled):
    def test_run_sends_exactly_the_leveled_program(self):
        want = leveled_text()
        out = self.level(zSurface=0, arcSegmentMm=0.1)
        self.assertEqual((out["lines"], out["total"]), (len(lines_of(want)), want.count("\n") + 1))
        self.assertEqual(out["warnings"], [])
        self.assertEqual(out["scan"], leveler.parse_scan(scan_text()).info())
        self.assertEqual((out["stats"]["lines"], out["stats"]["outside"]), (PROGRAM.count("\n"), 0))
        self.assertEqual(self.log("info")[-1], "Auto-level: t.nc LEVELED with last-scan.xyz (3 x 2 points, Z surface "
                                               f"0): {out['lines']} lines to send (from 7)")
        self.events.clear()
        self.assertEqual(self.run_to_end(), ["G20 G90"] + lines_of(want))
        info = self.log("info")
        self.assertIn(f"Job started: t.nc, LEVELED with last-scan.xyz (3 x 2 points, Z surface 0), {out['lines']} "
                      "lines", info)
        self.assertEqual(info[info.index("Job started: t.nc, LEVELED with last-scan.xyz (3 x 2 points, Z surface 0), "
                                         f"{out['lines']} lines") + 1],
                         "Run: inches (G20) and absolute (G90) first; the program's own G21/G91 still win")
        jobs = self.jobs()
        self.assertTrue(jobs and all(j["leveled"] is True for j in jobs))
        self.assertEqual(self.m.snapshot()["job"]["leveled"], True)

    def test_what_the_pages_get(self):
        """The program event and the snapshot say "leveled"; GET /api/machine/program's fields: the leveled text,
        "leveled" with "source" (the program line of each leveled line), and the original text."""
        self.events.clear()
        self.level()
        ev = self.evs("program")[-1]["program"]
        want = leveled_text()
        self.assertEqual({k: ev[k] for k in ("name", "lines", "total")},
                         {"name": "t.nc", "lines": len(lines_of(want)), "total": want.count("\n") + 1})
        info = ev["leveled"]
        self.assertEqual(set(info), {"scan", "time", "zSurface", "arcSegmentMm", "stats", "warnings", "wcs", "scanned"})
        self.assertEqual((info["scan"], info["zSurface"], info["arcSegmentMm"], info["wcs"], info["scanned"]),
                         ("last-scan.xyz", 0.0, 0.1, "G54", 1760000000))
        self.assertAlmostEqual(info["time"], time.time(), delta=5)
        self.assertEqual(self.m.snapshot()["program"], ev)
        fields = self.m.leveling.program_fields(self.m.program, True)
        self.assertEqual((fields["text"], fields["original"]), (want, PROGRAM))
        source = fields["leveled"].pop("source")
        self.assertEqual(fields["leveled"], info)
        mesh = leveler.parse_scan(scan_text())
        self.assertEqual(source, list(leveler.level_text(PROGRAM, mesh, {"arc_segment_mm": 0.1}).source))
        self.assertEqual(len(source), want.count("\n"))
        self.assertEqual(self.m.leveling.program_fields(self.m.program, False), {"leveled": info})
        self.assertEqual(self.m.snapshot()["level"], {"zSurface": 0.0, "arcSegmentMm": 0.1})

    def test_leveling_off(self):
        original = self.m.program
        self.level()
        self.events.clear()
        self.assertEqual(self.m.level({"off": True}), {"lines": 7, "total": 9, "warnings": []})
        self.assertIs(self.m.program, original)
        self.assertEqual(self.evs("program"), [{"type": "program", "program": original.info()}])   # (no "leveled")
        self.assertEqual(self.log("info"), ["Auto-level off: t.nc runs as it is (not leveled) again"])
        self.assertNotIn("leveled", self.m.snapshot()["program"])
        self.assertIsNone(self.m.leveling.program_fields(original, True))
        self.assertEqual(self.m.level({"off": True})["lines"], 7)      # (not leveled: nothing to do)
        self.assertEqual(self.run_to_end(), ["G20 G90"] + lines_of(PROGRAM))
        self.assertIn("Job started: t.nc, not leveled, 7 lines", self.log("info"))   # (a scan exists)
        self.assertEqual(self.jobs()[-1]["leveled"], False)

    def test_level_again_levels_the_original(self):
        self.level()
        out = self.level(zSurface=0.01, arcSegmentMm=0.5)
        want = leveled_text(z=0.01, arc=0.5)
        self.assertEqual(self.m.leveling.program_fields(self.m.program, True)["text"], want)
        self.assertEqual(out["lines"], len(lines_of(want)))
        self.assertEqual(self.m.leveling.program_fields(self.m.program, True)["original"], PROGRAM)
        self.m.level({"off": True})
        self.assertEqual(self.m.program.info()["lines"], 7)

    def test_a_scan_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "Surface.XYZ")
            with open(path, "w") as f:
                f.write(scan_text())
            self.m.level({"scan": {"path": path}, "zSurface": 0.005})
            info = self.m.snapshot()["program"]["leveled"]
            self.assertEqual((info["scan"], info["zSurface"]), (path, 0.005))
            self.assertEqual(self.m.leveling.program_fields(self.m.program, True)["text"], leveled_text(z=0.005))
            self.assertEqual(self.log("info")[-1].split(":")[1].strip(),
                             "t.nc LEVELED with Surface.XYZ (3 x 2 points, Z surface 0.005)")

    def test_settings_saved_and_used(self):
        self.assertNotIn("level", self.m.snapshot())         # (none saved yet)
        self.level(zSurface=-0.002, arcSegmentMm=0.05)
        self.assertEqual(self.m.settings.get("level"), {"zSurface": -0.002, "arcSegmentMm": 0.05})
        self.assertEqual(self.m.snapshot()["level"], {"zSurface": -0.002, "arcSegmentMm": 0.05})
        self.m.level({"off": True})
        self.level()                                        # (none given: the saved ones)
        info = self.m.snapshot()["program"]["leveled"]
        self.assertEqual((info["zSurface"], info["arcSegmentMm"]), (-0.002, 0.05))


class TurnedOff(Leveled):
    def test_loading_a_program(self):
        self.level()
        self.events.clear()
        self.load("G0 X1 Y1 Z1\n", "other.nc")
        self.assertEqual(self.log("info"), ["Auto-level off: the G-code changed"])
        self.assertEqual(self.evs("program"), [{"type": "program", "program": self.m.program.info()}])
        self.assertEqual(self.m.program.name, "other.nc")
        self.assertNotIn("leveled", self.m.snapshot()["program"])
        self.load("G0 X1 Y1 Z2\n", "third.nc")              # (not leveled: nothing to say)
        self.assertEqual(self.log("info"), ["Auto-level off: the G-code changed"])
        self.level()                                        # (the new program's text is the one leveled)
        self.assertEqual(self.m.leveling.program_fields(self.m.program, True)["original"], "G0 X1 Y1 Z2\n")
        self.m.load_program({"text": ""})                   # (unloaded: off too)
        self.assertEqual(self.log("info")[-1], "Auto-level off: the G-code changed")
        self.assertIsNone(self.m.program)

    def test_a_new_scan(self):
        original = self.m.program
        self.level()
        self.events.clear()
        self.m.scan_complete()
        self.assertIs(self.m.program, original)
        self.assertEqual(self.log("info"), ["Auto-level off: a new scan was made (Level again to use it)"])
        self.assertEqual(self.evs("program"), [{"type": "program", "program": original.info()}])
        self.m.scan_complete()                              # (not leveled: nothing)
        self.assertEqual(len(self.log("info")), 1)

    def test_another_fluidnc(self):
        original = self.m.program
        self.level()
        self.m.leveling.connected("/dev/fake")              # (the same one again: stays)
        self.assertIsNot(self.m.program, original)
        self.events.clear()
        self.m.leveling.connected("/dev/ttyUSB1")
        self.assertIs(self.m.program, original)
        self.assertEqual(self.log("info"), ["Auto-level off: connected to another FluidNC (/dev/ttyUSB1); level the "
                                            "G-code again for it"])

    def test_leveled_while_not_connected_is_the_next_fluidncs(self):
        self.m.link = None
        self.level()
        self.assertIn("Not connected to FluidNC: the scan's setup (work coordinate system and offsets) is checked at "
                      "Run", self.m.snapshot()["program"]["leveled"]["warnings"])
        self.m.leveling.connected("/dev/pts/7")
        self.m.leveling.connected("/dev/pts/7")
        self.assertIn("leveled", self.m.snapshot()["program"])
        self.m.leveling.connected("/dev/pts/8")
        self.assertNotIn("leveled", self.m.snapshot()["program"])


class Refusals(Leveled):
    def test_bad_calls(self):
        for data, why in (([], "Auto-level: give the scan and the settings"),
                          ({"scan": "first"}, 'scan: "last" or {"path": "/path/to/scan.xyz"}'),
                          ({"scan": {"path": ""}}, 'scan: "last" or {"path": "/path/to/scan.xyz"}'),
                          ({"scan": {"path": "/tmp/scan.txt"}}, "Not a scan file (.xyz): /tmp/scan.txt"),
                          ({"scan": "last", "z": 1}, "Unknown auto-level setting: z"),
                          ({"scan": "last", "arcSegmentMm": 5}, "Arc segment: a number from 0.01 to 2 mm")):
            self.refused_level(data, why, 400)
        missing = os.path.join(tempfile.gettempdir(), "no-such-dir-fluidsender", "a.xyz")
        self.refused_level({"scan": {"path": missing}}, f"{missing}: No such file or directory", 404)
        self.assertNotIn("leveled", self.m.snapshot()["program"])

    def test_nothing_to_level_with(self):
        self.scanner.text = None
        self.refused_level({"scan": "last"}, "No complete scan in this session yet: scan first, or choose a .xyz file")
        self.scanner.text = scan_text()
        self.m.load_program({"text": ""})
        self.refused_level({"scan": "last"}, "No program loaded: open a G-code file first")

    def test_leveler_refuses_with_the_line(self):
        self.load("G0 X0 Y0 Z0.1\nG92 X0\nG1 X1\n")
        self.refused_level({"scan": "last"}, "Line 2: G92 changes the work coordinates: the scan would no longer line "
                                             "up with the program")
        self.load("G0 X5 Y5 Z0.1\nG1 X6 Y6\n")
        with self.assertRaises(MachineError) as cm:
            self.level()
        self.assertTrue(str(cm.exception).startswith("The scan (X 0.0000 to 2.0000, Y 0.0000 to 1.0000) doesn't cover "
                                                     "this program"), str(cm.exception))
        self.scanner.text = "# xSamples=3 ySamples=2\n0 0 0\n1 0 0\n"
        self.load(PROGRAM)
        self.refused_level({"scan": "last"}, "The scan has 2 points but its header says xSamples=3 ySamples=2")
        with tempfile.TemporaryDirectory() as d:          # (a file whose points are all at one X)
            path = os.path.join(d, "line.xyz")
            with open(path, "w") as f:
                f.write("# xSamples=2 ySamples=2\n0 0 0\n0 0 0\n0 1 0\n0 1 0\n")
            self.refused_level({"scan": {"path": path}}, "The scan's 4 points all have the same X: a scan needs at "
                                                         "least 2 points along X and along Y")
        self.assertFalse(self.m.leveling.busy)

    def test_too_long_once_leveled(self):
        """FluidNC takes 127 characters of G-code: a line that the Z word makes longer is refused, by its line."""
        long_line = "G1X0.1" + "0" * 115 + "Y0"            # 123 characters: fine as it is
        self.load(f"G0 X0 Y0 Z0.1\n{long_line}\n")
        with self.assertRaises(MachineError) as cm:
            self.level()
        self.assertEqual(cm.exception.status, 409)
        self.assertTrue(str(cm.exception).startswith("Line 2 (leveled line 2) is too long: 129 characters without "
                                                     "comments and spaces"), str(cm.exception))

    def test_not_during_a_job_or_a_scan(self):
        self.level()
        self.m.run()
        self.refused_level({"scan": "last"}, "A job is running")
        self.refused_level({"off": True}, "A job is running")
        self.m.stop()
        self.m.job.state = "stopped"                        # (as the safe stop ends it)
        self.m.activity = "scan"
        self.refused_level({"scan": "last"}, "A scan is running")
        self.refused_level({"off": True}, "A scan is running")
        self.m.activity = None
        self.m.level({"off": True})


class Setup(Leveled):
    def test_checked_when_leveling(self):
        self.reply("<Idle|MPos:0,0,0|FS:0,0|WCO:0.0254,0,0>")       # X zero 0.001 in from the scan's: a warning
        out = self.level()
        moved = ("X/Y zero moved since the scan (X by +0.0010 in): fine if you shifted it for the "
                 "probe and put it back; otherwise the leveling is off by that much")
        self.assertEqual(out["warnings"], [moved])
        self.assertIn(f"Auto-level: {moved}", self.log("info"))
        self.assertIn("leveled", self.m.snapshot()["program"])
        self.m.level({"off": True})
        self.reply("<Idle|MPos:0,0,0|FS:0,0|WCO:0,0,-2.54>")        # Z zero 0.1 in lower: a warning
        out = self.level()
        why = ("Z zero changed since the scan (by -0.1000 in): Z must be zeroed on the surface at the scan's first "
               "point (X0.0000 Y0.0000)")
        self.assertEqual(out["warnings"], [why])
        self.assertIn(f"Auto-level: {why}", self.log("info"))
        self.m.level({"off": True})
        self.link.wcs = 2                                           # G55 (a line chose it)
        self.refused_level({"scan": "last"}, "The scan was taken in G54 but FluidNC is in G55 now: switch to G54 (or "
                                             "scan again)")

    def test_a_scan_without_its_setup(self):
        """A UGS scan: leveled for the work coordinate system FluidNC is in, with a warning."""
        self.scanner.text = scan_text(None)
        self.link.wcs = 3
        self.load(PROGRAM.replace("G54", ""))
        out = self.level()
        self.assertEqual(out["warnings"], ["This scan's setup can't be checked (it has no FluidSender setup line: a "
                                           "UGS scan?): X/Y zero must be the G-code's zero (any shift for the probe put back), and Z must be "
                                           "zeroed on the surface at the scan's first point (X0.0000 Y0.0000)"])
        self.assertEqual(self.m.snapshot()["program"]["leveled"]["wcs"], "G56")
        self.link.wcs = 1
        self.refused(self.m.run, "The G-code was leveled for G56 but FluidNC is in G54 now: switch to G56, or level "
                                 "it again")
        self.link.wcs = 3
        self.run_to_end()
        self.assertIn("Auto-level: This scan's setup can't be checked (it has no FluidSender setup line: a UGS scan?): "
                      "X/Y zero must be the G-code's zero (any shift for the probe put back), and Z must be zeroed on the surface at the scan's "
                      "first point (X0.0000 Y0.0000)", self.log("info"))

    def job_notes(self, nth=1):
        """The Auto-level lines right after the nth "Job started: t.nc, LEVELED" (from the end) in the console."""
        info = self.log("info")
        k = [i for i, t in enumerate(info) if t.startswith("Job started: t.nc, LEVELED")][-nth]
        notes = []
        for t in info[k + 1:]:
            if not t.startswith("Auto-level: "):
                break
            notes.append(t)
        return notes

    def test_checked_at_run(self):
        self.level()
        self.reply("<Idle|MPos:0,0,0|FS:0,0|WCO:0,-0.0254,0>", dt=0.1)  # Y zero moved after leveling: Run says so
        self.assertEqual(self.run_to_end(), ["G20 G90"] + lines_of(leveled_text()))
        self.assertEqual(self.job_notes(), ["Auto-level: X/Y zero moved since the scan (Y by -0.0010 in): "
                                            "fine if you shifted it for the probe and put it back; otherwise the "
                                            "leveling is off by that much"])
        self.reply("<Idle|MPos:0,0,0|FS:0,0|WCO:0,0,1.27>", dt=0.1)     # back; Z 0.05 in higher: Run says so
        self.run_to_end()
        self.assertEqual(self.job_notes(), ["Auto-level: Z zero changed since the scan (by +0.0500 in): Z must be "
                                            "zeroed on the surface at the scan's first point (X0.0000 Y0.0000)"])
        self.link.wcs = 2
        self.refused(self.m.run, "The G-code was leveled for G54 but FluidNC is in G55 now: switch to G54, or level it "
                                 "again")
        self.link.wcs = 1
        self.link.parser.wco = None                                     # (after a reset: not reported yet)
        self.run_to_end()
        self.assertEqual(self.job_notes(), ["Auto-level: FluidNC hasn't reported its work offset yet: the X/Y and Z "
                                            "zero couldn't be compared with the scan's"])

    def test_the_probe_shift_and_back(self):
        """The user's way (2026-10-17): the probe sits 3 in from the spindle, so Y zero is shifted by 3 in to scan and
        put back afterwards. Level after putting it back, and Level before it then Run after it: both go, and say
        Y by -3.0000 in."""
        shifted = {"wcs": "G54", "wco": [0.0, 3.0, 0.0], "port": "/dev/fake"}      # Y zero 3 in up for the scan
        self.scanner.text = scan_text(shifted)
        moved = ("Auto-level: X/Y zero moved since the scan (Y by -3.0000 in): fine if you shifted "
                 "it for the probe and put it back; otherwise the leveling is off by that much")
        out = self.level()                                  # (the harness's WCO is 0,0,0: Y is back)
        self.assertEqual(out["warnings"], [moved[len("Auto-level: "):]])
        self.assertEqual(self.run_to_end(), ["G20 G90"] + lines_of(leveled_text(scan=scan_text(shifted))))
        self.assertEqual(self.job_notes(), [moved])
        self.m.level({"off": True})
        self.reply("<Idle|MPos:0,0,0|FS:0,0|WCO:0,76.2,0>", dt=0.1)   # still shifted: Level says nothing
        self.assertEqual(self.level()["warnings"], [])
        self.reply("<Idle|MPos:0,0,0|FS:0,0|WCO:0,0,0>", dt=0.1)      # put back, then Run
        self.run_to_end()
        self.assertEqual(self.job_notes(), [moved])


class OutsideTheLock(Leveled):
    def test_the_machine_answers_while_it_levels_and_a_newer_program_wins(self):
        real, going, release = leveler.level_text, threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            going.set()
            self.assertTrue(release.wait(10))
            return real(*args, **kwargs)
        got = []

        def level():
            try:
                got.append(self.level())
            except MachineError as e:
                got.append(e)
        with mock.patch.object(leveler, "level_text", side_effect=slow):
            t = threading.Thread(target=level)
            t.start()
            self.assertTrue(going.wait(10))
            self.assertTrue(self.m.lock.acquire(timeout=2))        # (not held while it levels)
            self.m.lock.release()
            self.refused(self.m.run, "Auto-level is still leveling the G-code; try again in a second")
            self.refused_level({"scan": "last"}, "Already leveling the G-code; try again in a second")
            self.refused_level({"off": True}, "Already leveling the G-code; try again in a second")
            self.load("G0 X1 Y1 Z1\n", "newer.nc")                  # (an upload meanwhile wins)
            release.set()
            t.join(10)
        self.assertEqual((got[0].status, str(got[0])), (409, "The G-code changed while it was being leveled: level it "
                                                              "again"))
        self.assertEqual(self.m.program.name, "newer.nc")
        self.assertNotIn("leveled", self.m.snapshot()["program"])
        self.assertFalse(self.m.leveling.busy)
        self.level()                                                # (and it can level again)
        self.assertEqual(self.m.snapshot()["program"]["leveled"]["scan"], "last-scan.xyz")

    def test_another_fluidnc_connected_meanwhile(self):
        """Connected to another FluidNC while it levels (connected() saw no leveled program then): dropped."""
        real, going, release, got = leveler.level_text, threading.Event(), threading.Event(), []

        def slow(*args, **kwargs):
            going.set()
            self.assertTrue(release.wait(10))
            return real(*args, **kwargs)

        def level():
            try:
                got.append(self.level())
            except MachineError as e:
                got.append(e)
        with mock.patch.object(leveler, "level_text", side_effect=slow):
            t = threading.Thread(target=level)
            t.start()
            self.assertTrue(going.wait(10))
            self.link.path = "/dev/ttyUSB1"                         # (as a Disconnect and a Connect to it)
            release.set()
            t.join(10)
        self.assertEqual((got[0].status, str(got[0])), (409, "FluidSender connected to another FluidNC while the "
                                                              "G-code was being leveled: level it again"))
        self.assertNotIn("leveled", self.m.snapshot()["program"])
        self.assertFalse(self.m.leveling.busy)
        self.level()                                                # (levels for this one now)
        self.assertEqual(self.m.leveling.result(self.m.program).port, "/dev/ttyUSB1")

    def test_a_bug_answers_500_and_frees_it(self):
        with mock.patch.object(leveler, "level_text", side_effect=RuntimeError("bug")), \
                mock.patch.object(leveling.traceback, "print_exc") as printed:
            self.refused_level({"scan": "last"}, "Internal error in FluidSender while leveling (RuntimeError('bug'))",
                               500)
        printed.assert_called_once()
        self.assertFalse(self.m.leveling.busy)
        self.level()


class JobLines(Leveled):
    def test_no_scan_no_word_about_it(self):
        """Without a scan in this session, Run's line and the job's info are as they always were."""
        self.scanner.text = None
        self.run_to_end()
        self.assertIn("Job started: t.nc, 7 lines", self.log("info"))
        self.assertNotIn("leveled", self.jobs()[-1])
        self.assertNotIn("leveled", self.m.snapshot()["job"])

    def test_thousands(self):
        self.load("G0 X0 Y0 Z0.1\n" + "G1 X0.1\nG1 X0\n" * 600)
        self.level()
        n = len(self.m.program)
        self.assertGreater(n, 1000)
        self.m.run()
        self.assertIn(f"Job started: t.nc, LEVELED with last-scan.xyz (3 x 2 points, Z surface 0), {n:,} lines",
                      self.log("info"))


# ------------------------------------------------------------------ a Machine with the FluidNC simulator

class WithTheSimulator(unittest.TestCase):
    """The FluidNC simulator (4.1.1, inches, its work offset -150, -100, -30 mm) on a pty, a real Machine connected
    to it: a leveled job runs to the end and FluidNC gets exactly the leveled lines; a new X zero refuses Run."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.simlog = os.path.join(self.dir.name, "sim.log")
        self.sim = subprocess.Popen([sys.executable, SIM, "--inches", "--log", self.simlog], stdout=subprocess.PIPE,
                                    text=True)
        self.addCleanup(self.stop_sim)
        self.path = self.sim.stdout.readline().strip()
        self.assertTrue(self.path.startswith("/dev/pts/"), self.path)
        self.m = machine.Machine(extra_ports=[self.path], only_extra=True)
        self.addCleanup(self.m.shutdown)

    def stop_sim(self):
        self.sim.terminate()
        try:
            self.sim.wait(3)
        except subprocess.TimeoutExpired:
            self.sim.kill()
            self.sim.wait()
        self.sim.stdout.close()

    def wait(self, cond, what, timeout=10.0):
        end = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), end, f"no {what}")
            time.sleep(0.02)

    def received(self):
        with open(self.simlog) as f:
            return [line.rstrip("\n").split(" ", 1)[1] for line in f]

    def test_a_leveled_job_on_the_simulator(self):
        m = self.m
        m.connect(self.path)
        # (connected: G20 answered, the work offset reported and FluidNC Idle)
        self.wait(lambda: not m.link.inch and m.link.modal and m.link.modal["units"] == "G20", "G20")
        self.wait(lambda: (m.work_setup() or {}).get("wco") is not None, "work offset")
        setup = m.work_setup()
        self.assertEqual(setup, {"wcs": "G54", "wco": [-5.9055, -3.937, -1.1811], "port": self.path})   # (4 decimals)
        m.attach(scanner=FakeScanner(xyz_text(XS, YS, HEIGHTS, setup_line(setup, time.time()))))
        program = "G20 G90 G54\nG0 X0 Y0 Z0.1\nG1 Z-0.01 F100\nG1 X0.3 Y0.2\nG1 X0 Y0\nG0 Z0.1\n"
        m.load_program("sim.nc", program)
        m.level({"scan": "last"})
        want = leveled_text(program, m.scanner.text)
        self.wait(lambda: m.link.fresh_state(m.clock()) == "Idle", "Idle")
        before = len(self.received())
        m.run()
        self.wait(lambda: m.job.state != "running", "the job's end", timeout=30)
        self.assertEqual(m.job.state, "done", m.job.error)
        lines = [e[5:] for e in self.received()[before:] if e.startswith("LINE ")]
        self.assertEqual(lines, ["G20 G90"] + lines_of(want))
        self.assertTrue(any(e["text"].startswith("Job started: sim.nc, LEVELED with last-scan.xyz (3 x 2 points, Z "
                                                 "surface 0), ") for e in m.console))
        # a new X zero: Run goes, and says by how much it moved (the user moves the table after a scan)
        m.send("G10 L20 P0 X0.5")
        self.wait(lambda: abs(m.work_setup()["wco"][0] - (-5.9055 - 0.5)) < 1e-4, "the new X zero")
        m.run()
        self.wait(lambda: m.job.state != "running", "the second job's end", timeout=30)
        self.assertEqual(m.job.state, "done", m.job.error)
        texts = [e["text"] for e in list(m.console)]        # (a bounded deque: searched whole; only this run moved X)
        self.assertTrue(any(t.startswith("Auto-level: X/Y zero moved since the scan (X by -0.5000 in): "
                                         "fine if you shifted it for the probe") for t in texts), texts[-20:])


if __name__ == "__main__":
    unittest.main()
