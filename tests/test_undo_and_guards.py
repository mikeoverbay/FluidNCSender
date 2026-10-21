"""
Unit tests for the stage 3+4 safety fixes in machine.py, pendant.py, scan.py and settings.py:
- Undo Zero keeps the last 5 zeros (page and pendant), puts each one's work offset back with G10 L2 P<its
  coordinate system>, newest first, and keeps them over resets, reconnects and restarts (settings.json);
- the pendant's Home key ($H) acts only in ALARM, its Machine X0/Y0 keys (G53 G0 X0/Y0) not while the bit is
  low, and its Run says which program it started;
- a surface scan starts only with the bit SAFE_Z or more above the work zero.
Protocol-style like test_machine.py (the real Machine, no port, made-up time), the pendant's keys through
PendantLink._action with no port, and scans with test_scan.py's pretend machine and the fake probe on a pty
made there. Real serial ports are never opened.

    python3 -m unittest tests.test_undo_and_guards -v
"""

import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import machine  # noqa: E402
import pendant  # noqa: E402
import settings  # noqa: E402
import test_machine as base  # noqa: E402
import test_scan as scans  # noqa: E402
from machine import MachineError  # noqa: E402

BANNER = "Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"
# work X 0.5 Y 1.937 Z 1 in: MPos -127.3 -50.8 -6.6 mm, WCO -140 -100 -32 mm (G54, no G92 or tool offset)
BEFORE = "<Idle|MPos:-127.300,-50.800,-6.600|FS:0,0|WCO:-140.000,-100.000,-32.000>"
STILL = "<Idle|MPos:-127.300,-50.800,-6.600|FS:0,0>"        # the same place, without WCO (the computed one stays)
HOME_REFUSED = "Pendant: Home all ($H) refused: FluidNC isn't in ALARM. Use Home on the page if you mean it."
LOW = "Z is at {} in (work). Lift Z first (Machine Z0 key)."
NO_RUN = ("Pendant: Run refused: No program loaded: open a G-code file first. The pendant sends Run again only "
          "after its Stop key")


class Base(base.Stage2):
    """The protocol harness, with the settings in a temporary settings.json (Undo Zero's records go there)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "settings.json")
        super().setUp()
        self.m.settings = settings.Settings(self.path)

    def zero(self, axes, value=None, before=STILL, reply="ok", source="page"):
        """A zero sent and answered (a fresh Idle report first). Returns its line."""
        self.reply(before, dt=0.06)
        line = self.m.zero(axes, value, source=source)["line"]
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply(reply)
        return line

    def undo(self, reply="ok"):
        """Undo Zero sent and answered (a fresh Idle report first). Returns its line."""
        self.reply(STILL, dt=0.06)
        line = self.m.zero(undo=True)["line"]
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply(reply)
        return line

    def typed(self, line):
        self.m.send(line)
        self.tick()
        self.assertEqual(self.sent(), [line])
        self.reply("ok")

    def info(self):
        """The page's zeroUndo without its time."""
        got = self.m.snapshot()["zeroUndo"]
        return got and dict(got, t=None)

    def new_link(self, m=None):
        """A new connection to the same FluidNC (as after Disconnect and Connect), on m (FluidSender started
        again) or the same Machine."""
        for fd in (self.link.wake_r, self.link.wake_w):
            os.close(fd)
        if m is not None:
            m.emit = self.events.append
            m.clock = lambda: self.t
            self.m = m
        self.link = machine.Link(self.m, -1, "/dev/fake", 115200)
        self.link.write_realtime = lambda byte: self.rt.append(byte)
        self.m.link = self.link

    def disconnect(self):
        self.link.stopping = ("Disconnected from /dev/fake", "info")
        self.m.link_closed(self.link)


# ------------------------------------------------------------------ Undo Zero (S13)

class UndoZero(Base):
    def test_two_zeros_give_two_undos_newest_first(self):
        self.ready()
        self.assertEqual(self.zero("X", before=BEFORE), "G10 L20 P0 G20 X0")
        self.assertEqual(self.zero("Z"), "G10 L20 P0 G20 Z0")
        self.assertEqual(self.info(), {"axes": "Z", "text": "Z 1", "wcs": "G54", "source": "page", "t": None,
                                       "count": 2})
        self.assertEqual([(r["axes"], r["wco"]) for r in self.m.zeros],
                         [("X", [-140.0, -100.0, -32.0]), ("Z", [-127.3, -100.0, -32.0])])   # (the offsets before)
        self.assertEqual(self.undo(), "G10 L2 P1 G20 Z-1.259843")              # Z: -32 mm, as before its zero
        self.assertEqual(self.log("info")[-1], "Zero undone: the G54 work offset of Z is back to what it was before "
                                               "that zero. Work position now: Z 1 (in). 1 older zero can still be "
                                               "undone")
        self.assertEqual(self.evs("status")[-1]["wpos"], [0.0, 1.937008, 1.0])    # shown at once
        self.assertEqual(self.info(), {"axes": "X", "text": "X 0.5", "wcs": "G54", "source": "page", "t": None,
                                       "count": 1})
        self.assertEqual(self.evs("zero")[-1]["zeroUndo"]["axes"], "X")
        self.assertEqual(self.undo(), "G10 L2 P1 G20 X-5.511811")              # X: -140 mm
        self.assertEqual(self.log("info")[-1], "Zero undone: the G54 work offset of X is back to what it was before "
                                               "that zero. Work position now: X 0.5 (in)")
        self.assertEqual(self.evs("status")[-1]["wpos"], [0.5, 1.937008, 1.0])
        self.assertEqual(self.evs("zero")[-1], {"type": "zero", "zeroUndo": None})
        self.refused(lambda: self.m.zero(undo=True), "No zero to undo")

    def test_the_last_five_are_kept(self):
        self.ready()
        self.reply(BEFORE, dt=0.06)
        for axes in ("X", "Y", "Z", "XY", "XYZ", "Y"):
            self.zero(axes)
        self.assertEqual([r["axes"] for r in self.m.zeros], ["Y", "Z", "XY", "XYZ", "Y"])     # (the first X: gone)
        self.assertEqual(self.m.zero_info()["count"], 5)
        self.assertEqual(len(self.m.settings.get("zeros")), 5)

    def test_kept_over_a_reset(self):
        """FluidNC keeps its work offsets over a reset (Soft Reset, our safe stop, its own): so does Undo Zero."""
        self.ready()
        self.zero("X", before=BEFORE)
        n = len(self.evs("zero"))
        self.reply(BANNER)
        self.assertEqual(len(self.evs("zero")), n)          # (nothing said: nothing changed)
        self.assertEqual(self.info()["axes"], "X")
        self.reply(base.Harness.IDLE, dt=0.06)              # (Idle a moment after the banner: lines may go)
        self.inches()                                       # (G20 again after the reset)
        self.assertEqual(self.undo(), "G10 L2 P1 G20 X-5.511811")
        self.assertIsNone(self.info())

    def test_kept_over_a_reset_of_fluidnc_411(self):
        """The same with the user's FluidNC 4.1.1 (its banner, $I answers): it keeps the work offsets over a reset
        too (NVS), so does Undo Zero."""
        self.ready(fw="4.1.1")
        self.zero("X", before=BEFORE)
        self.reply("Grbl 4.1 [FluidNC v4.1.1 (esp32-noradio) '$' for help]")
        self.assertIn("*** Controller reset", self.log("info"))
        self.assertEqual(self.info()["axes"], "X")
        self.reply(base.Harness.IDLE, dt=0.06)
        self.inches()
        self.assertEqual(self.undo(), "G10 L2 P1 G20 X-5.511811")
        self.assertIsNone(self.info())

    def test_a_zero_in_g59_1_is_undone_there(self):
        """FluidNC 4.1.1 has G59.1-G59.3 (G10 P7-P9): Undo Zero must put the offset back in that system, not in
        G54."""
        self.ready(fw="4.1.1")
        self.typed("G59.1")
        self.zero("X", before=BEFORE)
        self.assertEqual(self.info(), {"axes": "X", "text": "X 0.5", "wcs": "G59.1", "source": "page", "t": None,
                                       "count": 1})
        self.typed("G54")                               # (another system active now: the position doesn't change)
        self.assertEqual(self.undo(), "G10 L2 P7 G20 X-5.511811")
        self.assertEqual(self.log("info")[-1], "Zero undone: the G59.1 work offset of X is back to what it was before "
                                               "that zero (G59.1 isn't the active one: the position shown doesn't "
                                               "change)")

    def test_kept_over_a_reconnect(self):
        self.ready()
        self.zero("X", before=BEFORE)
        self.disconnect()
        self.assertEqual(self.m.snapshot()["connected"], False)
        self.assertEqual(self.info()["text"], "X 0.5")      # (the page shows it only while connected)
        self.refused(lambda: self.m.zero(undo=True), "Not connected")
        self.new_link()
        self.events.clear()
        self.go_live()
        self.assertEqual([e["zeroUndo"]["text"] for e in self.evs("zero")], ["X 0.5"])    # (the page forgot it)
        self.inches()
        self.assertEqual(self.undo(), "G10 L2 P1 G20 X-5.511811")
        self.assertIsNone(self.info())

    def test_kept_in_settings_json_and_after_a_restart(self):
        self.ready()
        self.zero("X", before=BEFORE)
        with open(self.path) as f:
            saved = json.load(f)["zeros"]
        self.assertEqual([(r["axes"], r["wcs"], r["wco"], r["value"], r["source"], r["text"]) for r in saved],
                         [("X", 1, [-140.0, -100.0, -32.0], 0.0, "page", "X 0.5")])
        self.assertAlmostEqual(saved[0]["t"], time.time(), delta=5)
        self.disconnect()
        again = machine.Machine(settings=settings.Settings(self.path))     # FluidSender started again
        self.assertEqual(again.zeros, saved)
        self.assertEqual(dict(again.snapshot()["zeroUndo"], t=None),
                         {"axes": "X", "text": "X 0.5", "wcs": "G54", "source": "page", "t": None, "count": 1})
        self.new_link(again)
        self.go_live()
        self.inches()
        self.assertEqual(self.undo(), "G10 L2 P1 G20 X-5.511811")
        with open(self.path) as f:
            self.assertEqual(json.load(f)["zeros"], [])

    def test_in_millimeters_and_wherever_the_machine_is(self):
        """G10 L2 sets the offset itself: it doesn't count from where the machine is now (it moved since)."""
        self.ready()
        self.zero("Y", before=BEFORE)
        self.typed("G21")
        self.reply("<Idle|MPos:-10.000,-20.000,-6.600|FS:0,0>", dt=0.06)
        self.assertEqual(self.m.zero(undo=True)["line"], "G10 L2 P1 G21 Y-100")

    def test_an_undo_refused_by_fluidnc_keeps_the_record(self):
        self.ready()
        self.zero("X", before=BEFORE)
        self.undo(reply="error:9")
        self.assertEqual(self.info()["axes"], "X")
        self.assertEqual(self.undo(), "G10 L2 P1 G20 X-5.511811")

    def test_a_zero_refused_by_fluidnc_is_not_kept(self):
        self.ready()
        self.zero("X", before=BEFORE, reply="error:9")
        self.assertIsNone(self.info())
        self.assertFalse(os.path.exists(self.path))         # (nothing changed: nothing written)

    def test_from_the_pendant(self):
        self.ready()
        self.zero("Z", before=BEFORE, source="pendant")
        self.assertEqual(self.log("info")[-1], "Zeroed Z from the pendant. Work position before: Z 1 (in). Undo "
                                               "Zero on the page puts it back")
        self.assertEqual(self.info()["source"], "pendant")
        self.zero("X", source="anything else")                  # (the page's, as the server calls it)
        self.assertEqual(self.info()["source"], "page")
        self.assertEqual(self.log("info")[-1], "Zeroed X. Work position before: X 0.5 (in)")


class WorkCoordinateSystems(Base):
    """A zero (G10 L20 P0) changes the active system's offsets: its record says which one, and Undo Zero puts
    that one back (G10 L2 P<n>) whichever is active then."""

    def test_the_lines_say_which(self):
        self.ready()
        self.assertEqual(self.link.active_wcs(), 1)         # ($G said G54)
        self.typed("G55 G0 X0")
        self.assertEqual(self.link.active_wcs(), 2)
        self.typed("G1 X1 F10")                             # (no word for it: still G55)
        self.assertEqual(self.link.active_wcs(), 2)
        self.typed("M30")                                   # program end: back to G54
        self.assertEqual(self.link.active_wcs(), 1)
        self.typed("G59")
        self.reply(BANNER)                                  # a reset: G54
        self.assertEqual(self.link.active_wcs(), 1)
        self.link.wcs = None                                # (connected: $G's word, not its program-end M30)
        self.link.set_modal("G20", "G90", "G1 G56 G17 G20 G90 G94 M30 M5 M9 T0 F0 S0")
        self.assertEqual(self.link.active_wcs(), 3)

    def test_a_job_line_says_it_too(self):
        self.ready()
        self.m.load_program("t.nc", "G57\nG0 X0\n")
        self.m.run()
        self.tick(0.01)
        for _ in self.sent():                               # (however many lines go first)
            self.reply("ok")
        self.reply(base.Harness.IDLE, dt=0.06)
        self.assertEqual(self.link.active_wcs(), 4)

    def test_undo_puts_back_the_system_it_zeroed(self):
        self.ready()
        self.typed("G55")
        self.zero("X", before=BEFORE)
        self.assertEqual((self.m.zeros[-1]["wcs"], self.info()["wcs"]), (2, "G55"))
        self.typed("G54")
        shown = self.evs("status")[-1]["wpos"]
        self.assertEqual(self.undo(), "G10 L2 P2 G20 X-5.511811")
        self.assertEqual(self.log("info")[-1], "Zero undone: the G55 work offset of X is back to what it was before "
                                               "that zero (G55 isn't the active one: the position shown doesn't "
                                               "change)")
        self.assertEqual(self.evs("status")[-1]["wpos"], shown)

    def test_words(self):
        for line, wcs in (("G54", 1), ("g55 g0 x1", 2), ("G0 X1 (G56)", None), ("G10 L20 P2 X0", None),
                          ("G10 L2 P3 X0", None), ("G59 M30", 1), ("M2", 1), ("G054", 1), ("G54.1", None),
                          ("$J=G55 X1 F10", None), ("G58 G0 X1 ; G54", 5), ("", None)):
            self.assertEqual(machine.wcs_word(line), wcs, line)
        self.assertEqual(machine.gc_wcs("G0 G59 G17 G21 G90 G94 M2 M5 M9 T0 F0 S0"), 6)
        self.assertIsNone(machine.gc_wcs(None))


class ZeroRecordsInTheFile(unittest.TestCase):
    GOOD = {"axes": "XZ", "wcs": 1, "wco": [-140.0, 0.0, -32.0], "value": 0.0, "t": 1760000000.5, "source": "page",
            "text": "X 0.5 Z 1"}

    def test_checked_one_by_one(self):
        bad = [dict(self.GOOD, axes="ZX"), dict(self.GOOD, axes="XX"), dict(self.GOOD, axes=""),
               dict(self.GOOD, axes="A"), dict(self.GOOD, wcs=7), dict(self.GOOD, wcs=True), dict(self.GOOD, wcs=1.5),
               dict(self.GOOD, wco=[1, 2]), dict(self.GOOD, wco=[0, 0, float("nan")]), dict(self.GOOD, wco=[0, 0, 1e9]),
               dict(self.GOOD, value=101), dict(self.GOOD, t=-1), dict(self.GOOD, t="now"),
               dict(self.GOOD, source="ugs"), dict(self.GOOD, text=None), "X", None, 5]
        good = dict(self.GOOD, wco=[-140, 0, -32], source="pendant")
        self.assertEqual(settings.check_zeros(bad + [good]), [dict(good, wco=[-140.0, 0.0, -32.0])])
        self.assertEqual(settings.check_zeros([dict(self.GOOD, text=str(n)) for n in range(7)]),
                         [dict(self.GOOD, text=str(n)) for n in range(2, 7)])        # the newest 5
        with self.assertRaises(settings.SettingsError):
            settings.check_zeros({"axes": "X"})

    def test_a_damaged_file_keeps_the_good_ones(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump({"zeros": [self.GOOD, {"axes": "X"}]}, f)
            self.assertEqual(settings.Settings(path).get("zeros"), [self.GOOD])
            with open(path, "w") as f:
                json.dump({"zeros": "nothing"}, f)
            self.assertEqual(settings.Settings(path).get("zeros"), [])
        self.assertEqual(settings.Settings(None).get("zeros"), [])


# ------------------------------------------------------------------ the pendant's keys (S4b, S7, S8)

class PendantKeys(Base):
    """The keys through PendantLink._action against the real Machine (no port: the key's Machine calls only)."""

    def setUp(self):
        super().setUp()
        self.p = pendant.PendantLink(self.m, None)

    def key(self, n):
        ok, text = self.p._action(n)
        self.tick(0.01)
        return ok, text, self.sent()

    def test_home_only_in_alarm(self):
        self.ready()
        self.assertEqual(self.key(23), (False, HOME_REFUSED, []))
        self.reply("<Run|MPos:0,0,0|FS:100,0>")
        self.assertEqual(self.key(23), (False, HOME_REFUSED, []))
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>")
        self.assertEqual(self.key(23), (True, "Pendant: Home all ($H)", ["$H"]))

    def test_home_refused_for_the_machines_own_reasons_too(self):
        self.ready()
        self.t += 3                                         # no report for 3 s: "No response"
        self.assertEqual(self.key(23), (False, "Pendant: Home all ($H) refused: No response from FluidNC", []))
        self.m.link = None
        self.assertEqual(self.key(23), (False, "Pendant: Home all ($H) refused: Not connected", []))

    def test_machine_x0_and_y0_need_the_bit_up(self):
        self.ready()                                        # (work Z 0)
        for k, name in ((19, "Machine X0 (G53 G0 X0)"), (18, "Machine Y0 (G53 G0 Y0)")):
            self.assertEqual(self.key(k), (False, f"Pendant: {name} refused: {LOW.format('0.0000')}", []))
        self.reply("<Idle|MPos:0,0,5.077|FS:0,0>", dt=0.06)    # Z 0.1999 in
        self.assertEqual(self.key(19)[1], f"Pendant: Machine X0 (G53 G0 X0) refused: {LOW.format('0.1999')}")
        self.reply("<Idle|MPos:0,0,-12.7|FS:0,0>", dt=0.06)
        self.assertEqual(self.key(18)[1], f"Pendant: Machine Y0 (G53 G0 Y0) refused: {LOW.format('-0.5000')}")
        self.reply("<Idle|MPos:0,0,5.080|FS:0,0>", dt=0.06)    # Z 0.2 in: clear
        self.assertEqual(self.key(19), (True, "Pendant: Machine X0 (G53 G0 X0)", ["G53 G0 X0"]))
        self.reply("ok", "<Idle|MPos:0,0,5.080|FS:0,0>", dt=0.06)
        self.assertEqual(self.key(18), (True, "Pendant: Machine Y0 (G53 G0 Y0)", ["G53 G0 Y0"]))

    def test_machine_z0_and_return_to_zero_go_as_before(self):
        self.ready()                                        # (work Z 0: Machine Z0 is the way up)
        self.assertEqual(self.key(17), (True, "Pendant: Machine Z0 (G53 G0 Z0)", ["G53 G0 Z0"]))
        self.reply("ok", base.Harness.IDLE, dt=0.06)
        ok, text, sent = self.key(22)
        self.assertEqual((ok, sent), (True, ["G20 G90 G0 Z0.2"]))   # (Return to zero lifts first)

    def test_machine_x0_in_another_state_says_the_machines_reason(self):
        self.ready()
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>")
        self.assertEqual(self.key(19), (False, "Pendant: Machine X0 (G53 G0 X0) refused: ALARM: Unlock or Home "
                                               "first", []))

    def test_run_names_the_program(self):
        self.ready()
        self.assertEqual(self.key(25), (False, NO_RUN, []))
        self.m.load_program("job.nc", "G0 X1\nG0 X2\n(comment)\nG0 X0\n")
        ok, text, sent = self.key(25)
        self.assertEqual((ok, text), (True, "Pendant: Run job.nc (3 lines)"))
        self.assertEqual(self.m.job.state, "running")
        self.assertTrue(sent)

    def test_zero_keys_are_kept_for_undo(self):
        self.ready()
        self.reply(BEFORE, dt=0.06)
        self.assertEqual(self.key(13), (True, "Pendant: Zero Z", ["G10 L20 P0 G20 Z0"]))
        self.reply("ok")
        self.assertEqual(self.log("info")[-1], "Zeroed Z from the pendant. Work position before: Z 1 (in). Undo "
                                               "Zero on the page puts it back")
        self.assertEqual((self.info()["axes"], self.info()["source"]), ("Z", "pendant"))
        self.assertEqual(self.undo(), "G10 L2 P1 G20 Z-1.259843")

    def test_status_now(self):
        self.ready()
        st = self.m.status_now()
        self.assertEqual((st["state"], st["wpos"]), ("Idle", [0.0, 0.0, 0.0]))
        st["state"] = "changed"                             # (a copy)
        self.assertEqual(self.m.status_now()["state"], "Idle")
        self.t += 3
        self.assertEqual(self.m.status_now()["state"], "No response")
        self.m.link = None
        self.assertIsNone(self.m.status_now())


# ------------------------------------------------------------------ the scan's Z (S3)

class ScanNeedsTheBitUp(scans.ScanCase):
    def refused(self, z, why):
        self.machine.wpos = None if z is None else [0.0, 0.0, z]
        with self.assertRaises(MachineError) as cm:
            self.scanner.start(scans.SMALL)
        self.assertEqual((cm.exception.status, str(cm.exception)), (409, why))
        self.assertEqual(self.machine.calls[-2:], [("begin", "scan"), ("end", "scan")])     # (given back at once)
        self.assertIsNone(self.machine.activity)
        self.assertIsNone(self.probe.owner)
        self.assertTrue(self.probe.state()["ready"])        # (the probe untouched: no need to home it again)
        self.assertIsNone(self.scanner.thread)
        self.assertFalse(self.scanner.state()["running"])

    def test_refused_while_low(self):
        for z, shown in ((0.0, "0.0000"), (-0.00001, "0.0000"), (0.1999, "0.1999"), (-1.25, "-1.2500")):
            self.refused(z, f"Z is at {shown} in (work): raise Z so the bit clears the work (0.2 in or more above "
                            "work zero), then Start Scan.")
        self.refused(None, "Work position not known yet; try again in a second")
        self.assertEqual(self.machine.move_list(), [])

    def test_starts_at_safe_z(self):
        self.machine.wpos = [0.0, 0.0, 0.2]
        st = self.run_scan()
        self.assertEqual((st["phase"], st["point"]), ("done", 6))


if __name__ == "__main__":
    unittest.main()
