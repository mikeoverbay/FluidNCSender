"""
Unit tests for machine.py: report parsing, units, line checks, the line protocol (settings
reads, jogs, zero, home, unlock, alarms, a line FluidNC 3.9.4 runs after a reset, connecting while it
holds; no port, made-up time), port descriptions and holder detection. Only pseudo terminals made
here are opened; real serial ports never are.

    python3 -m unittest discover -s tests
"""

import os
import subprocess
import sys
import tempfile
import time
import tty
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import machine  # noqa: E402
from machine import MachineError, StatusParser, check_line, is_banner, modal_words, parse_gc, parse_status, parse_ver  # noqa: E402


class StatusReports(unittest.TestCase):
    def test_simple(self):
        p = parse_status("<Idle|MPos:1.000,2.000,3.000|FS:0,0>")
        self.assertEqual(p, {"state": "Idle", "sub": None, "MPos": [1.0, 2.0, 3.0], "feed": 0.0, "spindle": 0.0})

    def test_sub_state_pins_and_wco(self):
        p = parse_status("<Hold:0|MPos:-1.5,0,2|FS:500,12000|Pn:XZP|WCO:1,2,3>")
        self.assertEqual((p["state"], p["sub"]), ("Hold", "0"))
        self.assertEqual(p["pins"], "XZP")
        self.assertEqual(p["WCO"], [1.0, 2.0, 3.0])
        self.assertEqual((p["feed"], p["spindle"]), (500.0, 12000.0))

    def test_all_optional_fields(self):
        p = parse_status("<Run|WPos:1,2,3|Bf:15,128|Ln:42|F:300|Ov:120,100,50|A:SFM>")
        self.assertEqual(p["WPos"], [1.0, 2.0, 3.0])
        self.assertEqual(p["buf"], [15, 128])
        self.assertEqual(p["line"], 42)
        self.assertEqual(p["feed"], 300.0)
        self.assertNotIn("spindle", p)
        self.assertEqual(p["ov"], [120, 100, 50])

    def test_not_reports(self):
        for line in ("ok", "[MSG:INFO: <x>]", "<>", "<Idle>", "<Idle|MPos:abc>", "<1dle|MPos:1,2,3>", "Idle|MPos:1,2,3"):
            self.assertIsNone(parse_status(line), line)

    def test_damaged_field_is_skipped(self):
        p = parse_status("<Jog|MPos:1,2,3|FS:x,y|Pn:Z>")
        self.assertEqual((p["state"], p["pins"]), ("Jog", "Z"))
        self.assertNotIn("feed", p)

    def test_noise_numbers_are_damage(self):
        """nan, inf and 1e999 (line noise) are not numbers: they must not crash anything or reach the page."""
        for field in ("FS:0,nan", "FS:inf,0", "FS:0,1e999", "Ov:1e999,100,100", "Ov:nan,100,100", "F:inf",
                      "Bf:nan,1", "Ln:1e9"):
            p = parse_status(f"<Idle|MPos:0.000,0.000,0.000|{field}>")
            self.assertEqual(p["MPos"], [0.0, 0.0, 0.0], field)
            self.assertFalse({"feed", "spindle", "ov", "buf", "line"} & set(p), field)
        for bad in ("<Idle|MPos:nan,0,0|FS:0,0>", "<Idle|WPos:0,inf,0|FS:0,0>"):
            self.assertIsNone(parse_status(bad), bad)
        sp = StatusParser()
        sp.feed("<Idle|MPos:0,0,0|FS:0,0|WCO:1,2,3>")
        st = sp.feed("<Idle|MPos:0,0,0|FS:0,0|WCO:inf,0,0>")         # the good WCO is kept
        self.assertEqual(st["wpos"], [round(-1 / 25.4, 6), round(-2 / 25.4, 6), round(-3 / 25.4, 6)])
        self.assertEqual(machine.encode({"a": float("nan"), "b": [1.5, float("inf")]}), b'{"a":null,"b":[1.5,null]}\n')

    def test_reports_in_inches(self):
        """report_inches on: FluidNC prints 4 decimals and inches per minute, which must not be divided again."""
        p = parse_status("<Run|MPos:1.0000,-2.0000,0.5000|FS:10.0,1000|WCO:0.5000,0.0000,0.0000>")
        self.assertTrue(p["inches"])
        self.assertNear(p["MPos"], [25.4, -50.8, 12.7])
        self.assertAlmostEqual(p["feed"], 254.0)
        sp = StatusParser()
        st = sp.feed("<Run|MPos:1.0000,-2.0000,0.5000|FS:10.0,1000|WCO:0.5000,0.0000,0.0000>")
        self.assertTrue(sp.inches)
        self.assertEqual((st["wpos"], st["mpos"], st["feed"]), ([0.5, -2.0, 0.5], [1.0, -2.0, 0.5], 10.0))
        st = sp.feed("<Idle|MPos:25.400,0.000,0.000|FS:0,0>")         # millimeters again (3 decimals)
        self.assertFalse(sp.inches)
        self.assertEqual(st["mpos"], [1.0, 0.0, 0.0])
        self.assertNotIn("inches", parse_status("<Idle|MPos:1.000,2.000,3.000|FS:0,0>"))

    def assertNear(self, got, want):
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, places=6)


class StatusInInches(unittest.TestCase):
    def test_mm_to_inches_and_wco_is_remembered(self):
        sp = StatusParser()
        st = sp.feed("<Idle|MPos:25.400,50.800,-12.700|FS:254,10000>")
        self.assertIsNone(st["wpos"])                       # no work offset seen yet
        self.assertEqual(st["mpos"], [1.0, 2.0, -0.5])
        self.assertEqual(st["feed"], 10.0)                  # 254 mm/min = 10 in/min
        self.assertEqual(st["spindle"], 10000)
        st = sp.feed("<Idle|MPos:25.400,50.800,-12.700|FS:0,0|WCO:12.700,0,-25.400>")
        self.assertEqual(st["wpos"], [0.5, 2.0, 0.5])
        st = sp.feed("<Run|MPos:0,0,0|FS:0,0>")              # later reports have no WCO: the remembered one is used
        self.assertEqual(st["wpos"], [-0.5, 0.0, 1.0])
        self.assertEqual(st["mpos"], [0.0, 0.0, 0.0])

    def test_wpos_reports_give_mpos(self):
        sp = StatusParser()
        sp.feed("<Idle|WPos:0,0,0|FS:0,0|WCO:25.4,25.4,25.4>")
        st = sp.feed("<Idle|WPos:25.4,0,0|FS:0,0>")
        self.assertEqual(st["wpos"], [1.0, 0.0, 0.0])
        self.assertEqual(st["mpos"], [2.0, 1.0, 1.0])

    def test_extra_axes_keep_their_units(self):
        sp = StatusParser()
        st = sp.feed("<Idle|MPos:0,0,0,90,10|FS:0,0|WCO:0,0,0,45,0>")
        self.assertEqual(st["abc"], [45.0, 10.0])
        self.assertEqual(sp.feed("<Idle|MPos:0,0,0|FS:0,0|WCO:0,0,0>")["abc"], [])

    def test_overrides_are_remembered_and_fields_default(self):
        sp = StatusParser()
        self.assertIsNone(sp.feed("<Idle|MPos:0,0,0|FS:0,0>")["ov"])
        sp.feed("<Idle|MPos:0,0,0|FS:0,0|Ov:110,100,90>")
        st = sp.feed("<Idle|MPos:0,0,0|FS:0,0>")
        self.assertEqual(st["ov"], [110, 100, 90])
        self.assertEqual((st["pins"], st["buf"], st["line"], st["stalled"]), ("", None, None, False))
        self.assertEqual(st["raw"], "<Idle|MPos:0,0,0|FS:0,0>")
        sp.reset()
        self.assertIsNone(sp.feed("<Idle|MPos:0,0,0|FS:0,0>")["ov"])


class OtherReplies(unittest.TestCase):
    def test_banner(self):
        self.assertTrue(is_banner("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"))
        self.assertTrue(is_banner("Grbl 3.7 [FluidNC v3.9.4 (wifi) '$' for help]"))
        self.assertTrue(is_banner("Grbl 1.1h ['$' for help]"))
        self.assertTrue(is_banner("\x00�Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"))   # boot noise in front

    def test_messages_with_brackets_are_not_banners(self):
        for line in ("[MSG:INFO: Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]]",
                     "[MSG:'$H'|'$X' to unlock]", "[VER:3.7 FluidNC v3.9.4:]", "ok", "error:9", "ALARM:3",
                     "Grbl is great", "<Idle|MPos:0,0,0|FS:0,0>"):
            self.assertFalse(is_banner(line), line)

    def test_gc(self):
        self.assertEqual(parse_gc("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]"),
                         {"units": "G21", "distance": "G90", "gc": "G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0"})
        self.assertEqual(parse_gc("[GC:G1 G54 G17 G20 G91 G94 M3 M8 T1 F20 S12000]")["units"], "G20")
        self.assertEqual(parse_gc("[GC:G1 G54 G17 G20 G91 G94 M3 M8 T1 F20 S12000]")["distance"], "G91")
        self.assertIsNone(parse_gc("[MSG:GC]"))

    def test_ver(self):
        self.assertEqual(parse_ver("[VER:3.7 FluidNC v3.9.4:]"), "3.7 FluidNC v3.9.4")
        self.assertEqual(parse_ver("[VER:3.7 FluidNC v3.9.4:MyCNC]"), "3.7 FluidNC v3.9.4:MyCNC")
        self.assertIsNone(parse_ver("[OPT:PHS]"))

    def test_fluidnc_411_banner_and_ver(self):
        """The user's FluidNC 4.1.1: its banner (also a development build's), $I's [VER:], and the new [CLUSTER:]
        and [MSG:Machine: ...] lines, which are only shown."""
        for banner in ("Grbl 4.1 [FluidNC v4.1.1 (esp32-noradio) '$' for help]",
                       "Grbl 4.1 [FluidNC v4.1.1 (main-1a2b3c-dirty) (esp32-noradio) '$' for help]"):
            self.assertTrue(is_banner(banner), banner)
        self.assertEqual(parse_ver("[VER:4.1 FluidNC v4.1.1 (esp32-noradio) :]"), "4.1 FluidNC v4.1.1 (esp32-noradio)")
        for line in ("[CLUSTER:16]", "[MSG:Machine: COFFEE CUTTER]", "[OPT:MPHS]", "State 11 (Critical)",
                     "[MSG:ERR: Bad GCode: G1 X1 Q2]"):
            self.assertFalse(is_banner(line), line)
            self.assertIsNone(parse_status(line), line)
            self.assertIsNone(parse_gc(line), line)

    def test_g59_1_to_g59_3(self):
        """FluidNC 4.1.1 has G59.1-G59.3 (P7-P9): a zero there must be undone there, not in G54."""
        self.assertEqual([machine.wcs_word(line) for line in ("G59.1 G0 X1", "g59.3", "G55 G59.2", "G59", "G0 X1")],
                         [7, 9, 8, 6, None])
        self.assertEqual([machine.gc_wcs(gc) for gc in ("G0 G59.1 G17 G20", "G0 G54 G17", "G0 G17")], [7, 1, None])
        self.assertEqual([machine.wcs_name(w) for w in (1, 6, 7, 9)], ["G54", "G59", "G59.1", "G59.3"])
        self.assertEqual(machine.undo_line("X", [25.4, 0, 0], 7), "G10 L2 P7 G20 X1.000000")

    def test_the_page_says_the_same_codes(self):
        """static/codes.js has the same error and alarm meanings (FluidNC 3.9.4 and 4.1.1)."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "codes.js")) as f:
            js = f.read()
        import re
        for name, table in (("ERRORS", machine.ERRORS), ("ALARMS", machine.ALARMS)):
            body = js[js.index(f"const {name} = {{"):]
            body = body[:body.index("};")]
            got = {int(k): v for k, v in re.findall(r"(\d+): '([^']*)'", body)}
            self.assertEqual(got, table, name)

    def test_modal_words(self):
        self.assertEqual(modal_words("G20"), {"units": "G20"})
        self.assertEqual(modal_words("g91 g0 x1"), {"distance": "G91"})
        self.assertEqual(modal_words("G21G91"), {"units": "G21", "distance": "G91"})
        self.assertEqual(modal_words("N10 G020 G90"), {"units": "G20", "distance": "G90"})
        self.assertEqual(modal_words("$J=G91 G20 X1 F10"), {})          # a jog's words apply to the jog only
        self.assertEqual(modal_words("G90.1 G91.1"), {})                # arc modes, not distance modes
        self.assertEqual(modal_words("G0 X1 (G20) ; G91"), {})          # comments
        self.assertEqual(modal_words("M3 S1000"), {})
        self.assertEqual(modal_words("M30"), {"distance": "G90"})       # program end: back to G90, units stay
        self.assertEqual(modal_words("G91 G0 Z1 M2"), {"distance": "G90"})
        self.assertEqual(modal_words("M3 M300"), {})

    def test_lines_that_need_inches(self):
        """Lines whose numbers FluidNC reads in its current units: they must wait for FluidSender's G20."""
        need = machine.uses_modal_units
        for line in ("G0 Z0.25", "g91 g0 x1", "M3 S1000", "G0 X1 (G20)", "$J=G91 X1 F10", "$j=x1f10"):
            self.assertTrue(need(line), line)
        for line in ("G20 G0 X1", "G21", "g0 g20 x1", "$J=G20 G91 X1 F10", "$HZ", "$X", "$G", "$$", "$I"):
            self.assertFalse(need(line), line)

    def test_dwell_seconds(self):
        self.assertEqual(machine.dwell_seconds("G4 P12"), 12.0)
        self.assertEqual(machine.dwell_seconds("g04p0.5"), 0.5)
        self.assertEqual(machine.dwell_seconds("G10 L20 P1 X0"), 0.0)
        self.assertEqual(machine.dwell_seconds("G4"), 0.0)
        self.assertEqual(machine.dwell_seconds("$J=G4 P5"), 0.0)
        line = machine.Line("G4 P12")
        self.assertEqual((line.timeout, line.stall), (22.0, 17.0))

    def test_patch_gc(self):
        self.assertEqual(machine.patch_gc("G0 G54 G21 G90 F0", "G20", "G91"), "G0 G54 G20 G91 F0")
        self.assertIsNone(machine.patch_gc(None, "G20", "G90"))


class LineChecks(unittest.TestCase):
    def test_good_lines(self):
        self.assertEqual(check_line("  G0 X1  "), "G0 X1")
        self.assertEqual(check_line("$J=G91 X1 F10"), "$J=G91 X1 F10")
        self.assertEqual(check_line("G1 X1 (" + "x" * 119 + ")"), "G1 X1 (" + "x" * 119 + ")")   # 127 characters
        self.assertEqual(check_line("$" + "x" * 253), "$" + "x" * 253)    # FluidNC keeps 254 characters of a line
        self.assertEqual(check_line("[" + "x" * 253), "[" + "x" * 253)

    def test_refused_lines(self):
        for line in ("", "   ", None, 12, "$" + "x" * 254, "x" * 256, "G0\tX1", "G0 X1\x18", "G0 X1 (25°)",
                     "G0 X1 !", "G0 X1 ~", "G0 X1 (why?)", "?"):
            with self.assertRaises(MachineError, msg=repr(line)) as cm:
                check_line(line)
            self.assertEqual(cm.exception.status, 400)

    def test_gcode_over_127_characters_is_refused(self):
        """FluidNC 4.1.1 refuses a G-code line (or a $J= jog) over 127 characters, spaces and comments included
        (error:14: GCode.cpp gc_execute_line); $ commands still get 254."""
        for line in ("G1 X1 (" + "x" * 120 + ")", "x" * 128, "$J=" + "X1" * 62 + "F1", "$j=" + "x" * 125):
            with self.assertRaises(MachineError, msg=line) as cm:
                check_line(line)
            self.assertEqual(str(cm.exception), f"Line too long for FluidNC ({len(line)} characters; it refuses "
                                                "G-code lines over 127, spaces and comments included)")
        self.assertEqual(machine.ERRORS[14], "line too long (FluidNC takes 127 characters of G-code)")


SETTINGS = {"$/axes/x/max_rate_mm_per_min": "5000.000", "$/axes/y/max_rate_mm_per_min": "5000.000",
            "$/axes/z/max_rate_mm_per_min": "2000.000", "$/axes/x/acceleration_mm_per_sec2": "200.000",
            "$/axes/y/acceleration_mm_per_sec2": "200.000", "$/axes/z/acceleration_mm_per_sec2": "100.000",
            "$/planner_blocks": "16", "$/axes/x/soft_limits": "true", "$/axes/y/soft_limits": "true",
            "$/axes/z/soft_limits": "true",     # (like the simulator with --soft-limits; None is answered error:3)
            "$/coolant/flood_pin": "I2SO.4", "$/coolant/mist_pin": "I2SO.5",    # (pins FluidNC reads back: its
            "$/coolant/delay_ms": "0"}                                          # reports show them)
# $SS: FluidNC's startup log (the spindle's line names its type)
STARTUP = ("[MSG:INFO: FluidNC v4.1.1 https://github.com/bdring/FluidNC]", "[MSG:INFO: Machine Fake]",
           "[MSG:INFO: PWM Spindle Ena:NO_PIN Out:gpio.13 Dir:NO_PIN Freq:5000Hz Period:8191]",
           "[MSG:INFO: Flood coolant I2SO.4]", "[MSG:INFO: Mist coolant I2SO.5]")


class Harness(unittest.TestCase):
    """The line protocol without a port or thread: replies go in through handle(), the time is
    made up (also for the Machine's calls), and what would be written to FluidNC is collected:
    lines from link.out, realtime bytes from link.out and from Machine's writes at once (self.rt)."""

    IDLE = "<Idle|MPos:0,0,0|FS:0,0|WCO:0,0,0>"

    def setUp(self):
        self.m = machine.Machine()
        self.events = []
        self.m.emit = self.events.append
        self.m.clock = lambda: self.t
        self.link = machine.Link(self.m, -1, "/dev/fake", 115200)   # no port: its thread never runs
        self.link.write_realtime = lambda byte: self.rt.append(byte)
        self.m.link = self.link
        self.t = 1000.0
        self.rt = []                    # realtime bytes written (but '?')
        self.lines = []                 # lines written

    def tearDown(self):
        for fd in (self.link.wake_r, self.link.wake_w):
            os.close(fd)

    def reply(self, *lines, dt=0.01):
        for line in lines:
            self.t += dt
            self.link.handle(line, self.t)

    def tick(self, dt=0.1):
        self.t += dt
        self.link.tick(self.t)

    RT = set(machine.REALTIME.values()) | {byte for _, byte, _, _ in machine.COOLANT.values()} | {machine.LINE_EDIT_OFF}

    def drain(self):
        """Moves what the reader thread would write (link.out) to self.lines and self.rt (but '?' and the ^L
        written first when connecting)."""
        for data in self.link.out:
            if data in self.RT or (b"\n" not in data and all(b >= 0x80 for b in data)):   # (override bytes too)
                if data not in (b"?", machine.LINE_EDIT_OFF):
                    self.rt.append(data)
            else:
                self.lines += [x.decode() for x in data.split(b"\n") if x]
        self.link.out.clear()

    def sent(self):
        """The lines written since the last call."""
        self.drain()
        lines, self.lines = self.lines, []
        return lines

    def realtime(self):
        """The realtime bytes written since the last call (status requests left out), as one bytes object."""
        self.drain()
        rt, self.rt = b"".join(self.rt), []
        return rt

    def log(self, kind):
        return [e["text"] for e in self.events if e["type"] == "console" and e["dir"] == kind]

    def evs(self, kind):
        return [e for e in self.events if e["type"] == kind]

    def go_live(self, state="Idle", settings=SETTINGS, fw="3.9.4", startup=STARTUP, gc="M5 M9 T0 F0 S0"):
        """Connected (the first report in that state), and the connect reads answered: $I, $G, $T in an alarm
        (FluidNC 4.1.1 answers $I in a critical one too), the settings and $SS. fw: the $I answers of FluidNC
        3.9.4 or 4.1.1."""
        self.reply(f"<{state}|MPos:0,0,0|FS:0,0|WCO:0,0,0>")
        self.link.went_live(self.t)
        self.assertEqual(self.sent(), ["$I"])
        if fw == "4.1.1":
            self.reply("[VER:4.1 FluidNC v4.1.1 (esp32-noradio) :]", "[OPT:MPHS]", "[CLUSTER:16]",
                       "[MSG:Machine: COFFEE CUTTER]", "ok")
        else:
            self.reply("[VER:3.7 FluidNC v3.9.4:]", "[OPT:PHS]", "ok")
        self.assertEqual(self.sent(), ["$G"])
        self.reply(f"[GC:G0 G54 G17 G21 G90 G94 {gc}]", "ok")
        self.assertEqual(self.link.firmware, "4.1 FluidNC v4.1.1 (esp32-noradio)" if fw == "4.1.1"
                         else "3.7 FluidNC v3.9.4")
        self.assertEqual(self.link.modal["units"], "G21")
        if state == "Alarm":
            self.assertEqual(self.sent(), ["$T"])
            self.reply("State 1 (Alarm)", "ok")
        for query in machine.SETTINGS:                  # then the settings, one at a time
            self.assertEqual(self.sent(), [query])
            value = settings.get(query)
            self.reply(*(["error:3"] if value is None else [f"{query}={value}", "ok"]))
        self.assertEqual(self.sent(), ["$SS"])          # and FluidNC's startup log (the spindle type)
        self.reply(*startup, "ok")
        self.assertEqual(self.link.settings_left, [])

    def inches(self):
        """FluidSender's own G20 after connecting, answered ok."""
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.reply("ok")


class Protocol(Harness):

    def test_the_echo_is_turned_off_before_the_first_status_request(self):
        """^L first: it turns off FluidNC's UART echo and editing mode (lineedit.cpp), which a terminal session
        before (an arrow key typed) can leave on; it is ignored otherwise."""
        self.assertEqual(self.link.out, [machine.LINE_EDIT_OFF])
        self.tick()
        self.assertEqual(self.link.out, [b"\x0c", b"?"])

    def test_g20_after_a_fresh_idle_and_retried_on_error(self):
        self.go_live()
        self.tick()
        self.assertEqual(self.sent(), [])               # the last report is older than $G's ok
        for attempt in range(3):
            self.reply(self.IDLE)
            self.tick()
            self.assertEqual(self.sent(), ["G20"], attempt)
            self.reply("error:20")
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), [])               # three tries per connect/reset, then give up
        self.assertEqual(self.log("info").count("*** Setting units to inches (G20)"), 3)
        self.assertTrue(any("Could not set inches" in t for t in self.log("error")))
        self.assertEqual(self.link.modal["units"], "G21")
        # after giving up, G-code that would run in millimeters is refused; $ commands and lines with units go
        with self.assertRaises(MachineError) as cm:
            self.m.send("G0 X1")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("G20 failed", str(cm.exception))
        self.m.send("$G")
        self.m.send("G20 G0 X1")
        self.tick()
        self.assertEqual(self.sent(), ["$G"])
        self.reply("ok")
        self.assertEqual(self.sent(), ["G20 G0 X1"])
        self.reply("ok")
        self.assertEqual(self.link.modal["units"], "G20")
        self.m.send("G0 X2")                            # the user set the units: back to normal
        self.tick()
        self.assertEqual(self.sent(), ["G0 X2"])

    def test_waiting_gcode_dropped_when_g20_gives_up(self):
        self.go_live()
        for attempt in range(3):
            self.reply(self.IDLE)
            self.tick()
            self.assertEqual(self.sent(), ["G20"], attempt)
            if attempt == 2:
                self.m.send("G0 Z1")                    # queued behind the last try
                self.m.send("$X")
            self.reply("error:20")
        self.assertEqual(self.sent(), ["$X"])           # G0 Z1 would run in millimeters: not sent
        self.assertIn("Not sent: G0 Z1  (FluidNC may still be in millimeters: G20 failed. Send G20 yourself first "
                      "(or put G20 or G21 in the line))", self.log("error"))

    def test_g20_goes_before_gcode_typed_ahead(self):
        """Connect in Alarm, home Z, and type the next move while it homes: G20 must go before that move,
        or it runs in millimeters (0.25 mm instead of 0.25 in)."""
        self.go_live(state="Alarm")
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>")
        self.tick()
        self.assertEqual(self.sent(), [])
        self.m.send("$HZ")
        self.tick()
        self.assertEqual(self.sent(), ["$HZ"])
        self.reply("<Home|MPos:0,0,0|FS:0,0>")
        self.m.send("G0 Z0.25")
        self.m.send("$G")
        self.tick()
        self.assertIn("Machine is in ALARM: home ($HZ) or unlock ($X) from the console", self.log("info"))
        self.assertFalse([t for t in self.log("info") if t.startswith("Waiting")])
        self.tick(0.6)                                  # still homing: they look lost unless the console says so
        self.assertEqual(self.log("info")[-2:], ["Waiting to send: G0 Z0.25  (after '$HZ')",
                                                 "Waiting to send: $G  (after 'G0 Z0.25')"])
        self.reply("<Idle|MPos:0,0,0|FS:0,0>")
        self.tick()
        self.assertEqual(self.sent(), [])               # $HZ still in flight
        self.reply("ok")
        self.tick()
        self.assertEqual(self.sent(), [])               # waits for a report made after that ok
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.reply("ok")
        self.assertEqual(self.sent(), ["G0 Z0.25"])
        self.reply("ok")
        self.assertEqual(self.sent(), ["$G"])
        self.reply("[GC:G0 G54 G17 G20 G90 G94 M5 M9 T0 F0 S0]", "ok")
        self.assertEqual(self.link.modal, {"units": "G20", "distance": "G90", "gc": "G0 G54 G17 G20 G90 G94 M5 M9 T0 F0 S0"})
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), [])               # once only

    def test_gcode_in_alarm_goes_as_it_is(self):
        """In Alarm FluidNC refuses G-code (error:9), so nothing moves in millimeters: no need to hold it."""
        self.go_live(state="Alarm")
        self.m.send("G0 X1")
        self.tick()
        self.assertEqual(self.sent(), [])               # the last report is older than $G's ok
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>")
        self.tick()
        self.assertEqual(self.sent(), ["G0 X1"])
        self.reply("error:9")
        self.m.send("$X")
        self.m.send("G0 X2")
        self.tick()
        self.assertEqual(self.sent(), ["$X"])
        self.reply("[MSG:Caution: Unlocked]", "ok")
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>", dt=0)   # (made before the ok: not used)
        self.tick()
        self.assertEqual(self.sent(), [])
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.reply("ok")
        self.assertEqual(self.sent(), ["G0 X2"])

    def test_gcode_waits_while_busy_at_connect(self):
        """Connected while FluidNC runs: typed G-code waits for Idle and G20, and says so. (Connected while it
        holds: see HeldAtConnect.)"""
        self.go_live(state="Run")
        self.reply("<Run|MPos:0,0,0|FS:100,0>")
        self.m.send("G0 X1")
        self.tick()
        self.assertEqual(self.sent(), [])
        self.reply("<Run|MPos:0,0,0|FS:100,0>")
        self.tick(0.6)
        self.assertEqual(self.sent(), [])
        self.assertIn("Waiting to send: G0 X1  (until FluidNC is Idle and set to inches with G20)", self.log("info"))
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.reply("ok")
        self.assertEqual(self.sent(), ["G0 X1"])

    def test_nothing_goes_between_our_reset_and_its_banner(self):
        self.go_live()
        self.inches()
        self.link.reset_at = self.t                     # (what Machine.realtime('reset') does)
        self.m.send("G0 X2")
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), [])               # FluidNC may be resetting: it would run it in G21
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")
        self.assertIsNone(self.link.reset_at)
        self.assertIn("Dropped 1 line FluidNC had not answered", self.log("info"))   # (the banner drops all)
        self.reply(self.IDLE, dt=0.06)                  # (a moment after the banner: see Leftover)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.reply("ok")
        self.assertEqual(self.sent(), [])
        # no banner at all: after RESET_WAIT the units are assumed reset too
        self.link.reset_at = self.t
        self.m.send("G0 X3")
        for _ in range(19):
            self.reply(self.IDLE, dt=0.1)
            self.tick(0)
        self.assertEqual(self.sent(), [])
        self.reply(self.IDLE, dt=0.2)
        self.tick(0)
        self.assertIn("No reset banner from FluidNC; assuming it was reset", self.log("info"))
        self.reply(self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])

    def test_the_users_own_units_win(self):
        self.go_live()
        self.m.send("G21 G91")
        self.tick()
        self.assertEqual(self.sent(), ["G21 G91"])
        self.reply("ok", self.IDLE)
        self.tick()
        self.assertEqual(self.sent(), [])
        self.assertEqual((self.link.modal["units"], self.link.modal["distance"]), ("G21", "G91"))

    def test_modal_tracking(self):
        self.go_live()
        self.inches()
        for line, reply, units, distance in (("G91", "ok", "G20", "G91"), ("$J=G90 G21 X1 F10", "ok", "G20", "G91"),
                                             ("G21", "error:20", "G20", "G91"), ("G90 G21 (G20)", "ok", "G21", "G90")):
            self.m.send(line)
            self.tick()
            self.assertEqual(self.sent(), [line])
            self.reply(reply)
            self.assertEqual((self.link.modal["units"], self.link.modal["distance"]), (units, distance), line)
            if line.startswith("$J"):
                self.reply(self.IDLE, dt=0.06)          # (the next line waits until a report shows it has stopped)

    def test_only_ok_and_error_answer_a_line(self):
        self.go_live()
        self.inches()
        self.m.send("G0 X1")
        self.m.send("G0 X2")
        self.tick()
        self.assertEqual(self.sent(), ["G0 X1"])
        self.reply("ALARM:1", "[MSG:Reset to continue]", "[GC:G0 G54 G17 G20 G90 G94 M5 M9 T0 F0 S0]",
                   "[MSG:INFO: Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]]", "<Alarm|MPos:0,0,0|FS:0,0>",
                   "errors: 2", "okay")
        self.tick()
        self.assertEqual(self.sent(), [])               # still waiting for G0 X1's ok
        self.assertIsNotNone(self.link.inflight)
        self.assertNotIn("*** Controller reset", self.log("info"))
        self.reply("error:9")
        self.assertEqual(self.sent(), ["G0 X2"])
        self.reply("error: Bad number format")          # FluidNC with verbose_errors: true
        self.assertIsNone(self.link.inflight)

    def test_indents_are_kept_and_one_bad_line_is_survived(self):
        self.go_live()
        self.link.received(b"x:\r\n  steps_per_mm: 80.000\r\n   \r\n", self.t)
        self.assertEqual(self.log("rx")[-2:], ["x:", "  steps_per_mm: 80.000"])    # ($CD's YAML)
        with mock.patch.object(self.link, "handle", side_effect=[ValueError("boom"), None]) as handle, \
                mock.patch.object(machine.traceback, "print_exc"):
            self.link.received(b"<Idle|MPos:0,0,0|FS:0,nan>\nok\n", self.t)
        self.assertEqual(handle.call_count, 2)          # the next line was still handled
        self.assertTrue(self.log("error")[-1].startswith("Could not handle this line from FluidNC: '<Idle|"))

    def test_xon_xoff_bytes_are_left_out(self):
        """A 0x11 sent by an earlier terminal puts FluidNC's console in XON/XOFF mode: it puts 0x13/0x11 into its
        output then, which must not spoil an ok."""
        self.go_live()
        self.m.send("$G")
        self.tick()
        self.assertEqual(self.sent(), ["$G"])
        self.link.received(b"\x13[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n\x11ok\x13\r\n", self.t)
        self.assertIsNone(self.link.inflight)
        self.assertEqual(self.log("rx")[-1], "ok")

    def test_program_end_goes_back_to_absolute(self):
        self.go_live()
        self.inches()
        for line in ("G91", "M30"):
            self.m.send(line)
            self.tick()
            self.assertEqual(self.sent(), [line])
            self.reply("ok")
        self.assertEqual((self.link.modal["units"], self.link.modal["distance"]), ("G20", "G90"))

    def test_reset_banner(self):
        self.go_live()
        self.inches()
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:120,100,100>")
        self.m.send("G0 X1")
        self.m.send("G0 X2")
        self.m.send("G0 X3")
        self.tick()
        self.assertEqual(self.sent(), ["G0 X1"])
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")
        self.assertIn("*** Controller reset", self.log("info"))
        self.assertIn("Dropped 3 lines FluidNC had not answered", self.log("info"))
        self.assertEqual((len(self.link.queue), self.link.inflight), (0, None))
        self.assertEqual(self.link.modal, {"units": "G21", "distance": "G90", "gc": None})
        self.tick()
        self.assertEqual(self.sent(), [])               # needs a report from after the reset
        self.reply("<Idle|MPos:0,0,0|FS:0,0>")
        self.assertEqual(self.link.status["ov"], [100, 100, 100])   # overrides were reset; the work offset is kept
        self.assertEqual(self.link.status["wpos"], [0.0, 0.0, 0.0])
        self.tick()
        self.assertEqual(self.sent(), ["G20"])

    def test_timeout_drops_the_waiting_lines_and_holds_the_rest(self):
        self.go_live()
        self.inches()
        self.m.send("M8")
        self.m.send("G0 X1")
        self.tick()
        self.assertEqual(self.sent(), ["M8"])
        for _ in range(4):
            self.reply(self.IDLE, dt=1.0)
            self.tick()
        st = self.link.current_status(self.t)
        self.assertFalse(st["stalled"])
        self.reply(self.IDLE, dt=1.5)
        self.assertTrue(self.link.current_status(self.t)["stalled"])   # over 5 s, status still answers
        self.t += 3.0
        self.assertEqual(self.link.current_status(self.t)["state"], "No response")
        self.assertFalse(self.link.current_status(self.t)["stalled"])
        self.reply(self.IDLE, dt=1.0)
        self.tick(0.5)                                  # 10.4 s after sending
        self.assertEqual(self.log("error"), ["No reply to 'M8' within 10 s; dropped the 1 line waiting after it. "
                                             "FluidNC may still run it. No more lines are sent until it answers "
                                             "(Soft Reset gives up)."])
        self.assertEqual(self.sent(), [])
        self.assertTrue(self.link.current_status(self.t)["stalled"])   # still unanswered
        self.m.send("G91 G0 Z0.25 Q1")                  # a typo FluidNC will refuse
        self.tick()
        self.tick(0.6)
        self.assertEqual(self.log("info")[-1], "Waiting to send: G91 G0 Z0.25 Q1  (FluidNC has not answered 'M8' "
                                               "yet; Soft Reset drops it)")
        self.assertEqual(self.sent(), [])               # the late ok must not answer this one
        self.reply("ok")                                # M8's late ok
        self.assertEqual(self.log("info")[-1], "That was the late reply to 'M8'")
        self.assertEqual(self.sent(), ["G91 G0 Z0.25 Q1"])
        self.reply("error:20")
        self.assertEqual(self.link.modal["distance"], "G90")           # refused: nothing changed
        self.assertFalse(self.link.current_status(self.t)["stalled"])
        self.reply("ok")                                # a reply nothing waits for: only shown
        self.assertIsNone(self.link.inflight)

    def test_a_reset_ends_the_wait_for_a_late_reply(self):
        self.go_live()
        self.inches()
        self.m.send("M8")
        self.tick()
        self.sent()
        self.reply(self.IDLE, dt=10.5)
        self.tick(0)
        self.assertEqual(self.link.owed.text, "M8")
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertIsNone(self.link.owed)
        self.assertEqual(self.sent(), ["G20"])

    def test_lines_behind_moves_are_not_overdue(self):
        """FluidNC answers M5 (or M3, M8, G10, a full planner...) only after the moves before it: not stalled,
        no timeout, however long the moves take. A G4 gets its dwell on top."""
        self.go_live()
        self.inches()
        self.m.send("G1 X10 F5")
        self.tick()
        self.sent()
        self.reply("ok")                                # planned at once; it takes 2 minutes
        self.m.send("M5")
        self.tick()
        self.assertEqual(self.sent(), ["M5"])
        for _ in range(120):                            # 60 s of moving
            self.reply("<Run|MPos:1,0,0|FS:127,0>", dt=0.4)
            self.tick(0.1)
            self.assertFalse(self.link.current_status(self.t)["stalled"])
        self.assertEqual(self.log("error"), [])
        self.reply(self.IDLE)
        self.assertFalse(self.link.current_status(self.t)["stalled"])
        self.reply("ok")
        self.m.send("G4 P12")
        self.tick()
        self.assertEqual(self.sent(), ["G4 P12"])
        for _ in range(16):
            self.reply(self.IDLE, dt=1.0)
            self.tick(0)
        self.assertFalse(self.link.current_status(self.t)["stalled"])  # 16 s: not yet (5 s + the 12 s dwell)
        for _ in range(5):
            self.reply(self.IDLE, dt=1.0)
            self.tick(0)
        self.assertTrue(self.link.current_status(self.t)["stalled"])
        self.assertEqual(self.log("error"), [])         # 21 s: no timeout yet (10 s + 12 s)
        self.reply(self.IDLE, dt=1.1)
        self.tick(0)
        self.assertTrue(self.log("error")[-1].startswith("No reply to 'G4 P12' within 22 s"))

    def test_homing_waits_long_and_is_not_stalled_while_homing(self):
        self.go_live()
        self.inches()
        self.m.send("$HZ")
        self.tick()
        self.assertEqual(self.sent(), ["$HZ"])
        self.reply("<Home|MPos:0,0,0|FS:0,0>", dt=30)
        self.tick()
        self.assertFalse(self.link.current_status(self.t)["stalled"])
        self.assertEqual(self.log("error"), [])
        self.reply("<Idle|MPos:0,0,0|FS:0,0>")
        self.assertFalse(self.link.current_status(self.t)["stalled"])  # (FluidNC says Idle just before the ok)
        self.reply("<Idle|MPos:0,0,0|FS:0,0>", dt=5.1)
        self.assertTrue(self.link.current_status(self.t)["stalled"])   # Idle for 5 s but no ok: that is stuck

    def test_released_only_at_rest(self):
        self.go_live()
        self.inches()
        self.m.release_after = 2.0
        self.m.alone_since = self.t                     # no page open from now on
        self.m.send("G1 X3 F2")
        self.tick()
        self.sent()
        self.reply("ok")
        for _ in range(40):                             # 4 s of moving (FluidNC answered the move at once)
            self.reply("<Run|MPos:1,0,0|FS:50,0>", dt=0.1)
            self.tick(0)
        self.assertIsNone(self.link.stopping)
        self.reply(self.IDLE, dt=0.1)
        self.tick(1.0)
        self.assertIsNone(self.link.stopping)           # the count starts at rest
        self.reply(self.IDLE, dt=1.0)
        self.tick(0.05)
        self.assertEqual(self.link.stopping, ("Released the port because no FluidSender window is open", "info"))

    def test_status_events_are_limited(self):
        self.go_live()
        self.events.clear()
        for i in range(20):                             # 20 changing reports in 1 s
            self.reply(f"<Run|MPos:{i},0,0|FS:500,0>", dt=0.05)
            self.link.emit_status(self.t)
        self.assertLessEqual(len([e for e in self.events if e["type"] == "status"]), 6)
        self.events.clear()
        for i in range(30):                             # 3 s of the same report: one every 2 s
            self.reply("<Idle|MPos:0,0,0|FS:0,0>", dt=0.1)
            self.link.emit_status(self.t)
        self.assertEqual(len([e for e in self.events if e["type"] == "status"]), 2)


JOG = "<Jog|MPos:6.350,0.000,0.000|FS:3810,0>"
IDLE_AT_HALF = "<Idle|MPos:12.700,0.000,0.000|FS:0,0>"      # X 0.5 in from where the jogs start
JOGGING = "Jogging: let go of the jog button first"
CRITICAL = "FluidNC needs a Soft Reset first ([MSG:Reset to continue])"


class Stage2(Harness):
    """Helpers for the jog, zero, home and unlock tests."""

    def ready(self, **kw):
        """Connected, settings read, G20 done, and an Idle report made after all that."""
        self.go_live(**kw)
        self.inches()
        self.reply(self.IDLE, dt=0.06)
        self.events.clear()

    def refused(self, call, message, status=409):
        with self.assertRaises(MachineError) as cm:
            call()
        self.assertEqual((cm.exception.status, str(cm.exception)), (status, message))

    def step(self, feed=150, **dist):
        return self.m.jog({"step": dist, "feed": feed})["line"]

    def do_step(self, feed=150, **dist):
        """A step jog sent, answered and done (an Idle report after it). Returns its line."""
        line = self.step(feed, **dist)
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply("ok")
        self.reply(self.IDLE, dt=0.06)
        return line

    def start(self, feed=150, **direction):
        return self.m.jog({"start": direction, "feed": feed})["id"]

    def running(self, **direction):
        """A hold jog with its first piece sent. Returns (id, piece)."""
        jog_id = self.start(**(direction or {"x": 1}))
        self.tick(0.01)
        pieces = self.sent()
        self.assertEqual(len(pieces), 1)
        return jog_id, pieces[0]

    def stopped(self, report=IDLE_AT_HALF):
        """The piece in flight answered, then a report showing FluidNC has stopped."""
        if self.link.inflight:
            self.reply("ok")
        self.reply(report, dt=0.06)
        self.assertIsNone(self.link.cancel)


class Settings(Stage2):
    def test_travel_as_fluidnc_works_it_out(self):
        """The machine's envelope (machine coordinates, inches): homing toward + (the default) puts the travel below
        mpos_mm, toward - above it; no homing section: below 0; no max_travel_mm: unknown."""
        self.assertEqual(machine.travel({"maxTravel": 254.0, "homeMpos": 0.0, "homePositive": True}), [-10.0, 0.0])
        self.assertEqual(machine.travel({"maxTravel": 254.0, "homeMpos": 25.4, "homePositive": False}), [1.0, 11.0])
        self.assertEqual(machine.travel({"maxTravel": 254.0, "homeMpos": None, "homePositive": None}), [-10.0, 0.0])
        self.assertIsNone(machine.travel({"maxTravel": None, "homeMpos": 0.0, "homePositive": True}))
        self.go_live(settings=dict(SETTINGS, **{"$/axes/x/max_travel_mm": "762.000", "$/axes/x/homing/mpos_mm": "0.000",
                                                "$/axes/x/homing/positive_direction": "false",
                                                "$/axes/y/max_travel_mm": "609.600", "$/axes/y/homing/mpos_mm": "-5.000",
                                                "$/axes/y/homing/positive_direction": "true"}))
        axes = self.m.snapshot()["axes"]
        self.assertEqual((axes["x"]["travel"], axes["y"]["travel"], axes["z"]["travel"]),
                         ([0.0, 30.0], [-24.1969, -0.1969], None))

    def test_read_quietly_after_g(self):
        self.go_live()
        no_travel = {"maxTravel": None, "homeMpos": None, "homePositive": None}     # (not in SETTINGS here)
        self.assertEqual(self.link.axes, {"x": {"maxRate": 5000.0, "accel": 200.0, "softLimits": True, **no_travel},
                                          "y": {"maxRate": 5000.0, "accel": 200.0, "softLimits": True, **no_travel},
                                          "z": {"maxRate": 2000.0, "accel": 100.0, "softLimits": True, **no_travel}})
        self.assertEqual(self.link.planner_blocks, 16)
        self.assertEqual(self.log("tx"), ["$I", "$G"])          # the reads, their values and oks are not shown
        self.assertEqual(self.log("rx"), ["[VER:3.7 FluidNC v3.9.4:]", "[OPT:PHS]", "ok",
                                          "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]", "ok"])
        self.assertEqual(self.log("info")[-1], "FluidNC max speeds: X 196.9 · Y 196.9 · Z 78.7 in/min")
        axes = {"x": {"maxRate": 196.85, "accel": 7.874, "softLimits": True, "travel": None},
                "y": {"maxRate": 196.85, "accel": 7.874, "softLimits": True, "travel": None},
                "z": {"maxRate": 78.74, "accel": 3.937, "softLimits": True, "travel": None}}
        self.assertEqual(self.evs("axes"), [{"type": "axes", "axes": axes, "plannerBlocks": 16}])
        snap = self.m.snapshot()
        self.assertEqual((snap["axes"], snap["jog"], snap["zeroUndo"], snap["alarm"]), (axes, None, None, None))
        self.inches()                                   # then G20, as before

    def test_errors_are_shown_with_their_line(self):
        self.go_live(settings=dict(SETTINGS, **{"$/axes/z/max_rate_mm_per_min": None, "$/planner_blocks": None,
                                                "$/axes/x/acceleration_mm_per_sec2": "nan"}))
        self.assertEqual((self.link.axes["z"]["maxRate"], self.link.axes["x"]["accel"], self.link.planner_blocks),
                         (None, None, None))
        shown = [(e["dir"], e["text"]) for e in self.evs("console") if e["dir"] in ("tx", "rx")][-4:]
        self.assertEqual(shown, [("tx", "$/axes/z/max_rate_mm_per_min"), ("rx", "error:3"),
                                 ("tx", "$/planner_blocks"), ("rx", "error:3")])
        self.assertEqual(self.log("info")[-1], "FluidNC max speeds: X 196.9 · Y 196.9 in/min (Z: could not be read)")
        ev = self.evs("axes")[-1]
        self.assertEqual((ev["axes"]["z"], ev["axes"]["x"]["accel"], ev["plannerBlocks"]),
                         ({"maxRate": None, "accel": 3.937, "softLimits": True, "travel": None}, None, None))

    def test_soft_limits_off_are_said(self):
        self.go_live(settings=dict(SETTINGS, **{"$/axes/x/soft_limits": "false", "$/axes/z/soft_limits": "false"}))
        self.assertEqual([self.link.axes[a]["softLimits"] for a in "xyz"], [False, True, False])
        self.assertEqual(self.log("info")[-2:], ["FluidNC max speeds: X 196.9 · Y 196.9 · Z 78.7 in/min",
                                                 "FluidNC's soft limits are off on X Z: it doesn't stop jogs or moves at "
                                                 "the end of the travel there"])
        self.assertEqual([self.evs("axes")[-1]["axes"][a]["softLimits"] for a in "xyz"], [False, True, False])

    def test_nothing_read_means_no_caps_and_default_pieces(self):
        self.ready(settings={})
        self.assertIn("Could not read FluidNC's max speeds: jog speeds are not capped here (FluidNC still caps them)",
                      [e["text"] for e in self.m.console])
        self.assertEqual(self.do_step(400, x=1), "$J=G20G91X1F400")
        self.assertEqual(self.running(x=1)[1], "$J=G20G91X0.158F150")   # 50 mm/s^2, 16 planner blocks

    def test_unanswered_reads_are_asked_again_after_a_reset(self):
        self.reply(self.IDLE)
        self.link.went_live(self.t)
        self.assertEqual(self.sent(), ["$I"])
        self.reply("ok")
        self.assertEqual(self.sent(), ["$G"])
        self.reply("ok")
        self.assertEqual(self.sent(), [machine.SETTINGS[0]])
        self.reply(f"{machine.SETTINGS[0]}=5000.000", "ok")
        self.assertEqual(self.sent(), [machine.SETTINGS[1]])
        self.m.realtime("reset")                        # (keeps the reads that wait)
        self.assertEqual(len(self.link.queue), len(machine.READS) - 2)
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")
        self.assertEqual([line.text for line in self.link.queue], list(machine.READS[1:]))
        self.assertNotIn("Dropped", " ".join(self.log("info")))     # (they aren't the user's lines)
        self.tick()
        self.assertEqual(self.sent(), [])               # (until a report a moment after the banner: see Leftover)
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), [machine.SETTINGS[1]])
        self.assertEqual(self.link.axes["x"]["maxRate"], 5000.0)


class StepJogs(Stage2):
    def test_lines(self):
        self.ready()
        lines = [self.do_step(x=0.001), self.do_step(y=10), self.do_step(z=-1), self.do_step(x=0.1, y=-0.1, z=0),
                 self.do_step(x=1.23456), self.do_step(x=0.00004, y=0.01), self.do_step(150.06, x=-40)]
        self.assertEqual(lines, ["$J=G20G91X0.001F150", "$J=G20G91Y10F150", "$J=G20G91Z-1F78.7", "$J=G20G91X0.1Y-0.1F150",
                                 "$J=G20G91X1.2346F150", "$J=G20G91Y0.01F150", "$J=G20G91X-40F150.1"])
        self.assertEqual(self.log("tx")[-7:], lines)    # shown like typed lines, and their oks
        self.assertEqual(self.log("rx")[-7:], ["ok"] * 7)
        self.assertEqual(self.link.modal["distance"], "G90")        # (a jog's G20 G91 are its own)

    def test_feed_capped_by_the_max_rates(self):
        self.ready()                                    # X, Y 196.85 in/min, Z 78.74
        self.assertEqual(self.do_step(400, x=1), "$J=G20G91X1F196.8")       # rounded down
        self.assertEqual(self.do_step(150, z=-1), "$J=G20G91Z-1F78.7")
        self.assertEqual(self.do_step(150, x=0.1, z=0.1), "$J=G20G91X0.1Z0.1F111.3")   # Z's share is the limit
        self.assertEqual(self.do_step(250, x=1, y=1), "$J=G20G91X1Y1F250")   # each axis 176.8: not capped
        self.assertEqual(self.do_step(0.01, x=1), "$J=G20G91X1F0.1")

    def test_bad_calls(self):
        self.ready()
        for data in ({"step": {"x": 40.01}, "feed": 150}, {"step": {"x": -41}, "feed": 150}, {"step": {"a": 1}, "feed": 150},
                     {"step": {}, "feed": 150}, {"step": {"x": 0, "y": 0}, "feed": 150}, {"step": {"x": 0.00004}, "feed": 150},
                     {"step": {"x": "1"}, "feed": 150}, {"step": {"x": True}, "feed": 150}, {"step": {"x": 10 ** 400}},
                     {"step": {"x": float("nan")}, "feed": 150}, {"step": {"x": 1}, "feed": 0}, {"step": {"x": 1}, "feed": -5},
                     {"step": {"x": 1}}, {"step": {"x": 1}, "feed": float("inf")}, {"step": {"x": 1}, "feed": 10001},
                     {"step": [1, 0, 0], "feed": 150}, {"feed": 150}, {"step": {"x": 1}, "feed": 150, "stop": "all"}):
            with self.assertRaises(MachineError, msg=repr(data)) as cm:
                self.m.jog(data)
            self.assertEqual(cm.exception.status, 400, data)
        self.assertEqual(list(self.link.queue), [])

    def test_refused_unless_idle_or_jogging(self):
        self.refused(lambda: self.step(x=1), "Not connected")
        self.ready()
        self.t += 2.1
        self.refused(lambda: self.step(x=1), "No response from FluidNC")
        for report, why in (("<Alarm|MPos:0,0,0|FS:0,0>", "ALARM: Unlock or Home first"),
                            ("<Run|MPos:0,0,0|FS:100,0>", "Running: wait until FluidNC is Idle"),
                            ("<Hold:0|MPos:0,0,0|FS:0,0>", "Hold: Resume or Soft Reset first"),
                            ("<Home|MPos:0,0,0|FS:0,0>", "Homing: wait until it is done"),
                            ("<Door:1|MPos:0,0,0|FS:0,0>", "The safety door is open")):
            self.reply(report)
            self.refused(lambda: self.step(x=1), why)
        self.reply("ALARM:1", "[MSG:ERR: Reset to continue]", "<Alarm|MPos:0,0,0|FS:0,0>")
        self.refused(lambda: self.step(x=1), CRITICAL)
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.refused(lambda: self.step(x=1), "Still talking to FluidNC after connecting; try again in a second")
        self.reply("ok", self.IDLE)
        self.link.reset_at = self.t
        self.refused(lambda: self.step(x=1), "FluidNC is being reset; try again in a second")
        self.link.reset_at = None
        self.m.send("G4 P1")                            # a line in flight, then one waiting
        self.tick()
        self.refused(lambda: self.step(x=1), "Waiting for FluidNC to finish 'G4 P1'")
        self.m.send("M5")
        self.reply("ok")
        self.refused(lambda: self.step(x=1), "Waiting for FluidNC to finish 'M5'")
        self.reply("ok", self.IDLE, dt=10.5)
        self.tick(0)
        self.m.send("M8")
        self.tick()
        self.reply(self.IDLE, dt=10.5)
        self.tick(0)                                    # M8 timed out: FluidNC may still answer it
        self.refused(lambda: self.step(x=1), "FluidNC has not answered 'M8' yet; Soft Reset drops it")
        self.reply("ok", self.IDLE)
        self.assertEqual(self.step(x=1), "$J=G20G91X1F150")
        self.assertEqual(self.link.queue[-1].tag, "step")

    def test_at_most_five_steps_pending(self):
        self.ready()
        for _ in range(5):
            self.step(x=0.1)
        self.refused(lambda: self.step(x=0.1), "5 jog steps are still waiting or moving; wait for them")
        self.tick(0.01)
        for _ in range(5):
            self.assertEqual(self.sent(), ["$J=G20G91X0.1F150"])
            self.reply("ok")                            # each goes once FluidNC answered the last
        self.reply(self.IDLE, dt=0.02)                  # too soon after the last ok to tell
        self.refused(lambda: self.step(x=0.1), "5 jog steps are still waiting or moving; wait for them")
        self.reply(JOG, dt=0.1)
        self.refused(lambda: self.step(x=0.1), "5 jog steps are still waiting or moving; wait for them")
        self.reply(self.IDLE, dt=0.1)                   # they are done
        self.step(x=0.1)


class HoldJogs(Stage2):
    def test_pieces_from_the_formula(self):
        self.ready()
        for direction, feed, piece in (({"x": 1}, 150, "$J=G20G91X0.073F150"),
                                       ({"x": 1, "y": -1}, 150, "$J=G20G91X0.0516Y-0.0516F150"),
                                       ({"z": -1}, 150, "$J=G20G91Z-0.039F78.7"),     # capped at Z's max rate
                                       ({"x": -1}, 0.1, "$J=G20G91X-0.0001F0.1")):
            jog_id = self.start(feed, **direction)
            self.tick(0.01)
            self.assertEqual(self.sent(), [piece], direction)
            self.m.jog({"stop": jog_id})
            self.stopped()

    def test_pieces_with_other_settings(self):
        self.ready(settings=dict(SETTINGS, **{"$/planner_blocks": "10"}))
        self.assertEqual(self.running(x=1)[1], "$J=G20G91X0.1277F150")     # fewer blocks: longer pieces
        self.assertEqual(machine.segment_line([1, 0, 0], 150, self.link.axes, 64), ("$J=G20G91X0.073F150", 0.073))

    def test_one_piece_at_a_time_quietly(self):
        self.ready()
        jog_id, piece = self.running(x=1, y=-1)
        self.assertEqual(piece, "$J=G20G91X0.0516Y-0.0516F150")
        info = {"id": jog_id, "dir": {"x": 1, "y": -1, "z": 0}, "feed": 150}
        self.assertEqual(self.evs("jog"), [{"type": "jog", "jog": info}])
        self.assertEqual(self.m.snapshot()["jog"], info)
        self.assertEqual(self.log("info"), ["Jog X+ Y- at 150 in/min (hold)"])
        for _ in range(3):
            self.tick(0.05)
            self.m.jog({"keep": jog_id})
            self.assertEqual(self.sent(), [])           # the next piece only once FluidNC took this one
        for _ in range(20):
            self.reply("ok")
            self.assertEqual(self.sent(), [piece])
        self.reply(JOG)
        self.assertEqual((self.log("tx"), self.log("rx")), ([], []))   # pieces and their oks are not shown
        self.assertLessEqual(self.link.next_poll - self.t, 0.1)         # status every 0.1 s while it runs

    def test_keep_and_deadman(self):
        self.ready()
        jog_id, _ = self.running(x=1)
        for _ in range(10):                             # 1 s, held: keeps every 0.1 s
            self.tick(0.1)
            self.reply(JOG)
            self.m.jog({"keep": jog_id})
        self.assertEqual((self.link.hold.id, self.realtime()), (jog_id, b""))
        self.tick(0.3)
        self.reply(JOG)
        self.tick(0.08)                                 # 0.39 s without a keep
        self.assertEqual(self.realtime(), b"")
        self.tick(0.02)
        self.assertEqual(self.realtime(), b"\x85")      # the page went quiet: stopped
        self.assertIsNone(self.link.hold)
        self.assertEqual(self.evs("jog")[-1], {"type": "jog", "jog": None})
        self.refused(lambda: self.m.jog({"keep": jog_id}), "That jog has ended")
        self.assertEqual(self.m.jog({"stop": jog_id}), {})          # stopping it again is fine
        self.assertEqual(self.m.jog({"stop": "all"}), {})
        self.assertEqual(self.realtime(), b"")
        self.stopped()
        self.assertEqual(self.log("info")[-1], "Jog stopped (the page stopped answering): moved 0.5 in")

    def test_stop_cancels_until_a_report_says_it_stopped(self):
        self.ready()
        jog_id, piece = self.running(x=1)
        self.reply("ok")
        self.assertEqual(self.sent(), [piece])          # the second piece waits for room
        self.assertEqual(self.m.jog({"stop": jog_id}), {})
        self.assertEqual(self.realtime(), b"\x85")      # at once, straight to the port
        self.assertEqual(self.evs("jog")[-1], {"type": "jog", "jog": None})
        self.reply(self.IDLE, dt=0.02)                  # too soon to tell, and the piece is not answered yet
        self.reply(JOG, dt=0.04)
        self.assertEqual(self.realtime(), b"")          # still jogging, but not again within 0.1 s
        self.reply(JOG, dt=0.05)
        self.assertEqual(self.realtime(), b"\x85")      # still jogging 0.11 s after the stop: again
        self.reply(self.IDLE, dt=0.06)                  # a piece unanswered may still start a jog
        self.assertIsNotNone(self.link.cancel)
        self.reply("ok")                                # FluidNC dropped the waiting piece
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # no more pieces
        self.reply(JOG, dt=0.03)                        # too soon after that ok
        self.assertEqual(self.realtime(), b"")
        self.reply(JOG, dt=0.03)
        self.assertEqual(self.realtime(), b"\x85")
        self.reply(JOG, dt=0.06)                        # not again within 0.1 s
        self.assertEqual(self.realtime(), b"")
        self.reply(JOG, dt=0.05)
        self.assertEqual(self.realtime(), b"\x85")
        self.assertNotIn("Jog stopped", " ".join(self.log("info")))
        self.reply("<Idle|MPos:25.400,0.000,0.000|FS:0,0>", dt=0.06)
        self.assertIsNone(self.link.cancel)
        self.assertEqual(self.log("info")[-1], "Jog stopped: moved 1 in")
        self.reply(JOG, dt=0.1)
        self.assertEqual(self.realtime(), b"")          # done: no more cancels

    def test_cancel_gives_up_after_30(self):
        self.ready()
        self.running(x=1)
        self.m.realtime("jogcancel")                    # the Stop button: the same
        self.assertIsNone(self.link.hold)
        self.reply("ok")
        for _ in range(40):
            self.reply(JOG, dt=0.11)
        self.assertEqual(self.realtime(), b"\x85" * 30)       # then no '!': it would hold FluidNC if the jog ended
        self.assertEqual(self.log("error"), ["FluidNC still jogs after 30 jog cancels. Press Feed Hold, "
                                             "or Soft Reset if it doesn't stop."])
        self.m.send("G0 X0")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # still waits
        self.stopped()
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G0 X0"])

    def test_stops_on_an_error_reply(self):
        self.ready()
        _, piece = self.running(x=1)
        self.reply("error:15")
        self.assertEqual((self.link.hold, self.realtime()), (None, b"\x85"))
        self.assertEqual(self.log("tx")[-1], piece)     # (shown with the line it answers)
        self.assertEqual(self.log("rx")[-1], "error:15")
        self.stopped()
        self.assertEqual(self.log("info")[-1], "Jog stopped (FluidNC answered error:15): moved 0.5 in")

    def test_stops_on_an_alarm(self):
        self.ready()
        self.running(x=1)
        self.reply("ALARM:1")
        self.assertEqual((self.link.hold, self.realtime()), (None, b"\x85"))
        self.reply("[MSG:ERR: Reset to continue]")
        self.stopped("<Alarm|MPos:12.700,0.000,0.000|FS:0,0>")
        self.assertEqual(self.log("info")[-1], "Jog stopped (ALARM:1): moved 0.5 in")

    def test_stops_on_critical(self):
        self.ready()
        self.running(x=1)
        self.reply("[MSG:ERR: Reset to continue]")
        self.assertEqual((self.link.hold, self.realtime()), (None, b"\x85"))
        self.reply("<Alarm|MPos:12.700,0.000,0.000|FS:0,0>", dt=0.06)   # (a hard limit: the piece is never answered)
        self.assertIsNone(self.link.cancel)
        self.assertEqual(self.log("info")[-1], "Jog stopped (FluidNC needs a Soft Reset): moved 0.5 in")
        self.assertIsNotNone(self.link.inflight)

    def test_stops_on_a_reset_banner(self):
        self.ready()
        self.running(x=1)
        self.reply("ok", JOG)
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")
        self.assertEqual((self.link.hold, self.link.cancel, self.realtime()), (None, None, b""))   # (no cancel needed)
        self.assertEqual(self.evs("jog")[-1], {"type": "jog", "jog": None})
        self.assertEqual(self.log("info")[-2:], ["*** Controller reset", "Jog stopped (FluidNC was reset): moved 0.25 in"])

    def test_stops_on_our_reset(self):
        self.ready()
        self.running(x=1)
        self.m.realtime("reset")
        self.assertEqual((self.link.hold, self.link.cancel, self.realtime()), (None, None, b"\x18"))
        self.assertEqual(self.log("info")[-1], "Jog stopped (Soft Reset): moved 0 in")

    def test_stops_when_status_goes_stale(self):
        self.ready()
        jog_id, _ = self.running(x=1)
        for _ in range(4):
            self.tick(0.1)
            self.m.jog({"keep": jog_id})
        self.assertEqual(self.realtime(), b"")
        self.tick(0.11)                                 # no report for over 0.5 s
        self.assertEqual((self.link.hold, self.realtime()), (None, b"\x85"))
        self.assertEqual(self.link.cancel["why"], "no status from FluidNC for 0.5 s")
        self.assertEqual(self.m.jog({"stop": "all"}), {})

    def test_stops_when_the_state_changes(self):
        self.ready()
        self.running(x=1)
        self.reply(JOG, "<Hold:0|MPos:5.000,0.000,0.000|FS:0,0>")
        self.assertEqual((self.link.hold, self.realtime()), (None, b"\x85"))
        self.stopped()
        self.assertEqual(self.log("info")[-1], "Jog stopped (FluidNC went to Hold): moved 0.5 in")

    def test_stops_on_disconnect(self):
        self.ready()
        self.running(x=1)
        self.m.disconnect()
        self.assertEqual(self.realtime(), b"\x85")      # before letting go of the port
        self.m.link_closed(self.link)                   # (what the reader thread does once closed)
        self.assertEqual([e["type"] for e in self.events[-4:]], ["console", "console", "axes", "conn"])
        self.assertEqual(self.evs("jog")[-1], {"type": "jog", "jog": None})
        self.assertEqual(self.log("info")[-2:], ["Disconnected from /dev/fake", "Jog stopped (disconnected): moved 0 in"])
        self.assertEqual(self.m.snapshot()["axes"], {a: {"maxRate": None, "accel": None, "softLimits": None, "travel": None} for a in "xyz"})

    def test_feed_hold_stops_it_too(self):
        self.ready()
        self.running(x=1)
        self.m.realtime("hold")                         # FluidNC cancels a jog on '!'
        self.assertEqual((self.link.hold, self.realtime()), (None, b"!"))
        self.reply("ok", JOG, dt=0.06)
        self.assertEqual(self.realtime(), b"\x85")
        self.stopped()
        self.assertEqual(self.log("info")[-1], "Jog stopped (Feed Hold): moved 0.5 in")

    def test_everything_else_refused_while_held(self):
        self.ready()
        jog_id, _ = self.running(x=1)
        for call in (lambda: self.m.send("G0 X1"), lambda: self.step(x=1), lambda: self.m.zero("X"),
                     lambda: self.m.zero(undo=True), lambda: self.m.home(""), lambda: self.m.unlock()):
            self.refused(call, JOGGING)
        self.assertEqual(self.m.jog({"keep": jog_id}), {})

    def test_a_new_start_stops_the_old_jog_first(self):
        self.ready()
        a, _ = self.running(x=1)
        b = self.start(y=1)
        self.assertGreater(b, a)
        self.assertEqual(self.realtime(), b"\x85")
        self.assertEqual([e["jog"] and e["jog"]["id"] for e in self.evs("jog")], [a, None, b])
        self.refused(lambda: self.m.jog({"keep": a}), "That jog has ended")
        self.m.jog({"keep": b})
        self.reply("ok")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # b waits until a has stopped
        self.reply(JOG, dt=0.1)
        self.assertEqual(self.realtime(), b"\x85")
        self.reply(IDLE_AT_HALF, dt=0.11)
        self.assertEqual(self.log("info")[-1], "Jog stopped: moved 0.5 in")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$J=G20G91Y0.073F150"])

    def test_refused_like_step_jogs(self):
        self.ready()
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>")
        self.refused(lambda: self.start(x=1), "ALARM: Unlock or Home first")
        self.reply(self.IDLE)
        self.m.send("G4 P1")
        self.tick()
        self.refused(lambda: self.start(x=1), "Waiting for FluidNC to finish 'G4 P1'")
        for direction in ({"x": 0.5}, {"x": 2}, {"x": 0, "y": 0}):
            self.refused(lambda: self.start(**direction),
                         "Jog direction: x, y and z must each be -1, 0 or 1, and not all 0", 400)
        for direction in ({"w": 1}, {}):
            self.refused(lambda: self.start(**direction), "Jog direction: give x, y and/or z", 400)

    def ended_by(self, report, why):
        """A hold jog at speed, then this report: FluidNC stopped it by itself, and no piece may start it again."""
        self.ready()
        jog_id, piece = self.running(x=1)
        self.reply("ok")                                # its first piece runs ...
        self.assertEqual(self.sent(), [piece])
        self.reply("<Jog|MPos:1.000,0.000,0.000|FS:1000,0>", dt=0.06)   # (getting up to speed: fine)
        self.reply(JOG, dt=0.1)                         # ... at 150 in/min
        self.m.jog({"keep": jog_id})
        self.assertEqual((self.link.hold.id, self.realtime()), (jog_id, b""))
        self.reply(report, dt=0.1)
        self.assertEqual((self.link.hold, self.realtime()), (None, b"\x85"))
        self.reply("ok")                                # (the waiting piece, dropped by FluidNC)
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # no more pieces
        self.refused(lambda: self.m.jog({"keep": jog_id}), "That jog has ended")
        self.stopped()
        self.assertEqual(self.log("info")[-1], f"Jog stopped ({why}): moved 0.5 in")

    def test_ends_on_the_feed_hold_pin(self):
        """A feed hold button cancels a jog without FluidNC ever showing Hold."""
        self.ended_by("<Jog|MPos:6.350,0.000,0.000|FS:3810,0|Pn:H>", "FluidNC's feed hold input is on")

    def test_ends_when_fluidnc_slows_it_down(self):
        self.ended_by("<Jog|MPos:6.350,0.000,0.000|FS:1800,0>", "FluidNC slowed it down by itself: feed hold or soft limit?")

    def test_ends_when_fluidnc_does_not_move(self):
        """At a soft limit FluidNC cuts every piece to nothing and answers it at once: no flood of pieces."""
        self.ready()
        self.running(z=1)
        self.reply("ok")
        self.reply(self.IDLE, dt=0.02)                  # too soon after the first ok to tell
        self.assertIsNotNone(self.link.hold)
        for _ in range(5):
            self.assertEqual(len(self.sent()), 1)
            self.reply("ok")
        self.reply(self.IDLE, dt=0.04)
        self.assertEqual((self.link.hold, self.realtime()), (None, b"\x85"))
        self.assertEqual(self.link.cancel["why"], "FluidNC is not moving: at a soft limit?")

    def test_a_big_planner_gets_no_more_pieces_than_16_blocks_would(self):
        self.ready(settings=dict(SETTINGS, **{"$/planner_blocks": "120"}))
        jog_id, piece = self.running(x=1)
        self.assertEqual(piece, "$J=G20G91X0.073F150")  # sized as for 16 blocks
        for _ in range(14):                             # FluidNC answers each at once (its planner has room) ...
            self.reply("ok")
            self.assertEqual(self.sent(), [piece])
        self.reply("ok")
        self.assertEqual(self.sent(), [])               # ... but 15 not yet run are enough
        self.reply("<Jog|MPos:3.708,0.000,0.000|FS:3810,0>")    # 2 pieces done
        self.tick(0.01)
        self.assertEqual(self.sent(), [piece])
        self.reply("ok")
        self.assertEqual(self.sent(), [piece])
        self.reply("ok")
        self.assertEqual(self.sent(), [])
        self.m.jog({"stop": jog_id})
        self.stopped("<Idle|MPos:12.700,0.000,0.000|FS:0,0>")

    def test_no_more_cancels_while_it_slows_down(self):
        """0x85 again only if FluidNC may not have taken the first: not while the speed goes down."""
        self.ready()
        jog_id, _ = self.running(x=1)
        self.reply("ok")
        self.reply(JOG, dt=0.06)                        # 150 in/min
        self.m.jog({"stop": jog_id})
        self.assertEqual(self.realtime(), b"\x85")
        for fs in (3000, 1500, 400, 0):                 # slowing down (in/min * 25.4)
            self.reply(f"<Jog|MPos:8.000,0.000,0.000|FS:{fs},0>", dt=0.11)
            self.assertEqual(self.realtime(), b"", fs)
        self.reply("<Jog|MPos:8.000,0.000,0.000|FS:500,0>", dt=0.11)   # moving faster again: it didn't take it
        self.assertEqual(self.realtime(), b"\x85")
        self.stopped()

    def test_a_cancel_never_overtakes_a_piece(self):
        """Every byte goes out in the order decided: a piece written after a jog cancel would start a new jog."""
        self.ready()
        jog_id, piece = self.running(x=1)
        self.reply("ok")                                # the next piece is decided (not written yet) ...
        self.m.jog({"stop": jog_id})                    # ... and then the stop
        self.assertEqual(self.link.out, [piece.encode() + b"\n", b"\x85"])
        self.m.realtime("hold")
        self.assertEqual(self.link.out[-1], b"!")
        self.assertEqual(self.rt, [])                   # (nothing written past self.out)


    def test_a_line_stuck_after_a_cancel_gets_one_cycle_start(self):
        """FluidNC 3.9 can be left suspended by a jog cancel it reads just as the jog ends by itself: it keeps
        reporting Idle but answers no line until a cycle start ('~')."""
        self.ready()
        self.running(x=1)
        self.m.realtime("jogcancel")
        self.stopped()
        self.realtime()
        self.m.send("G0 X0")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G0 X0"])
        for _ in range(14):                             # unanswered while FluidNC says Idle, for 1.4 s ...
            self.reply(IDLE_AT_HALF, dt=0.1)
            self.tick(0)
        self.assertEqual(self.realtime(), b"")
        for _ in range(10):                             # ... then past 1.5 s: one '~', never more
            self.reply(IDLE_AT_HALF, dt=0.1)
            self.tick(0)
        self.assertEqual(self.realtime(), b"~")
        self.assertIn("sent cycle start (~) to wake it up", self.log("info")[-1])
        self.reply("ok")
        self.assertIsNone(self.link.inflight)

    def test_no_cycle_start_without_a_cancel_or_during_a_dwell(self):
        self.ready()
        self.m.send("G4 P0.1")                          # no jog cancel before it: never
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G4 P0.1"])
        for _ in range(25):
            self.reply(self.IDLE, dt=0.1)
            self.tick(0)
        self.assertEqual(self.realtime(), b"")
        self.reply("ok")
        self.running(x=1)
        self.m.realtime("jogcancel")
        self.stopped()
        self.realtime()
        self.m.send("G4 P2")                            # after a cancel, but a dwell answers late by design
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G4 P2"])
        for _ in range(30):                             # 3 s < 1.5 s + the 2 s dwell
            self.reply(IDLE_AT_HALF, dt=0.1)
            self.tick(0)
        self.assertEqual(self.realtime(), b"")


class StepsAndLinesAfterJogs(Stage2):
    def test_lines_wait_until_a_report_shows_the_jog_stopped(self):
        self.ready()
        self.step(x=1)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$J=G20G91X1F150"])
        self.m.send("G0 X0")
        self.m.send("$J=G20G91X-1F100")                 # (typed jogs are jogs too)
        self.reply("ok")                                # planned; FluidNC starts it just after this ok
        self.assertEqual(self.sent(), [])
        self.reply(self.IDLE, dt=0.02)                  # too soon to tell
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.reply(JOG, dt=0.1)
        self.tick(0.6)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.log("info")[-2:], ["Waiting to send: G0 X0  (until the jog has stopped)",
                                                 "Waiting to send: $J=G20G91X-1F100  (after 'G0 X0')"])
        self.reply(IDLE_AT_HALF, dt=0.1)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G0 X0"])
        self.reply("ok")
        self.assertEqual(self.sent(), ["$J=G20G91X-1F100"])     # a jog after G-code goes once that is answered

    def test_jogs_follow_jogs_at_once_and_g20_waits_too(self):
        self.go_live()
        self.reply(self.IDLE)
        self.step(x=1)
        self.step(y=1)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$J=G20G91X1F150"])      # (the step has its own G20)
        self.reply("ok")
        self.assertEqual(self.sent(), ["$J=G20G91Y1F150"])
        self.reply("ok", JOG)
        self.tick()
        self.assertEqual(self.sent(), [])               # FluidSender's G20 is G-code too
        self.reply(self.IDLE, dt=0.1)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])

    def test_stop_drops_the_steps_not_sent_and_new_ones_wait(self):
        self.ready()
        for _ in range(3):
            self.step(x=1)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$J=G20G91X1F150"])
        self.m.realtime("jogcancel")
        self.assertEqual(self.realtime(), b"\x85")
        self.assertEqual(list(self.link.queue), [])
        self.assertIn("Dropped 2 jog lines not sent yet", self.log("info"))
        self.step(y=1)                                  # clicked while it stops: it goes once stopped
        self.reply("ok")
        self.tick(0.01)
        self.tick(0.6)
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.log("info")[-1], "Waiting to send: $J=G20G91Y1F150  (until the jog has stopped)")
        self.reply(self.IDLE, dt=0.06)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$J=G20G91Y1F150"])


class ZeroHomeUnlock(Stage2):
    # WCO -140 -100 -32 mm: G54 (a zero counts a G92 or tool length offset on top in; Undo Zero's G10 L2 puts the
    # WCO back as G54's offset, right while there is none: FluidSender sets none)
    BEFORE = "<Idle|MPos:-127.300,-50.800,-6.600|FS:0,0|WCO:-140.000,-100.000,-32.000>"   # work X 0.5 Y 1.937 Z 1 in
    AFTER = "<Idle|MPos:-127.300,-50.800,-6.600|FS:0,0|WCO:-127.300,-50.800,-32.000>"     # X Y zeroed there
    MOVED = "<Idle|MPos:-100.000,-40.000,-6.600|FS:0,0>"

    def zero(self, axes, value=None, before=BEFORE, reply="ok"):
        self.reply(before, dt=0.06)
        line = self.m.zero(axes, value)["line"]
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply(reply)
        return line

    def test_zero_lines(self):
        self.ready()
        for axes, value, line in (("XY", 0, "G10 L20 P0 G20 X0 Y0"), ("yx", None, "G10 L20 P0 G20 X0 Y0"),
                                  ("z", -0.00001, "G10 L20 P0 G20 Z0"), ("Z", 1.5, "G10 L20 P0 G20 Z1.5"),
                                  ("XYZ", -0.25, "G10 L20 P0 G20 X-0.25 Y-0.25 Z-0.25"), ("x", 100, "G10 L20 P0 G20 X100")):
            self.assertEqual(self.zero(axes, value), line)
        self.assertEqual(self.log("tx")[-1], "G10 L20 P0 G20 X100")
        for axes, value in (("", 0), ("XX", 0), ("XA", 0), (None, 0), (5, 0), ("X", 100.01), ("X", "1"),
                            ("X", float("nan")), ("X", True)):
            with self.assertRaises(MachineError, msg=repr((axes, value))) as cm:
                self.m.zero(axes, value)
            self.assertEqual(cm.exception.status, 400)

    def test_zero_is_logged_and_undone_exactly(self):
        self.ready()
        self.zero("XY")
        self.assertEqual(self.log("info")[-1], "Zeroed X Y. Work position before: X 0.5 Y 1.937 (in)")
        info = {"axes": "XY", "text": "X 0.5 Y 1.937", "wcs": "G54", "source": "page", "count": 1}
        evs = self.evs("zero")
        self.assertEqual([(e["type"], dict(e["zeroUndo"], t=None)) for e in evs], [("zero", dict(info, t=None))])
        self.assertAlmostEqual(evs[0]["zeroUndo"]["t"], time.time(), delta=5)
        self.assertEqual(dict(self.m.snapshot()["zeroUndo"], t=None), dict(info, t=None))
        self.reply(self.AFTER, self.MOVED, dt=0.06)
        self.assertEqual(self.m.zero(undo=True), {"line": "G10 L2 P1 G20 X-5.511811 Y-3.937008"})
        # (G10 L2: G54's X and Y offsets are the WCO from before again, -140 and -100 mm, wherever the machine is)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G10 L2 P1 G20 X-5.511811 Y-3.937008"])
        self.assertEqual(len(self.m.zeros), 1)          # used up only by its ok
        self.reply("ok")
        self.assertEqual(self.evs("zero")[-1], {"type": "zero", "zeroUndo": None})
        self.assertIsNone(self.m.snapshot()["zeroUndo"])
        self.refused(lambda: self.m.zero(undo=True), "No zero to undo")

    def test_undo_with_reports_in_inches(self):
        self.ready()
        self.zero("XY", before="<Idle|MPos:-5.0118,-2.0000,-0.2598|FS:0,0|WCO:-5.5118,-3.9370,-1.2598>")
        self.assertEqual(self.log("info")[-1], "Zeroed X Y. Work position before: X 0.5 Y 1.937 (in)")
        self.reply("<Idle|MPos:-3.9370,-1.5748,-0.2598|FS:0,0|WCO:-5.0118,-2.0000,-1.2598>", dt=0.06)
        self.assertEqual(self.m.zero(undo=True)["line"], "G10 L2 P1 G20 X-5.511800 Y-3.937000")

    def test_set_to_a_value_and_errors(self):
        self.ready()
        self.zero("Z", 1.25)
        self.assertEqual(self.log("info")[-1], "Set Z to 1.25 in. Work position before: Z 1 (in)")
        self.zero("X", reply="error:9")                 # an error: nothing to undo for it
        self.assertEqual([r["axes"] for r in self.m.zeros], ["Z"])
        self.reply(self.IDLE, dt=0.06)
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")    # a reset keeps it (FluidNC keeps G54)
        self.assertEqual([r["axes"] for r in self.m.zeros], ["Z"])
        self.assertEqual(self.evs("zero")[-1]["zeroUndo"]["axes"], "Z")

    def test_zero_refused(self):
        self.ready()
        for report, why in (("<Alarm|MPos:0,0,0|FS:0,0>", "ALARM: Unlock or Home first"),
                            ("<Run|MPos:0,0,0|FS:100,0>", "Running: wait until FluidNC is Idle"),
                            ("<Hold:0|MPos:0,0,0|FS:0,0>", "Hold: Resume or Soft Reset first"),
                            ("<Home|MPos:0,0,0|FS:0,0>", "Homing: wait until it is done"),
                            ("<Door:0|MPos:0,0,0|FS:0,0>", "The safety door is open")):
            self.reply(report)
            self.refused(lambda: self.m.zero("X"), why)
        self.reply("[MSG:ERR: Reset to continue]")
        self.refused(lambda: self.m.zero("X"), CRITICAL)
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]", self.IDLE)
        self.link.parser.wco = None
        self.refused(lambda: self.m.zero("X"), "Work offset not known yet; try again in a second")
        self.m.link = None
        self.refused(lambda: self.m.zero("X"), "Not connected")

    def test_zero_keeps_the_users_millimeters(self):
        """A units word in a G10 stays in force: after the user's G21 the zero is written in millimeters."""
        self.ready()
        self.m.send("G21")
        self.tick()
        self.assertEqual(self.sent(), ["G21"])
        self.reply("ok")
        self.assertEqual(self.zero("X"), "G10 L20 P0 G21 X0")
        self.assertEqual(self.zero("Z", 1.5), "G10 L20 P0 G21 Z38.1")
        self.assertEqual(self.zero("Y", -0.0001), "G10 L20 P0 G21 Y-0.00254")
        self.assertEqual(self.link.modal["units"], "G21")
        self.assertEqual(self.log("info")[-1], "Set Y to -0.0001 in. Work position before: Y 1.937 (in)")
        self.reply("<Idle|MPos:-127.300,-50.800,-6.600|FS:0,0>", dt=0.06)
        self.assertEqual(self.m.zero(undo=True)["line"], "G10 L2 P1 G21 Y-100")
        self.assertEqual(machine.undo_line("X", [-2.54, 0, 0]), "G10 L2 P1 G20 X-0.100000")
        self.assertEqual(machine.undo_line("X", [-2.54, 0, 0], 1, "G21"), "G10 L2 P1 G21 X-2.54")
        self.assertEqual(machine.undo_line("XZ", [-2.54, 0, 0.00001], 3), "G10 L2 P3 G20 X-0.100000 Z0.000000")

    def test_zero_shows_the_new_position_at_once(self):
        """FluidNC sends WCO only in every 10th report: after a zero's ok it is worked out here (amendment C)."""
        self.ready()
        self.zero("X")
        st = self.evs("status")[-1]
        self.assertEqual(st["wpos"], [0.0, 1.937008, 1.0])          # at once (WCO = MPos - the work position)
        self.assertEqual(self.link.parser.wco[:3], [-127.3, -100.0, -32.0])
        still = "<Idle|MPos:-127.300,-50.800,-6.600|FS:0,0>"       # (no WCO in it)
        self.zero("XYZ", before=still)                  # the next zero's undo record starts from there
        self.assertEqual(self.log("info")[-1], "Zeroed X Y Z. Work position before: X 0 Y 1.937 Z 1 (in)")
        self.reply(still, dt=0.06)
        self.assertEqual(self.evs("status")[-1]["wpos"], [0.0, 0.0, 0.0])
        self.assertEqual(self.m.zero(undo=True)["line"], "G10 L2 P1 G20 X-5.011811 Y-3.937008 Z-1.259843")
        self.tick(0.01)
        self.sent()
        self.reply("ok")
        self.assertEqual(self.evs("status")[-1]["wpos"], [0.0, 1.937008, 1.0])

    def test_zero_waits_for_no_line_of_the_users(self):
        """G10 L20 counts from where the lines before it leave the machine (FluidNC stays Idle during a G4)."""
        self.ready()
        self.m.send("G4 P2")
        self.tick()
        self.assertEqual(self.sent(), ["G4 P2"])
        self.m.send("G0 X1")
        self.refused(lambda: self.m.zero("X"), "Waiting for FluidNC to finish 'G4 P2'")
        self.reply("ok")
        self.assertEqual(self.sent(), ["G0 X1"])
        self.refused(lambda: self.m.zero("X"), "Waiting for FluidNC to finish 'G0 X1'")
        self.reply("ok")
        self.refused(lambda: self.m.zero("X"), "FluidNC is busy; try again in a second")   # no report since
        self.reply("<Run|MPos:10.000,0.000,0.000|FS:5000,0>")
        self.refused(lambda: self.m.zero("X"), "Running: wait until FluidNC is Idle")
        self.reply(self.BEFORE)
        self.assertEqual(self.m.zero("X")["line"], "G10 L20 P0 G20 X0")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G10 L20 P0 G20 X0"])
        self.reply("ok")
        self.assertEqual(self.m.zero("Y")["line"], "G10 L20 P0 G20 Y0")     # (a G10 doesn't move: no wait)

    def test_zero_while_a_step_jogs_waits_for_it(self):
        self.ready()
        self.do_step(x=1)
        self.step(x=1)
        self.tick(0.01)
        self.sent()
        self.reply("ok", JOG)
        self.assertEqual(self.m.zero("X")["line"], "G10 L20 P0 G20 X0")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.reply(IDLE_AT_HALF, dt=0.06)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G10 L20 P0 G20 X0"])
        self.reply("ok")
        self.assertEqual(self.log("info")[-1], "Zeroed X. Work position before: X 0.5 (in)")

    def test_undo_refused(self):
        self.ready()
        self.zero("X")
        self.reply("<Run|MPos:0,0,0|FS:100,0>", dt=0.06)
        self.refused(lambda: self.m.zero(undo=True), "Running: wait until FluidNC is Idle")
        self.reply(self.IDLE)
        self.m.send("G4 P1")
        self.tick()
        self.assertEqual(self.sent(), ["G4 P1"])
        self.refused(lambda: self.m.zero(undo=True), "Waiting for FluidNC to finish 'G4 P1'")
        self.reply("ok")
        self.refused(lambda: self.m.zero(undo=True), "FluidNC is busy; try again in a second")   # no report since
        self.reply(self.IDLE)
        self.do_step(x=1)
        self.step(x=1)
        self.tick(0.01)
        self.refused(lambda: self.m.zero(undo=True), "Jogging: wait until it has stopped")
        self.reply("ok")
        self.refused(lambda: self.m.zero(undo=True), "FluidNC is busy; try again in a second")
        self.reply(self.IDLE, dt=0.06)
        self.assertTrue(self.m.zero(undo=True)["line"].startswith("G10 L2 P1 G20 X"))
        self.m.link_closed(self.link)                   # (nothing: link.stopping isn't set)
        self.link.stopping = ("Disconnected from /dev/fake", "info")
        n = len(self.evs("zero"))
        self.m.link_closed(self.link)                   # (the undo's line is lost, the record kept: FluidNC keeps G54)
        self.assertEqual((len(self.evs("zero")), [r["axes"] for r in self.m.zeros]), (n, ["X"]))

    def test_home(self):
        self.ready()
        for axes, line in (("", "$H"), (None, "$H"), ("x", "$HX"), ("Y", "$HY"), ("z", "$HZ")):
            self.assertEqual(self.m.home(axes), {"line": line})
            self.tick(0.01)
            self.assertEqual(self.sent(), [line])
            self.assertEqual(self.link.inflight.timeout, machine.LONG_TIMEOUT)
            self.reply("<Home|MPos:0,0,0|FS:0,0>")
            self.refused(lambda: self.m.home(""), "Homing: wait until it is done")
            self.reply(self.IDLE, "ok", self.IDLE)
        self.assertEqual(self.log("tx")[-1], "$HZ")
        for axes in ("XY", "A", 5, "$H"):
            self.refused(lambda: self.m.home(axes), "Home which axis? '' for all, or one of X, Y, Z", 400)
        self.reply("ALARM:14", "<Alarm|MPos:0,0,0|FS:0,0>")
        self.assertEqual(self.m.home("z")["line"], "$HZ")           # in Alarm too
        self.refused(lambda: self.m.home(""), "Waiting for FluidNC to finish '$HZ'")
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]", "<Alarm|MPos:0,0,0|FS:0,0>")
        self.reply("[MSG:ERR: Reset to continue]")
        self.refused(lambda: self.m.home(""), CRITICAL)
        self.reply("Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]")
        self.reply(self.IDLE, dt=0.06)
        self.do_step(x=1)
        self.step(x=1)
        self.tick(0.01)
        self.reply("ok")
        self.refused(lambda: self.m.home(""), "Jogging: wait until it has stopped")
        self.reply(JOG, dt=0.06)
        self.refused(lambda: self.m.home(""), "Jogging: wait until it has stopped")

    def test_unlock(self):
        self.ready()
        self.refused(self.m.unlock, "Not in ALARM: nothing to unlock")
        self.reply("ALARM:14", "[MSG:INFO: '$H'|'$X' to unlock]", "<Alarm|MPos:0,0,0|FS:0,0>")
        self.assertEqual(self.m.unlock(), {"line": "$X"})
        self.refused(self.m.unlock, "Waiting for FluidNC to finish '$X'")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$X"])
        self.reply("[MSG:INFO: Caution: Unlocked]", "ok")
        self.assertIsNone(self.link.alarm)
        self.reply("ALARM:1", "[MSG:ERR: Reset to continue]", "<Alarm|MPos:0,0,0|FS:0,0>")
        self.refused(self.m.unlock, CRITICAL)


class Alarms(Stage2):
    BANNER = "Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"

    def test_codes_critical_and_clearing(self):
        """In FluidNC 3.9.4's order: a hard limit, then Soft Reset: ALARM:14 comes just before the banner."""
        self.ready()
        self.m.send("G0 X1")
        self.tick()
        self.sent()
        self.reply("[MSG:INFO: ALARM: Hard Limit]", "ALARM:1")
        self.assertEqual(self.link.alarm, {"code": 1, "critical": False, "text": "ALARM:1", "outputs": False})
        self.assertIsNotNone(self.link.inflight)        # an ALARM never answers the line in flight
        for _ in range(6):                              # FluidNC sleeps 0.5 s after printing it, and its
            self.reply("<Run|MPos:0,0,0|FS:0,0>", dt=0.1)   # reports meanwhile still say Run
        self.assertEqual(self.link.alarm, {"code": 1, "critical": False, "text": "ALARM:1", "outputs": False})
        self.reply("[MSG:ERR: Reset to continue]", "<Alarm|MPos:0,0,0|FS:0,0>")
        crit = {"code": 1, "critical": True, "text": "ALARM:1", "outputs": False}
        self.assertEqual(self.link.alarm, crit)
        self.assertEqual(self.m.snapshot()["alarm"], crit)
        self.reply("[MSG:INFO: ALARM: Unhomed]", "ALARM:14", self.BANNER, "<Alarm|MPos:0,0,0|FS:0,0>")
        unhomed = {"code": 14, "critical": False, "text": "ALARM:14", "outputs": False}
        self.assertEqual(self.link.alarm, unhomed)      # the banner keeps the alarm the reset left it in
        self.reply("[MSG:INFO: Caution: Unlocked]")
        self.reply("ALARM:2")
        self.reply("<Idle|MPos:0,0,0|FS:0,0>", dt=0.6)  # (still FluidNC's pause after the ALARM)
        self.assertEqual(self.link.alarm["code"], 2)
        self.reply("<Idle|MPos:0,0,0|FS:0,0>", dt=0.11)  # a report in another state after it clears it
        self.reply("[MSG:INFO: '$H'|'$X' to unlock]")   # (in alarm, no number given)
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>", "[MSG:ERR: Reset to continue]")
        hint = "[MSG:INFO: '$H'|'$X' to unlock]"
        self.reply(self.BANNER)                         # a reset always ends critical ...
        self.reply("<Idle|MPos:0,0,0|FS:0,0>")          # ... and FluidNC went Idle (all axes homed)
        self.assertEqual([e["alarm"] for e in self.evs("alarm")],
                         [{"code": 1, "critical": False, "text": "ALARM:1", "outputs": False}, crit, unhomed, None,
                          {"code": 2, "critical": False, "text": "ALARM:2", "outputs": False}, None,
                          {"code": None, "critical": False, "text": hint, "outputs": False},
                          {"code": None, "critical": True, "text": hint, "outputs": False},
                          {"code": None, "critical": False, "text": hint, "outputs": False}, None])
        self.reply("ALARM:3")                           # Ctrl-X while moving: ALARM:3, Run reports for 0.5 s,
        for _ in range(4):
            self.reply("<Run|MPos:0,0,0|FS:0,0>", dt=0.1)
        self.reply(self.BANNER, "<Alarm|MPos:0,0,0|FS:0,0>")    # then the banner
        self.assertEqual(self.link.alarm, {"code": 3, "critical": False, "text": "ALARM:3", "outputs": False})
        self.m.realtime("reset")
        self.refused(self.m.unlock, "FluidNC is being reset; try again in a second")

    def test_connected_in_a_critical_alarm(self):
        """Connected after a hard limit: "Reset to continue" came before, but FluidNC refuses $I only then."""
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>")
        self.link.went_live(self.t)
        self.assertEqual(self.sent(), ["$I"])
        self.reply("error:8")
        self.assertEqual(self.sent(), ["$G"])
        self.assertEqual(self.link.alarm, {"code": None, "critical": True, "text": "$I: error:8", "outputs": False})
        self.assertEqual(self.evs("alarm")[-1]["alarm"], self.link.alarm)
        self.assertIn("FluidNC refused $I: it is in a critical alarm (hard or soft limit), so only a Soft Reset "
                      "gets out of it", self.log("info"))
        self.refused(self.m.unlock, CRITICAL)

    def test_snapshot_when_not_connected(self):
        snap = self.m.snapshot()
        self.assertEqual((snap["connected"], snap["axes"], snap["jog"], snap["zeroUndo"], snap["alarm"]),
                         (False, {a: {"maxRate": None, "accel": None, "softLimits": None, "travel": None} for a in "xyz"}, None, None, None))


BANNER = "Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"
HOLD0 = "<Hold:0|MPos:0,0,0|FS:0,0>"
RUN = "<Run|MPos:1.000,0.000,0.000|FS:500,0>"


class Leftover(Stage2):
    """FluidNC 3.9.4 runs a line left from before its reset: its poller hands the next line to the main loop
    also while it holds (Hold:0), and a reset keeps that line (Protocol.cpp). Its reply comes after the banner,
    before FluidSender has sent anything since: FluidSender holds it at once and resets again once it has
    stopped (once a stop), and nothing goes before a report shows FluidNC ran nothing from before."""

    def held_with_a_line(self):
        """A move held (Hold:0), and a line typed meanwhile: FluidNC takes it, to run it after a Resume."""
        self.ready()
        self.m.send("G1 X5 F10")
        self.tick(0.01)
        self.sent()
        self.reply("ok", RUN)
        self.m.realtime("hold")
        self.reply(HOLD0, dt=0.3)
        self.m.send("G0 X1")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G0 X1"])        # (no reply in Hold:0)
        self.realtime()

    def test_held_at_once_and_reset_again(self):
        self.held_with_a_line()
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")      # (held already: the reset)
        self.reply(BANNER)
        self.assertIn("Dropped 1 line FluidNC had not answered", self.log("info"))
        self.reply("ok")                                # G0 X1 ran after the reset (in millimeters)
        self.assertEqual(self.realtime(), b"!")         # held at once
        self.assertEqual(self.log("error")[-1], machine.LEFTOVER)
        self.tick()
        self.assertEqual(self.sent(), [])               # nothing goes meanwhile (no G20)
        self.reply(RUN, dt=0.02)                        # (its move had started)
        self.reply("<Hold:1|MPos:1.000,0.000,0.000|FS:100,0>", dt=0.06)
        self.assertEqual(self.realtime(), b"")          # slowing down: no reset yet
        self.refused(lambda: self.m.realtime("resume"), machine.NO_RESUME)
        self.reply(HOLD0, dt=0.3)
        self.assertEqual(self.realtime(), b"\x18")      # stopped: the second reset (a clean one)
        self.assertEqual(self.log("info")[-1], "Stopped: FluidNC held, then Soft Reset (the position is kept)")
        self.reply(BANNER)
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])          # inches again, as after any reset
        self.assertEqual((self.link.strays, self.log("error")), (0, [machine.LEFTOVER]))

    def test_moving_or_a_spindle_turning_after_a_reset_is_held_too(self):
        self.ready()
        self.m.realtime("reset")
        self.realtime()
        self.reply(BANNER)
        self.reply(RUN, dt=0.02)                        # nothing sent since the banner, yet it moves
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual(self.log("error")[-1], machine.LEFTOVER)
        self.reply(HOLD0, dt=0.3)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(BANNER)
        self.reply(self.IDLE, dt=0.06)
        self.assertEqual(self.link.strays, 0)
        self.tick()
        self.sent()
        self.reply("ok", self.IDLE)                     # (G20)
        self.m.realtime("reset")
        self.realtime()
        self.reply(BANNER)
        self.reply("<Idle|MPos:0,0,0|FS:0,12000>", dt=0.06)    # an M3 S12000 from before ran: spinning up
        self.assertEqual(self.realtime(), b"!")
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")      # (the reset turns it off)

    def test_a_refused_line_from_before_ran_nothing(self):
        self.ready()
        self.m.realtime("reset")
        self.realtime()
        self.reply(BANNER, "error:9")
        self.assertEqual(self.log("info")[-1], "FluidNC refused a line left from before the reset (error:9 (locked "
                                               "out during alarm or jog)): nothing ran")
        self.assertEqual(self.realtime(), b"")
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])

    def test_reset_again_once_only(self):
        """If the second reset leaves FluidNC running a line again, it is only held: FluidSender never resets it
        over and over by itself. The user's Soft Reset starts over."""
        self.ready()
        self.m.realtime("reset")
        self.reply(BANNER, "ok")
        self.assertEqual(self.realtime(), b"\x18!")
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(self.IDLE, dt=0.06)                  # (a report before that reset's banner: it tells nothing)
        self.reply(BANNER, "ok")                        # again (FluidNC shouldn't, but)
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual(self.log("error")[-1], "FluidNC ran a line left from before the reset again: held it. Press "
                                                "Soft Reset when ready (FluidSender doesn't reset it again by itself)")
        self.reply(HOLD0, dt=0.06)
        self.tick(11)
        self.assertEqual(self.realtime(), b"")          # held, not reset
        self.tick()
        self.assertEqual(self.sent(), [])               # and nothing goes
        self.reply(RUN, dt=0.1)                         # (resumed at FluidNC itself)
        self.assertEqual(self.realtime(), b"!")         # held again, quietly
        self.assertEqual(self.log("error").count(machine.LEFTOVER), 1)
        self.m.realtime("reset")                        # the user's Soft Reset
        self.reply(BANNER)
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.assertEqual(self.link.strays, 0)

    def test_nothing_goes_before_a_report_a_moment_after_the_banner(self):
        """A reply that comes behind a report made just after the banner is still a line from before."""
        self.go_live()
        self.reply(BANNER)                              # (FluidNC's own reset button)
        self.reply(self.IDLE)                           # too soon after it to tell
        self.tick()
        self.assertEqual(self.sent(), [])
        self.m.send("$G")
        self.tick()
        self.tick(0.6)
        self.assertEqual(self.log("info")[-1], "Waiting to send: $G  (until FluidNC reports Idle after its reset)")
        self.reply("ok")
        self.assertEqual(self.log("error")[-1], machine.LEFTOVER)


class HeldAtConnect(Stage2):
    """Connected while FluidNC holds (lines from before may wait there, one of them handed over already): no
    reads or lines go until a reset or until FluidNC is Idle again, and Resume is refused."""

    def test_nothing_goes_and_no_resume_until_a_reset(self):
        self.reply("<Hold:0|MPos:0,0,0|FS:0,0|WCO:0,0,0>")
        self.link.went_live(self.t)
        self.assertEqual(self.sent(), [])               # no $I, $G or settings reads
        self.assertEqual(self.log("error"), [machine.OLD_HOLD])
        self.refused(lambda: self.m.realtime("resume"), machine.OLD_HOLD)
        self.refused(self.m.pause_toggle, machine.OLD_HOLD)
        self.refused(lambda: self.m.goto("z"), machine.OLD_HOLD)   # (the buttons say why too)
        self.assertEqual(self.realtime(), b"")
        self.m.send("$X")
        self.tick()
        self.tick(0.6)
        self.assertEqual(self.sent(), [])
        self.assertIn("Waiting to send: $X  (until FluidNC is reset: it holds lines from before)", self.log("info"))
        self.m.realtime("reset")                        # Soft Reset clears them
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(BANNER)
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["$I"])           # the reads now
        self.assertIsNone(self.link.old_hold)

    def test_the_line_taken_during_that_hold_runs_after_the_reset(self):
        """The usual case: one of those lines had been handed over already, and runs after the Soft Reset (see
        Leftover). The reads go once the second reset is clean."""
        self.reply("<Hold:0|MPos:0,0,0|FS:0,0|WCO:0,0,0>")
        self.link.went_live(self.t)
        self.m.realtime("reset")
        self.reply(BANNER, "ok")                        # the line from before ran
        self.assertEqual(self.realtime(), b"\x18!")
        self.refused(lambda: self.m.realtime("resume"), machine.NO_RESUME)
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(BANNER)
        self.reply(self.IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["$I"])           # (not dropped by the second reset)
        self.reply("[VER:3.7 FluidNC v3.9.4:]", "ok")
        self.assertEqual(self.sent(), ["$G"])

    def test_reads_once_idle_again(self):
        self.reply("<Door:0|MPos:0,0,0|FS:0,0|WCO:0,0,0>")
        self.link.went_live(self.t)
        self.assertEqual(self.sent(), [])
        self.reply("<Run|MPos:0,0,0|FS:100,0>")         # (resumed at FluidNC: its lines run)
        self.tick()
        self.assertEqual(self.sent(), [])
        self.reply(self.IDLE)                           # done
        self.tick()
        self.assertEqual(self.sent(), ["$I"])


class ReaderSafety(unittest.TestCase):
    """A bug in the reader thread must close the port and tell the pages, not leave them showing a live
    machine that no longer answers. Uses a pty pair made here."""

    def test_internal_error_closes_the_port_and_says_so(self):
        master, slave = os.openpty()
        tty.setraw(slave)
        m = machine.Machine()
        events = []
        m.emit = events.append
        link = machine.Link(m, slave, os.ttyname(slave), 115200)
        m.link = link
        link.connected = True
        try:
            with mock.patch.object(link, "received", side_effect=RuntimeError("bug")), \
                    mock.patch.object(machine.traceback, "print_exc"):
                link.start()
                os.write(master, b"ok\n")
                link.thread.join(5)
        finally:
            os.close(master)
        self.assertFalse(link.thread.is_alive())
        self.assertIsNone(m.link)
        conn = [e for e in events if e["type"] == "conn"]
        self.assertEqual(len(conn), 1)
        self.assertFalse(conn[0]["connected"])
        self.assertIn("after an internal error in FluidSender (RuntimeError('bug'))", conn[0]["message"])


class Ports(unittest.TestCase):
    def fake_sys(self, root, name, usb_dir, ids):
        """A /sys/class/tty/<name>/device link like the kernel's, with the USB ids in usb_dir."""
        iface = os.path.join(root, "devices", usb_dir, usb_dir + ":1.0")
        os.makedirs(iface)
        for key, value in ids.items():
            with open(os.path.join(root, "devices", usb_dir, key), "w") as f:
                f.write(value + "\n")
        os.makedirs(os.path.join(root, "class", "tty", name))
        os.symlink(iface, os.path.join(root, "class", "tty", name, "device"))

    def test_labels(self):
        with tempfile.TemporaryDirectory() as root:
            self.fake_sys(root, "ttyUSB0", "2-2", {"idVendor": "1a86", "idProduct": "7523", "product": "USB Serial"})
            self.fake_sys(root, "ttyACM0", "2-1.4", {"idVendor": "2341", "idProduct": "8036", "product": "Arduino Leonardo"})
            self.fake_sys(root, "ttyUSB1", "2-3", {"idVendor": "0403", "idProduct": "6001", "product": "FT232R USB UART"})
            sys_tty = os.path.join(root, "class", "tty")
            usb0 = machine.describe_port("/dev/ttyUSB0", sys_tty)
            self.assertEqual(usb0, {"path": "/dev/ttyUSB0", "label": "CH340 USB serial (FluidNC?)", "kind": "usb-serial",
                                    "vid": "1a86", "pid": "7523", "product": "USB Serial", "inUseBy": None})
            acm = machine.describe_port("/dev/ttyACM0", sys_tty)
            self.assertEqual((acm["label"], acm["kind"]), ("Arduino Leonardo (Z probe)", "usb-acm"))
            self.assertEqual(machine.describe_port("/dev/ttyUSB1", sys_tty)["label"], "FT232R USB UART (0403:6001)")
            bt = machine.describe_port("/dev/rfcomm0", sys_tty)
            self.assertEqual((bt["label"], bt["kind"], bt["vid"]), ("Bluetooth (pendant)", "bluetooth", None))
            self.assertEqual(machine.describe_port("/dev/ttyUSB7", sys_tty)["label"], "USB serial")

    def test_kinds(self):
        self.assertEqual(machine.port_kind("/dev/ttyUSB3"), "usb-serial")
        self.assertEqual(machine.port_kind("/dev/ttyACM0"), "usb-acm")
        self.assertEqual(machine.port_kind("/dev/rfcomm2"), "bluetooth")
        self.assertEqual(machine.port_kind("/dev/pts/4"), "pty")
        self.assertEqual(machine.port_kind("/dev/ttyS0"), "other")

    def test_only_serial_ports_can_be_opened(self):
        with tempfile.NamedTemporaryFile(prefix="ttyX") as f:          # not a device: harmless even if opened
            with self.assertRaises(MachineError) as cm:
                machine.Machine().connect(f.name)
        self.assertIn("is not a USB or Bluetooth serial port", str(cm.exception))

    def test_real_port_list_reads_only(self):
        """The real list (from /dev, /sys and /proc only: nothing is opened) has the promised shape."""
        data = machine.Machine().ports()
        self.assertEqual(set(data), {"ports", "default"})
        for p in data["ports"]:
            self.assertEqual(set(p), {"path", "label", "kind", "vid", "pid", "product", "inUseBy"})
            self.assertIn(p["kind"], ("usb-serial", "usb-acm", "bluetooth"))
            if (p["vid"], p["pid"]) == ("1a86", "7523"):
                self.assertEqual(p["label"], "CH340 USB serial (FluidNC?)")
        if data["default"]:
            self.assertTrue(data["default"].startswith("/dev/ttyUSB"))


class Holders(unittest.TestCase):
    """A child process holds one of our own ptys open; the port must be refused, naming it."""

    def setUp(self):
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        os.close(slave)
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import os, sys, time; os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY); "
             "print('open', flush=True); time.sleep(30)", self.path], stdout=subprocess.PIPE, text=True)
        self.assertEqual(self.child.stdout.readline().strip(), "open")
        self.machine = machine.Machine(extra_ports=[self.path], only_extra=True)

    def tearDown(self):
        self.child.kill()
        self.child.wait()
        self.child.stdout.close()
        self.machine.shutdown()
        os.close(self.master)

    def test_scan_names_the_holder(self):
        self.assertEqual(machine.scan_holders({self.path}), {self.path: [self.child.pid]})
        name = rf"python3[.\d]* \(pid {self.child.pid}\)"
        self.assertRegex(machine.process_name(self.child.pid), "^" + name + "$")
        ports = self.machine.ports()["ports"]
        self.assertRegex(ports[0]["inUseBy"], "^" + name + "$")

    def test_connect_is_refused(self):
        with self.assertRaises(MachineError) as cm:
            self.machine.connect(self.path)
        self.assertEqual(cm.exception.status, 409)
        self.assertRegex(str(cm.exception), rf"^Port {self.path} is in use by python3[.\d]* \(pid {self.child.pid}\)\. "
                                            r"Disconnect it there first\.$")
        self.assertIsNone(self.machine.link)

    def test_no_answer_releases_the_port(self):
        self.child.kill()
        self.child.wait()
        with mock.patch.object(machine, "FIRST_STATUS", 0.3):
            t0 = time.monotonic()
            with self.assertRaises(MachineError) as cm:
                self.machine.connect(self.path)       # nothing answers on our master side
        self.assertIn("No answer from", str(cm.exception))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertIsNone(self.machine.link)
        os.close(os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))   # closed and not exclusive any more
        os.set_blocking(self.master, False)
        self.assertTrue(os.read(self.master, 100).startswith(b"\x0c?"))     # ^L (echo mode off), then '?'

    def test_probe_and_pendant_need_force(self):
        self.child.kill()
        self.child.wait()
        with mock.patch.object(machine, "port_kind", return_value="usb-acm"), mock.patch.object(machine, "FIRST_STATUS", 0.3):
            with self.assertRaises(MachineError) as cm:
                self.machine.connect(self.path)
            self.assertIn("looks like the Z probe", str(cm.exception))
            with self.assertRaises(MachineError) as cm:
                self.machine.connect(self.path, force=True)
            self.assertIn("No answer from", str(cm.exception))   # past the check; nothing answers here

    def test_only_test_ports_in_test_mode(self):
        with self.assertRaises(MachineError) as cm:
            self.machine.connect("/dev/ttyUSB99")     # (a port that doesn't exist, just in case)
        self.assertIn("not one of the test ports", str(cm.exception))
        with self.assertRaises(MachineError):
            self.machine.connect(self.path, baud=12345)

    def test_real_ports_listed_but_never_opened(self):
        m = machine.Machine(extra_ports=[self.path], open_only_extra=True)
        listed = [p["path"] for p in m.ports()["ports"]]          # (listing only reads /dev, /sys and /proc)
        self.assertEqual(listed[-1], self.path)
        self.assertTrue(all(p.startswith(("/dev/ttyUSB", "/dev/ttyACM", "/dev/rfcomm")) for p in listed[:-1]))
        with self.assertRaises(MachineError) as cm:
            m.connect("/dev/ttyUSB99")                # (a port that doesn't exist, just in case)
        self.assertIn("not one of the test ports", str(cm.exception))
        self.assertIsNone(m.link)


if __name__ == "__main__":
    unittest.main()
