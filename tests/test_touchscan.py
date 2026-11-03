"""
The touch probe scan (touchscan.py), end to end: pretend FluidNCs (tools/fake_fluidnc.py, reporting inches like the
user's machine, with a pretend copper surface for G38.2) and a pretend auto probe (tools/fake_probe.py) on pseudo
terminals, a real server on port 18822 (FLUIDSENDER_TOUCH_TEST_PORT sets another; else FLUIDSENDER_TEST_PORT + 2),
and the HTTP calls and event stream the way the page uses them: the heights match the surface, the probe check
(the input seen open, closed, open again: kill -USR2 on the simulator is the user touching the clip to the bit),
Pause / Resume between points, Stop (the touch tab's, and the machine's) raises Z and never sends anything to the
auto probe, and the refusals (no area, no check, the input closed, the spindle on, no contact). Also the settings
(their own, apart from the auto probe's) and the auto probe scan's Stop raising Z (scan.py with a pretend machine).
And Touch off Z (a touch plate on the stock): the work Z it sets, Undo Zero, its refusals, no contact, Stop.

No real serial port is ever opened: the server runs with FLUIDSENDER_ONLY_EXTRA_PORTS=1. Its settings file and scans
folder are temporary.

    python3 -m unittest tests.test_touchscan        (about a minute)
"""

import json
import os
import signal
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leveler  # noqa: E402
import settings  # noqa: E402
import touchscan  # noqa: E402
from machine import NO_CHECK, SPINDLE_ON, MachineError, SpindleOn  # noqa: E402
from test_e2e_level import Events, Probe, Server, Sim, console  # noqa: E402

PORT = int(os.environ.get("FLUIDSENDER_TOUCH_TEST_PORT")
           or (int(os.environ["FLUIDSENDER_TEST_PORT"]) + 2 if os.environ.get("FLUIDSENDER_TEST_PORT") else 18822))
# the pretend copper: machine Z -30 mm (the simulator's work Z 0) at the work zero, rising 0.01 mm per mm in X and
# 0.02 in Y: its work Z (in) at work X, Y (in) is 0.01 X + 0.02 Y
SLOPE = (0.01, 0.02)
GRID = {"minX": 0, "maxX": 0.2, "xPoints": 3, "minY": 0, "maxY": 0.1, "yPoints": 2, "clearance": 0.1, "depth": 0.1,
        "feed": 20}
TOP = "Z raised to the top (machine Z -0.04 in)."     # (the simulator's Z travel: -100 .. 0 mm)


# Touch off Z: a 0.25 in plate on the stock, whose top is the simulator's boot work Z 0 (machine Z -30 mm); the
# plate's top is then at machine Z -30 + 6.35 mm
PLATE = 0.25
PLATE_TOP = -30 + PLATE * 25.4
OFF = {"plateThickness": PLATE, "searchDistance": 1, "feed": 20, "liftAfter": 0.25}


def touch_phase(*phases):
    return lambda e: e["type"] == "touch" and e["touch"] and e["touch"]["phase"] in phases


def off_phase(*phases):
    return lambda e: e["type"] == "touch" and e["touch"] and e["touch"]["off"]["phase"] in phases


class TouchSettings(unittest.TestCase):
    """settings.py's "touchScan" and touchscan.check_settings: their own, checked like the auto probe's."""

    def test_defaults_and_checks(self):
        self.assertEqual(touchscan.check_settings(), dict(settings.TOUCH))
        self.assertTrue(touchscan.area_unset(touchscan.check_settings()))
        got = touchscan.check_settings({"maxX": 2, "xPoints": 3}, {"maxY": 1, "feed": 5})
        self.assertEqual((got["maxX"], got["maxY"], got["feed"], got["xPoints"]), (2, 1, 5, 3))
        for bad, why in (({"clearance": 0}, "Touch scan clearance: a number from 0.02 to 2 in"),
                         ({"depth": 2}, "Touch scan depth: a number from 0.01 to 1 in"),
                         ({"feed": 0}, "Touch scan feed: a number from 0.1 to 50 in/min"),
                         ({"minX": 2, "maxX": 1}, "Touch scan: min X must be below max X (the same only with 1 point in X)"),
                         ({"probeOffY": 3}, "Unknown touch scan setting: probeOffY")):
            with self.assertRaises(MachineError) as e:
                touchscan.check_settings(bad)
            self.assertEqual(str(e.exception), why)
        self.assertEqual(touchscan.load_settings({"feed": "x"}), dict(settings.TOUCH))

    def test_touch_off_settings(self):
        got = touchscan.check_off_settings()
        self.assertEqual(got, {"plateThickness": 0.0, "searchDistance": 1.0, "feed": 2.0, "liftAfter": 0.25})
        got = touchscan.check_off_settings({"plateThickness": 0.5}, {"feed": 5, "searchDistance": 2})
        self.assertEqual(got, {"plateThickness": 0.5, "searchDistance": 2.0, "feed": 5.0, "liftAfter": 0.25})
        for bad, why in (({"plateThickness": -0.1}, "Plate thickness: a number from 0 to 2 in"),
                         ({"searchDistance": 3.5}, "Search distance: a number from 0.05 to 3 in"),
                         ({"feed": 0.2}, "Touch off feed: a number from 0.5 to 20 in/min"),
                         ({"feed": 25}, "Touch off feed: a number from 0.5 to 20 in/min"),
                         ({"liftAfter": "x"}, "Lift after: a number from 0 to 2 in"),
                         ({"depth": 1}, "Unknown touch off setting: depth")):
            with self.assertRaises(MachineError) as e:
                touchscan.check_off_settings(bad)
            self.assertEqual(str(e.exception), why)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            s = settings.Settings(path)
            s.set("touchOff", dict(OFF))
            self.assertEqual(settings.Settings(path).get("touchOff")["plateThickness"], PLATE)
            self.assertEqual(s.get("touchScan"), settings.TOUCH)
        # a zero record from Touch off Z passes settings.json's check (Undo Zero keeps it over restarts)
        record = {"axes": "Z", "wcs": 1, "wco": [0, 0, -30], "value": 0.25, "t": 1.0, "source": "touch off",
                  "text": "Z 0.05"}
        self.assertEqual(settings.check_zeros([record]), [dict(record, wco=[0.0, 0.0, -30.0])])

    def test_kept_apart_from_the_auto_probe(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            s = settings.Settings(path)
            scan = s.get("scan")
            s.set("touchScan", dict(GRID))
            self.assertEqual(s.get("scan"), scan)
            with open(path) as f:
                saved = json.load(f)
            self.assertEqual(saved["touchScan"], {k: float(v) if k not in ("xPoints", "yPoints") else v
                                                  for k, v in GRID.items()})
            self.assertEqual(settings.Settings(path).get("touchScan")["clearance"], 0.1)


class FakeMachine:
    """machine.py's side of a touch scan, for what the simulator can't do: the spindle coming on during it."""

    def __init__(self):
        self.calls = []
        self.activity = None
        self.activity_stop = False

    def begin_activity(self, name):
        self.activity = name

    def end_activity(self, name):
        self.activity = None

    def touch_ready(self):
        return [0.0, 0.0, 0.5]

    def touch_line(self, text, timeout, at=None, probe=False):
        self.calls.append(text)
        if probe:
            raise SpindleOn("The spindle came on during the touch scan", 409)
        return {"x": 0.0, "y": 0.0, "z": 0.1}

    def stop(self):
        self.calls.append("stop")

    def status_now(self):
        return {"state": "Idle", "wpos": [0.0, 0.0, 0.5]}


class SpindleDuringTheScan(unittest.TestCase):
    def test_the_spindle_on_ends_it_at_once(self):
        m, logs = FakeMachine(), []
        with tempfile.TemporaryDirectory() as d:
            t = touchscan.TouchScanner(m, {}, None, lambda k, x: logs.append((k, x)), scans_dir=d)
            t.start(GRID)
            t.thread.join(5)
            st = t.state()
        self.assertEqual(st["phase"], "failed")
        self.assertEqual(st["error"], "Touch scan stopped at point 1 of 6: the spindle came on. FluidNC was stopped "
                                      "(Hold, then Soft Reset, which turns it off). Never run a touch scan with the "
                                      "spindle on.")
        self.assertEqual(m.calls[-2:], ["G20 G90 G38.2 Z-0.1 F20", "stop"])
        self.assertIsNone(m.activity)

    def test_touch_off_too(self):
        m, logs = FakeMachine(), []
        t = touchscan.TouchScanner(m, {}, None, lambda k, x: logs.append((k, x)))
        t.touch_off(OFF)
        t.thread.join(5)
        off = t.state()["off"]
        self.assertEqual((off["phase"], off["z"]), ("failed", None))
        self.assertEqual(off["error"], "Touch off Z stopped: the spindle came on. FluidNC was stopped (Hold, then Soft "
                                       "Reset, which turns it off). Z not set.")
        self.assertEqual(m.calls, ["G20 G90 G38.2 Z-0.5 F20", "stop"])
        self.assertIsNone(m.activity)
        self.assertFalse(t.state()["clipWarn"])


class SimServer(unittest.TestCase):
    """One server for the class (on PORT: the classes run one after the other); each test connects to its own
    simulator. The tests are TouchScan's and TouchOffZ's."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sims, cls.server, cls.probe = [], None, None
        try:
            cls.cnc = cls.start_sim("touch", "--inches", f"--surface=-30,{SLOPE[0]},{SLOPE[1]}")
            cls.stops = cls.start_sim("stops", "--inches", f"--surface=-30,{SLOPE[0]},{SLOPE[1]}")
            cls.flat = cls.start_sim("nocontact", "--inches", "--surface=-40")   # (below the touch's depth)
            cls.plate = cls.start_sim("plate", "--inches", f"--surface={PLATE_TOP}")
            cls.probe = Probe(cls.tmp.name, "--probe-time", "0.05", "--home-time", "0.05")
            cls.server = Server(PORT, [s.path for s in cls.sims] + [cls.probe.path], cls.tmp.name)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def start_sim(cls, name, *args):
        sim = Sim(cls.tmp.name, name, *args)
        cls.sims.append(sim)
        return sim

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.stop()
        for sim in cls.sims:
            sim.stop()
        if cls.probe:
            cls.probe.stop()
        cls.tmp.cleanup()

    # -------------------------------------------------- helpers

    def call(self, path, body=None):
        return self.server.call(path, body)

    def ok(self, path, body=None):
        st, data = self.call(path, body)
        self.assertEqual(st, 200, (path, body, data))
        return data

    def events(self):
        ev = Events(PORT)
        self.addCleanup(ev.close)
        ev.wait(lambda e: e["type"] == "snapshot", what="snapshot")
        return ev

    def state(self):
        return self.call("/api/machine/state")[1]

    def wait_for(self, cond, what, timeout=10.0):
        end = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), end, f"no {what}")
            time.sleep(0.05)

    def ready(self, ev, sim):
        """Connected, FluidSender's G20 answered, the work position known; at the work zero, Z 0.3."""
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/connect", {"port": sim.path, "baud": 115200}), (200, {"ok": True}))
        self.addCleanup(self.disconnect)
        _, k = ev.wait(console("tx", "G20"), since=k, what="G20")
        ev.wait(console("rx", "ok"), since=k, what="ok for G20")
        self.wait_for(lambda: (self.state()["status"] or {}).get("wpos") is not None, "a work position", 8)
        self.go("G0 X0 Y0 Z0.3", [0, 0, 0.3])

    def disconnect(self):
        if self.state()["connected"]:
            self.call("/api/machine/disconnect", {})
            self.wait_for(lambda: not self.state()["connected"], "disconnected")

    def status(self):
        return self.state()["status"] or {}

    def go(self, line, wpos, tol=1e-4):
        self.assertEqual(self.call("/api/machine/send", {"line": line}), (200, {"ok": True}), line)
        self.wait_for(lambda: self.status().get("state") == "Idle" and self.status().get("wpos")
                      and all(abs(g - w) <= tol for g, w in zip(self.status()["wpos"], wpos)), f"Idle at {wpos}")

    def clip(self, sim):
        """The user touches the clip to the bit (the probe input closes), then lets go."""
        sim.proc.send_signal(signal.SIGUSR2)
        self.wait_for(lambda: "P" in (self.status().get("pins") or ""), "the input closed", 3)
        sim.proc.send_signal(signal.SIGUSR2)
        self.wait_for(lambda: "P" not in (self.status().get("pins") or ""), "the input open again", 3)
        self.wait_for(lambda: self.status().get("probeCheck") == "verified", "the probe check passed", 3)

    def start(self, grid=GRID):
        return self.call("/api/touch/start", {"settings": grid})

    def wait_end(self, ev, k, timeout=60):
        end, _ = ev.wait(touch_phase("done", "failed", "stopped"), since=k, timeout=timeout, what="the scan's end")
        return end["touch"]

    def wait_point(self, ev, k, n):
        """Until point n has been touched; returns the event mark after it."""
        _, k = ev.wait(console("info", start=f"Touch scan point {n} of "), since=k, timeout=30, what=f"point {n}")
        return k



class TouchScan(SimServer):

    def test_1_refusals_then_a_whole_scan(self):
        sim = self.cnc
        ev = self.events()
        self.ready(ev, sim)
        self.assertEqual(self.start(dict(GRID, maxX=0, maxY=0, minX=0, minY=0)),
                         (400, {"error": touchscan.UNSET}))
        # the probe check: nothing but the input seen open, closed, open again passes it
        self.assertEqual(self.status()["probeCheck"], "open")
        self.assertEqual(self.start(), (409, {"error": NO_CHECK}))
        sim.proc.send_signal(signal.SIGUSR2)        # closed: refused, and not passed yet
        self.wait_for(lambda: self.status().get("probeCheck") == "closed", "closed seen", 3)
        st, data = self.start()
        self.assertEqual(st, 409)
        self.assertTrue(data["error"].startswith("The probe input is already closed: check the clip."), data)
        sim.proc.send_signal(signal.SIGUSR2)
        self.wait_for(lambda: self.status().get("probeCheck") == "verified", "verified", 3)
        # the spindle on: refused (the check stays)
        self.go("M3 S1000", [0, 0, 0.3])
        self.wait_for(lambda: (self.status().get("spindle") or 0) > 0, "the spindle on", 5)
        self.assertEqual(self.start(), (409, {"error": SPINDLE_ON}))
        self.go("M5", [0, 0, 0.3])
        self.go("G0 Z0.31", [0, 0, 0.31])                # (FluidNC shows the S after M5 until something moves)
        self.go("G0 Z0.3", [0, 0, 0.3])
        self.wait_for(lambda: not self.status().get("spindle"), "the spindle off", 5)
        # the scan: the check is used up by it
        log0, k = len(sim.entries()), ev.mark()
        self.assertEqual(self.start(), (200, {"ok": True, "points": 6}))
        self.assertEqual(self.call("/api/machine/send", {"line": "G0 X1"}), (409, {"error": "A touch scan is running"}))
        sc = self.wait_end(ev, k)
        self.assertEqual((sc["phase"], sc["error"], sc["clipWarn"]), ("done", None, True), sc)
        self.assertTrue(sc["message"].startswith("Touch scan complete: 6 points in "), sc["message"])
        for x, y, z in sc["result"]["points"]:
            self.assertAlmostEqual(z, SLOPE[0] * x + SLOPE[1] * y, delta=2e-4, msg=(x, y, z))
        lines = sim.lines(log0)
        touch = "G20 G90 G38.2 Z-0.1 F20"
        self.assertEqual(lines[:5], ["G20 G90 G0 X0 Y0", "G20 G90 G0 Z0.1", touch, "G20 G90 G0 Z0.1",
                                     "G20 G90 G0 X0 Y0.1"])
        self.assertEqual(lines.count(touch), 6)
        self.assertEqual(lines[-1], "G20 G90 G0 Z0.1")
        text = sc["result"]["text"]
        self.assertRegex(text.splitlines()[4], r"^# wcs=G54 wcoX=-5\.905500 wcoY=-3\.937000 wcoZ=-1\.181100 time=\d+$")
        with open(os.path.join(self.tmp.name, "scans", "last-scan.xyz")) as f:
            self.assertEqual(f.read(), text)
        mesh = leveler.parse_scan(text)
        self.assertEqual((len(mesh.xs), len(mesh.ys)), (3, 2))
        self.assertIn(self.status()["probeCheck"], ("none", "open"))     # (used up)
        self.assertEqual(self.start()[0], 409)
        # auto-level's "last scan" is this one; Save Scan writes it
        self.ok("/api/machine/program", {"name": "t.nc", "text": "G20 G90\nG0 Z0.1\nG0 X0 Y0\nG1 Z-0.01 F10\nG1 X0.2\n"})
        out = self.ok("/api/machine/level", {"scan": "last"})
        self.assertEqual(out["scan"], mesh.info())
        self.ok("/api/machine/level", {"off": True})
        path = os.path.join(self.tmp.name, "touch.xyz")
        self.assertEqual(self.call("/api/touch/save", {"path": path}), (200, {"ok": True, "path": path,
                                                                             "size": len(text)}))
        # the remove-the-clip warning lasts until FluidNC is disconnected
        self.disconnect()
        self.assertFalse(self.state()["touch"]["clipWarn"])

    def test_2_pause_and_resume(self):
        sim = self.cnc
        ev = self.events()
        self.ready(ev, sim)
        self.clip(sim)
        k = ev.mark()
        self.assertEqual(self.call("/api/touch/pause", {}), (409, {"error": "No touch scan is running"}))
        self.assertEqual(self.start(), (200, {"ok": True, "points": 6}))
        k2 = self.wait_point(ev, k, 2)
        self.assertEqual(self.call("/api/touch/pause", {}), (200, {"ok": True, "paused": False, "pausing": True}))
        p, _ = ev.wait(touch_phase("paused"), since=k2, timeout=15, what="paused")
        self.assertTrue(p["touch"]["paused"])
        n = p["touch"]["point"]
        self.assertIn(n, (2, 3))
        ev.wait(console("info", f"Touch scan paused at point {n} of 6 (Z at the clearance). Resume goes on with point "
                                f"{n + 1}."), since=k)
        self.wait_for(lambda: abs(self.status()["wpos"][2] - 0.1) < 1e-4 and self.status()["state"] == "Idle", "at the "
                      "clearance")
        log0 = len(sim.entries())
        time.sleep(1.0)
        self.assertEqual(sim.lines(log0), [])                    # nothing goes while paused
        self.assertEqual(self.call("/api/touch/pause", {}), (409, {"error": "The touch scan is paused already"}))
        self.assertEqual(self.call("/api/machine/send", {"line": "G0 X1"}), (409, {"error": "A touch scan is running"}))
        self.assertEqual(self.call("/api/touch/resume", {}), (200, {"ok": True, "resumed": True}))
        sc = self.wait_end(ev, k)
        self.assertEqual((sc["phase"], len(sc["result"]["points"])), ("done", 6))
        self.assertEqual(self.call("/api/touch/resume", {}), (409, {"error": "The touch scan is not paused"}))

    def test_3_stop_raises_z_and_leaves_the_auto_probe_alone(self):
        sim, probe = self.stops, self.probe
        ev = self.events()
        self.ready(ev, sim)
        self.assertEqual(self.call("/api/probe/connect", {"port": probe.path}), (200, {"ok": True,
                                                                                     "port": probe.path}))
        self.addCleanup(self.call, "/api/probe/disconnect", {})
        self.ok("/api/probe/home", {})
        plog = len(self.probe_entries())
        # the touch tab's Stop, mid-touch (slowly, so it is caught moving)
        for how in ("/api/touch/stop", "/api/machine/stop", "pause"):
            self.go("G0 X0 Y0 Z0.3", [0, 0, 0.3])
            self.clip(sim)
            log0, k = len(sim.entries()), ev.mark()
            self.assertEqual(self.start(dict(GRID, feed=1)), (200, {"ok": True, "points": 6}))
            if how == "pause":                  # (a Stop while paused raises Z too)
                self.wait_point(ev, k, 1)
                self.ok("/api/touch/pause", {})
                ev.wait(touch_phase("paused"), since=k, timeout=20, what="paused")
                self.ok("/api/touch/stop", {})
            else:
                ev.wait(touch_phase("probing"), since=k, timeout=10, what="the touch")
                self.wait_for(lambda: self.status().get("state") == "Run" and self.status()["wpos"][2] < 0.099,
                              "the bit going down")
                self.ok(how, {})
            sc = self.wait_end(ev, k, 30)
            self.assertEqual((sc["phase"], sc["error"]), ("stopped", f"Touch scan stopped. {TOP}"), how)
            self.assertEqual(sim.lines(log0)[-1], "G20 G53 G0 Z-0.04")
            self.assertAlmostEqual(self.status()["mpos"][2], -0.04, delta=1e-4)
            if how != "pause":
                self.assertEqual([e for e in sim.entries(log0) if e == "RT ^X"].count("RT ^X"), 1)     # (held, then reset, then raised)
        self.assertEqual(self.probe_entries(plog), [])     # never a word to the auto probe

    def probe_entries(self, since=0):
        out = []
        try:
            with open(self.probe.log) as f:
                out = [json.loads(line) for line in f if line.strip()]
        except (FileNotFoundError, ValueError):
            pass
        return [e for e in out[since:] if "cmd" in e or "abort" in e]

    def test_4_no_contact_ends_the_scan(self):
        sim = self.flat
        ev = self.events()
        self.ready(ev, sim)
        self.clip(sim)
        k = ev.mark()
        self.assertEqual(self.start(), (200, {"ok": True, "points": 6}))
        sc = self.wait_end(ev, k)
        self.assertEqual((sc["phase"], sc["error"]),
                         ("failed", "No contact at point 1 (X0.0000 Y0.0000): is the clip on the copper? The bit went "
                                    "down to Z -0.1000 without touching. FluidNC is in ALARM:5 now: Unlock, then "
                                    "raise Z."))
        self.assertEqual(sc["result"]["points"], [])
        self.assertEqual(self.state()["alarm"]["code"], 5)
        self.assertEqual(self.status()["probeCheck"], "none")     # (an alarm clears the check)
        self.wait_for(lambda: self.status().get("state") == "Alarm", "Alarm reported")
        self.ok("/api/machine/unlock", {})


class AutoProbeStopRaisesZ(unittest.TestCase):
    """scan.py: a Stop homes the probe, then raises the CNC's Z (machine.scan_raise), in that order."""

    def test_order_and_message(self):
        import test_scan
        case = test_scan.Stops("test_stop_during_a_move")
        case.setUp()
        try:
            calls = []

            def up():
                calls.append(("raise", list(case.commands())))
                return "Z raised to the top (machine Z -0.04 in)."
            case.machine.scan_raise = up
            case.machine.hold_move = 3
            case.scanner.start(test_scan.SMALL)
            case.wait_for(lambda: case.machine.moves == 3 and case.machine.moving.is_set())
            case.scanner.stop()
            case.scanner.thread.join(5)
            st = case.scanner.state()
            self.assertEqual(st["error"], f"Scan stopped. Probe retracted (homed). {TOP}")
            self.assertEqual(calls[0][1][-2:], ["A", "H"])      # (the probe homed before Z goes up)
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()


class TouchOffZ(SimServer):
    """Touch off Z (touchscan.TouchScanner.touch_off) on the simulator: the bit down on a 0.25 in plate on the stock."""

    def off(self, data=OFF):
        return self.call("/api/touch/off", {"settings": data})

    def wait_off(self, ev, k, timeout=40):
        end, _ = ev.wait(off_phase("done", "failed", "stopped"), since=k, timeout=timeout, what="the touch-off's end")
        return end["touch"]["off"]

    def wco_z(self):
        st = self.status()
        return st["wpos"][2] - st["mpos"][2]        # (work Z - machine Z: the work Z of machine Z 0, in)

    def test_1_refusals_then_z_set_and_undone(self):
        sim = self.plate
        ev = self.events()
        self.ready(ev, sim)
        # a Z zero of the user's own first, 0.2 in off: Undo Zero must get it back after the touch-off
        self.ok("/api/machine/zero", {"axes": "Z", "value": 0.1})
        self.wait_for(lambda: abs(self.status()["wpos"][2] - 0.1) < 1e-4, "Z 0.1")
        before = self.wco_z()
        # refusals: no plate thickness (a first run), no probe check, the spindle on
        self.assertEqual(self.call("/api/touch/off", {}), (400, {"error": touchscan.NO_PLATE}))
        self.assertEqual(self.off(), (409, {"error": NO_CHECK}))
        self.clip(sim)
        self.go("M3 S1000", [0, 0, 0.1])
        self.wait_for(lambda: (self.status().get("spindle") or 0) > 0, "the spindle on", 5)
        self.assertEqual(self.off(), (409, {"error": SPINDLE_ON}))
        self.go("M5", [0, 0, 0.1])
        self.go("G0 Z0.11", [0, 0, 0.11])
        self.go("G0 Z0.1", [0, 0, 0.1])
        self.wait_for(lambda: not self.status().get("spindle"), "the spindle off", 5)
        self.assertEqual(self.state()["touch"]["off"]["settings"], {**OFF, "plateThickness": 0.0, "searchDistance": 1.0,
                                                                     "feed": 2.0})   # (nothing saved yet but the defaults)
        # a G91 left by the user: the touch-off's lines are all G90, and leave G90
        self.go("G91", [0, 0, 0.1])
        time.sleep(0.3)                     # (a report after its ok: FluidNC Idle since)
        log0, k = len(sim.entries()), ev.mark()
        self.assertEqual(self.off(), (200, {"ok": True, "settings": {k: float(v) for k, v in OFF.items()}}))
        self.assertEqual(self.call("/api/machine/send", {"line": "G0 X1"}), (409, {"error": "A touch-off is running"}))
        self.assertEqual(self.off()[1], {"error": "Touch off Z is running"})
        self.assertEqual(self.call("/api/touch/pause", {}), (409, {"error": "No touch scan is running"}))
        off = self.wait_off(ev, k)
        self.assertEqual((off["phase"], off["error"], off["z"]), ("done", None, PLATE), off)
        self.assertEqual(off["message"], "Touch off Z done: Z 0 is the stock's top; the bit is up at Z 0.5000. Take the "
                                         "plate away and the clip off the bit. Undo Zero puts the old Z zero back.")
        self.assertEqual(sim.lines(log0), ["G20 G90 G38.2 Z-0.9 F20", "G10 L20 P0 G20 Z0.25", "G20 G90 G0 Z0.5"])
        # the plate's top reads +0.25, so the stock's top (machine Z -30 mm) is work Z 0
        self.wait_for(lambda: abs(self.status()["wpos"][2] - 0.5) < 1e-4, "Z 0.5")
        self.assertAlmostEqual(self.status()["mpos"][2], (PLATE_TOP / 25.4) + 0.25, delta=1e-4)
        self.assertAlmostEqual(self.wco_z(), 30 / 25.4, delta=1e-4)
        st = self.state()
        self.assertTrue(st["touch"]["clipWarn"])
        self.assertFalse(st["touch"]["running"])                # (the touch scan's own state is left alone)
        self.assertEqual(st["zeroUndo"]["source"], "touch off")
        self.assertEqual(st["zeroUndo"]["axes"], "Z")
        self.assertIn(self.status()["probeCheck"], ("none", "open"))      # (used up)
        self.assertEqual(self.off()[1], {"error": NO_CHECK})
        _, k2 = ev.wait(console("info", start="Set Z to 0.25 in by Touch off Z (the touch plate). Work position "
                                              "before: Z 0.05 (in). Undo Zero on the page puts it back"), since=k)
        # G90 is in force afterwards
        k = ev.mark()
        self.ok("/api/machine/send", {"line": "$G"})
        g, _ = ev.wait(console("rx", start="[GC:"), since=k, what="$G")
        self.assertIn(" G90 ", g["text"])
        # Undo Zero: the user's own Z zero back
        time.sleep(0.3)
        self.ok("/api/machine/zero", {"undo": True})
        self.wait_for(lambda: abs(self.wco_z() - before) < 1e-4, "the old Z zero back")
        self.assertEqual(self.state()["zeroUndo"]["source"], "page")
        # its settings were kept
        self.assertEqual(self.state()["touch"]["off"]["settings"], {k: float(v) for k, v in OFF.items()})
        self.ok("/api/touch/offsettings", {"liftAfter": 0.5})
        self.assertEqual(self.state()["touch"]["off"]["settings"]["liftAfter"], 0.5)
        self.assertEqual(self.call("/api/touch/offsettings", {"feed": 50}),
                         (400, {"error": "Touch off feed: a number from 0.5 to 20 in/min"}))
        self.ok("/api/touch/offsettings", OFF)
        self.disconnect()
        self.assertFalse(self.state()["touch"]["clipWarn"])

    def test_2_no_contact_stops_cleanly(self):
        sim = self.flat
        ev = self.events()
        self.ready(ev, sim)
        self.go("G91", [0, 0, 0.3])
        time.sleep(0.3)
        self.clip(sim)
        log0, k = len(sim.entries()), ev.mark()
        self.assertEqual(self.off(dict(OFF, searchDistance=0.2))[0], 200)
        off = self.wait_off(ev, k)
        self.assertEqual((off["phase"], off["z"]), ("failed", None))
        self.assertEqual(off["error"], "No contact within 0.2 in: is the plate under the bit and the clip on? Z not "
                                       "set. FluidNC is in ALARM:5 now: Unlock, then raise Z (or bring the bit closer "
                                       "to the plate, or make the search distance longer).")
        self.assertEqual(sim.lines(log0), ["G20 G90 G38.2 Z0.1 F20"])     # (nothing more)
        self.assertEqual(self.state()["alarm"]["code"], 5)
        self.wait_for(lambda: self.status().get("state") == "Alarm", "Alarm reported")
        self.ok("/api/machine/unlock", {})
        self.wait_for(lambda: self.status().get("state") == "Idle", "Idle")
        k = ev.mark()
        self.ok("/api/machine/send", {"line": "$G"})
        g, _ = ev.wait(console("rx", start="[GC:"), since=k, what="$G")
        self.assertIn(" G90 ", g["text"])
        self.assertFalse(self.state()["touch"]["clipWarn"])

    def test_3_stop_mid_probe(self):
        sim = self.stops
        ev = self.events()
        self.ready(ev, sim)
        zeros = self.state()["zeroUndo"]
        for how in ("/api/touch/stop", "/api/machine/stop"):
            self.go("G0 X0 Y0 Z0.3", [0, 0, 0.3])
            self.clip(sim)
            log0, k = len(sim.entries()), ev.mark()
            self.assertEqual(self.off(dict(OFF, feed=1))[0], 200)
            self.wait_for(lambda: self.status().get("state") == "Run" and self.status()["wpos"][2] < 0.299,
                          "the bit going down")
            self.ok(how, {})
            off = self.wait_off(ev, k)
            self.assertEqual((off["phase"], off["error"]), ("stopped", f"Touch off Z stopped: Z not set. {TOP}"), how)
            self.assertEqual(sim.lines(log0)[-1], "G20 G53 G0 Z-0.04")
            self.assertNotIn("G10", " ".join(sim.lines(log0)))
            self.assertEqual(self.state()["zeroUndo"], zeros)          # (no zero recorded)

