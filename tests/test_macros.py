"""
Unit tests for the macros (Motion control -> Macros): settings.py's check (the defaults when the file has none, the
limits, the lines refused) and machine.py's set_macros / run_macro (lines one at a time like typed ones, in order;
refused unless Idle with nothing else going; a refused line, an alarm, Feed Hold, Stop and Soft Reset drop the rest).
Protocol-style like test_jobs.py: no port, made-up time. (The server's calls on the simulator: test_e2e.py.)

    python3 -m unittest tests.test_macros
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import machine  # noqa: E402
import settings  # noqa: E402
import test_jobs as jobs  # noqa: E402

IDLE = jobs.IDLE
DEFAULTS = [{"name": "Park X axis", "code": "G53 G0 X0"}, {"name": "Park Y axis", "code": "G53 G0 Y0"},
            {"name": "Park Z axis", "code": "G53 G0 Z0"}]
PARK = {"name": "Park all", "code": "G53 G0 Z0\nG53 G0 X0 Y0"}


class MacroSettings(unittest.TestCase):
    def test_defaults_when_absent(self):
        self.assertEqual(settings.Settings(None).get("macros"), DEFAULTS)
        self.assertNotIn("$H", json.dumps(settings.MACROS))     # (parking is a G53 rapid, never a homing cycle)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump({"jog": {"step": 1, "feed": 100}}, f)
            self.assertEqual(settings.Settings(path).get("macros"), DEFAULTS)
            with open(path, "w") as f:
                json.dump({"macros": "damaged"}, f)
            self.assertEqual(settings.Settings(path).get("macros"), DEFAULTS)
            s = settings.Settings(path)
            s.set("macros", [PARK])
            self.assertEqual(settings.Settings(path).get("macros"), [PARK])
            s.set("macros", [])                         # (all deleted: none, not the defaults again)
            self.assertEqual(settings.Settings(path).get("macros"), [])

    def test_tidied(self):
        messy = {"name": "  Park   all ", "code": "  G53 G0 Z0 \r\n\n\n G53 G0 X0 Y0\n"}
        self.assertEqual(settings.check_macros([messy]), [PARK])

    def test_limits_and_refusals(self):
        ok = {"name": "a", "code": "G0 X1"}
        for value, why in (
                ("x", "Macros: a list of macros"),
                ([ok] * 51, "Macros: 51 of them; at most 50"),
                ([5], "Macro 1: give its name and code"),
                ([{"name": "", "code": "G0"}], "Macro 1: a name of 1 to 40 characters"),
                ([{"name": "   ", "code": "G0"}], "Macro 1: a name of 1 to 40 characters"),
                ([ok, {"name": "x" * 41, "code": "G0"}], "Macro 2: a name of 1 to 40 characters"),
                ([{"name": "a", "code": None}], 'Macro "a": up to 4000 characters of G-code'),
                ([{"name": "a", "code": "G0\n" * 1334}], 'Macro "a": up to 4000 characters of G-code'),
                ([{"name": "a", "code": " \n\n "}], 'Macro "a": no G-code in it'),
                ([ok, {"name": "A", "code": "G0"}], 'Two macros are named "A": give each its own name'),
                ([{"name": "a", "code": "G0 X1\nM3 S100!"}],
                 'Macro "a", line 2 (M3 S100!): Line not sent: FluidNC takes \'!\' as a realtime command wherever it '
                 'appears (? status, ! feed hold, ~ resume). Use the buttons for those.'),
                ([{"name": "a", "code": "G0 X" + "1" * 130}],
                 f'Macro "a", line 1 (G0 X{"1" * 36}): Line too long for FluidNC (134 characters; it refuses G-code '
                 'lines over 127, spaces and comments included)'),
                ([{"name": "a", "code": "G0 X1\x18"}],
                 'Macro "a", line 1 (G0 X1\x18): Line not sent: it contains the control or non-ASCII character 0x18, '
                 'which FluidNC could take as a realtime command')):
            with self.assertRaises(settings.SettingsError) as cm:
                settings.check_macros(value)
            self.assertEqual(str(cm.exception), why)
        self.assertEqual(len(settings.check_macros([dict(ok, name=f"m{i}") for i in range(50)])), 50)
        self.assertEqual(settings.check_macros([{"name": "x" * 40, "code": "G0 X1\n" * 666}])[0]["code"],
                         "\n".join(["G0 X1"] * 666))


class Macros(jobs.Stage3):
    def run_all(self, name):
        """Runs the macro, answering each line ok as it goes; returns the lines sent."""
        out = self.m.run_macro(name)
        sent = []
        for _ in out["lines"]:
            self.tick(0.01)
            got = self.sent()
            self.assertEqual(len(got), 1)               # one at a time, like typed lines
            sent += got
            self.reply("ok")
        self.reply(IDLE, dt=0.06)
        return sent

    def test_set_and_shown(self):
        self.assertEqual(self.m.macros(), DEFAULTS)
        self.assertEqual(self.m.snapshot()["macros"], DEFAULTS)
        self.assertIsNone(self.m.snapshot()["macroRun"])
        out = self.m.set_macros({"macros": DEFAULTS + [PARK]})
        self.assertEqual(out, {"macros": DEFAULTS + [PARK]})
        self.assertEqual(self.evs("macros"), [{"type": "macros", "macros": DEFAULTS + [PARK]}])
        self.m.set_macros(DEFAULTS + [PARK])            # unchanged: no event
        self.assertEqual(len(self.evs("macros")), 1)
        self.refused(lambda: self.m.set_macros([{"name": "a", "code": "G0 X1?"}]),
                     "Macro \"a\", line 1 (G0 X1?): Line not sent: FluidNC takes '?' as a realtime command wherever "
                     "it appears (? status, ! feed hold, ~ resume). Use the buttons for those.", 400)
        self.assertEqual(self.m.macros(), DEFAULTS + [PARK])    # (works while disconnected)

    def test_runs_in_order(self):
        self.ready()
        self.m.set_macros(DEFAULTS + [PARK])
        self.assertEqual(self.run_all("Park Z axis"), ["G53 G0 Z0"])
        self.assertEqual(self.run_all("park all"), ["G53 G0 Z0", "G53 G0 X0 Y0"])     # (any case)
        self.assertEqual(self.m.run_macro({"index": 0})["name"], "Park X axis")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G53 G0 X0"])
        self.reply("ok", IDLE)
        self.assertEqual(self.log("tx")[-4:], ["G53 G0 Z0", "G53 G0 Z0", "G53 G0 X0 Y0", "G53 G0 X0"])
        info = [t for t in self.log("info") if "acro" in t]
        self.assertEqual(info, ["Running macro Park Z axis: line 1 of 1", "Macro Park Z axis done",
                                "Running macro Park all: line 1 of 2", "Running macro Park all: line 2 of 2",
                                "Macro Park all done", "Running macro Park X axis: line 1 of 1",
                                "Macro Park X axis done"])
        runs = [e["macro"] for e in self.evs("macro")]
        park = {"name": "Park all", "total": 2}
        self.assertEqual(runs[3:7], [dict(park, line=0), dict(park, line=1), dict(park, line=2), None])
        self.assertIsNone(self.link.macro)

    def test_snapshot_while_running(self):
        self.ready()
        self.m.set_macros([PARK])
        self.m.run_macro("Park all")
        self.tick(0.01)
        self.sent()
        self.assertEqual(self.m.snapshot()["macroRun"], {"name": "Park all", "line": 1, "total": 2})
        self.refused(lambda: self.m.run_macro("Park all"), "Macro Park all is running")

    def test_refused(self):
        self.refused(lambda: self.m.run_macro("Park Z axis"), "Not connected")
        self.ready()
        self.refused(lambda: self.m.run_macro("Nothing"), 'There is no macro named "Nothing"', 400)
        for bad in (None, {"index": 3}, {"index": -1}, {"index": True}, {"index": "0"}):
            self.refused(lambda: self.m.run_macro(bad), "Run which macro? Give its name", 400)
        for report, why in (("<Alarm|MPos:0,0,0|FS:0,0>", "ALARM: Unlock or Home first"),
                            ("<Run|MPos:0,0,0|FS:100,0>", "Running: wait until FluidNC is Idle"),
                            (jobs.HOLD0, "Hold: Resume or Soft Reset first")):
            self.reply(report)
            self.refused(lambda: self.m.run_macro("Park Z axis"), why)
        self.reply(IDLE, dt=0.06)
        self.m.send("G4 P1")
        self.refused(lambda: self.m.run_macro("Park Z axis"), "Waiting for FluidNC to finish 'G4 P1'")
        self.tick()
        self.sent()
        self.reply("ok", IDLE)
        self.reply(IDLE, dt=0.06)
        self.running(x=1)
        self.refused(lambda: self.m.run_macro("Park Z axis"), jobs.base.JOGGING)
        self.m.jog({"stop": "all"})
        self.stopped()
        self.start_job(jobs.LONG)
        self.refused(lambda: self.m.run_macro("Park Z axis"), jobs.JOB_RUNNING)

    def test_refused_while_scanning(self):
        self.ready()
        self.m.activity = "scan"
        self.refused(lambda: self.m.run_macro("Park Z axis"), jobs.SCANNING)
        self.m.activity = machine.TOUCH
        self.refused(lambda: self.m.run_macro("Park Z axis"), "A touch scan is running")

    def test_a_refused_line_stops_the_rest(self):
        self.ready()
        self.m.set_macros([{"name": "Three", "code": "G0 X1\nG0 X2\nG0 X3"}])
        self.m.run_macro("Three")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G0 X1"])
        self.reply("ok")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G0 X2"])
        self.reply("error:15")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # the rest doesn't go
        self.assertEqual(list(self.link.queue), [])
        self.assertEqual(self.log("error")[-1], "Macro Three stopped: G0 X2 was refused: error:15 (jog target is "
                                                "beyond the travel); 1 line of it not sent")
        self.assertIsNone(self.link.macro)
        self.assertIsNone(self.evs("macro")[-1]["macro"])

    def test_an_alarm_stops_the_rest(self):
        self.ready()
        self.m.set_macros([PARK])
        self.m.run_macro("Park all")
        self.tick(0.01)
        self.sent()
        self.reply("ALARM:2")
        self.reply("error:9")                           # (FluidNC's answer to the line: nothing follows it)
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.assertEqual(list(self.link.queue), [])
        self.assertIn("Macro Park all stopped: ALARM:2; 1 line of it not sent", self.log("error"))

    def test_hold_stops_the_rest(self):
        self.ready()
        self.m.set_macros([PARK])
        self.m.run_macro("Park all")
        self.tick(0.01)
        self.sent()
        self.reply(jobs.RUN)
        self.m.realtime("hold")
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual(list(self.link.queue), [])
        self.assertEqual(self.log("error")[-1], "Macro Park all stopped: Feed Hold; 1 line of it not sent")
        self.reply("ok")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])

    def test_stop_drops_the_rest(self):
        self.ready()
        self.m.set_macros([PARK])
        self.m.run_macro("Park all")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G53 G0 Z0"])
        self.reply(jobs.RUN)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"!")         # the safe stop: hold first, then the reset
        self.assertEqual(list(self.link.queue), [])
        self.assertEqual(self.log("error")[-1], "Macro Park all stopped: Stopped by the user; 1 line of it not sent")
        self.reply("ok")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.reply(jobs.HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"\x18")
        self.assertIsNone(self.link.macro)

    def test_stop_before_a_report_shows_it_moving(self):
        self.ready()
        self.m.set_macros([PARK])
        self.m.run_macro("Park all")
        self.tick(0.01)
        self.sent()
        self.assertEqual(self.m.stop(), {"stopped": "motion"})   # (its line is in flight: it may move)
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual(list(self.link.queue), [])

    def test_soft_reset_drops_the_rest(self):
        self.ready()
        self.m.set_macros([PARK])
        self.m.run_macro("Park all")
        self.tick(0.01)
        self.sent()
        self.m.realtime("reset")
        self.assertEqual(self.realtime(), b"\x18")
        self.assertEqual(list(self.link.queue), [])
        self.assertEqual(self.log("error")[-1], "Macro Park all stopped: Soft Reset; 1 line of it not sent")
        self.assertIsNone(self.link.macro)

    def test_kept_in_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with mock.patch.dict(os.environ, {"FLUIDSENDER_SETTINGS": path}):
                m = machine.Machine(settings=settings.Settings())
                m.emit = lambda ev: None
                m.set_macros([PARK])
                with open(path) as f:
                    self.assertEqual(json.load(f)["macros"], [PARK])
                self.assertEqual(machine.Machine(settings=settings.Settings()).macros(), [PARK])


if __name__ == "__main__":
    unittest.main()
