"""
Unit tests for scan.py: the scan settings, the grid and its order, the .xyz text (byte for byte as the
user's UGS wrote it), and whole scans with a pretend machine (scan_move and friends, no port) and the
fake probe (tools/fake_probe.py) on a pseudo terminal made here. Real serial ports are never opened.

    python3 -m unittest tests.test_scan -v
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import probe  # noqa: E402
import scan  # noqa: E402
from machine import MachineError  # noqa: E402
from probe import Probe  # noqa: E402
from scan import Scanner, axis_values, check_settings, java_double, load_settings, probe_order, xyz_text  # noqa: E402

FAKE = os.path.join(ROOT, "tools", "fake_probe.py")

# The user's scan, saved by their UGS fork (scan.XYZ next to the G-code on the share), byte for byte
REAL_SCAN = (
    '# minX=0.50000 maxX=8.50000\n'
    '# minY=1.00000 maxY=12.00000\n'
    '# xSamples=15 ySamples=5\n'
    '# units=INCH\n'
    '0.5 1.0 0.0\n'
    '0.5 3.75 -0.009\n'
    '0.5 6.5 -0.008\n'
    '0.5 9.25 -0.006\n'
    '0.5 12.0 -0.005\n'
    '1.0714285714285714 1.0 0.01\n'
    '1.0714285714285714 3.75 0.004\n'
    '1.0714285714285714 6.5 0.002\n'
    '1.0714285714285714 9.25 0.001\n'
    '1.0714285714285714 12.0 0.002\n'
    '1.6428571428571428 1.0 0.022\n'
    '1.6428571428571428 3.75 0.018\n'
    '1.6428571428571428 6.5 0.01\n'
    '1.6428571428571428 9.25 0.006\n'
    '1.6428571428571428 12.0 0.006\n'
    '2.2142857142857144 1.0 0.012\n'
    '2.2142857142857144 3.75 0.004\n'
    '2.2142857142857144 6.5 0.002\n'
    '2.2142857142857144 9.25 0.004\n'
    '2.2142857142857144 12.0 0.006\n'
    '2.7857142857142856 1.0 0.004\n'
    '2.7857142857142856 3.75 -0.005\n'
    '2.7857142857142856 6.5 -0.006\n'
    '2.7857142857142856 9.25 -0.01\n'
    '2.7857142857142856 12.0 -0.001\n'
    '3.357142857142857 1.0 -0.005\n'
    '3.357142857142857 3.75 -0.014\n'
    '3.357142857142857 6.5 -0.015\n'
    '3.357142857142857 9.25 -0.014\n'
    '3.357142857142857 12.0 -0.014\n'
    '3.9285714285714284 1.0 -0.014\n'
    '3.9285714285714284 3.75 -0.022\n'
    '3.9285714285714284 6.5 -0.024\n'
    '3.9285714285714284 9.25 -0.02\n'
    '3.9285714285714284 12.0 -0.017\n'
    '4.5 1.0 -0.019\n'
    '4.5 3.75 -0.031\n'
    '4.5 6.5 -0.031\n'
    '4.5 9.25 -0.026\n'
    '4.5 12.0 -0.02\n'
    '5.071428571428571 1.0 -0.026\n'
    '5.071428571428571 3.75 -0.036\n'
    '5.071428571428571 6.5 -0.037\n'
    '5.071428571428571 9.25 -0.032\n'
    '5.071428571428571 12.0 -0.024\n'
    '5.642857142857143 1.0 -0.035\n'
    '5.642857142857143 3.75 -0.042\n'
    '5.642857142857143 6.5 -0.042\n'
    '5.642857142857143 9.25 -0.037\n'
    '5.642857142857143 12.0 -0.031\n'
    '6.214285714285714 1.0 -0.041\n'
    '6.214285714285714 3.75 -0.047\n'
    '6.214285714285714 6.5 -0.047\n'
    '6.214285714285714 9.25 -0.042\n'
    '6.214285714285714 12.0 -0.035\n'
    '6.785714285714286 1.0 -0.041\n'
    '6.785714285714286 3.75 -0.047\n'
    '6.785714285714286 6.5 -0.047\n'
    '6.785714285714286 9.25 -0.043\n'
    '6.785714285714286 12.0 -0.037\n'
    '7.357142857142857 1.0 -0.04\n'
    '7.357142857142857 3.75 -0.046\n'
    '7.357142857142857 6.5 -0.045\n'
    '7.357142857142857 9.25 -0.041\n'
    '7.357142857142857 12.0 -0.036\n'
    '7.928571428571429 1.0 -0.036\n'
    '7.928571428571429 3.75 -0.041\n'
    '7.928571428571429 6.5 -0.041\n'
    '7.928571428571429 9.25 -0.037\n'
    '7.928571428571429 12.0 -0.03\n'
    '8.5 1.0 -0.027\n'
    '8.5 3.75 -0.035\n'
    '8.5 6.5 -0.036\n'
    '8.5 9.25 -0.032\n'
    '8.5 12.0 -0.026\n'
)


def real_grid():
    """The real scan's Z values as {(x index, y index): z}."""
    lines = [line.split() for line in REAL_SCAN.splitlines() if not line.startswith("#")]
    return {(n // 5, n % 5): float(z) for n, (_, _, z) in enumerate(lines)}


class FakeMachine:
    """machine.py's side of a scan: begin/end_activity, scan_move, stop, status_now, snapshot. No port."""

    def __init__(self, move_time=0.005):
        self.calls = []
        self.activity = None
        self.wpos = [0.0, 0.0, 0.5]         # the work position status_now() and snapshot() say (Z clear of the work)
        self.move_time = move_time
        self.stopped = threading.Event()
        self.moving = threading.Event()
        self.refuse = None                  # begin_activity raises MachineError(this)
        self.fail_move = {}                 # move number -> scan_move raises MachineError(this)
        self.hold_move = None               # this move runs until stop()
        self.on_stop = None
        self.moves = 0

    def begin_activity(self, name):
        self.calls.append(("begin", name))
        if self.refuse:
            raise MachineError(self.refuse, 409)
        self.activity = name

    def end_activity(self, name):
        self.calls.append(("end", name))
        if self.activity == name:
            self.activity = None
            self.activity_stop = False

    def scan_move(self, x, y, timeout):
        self.moves += 1
        n = self.moves
        self.calls.append(("move", x, y, timeout))
        if self.activity != "scan":
            raise MachineError("not scanning")
        if self.stopped.is_set():
            raise MachineError("Stopped")
        if n in self.fail_move:
            raise MachineError(self.fail_move[n])
        self.moving.set()
        try:
            if self.stopped.wait(30 if n == self.hold_move else self.move_time):
                raise MachineError("Stopped")
        finally:
            self.moving.clear()

    def stop(self):
        self.calls.append(("stop",))
        self.stopped.set()
        if self.on_stop:
            self.on_stop()

    def status_now(self):
        return {"state": "Idle", "wpos": self.wpos and list(self.wpos)}

    def snapshot(self):
        return {"status": {"wpos": list(self.wpos)}, "axes": {"x": {"maxRate": 393.7}, "y": {"maxRate": 300.0},
                                                             "z": {"maxRate": 196.9}}}

    def move_list(self):
        return [(c[1], c[2]) for c in self.calls if c[0] == "move"]


class Store:
    """settings.py's store, as far as the scanner uses it."""

    def __init__(self, scan_settings=None):
        self.data = {} if scan_settings is None else {"scan": scan_settings}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value


UGS = dict(scan.DEFAULTS, minX=0.5, minY=1.0, maxX=8.5, maxY=12.0)    # the user's UGS area (the defaults were once)
SMALL = {"minX": 1.0, "maxX": 2.0, "minY": 0.5, "maxY": 1.0, "xPoints": 3, "yPoints": 2, "zRetract": 0.25,
         "probeOffX": 0.0, "probeOffY": 0.0}


class ScanCase(unittest.TestCase):
    """A fake probe on a pty (connected and homed), a FakeMachine and a Scanner writing into a temp folder."""
    fake_args = ()
    probe_time = "0.005"
    home_time = "0.005"

    def setUp(self):
        self.enterContext(mock.patch.multiple(probe, SETTLE=0.05, QUIET=0.002))
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.fake_log = os.path.join(self.dir, "fake.log")
        self.proc = subprocess.Popen([sys.executable, FAKE, "--home-time", self.home_time, "--probe-time",
                                      self.probe_time, "--log", self.fake_log, *self.fake_args],
                                     stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup)
        self.path = self.proc.stdout.readline().strip()
        self.assertTrue(self.path.startswith("/dev/pts/"), self.path)
        self.logs = []
        self.events = []
        self.probe = Probe(self.log, test_ports=[self.path], only_test_ports=True)
        self.probe.connect(self.path)
        self.probe.home()
        self.machine = FakeMachine()
        self.store = Store()
        self.scans = os.path.join(self.dir, "scans")
        self.scanner = Scanner(self.machine, self.probe, self.store, self.events.append, self.log, scans_dir=self.scans)

    def cleanup(self):
        self.scanner.shutdown(5)
        self.assertFalse(self.scanner.thread and self.scanner.thread.is_alive(), "the scan thread is still running")
        self.probe.disconnect(force=True)
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()

    def log(self, kind, text):
        self.logs.append((kind, text))

    def run_scan(self, settings=SMALL, timeout=20):
        self.scanner.start(settings)
        self.scanner.thread.join(timeout)
        self.assertFalse(self.scanner.thread.is_alive())
        return self.scanner.state()

    def received(self):
        time.sleep(0.05)
        with open(self.fake_log) as f:
            return [json.loads(line) for line in f]

    def commands(self):
        return [e["cmd"] for e in self.received() if "cmd" in e]

    def wait_for(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                self.fail("timed out")
            time.sleep(0.005)

    def assert_ended(self, phase):
        """Nothing left running: the thread is done, the machine and the probe are free."""
        st = self.scanner.state()
        self.assertEqual((st["running"], st["phase"]), (False, phase))
        self.assertEqual(self.machine.calls[0], ("begin", "scan"))
        self.assertEqual([c for c in self.machine.calls if c[0] == "end"], [("end", "scan")])
        self.assertEqual(self.machine.calls[-1][0] in ("end", "stop"), True)
        self.assertIsNone(self.probe.owner)
        self.assertFalse(self.probe.state()["busy"])
        last = self.events[-1]
        self.assertEqual(last["type"], "scan")
        self.assertFalse(last["scan"]["running"])
        return st


class Settings(unittest.TestCase):
    def test_first_run_defaults_have_no_area_and_no_offset(self):
        """A first run makes nothing up: Min / Max all 0 (kept, but Start Scan refuses it) and offset 0, 0."""
        self.assertEqual(check_settings(), {"minX": 0.0, "minY": 0.0, "maxX": 0.0, "maxY": 0.0, "xPoints": 15,
                                            "yPoints": 5, "zRetract": 0.2, "probeOffX": 0.0, "probeOffY": 0.0})
        self.assertTrue(scan.area_unset(check_settings()))
        self.assertFalse(scan.area_unset(check_settings({"maxX": 1, "maxY": 1})))
        self.assertEqual(check_settings({"probeOffY": "3"})["probeOffY"], 3.0)
        with self.assertRaises(MachineError) as cm:
            check_settings({"probeOffY": 13})
        self.assertEqual(str(cm.exception), "Probe offset Y must be from -12 to 12 in")

    def test_some_over_the_saved_ones(self):
        saved = dict(SMALL)
        s = check_settings({"xPoints": "4", "maxX": " 2.5"}, saved)
        self.assertEqual(s, dict(SMALL, xPoints=4, maxX=2.5))
        self.assertIsInstance(s["xPoints"], int)
        self.assertEqual(check_settings({"xPoints": 7.0}, UGS)["xPoints"], 7)
        self.assertEqual(check_settings({"minX": 3, "maxX": 3, "xPoints": 1}, UGS)["minX"], 3.0)   # one point: may be equal
        self.assertEqual(check_settings({"xPoints": 1}, UGS)["maxX"], 8.5)     # (one point at min X)
        self.assertEqual(check_settings({"xPoints": 100, "yPoints": 20}, UGS)["xPoints"], 100)

    def refused(self, data):
        with self.assertRaises(MachineError) as cm:
            check_settings(data, UGS if isinstance(data, dict) else None)
        self.assertEqual(cm.exception.status, 400)
        return str(cm.exception)

    def test_refused(self):
        self.assertEqual(self.refused({"minX": "abc"}), "Min X must be a number")
        self.assertEqual(self.refused({"maxY": True}), "Max Y must be a number")
        self.assertEqual(self.refused({"minY": float("nan")}), "Min Y must be a number")
        self.assertEqual(self.refused({"maxX": float("inf")}), "Max X must be a number")
        self.assertEqual(self.refused({"maxX": None}), "Max X must be a number")
        self.assertEqual(self.refused({"minX": 9}), "Min X (9) is more than Max X (8.5)")
        self.assertEqual(self.refused({"minY": 12}), "Min Y and Max Y are the same: that takes 1 point in Y")
        self.assertEqual(self.refused({"xPoints": 0}), "Points X must be a whole number from 1 to 100")
        self.assertEqual(self.refused({"yPoints": 101}), "Points Y must be a whole number from 1 to 100")
        self.assertEqual(self.refused({"yPoints": 2.5}), "Points Y must be a whole number from 1 to 100")
        self.assertEqual(self.refused({"xPoints": 100, "yPoints": 21}),
                         "Too many points: 100 x 21 = 2100 (2000 at most)")
        self.assertEqual(self.refused({"zRetract": 0.005}), "Z retract must be from 0.01 to 2 in")
        self.assertEqual(self.refused({"zRetract": 2.5}), "Z retract must be from 0.01 to 2 in")
        self.assertEqual(self.refused({"maxX": 150}), "Max X must be from -100 to 100 in")
        self.assertEqual(self.refused({"minx": 1}), "Unknown scan setting: minx")
        self.assertEqual(self.refused([1, 2]), "Scan settings must be an object")

    def test_saved_settings_fall_back_to_the_defaults(self):
        self.assertEqual(load_settings(None), scan.DEFAULTS)
        self.assertEqual(load_settings({"minX": 99, "maxX": 1}), scan.DEFAULTS)     # (more than max X)
        self.assertEqual(load_settings(dict(SMALL, extra=1)), SMALL)               # (unknown keys ignored)
        self.assertEqual(load_settings({"xPoints": 3}), dict(scan.DEFAULTS, xPoints=3))


class Grid(unittest.TestCase):
    def test_zigzag_column_by_column(self):
        self.assertEqual(probe_order(3, 3), [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0), (2, 0), (2, 1), (2, 2)])
        self.assertEqual(probe_order(2, 2), [(0, 0), (0, 1), (1, 1), (1, 0)])
        self.assertEqual(probe_order(1, 3), [(0, 0), (0, 1), (0, 2)])
        self.assertEqual(probe_order(3, 1), [(0, 0), (1, 0), (2, 0)])
        self.assertEqual(len(set(probe_order(15, 5))), 75)

    def test_positions_are_the_very_numbers_ugs_wrote(self):
        xs = [float(line.split()[0]) for line in REAL_SCAN.splitlines()[4::5]]
        self.assertEqual(axis_values(0.5, 8.5, 15), xs)
        self.assertEqual(axis_values(1.0, 12.0, 5), [1.0, 3.75, 6.5, 9.25, 12.0])
        self.assertEqual(axis_values(2.0, 2.0, 1), [2.0])
        self.assertEqual(axis_values(2.0, 5.0, 1), [2.0])

    def test_java_double(self):
        # (each checked against Java 17's Double.toString)
        cases = [(0.0, "0.0"), (-0.0, "-0.0"), (12.0, "12.0"), (-0.009, "-0.009"), (0.01, "0.01"),
                 (1.0714285714285714, "1.0714285714285714"), (100.0, "100.0"), (0.001, "0.001"),
                 (0.0005, "5.0E-4"), (-0.0001, "-1.0E-4"), (0.00099, "9.9E-4"), (1e7, "1.0E7"),
                 (9999999.999, "9999999.999"), (0.1 + 0.2, "0.30000000000000004"), (1e16, "1.0E16")]
        for v, want in cases:
            self.assertEqual(java_double(v), want, v)

    def test_file_text_byte_for_byte(self):
        xs, ys = axis_values(0.5, 8.5, 15), axis_values(1.0, 12.0, 5)
        self.assertEqual(xyz_text(xs, ys, real_grid()), REAL_SCAN)

    def test_header_rounds_like_java(self):
        text = xyz_text([0.123455, 1.5], [-0.0000049, 2.0], {(i, j): 0.0 for i in range(2) for j in range(2)})
        self.assertEqual(text.splitlines()[:4], ["# minX=0.12346 maxX=1.50000", "# minY=-0.00000 maxY=2.00000",
                                                 "# xSamples=2 ySamples=2", "# units=INCH"])


class Scans(ScanCase):
    fake_args = ("--values", "0.30,0.31,0.29,0.28,0.32,0.305")

    def test_a_whole_scan(self):
        st = self.run_scan()
        self.assert_ended("done")
        xs, ys = [1.0, 1.5, 2.0], [0.5, 1.0]
        # to the first point, then each point (zigzag), then back to the first
        self.assertEqual(self.machine.move_list(), [(1.0, 0.5), (1.0, 0.5), (1.0, 1.0), (1.5, 1.0), (1.5, 0.5),
                                                    (2.0, 0.5), (2.0, 1.0), (1.0, 0.5)])
        self.assertNotIn(("stop",), self.machine.calls)
        # the probe: its own home first (setUp), then ZR and P at every point, then H at the end
        self.assertEqual(self.commands(), ["H"] + ["ZR:0.250", "P"] * 6 + ["H"])
        # Z by probing order: 0.30 (the reference), 0.31, 0.29, 0.28, 0.32, 0.305 -> relative to the first
        z = {(0, 0): 0.0, (0, 1): 0.01, (1, 1): -0.01, (1, 0): -0.02, (2, 0): 0.02, (2, 1): 0.005}
        self.assertEqual(st["result"]["points"], [[x, y, z[i, j]] for i, x in enumerate(xs) for j, y in enumerate(ys)])
        self.assertEqual((st["result"]["xs"], st["result"]["ys"], st["result"]["complete"]), (xs, ys, True))
        want = ("# minX=1.00000 maxX=2.00000\n# minY=0.50000 maxY=1.00000\n# xSamples=3 ySamples=2\n# units=INCH\n"
                "1.0 0.5 0.0\n1.0 1.0 0.01\n1.5 0.5 -0.02\n1.5 1.0 -0.01\n2.0 0.5 0.02\n2.0 1.0 0.005\n")
        self.assertEqual(st["result"]["text"], want)
        saved = os.path.join(self.scans, "last-scan.xyz")
        with open(saved, newline="") as f:
            self.assertEqual(f.read(), want)
        self.assertEqual(os.listdir(self.scans), ["last-scan.xyz"])     # (no temporary file left)
        self.assertEqual((st["savedTo"], st["error"], st["point"], st["points"]), (saved, None, 6, 6))
        self.assertTrue(st["message"].startswith("Scan complete: 6 points in 0:0"))
        self.assertTrue(self.probe.state()["ready"])        # homed at the end: ready for the next scan
        self.assertFalse(self.probe.state()["mayBeDown"])   # (its tip is up: no "Probe may be down")
        self.assertEqual(self.store.data["scan"], SMALL)    # (the settings it ran with are kept)
        self.assertIn(("info", "Scan point 2 of 6: X1.0000 Y1.0000 Z0.0100"), self.logs)
        self.assertIn(("info", f"Scan saved to {saved}"), self.logs)

    def test_progress_events(self):
        self.run_scan()
        states = [e["scan"] for e in self.events if e["type"] == "scan"]
        self.assertTrue(states[0]["running"])
        done = [s["point"] for s in states]
        self.assertEqual(sorted(set(done)), list(range(7)))
        self.assertEqual(done, sorted(done))                # never backwards
        firsts = {}
        for s in states:
            firsts.setdefault(s["point"], s)
        self.assertEqual([len(firsts[n]["result"]["points"]) for n in range(7)], list(range(7)))
        self.assertEqual(firsts[1]["lastZ"], 0.0)
        self.assertEqual(firsts[2]["lastZ"], 0.01)
        moving = [s["at"] for s in states if s["phase"] == "moving"]
        self.assertIn({"n": 3, "x": 1.5, "y": 1.0}, moving)      # (zigzag: up the first column, down the next)
        self.assertIn({"n": 4, "x": 1.5, "y": 0.5}, moving)
        phases = [s["phase"] for s in states]
        self.assertLess(phases.index("probing"), phases.index("homing"))
        self.assertLess(phases.index("homing"), phases.index("returning"))
        self.assertEqual(phases[-1], "done")
        self.assertEqual(set(states[-1]), {"running", "phase", "point", "points", "at", "lastZ", "settings", "result",
                                           "error", "message", "savedTo", "started", "finished"})

    def test_probe_offset(self):
        """Offset from spindle Y 3 (the user's: the probe sits 3 in toward -Y and touches at the bit's Y minus 3): the
        bit goes to each point plus 3 in Y, and the grid and the file stay where the probe touched (the job's
        coordinates). The probe data is shifted, so X/Y zero and the G-code never move."""
        st = self.run_scan(dict(SMALL, probeOffY=3.0))
        self.assert_ended("done")
        self.assertEqual(self.machine.move_list(), [(1.0, 3.5), (1.0, 3.5), (1.0, 4.0), (1.5, 4.0), (1.5, 3.5),
                                                    (2.0, 3.5), (2.0, 4.0), (1.0, 3.5)])
        self.assertEqual((st["result"]["xs"], st["result"]["ys"]), ([1.0, 1.5, 2.0], [0.5, 1.0]))
        self.assertTrue(st["result"]["text"].startswith("# minX=1.00000 maxX=2.00000\n# minY=0.50000 maxY=1.00000\n"))
        self.assertIn(("info", "Scan point 2 of 6: X1.0000 Y1.0000 Z0.0100"), self.logs)

    def test_no_scan_without_an_area(self):
        """A first run's Min / Max all 0: Start Scan is refused before anything moves."""
        with self.assertRaises(MachineError) as cm:
            self.scanner.start(dict(SMALL, minX=0.0, maxX=0.0, minY=0.0, maxY=0.0))
        self.assertEqual(str(cm.exception), scan.UNSET)
        self.assertEqual([c for c in self.machine.calls if c[0] == "move"], [])

    def test_move_timeouts_like_ugs(self):
        self.run_scan()
        timeouts = [c[3] for c in self.machine.calls if c[0] == "move"]
        self.assertTrue(all(t >= 15.0 for t in timeouts))
        self.machine.calls.clear()
        far = dict(SMALL, minX=-90.0, maxX=90.0, xPoints=2, yPoints=1)
        self.machine.snapshot = lambda: {"status": {"wpos": [0.0, 0.0, 0.0]}, "axes": {"x": {"maxRate": 60.0},
                                                                                       "y": {"maxRate": 30.0}}}
        self.probe.home()
        self.run_scan(far)
        moves = [c for c in self.machine.calls if c[0] == "move"]
        # 3x the time at the slowest X/Y max rate (30 in/min) + 2 s, from where the machine was (0, 0)
        self.assertAlmostEqual(moves[0][3], (90.0 ** 2 + 0.5 ** 2) ** 0.5 / 30.0 * 60 * 3 + 2)
        self.assertAlmostEqual(moves[2][3], 180.0 / 30.0 * 60 * 3 + 2)
        # scan_move returns where FluidNC got to: the next move's time counts from there
        self.machine.calls.clear()
        real = self.machine.scan_move

        def reports(x, y, timeout):
            real(x, y, timeout)
            return {"x": x - 60.0, "y": y, "z": 0.0}
        self.machine.scan_move = reports
        self.probe.home()
        self.run_scan(far)
        moves = [c for c in self.machine.calls if c[0] == "move"]
        self.assertAlmostEqual(moves[2][3], 240.0 / 30.0 * 60 * 3 + 2)    # from -150, not -90

    def test_move_timeouts_count_the_rapid_override(self):
        """The moves are G0s: with FluidNC's rapid override at 25 % they take 4 times as long, and so may their
        timeouts (else a long move fails the scan: the reviewer's C). Read at every move."""
        far = dict(SMALL, minX=-90.0, maxX=90.0, xPoints=2, yPoints=1)
        self.machine.snapshot = lambda: {"status": {"wpos": [0.0, 0.0, 0.0]}, "axes": {"x": {"maxRate": 60.0},
                                                                                       "y": {"maxRate": 30.0}}}
        rapid = {0: 25, 1: 25, 2: 50}                   # (by the moves made so far; then 100 %)
        self.machine.status_now = lambda: {"state": "Idle", "wpos": [0.0, 0.0, 0.5],
                                           "ov": [100, rapid.get(self.machine.moves, 100), 100]}
        self.run_scan(far)
        moves = [c for c in self.machine.calls if c[0] == "move"]
        self.assertAlmostEqual(moves[0][3], (90.0 ** 2 + 0.5 ** 2) ** 0.5 / (30.0 * 0.25) * 60 * 3 + 2)
        self.assertAlmostEqual(moves[2][3], 180.0 / (30.0 * 0.5) * 60 * 3 + 2)
        self.assertEqual(moves[3][3], 180.0 / 30.0 * 60 * 3 + 2)        # (back to the first point at 100 %)
        self.machine.calls.clear()
        self.machine.status_now = lambda: {"state": "Idle", "wpos": [0.0, 0.0, 0.5], "ov": None}
        self.probe.home()
        self.run_scan(far)
        moves = [c for c in self.machine.calls if c[0] == "move"]
        self.assertAlmostEqual(moves[2][3], 180.0 / 30.0 * 60 * 3 + 2)    # (not known: 100 %)

    def test_save_to_a_chosen_file(self):
        with self.assertRaises(MachineError) as cm:
            self.scanner.save(os.path.join(self.dir, "scan.xyz"))
        self.assertEqual((cm.exception.status, str(cm.exception)), (409, "No complete scan to save yet"))
        st = self.run_scan()
        for bad, status in (("scan.txt", 400), ("nope/scan.xyz", 404)):
            with self.assertRaises(MachineError) as cm:
                self.scanner.save(os.path.join(self.dir, bad))
            self.assertEqual(cm.exception.status, status)
        path = os.path.join(self.dir, "scan.XYZ")       # (the user's own name keeps its case)
        out = self.scanner.save(path)
        self.assertEqual(out, {"path": path, "size": len(st["result"]["text"])})
        with open(path, newline="") as f:
            self.assertEqual(f.read(), st["result"]["text"])
        self.assertEqual(self.scanner.state()["savedTo"], path)
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(["fake.log", "scan.XYZ", "scans"]))

    def test_auto_save_failure_keeps_the_scan(self):
        with open(os.path.join(self.dir, "file"), "w"):
            pass
        self.scanner.scans_dir = os.path.join(self.dir, "file", "scans")     # (can't be made)
        st = self.run_scan()
        self.assertEqual((st["phase"], st["savedTo"]), ("done", None))
        self.assertTrue(st["result"]["complete"])
        self.assertTrue(any(k == "error" and t.startswith("Could not save the scan to") for k, t in self.logs))
        self.scanner.save(os.path.join(self.dir, "keep.xyz"))       # it can still be saved by hand

    def test_save_settings(self):
        self.scanner.save_settings(UGS)
        out = self.scanner.save_settings({"xPoints": 4})
        self.assertEqual(out, {"settings": dict(UGS, xPoints=4)})
        self.assertEqual(self.store.data["scan"], dict(UGS, xPoints=4))
        self.assertEqual(self.events[-1]["scan"]["settings"], dict(UGS, xPoints=4))
        self.assertEqual(self.events[-1]["scan"]["points"], 20)
        with self.assertRaises(MachineError):
            self.scanner.save_settings({"xPoints": 0})
        self.assertEqual(self.store.data["scan"]["xPoints"], 4)
        self.assertEqual(self.scanner.start()["points"], 20)    # (no settings: the saved ones)
        self.scanner.stop()

    def test_scan_again_without_homing(self):
        """The final H makes the probe ready again; the next scan's first point reads 0.0 too."""
        self.run_scan()
        self.machine = FakeMachine()
        self.scanner.machine = self.machine
        st = self.run_scan(dict(SMALL, xPoints=1, minX=1.0, maxX=1.0))
        self.assertEqual(st["phase"], "done")
        self.assertEqual(st["result"]["points"][0][2], 0.0)


class RealScan(ScanCase):
    """The user's own scan again, through the whole chain: the file must come out byte for byte the same."""

    def setUp(self):
        z = real_grid()
        self.fake_args = ("--values", ",".join(repr(z[p]) for p in probe_order(15, 5)))
        super().setUp()

    def test_same_file_as_ugs(self):
        st = self.run_scan(UGS, timeout=60)
        self.assertEqual(st["phase"], "done", st["error"])
        with open(os.path.join(self.scans, "last-scan.xyz"), "rb") as f:
            self.assertEqual(f.read(), REAL_SCAN.encode())


class Failures(ScanCase):
    def start_fake_again(self, *args, home=True):
        self.probe.disconnect(force=True)
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.fake_log = os.path.join(self.dir, f"fake-{len(args)}-{time.monotonic_ns()}.log")
        self.proc = subprocess.Popen([sys.executable, FAKE, "--home-time", self.home_time, "--probe-time",
                                      self.probe_time, "--log", self.fake_log, *args],
                                     stdout=subprocess.PIPE, text=True)
        self.path = self.proc.stdout.readline().strip()
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)
        if home:
            self.probe.home()

    def test_over_travel(self):
        self.start_fake_again("--overtravel-at", "3")
        st = self.run_scan()
        self.assert_ended("failed")
        self.assertEqual(st["error"], "Over Travel Error: Probe reported over travel while probing. "
                                      "Probe retracted, scan cancelled.")
        self.assertEqual(self.logs[-1], ("error", st["error"]))
        # no move after the over travel; the probe was aborted and homed
        self.assertEqual(len(self.machine.move_list()), 4)
        self.assertEqual(self.commands(), ["H", "ZR:0.250", "P", "ZR:0.250", "P", "ZR:0.250", "P", "A", "H"])
        self.assertTrue(self.probe.state()["ready"])
        self.assertFalse(self.probe.state()["mayBeDown"])
        self.assertEqual((st["point"], st["result"]["complete"], st["savedTo"]), (2, False, None))
        self.assertFalse(os.path.exists(os.path.join(self.scans, "last-scan.xyz")))
        self.assertNotIn(("stop",), self.machine.calls)

    def test_over_travel_and_no_retract(self):
        self.start_fake_again("--overtravel-at", "2", "--reply", "H#2=none")
        with mock.patch.object(probe, "REPLY_TIMEOUT", 0.3):
            st = self.run_scan()
        self.assertEqual(st["error"], "Over Travel Error: Probe reported over travel while probing. Probe did "
                                      "NOT confirm retract (No response from Auto Probe. Is it connected?). Check "
                                      "the probe before moving the machine. Scan cancelled.")
        self.assertEqual((self.probe.state()["ready"], self.probe.state()["mayBeDown"]), (False, True))
        self.assert_ended("failed")

    def test_retract_errors_count_as_over_travel(self):
        self.start_fake_again("--reply", "ZR#2=E")
        st = self.run_scan()
        self.assertEqual(st["error"], "Over Travel Error: Probe reported an error on Z retract (ZR). "
                                      "Probe retracted, scan cancelled.")
        self.assertEqual(self.commands()[-2:], ["A", "H"])
        self.assert_ended("failed")

    def test_no_reply_to_a_touch(self):
        self.start_fake_again("--reply", "P#2=none")
        with mock.patch.object(probe, "REPLY_TIMEOUT", 0.3):
            st = self.run_scan()
        self.assertEqual(st["error"], "Scan failed: No response from probe to P. Probe retracted.")
        self.assertEqual(self.commands()[-2:], ["A", "H"])
        self.assert_ended("failed")

    def test_bad_number(self):
        self.start_fake_again("--reply", "P#2=abc")
        st = self.run_scan()
        self.assertEqual(st["error"], "Scan failed: Invalid probe numeric response: abc. Probe retracted.")
        self.assert_ended("failed")

    def test_machine_fails_a_move(self):
        self.machine.fail_move = {3: "ALARM:1 Hard limit"}
        st = self.run_scan()
        self.assertEqual(st["error"], "Scan failed: ALARM:1 Hard limit. Probe retracted.")
        self.assertEqual(len(self.machine.move_list()), 3)
        self.assertEqual(self.commands(), ["H", "ZR:0.250", "P", "A", "H"])
        self.assert_ended("failed")

    def test_final_retract_fails(self):
        self.start_fake_again("--reply", "H#2=E")
        st = self.run_scan()
        self.assertEqual(st["error"], "Scan failed: the final probe retract / return did not finish (scan data was "
                                      "kept): Probe error on final retract (H): E.")
        self.assertTrue(st["result"]["complete"])
        self.assertTrue(os.path.exists(os.path.join(self.scans, "last-scan.xyz")))
        self.assertEqual(self.machine.move_list()[-1], (2.0, 1.0))     # (no return move)
        self.assertEqual((self.probe.state()["ready"], self.probe.state()["mayBeDown"]), (False, True))
        self.assert_ended("failed")

    def test_return_move_fails(self):
        self.machine.fail_move = {8: "Timeout waiting for XY move to complete after 15000ms."}
        st = self.run_scan()
        self.assertEqual(st["error"], "Scan failed: the final probe retract / return did not finish (scan data was "
                                      "kept): Timeout waiting for XY move to complete after 15000ms.")
        self.assertTrue(st["result"]["complete"])
        self.assertFalse(self.probe.state()["mayBeDown"])           # (homed before the return move)
        self.assert_ended("failed")

    def test_probe_lost(self):
        self.machine.hold_move = 3
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.machine.moves == 3 and self.machine.moving.is_set())
        self.proc.kill()
        self.proc.wait()
        self.wait_for(lambda: not self.probe.state()["connected"])
        self.machine.hold_move = None
        self.machine.stopped.set()          # (the move ends; the machine says why)
        self.scanner.thread.join(5)
        st = self.scanner.state()
        self.assertTrue(st["error"].startswith("Scan failed: Stopped. Probe did NOT confirm retract ("), st["error"])
        ps = self.probe.state()
        self.assertEqual((ps["connected"], ps["mayBeDown"]), (False, True))    # (a page opened later still warns)
        self.assert_ended("failed")


class Refusals(ScanCase):
    def refused(self, settings=SMALL):
        with self.assertRaises(MachineError) as cm:
            self.scanner.start(settings)
        self.assertFalse(self.scanner.state()["running"])
        self.assertIsNone(self.probe.owner)
        self.assertIsNone(self.scanner.thread)
        self.assertNotIn(("move",), [c[:1] for c in self.machine.calls])
        return cm.exception

    def test_probe_not_homed(self):
        self.probe.abort()
        e = self.refused()
        self.assertEqual((e.status, str(e)), (409, "Probe not ready. Home the probe before scanning."))
        self.assertEqual(self.machine.calls, [])

    def test_probe_not_connected(self):
        self.probe.disconnect()
        self.assertEqual(str(self.refused()), "The probe is not connected. Connect it first.")

    def test_probe_busy(self):
        self.probe.cmd_lock.acquire()
        self.probe.busy = "H"
        try:
            self.assertEqual(str(self.refused()), "The probe is busy: wait until it is done")
        finally:
            self.probe.busy = None
            self.probe.cmd_lock.release()

    def test_machine_refuses(self):
        self.machine.refuse = "A job is running"
        e = self.refused()
        self.assertEqual((e.status, str(e)), (409, "A job is running"))
        self.assertEqual(self.machine.calls, [("begin", "scan")])
        self.assertTrue(self.probe.state()["ready"])        # (nothing happened to it)

    def test_bad_settings(self):
        e = self.refused(dict(SMALL, xPoints=0))
        self.assertEqual(e.status, 400)
        self.assertEqual(self.machine.calls, [])

    def test_one_scan_at_a_time(self):
        self.machine.hold_move = 1
        self.scanner.start(SMALL)
        with self.assertRaises(MachineError) as cm:
            self.scanner.start(SMALL)
        self.assertEqual((cm.exception.status, str(cm.exception)), (409, "A scan is already running"))
        with self.assertRaises(MachineError):     # nobody else may use the probe meanwhile
            self.probe.home()
        with self.assertRaises(MachineError):
            self.probe.disconnect()
        self.scanner.stop()


class Stops(ScanCase):
    probe_time = "0.02"

    def test_stop_during_a_move(self):
        self.machine.hold_move = 4
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.machine.moves == 4 and self.machine.moving.is_set())
        t0 = time.monotonic()
        self.assertTrue(self.scanner.stop())
        self.scanner.thread.join(5)
        self.assertLess(time.monotonic() - t0, 1.0)
        st = self.assert_ended("stopped")
        self.assertEqual(st["error"], "Scan stopped. Probe retracted (homed).")
        self.assertEqual(self.machine.calls.count(("stop",)), 1)
        self.assertEqual(len(self.machine.move_list()), 4)          # nothing moved after the stop
        self.assertEqual(self.commands(), ["H"] + ["ZR:0.250", "P"] * 2 + ["A", "H"])   # aborted, then homed
        self.assertEqual((self.probe.state()["ready"], self.probe.state()["mayBeDown"]), (True, False))
        self.assertFalse(self.scanner.stop())                       # (nothing to stop any more)
        self.assertIn(("info", "Scan stopped. Probe retracted (homed)."), self.logs)

    def test_stop_during_a_touch(self):
        self.probe.disconnect(force=True)
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc = subprocess.Popen([sys.executable, FAKE, "--home-time", "0.005", "--probe-time", "10",
                                      "--log", self.fake_log], stdout=subprocess.PIPE, text=True)
        self.path = self.proc.stdout.readline().strip()
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)
        self.probe.home()
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.commands()[-1:] == ["P"] and self.probe.state()["busy"])
        t0 = time.monotonic()
        stopper = threading.Thread(target=self.scanner.stop)      # (from another thread)
        stopper.start()
        stopper.join(2)
        self.scanner.thread.join(5)
        self.assertLess(time.monotonic() - t0, 1.0)                 # not the 10 s of the touch
        self.assert_ended("stopped")
        received = self.received()
        self.assertEqual([e["cmd"] for e in received if "cmd" in e][-4:], ["ZR:0.250", "P", "A", "H"])  # then homed
        self.assertTrue(any("abort" in e for e in received))
        self.assertEqual((self.probe.state()["ready"], self.probe.state()["mayBeDown"]), (True, False))
        self.assertEqual(self.machine.calls.count(("stop",)), 1)
        self.assertEqual(len(self.machine.move_list()), 2)

    def test_stop_during_the_final_retract(self):
        self.probe.disconnect(force=True)
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.proc = subprocess.Popen([sys.executable, FAKE, "--home-time", "0.5", "--probe-time", "0.005",
                                      "--log", self.fake_log], stdout=subprocess.PIPE, text=True)
        self.path = self.proc.stdout.readline().strip()
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)
        self.probe.home()
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.scanner.state()["phase"] == "homing" and self.probe.state()["busy"])
        self.scanner.stop()
        self.scanner.thread.join(5)
        st = self.assert_ended("stopped")
        self.assertEqual(st["error"], "Scan complete, data kept. The final probe retract / return move was stopped. "
                                      "Probe retracted (homed).")
        self.assertTrue(st["result"]["complete"])
        self.assertEqual(st["savedTo"], os.path.join(self.scans, "last-scan.xyz"))
        self.assertEqual(self.machine.move_list()[-1], (2.0, 1.0))      # no return move
        self.assertEqual((self.probe.state()["ready"], self.probe.state()["mayBeDown"]), (True, False))

    def test_stop_while_starting(self):
        """A stop that comes before the scan thread runs: nothing moves."""
        real = self.machine.begin_activity

        def begin(name):
            real(name)
            self.assertTrue(self.scanner.stop())
        self.machine.begin_activity = begin
        self.scanner.start(SMALL)
        self.scanner.thread.join(5)
        st = self.assert_ended("stopped")
        self.assertEqual(st["error"], "Scan stopped. Probe retracted (homed).")
        self.assertEqual(self.machine.move_list(), [])
        self.assertEqual(self.commands(), ["H", "A", "H"])

    def test_machine_stop_calls_back(self):
        """machine.stop() may call scanner.stop() itself (POST /api/machine/stop during a scan): no loop."""
        self.machine.on_stop = self.scanner.stop
        self.machine.hold_move = 2
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.machine.moves == 2 and self.machine.moving.is_set())
        self.assertTrue(self.scanner.stop())
        self.scanner.thread.join(5)
        self.assert_ended("stopped")
        self.assertEqual(self.machine.calls.count(("stop",)), 1)

    def test_stop_without_the_machine(self):
        """stop(stop_machine=False): what machine.stop() calls when it does the CNC stop itself."""
        self.machine.hold_move = 2
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.machine.moves == 2 and self.machine.moving.is_set())
        self.assertTrue(self.scanner.stop(stop_machine=False))
        self.assertNotIn(("stop",), self.machine.calls)
        self.assertEqual(self.commands()[-1], "A")
        self.machine.stop()                 # (what the machine's own stop then does)
        self.scanner.thread.join(5)
        self.assert_ended("stopped")

    def test_machine_stopped_by_someone_else(self):
        """The CNC is stopped without scanner.stop() (e.g. a stop the server didn't route here): the scan still
        ends, the probe retracted, and nothing is left running."""
        self.machine.hold_move = 3
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.machine.moves == 3 and self.machine.moving.is_set())
        self.machine.stop()
        self.scanner.thread.join(5)
        st = self.assert_ended("failed")
        self.assertEqual(st["error"], "Scan failed: Stopped. Probe retracted.")

    def test_machine_stop_first(self):
        """machine.py's Stop sets activity_stop and wakes scan_move before it calls scanner.stop(): the move
        fails first. That is still a stop: the probe is aborted, then homed."""
        self.machine.hold_move = 3
        real = self.machine.stop

        def machine_stop():
            self.machine.activity_stop = True
            real()
            self.scanner.thread.join(5)     # (the worst case: the scan has ended before stop() comes)
            self.assertFalse(self.scanner.stop(stop_machine=False))
        self.machine.stop = machine_stop
        self.scanner.start(SMALL)
        self.wait_for(lambda: self.machine.moves == 3 and self.machine.moving.is_set())
        self.machine.stop()
        st = self.assert_ended("stopped")
        self.assertEqual(st["error"], "Scan stopped. Probe retracted (homed).")
        self.assertEqual(self.commands(), ["H", "ZR:0.250", "P", "A", "H"])     # (point 1 only) aborted, then homed
        self.assertTrue(self.probe.state()["ready"])

    def test_stop_when_nothing_runs(self):
        self.assertFalse(self.scanner.stop())
        self.assertEqual(self.machine.calls, [])
        self.assertEqual(self.commands(), ["H"])


if __name__ == "__main__":
    unittest.main()
