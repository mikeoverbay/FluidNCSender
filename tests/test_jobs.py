"""
Unit tests for stage 3+4 in machine.py, job.py and settings.py: the vacuum and AUX switches, Go to zero,
programs and jobs (FluidSender's G20 G90 first, character counting, pauses, Hold and Resume, the safe
stop, Stop right after a move's ok, how a job ends, Disconnect's safe stop), the refusals while a job or
scan runs, the shared jog settings and their file, the pendant's numbers and the scanner's calls.
Protocol-style like test_machine.py: no port, made-up time, nothing opened (the scan moves wait in a
thread of their own).

    python3 -m unittest tests.test_jobs
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import job  # noqa: E402
import machine  # noqa: E402
import settings  # noqa: E402
import test_machine as base  # noqa: E402
from machine import MachineError  # noqa: E402

IDLE = base.Harness.IDLE
RUN = "<Run|MPos:1.000,0.000,0.000|FS:2540,0>"
HOLD0 = "<Hold:0|MPos:1.000,0.000,0.000|FS:0,0>"
HOLD1 = "<Hold:1|MPos:1.000,0.000,0.000|FS:500,0>"
BANNER = "Grbl 3.7 [FluidNC v3.9.4 (noradio) '$' for help]"
JOB_RUNNING = "A job is running"
SCANNING = "A scan is running"
# 30 lines of 9 characters (G0X1.0001 ... G0X0.0030): 12 of them (10 bytes each with "\n") fit in 128 bytes
LONG = "".join(f"G0X{i % 10}.{i:04d}\n" for i in range(1, 31))


class Stage3(base.Stage2):
    """Helpers for jobs: load and run a program, look at the job."""

    def load(self, text, name="t.nc"):
        return self.m.load_program(name, text)

    def start_job(self, text, name="t.nc"):
        """Loads and runs it, and answers FluidSender's own first line (G20 G90, tested in FirstLine); returns
        the program's lines sent so far."""
        self.load(text, name)
        self.m.run()
        self.tick(0.01)
        sent = self.sent()
        self.assertEqual(sent[0], "G20 G90")
        self.reply("ok")
        return sent[1:] + self.sent()

    @property
    def job(self):
        return self.m.job

    def jobs(self):
        return [e["job"] for e in self.evs("job")]

    def in_flight(self):
        return sum(line.size for line in self.link.job.inflight)


# ------------------------------------------------------------------ programs (job.py)

class Programs(unittest.TestCase):
    def test_comments_spaces_and_empty_lines(self):
        text = ("%\n(header comment)\nG20 G90 ; inches\n\n  G0 X1.5  Y-2 (rapid)\n%\nG1 Z-0.1 F20\n"
                "M3 S12000 ( spindle (on\n\t G4\tP1\n")
        p = job.parse_program("a.nc", text)
        self.assertEqual([p[i] for i in range(len(p))],
                         [(3, "G20G90"), (5, "G0X1.5Y-2"), (7, "G1Z-0.1F20"), (8, "M3S12000"), (9, "G4P1")])
        self.assertEqual((len(p), p.total), (5, 10))
        self.assertEqual(p.info(), {"name": "a.nc", "lines": 5, "total": 10, "warnings": []})

    def test_line_ends_count_like_the_editor(self):
        """CodeMirror splits on \\r\\n, \\r and \\n: the line numbers must be the editor's."""
        p = job.parse_program("a.nc", "G0 X1\r\nG0 X2\rG0 X3\n\r\nG0 X4")
        self.assertEqual([p[i] for i in range(len(p))], [(1, "G0X1"), (2, "G0X2"), (3, "G0X3"), (5, "G0X4")])
        self.assertEqual(p.total, 5)

    def test_realtime_and_control_characters_outside_comments(self):
        for text, msg in (("G0 X1\nG0 X2 ?\n", "Line 2: '?' outside a comment"),
                          ("G0 X1\n\nM0 !\n", "Line 3: '!' outside a comment"),
                          ("~\n", "Line 1: '~' outside a comment"),
                          ("G0 X1\x18\n", "Line 1: the control or non-ASCII character 0x18 outside a comment"),
                          ("G0 X1\nG0 X2\x00", "Line 2: the control or non-ASCII character 0x00"),
                          ("G0 X1 Y2°\n", "Line 1: the control or non-ASCII character 0xb0")):
            with self.assertRaises(job.ProgramError, msg=repr(text)) as cm:
                job.parse_program("a.nc", text)
            self.assertTrue(str(cm.exception).startswith(msg), str(cm.exception))
        p = job.parse_program("a.nc", "G0 X1 (why? stop! ~ 25° \x18)\n; done! ?\n(unclosed ? comment\n")
        self.assertEqual([p[i] for i in range(len(p))], [(1, "G0X1")])

    def test_line_length(self):
        """FluidNC 4.1.1 refuses G-code lines over 127 characters (error:14; 3.9.4 kept 254): FluidSender sends them
        without comments and spaces, so that is what counts."""
        ok = "G1X" + "1" * 124                         # 127 characters: the most FluidNC 4.1.1 takes
        p = job.parse_program("a.nc", f"{ok}\n(a long comment {'x' * 300})\n" + "G1 X1   Y2 " * 20)
        self.assertEqual([len(p[i][1]) for i in range(len(p))], [127, 120])
        with self.assertRaises(job.ProgramError) as cm:
            job.parse_program("a.nc", f"G0 X1\n{ok}1\n")
        self.assertEqual(str(cm.exception), "Line 2 is too long: 128 characters without comments and spaces "
                                            "(FluidNC refuses G-code lines over 127 characters: error:14)")
        p = job.parse_program("a.nc", "G1 X1 (" + "x" * 300 + ")\n")      # (a long comment isn't sent)
        self.assertEqual(p[0], (1, "G1X1"))

    def test_percent_inside_a_line_is_dropped(self):
        """FluidNC 4.1.1 drops only a line of nothing but '%' (start and end marks) and refuses one inside a line
        (error:1); 3.9.4 dropped it anywhere: FluidSender sends the line without it, as 3.9.4 read it, on both."""
        p = job.parse_program("a.nc", "%\nG1 X1 %\n % \n%G0X2\n(%)\n")
        self.assertEqual([p[i] for i in range(len(p))], [(2, "G1X1"), (4, "G0X2")])
        self.assertEqual(p.total, 6)

    def test_in_pieces_the_same(self):
        """A big text goes through in pieces cut after a "\\n": the lines and their numbers must not change."""
        texts = ["G0 X1\r\nG0 X2 (a\r\n(b) G0 X3\rG0 X4\n\n%\r\nG1 Y1 ; c\r\n", "G0 X1\n" * 7 + "G1 X2", "\n\n\n",
                 "G0\r\n\r\nG1\r\n", "G0 X1 ; ?\nG0 X2\nG0 X3 ?\n", "G0\nG1\n" + "G1X" + "1" * 252 + "\nG0 ?\n",
                 "\rG0\r\n\nG1\r", "x" * 30 + "\n" + "(" + "y" * 20 + "\nG0 X1\n",
                 "M8 F10.1\r\nG0 X1\nM1\n\nM7 (c) X2.1\rG0\n"]

        def parse(text):
            try:
                p = job.parse_program("a.nc", text)
                return [p[i] for i in range(len(p))], p.total, p.warnings
            except job.ProgramError as e:
                return str(e)
        for text in texts:
            whole = parse(text)
            for size in (1, 2, 3, 5, 8):
                with mock.patch.object(job, "CHUNK", size):
                    self.assertEqual(parse(text), whole, (text, size))

    def test_warnings_for_what_fluidnc_reads_otherwise(self):
        """FluidNC (3.9.4 and 4.1.1) takes M7.1 / M8.1 (off) from the fraction of a line's last word (a G word leaves
        none), and M1 does nothing: the program still runs, with a warning for each such line."""
        text = ("M8\nG1 X1 M8 F100.1\nM7 S1000.1 (spindle)\nM8 X0.1 G1\nM8.1 X1\nM8.1\nM1\nM8 X-0.1\nN10 M8\nM9\n"
                "m8 x1.0999\nM01\nM10\nM18\nM8 X#1\nM7 M8 X0.1\n")
        p = job.parse_program("a.nc", text)
        self.assertEqual((len(p), p.total), (16, 17))     # (they all run)
        self.assertEqual(p.warnings, [
            "Line 2 (G1X1M8F100.1): FluidNC turns the vacuum (M8) OFF here: it takes M8.1 from the last number "
            "on the line, F100.1. Put M8 on a line of its own",
            "Line 3 (M7S1000.1): FluidNC turns AUX (M7) OFF here: it takes M7.1 from the last number on the line, "
            "S1000.1. Put M7 on a line of its own",
            "Line 5 (M8.1X1): FluidNC turns the vacuum (M8) ON here: M8.1 works only alone on a line. Put M8.1 "
            "(or M9) on a line of its own",
            "Line 7 (M1): M1 (optional stop) does nothing in FluidNC: the program does not stop there. Use M0 to "
            "stop",
            "Line 11 (m8x1.0999): FluidNC turns the vacuum (M8) OFF here: it takes M8.1 from the last number on "
            "the line, X1.0999. Put M8 on a line of its own",
            "Line 12 (M01): M1 (optional stop) does nothing in FluidNC: the program does not stop there. Use M0 "
            "to stop"])
        self.assertEqual(p.info()["warnings"], p.warnings)
        self.assertEqual([job.mantissa(v) for v in (8, 8.1, 1.0999, 1.0949, -0.1, 100.1, 1e39)], [0, 10, 10, 9, -10, 10, 0])
        many = job.parse_program("b.nc", "M8 F1.1\n" * 25 + "G0 X1\n")
        self.assertEqual(len(many.warnings), job.MAX_WARNINGS)
        self.assertEqual(many.warnings[-1], "... and 6 more lines like these")
        self.assertTrue(many.warnings[-2].startswith("Line 19 (M8F1.1)"))

    def test_clock_text(self):
        self.assertEqual([job.clock_text(s) for s in (0, 59.6, 83, 3725)], ["0:00", "1:00", "1:23", "1:02:05"])


class LoadProgram(Stage3):
    def test_loaded_shown_and_unloaded(self):
        self.assertEqual(self.load("G0 X1\n(c)\nG0 X2\n"), {"lines": 2, "total": 4, "warnings": []})
        info = {"name": "t.nc", "lines": 2, "total": 4, "warnings": []}
        self.assertEqual(self.evs("program"), [{"type": "program", "program": info}])
        self.assertEqual(self.m.snapshot()["program"], info)
        self.assertEqual(self.m.load_program({"text": ""}), {"lines": 0, "total": 0, "warnings": []})
        self.assertEqual(self.evs("program")[-1], {"type": "program", "program": None})
        self.assertIsNone(self.m.snapshot()["program"])
        self.m.load_program(None, "")                   # (nothing loaded: nothing to say)
        self.assertEqual(len(self.evs("program")), 2)

    def test_a_refused_program_unloads_the_old_one(self):
        """The page shows the new text: running the old program then would run something else."""
        self.load("G0 X1\n")
        with self.assertRaises(MachineError) as cm:
            self.load("G0 X1\nG0 X2\nG0 X3 ?\n")
        self.assertEqual(cm.exception.status, 400)
        self.assertTrue(str(cm.exception).startswith("Program not loaded: Line 3: '?' outside a comment"))
        self.assertIsNone(self.m.program)
        self.assertEqual(self.evs("program")[-1], {"type": "program", "program": None})
        for bad in ({"text": 5}, {"name": 7, "text": "G0"}):
            with self.assertRaises(MachineError):
                self.m.load_program(bad)

    def test_an_older_upload_that_finishes_last_does_not_win(self):
        real = machine.parse_program
        calls = []

        def slow(name, text):
            calls.append(text)
            if len(calls) == 1:                         # a newer upload comes in while this one is checked
                self.m.load_program("t.nc", "G0 X2\nG0 X3\n")
            return real(name, text)
        with mock.patch.object(machine, "parse_program", side_effect=slow):
            self.load("G0 X1\n")
        self.assertEqual(len(self.m.program), 2)
        self.assertEqual(self.m.program[0], (1, "G0X2"))

    def test_a_running_job_keeps_its_own_program(self):
        self.ready()
        self.start_job("G0 X1\nG0 X2\n")
        self.load("G0 X9\n", "other.nc")                # (uploads are fine during a job)
        self.load("")
        self.assertEqual(self.job.program[1], (2, "G0X2"))
        self.assertEqual(self.job.name, "t.nc")


# ------------------------------------------------------------------ the vacuum and AUX switches

class Coolant(Stage3):
    def test_parsed_from_reports_with_ov(self):
        sp = machine.StatusParser()
        st = sp.feed("<Idle|MPos:0,0,0|FS:0,0>")
        self.assertEqual((st["flood"], st["mist"]), (None, None))        # not known yet
        st = sp.feed("<Run|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:SFM>")
        self.assertEqual((st["flood"], st["mist"], sp.spindle), (True, True, "cw"))
        st = sp.feed("<Run|MPos:0,0,0|FS:0,0>")                          # no Ov: says nothing about it
        self.assertEqual((st["flood"], st["mist"]), (True, True))
        st = sp.feed("<Run|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:CM>")
        self.assertEqual((st["flood"], st["mist"], sp.spindle), (False, True, "ccw"))
        st = sp.feed("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>")          # Ov without A: all off
        self.assertEqual((st["flood"], st["mist"], sp.spindle), (False, False, "off"))
        self.assertEqual(machine.parse_status("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:F>")["acc"], "F")

    def test_seeded_by_g_and_set_by_a_reset(self):
        self.ready()                                    # ($G at connect said M9)
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (False, False))
        self.link.parser.flood = self.link.parser.mist = None
        self.reply("[GC:G0 G54 G17 G20 G90 G94 M3 M7 M8 T0 F0 S0]")
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (True, True))
        self.assertEqual((self.evs("status")[-1]["flood"], self.evs("status")[-1]["mist"]), (True, True))
        self.reply("[GC:G0 G54 G17 G20 G90 G94 M5 M9 T0 F0 S0]")         # known: reports with Ov say it
        self.assertEqual(self.link.parser.flood, True)
        self.assertEqual(machine.gc_coolant("G0 G54 M5 M8 T0"), (True, False))
        self.assertEqual(machine.gc_coolant("G0 G54 M5 M7 T0"), (False, True))
        self.assertIsNone(machine.gc_coolant("G0 G54"))
        self.reply(BANNER)                              # a reset turns them off
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (False, False))

    def test_toggle_on_and_off(self):
        self.ready()
        self.assertEqual(self.m.coolant("vacuum", False), {"sent": False})    # already off: nothing sent
        self.assertEqual(self.realtime(), b"")
        self.assertEqual(self.m.coolant("vacuum", True), {"sent": True})
        self.assertEqual(self.realtime(), b"\xa0")
        self.assertEqual(self.log("info")[-1], "Vacuum on (M8 toggle)")
        st = self.evs("status")[-1]
        self.assertEqual((st["flood"], st["mist"]), (True, False))      # shown at once
        self.assertEqual(self.link.next_poll, self.t)                    # and a report asked for at once
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.02)   # (made before FluidNC took it)
        self.assertTrue(self.link.status["flood"])
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:F>", dt=0.06)
        self.assertTrue(self.link.status["flood"])
        self.assertEqual(self.m.coolant({"which": "aux", "on": True}), {"sent": True})
        self.assertEqual(self.realtime(), b"\xa1")
        self.assertEqual(self.log("info")[-1], "AUX on (M7 toggle)")
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:FM>", dt=0.06)
        self.m.coolant("vacuum", False)
        self.assertEqual((self.realtime(), self.log("info")[-1]), (b"\xa0", "Vacuum off (M8 toggle)"))
        self.assertEqual((self.link.status["flood"], self.link.status["mist"]), (False, True))
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.06)  # (the program's M9, say)
        self.assertEqual((self.link.status["flood"], self.link.status["mist"]), (False, False))

    def test_refused(self):
        self.refused(lambda: self.m.coolant("vacuum", True), "Not connected")
        self.ready()
        for which, on in (("flood", True), ("vacuum", "yes"), (None, True)):
            self.refused(lambda: self.m.coolant(which, on), self.message(which, on), 400)
        self.link.parser.flood = self.link.parser.mist = None
        self.refused(lambda: self.m.coolant("vacuum", True), "Vacuum state not known yet")
        self.refused(lambda: self.m.coolant("aux", False), "AUX state not known yet")
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>")
        for report, state in (("<Alarm|MPos:0,0,0|FS:0,0>", "Alarm"), (base.JOG, "Jog"),
                              ("<Home|MPos:0,0,0|FS:0,0>", "Home"), ("<Door:1|MPos:0,0,0|FS:0,0>", "Door")):
            self.reply(report)
            self.refused(lambda: self.m.coolant("vacuum", True), f"Vacuum not switched: FluidNC ignores the switch "
                         f"in {state} (it takes it only when Idle, running or in Hold)")
        self.reply(HOLD0)
        self.assertEqual(self.m.coolant("aux", True), {"sent": True})   # in Hold: fine
        self.t += 2.1
        self.refused(lambda: self.m.coolant("vacuum", True), "No response from FluidNC")
        self.assertEqual(self.realtime(), b"\xa1")

    @staticmethod
    def message(which, on):
        return 'Which switch? "vacuum" (M8) or "aux" (M7)' if which != "vacuum" else "on: true or false"

    def test_allowed_during_a_job(self):
        self.ready()
        self.start_job(LONG)
        self.reply(RUN)
        self.assertEqual(self.m.coolant("vacuum", True), {"sent": True})
        self.assertEqual(self.realtime(), b"\xa0")

    def poll_gap(self):
        """The time from the next status request to the one after it."""
        self.t = max(self.t, self.link.next_poll)
        self.tick(0)
        return round(self.link.next_poll - self.t, 4)

    def test_reports_asked_for_quickly_after_a_toggle(self):
        """FluidNC 3.9.4 says the coolant only in every 10th report when polled with '?': after a toggle the
        reports are asked for every 15 ms until one made after it has Ov (at most 11), then every 200 ms again."""
        self.ready()
        self.assertEqual(self.poll_gap(), machine.POLL)
        self.m.coolant("vacuum", True)
        self.assertEqual(self.link.next_poll, self.t)                    # one at once ...
        self.assertEqual(self.poll_gap(), machine.BURST_POLL)            # ... and quickly after it
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.01)   # made before FluidNC took the toggle:
        self.assertEqual(self.link.burst, machine.BURST_REPORTS)         # it doesn't count
        self.assertTrue(self.link.status["flood"])                       # (the switch shows what was asked for)
        for n in range(3):                                               # newer reports without Ov
            self.reply(IDLE, dt=0.05)
            self.assertEqual(self.poll_gap(), machine.BURST_POLL)
        self.assertEqual(self.link.burst, machine.BURST_REPORTS - 3)
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.02)   # FluidNC ignored it (no flood output?):
        self.assertFalse(self.link.status["flood"])                      # the switch says so at once
        self.assertEqual(self.evs("status")[-1]["flood"], False)
        self.assertEqual(self.link.burst, 0)
        self.tick(0.02)
        self.assertEqual(self.poll_gap(), machine.POLL)                  # back to every 200 ms
        self.m.coolant("aux", True)                                      # gives up quietly after 11 reports ...
        for n in range(machine.BURST_REPORTS):
            self.reply(IDLE, dt=0.06)
        self.assertEqual(self.link.burst, 0)
        self.assertTrue(self.link.status["mist"])
        self.assertEqual(self.log("error"), [])
        self.m.coolant("aux", False)                                     # ... or after 1 s without reports
        self.tick(0.5)
        self.assertEqual(self.poll_gap(), machine.BURST_POLL)
        self.tick(0.51)
        self.assertEqual(self.poll_gap(), machine.POLL)

    FLOOD = "<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:F>"
    NO_OV = "<Idle|MPos:0,0,0|FS:0,0>"

    def typed(self, line, reply="ok"):
        self.m.send(line)
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply(reply)

    def test_the_switch_follows_an_m9_at_once(self):
        """FluidNC switches M7/M8/M9 before it answers, but says so in a report only every 10th one: the switch
        must not wait for that, or a click on it meanwhile toggles the vacuum back on."""
        self.ready()
        self.reply(self.FLOOD, dt=0.2)
        self.assertTrue(self.link.parser.flood)
        self.typed("M9")
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (False, False))
        self.assertEqual(self.evs("status")[-1]["flood"], False)        # shown at once
        self.assertEqual((self.link.burst, self.link.next_poll), (machine.BURST_REPORTS, self.t))   # and asked for
        self.reply(self.NO_OV, dt=0.2)                  # (the next reports say nothing about it)
        self.assertEqual(self.m.coolant("vacuum", False), {"sent": False})
        self.assertEqual(self.realtime(), b"")          # no toggle: it is off already

    def test_a_jobs_m8_and_m9_show_at_once(self):
        self.ready()
        self.start_job("M8\nG4 P1\nM9\nM7\nM30\n")
        self.reply("ok")                                # M8
        self.assertTrue(self.evs("status")[-1]["flood"])
        self.assertEqual(self.m.coolant("vacuum", True), {"sent": False})   # (a click "on" now: already on)
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.02)    # made before the ok counted: ignored
        self.assertTrue(self.link.parser.flood)
        self.reply(self.FLOOD, dt=0.06)                 # FluidNC's word
        self.assertTrue(self.link.parser.flood)
        self.reply("ok", "ok")                          # G4, M9
        self.assertFalse(self.link.parser.flood)
        self.reply("ok")                                # M7
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (False, True))
        self.reply("[MSG:INFO: Program End]", "ok")     # M30 turns both off
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (False, False))
        self.assertEqual(self.realtime(), b"")

    def test_m8_takes_its_off_from_the_last_word(self):
        """FluidNC 3.9.4 reads M8.1 (off) from the fraction of the line's last word: "M8 F100.1" turns it off."""
        self.ready()
        self.typed("M8 F100.1")
        self.assertIs(self.link.parser.flood, False)
        self.typed("M8")
        self.assertIs(self.link.parser.flood, True)
        self.typed("M9 Q1", "error:20")                 # refused: nothing changed
        self.assertIs(self.link.parser.flood, True)

    def test_a_report_after_the_ok_that_disagrees_wins(self):
        """A machine without a flood output answers M8 ok and changes nothing: FluidNC's next report with Ov
        (asked for quickly) says off, and that wins."""
        self.ready()
        self.typed("M8")
        self.assertTrue(self.link.parser.flood)
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.06)
        self.assertFalse(self.link.parser.flood)
        self.assertEqual(self.evs("status")[-1]["flood"], False)
        self.assertEqual(self.link.burst, 0)


GPIO = dict(base.SETTINGS, **{"$/coolant/flood_pin": "gpio.25", "$/coolant/mist_pin": "gpio.26"})
GC_M8 = "[GC:G0 G54 G17 G20 G90 G94 M5 M8 T0 F0 S0]"
GC_M9 = "[GC:G0 G54 G17 G20 G90 G94 M5 M9 T0 F0 S0]"
OV = "<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>"      # (FluidNC 4.1.1: Ov in the next report after a change)


class CoolantPins(Stage3):
    """FluidNC builds A:'s F and M by reading the coolant pins back, and a plain ESP32 GPIO set up as an output
    reads 0: on such a pin its reports never show the output. The switches then follow FluidNC's M7/M8/M9 state
    (toggles, M7/M8/M9/M2/M30 at their ok, resets), confirmed by a quiet $G after a toggle and when a job ends.
    I2SO and expander pins read back: their reports count as before. NO_PIN: no such output (refused)."""

    def toggle_on(self):
        """The vacuum switched on: 0xA0, then a quiet $G (a moment later: see settled), answered M8."""
        self.assertEqual(self.m.coolant("vacuum", True), {"sent": True})
        self.assertEqual(self.realtime(), b"\xa0")
        self.assertEqual(self.settled(), ["$G"])
        self.reply(GC_M8, "ok")

    def settled(self):
        """The reader thread's passes after a toggle: the toggle goes out in the first, and a coolant $G (or an
        M7/M8/M9) only SETTLE after it, once FluidNC has acted on it. Returns the lines sent then."""
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.tick(machine.SETTLE)
        return self.sent()

    def test_a_gpio_output_follows_fluidncs_modes_not_its_reports(self):
        self.ready(settings=GPIO)
        self.assertEqual((self.link.coolant_pins, self.link.parser.readback),
                         ({"flood": "gpio.25", "mist": "gpio.26"}, {"flood": False, "mist": False}))
        st = self.m.snapshot()["status"]
        self.assertEqual((st["coolantFollows"], st["coolantPins"]), ({"vacuum": "modal", "aux": "modal"},
                                                                     {"vacuum": "gpio.25", "aux": "gpio.26"}))
        self.toggle_on()
        self.assertEqual((self.link.parser.flood, self.link.coolant_sure), (True, True))
        self.reply(OV, dt=0.06)                         # (no F: the pin reads 0) ...
        self.assertTrue(self.link.parser.flood)         # ... which doesn't count
        self.assertTrue(self.evs("status")[-1]["flood"])
        self.reply(OV, dt=0.2)
        self.assertEqual(self.log("info").count("FluidNC's reports don't show the coolant outputs; following its "
                                                "M7/M8/M9 state"), 1)
        self.assertEqual((self.log("tx"), self.log("rx")), ([], []))    # (the $G is quiet: its answer too)
        self.assertEqual(self.m.coolant("vacuum", True), {"sent": False})     # (on already)
        self.assertEqual(self.m.coolant("vacuum", False), {"sent": True})
        self.assertEqual(self.settled(), ["$G"])
        self.reply(GC_M9, "ok")
        self.assertEqual(self.link.parser.flood, False)

    def test_the_g_says_what_fluidnc_did(self):
        """A toggle FluidNC didn't take (it was in another state by then, say): its $G shows it at once."""
        self.ready(settings=GPIO)
        self.m.coolant("vacuum", True)
        self.assertEqual(self.settled(), ["$G"])
        self.assertTrue(self.evs("status")[-1]["flood"])        # (shown at once, as asked)
        self.reply(GC_M9, "ok")
        self.assertEqual((self.link.parser.flood, self.evs("status")[-1]["flood"]), (False, False))

    def test_a_g_sent_before_a_toggle_says_nothing_about_it(self):
        self.ready(settings=GPIO)
        self.m.coolant("vacuum", True)
        self.assertEqual(self.settled(), ["$G"])
        self.m.coolant("aux", True)                     # (while that $G is in FluidNC's hands)
        self.assertEqual(self.realtime(), b"\xa0\xa1")
        self.reply(GC_M8, "ok")                         # (made before the AUX toggle, maybe)
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (True, True))    # not taken
        self.assertEqual(self.settled(), ["$G"])        # asked again (once the AUX toggle has settled)
        self.reply("[GC:G0 G54 G17 G20 G90 G94 M5 M7 M8 T0 F0 S0]", "ok")
        self.assertEqual((self.link.parser.flood, self.link.parser.mist, self.link.coolant_sure), (True, True, True))

    def test_the_g_never_goes_with_the_toggle(self):
        """FluidNC (4.1.1 and 3.9.4) queues a toggle as an event, and its main loop runs a line that came in the same
        read first: a $G written with the toggle could show the modes from before it, and the switch would show
        the opposite of the relay. So the quiet $G goes SETTLE after the toggle went out (the reviewer's B)."""
        self.ready(settings=GPIO)
        self.m.coolant("vacuum", True)
        self.tick(0.001)                                # (the reader thread's next pass: the toggle goes)
        self.assertEqual((self.realtime(), self.sent()), (b"\xa0", []))
        self.reply(OV, dt=0.01)                         # (a report meanwhile: no line goes yet)
        self.tick(machine.SETTLE - 0.02)
        self.assertEqual(self.sent(), [])
        self.tick(0.02)
        self.assertEqual(self.sent(), ["$G"])
        self.reply(GC_M8, "ok")                         # (FluidNC took the toggle before it)
        self.assertEqual((self.link.parser.flood, self.link.coolant_sure), (True, True))

    def test_a_g_sent_with_a_toggle_is_not_its_word(self):
        """A $G that went out less than SETTLE after a toggle (typed: it doesn't wait) may show the modes from
        before it: its coolant isn't taken. (I2SO pins: no quiet $G of FluidSender's own.)"""
        self.ready()
        self.m.coolant("vacuum", True)
        self.m.send("$G")
        self.tick(0.01)
        self.assertEqual((self.realtime(), self.sent()), (b"\xa0", ["$G"]))
        self.reply(GC_M9, "ok")                         # (made before FluidNC took the toggle)
        self.assertEqual((self.link.parser.flood, self.link.coolant_sure), (True, False))
        self.assertTrue(self.evs("status")[-1]["flood"])
        self.reply(OV, dt=0.06)                         # (its report says it: I2SO reads back)
        self.assertFalse(self.link.parser.flood)

    def test_a_jobs_m8_waits_for_a_toggle_just_sent(self):
        """A toggle right before the job's next line has M8: FluidNC would run the M8 first (it sets the mode, then
        waits for the moves) and the toggle in that wait would flip it. So that line waits until the toggle has
        settled; lines without M7/M8/M9 go on as usual."""
        self.ready()
        lines = LONG.splitlines()[:13]
        sent = self.start_job("\n".join(lines + ["M8", "G4 P0.1", "M9"]) + "\n")
        self.assertEqual(sent, lines[:12])              # (128 bytes in FluidNC's hands at most)
        self.reply(RUN)
        self.m.coolant("aux", True)                     # (an M8 only waits for the vacuum; any toggle will do)
        self.reply("ok")                                # (line 1: room for line 13 and the M8)
        self.assertEqual((self.realtime(), self.sent()), (b"\xa1", [lines[12]]))
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.tick(machine.SETTLE)
        self.assertEqual(self.sent(), ["M8"])
        self.reply(*["ok"] * 12)
        self.assertEqual(self.sent(), ["G4P0.1", "M9"])     # (no toggle since: they go at once)

    def test_a_job_follows_its_m8_and_m9_and_asks_at_its_end(self):
        self.ready(settings=GPIO)
        self.start_job("M8\nG4 P1\nM9\nM7\n")
        self.reply("ok")                                # M8
        self.assertTrue(self.link.parser.flood)
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.06)
        self.assertTrue(self.link.parser.flood)         # (its reports can't say it)
        self.reply("ok", "ok", "ok")                    # G4, M9, M7
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (False, True))
        self.reply(IDLE, dt=0.06)
        self.assertEqual(self.job.state, "done")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$G"])           # FluidNC's word for it, once the job is over
        self.reply("[GC:G0 G54 G17 G20 G90 G94 M5 M7 T0 F0 S0]", "ok")
        self.assertEqual((self.link.parser.flood, self.link.parser.mist, self.link.coolant_sure), (False, True, True))

    def test_no_g_while_lines_wait_or_a_job_runs(self):
        self.ready(settings=GPIO)
        self.m.send("G1 X5 F10")
        self.tick(0.01)
        self.sent()
        self.m.coolant("vacuum", True)                  # (G1 X5 in FluidNC's hands: its ok may take a while)
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.reply("ok", RUN)
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # nothing asked: the tracking says it
        self.reply(IDLE, dt=0.3)
        self.start_job(LONG)
        self.reply(RUN, dt=0.06)
        self.assertEqual(self.m.coolant("aux", True), {"sent": True})
        self.tick(0.01)
        self.assertEqual([line for line in self.sent() if line == "$G"], [])     # (nor during a job)

    def test_reads_back_i2so_pins_and_trusts_their_reports(self):
        self.ready()                                    # (I2SO.4, I2SO.5: see test_machine.SETTINGS)
        self.assertEqual(self.link.parser.readback, {"flood": True, "mist": True})
        self.assertEqual(self.m.coolant("vacuum", True), {"sent": True})
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # no $G: its reports say it
        self.reply(OV, dt=0.06)
        self.assertFalse(self.link.parser.flood)        # (FluidNC's word)

    def test_an_unknown_pin_is_checked_and_followed_once_its_reports_fail(self):
        """The pins couldn't be read: the reports count, but a toggle is confirmed with $G, and an output that
        $G says is on but no report shows is followed by FluidNC's modes from then on."""
        self.ready(settings=dict(base.SETTINGS, **{"$/coolant/flood_pin": None, "$/coolant/mist_pin": None}))
        self.assertEqual(self.link.parser.readback, {"flood": True, "mist": True})
        self.toggle_on()
        self.reply(OV, dt=0.06)
        self.assertEqual((self.link.parser.readback["flood"], self.link.parser.flood), (False, True))
        self.assertIn("FluidNC's reports don't show the coolant outputs; following its M7/M8/M9 state",
                      self.log("info"))
        self.assertEqual(self.m.snapshot()["status"]["coolantFollows"]["vacuum"], "modal")

    def test_until_the_pins_are_read_reports_dont_change_the_switches(self):
        """Connected with the vacuum on: $G says M8; a report before the pin reads must not turn it off."""
        self.reply(IDLE)
        self.link.went_live(self.t)
        self.assertEqual(self.sent(), ["$I"])
        self.reply("[VER:4.1 FluidNC v4.1.1 (esp32-noradio) :]", "ok")
        self.assertEqual(self.sent(), ["$G"])
        self.reply("[GC:G0 G54 G17 G21 G90 G94 M5 M8 T0 F0 S0]", "ok")
        self.assertTrue(self.link.parser.flood)
        self.reply(OV, dt=0.06)
        self.assertTrue(self.link.parser.flood)

    def test_a_pin_read_answered_without_its_value_trusts_the_reports(self):
        """A pin read answered ok but without its value: the pin is unknown, so the reports count (as when the read
        is refused), not "wait for the pins" for ever."""
        self.reply(IDLE)
        self.link.went_live(self.t)
        for q in ["$I", "$G", *machine.READS]:
            self.assertEqual(self.sent(), [q])
            self.reply("ok")
        self.assertEqual((self.link.parser.readback, self.link.coolant_pins),
                         ({"flood": True, "mist": True}, {"flood": None, "mist": None}))
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:F>", dt=0.06)
        self.assertEqual((self.link.parser.flood, self.link.parser.mist), (True, False))

    def test_no_pin_is_refused_and_said(self):
        self.ready(settings=dict(base.SETTINGS, **{"$/coolant/flood_pin": "NO_PIN"}))
        why = "FluidNC has no flood_pin configured (the vacuum output)"
        self.refused(lambda: self.m.coolant("vacuum", True), why)
        st = self.m.snapshot()["status"]
        self.assertEqual(st["coolantMissing"], {"vacuum": why, "aux": None})
        self.typed("M8")                                # FluidNC drops an M8 without the pin: still off
        self.assertFalse(self.link.parser.flood)
        self.assertEqual(self.m.coolant("aux", True), {"sent": True})

    def typed(self, line, reply="ok"):
        self.m.send(line)
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply(reply)

    def test_a_waiting_m8_refuses_a_click(self):
        """FluidNC sets the mode an M8 gives, then waits for the moves before it, then switches: a toggle in that
        wait flips the mode set already, and the program's M8 is cancelled (the vacuum stays off for the cut).
        So while FluidNC has an M7/M8/M9 it hasn't answered, a click on that output is refused (and said)."""
        self.ready()
        self.start_job("G1 X1 F20\nM8\nG4 P0.5\nM9\n")  # (all in FluidNC's hands at once)
        why = ("The program's M8 (line 2) hasn't run yet: it waits for the moves before it, and a click now would "
               "cancel it. Try again once it has run.")
        self.refused(lambda: self.m.coolant("vacuum", True), why)
        busy = self.m.snapshot()["status"]["coolantBusy"]
        self.assertEqual(busy, {"vacuum": why, "aux": why.replace("M8 (line 2)", "M9 (line 4)")})
        self.reply("ok", "ok")                          # G1, M8 (it switched)
        self.assertTrue(self.link.parser.flood)
        self.refused(lambda: self.m.coolant("vacuum", False), why.replace("M8 (line 2)", "M9 (line 4)"))
        self.reply("ok", "ok")                          # G4, M9
        self.assertEqual(self.m.snapshot()["status"]["coolantBusy"], {"vacuum": None, "aux": None})
        self.assertEqual(self.m.coolant("vacuum", True), {"sent": True})
        self.assertEqual(self.realtime(), b"\xa0")

    def test_a_typed_m8_waiting_refuses_a_click_too(self):
        self.ready()
        self.typed("G1 X5 F10")
        self.m.send("M8")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["M8"])           # (it waits for the move in FluidNC)
        self.refused(lambda: self.m.coolant("vacuum", True), "The M8 you sent hasn't run yet: it waits for the moves "
                     "before it, and a click now would cancel it. Try again once it has run.")
        self.assertEqual(self.m.coolant("aux", True), {"sent": True})   # (M8 doesn't change AUX)

    def test_refused_while_a_jog_runs_or_starts(self):
        self.ready()
        self.running(x=1)                               # a hold jog: its first piece sent, the report still Idle
        self.refused(lambda: self.m.coolant("vacuum", True), "Vacuum not switched: a jog is running or starting "
                     "(FluidNC ignores the switch while it jogs)")
        self.assertEqual(self.realtime(), b"")

    def test_the_delay_is_read(self):
        self.ready(settings=dict(base.SETTINGS, **{"$/coolant/delay_ms": "500"}))
        self.assertEqual(self.m.snapshot()["status"]["coolantDelayMs"], 500)


class FluidNC411(Stage3):
    """FluidNC 4.1.1 (the user's): its $I in a critical alarm, $T, $SS, the S and F in force, its "Bad GCode"
    message before an error, Ov in the next report after a toggle."""

    def test_a_critical_alarm_at_connect_is_learned_from_t(self):
        self.reply("<Alarm|MPos:0,0,0|FS:0,0|WCO:0,0,0>")
        self.link.went_live(self.t)
        self.assertEqual(self.sent(), ["$I"])
        self.reply("[VER:4.1 FluidNC v4.1.1 (esp32-noradio) :]", "[OPT:MPHS]", "[CLUSTER:16]", "ok")   # (answered)
        self.assertEqual(self.sent(), ["$G"])
        self.reply("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]", "ok")
        self.assertEqual(self.sent(), ["$T"])
        self.reply("State 11 (Critical)", "ok")
        self.assertEqual(self.link.alarm, {"code": None, "critical": True, "text": "$T: State 11 (Critical)",
                                           "outputs": False})
        self.assertIn("FluidNC is in a critical alarm (hard or soft limit): only a Soft Reset gets out of it",
                      self.log("info"))
        self.assertEqual(self.log("tx"), ["$I", "$G", "$T"])
        self.refused(self.m.unlock, base.CRITICAL)
        self.assertEqual(self.sent(), [machine.SETTINGS[0]])

    def test_a_plain_alarm_t_changes_nothing(self):
        self.go_live(state="Alarm", fw="4.1.1")
        self.assertIsNone(self.link.alarm)
        self.assertEqual(self.link.firmware, "4.1 FluidNC v4.1.1 (esp32-noradio)")

    def test_no_t_when_idle(self):
        self.go_live(fw="4.1.1")
        self.assertEqual(self.log("tx"), ["$I", "$G"])

    def test_the_spindle_type_from_ss(self):
        self.ready()
        self.assertEqual(self.m.snapshot()["status"]["spindleType"], "PWM")
        self.assertEqual([t for t in self.log("rx") if "Spindle" in t or "coolant" in t], [])   # (quiet)
        self.setUp()
        self.ready(startup=("[MSG:INFO: Relay Spindle Ena:NO_PIN Out:gpio.13 Dir:NO_PIN]",))
        self.assertEqual(self.m.snapshot()["status"]["spindleType"], "Relay")
        self.setUp()
        self.ready(startup=("[MSG:INFO: Machine Fake]",))
        self.assertIsNone(self.m.snapshot()["status"]["spindleType"])

    def test_an_alarm_during_ss_is_still_shown(self):
        self.reply(IDLE)
        self.link.went_live(self.t)
        for q in ["$I", "$G", *machine.SETTINGS]:
            self.assertEqual(self.sent(), [q])
            self.reply("ok")
        self.assertEqual(self.sent(), ["$SS"])
        self.reply("[MSG:INFO: Machine Fake]", "ALARM:1", "[MSG:ERR: Reset to continue]", "ok")
        self.assertEqual(self.log("rx")[-2:], ["ALARM:1", "[MSG:ERR: Reset to continue]"])
        self.assertTrue(self.link.alarm["critical"])

    def test_programmed_s_and_f(self):
        self.ready(gc="M5 M9 T0 F508.0 S12000")         # ($G's F: mm/min, also with report_inches)
        st = self.m.snapshot()["status"]
        self.assertEqual((st["programmedS"], st["programmedF"]), (12000, 20.0))
        self.typed("M3 S18000")
        self.typed("G1 X1 F30")                         # (in inches: G20)
        st = self.m.snapshot()["status"]
        self.assertEqual((st["programmedS"], st["programmedF"]), (18000, 30.0))
        self.typed("G21 G1 X1 F254")
        self.assertEqual(self.m.snapshot()["status"]["programmedF"], 10.0)
        self.typed("$J=G20G91X1F100")                   # (a jog's F is the jog's only)
        self.reply(IDLE, dt=0.06)
        self.assertEqual(self.m.snapshot()["status"]["programmedF"], 10.0)
        self.typed("G20 S#1")                           # (a parameter: not known here)
        self.assertIsNone(self.m.snapshot()["status"]["programmedS"])
        self.reply(IDLE, dt=0.06)
        self.start_job("G1 X2 F15 S9000\n")
        self.reply("ok")
        st = self.m.snapshot()["status"]
        self.assertEqual((st["programmedS"], st["programmedF"]), (9000, 15.0))
        self.reply(BANNER)
        st = self.m.snapshot()["status"]
        self.assertEqual((st["programmedS"], st["programmedF"]), (0, 0.0))

    def typed(self, line, reply="ok"):
        self.m.send(line)
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply(reply)

    def test_bad_gcode_before_an_error_is_not_its_meaning(self):
        """4.1.1 prints "[MSG:ERR: Bad GCode: <line>]" before every G-code error:N; with verbose_errors off, the next
        refused line's must not become the last error's meaning."""
        self.ready()
        self.start_job("G1 X1 Q1\nG1 X2 Q2\nG1 X3\n")
        self.reply("[MSG:ERR: Bad GCode: G1X1Q1]", "error:20")
        self.assertEqual(self.job.error, "line 1: G1X1Q1: error:20 (unsupported or invalid G-code)")
        self.reply("[MSG:ERR: Bad GCode: G1X2Q2]")      # (the next line's, before its error)
        self.assertEqual(self.job.error, "line 1: G1X1Q1: error:20 (unsupported or invalid G-code)")
        self.reply("error:20", "[MSG:ERR: Unsupported GCode command]")
        self.assertEqual(self.job.error, "line 1: G1X1Q1: error:20 (unsupported or invalid G-code)")

    def test_quoted_gcode_in_a_message_is_not_a_message(self):
        """A soft limit's "[MSG:INFO: GCode line: ...]" (and "Bad GCode") quote the user's line: its comment can't
        pass for FluidNC's messages."""
        self.ready()
        for text in ("[MSG:INFO: GCode line: G0X1(Reset to continue)]", "[MSG:ERR: Bad GCode: G0 (to unlock)]"):
            self.reply(text)
        self.assertIsNone(self.link.alarm)
        self.reply("[MSG:INFO: ALARM: Expander Reset]", "ALARM:16", "[MSG:ERR: Reboot FluidNC]")
        self.assertEqual(self.link.alarm, {"code": 16, "critical": True, "text": "ALARM:16", "outputs": False})
        self.assertEqual(machine.explain("ALARM:16"), "ALARM:16 (I/O expander reset: reboot FluidNC)")

    def test_the_first_report_after_a_toggle_has_ov(self):
        """4.1.1 puts Ov in the next report after any coolant change: the quick reports end after one (3.9.4: see
        Coolant.test_reports_asked_for_quickly_after_a_toggle)."""
        self.ready()
        self.m.coolant("vacuum", True)
        self.assertEqual(self.link.burst, machine.BURST_REPORTS)
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:F>", dt=0.06)
        self.assertEqual((self.link.burst, self.link.parser.flood), (0, True))


# ------------------------------------------------------------------ Go to zero

class GoTo(Stage3):
    LOW = "<Idle|MPos:25.400,50.800,2.540|FS:0,0|WCO:0,0,0>"         # work Z 0.1 in: below 0.2
    HIGH = "<Idle|MPos:25.400,50.800,12.700|FS:0,0|WCO:0,0,0>"       # work Z 0.5 in

    def goto(self, to, report=None):
        if report:
            self.reply(report, dt=0.06)
        lines = self.m.goto(to)["lines"]
        for i, line in enumerate(lines):
            self.tick(0.01)
            self.assertEqual(self.sent(), [line], to)     # one at a time, like typed lines
            self.reply("ok")
        self.reply(IDLE, dt=0.06)
        return lines

    def test_lines(self):
        self.ready()
        self.assertEqual(self.goto("xy", self.LOW), ["G20 G90 G0 Z0.2", "G20 G90 G0 X0 Y0"])
        self.assertEqual(self.goto("xy", self.HIGH), ["G20 G90 G0 X0 Y0"])
        self.assertEqual(self.goto("XY", "<Idle|MPos:0,0,5.080|FS:0,0|WCO:0,0,0>"), ["G20 G90 G0 X0 Y0"])   # at 0.2
        self.assertEqual(self.goto("z"), ["G20 G90 G0 Z0"])
        self.assertEqual(self.goto("zero", self.LOW), ["G20 G90 G0 Z0.2", "G20 G90 G0 X0 Y0", "G20 G90 G0 Z0"])
        self.assertEqual(self.goto("zero", self.HIGH), ["G20 G90 G0 X0 Y0", "G20 G90 G0 Z0"])
        self.assertEqual(self.goto({"to": "mz"}), ["G53 G0 Z0"])
        self.assertEqual(self.goto("my"), ["G53 G0 Y0"])
        self.assertEqual(self.goto("mx"), ["G53 G0 X0"])
        self.assertEqual(self.log("tx")[-3:], ["G53 G0 Z0", "G53 G0 Y0", "G53 G0 X0"])   # shown like typed lines
        self.assertEqual(self.log("rx")[-1], "ok")
        self.assertEqual(self.link.modal["distance"], "G90")

    def test_machine_zero_goes_even_when_g20_failed(self):
        self.ready()
        self.link.inch, self.link.inch_tries = True, machine.INCH_TRIES
        self.assertEqual(self.goto("mz"), ["G53 G0 Z0"])   # (0 is 0 in millimeters too)

    def test_refused(self):
        self.refused(lambda: self.m.goto("z"), "Not connected")
        self.ready()
        for to in ("", "home", None, 5, "x"):
            self.refused(lambda: self.m.goto(to), 'Go to where? "xy", "z" or "zero" (work zero), or "mx", "my" or '
                                                  '"mz" (machine zero)', 400)
        for report, why in (("<Alarm|MPos:0,0,0|FS:0,0>", "ALARM: Unlock or Home first"),
                            ("<Run|MPos:0,0,0|FS:100,0>", "Running: wait until FluidNC is Idle"),
                            (HOLD0, "Hold: Resume or Soft Reset first"),
                            (base.JOG, "Jogging: wait until it has stopped")):
            self.reply(report)
            self.refused(lambda: self.m.goto("z"), why)
        self.reply("ALARM:1", "[MSG:ERR: Reset to continue]", "<Alarm|MPos:0,0,0|FS:0,0>")
        self.refused(lambda: self.m.goto("xy"), base.CRITICAL)
        self.reply(BANNER)
        self.reply(IDLE, dt=0.06)                       # (a moment after the banner: it ran nothing from before)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])
        self.reply("ok")                                # (our G20)
        self.refused(lambda: self.m.goto("z"), "FluidNC is busy; try again in a second")   # no report since
        self.reply(IDLE, dt=0.06)
        self.m.send("G4 P1")
        self.refused(lambda: self.m.goto("z"), "Waiting for FluidNC to finish 'G4 P1'")
        self.tick()
        self.sent()
        self.reply("ok", IDLE)
        self.do_step(x=0.1)
        self.step(x=0.1)
        self.tick(0.01)
        self.sent()
        self.reply("ok")
        self.refused(lambda: self.m.goto("z"), "Jogging: wait until it has stopped")
        self.reply(IDLE, dt=0.06)
        self.running(x=1)
        self.refused(lambda: self.m.goto("z"), base.JOGGING)
        self.m.jog({"stop": "all"})
        self.stopped()
        self.link.status = dict(self.link.status, wpos=None)
        self.refused(lambda: self.m.goto("xy"), "Work position not known yet; try again in a second")
        self.assertEqual(self.m.goto("z")["lines"], ["G20 G90 G0 Z0"])   # (Z alone needs no work position)

    def test_a_refused_line_stops_the_rest(self):
        """If FluidNC refuses the Z lift, the XY rapid must not run at the low Z (nor Z0 at the wrong place)."""
        self.ready()
        self.reply(self.LOW, dt=0.06)
        self.assertEqual(self.m.goto("zero")["lines"], ["G20 G90 G0 Z0.2", "G20 G90 G0 X0 Y0", "G20 G90 G0 Z0"])
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G20 G90 G0 Z0.2"])
        self.reply("error:9")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # the rest of it doesn't go
        self.assertEqual(list(self.link.queue), [])
        self.assertEqual(self.log("error")[-1], "Return to zero stopped: G20 G90 G0 Z0.2 was refused (error:9)")
        self.reply(self.LOW, dt=0.06)
        self.assertEqual(self.goto("xy"), ["G20 G90 G0 Z0.2", "G20 G90 G0 X0 Y0"])   # (the next one goes as usual)
        self.reply(self.LOW, dt=0.06)
        self.m.goto("xy")
        self.tick(0.01)
        self.sent()
        self.reply("ok")                                # the lift is fine, the XY move refused: nothing left
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G20 G90 G0 X0 Y0"])
        self.reply("error:15")
        self.assertEqual(self.log("error")[-1:], ["Return to zero stopped: G20 G90 G0 Z0.2 was refused (error:9)"])


# ------------------------------------------------------------------ jobs: streaming

class Streaming(Stage3):
    def test_character_counting(self):
        """The job lines FluidNC has not answered never take more than 128 bytes, and the replies answer them
        in order."""
        self.ready()
        first = self.start_job(LONG)
        self.assertEqual(len(first), 12)                # 12 x 10 bytes; a 13th would make 130
        self.assertEqual(first[0], "G0X1.0001")
        self.assertEqual(self.in_flight(), 120)
        answered = 0
        while self.job.state == "running" and answered < 30:
            self.reply("ok")
            answered += 1
            self.assertLessEqual(self.in_flight(), 128)
            new = self.sent()
            self.assertEqual(len(new), 1 if self.job.sent < 30 or new else 0)
            self.assertEqual(self.job.line, answered)   # (no comments: the file line numbers are 1, 2, 3...)
        self.assertEqual((self.job.sent, self.job.acked, self.job.next), (30, 30, 30))
        self.assertEqual(self.log("tx"), [])            # job lines and their oks are not in the console
        self.assertEqual(self.log("rx"), [])

    def test_lines_in_order_with_their_file_numbers(self):
        self.ready()
        sent = self.start_job("(t.nc)\nG20\n\nG0 Z0.25 ; up\nG0 X1 Y1\n%\nG1 Z-0.1 F20\n")
        self.assertEqual(sent, ["G20", "G0Z0.25", "G0X1Y1", "G1Z-0.1F20"])
        lines = []
        for _ in range(4):
            self.reply("ok")
            lines.append(self.job.line)
        self.assertEqual(lines, [2, 4, 5, 7])
        self.assertEqual(self.link.modal["units"], "G20")  # (a job's modal words count too)

    def test_a_long_line_goes_alone(self):
        self.ready()
        long_line = "G1X1." + "0" * 121 + "1"           # 127 characters (FluidNC's most): 128 bytes with its "\n"
        sent = self.start_job(f"G0 X1\nG0 X2\n{long_line}\nG0 X3\n")
        self.assertEqual(sent, ["G0X1", "G0X2"])
        self.reply("ok")
        self.assertEqual(self.sent(), [])
        self.reply("ok")
        self.assertEqual(self.sent(), [long_line])      # once nothing else is in FluidNC's hands
        self.assertEqual(self.in_flight(), 128)
        self.reply("ok")
        self.assertEqual(self.sent(), ["G0X3"])

    def test_job_events(self):
        self.ready()
        self.start_job(LONG)
        first = self.jobs()[0]
        self.assertEqual(set(first), {"name", "state", "total", "sent", "acked", "line", "started", "elapsed",
                                      "why", "error"})
        self.assertEqual((first["name"], first["state"], first["total"], first["sent"], first["acked"],
                          first["line"], first["why"], first["error"]), ("t.nc", "running", 30, 0, 0, None, None, None))
        self.assertAlmostEqual(first["started"], time.time(), delta=5)
        self.assertEqual(self.log("info")[-2:], ["Job started: t.nc, 30 lines", "Run: inches (G20) and absolute "
                                                 "(G90) first; the program's own G21/G91 still win"])
        n = len(self.jobs())
        for _ in range(10):                             # 10 replies in 0.1 s: no event for each
            self.reply("ok")
            self.tick(0)
        self.assertEqual(len(self.jobs()), n)
        self.tick(0.5)
        self.assertEqual(len(self.jobs()), n + 1)      # then the progress, at most every 0.5 s
        self.assertEqual((self.jobs()[-1]["acked"], self.jobs()[-1]["sent"]), (10, 22))
        self.tick(0.3)
        self.assertEqual(len(self.jobs()), n + 1)      # (nothing new)
        self.tick(0.8)
        self.assertEqual(len(self.jobs()), n + 2)      # (but every second, for the time shown)
        self.assertEqual(self.m.snapshot()["job"]["acked"], 10)

    def test_done_once_a_report_after_the_last_ok_says_idle(self):
        self.ready()
        self.assertEqual(self.start_job("G0 X1\nG0 X2\n"), ["G0X1", "G0X2"])
        self.reply("ok", "ok")
        self.reply(IDLE, dt=0.02)                       # made too soon after the last ok to tell
        self.assertEqual(self.job.state, "running")
        self.reply(RUN, dt=0.1)                         # the last moves still run
        self.assertEqual(self.job.state, "running")
        self.t += 63
        self.reply(IDLE, dt=0.1)
        self.assertEqual(self.job.state, "done")
        self.assertEqual(self.log("info")[-1], "Job done: t.nc, 2 lines in 1:03")
        ev = self.jobs()[-1]
        self.assertEqual((ev["state"], ev["acked"], ev["line"], ev["why"]), ("done", 2, 2, None))
        self.assertEqual(ev["elapsed"], self.job.elapsed(self.t + 100))   # (the time stops at the end)
        self.assertEqual(self.m.snapshot()["job"]["state"], "done")
        self.m.send("G0 X0")                            # the user's lines go again
        self.tick()
        self.assertEqual(self.sent(), ["G0 X0"])

    def test_a_dwell_and_m3_count_as_busy_or_get_their_time(self):
        self.ready()
        self.start_job("G4 P15\nG0 X1\n")
        for _ in range(23):                             # 23 s Idle while dwelling: 10 s + its 15 s not yet
            self.reply(IDLE, dt=1.0)
            self.tick(0)
        self.assertEqual(self.job.state, "running")
        self.reply(IDLE, dt=2.1)
        self.tick(0)
        self.assertEqual(self.job.state, "paused")


class FirstLine(Stage3):
    """Every job starts with FluidSender's G20 G90: a program without units runs in inches (and in absolute)
    whatever an earlier program or typed line left in force (M2/M30 keep the units)."""

    def test_inches_and_absolute_first(self):
        self.ready()
        for line in ("G21", "G91"):                     # (left by an earlier program, say)
            self.m.send(line)
            self.tick(0.01)
            self.sent()
            self.reply("ok")
        self.reply(IDLE, dt=0.3)
        self.assertEqual((self.link.modal["units"], self.link.modal["distance"]), ("G21", "G91"))
        self.load("G0 X1 Y1\nG1 Z-0.1 F20\n")
        self.m.run()
        self.assertEqual(self.log("info")[-1], "Run: inches (G20) and absolute (G90) first; the program's own "
                                               "G21/G91 still win")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G20 G90", "G0X1Y1", "G1Z-0.1F20"])
        self.assertEqual(self.in_flight(), 8 + 7 + 11)  # (counted like the program's lines)
        self.assertEqual((self.job.total, self.job.sent), (2, 2))   # (but not one of them)
        self.reply("ok")
        self.assertEqual((self.link.modal["units"], self.link.modal["distance"]), ("G20", "G90"))
        self.assertEqual((self.job.acked, self.job.line), (0, None))
        self.reply("ok", "ok", IDLE, dt=0.06)
        self.assertEqual((self.job.state, self.job.acked, self.job.line), ("done", 2, 2))

    def test_the_programs_own_units_still_win(self):
        self.ready()
        self.assertEqual(self.start_job("G21 G91\nG0 X10\n"), ["G21G91", "G0X10"])
        self.reply("ok")
        self.assertEqual((self.link.modal["units"], self.link.modal["distance"]), ("G21", "G91"))

    def test_an_error_on_it_pauses_like_any_line(self):
        self.ready()
        self.load("G0 X1\n")
        self.m.run()
        self.tick(0.01)
        self.sent()
        self.reply("error:20")
        self.assertEqual((self.job.state, self.job.why), ("paused", "error on FluidSender's first line"))
        self.assertEqual(self.log("error")[-1], "FluidSender's first line was refused by FluidNC (error:20 unsupported "
                                                "or invalid G-code) and won't run. Resume goes on with the next lines; "
                                                "Stop ends the job.")
        self.assertEqual(self.job.acked, 0)


# ------------------------------------------------------------------ jobs: run refused

class RunRefused(Stage3):
    def test_refused(self):
        self.refused(self.m.run, "Not connected")
        self.ready()
        self.refused(self.m.run, "No program loaded: open a G-code file first")
        self.load("(only a comment)\n%\n")
        self.refused(self.m.run, "Nothing to run: the program has no G-code lines")
        self.load("G0 X1\n")
        for report, why in (("<Alarm|MPos:0,0,0|FS:0,0>", "ALARM: Unlock or Home first"),
                            ("<Run|MPos:0,0,0|FS:100,0>", "Running: wait until FluidNC is Idle"),
                            (HOLD0, "Hold: Resume or Soft Reset first"), (base.JOG, "Jogging: wait until it has stopped"),
                            ("<Check|MPos:0,0,0|FS:0,0>", "Check mode ($C) is on")):
            self.reply(report)
            self.refused(self.m.run, why)
        self.reply(IDLE, dt=0.06)
        self.link.inch = True                           # (after a reset, before our G20)
        self.refused(self.m.run, "FluidNC is not set to inches (G20) yet; try again in a second")
        self.link.inch_tries = machine.INCH_TRIES
        self.refused(self.m.run, f"Not started: {machine.NO_INCHES}")
        self.link.inch = False
        modal = self.link.modal
        self.link.modal = None
        self.refused(self.m.run, "FluidNC's units are not known yet; try again in a second")
        self.link.modal = modal
        self.m.send("G4 P1")
        self.refused(self.m.run, "Waiting for FluidNC to finish 'G4 P1'")
        self.tick()
        self.sent()
        self.reply("ok")
        self.refused(self.m.run, "FluidNC is busy; try again in a second")    # no report since that ok
        self.reply(IDLE, dt=0.06)
        self.running(x=1)
        self.refused(self.m.run, base.JOGGING)
        self.m.jog({"stop": "all"})
        self.stopped()
        self.m.begin_activity("scan")
        self.refused(self.m.run, SCANNING)
        self.m.end_activity("scan")
        self.m.run()
        self.refused(self.m.run, JOB_RUNNING)


# ------------------------------------------------------------------ jobs: pauses, Hold and Resume

class Pauses(Stage3):
    def test_an_error_holds_at_once_and_resume_goes_on(self):
        self.ready()
        text = LONG.replace("G0X2.0002", "G0X2Q.02", 1)
        self.assertEqual(len(self.start_job(text)), 12)
        self.reply("ok")
        self.sent()
        self.reply("error:20")
        self.assertEqual(self.realtime(), b"!")         # at once, before anything else
        self.assertEqual((self.job.state, self.job.why), ("paused", "error on line 2"))
        self.assertEqual(self.job.error, "line 2: G0X2Q.02: error:20 (unsupported or invalid G-code)")
        self.assertEqual(self.log("error")[-1], "Line 2 was refused by FluidNC (error:20 unsupported or invalid "
                                                "G-code) and won't run. Resume goes on with the next lines; Stop ends "
                                                "the job.")
        self.assertEqual((self.log("tx")[-1], self.log("rx")[-1]), ("G0X2Q.02", "error:20"))
        self.assertEqual(self.jobs()[-1]["state"], "paused")
        self.reply("ok", "ok")                          # (FluidNC took two more before the hold)
        self.reply("error:2")                           # another error meanwhile: told, the first one kept
        self.assertEqual(self.log("error")[-1], "Job: line 5: G0X5.0005: error:2 (bad number format)")
        self.assertEqual(self.job.error, "line 2: G0X2Q.02: error:20 (unsupported or invalid G-code)")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # paused: nothing more goes
        self.assertEqual(self.job.acked, 5)
        self.reply(HOLD0, dt=0.06)
        self.m.realtime("resume")
        self.assertEqual(self.realtime(), b"~")
        self.assertEqual((self.job.state, self.job.why, self.job.error), ("running", None, None))
        self.assertEqual(len(self.sent()), 4)           # the room the answers made (80 of 128 bytes in use)
        self.reply(HOLD0, dt=0.02)                      # (made before the '~')
        self.assertEqual(self.job.state, "running")

    def test_hold_and_resume_only_at_hold_0(self):
        self.ready()
        self.start_job(LONG)
        self.reply(RUN)
        self.m.realtime("hold")
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual((self.job.state, self.job.why), ("paused", "Feed hold"))
        self.reply("ok")
        self.assertEqual(self.sent(), [])               # nothing goes while paused
        self.reply(HOLD1, dt=0.06)                      # still slowing down
        self.m.realtime("resume")
        self.assertEqual(self.realtime(), b"")          # FluidNC would drop a '~' now
        self.assertEqual(self.log("info")[-1], "Resume: FluidNC is still slowing down; Resume goes once it has stopped")
        self.assertEqual(self.job.state, "paused")
        self.assertLessEqual(self.link.next_poll - self.t, machine.FAST_POLL)
        self.reply(HOLD1, dt=0.1)
        self.assertEqual(self.realtime(), b"")
        self.reply(HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"~")         # at the first Hold:0
        self.assertEqual(self.job.state, "running")
        self.assertEqual(len(self.sent()), 1)
        self.reply(HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"")          # once only
        self.reply(HOLD0, dt=0.1)                       # still held 0.2 s after the '~' (it didn't take it):
        self.assertEqual((self.job.state, self.job.why), ("paused", "Feed hold"))   # paused again, said so

    def test_resume_right_after_hold_waits_for_a_report(self):
        self.ready()
        self.start_job(LONG)
        self.reply(RUN, dt=0.1)
        self.m.realtime("hold")
        self.m.realtime("resume")                       # the last report is from before the '!'
        self.assertEqual(self.realtime(), b"!")
        self.reply(HOLD0, dt=0.02)                      # (too soon to show the hold)
        self.assertEqual(self.realtime(), b"")
        self.reply(HOLD0, dt=0.04)
        self.assertEqual(self.realtime(), b"~")
        self.assertEqual(self.job.state, "running")

    def test_resume_gives_up_after_5_s(self):
        self.ready()
        self.start_job(LONG)
        self.m.realtime("hold")
        self.reply(HOLD1, dt=0.06)
        self.m.realtime("resume")
        for _ in range(49):
            self.reply(HOLD1, dt=0.1)
            self.tick(0)
        self.assertEqual(self.log("error"), [])
        self.reply(HOLD1, dt=0.11)
        self.tick(0)
        self.assertEqual(self.log("error")[-1], "Resume not sent: FluidNC was still slowing down (Hold:1) after 5 s, "
                                                "and it ignores Resume until it has stopped. Press Resume again.")
        self.reply(HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"!")         # (no '~' after giving up)
        self.assertEqual(self.job.state, "paused")

    def test_m0_and_fluidncs_own_hold_pause_the_job(self):
        self.ready()
        self.assertEqual(self.start_job("G0 X1\nM0 (change the bit)\nG0 X2\n"), ["G0X1", "M0", "G0X2"])
        self.reply("ok")                                # FluidNC waits at M0 (its ok comes after Resume)
        self.reply(HOLD0, dt=0.06)
        self.assertEqual((self.job.state, self.job.why), ("paused", "M0 in the program (line 2): press Resume to go on"))
        self.m.realtime("resume")
        self.assertEqual((self.realtime(), self.job.state), (b"~", "running"))
        self.reply("ok", "ok", RUN)
        self.reply("<Hold:1|MPos:1.000,0.000,0.000|FS:500,0|Pn:H>", dt=0.06)   # its feed hold button
        self.assertEqual((self.job.state, self.job.why), ("paused", "Feed hold"))
        self.reply(HOLD0, dt=0.1)
        self.reply(RUN, dt=0.1)                         # its cycle start button
        self.assertEqual(self.job.state, "running")
        self.assertEqual(self.log("info")[-1], "FluidNC is running again (its cycle start button?): the job goes on")
        self.reply(IDLE, dt=0.1)
        self.assertEqual(self.job.state, "done")
        self.reply("<Door:1|MPos:0,0,0|FS:0,0>")

    def test_m1_is_not_a_pause(self):
        """M1 does nothing in FluidNC 3.9.4: a hold at that line is the user's (or FluidNC's button)."""
        self.ready()
        self.start_job("G0 X1\nM1\nG0 X2\n")
        self.reply("ok")
        self.reply(HOLD0, dt=0.06)
        self.assertEqual((self.job.state, self.job.why), ("paused", "Feed hold"))

    def test_fluidncs_own_words_for_an_error(self):
        """verbose_errors (on by default): "[MSG:ERR: ...]" after an error:N answers no line; the job's error says
        it in FluidNC's words."""
        self.ready()
        self.start_job(LONG.replace("G0X2.0002", "G0X2Q.02", 1))
        self.reply("ok", "error:20", "[MSG:ERR: Unsupported GCode command]")
        self.assertEqual(self.job.error, "line 2: G0X2Q.02: error:20 (Unsupported GCode command)")
        self.assertEqual(self.jobs()[-1]["error"], self.job.error)
        self.assertEqual((self.job.acked, self.in_flight()), (2, 110))   # (the message answered nothing)
        self.reply("ok", "error:2", "[MSG:ERR: Bad GCode number format]")   # another one meanwhile: the first stays
        self.assertEqual(self.job.error, "line 2: G0X2Q.02: error:20 (Unsupported GCode command)")
        self.assertEqual((self.job.acked, self.in_flight()), (4, 90))
        self.assertEqual(self.log("rx")[-1], "[MSG:ERR: Bad GCode number format]")    # (shown)
        self.reply("[MSG:ERR: Something else]")         # not right after an error: nothing to do with the job
        self.assertEqual(self.job.error, "line 2: G0X2Q.02: error:20 (Unsupported GCode command)")

    def test_oks_while_slowing_down_are_counted_exactly(self):
        """After the '!' of an error FluidNC still reads and answers lines while it slows down (Hold:1), and
        none at Hold:0: every answer counts, so what is in its hands stays exactly known."""
        self.ready()
        self.start_job(LONG.replace("G0X1.0001", "G0X1Q.001", 1))        # (10 bytes, like the others)
        self.reply("error:20")                          # line 1: paused, 11 lines (110 bytes) still in its hands
        self.reply(HOLD1, dt=0.06)
        self.reply("ok", "ok", "ok", HOLD1)             # three more taken while it slows down
        self.assertEqual((self.job.acked, self.in_flight(), self.job.line), (4, 80, 4))
        self.reply(HOLD0, dt=0.1)
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # (paused: nothing more goes)
        self.m.realtime("resume")
        self.assertEqual(self.realtime(), b"!~")
        self.assertEqual(len(self.sent()), 4)           # 80 + 4 x 10 = 120: a fifth would make 130
        self.assertEqual(self.in_flight(), 120)
        while self.job.acked < 30:
            self.reply("ok")
            self.assertLessEqual(self.in_flight(), machine.JOB_BUFFER)
            self.sent()
        self.reply(IDLE, dt=0.06)
        self.assertEqual((self.job.state, self.job.sent, self.in_flight()), ("done", 30, 0))

    def test_the_door_pauses_it(self):
        self.ready()
        self.start_job(LONG)
        self.reply("<Door:2|MPos:0,0,0|FS:0,0>", dt=0.06)
        self.assertEqual((self.job.state, self.job.why), ("paused", "the safety door is open"))

    def test_resume_without_a_job(self):
        self.ready()
        self.reply(HOLD0, dt=0.06)
        self.m.realtime("resume")
        self.assertEqual(self.realtime(), b"~")         # at once in Hold:0
        self.reply(HOLD1, dt=0.06)
        self.m.realtime("resume")
        self.assertEqual(self.realtime(), b"")
        self.reply(HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"~")
        self.reply(IDLE, dt=0.06)
        self.m.realtime("resume")                       # (Idle: harmless, as before)
        self.assertEqual(self.realtime(), b"~")

    def test_an_error_while_held_is_the_jobs_error(self):
        """FluidNC still reads and answers lines while it holds (Feed hold with room left in its planner): an error
        then is the job's error as when running (it was only told), so Resume follows the rules after an error (the
        pendant leaves it to the page) and goes on with the next line."""
        self.ready()
        self.start_job(LONG)
        self.reply("ok", "ok")
        self.sent()
        self.reply(RUN, dt=0.1)
        self.m.realtime("hold")
        self.assertEqual((self.realtime(), self.job.state, self.job.error), (b"!", "paused", None))
        self.reply("error:20")                          # line 3, read while it slows down
        self.assertEqual((self.job.state, self.job.why), ("paused", "error on line 3"))
        self.assertEqual(self.job.error, "line 3: G0X3.0003: error:20 (unsupported or invalid G-code)")
        self.assertEqual(self.jobs()[-1]["error"], self.job.error)
        self.assertEqual(self.log("error")[-1], "Line 3 was refused by FluidNC (error:20 unsupported or invalid "
                                                "G-code) and won't run. Resume goes on with the next lines; Stop ends "
                                                "the job.")
        self.assertEqual(self.realtime(), b"!")         # (held already: changes nothing)
        self.reply("[MSG:ERR: Unsupported GCode command]")  # (verbose_errors: FluidNC's words)
        self.assertEqual(self.job.error, "line 3: G0X3.0003: error:20 (Unsupported GCode command)")
        self.reply(HOLD0, dt=0.06)
        self.m.realtime("resume")
        self.assertEqual((self.realtime(), self.job.state, self.job.error), (b"~", "running", None))

    def test_an_error_cancels_a_resume_waiting_for_hold_0(self):
        """Resume pressed while FluidNC slows down waits for Hold:0; a line refused meanwhile cancels it (the user
        pressed it before knowing of that error): no '~' at Hold:0, and Resume again goes on. Also for a second
        error while paused by an error, which is told (the first one stays the job's)."""
        self.ready()
        self.start_job(LONG)
        self.reply("ok", "ok")
        self.sent()
        self.reply(RUN, dt=0.1)
        self.m.realtime("hold")
        self.reply(HOLD1, dt=0.06)
        self.m.realtime("resume")
        self.assertIsNotNone(self.link.resume_at)
        self.reply("error:20")                          # line 3
        self.assertIsNone(self.link.resume_at)
        self.assertEqual(self.log("error")[-1], "Line 3 was refused by FluidNC (error:20 unsupported or invalid "
                                                "G-code) and won't run. Resume goes on with the next lines; Stop ends "
                                                "the job. The Resume waiting for FluidNC to stop is cancelled: press "
                                                "Resume again to go on.")
        self.reply(HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"!!")        # the hold, the error's '!'; no '~'
        self.assertEqual((self.job.state, self.job.why), ("paused", "error on line 3"))
        self.reply(HOLD1, dt=0.1)                       # (say it slows down again, so the next Resume waits)
        self.m.realtime("resume")
        self.assertIsNotNone(self.link.resume_at)
        self.reply("error:2")                           # line 4, another error: told, the first one stays
        self.assertIsNone(self.link.resume_at)
        self.assertEqual(self.log("error")[-1], "Job: line 4: G0X4.0004: error:2 (bad number format) The Resume "
                                                "waiting for FluidNC to stop is cancelled: press Resume again to go "
                                                "on.")
        self.assertEqual(self.job.error, "line 3: G0X3.0003: error:20 (unsupported or invalid G-code)")
        self.reply(HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"")          # no '~'
        self.m.realtime("resume")
        self.assertEqual((self.realtime(), self.job.state), (b"~", "running"))

    def test_an_error_after_no_answer_holds_and_is_the_jobs_error(self):
        """The job paused because FluidNC stopped answering (no '!' then); it answers again with an error: FluidNC
        holds at once, as when running (it would run the next lines it has), and that line is the job's error."""
        self.ready()
        self.start_job("G0 X1\nG0 X2Q.02\nG0 X3\n")
        self.reply("ok")
        for _ in range(12):
            self.reply(IDLE, dt=1.0)
            self.tick(0)
        self.assertEqual((self.job.state, self.job.why), ("paused", "FluidNC stopped answering"))
        self.assertEqual(self.realtime(), b"")
        self.reply("error:20")
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual((self.job.state, self.job.why), ("paused", "error on line 2"))
        self.assertEqual(self.job.error, "line 2: G0X2Q.02: error:20 (unsupported or invalid G-code)")

    def test_the_pendants_toggle_holds_while_a_resume_waits(self):
        """A Resume waiting for Hold:0 (FluidNC still slowing down): the pendant's Pause/Resume key holds, which
        cancels that Resume, rather than resume again (pressing again meant to hold)."""
        self.ready()
        self.start_job(LONG)
        self.reply(RUN, dt=0.1)
        self.m.realtime("hold")
        self.reply(HOLD1, dt=0.06)
        self.m.realtime("resume")
        self.assertEqual(self.realtime(), b"!")
        self.assertIsNotNone(self.link.resume_at)
        self.assertEqual(self.m.pause_toggle(), {"did": "hold"})
        self.assertIsNone(self.link.resume_at)
        self.assertEqual(self.realtime(), b"!")
        self.reply(HOLD0, dt=0.1)
        self.assertEqual((self.realtime(), self.job.state), (b"", "paused"))    # no '~'
        self.assertEqual(self.m.pause_toggle(), {"did": "resume"})    # (nothing waits now: the toggle resumes)
        self.assertEqual((self.realtime(), self.job.state), (b"~", "running"))

    def test_no_answer_pauses_the_job(self):
        self.ready()
        self.start_job("G0 X1\nG0 X2\n")
        for _ in range(30):                             # FluidNC busy: the wait doesn't count
            self.reply(RUN, dt=1.0)
            self.tick(0)
        self.assertEqual(self.job.state, "running")
        for _ in range(5):
            self.reply(IDLE, dt=1.0)
            self.tick(0)
        self.assertFalse(self.link.current_status(self.t)["stalled"])
        self.reply(IDLE, dt=0.5)
        self.assertTrue(self.link.current_status(self.t)["stalled"])     # past 5 s while not busy
        for _ in range(5):
            self.reply(IDLE, dt=1.0)
            self.tick(0)
        self.assertEqual((self.job.state, self.job.why), ("paused", "FluidNC stopped answering"))
        self.assertEqual(self.job.error, "line 1: G0X1: FluidNC stopped answering")
        self.assertEqual(self.log("error")[-1], "No reply to line 1 of the job (G0X1) within 10 s: FluidNC stopped "
                                                "answering (2 lines not answered). The job is paused: nothing more is "
                                                "sent. Stop ends it; Resume goes on if FluidNC answers again.")
        self.assertEqual(self.realtime(), b"")          # never a reset or a resend by itself
        self.assertEqual(self.sent(), [])
        self.reply("ok")                                # it answers after all
        self.assertEqual((self.job.acked, self.job.state), (1, "paused"))
        self.m.realtime("resume")
        self.assertEqual((self.realtime(), self.job.state), (b"~", "running"))
        self.reply("ok", IDLE, dt=0.06)
        self.assertEqual(self.job.state, "done")


# ------------------------------------------------------------------ jobs: the safe stop

class SafeStop(Stage3):
    def running_job(self):
        self.ready()
        self.start_job(LONG)
        self.reply("ok", "ok")
        self.sent()
        self.reply(RUN, dt=0.1)

    def test_hold_then_reset_once_stopped(self):
        self.running_job()
        self.assertEqual(self.m.stop(), {"stopped": "job"})
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual((self.job.state, self.job.why), ("stopping", "Stopped by the user"))
        self.assertEqual(self.jobs()[-1]["state"], "stopping")
        self.assertEqual(self.log("info")[-1], "Stop: Feed Hold, then Soft Reset once FluidNC has stopped")
        self.assertLessEqual(self.link.next_poll - self.t, machine.FAST_POLL)
        self.reply(HOLD0, dt=0.02)                      # (made before the '!')
        self.assertEqual(self.realtime(), b"")
        self.reply(HOLD1, dt=0.1)                       # slowing down: a reset now would lose the position
        self.assertEqual(self.realtime(), b"")
        self.refused(lambda: self.m.realtime("resume"), "The job is being stopped")
        self.reply("ok")                                # (a line FluidNC took before the hold)
        self.tick(0.01)
        self.assertEqual(self.sent(), [])
        self.reply(HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"\x18")      # stopped: now the reset keeps the position
        self.assertEqual(self.job.state, "stopped")
        self.assertEqual(self.log("info")[-1], "Job stopped: t.nc, 3 of 30 lines done in 0:00 (up to line 3). "
                                               "Stopped by the user.")
        self.assertEqual(list(self.link.job.inflight), [])   # (FluidNC drops them: never answered)
        self.reply("ok")                                # a reply already on its way: only shown
        self.assertEqual(self.job.acked, 3)
        self.reply(IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), [])               # nothing before the reset's banner
        self.reply(BANNER, IDLE)
        self.tick()
        self.assertEqual(self.sent(), [])               # (nor before a report a moment after it: see Leftover)
        self.reply(IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])          # inches again, as after any reset
        self.assertEqual(self.job.state, "stopped")

    def test_already_held(self):
        self.running_job()
        self.reply("error:20")                          # (paused, '!' sent)
        self.reply(HOLD0, dt=0.06)
        self.realtime()
        self.m.stop()
        self.assertEqual(self.realtime(), b"")          # already held: no second '!'
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.assertEqual(self.job.state, "stopped")

    def test_stop_right_after_a_resume_holds_first(self):
        """Right after Resume the last report still says Hold, but FluidNC runs again: Stop holds it first, and
        resets only once it has stopped (the pendant's Pause, Pause, Stop)."""
        self.running_job()
        self.m.realtime("hold")
        self.reply(HOLD0, dt=0.06)
        self.m.realtime("resume")
        self.assertEqual((self.realtime(), self.job.state), (b"!~", "running"))
        self.assertEqual(self.m.stop(), {"stopped": "job"})     # (the last report: Hold:0, from before the '~')
        self.assertEqual(self.realtime(), b"!")
        self.reply(RUN, dt=0.06)
        self.reply(HOLD1, dt=0.1)
        self.assertEqual(self.realtime(), b"")
        self.reply(HOLD0, dt=0.3)
        self.assertEqual((self.realtime(), self.job.state), (b"\x18", "stopped"))

    def test_reset_anyway_after_10_s(self):
        self.running_job()
        self.m.stop()
        self.realtime()
        for _ in range(99):                             # it doesn't stop
            self.reply(RUN, dt=0.1)
            self.tick(0)
        self.assertEqual((self.realtime(), self.job.state), (b"", "stopping"))
        self.reply(RUN, dt=0.1)
        self.tick(0.01)
        self.assertEqual(self.realtime(), b"\x18")
        self.assertEqual(self.log("error")[-1], "FluidNC did not stop within 10 s of the feed hold: sent Soft Reset "
                                                "anyway. The position may be lost: home before the next job.")
        self.assertEqual((self.job.state, self.job.why), ("stopped", "Stopped by the user (FluidNC did not stop within "
                                                          "10 s: reset anyway, the position may be lost)"))

    def test_idle_after_the_hold_counts_as_stopped(self):
        self.running_job()
        self.m.stop()
        self.reply(IDLE, dt=0.06)
        self.assertEqual(self.realtime(), b"!\x18")

    def test_stop_twice_is_one_stop(self):
        self.running_job()
        self.m.stop()
        self.reply(HOLD1, dt=0.06)
        at = self.link.halt["at"]
        self.assertEqual(self.m.stop(), {"stopped": "job"})
        self.assertEqual(self.link.halt["at"], at)
        self.assertEqual(self.log("info").count("Stop: Feed Hold, then Soft Reset once FluidNC has stopped"), 1)

    def test_an_alarm_meanwhile_ends_it_and_the_reset_still_goes(self):
        """An ALARM during the safe stop ends the job, and the Soft Reset the user asked for still goes: nothing
        moves in ALARM, and the reset turns the spindle and the vacuum/AUX off (an ALARM leaves them on)."""
        self.running_job()
        self.m.stop()
        self.reply("ALARM:1")
        self.assertEqual((self.job.state, self.job.why), ("failed", "ALARM:1 (hard limit: position likely lost, "
                                                          "home again)"))
        self.assertIsNone(self.link.halt)
        self.assertEqual(self.realtime(), b"!\x18")     # the hold, then the reset at once
        self.assertEqual(self.log("info")[-1], "ALARM:1 during the stop: sent the Soft Reset anyway (nothing moves "
                                               "in ALARM, and the reset turns the spindle and the vacuum/AUX off)")
        self.assertFalse(self.link.alarm["outputs"])    # (the reset turns them off: no warning for that)
        self.assertNotIn(machine.OUTPUTS_ON, self.log("error"))
        self.reply(HOLD0, dt=0.6)
        self.tick(11)
        self.assertEqual(self.realtime(), b"")          # nothing more is sent

    def test_an_alarm_without_a_stop_sends_no_reset(self):
        self.running_job()
        self.reply("ALARM:1")
        self.assertEqual(self.job.state, "failed")
        self.tick(11)
        self.assertEqual(self.realtime(), b"")          # (FluidSender never resets by itself)
        self.assertIn(machine.OUTPUTS_ON, self.log("error"))

    def test_soft_reset_during_a_job_ends_it_at_once(self):
        self.running_job()
        self.m.realtime("reset")
        self.assertEqual(self.realtime(), b"\x18")
        self.assertEqual((self.job.state, self.job.why), ("stopped", "Soft Reset"))
        self.assertEqual(list(self.link.job.inflight), [])
        self.m.stop()                                   # nothing left to stop: nothing more
        self.assertEqual(self.realtime(), b"")

    def test_stop_without_a_job(self):
        self.refused(self.m.stop, "Not connected")
        self.ready()
        self.assertEqual(self.m.stop(), {"stopped": None})   # nothing going on
        self.assertEqual(self.realtime(), b"")
        self.running(x=1)
        self.assertEqual(self.m.stop(), {"stopped": "jog"})  # a jog: jog cancel, never a reset
        self.assertEqual(self.realtime(), b"\x85")
        self.stopped()
        self.m.goto("z")                                # a move: the safe stop too
        self.tick(0.01)
        self.sent()
        self.reply("ok", RUN)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"!")
        self.refused(lambda: self.m.send("G0 X1"), "Stopping: FluidNC is being held, then reset; try again in a second")
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.assertEqual(self.log("info")[-1], "Stopped: FluidNC held, then Soft Reset (the position is kept)")


class StopRace(Stage3):
    """Stop right after FluidNC answered a move (its ok comes as the move is planned, before any report shows
    Run) must hold it all the same; and nothing may resume a move while the safe stop holds it."""

    def moved(self, line="G0 X5"):
        self.m.send(line)
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply("ok")                                # it starts moving now; the last report still says Idle

    def test_stop_right_after_a_moves_ok_holds_it(self):
        self.ready()
        self.moved()
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"!")
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")      # once stopped: the position is kept

    def test_the_pendants_go_to_keys_and_a_scan_move_too(self):
        self.ready()
        self.moved("G53 G0 X0")                         # (pendant key 19)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"!")
        self.reply(IDLE, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(BANNER)
        self.reply(IDLE, dt=0.06)
        self.tick()
        self.sent()
        self.reply("ok", IDLE, dt=0.3)                  # (G20)
        self.m.begin_activity("scan")
        self.link.queue.append(machine.Line("G20 G90 G0 X3 Y4", tag="scan"))
        self.tick(0.01)
        self.sent()
        self.reply("ok")
        self.assertEqual(self.m.stop(), {"stopped": "scan"})
        self.assertEqual(self.realtime(), b"!")

    def test_nothing_to_stop_once_a_report_shows_it_still(self):
        self.ready()
        self.moved()
        self.reply(IDLE, dt=0.3)                        # (a 0.01 in move: already over)
        self.assertEqual(self.m.stop(), {"stopped": None})
        self.assertEqual(self.realtime(), b"")
        self.moved("$G")                                # a $ read can't move
        self.assertEqual(self.m.stop(), {"stopped": None})
        self.moved("M5")                                # any G-code line counts (the rule is kept simple)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})

    def test_no_report_for_a_while_counts_as_moving(self):
        self.ready()
        self.t += 2.1
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"!")

    def test_resume_refused_while_the_safe_stop_holds(self):
        """Pendant Stop, then its Pause/Resume key (or Resume) before FluidNC is reset: a '~' would let the move
        run on, and the reset would then hit it moving (ALARM:3, the position lost)."""
        self.ready()
        self.moved("G0 X20")
        self.reply(RUN, dt=0.06)
        self.m.realtime("hold")
        self.reply(HOLD0, dt=0.3)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.realtime()
        self.refused(self.m.pause_toggle, "Stopping: FluidNC is being held, then reset")
        self.refused(lambda: self.m.realtime("resume"), "Stopping: FluidNC is being held, then reset")
        self.assertEqual(self.realtime(), b"")          # no '~'
        self.assertIsNotNone(self.link.halt)
        self.reply(HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(BANNER)
        self.reply(IDLE, dt=0.06)                       # (a clean report after the reset: see the next test)
        self.reply(HOLD0, dt=0.06)                      # (some other hold after the reset: Resume is fine again)
        self.assertTrue(self.link.resume(self.t))

    def test_no_resume_during_a_reset_or_just_after_it(self):
        """Resume (the page's button, a typed '~', the pendant's toggle: all Link.resume) is refused during our reset
        and after FluidNC's banner until a report a moment later shows it ran nothing from before the reset: a '~'
        there would run a line left from before the reset (see Link.stray), or one the stop held."""
        self.ready()
        self.moved("G0 X20")
        self.reply(RUN, dt=0.06)
        self.m.realtime("hold")
        self.reply(HOLD0, dt=0.3)
        self.m.realtime("reset")
        self.assertEqual(self.realtime(), b"!\x18")
        self.refused(lambda: self.m.realtime("resume"), machine.RESETTING)
        self.refused(self.m.pause_toggle, machine.RESETTING)       # (the last report still says Hold)
        self.reply(BANNER)
        self.refused(lambda: self.m.realtime("resume"), machine.JUST_RESET)
        self.reply(HOLD0, dt=0.06)                      # held after the banner (a line from before it): no '~'
        self.refused(lambda: self.m.realtime("resume"), machine.JUST_RESET)
        self.refused(self.m.pause_toggle, machine.JUST_RESET)
        self.assertEqual(self.realtime(), b"")
        self.reply(IDLE, dt=0.06)                       # clean: it ran nothing from before
        self.reply(HOLD0, dt=0.06)
        self.m.realtime("resume")
        self.assertEqual(self.realtime(), b"~")


class StopInAnAlarm(Stage3):
    """FluidNC takes a feed hold in a critical alarm (a hard or soft limit, reported as Alarm): it goes to Hold, and
    the reset after it lands in Idle, unhomed, with no alarm (both versions; on 4.1.1 a '~' even leaves it without a
    reset). So in an alarm Stop and Disconnect send the Soft Reset at once, with no '!', and Feed Hold and Resume
    are refused. Homing ignores '!' ("use Reset instead"): Stop resets at once there too."""

    def crashed(self):
        """A job with lines FluidNC hasn't answered, then a hard limit (critical)."""
        self.ready()
        self.start_job(LONG)
        self.reply("ok", "ok")
        self.sent()
        self.reply(RUN, dt=0.1)
        self.reply("[MSG:INFO: ALARM: Hard Limit]", "ALARM:1")
        self.assertEqual(self.job.state, "failed")
        self.assertTrue(self.link.job.inflight)         # (FluidNC never answers them now)
        self.reply("<Alarm|MPos:1.000,0.000,0.000|FS:0,0>", dt=0.6)     # (4.1.1 says Alarm at once)
        self.reply("[MSG:ERR: Reset to continue]")
        self.assertTrue(self.link.alarm["critical"])
        self.realtime()

    def test_stop_in_a_critical_alarm_resets_at_once_without_a_feed_hold(self):
        self.crashed()
        self.assertTrue(self.link.may_move(self.t))     # (its lines are unanswered: it may move, as far as it knows)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"\x18")      # no '!'
        self.assertIsNone(self.link.halt)
        self.assertEqual(self.log("info")[-1], "Stop: FluidNC is in ALARM: Soft Reset at once, no Feed Hold")
        self.assertEqual(list(self.link.job.inflight), [])
        self.reply("[MSG:INFO: ALARM: Unhomed]", "ALARM:14", BANNER)    # (the limit unhomed every axis)
        self.assertEqual(self.link.alarm, {"code": 14, "critical": False, "text": "ALARM:14", "outputs": False})
        self.m.stop()                                   # nothing left to stop
        self.assertEqual(self.realtime(), b"")

    def test_hold_and_resume_are_refused_in_an_alarm(self):
        self.crashed()
        for cmd in ("hold", "resume"):
            self.refused(lambda: self.m.realtime(cmd), machine.IN_ALARM)
        self.refused(self.m.pause_toggle, "Nothing to hold or resume (FluidNC is Alarm)")
        self.assertEqual(self.realtime(), b"")
        self.m.realtime("status")
        self.m.realtime("reset")                        # what gets out of it
        self.assertEqual(self.realtime(), b"\x18")

    def test_an_alarm_whose_reports_still_say_run_counts(self):
        """FluidNC 3.9.4 prints ALARM:N first and reports the old state during its 0.5 s pause: the ALARM decides."""
        self.ready()
        self.m.send("G1 X5 F10")
        self.tick(0.01)
        self.sent()
        self.reply("ok", RUN)
        self.reply("ALARM:2", RUN)
        self.refused(lambda: self.m.realtime("hold"), machine.IN_ALARM)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(self.IDLE, dt=0.8)                   # (a plain alarm that a report clears: Feed Hold is fine)
        self.m.realtime("hold")
        self.assertEqual(self.realtime(), b"!")

    def test_disconnect_in_a_critical_alarm_resets_and_waits_for_the_banner(self):
        self.crashed()
        done = threading.Event()
        threading.Thread(target=lambda: (self.m.disconnect(), done.set()), daemon=True).start()
        end = time.monotonic() + 2
        while self.link.reset_at is None and not done.is_set():
            self.assertLess(time.monotonic(), end, "no reset")
            time.sleep(0.005)
        self.assertFalse(done.wait(0.2))                # (the port stays open until FluidNC has reset)
        with self.m.lock:
            self.assertIsNone(self.link.stopping)
            self.assertEqual(self.realtime(), b"\x18")
            self.reply("ALARM:14", BANNER)
        self.assertFalse(done.wait(0.2))                # (and a report shows it ran no line from before)
        with self.m.lock:
            self.reply("<Alarm|MPos:1.000,0.000,0.000|FS:0,0>", dt=0.06)
        self.assertTrue(done.wait(2))
        self.assertEqual(self.link.stopping, ("Disconnected from /dev/fake", "info"))
        self.assertEqual(self.realtime(), b"")          # never a '!'

    def test_disconnect_in_a_critical_alarm_without_reports_says_it_reset(self):
        """FluidNC stopped answering in a critical alarm: Disconnect sends the Soft Reset (no '!'), can't see it
        reset, closes the port, and says what it sent."""
        self.crashed()
        self.t += machine.NO_RESPONSE + 0.1
        self.m.disconnect()
        self.assertEqual(self.realtime(), b"\x18")
        self.assertEqual(self.log("info")[-2:], [
            "Stop: FluidNC is in ALARM: Soft Reset at once, no Feed Hold",
            "Disconnect: FluidNC doesn't answer, so it can't be seen to reset: sent Soft Reset and closed the port. "
            "Connect again when it answers."])
        self.assertEqual(self.link.stopping, ("Disconnected from /dev/fake", "info"))

    def test_stop_while_homing_resets_at_once(self):
        self.ready()
        self.m.home("")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["$H"])
        self.reply("<Home|MPos:0,0,0|FS:0,0>", dt=0.06)
        self.assertEqual(self.m.stop(), {"stopped": "motion"})
        self.assertEqual(self.realtime(), b"\x18")      # (FluidNC ignores '!' while homing: ALARM:6 now)
        self.assertEqual(self.log("info")[-1], "Stop: FluidNC is homing (it ignores a feed hold then): Soft Reset at "
                                               "once, no Feed Hold")


# ------------------------------------------------------------------ jobs: how else they end

class JobEnds(Stage3):
    def test_an_alarm_ends_it_and_its_lines_are_answered_first(self):
        self.ready()
        self.start_job(LONG)
        self.reply("ok", "ok")
        self.assertEqual(len(self.sent()), 2)
        self.reply("[MSG:INFO: ALARM: Hard Limit]", "ALARM:1")
        self.assertEqual((self.job.state, self.job.why), ("failed", "ALARM:1 (hard limit: position likely lost, "
                                                          "home again)"))
        self.assertEqual(self.log("error")[-2:], ["Job failed: t.nc, 2 of 30 lines done in 0:00 (up to line 2). "
                                                  "ALARM:1 (hard limit: position likely lost, home again).",
                                                  machine.OUTPUTS_ON])     # (its spindle and coolant may be on)
        self.assertTrue(self.link.alarm["outputs"])
        self.reply("ok")
        self.tick(0.01)
        self.assertEqual(self.sent(), [])               # nothing more of it
        self.assertEqual(self.job.acked, 2)
        self.m.send("$X")                               # (the user's lines are fine again ...)
        self.tick(0.01)
        self.tick(0.6)
        self.assertEqual(self.sent(), [])               # (... once FluidNC has answered the job's lines)
        self.assertEqual(self.log("info")[-1], "Waiting to send: $X  (FluidNC has not answered 11 lines of the last "
                                               "job yet; Soft Reset drops them)")
        self.refused(lambda: self.m.home(""), "FluidNC has not answered 11 lines of the last job yet; Soft Reset "
                                              "drops them")
        self.reply(*["error:9"] * 11)                   # (FluidNC refuses them in Alarm)
        self.assertEqual(self.sent(), ["$X"])

    def test_an_unexpected_reset(self):
        self.ready()
        self.start_job(LONG)
        self.reply("ok")
        self.sent()
        self.reply(BANNER)
        self.assertEqual((self.job.state, self.job.why), ("failed", "FluidNC was reset"))
        self.assertEqual(list(self.link.job.inflight), [])
        self.reply(IDLE, dt=0.06)
        self.tick()
        self.assertEqual(self.sent(), ["G20"])

    def closing(self, call):
        """Disconnect or shutdown in a thread of its own (it waits for the safe stop); returns its done Event
        once the stop is under way."""
        done = threading.Event()
        threading.Thread(target=lambda: (call(), done.set()), daemon=True).start()
        end = time.monotonic() + 2
        while self.link.halt is None and not done.is_set():
            self.assertLess(time.monotonic(), end, "no safe stop")
            time.sleep(0.005)
        return done

    def test_disconnect_does_the_safe_stop_first(self):
        """Disconnect during a job: the safe stop (Feed Hold, then Soft Reset once FluidNC has stopped), and the
        port closes only then, so FluidNC is never left holding (the spindle on) with the job's lines."""
        self.ready()
        self.start_job(LONG)
        self.reply(RUN, dt=0.1)
        done = self.closing(self.m.disconnect)
        self.assertFalse(done.wait(0.3))                # (it waits for FluidNC to stop)
        with self.m.lock:
            self.assertIsNone(self.link.stopping)       # the port still open
            self.assertEqual(self.realtime(), b"!")
            self.reply(HOLD0, dt=0.1)
            self.assertEqual(self.realtime(), b"\x18")  # the reset, once stopped
            self.reply(BANNER)
        self.assertFalse(done.wait(0.2))                # (and a report showing it ran nothing from before)
        with self.m.lock:
            self.reply(IDLE, dt=0.06)
        self.assertTrue(done.wait(2))
        self.assertEqual(self.link.stopping, ("Disconnected from /dev/fake", "info"))
        self.assertEqual((self.job.state, self.job.why), ("stopped", "Stopped by the user"))
        self.assertIn("Disconnect during the job: the port closes once FluidNC has stopped and been reset",
                      self.log("info"))
        self.m.link_closed(self.link)                   # (what the reader thread does once closed)
        self.assertEqual(self.m.snapshot()["job"]["state"], "stopped")   # kept for the page

    def test_shutdown_does_the_safe_stop_first(self):
        """Stopping the server (an update, Ctrl-C) during a move: the same safe stop before the port closes."""
        self.ready()
        self.m.send("G1 X5 F10")
        self.tick(0.01)
        self.sent()
        self.reply("ok", RUN)
        done = self.closing(self.m.shutdown)
        with self.m.lock:
            self.assertEqual(self.realtime(), b"!")
            self.reply(HOLD1, dt=0.1)
            self.assertEqual(self.realtime(), b"")      # (still slowing down: a reset now would lose the position)
            self.reply(HOLD0, dt=0.3)
            self.assertEqual(self.realtime(), b"\x18")
            self.reply(BANNER)
            self.reply(IDLE, dt=0.06)
        self.assertTrue(done.wait(3))
        self.assertEqual(self.link.stopping, ("Disconnected from /dev/fake", "info"))

    def test_disconnect_waits_for_a_line_left_from_before_the_reset(self):
        """A line typed while FluidNC held runs after the safe stop's reset (see test_machine.Leftover): the port
        stays open until that is held and reset again, never with nobody watching."""
        self.ready()
        self.m.send("G1 X5 F10")
        self.tick(0.01)
        self.sent()
        self.reply("ok", RUN)
        self.m.realtime("hold")
        self.reply(HOLD0, dt=0.3)
        self.m.send("G0 X1")
        self.tick(0.01)
        self.sent()
        self.realtime()
        done = self.closing(self.m.disconnect)
        with self.m.lock:
            self.reply(HOLD0, dt=0.06)
            self.assertEqual(self.realtime(), b"\x18")
            self.reply(BANNER, "ok")                    # G0 X1 runs after the reset
            self.assertEqual(self.realtime(), b"!")
            self.reply(HOLD0, dt=0.3)
            self.assertEqual(self.realtime(), b"\x18")
        self.assertFalse(done.wait(0.2))
        with self.m.lock:
            self.reply(BANNER)
            self.reply(IDLE, dt=0.06)
        self.assertTrue(done.wait(2))
        self.assertEqual(self.link.stopping, ("Disconnected from /dev/fake", "info"))

    def test_disconnect_when_fluidnc_doesnt_answer(self):
        """No report for a while: the stop can't be seen, so Disconnect holds it and closes at once."""
        self.ready()
        self.start_job(LONG)
        self.t += 2.1
        self.m.disconnect()                             # (no thread: it doesn't wait)
        self.assertEqual(self.realtime(), b"!")
        self.assertIn("Disconnect: FluidNC doesn't answer, so it can't be seen to stop: sent Feed Hold and closed "
                      "the port. Connect again and Soft Reset when it answers.", self.log("info"))
        self.assertEqual(self.link.stopping, ("Disconnected from /dev/fake", "info"))

    def test_disconnect_without_a_job_sends_nothing(self):
        self.ready()
        self.start_job("G0 X1\n")
        self.reply("ok", IDLE, dt=0.06)
        self.assertEqual(self.job.state, "done")
        self.reply(IDLE, dt=0.3)                        # (it stands still: no move can have just started)
        self.m.disconnect()
        self.assertEqual((self.rt, self.realtime()), ([], b""))
        self.assertEqual(self.link.stopping, ("Disconnected from /dev/fake", "info"))

    def test_port_lost(self):
        self.ready()
        self.start_job(LONG)
        self.link.stopping = ("Lost the connection to /dev/fake (EIO). Unplugged or switched off?", "error")
        self.m.link_closed(self.link)
        self.assertEqual((self.job.state, self.job.why), ("failed", "the connection was lost"))
        self.assertEqual(self.job.error, "Lost the connection to /dev/fake (EIO). Unplugged or switched off?")

    def test_never_released_during_a_job(self):
        self.ready()
        self.m.release_after = 2.0
        self.m.alone_since = self.t                     # no page open
        self.start_job("G0 X1\n")
        for _ in range(40):                             # paused by no answer, Idle for 20 s
            self.reply(IDLE, dt=0.5)
            self.tick(0)
        self.assertEqual(self.job.state, "paused")
        self.assertIsNone(self.link.stopping)
        self.reply("ok", IDLE, dt=0.06)                 # (answered: still paused, still a job)
        self.tick(3)
        self.assertIsNone(self.link.stopping)

    class Extra:
        """A pendant or probe for release_extras: its port, and the disconnects it got. more: more of its state
        (the probe's connecting and mayBeDown)."""
        def __init__(self, port, **more):
            self.port, self.disconnects, self.more = port, 0, more

        def state(self):
            return {"port": self.port, "state": "connected" if self.port else "off", **self.more}

        def disconnect(self, force=False):
            self.disconnects += 1
            self.port = None
            return {}

    def test_closing_the_window_lets_go_of_everything_at_rest(self):
        """The user: "should it not disconnet when I close the app?" A connected pendant no longer keeps the
        FluidNC port with no page open: at rest the port goes as usual, then the pendant and the probe."""
        pendant, probe = self.Extra("/dev/rfcomm0"), self.Extra("/dev/ttyACM0")
        self.m.attach(pendant=pendant, probe=probe)
        self.ready()
        self.m.release_after = 2.0
        self.m.alone_since = self.t                     # no page open
        self.assertEqual(self.m.release_extras(self.t + 5), [])     # (FluidNC's port first)
        for _ in range(9):                              # Idle, nothing going on
            self.reply(IDLE, dt=0.2)
            self.tick(0)
        self.assertIsNone(self.link.stopping)           # (2 s from then)
        self.reply(IDLE, dt=0.25)
        self.tick(0)
        self.assertEqual(self.link.stopping, ("Released the port because no FluidSender window is open", "info"))
        self.m.link_closed(self.link)
        self.assertEqual(self.m.release_extras(self.t), ["pendant", "probe"])
        self.assertEqual((pendant.disconnects, probe.disconnects), (1, 1))
        self.assertIn("Released the pendant and the probe because no FluidSender window is open", self.log("info"))
        self.assertEqual(self.m.release_extras(self.t + 1), [])     # (nothing held any more)

    def test_the_pendant_stays_during_a_job_and_while_a_window_is_open(self):
        """With no page open, a job keeps FluidNC's port and so the pendant (its Stop and Pause work); so does a
        scan; a page open keeps all of them. Not connected to FluidNC: the pendant and probe go once no page has
        been open for release_after."""
        pendant, probe = self.Extra("/dev/rfcomm0"), self.Extra("/dev/ttyACM0")
        self.m.attach(pendant=pendant, probe=probe)
        self.ready()
        self.m.release_after = 2.0
        self.m.alone_since = self.t
        self.start_job(LONG)
        for _ in range(30):
            self.reply(IDLE, dt=0.2)
            self.tick(0)
            self.assertEqual(self.m.release_extras(self.t), [])
        self.assertIsNone(self.link.stopping)
        self.assertEqual((pendant.disconnects, probe.disconnects), (0, 0))
        self.link.stopping = ("Lost the connection to /dev/fake (EIO). Unplugged or switched off?", "error")
        self.m.link_closed(self.link)                   # (not connected now)
        self.m.activity = "scan"
        self.assertEqual(self.m.release_extras(self.t + 10), [])
        self.m.activity = None
        sub = object()
        self.m.subs.add(sub)                            # a page open
        self.assertEqual(self.m.release_extras(self.t + 10), [])
        self.m.subs.discard(sub)
        self.m.alone_since = self.t                     # (the page just closed)
        self.assertEqual(self.m.release_extras(self.t + 1.9), [])
        self.assertEqual(self.m.release_extras(self.t + 2.0), ["pendant", "probe"])
        self.assertEqual((pendant.disconnects, probe.disconnects), (1, 1))

    def test_a_probe_still_connecting_stays_and_one_that_may_be_down_is_said(self):
        """A probe connect waits for the board on the open port (its state() gives the port meanwhile): it is left
        to finish and let go of the next time (the review: every connect with no window open was cut short). A
        probe let go of while its tip may be down: the console says so after the release line (the page warns
        from its mayBeDown, which outlives the port); one that may not be down: nothing more."""
        pendant = self.Extra("/dev/rfcomm0")
        probe = self.Extra("/dev/ttyACM0", connecting=True, mayBeDown=True)
        self.m.attach(pendant=pendant, probe=probe)
        self.m.link_closed(self.link)                   # (FluidNC not connected)
        self.m.release_after = 2.0
        self.m.alone_since = self.t                     # no page open
        self.assertEqual(self.m.release_extras(self.t + 2.0), ["pendant"])
        self.assertEqual((pendant.disconnects, probe.disconnects), (1, 0))
        self.assertEqual(self.log("error"), [])
        probe.more["connecting"] = False                # (connected now)
        self.assertEqual(self.m.release_extras(self.t + 3.0), ["probe"])
        self.assertEqual(probe.disconnects, 1)
        self.assertEqual(self.log("info")[-1], "Released the probe because no FluidSender window is open")
        self.assertEqual(self.log("error"), [machine.PROBE_LET_GO_DOWN])
        self.assertEqual([e["text"] for e in self.evs("console")][-2:],
                         ["Released the probe because no FluidSender window is open", machine.PROBE_LET_GO_DOWN])
        probe.port, probe.more["mayBeDown"] = "/dev/ttyACM0", False     # (connected again and homed)
        self.assertEqual(self.m.release_extras(self.t + 4.0), ["probe"])
        self.assertEqual(self.log("error"), [machine.PROBE_LET_GO_DOWN])
        pendant.port = "/dev/rfcomm0"                   # (a pendant has no mayBeDown: never said for it)
        self.assertEqual(self.m.release_extras(self.t + 5.0), ["pendant"])
        self.assertEqual(self.log("error"), [machine.PROBE_LET_GO_DOWN])

    def test_an_alarm_that_may_have_left_the_spindle_on_keeps_everything_until_a_reset(self):
        """The verify of the release: a hard limit on a job's last move (every line answered), the spindle and the
        vacuum on, no window open. FluidNC leaves them on in an alarm, and after a hard or soft limit only a Soft
        Reset gets out of it: the pendant's Stop is that reset. FluidNC's port was let go of release_after after
        the alarm, then the pendant, whose Stop reached nothing. Now all three stay until a reset turns them off,
        and then go as they do at rest."""
        pendant, probe = self.Extra("/dev/rfcomm0"), self.Extra("/dev/ttyACM0")
        self.m.attach(pendant=pendant, probe=probe)
        self.ready()
        self.m.release_after = 2.0
        self.m.alone_since = self.t                     # no page open
        self.start_job("M3 S12000\nM8\nG1 X5 F10\n")
        self.reply("ok", "ok", "ok", "<Run|MPos:1.000,0.000,0.000|FS:254,12000|Ov:100,100,100|A:SF>")
        self.assertEqual(list(self.link.job.inflight), [])     # (every line answered: the move still runs)
        self.reply("[MSG:INFO: ALARM: Hard Limit]", "ALARM:1", "[MSG:ERR: Reset to continue]")
        self.assertEqual(self.job.state, "failed")
        self.assertEqual(self.link.alarm, {"code": 1, "critical": True, "text": "ALARM:1", "outputs": True})
        for _ in range(30):                             # 6 s in the alarm, the spindle and the vacuum still on
            self.reply("<Alarm|MPos:1.000,0.000,0.000|FS:0,12000|Ov:100,100,100|A:SF>", dt=0.2)
            self.tick(0)
            self.assertEqual(self.m.release_extras(self.t), [])
        self.assertIsNone(self.link.stopping)
        self.assertEqual((pendant.disconnects, probe.disconnects), (0, 0))
        busy = self.t                                   # (the last tick that found it not at rest)
        self.m.realtime("reset")                        # (what the pendant's Stop does in a critical alarm)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply("[MSG:INFO: ALARM: Unhomed]", "ALARM:14", BANNER)
        self.assertFalse(self.link.alarm["outputs"])
        while self.link.stopping is None:               # at rest now: the port goes release_after later
            self.assertLess(self.t - busy, 3.0, "not let go of after the reset")
            self.reply("<Alarm|MPos:1.000,0.000,0.000|FS:0,0|Ov:100,100,100>", dt=0.2)
            self.tick(0)
        self.assertGreaterEqual(self.t - busy, 2.0)
        self.assertEqual(self.link.stopping, ("Released the port because no FluidSender window is open", "info"))
        self.m.link_closed(self.link)
        self.assertEqual(self.m.release_extras(self.t), ["pendant", "probe"])
        self.assertEqual((pendant.disconnects, probe.disconnects), (1, 1))

    def test_an_alarm_whose_outputs_a_report_says_are_off_is_at_rest(self):
        """A job's alarm keeps the port while its spindle and vacuum/AUX may be on, until a report with Ov says they
        are off (AlarmOutputs): from then it is at rest, and the port goes release_after later. A report without
        Ov says nothing about them."""
        self.ready()
        self.m.release_after = 2.0
        self.m.alone_since = self.t                     # no page open
        self.start_job("G1 X5 F10\n")
        self.reply("ok", RUN)
        self.reply("ALARM:1")
        self.assertTrue(self.link.alarm["outputs"])     # (its program could have started them)
        for _ in range(15):
            self.reply("<Alarm|MPos:0,0,0|FS:0,0>", dt=0.2)
            self.tick(0)
        self.assertIsNone(self.link.stopping)
        busy = self.t                                   # (the last tick that found it not at rest)
        self.reply("<Alarm|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.2)    # all off
        self.assertFalse(self.link.alarm["outputs"])
        while self.link.stopping is None:
            self.assertLess(self.t - busy, 3.0, "not let go of once the outputs were off")
            self.reply("<Alarm|MPos:0,0,0|FS:0,0>", dt=0.2)
            self.tick(0)
        self.assertGreaterEqual(self.t - busy, 2.0)
        self.assertEqual(self.link.stopping, ("Released the port because no FluidSender window is open", "info"))

    def test_release_extras_says_how_long_a_pendant_disconnect_waits(self):
        """The verify of the release: release_extras's docstring said "WRITE_TIMEOUT + 2 s", which in machine.py
        reads as 3 + 2 s; the pendant's disconnect waits pendant.WRITE_TIMEOUT + 2 s for its reader (pendant.py)."""
        import pendant
        doc = " ".join(machine.Machine.release_extras.__doc__.split())
        self.assertIn(f"pendant.WRITE_TIMEOUT + 2 s: {pendant.WRITE_TIMEOUT + 2:g} s", doc)

    def test_refused_while_a_job_runs(self):
        self.ready()
        self.zero_record()
        self.start_job(LONG)
        for call in (lambda: self.m.send("G0 X1"), lambda: self.step(x=1), lambda: self.start(x=1),
                     lambda: self.m.zero("X"), lambda: self.m.zero(undo=True), lambda: self.m.goto("z"),
                     lambda: self.m.home(""), lambda: self.m.unlock(), self.m.run,
                     lambda: self.m.begin_activity("scan")):
            self.refused(call, JOB_RUNNING)
        for cmd in ("status", "jogcancel", "hold", "resume"):     # realtime is fine
            self.m.realtime(cmd)
        self.reply(RUN, dt=0.06)                        # (a report after the jog cancel: no jog runs)
        self.m.coolant("aux", True)
        self.load("G0 X1\n")
        self.m.set_jog_settings(step=1)
        self.assertEqual(self.m.jog({"stop": "all"}), {})
        self.m.realtime("reset")

    def zero_record(self):
        self.reply("<Idle|MPos:1.000,0.000,0.000|FS:0,0|WCO:0,0,0>", dt=0.06)
        self.m.zero("X")
        self.tick(0.01)
        self.sent()
        self.reply("ok", IDLE, dt=0.06)


class AlarmOutputs(Stage3):
    """An ALARM leaves the spindle and the coolant on in FluidNC 3.9.4: the alarm says when they may be ("outputs",
    for the page's warning and its Soft Reset), until a report with Ov says otherwise or a reset turns them off."""

    def test_an_alarm_in_a_job(self):
        self.ready()
        self.start_job("M3 S12000\nM8\nG1 X5 F10\n")
        self.reply("ok", "ok", "<Run|MPos:1.000,0.000,0.000|FS:254,12000|Ov:100,100,100|A:SF>")
        self.reply("[MSG:INFO: ALARM: Hard Limit]", "ALARM:1")
        self.assertEqual(self.link.alarm, {"code": 1, "critical": False, "text": "ALARM:1", "outputs": True})
        self.assertEqual(self.log("error")[-1], "ALARM: the spindle and vacuum/AUX may still be on. Soft Reset turns "
                                                "them off.")
        self.reply("[MSG:ERR: Reset to continue]", "<Alarm|MPos:1.000,0.000,0.000|FS:0,12000>")
        self.assertEqual(self.link.alarm, {"code": 1, "critical": True, "text": "ALARM:1", "outputs": True})
        self.reply("<Alarm|MPos:1.000,0.000,0.000|FS:0,12000|Ov:100,100,100|A:SF>", dt=0.2)
        self.assertTrue(self.link.alarm["outputs"])     # still on
        self.assertEqual(self.evs("alarm")[-1]["alarm"], self.link.alarm)
        self.m.realtime("reset")                        # our Soft Reset: FluidNC's ALARM:14, then the banner
        self.reply("[MSG:INFO: ALARM: Unhomed]", "ALARM:14")
        self.assertEqual(self.link.alarm, {"code": 14, "critical": False, "text": "ALARM:14", "outputs": False})
        self.reply(BANNER, "<Alarm|MPos:1.000,0.000,0.000|FS:0,0|Ov:100,100,100>")
        self.assertFalse(self.link.alarm["outputs"])
        self.assertEqual(self.log("error").count(machine.OUTPUTS_ON), 1)

    def test_a_job_without_spindle_or_coolant(self):
        self.ready()
        self.start_job("G1 X5 F10\n")
        self.reply("ok", RUN)
        self.reply("ALARM:1")
        self.assertTrue(self.link.alarm["outputs"])     # (its program could have started them) ...
        self.reply("<Alarm|MPos:0,0,0|FS:0,0|Ov:100,100,100>", dt=0.1)
        self.assertFalse(self.link.alarm["outputs"])    # ... until FluidNC says all is off

    def test_without_a_job(self):
        self.ready()
        self.reply("ALARM:2")                           # nothing on: no warning
        self.assertFalse(self.link.alarm["outputs"])
        self.assertEqual(self.log("error"), [])
        self.reply(IDLE, dt=0.8)
        self.assertIsNone(self.link.alarm)
        self.reply("<Idle|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:F>")    # the vacuum is on
        self.reply("ALARM:1")
        self.assertTrue(self.link.alarm["outputs"])
        self.assertEqual(self.log("error"), [machine.OUTPUTS_ON])
        self.reply(BANNER)                              # FluidNC's own reset (its button): all off
        self.assertFalse(self.link.alarm["outputs"])
        self.reply("ALARM:3")                           # what a reset prints never warns
        self.assertFalse(self.link.alarm["outputs"])
        self.reply("<Alarm|MPos:0,0,0|FS:0,0|Ov:100,100,100|A:F>", dt=0.1)
        self.assertFalse(self.link.alarm["outputs"])


class OvEvents:
    """FluidNC 4.1.1's (and 3.9.4's) override bytes as it handles them: each one queues an event, which its protocol
    task applies in order once it is free (RealtimeCmd.cpp, Protocol.cpp protocol_handle_events). The task is busy
    until busy_until (a VFD syncing to the program's M3, say), and for `sync` s after every spindle change (a VFD
    with speed feedback while the spindle stands: protocol_do_spindle_override -> VFDSpindle::setState waits for
    the drive's speed). Reports come from the polling task, so they show the values applied so far."""

    STEP = {0x91: 10, 0x92: -10, 0x93: 1, 0x94: -1, 0x9A: 10, 0x9B: -10, 0x9C: 1, 0x9D: -1}
    RAPID = {0x95: 100, 0x96: 50, 0x97: 25, 0x98: 5}

    def __init__(self, sync=0.0, busy_until=0.0):
        self.ov = [100, 100, 100]
        self.pending = []
        self.sync = sync
        self.busy_until = busy_until

    def receive(self, data, now):
        self.pending += [b for b in data if 0x90 <= b <= 0x9D]
        self.run(now)

    def run(self, now):
        while self.pending and now >= self.busy_until:
            b = self.pending.pop(0)
            if b <= 0x94:
                self.ov[0] = 100 if b == 0x90 else max(10, min(200, self.ov[0] + self.STEP[b]))
            elif b <= 0x98:
                self.ov[1] = self.RAPID[b]
            else:
                old = self.ov[2]
                self.ov[2] = 100 if b == 0x99 else max(10, min(200, self.ov[2] + self.STEP[b]))
                if self.ov[2] != old and self.sync:
                    self.busy_until = now + self.sync

    def report(self, now, state="Hold:0"):
        self.run(now)
        return "<{}|MPos:0,0,0|FS:0,12000|Ov:{},{},{}|A:S>".format(state, *self.ov)


class Overrides(Stage3):
    """Feed, rapid and spindle overrides: FluidNC's realtime bytes only (so also during a job, a hold or an alarm),
    the value they give shown at once, reports made before FluidNC took them don't count, and a report after them
    says it for sure (the console too). The spindle goes at most 2 steps a report (a VFD syncs each one)."""

    def known(self, ov="100,100,100", dt=0.06):
        self.reply(f"<Idle|MPos:0,0,0|FS:0,0|Ov:{ov}>", dt=dt)

    def ov(self, **call):
        return self.m.override(call)

    def status(self):
        return self.m.snapshot()["status"]

    def test_feed_to_a_value_and_steps(self):
        self.ready()
        self.known()
        self.assertEqual(self.ov(which="feed", to=110), {"sent": 1, "expect": [110, 100, 100]})
        self.assertEqual(self.realtime(), b"\x91")
        st = self.evs("status")[-1]
        self.assertEqual((st["ov"], st["ovTarget"]), ([110, 100, 100], [110, None, None]))    # shown at once
        self.assertEqual(self.link.next_poll, self.t + machine.SETTLE)  # (a report asked for once FluidNC has it)
        self.known(dt=0.02)                             # (made before FluidNC took it: doesn't count)
        self.assertEqual(self.status()["ov"], [110, 100, 100])
        self.assertEqual(self.log("info"), [])
        self.known("110,100,100")                       # FluidNC's word (4.1.1: in the next report)
        self.assertEqual((self.status()["ov"], self.status()["ovTarget"]), ([110, 100, 100], None))
        self.assertEqual(self.log("info"), ["Feed override 110%"])
        for call, byte, want in ((dict(step=1), b"\x93", 111), (dict(step=-10), b"\x92", 101),
                                 (dict(to=100), b"\x94", 100), (dict(step=10), b"\x91", 110)):
            self.assertEqual(self.ov(which="feed", **call)["expect"][0], want, call)
            self.assertEqual(self.realtime(), byte, call)
            self.known(f"{want},100,100")
        self.assertEqual(self.ov(which="feed", to=200), {"sent": 9, "expect": [200, 100, 100]})
        self.assertEqual(self.realtime(), b"\x91" * 9)                 # (110 -> 200: 9 coarse steps)
        self.known("200,100,100")
        self.assertEqual(self.ov(which="feed", to=200), {"sent": 0, "expect": [200, 100, 100]})   # (there already)
        self.assertEqual((self.realtime(), self.status()["ovTarget"]), (b"", None))
        self.assertEqual(self.log("info")[-1], "Feed override 200%")

    def test_the_fewest_bytes(self):
        steps = machine.OV_STEPS["feed"]
        for cur, target, data in ((100, 200, b"\x91" * 10), (10, 200, b"\x90" + b"\x91" * 10),
                                  (105, 10, b"\x90" + b"\x92" * 9), (200, 100, b"\x90"), (100, 109, b"\x91\x94"), (15, 10, b"\x94" * 5), (100, 100, b"")):
            self.assertEqual(machine.ov_bytes(cur, target, steps), data, (cur, target))
            self.assertEqual(machine.ov_after(cur, data, steps), target)

    def test_at_the_limits_and_not_known(self):
        self.ready()
        self.refused(lambda: self.ov(which="feed", step=1), "Overrides not known yet: FluidNC hasn't reported them "
                     "since connecting; try again in a second")
        self.assertEqual(self.ov(which="rapid", to=50), {"sent": 1, "expect": [None, 50, None]})     # (absolute)
        self.assertEqual(self.realtime(), b"\x96")
        self.known("200,50,10")
        self.assertEqual(self.log("info"), ["Rapid override 50%"])
        self.refused(lambda: self.ov(which="feed", step=10), "Feed override already at 200%")
        self.refused(lambda: self.ov(which="spindle", step=-1), "Spindle override already at 10%")
        self.assertEqual(self.ov(which="feed", step=-1)["expect"], [199, 50, 10])
        self.assertEqual(self.ov(which="spindle", step=10)["expect"], [199, 50, 20])

    def test_rapid_and_reset_all(self):
        self.ready()
        self.known()
        for to, byte in ((25, b"\x97"), (50, b"\x96"), (100, b"\x95")):
            self.assertEqual(self.ov(which="rapid", to=to)["expect"], [100, to, 100])
            self.assertEqual(self.realtime(), byte)
            self.known(f"100,{to},100")
        self.known("120,25,80")
        self.assertEqual(self.ov(which="all", reset=True), {"sent": 3, "expect": [100, 100, 100]})
        self.assertEqual(self.realtime(), b"\x90\x95\x99")
        self.known()
        self.assertEqual(self.log("info")[-1], "Overrides: feed 100%, rapid 100%, spindle 100%")

    def test_the_spindle_goes_two_steps_a_report(self):
        """A VFD with speed feedback waits for its speed at every step (and alarms after 3 s): at most 2 at a time,
        the next ones once a report shows those took."""
        self.ready()
        self.known()
        self.assertEqual(self.ov(which="spindle", to=150), {"sent": 2, "expect": [100, 100, 120]})
        self.assertEqual(self.realtime(), b"\x9a\x9a")
        self.assertEqual(self.status()["ovTarget"], [None, None, 150])
        self.known("100,100,100", dt=0.02)              # (from before them)
        self.assertEqual(self.realtime(), b"")
        self.known("100,100,120")
        self.assertEqual(self.realtime(), b"\x9a\x9a")
        self.assertEqual(self.status()["ov"], [100, 100, 140])
        self.known("100,100,140")
        self.assertEqual(self.realtime(), b"\x9a")
        self.known("100,100,150")
        self.assertEqual(self.realtime(), b"")
        self.assertEqual((self.status()["ovTarget"], self.log("info")), (None, ["Spindle override 150%"]))

    def drive(self, fluidnc, clicks, until, state="Hold:0"):
        """The override calls in clicks ([(seconds from now, call)]) while FluidNC (an OvEvents) gets what is written
        and reports every 50 ms, for `until` seconds. Returns all the override bytes written."""
        t0, clicks, written = self.t, list(clicks), b""
        while self.t - t0 < until:
            while clicks and self.t - t0 >= clicks[0][0]:
                self.ov(**clicks.pop(0)[1])
            data = self.realtime()
            written += data
            fluidnc.receive(data, self.t)
            self.reply(fluidnc.report(self.t, state), dt=0.05)
        data = self.realtime()
        fluidnc.receive(data, self.t)
        return written + data

    def test_bytes_waiting_in_a_busy_fluidnc_are_not_sent_again(self):
        """FluidNC applies override bytes as events once its protocol task is free (here busy 2 s: a VFD with speed
        feedback getting to the program's M3 speed, say). Steps sent again would come on top of the ones waiting
        (the reviewer's A: one Feed +10 ended at 140 %), and any byte sent again fills FluidNC's event queue, where a
        Feed Hold or a Soft Reset must still fit: nothing goes again, and it ends at 110 %."""
        self.ready()
        self.known()
        fluidnc = OvEvents(busy_until=self.t + 2.0)
        written = self.drive(fluidnc, [(0, dict(which="feed", step=10))], 4.0, state="Run")
        self.assertEqual((fluidnc.ov, written), ([110, 100, 100], b"\x91"))
        self.assertEqual((self.status()["ov"], self.status()["ovTarget"]), ([110, 100, 100], None))
        self.assertEqual((self.log("info"), self.log("error")), (["Feed override 110%"], []))

    def test_spindle_steps_wait_for_a_vfd_and_never_go_twice(self):
        """A VFD with speed feedback: FluidNC sets the spindle at every spindle step while it stands (Hold:0) and
        waits for its speed (here 1 s), handling no other event meanwhile. Three quick Spindle -10 clicks end at
        70 % (the reviewer's A: 50 %), and a +10 at 195 % (5 steps, 2 a report) at 200 %, with no error."""
        self.ready()
        self.known()
        fluidnc = OvEvents(sync=1.0)
        minus = dict(which="spindle", step=-10)
        written = self.drive(fluidnc, [(0, minus), (0.3, minus), (0.6, minus)], 4.0)
        self.assertEqual((fluidnc.ov[2], written), (70, b"\x9b" * 3))
        self.assertEqual((self.status()["ov"][2], self.status()["ovTarget"]), (70, None))
        self.assertEqual((self.log("info")[-1], self.log("error")), ("Spindle override 70%", []))
        self.known("100,100,195", dt=1.1)
        fluidnc.ov[2] = 195
        written = self.drive(fluidnc, [(0, dict(which="spindle", step=10))], 8.0)
        self.assertEqual((fluidnc.ov[2], written), (200, b"\x9c" * 5))
        self.assertEqual((self.log("info")[-1], self.log("error")), ("Spindle override 200%", []))

    def test_rapid_waits_for_a_busy_fluidnc_too(self):
        """Rapid is absolute, but goes once too: also when asked before FluidNC said its overrides (no value
        expected then: its target is)."""
        self.ready()                                    # (no report with Ov yet)
        fluidnc = OvEvents(busy_until=self.t + 1.2)
        written = self.drive(fluidnc, [(0, dict(which="rapid", to=25))], 2.0, state="Run")
        self.assertEqual((fluidnc.ov, written), ([100, 25, 100], b"\x97"))
        self.assertEqual((self.log("info"), self.log("error")), (["Rapid override 25%"], []))
        fluidnc.busy_until = self.t + 1.2
        written = self.drive(fluidnc, [(0, dict(which="rapid", to=50))], 2.0, state="Run")
        self.assertEqual((fluidnc.ov, written, self.status()["ov"]), ([100, 50, 100], b"\x96", [100, 50, 100]))
        self.assertEqual((self.log("info")[-1], self.log("error")), ("Rapid override 50%", []))

    def test_bytes_fluidnc_never_takes_it_gives_up_on_and_says_so(self):
        """FluidNC's value doesn't move for OV_GIVE_UP since the last bytes (they were lost, or it is still busy):
        FluidSender stops waiting and says so, and FluidNC's word counts. If it gets there later, that is said too.
        No byte went twice."""
        self.ready()
        self.known()
        fluidnc = OvEvents(busy_until=self.t + 30)
        written = self.drive(fluidnc, [(0, dict(which="feed", to=120)), (0, dict(which="spindle", step=-1))],
                             machine.OV_GIVE_UP - 0.5)
        self.assertEqual((self.log("error"), self.status()["ovTarget"]), ([], [120, None, 99]))  # (still waiting)
        self.assertEqual(self.status()["ov"], [120, 100, 99])          # (as expected meanwhile)
        written += self.drive(fluidnc, [], 1.0)
        self.assertEqual(written, b"\x91\x91\x9d")
        self.assertEqual(self.log("error"), [
            "FluidNC has not taken the feed override: it still says 100%, 120% was asked (is it busy? A VFD spindle "
            "getting to speed holds it up)",
            "FluidNC has not taken the spindle override: it still says 100%, 99% was asked (is it busy? A VFD "
            "spindle getting to speed holds it up)"])
        self.assertEqual((self.status()["ov"], self.status()["ovTarget"]), ([100, 100, 100], None))
        fluidnc.busy_until = self.t                     # (it was busy after all)
        self.assertEqual(self.drive(fluidnc, [], 0.2), b"")
        self.assertEqual(fluidnc.ov, [120, 100, 99])
        self.assertEqual(self.log("info"), ["Feed override 120% (FluidNC took it late)",
                                            "Spindle override 99% (FluidNC took it late)"])
        self.assertEqual(self.status()["ov"], [120, 100, 99])

    def test_bad_calls(self):
        self.ready()
        self.known()
        for call in (dict(which="speed", to=100), dict(which="feed", to=5), dict(which="feed", to=201),
                     dict(which="feed", to="110"), dict(which="feed", to=110.5), dict(which="feed", step=2),
                     dict(which="feed", step=True), dict(which="feed", step=1.0), dict(which="feed", to=110, step=1),
                     dict(which="feed"), dict(which="feed", reset=True), dict(which="rapid", to=5),
                     dict(which="rapid", to=75), dict(which="all"), dict(which="all", reset=True, to=100)):
            with self.assertRaises(MachineError, msg=call) as cm:
                self.ov(**call)
            self.assertEqual(cm.exception.status, 400, call)
        self.assertEqual(self.ov(which="feed", to=110.0)["expect"][0], 110)     # (a whole number is fine)
        self.assertEqual(self.realtime(), b"\x91")

    def test_any_state_but_not_without_fluidnc(self):
        self.refused(lambda: self.ov(which="all", reset=True), "Not connected")
        self.ready()
        self.known()
        self.start_job(LONG)
        self.reply(RUN, dt=0.1)
        self.ov(which="feed", step=10)                  # during a job: fine
        self.assertEqual(self.realtime(), b"\x91")
        self.m.realtime("hold")
        self.reply(HOLD0, dt=0.3)
        self.ov(which="spindle", to=90)                 # in a hold too
        self.assertEqual(self.realtime(), b"!\x9b")
        self.reply("ALARM:1", "<Alarm|MPos:0,0,0|FS:0,0|Ov:110,100,90>")
        self.ov(which="feed", to=100)                   # and in an alarm (harmless)
        self.assertEqual(self.realtime(), b"\x92")     # (110 -> 100: one coarse step)
        self.t += 2.1
        self.refused(lambda: self.ov(which="feed", to=100), "No response from FluidNC")
        self.reply(IDLE)
        self.m.realtime("reset")
        self.refused(lambda: self.ov(which="feed", to=100), "FluidNC is being reset; try again in a second")

    def test_program_end_and_reset_put_them_back(self):
        self.ready()
        self.known("150,50,120")
        self.ov(which="spindle", to=200)
        self.realtime()
        self.typed("M30")                               # FluidNC: overrides back to 100 % before its ok
        self.assertEqual((self.status()["ov"], self.status()["ovTarget"]), ([100, 100, 100], None))
        self.known("150,50,140", dt=0.02)               # (from before)
        self.assertEqual(self.status()["ov"], [100, 100, 100])
        self.assertEqual(self.realtime(), b"")
        self.known("130,100,100")
        self.ov(which="feed", to=100)
        self.realtime()
        self.reply(BANNER)
        self.assertEqual((self.status()["ov"], self.status()["ovTarget"]), ([100, 100, 100], None))   # (a reset)

    def typed(self, line, reply="ok"):
        self.m.send(line)
        self.tick(0.01)
        self.assertEqual(self.sent(), [line])
        self.reply(reply)


class PendantKeys(Stage3):
    """The pendant's jog keys on the Machine: 1 in at most, and never more than 2 steps waiting or moving."""

    def test_a_jog_key_steps_1_in_at_most_and_two_ahead(self):
        import pendant
        self.ready()
        p = pendant.PendantLink(self.m, None)
        self.m.set_jog_settings(step=10)
        self.assertEqual(p._action(5), (True, "Pendant: Jog X+ 1 in"))
        self.assertEqual(self.log("info")[-1], "Pendant: step 10 in is too big for the pendant; using 1 in")
        self.assertEqual(self.link.queue[-1].text, "$J=G20G91X1F150")
        self.assertEqual(p._action(7), (True, "Pendant: Jog X- 1 in"))
        self.assertEqual(p._action(5), (False, "Pendant: Jog X+ refused: 2 jog steps are still waiting or moving; "
                                               "wait for them"))
        self.assertEqual(len(self.link.queue), 2)       # (nothing queued for later)
        self.assertEqual(self.step(x=0.1), "$J=G20G91X0.1F150")     # the page's own steps: up to 5
        self.m.set_jog_settings(step=0.01)
        self.tick(0.01)
        for _ in range(3):
            self.sent()
            self.reply("ok")
        self.assertEqual(p._action(6)[0], False)        # answered, but still moving (no Idle report since)
        self.reply(base.JOG, dt=0.1)
        self.reply(IDLE, dt=0.1)                        # done
        self.assertEqual(p._action(6), (True, "Pendant: Jog Y+ 0.01 in"))
        self.assertEqual(self.log("info").count("Pendant: step 10 in is too big for the pendant; using 1 in"), 2)


# ------------------------------------------------------------------ shared jog settings, settings.py

class JogSettings(Stage3):
    def test_set_and_shown(self):
        self.assertEqual(self.m.jog_settings(), {"step": 0.1, "feed": 150})
        self.assertEqual(self.m.snapshot()["jogSettings"], {"step": 0.1, "feed": 150})
        self.assertEqual(self.m.set_jog_settings(step=0.01), {"step": 0.01, "feed": 150})
        self.assertEqual(self.evs("jogsettings"), [{"type": "jogsettings", "step": 0.01, "feed": 150}])
        self.assertEqual(self.m.set_jog_settings({"feed": 200.04}), {"step": 0.01, "feed": 200})
        self.assertEqual(self.m.set_jog_settings(10, 33.35)["feed"], 33.4)
        self.m.set_jog_settings(step=10)                # unchanged: no event
        self.assertEqual(len(self.evs("jogsettings")), 3)
        self.assertEqual(self.m.jog_settings(), {"step": 10, "feed": 33.4})
        for step, feed, why in ((0.5, None, "Jog step: one of 10, 1, 0.1, 0.01, 0.001 in"),
                                ("1", None, "Jog step: one of 10, 1, 0.1, 0.01, 0.001 in"),
                                (None, 0, "Jog speed: a number above 0 and up to 10000 in/min"),
                                (None, float("nan"), "Jog speed: a number above 0 and up to 10000 in/min"),
                                (None, 10001, "Jog speed: a number above 0 and up to 10000 in/min"),
                                (None, None, "Jog settings: give step and/or feed")):
            self.refused(lambda: self.m.set_jog_settings(step, feed), why, 400)
        self.assertEqual(self.m.jog_settings(), {"step": 10, "feed": 33.4})

    def test_kept_in_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with mock.patch.dict(os.environ, {"FLUIDSENDER_SETTINGS": path}):
                m = machine.Machine(settings=settings.Settings())
                m.emit = lambda ev: None
                m.set_jog_settings(step=0.001, feed=75)
                with open(path) as f:
                    self.assertEqual(json.load(f)["jog"], {"step": 0.001, "feed": 75})
                self.assertEqual(machine.Machine(settings=settings.Settings()).jog_settings(),
                                 {"step": 0.001, "feed": 75})
            self.assertEqual(os.listdir(d), ["settings.json"])


class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "settings.json")
        self.env = mock.patch.dict(os.environ, {"FLUIDSENDER_SETTINGS": self.path})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def write(self, data):
        with open(self.path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_defaults_and_written_only_when_changed(self):
        s = settings.Settings()
        self.assertEqual(s.path, self.path)
        self.assertEqual((s.get("jog"), s.get("scan"), s.get("probePort"), s.get("pendantPort")),
                         ({"step": 0.1, "feed": 150}, settings.SCAN, None, None))
        self.assertFalse(os.path.exists(self.path))     # nothing changed: nothing written
        s.set("jog", {"step": 0.1, "feed": 150})
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(s.set("pendantPort", " /dev/rfcomm1 "), "/dev/rfcomm1")
        with open(self.path) as f:
            self.assertEqual(json.load(f)["pendantPort"], "/dev/rfcomm1")
        s["probePort"] = "/dev/ttyACM0"
        self.assertEqual(settings.Settings()["probePort"], "/dev/ttyACM0")
        self.assertEqual(os.listdir(self.dir.name), ["settings.json"])     # (atomic: no temporary file left)
        got = s.get("jog")
        got["step"] = 10                                # (a copy)
        self.assertEqual(s.get("jog")["step"], 0.1)
        self.assertIn("scan", s)
        with self.assertRaises(KeyError):
            s["nothing"]
        with self.assertRaises(settings.SettingsError):
            s.set("nothing", 1)

    def test_bad_file_and_bad_values_fall_back(self):
        self.write("{not json")
        self.assertEqual(settings.Settings().get("jog"), {"step": 0.1, "feed": 150})
        self.write([1, 2])
        self.assertEqual(settings.Settings().get("jog"), {"step": 0.1, "feed": 150})
        scan = dict(settings.SCAN, xPoints=9)
        self.write({"jog": {"step": 0.5, "feed": 150}, "scan": scan, "probePort": 5, "pendantPort": "/dev/rfcomm4",
                    "other": True})
        s = settings.Settings()
        self.assertEqual((s.get("jog"), s.get("scan"), s.get("probePort"), s.get("pendantPort")),
                         ({"step": 0.1, "feed": 150}, scan, None, "/dev/rfcomm4"))
        self.assertIsNone(s.get("other"))

    def test_scan_settings_checked(self):
        s = settings.Settings()
        self.assertEqual(s.set("scan", dict(settings.SCAN)), settings.SCAN)     # (a first run's all-0 area: kept)
        ok = dict(settings.SCAN, minX=0.5, minY=1.0, maxX=8.5, maxY=12.0, probeOffY=3.0)
        self.assertEqual(s.set("scan", ok), ok)
        self.assertEqual(s.update("scan", {"xPoints": 9.0})["xPoints"], 9)
        self.assertEqual(s.get("scan"), dict(ok, xPoints=9))
        for change, why in (({"minX": 9}, "Scan: min X must be below max X (the same only with 1 point in X)"),
                            ({"minY": 12.0}, "Scan: min Y must be below max Y (the same only with 1 point in Y)"),
                            ({"xPoints": 101}, "Scan xPoints: a whole number from 1 to 100"),
                            ({"yPoints": 2.5}, "Scan yPoints: a whole number from 1 to 100"),
                            ({"xPoints": 50, "yPoints": 41}, "Scan: 2050 points; at most 2000"),
                            ({"zRetract": 0.001}, "Scan zRetract: a number from 0.01 to 2 in"),
                            ({"maxX": "8"}, "Scan maxX: a number from -100 to 100 in"),
                            ({"maxX": 101}, "Scan maxX: a number from -100 to 100 in")):
            with self.assertRaises(settings.SettingsError, msg=repr(change)) as cm:
                s.update("scan", change)
            self.assertEqual(str(cm.exception), why)
        self.assertEqual(s.update("scan", {"minX": 2, "maxX": 2, "xPoints": 1})["minX"], 2)   # one column: fine

    def test_in_memory_only(self):
        s = settings.Settings(None)
        s.set("jog", {"step": 1, "feed": 99})
        self.assertEqual(s.get("jog"), {"step": 1, "feed": 99})
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(os.listdir(self.dir.name), [])


# ------------------------------------------------------------------ the pendant's numbers

class PendantStatus(Stage3):
    def test_numbers(self):
        self.m.link = None
        self.assertEqual(self.m.pendant_status(), {"x": 0.0, "y": 0.0, "z": 0.0, "status": 3, "feed": 150, "line": 0,
                                                   "total": 0, "step": 0.1, "live": 0.0})
        self.m.link = self.link
        self.ready()
        self.reply("<Idle|MPos:25.400,-12.700,2.540|FS:0,0|WCO:0,0,0>")
        st = self.m.pendant_status()
        self.assertEqual((st["x"], st["y"], st["z"], st["status"]), (1.0, -0.5, 0.1, 0))
        for report, status in ((RUN, 3), (base.JOG, 3), (HOLD1, 1), (HOLD0, 1), ("<Alarm|MPos:0,0,0|FS:0,0>", 3),
                               ("<Home|MPos:0,0,0|FS:0,0>", 3)):
            self.reply(report)
            self.assertEqual(self.m.pendant_status()["status"], status, report)
        self.assertEqual(self.m.pendant_status()["live"], 0.0)
        self.reply(RUN)
        self.assertEqual(self.m.pendant_status()["live"], 100.0)    # FS 2540 mm/min
        self.t += 2.1
        self.assertEqual(self.m.pendant_status()["status"], 3)      # no response
        self.reply(IDLE, dt=0.06)
        self.m.set_jog_settings(step=0.01, feed=80)
        self.load("G0 X1\nG0 X2\nG0 X3\n")
        self.assertEqual([self.m.pendant_status()[k] for k in ("feed", "step", "line", "total")], [80, 0.01, 0, 3])
        self.m.run()
        self.tick(0.01)
        self.reply("ok", "ok")                          # (FluidSender's G20 G90, then the program's first line)
        self.assertEqual([self.m.pendant_status()[k] for k in ("status", "line", "total")], [2, 1, 3])
        self.m.realtime("hold")
        self.assertEqual(self.m.pendant_status()["status"], 1)       # paused
        self.reply(HOLD0, dt=0.06)
        self.m.realtime("resume")
        self.reply("ok", "ok", IDLE, dt=0.06)
        self.assertEqual(self.job.state, "done")
        self.assertEqual([self.m.pendant_status()[k] for k in ("status", "line", "total")], [0, 3, 3])
        self.load("G0 X1\n")                            # another program: its numbers
        self.assertEqual([self.m.pendant_status()[k] for k in ("line", "total")], [0, 1])
        self.m.begin_activity("scan")
        self.assertEqual(self.m.pendant_status()["status"], 3)


# ------------------------------------------------------------------ the scanner's calls

class FakeScanner:
    def __init__(self):
        self.calls = []

    def stop(self, stop_machine=True):
        self.calls.append(stop_machine)
        return True

    def state(self):
        return {"running": True}


class ScanCalls(Stage3):
    """scan_move blocks (the scanner's thread): it runs in a thread here, and the test feeds FluidNC's
    replies with the Machine's lock held, as the reader thread does."""

    def reply(self, *lines, dt=0.01):
        with self.m.lock:
            super().reply(*lines, dt=dt)

    def tick(self, dt=0.1):
        with self.m.lock:
            super().tick(dt)

    def sent(self):
        with self.m.lock:
            return super().sent()

    def move(self, x, y, timeout=60.0):
        out = {}

        def go():
            try:
                out["ok"] = self.m.scan_move(x, y, timeout)
            except MachineError as e:
                out["error"] = str(e)
        th = threading.Thread(target=go, daemon=True)
        th.start()
        end = time.monotonic() + 2
        while not (self.link.queue or self.link.inflight or out) and time.monotonic() < end:
            time.sleep(0.005)
        return th, out

    def done(self, th, out):
        th.join(2)
        self.assertFalse(th.is_alive())
        return out

    def scanning(self):
        self.ready()
        self.m.begin_activity("scan")

    def test_move_and_wait_for_idle_there(self):
        self.scanning()
        th, out = self.move(1, 2)
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G20 G90 G0 X1 Y2"])
        self.assertEqual(self.log("tx")[-1], "G20 G90 G0 X1 Y2")    # (shown, like the user's lines)
        self.reply("ok")
        self.reply("<Idle|MPos:25.400,50.800,0.000|FS:0,0>", dt=0.02)   # (made before the move started)
        time.sleep(0.1)
        self.assertEqual(out, {})
        self.reply("<Run|MPos:20.000,40.000,0.000|FS:5000,0>", dt=0.1)
        time.sleep(0.1)
        self.assertEqual(out, {})
        self.reply("<Idle|MPos:25.430,50.800,0.000|FS:0,0>", dt=0.1)    # 0.0012 in off: within 0.002 in, there
        self.assertEqual(self.done(th, out), {"ok": {"x": 1.001181, "y": 2.0, "z": 0.0}})

    def test_not_there_yet(self):
        self.scanning()
        th, out = self.move(1, 2)
        self.tick(0.01)
        self.reply("ok")
        self.reply("<Idle|MPos:25.460,50.800,0.000|FS:0,0>", dt=0.1)    # 0.0024 in off
        time.sleep(0.1)
        self.assertEqual(out, {})
        self.reply("<Idle|MPos:25.400,50.800,0.000|FS:0,0>", dt=0.1)
        self.assertEqual(self.done(th, out), {"ok": {"x": 1.0, "y": 2.0, "z": 0.0}})

    def test_time_out(self):
        self.scanning()
        th, out = self.move(1, 2, 0.5)
        self.tick(0.01)
        self.reply("ok")
        self.reply(RUN, dt=0.6)
        self.assertEqual(self.done(th, out), {"error": "The machine did not get to X1 Y2 within 0.5 s"})

    def test_alarm_error_reset_stop_disconnect(self):
        cases = (("ALARM:1", "ALARM:1 (hard limit: position likely lost, home again) during the scan"),
                 ("error:9", "FluidNC answered error:9 (locked out during alarm or jog) to G20 G90 G0 X1 Y2"),
                 (BANNER, "FluidNC was reset during the scan"))
        for line, why in cases:
            with self.subTest(line=line):
                self.setUp()
                self.scanning()
                th, out = self.move(1, 2)
                self.tick(0.01)
                self.reply(line)
                self.assertEqual(self.done(th, out), {"error": why})
                self.tearDown()
        self.setUp()
        self.scanning()
        th, out = self.move(1, 2)
        self.assertEqual(self.m.stop(), {"stopped": "scan"})
        self.assertEqual(self.done(th, out), {"error": "Stopped"})
        self.assertEqual(list(self.link.queue), [])     # (the move not sent is dropped)
        self.refused(lambda: self.m.scan_move(1, 2), "Stopped")
        self.m.end_activity("scan")
        self.m.begin_activity("scan")
        th, out = self.move(1, 2)
        self.link.stopping = ("Disconnected from /dev/fake", "info")
        self.m.link_closed(self.link)
        self.assertEqual(self.done(th, out), {"error": "Disconnected during the scan"})

    def test_stop_stops_the_scanner_and_a_move(self):
        scanner = FakeScanner()
        self.m.attach(scanner=scanner)
        self.scanning()
        self.assertEqual(self.m.stop(), {"stopped": "scan"})
        self.assertEqual(scanner.calls, [False])        # (the machine's part is Machine.stop's)
        self.assertEqual(self.realtime(), b"")          # the CNC stood still
        self.m.end_activity("scan")
        self.m.begin_activity("scan")
        th, out = self.move(1, 2)
        self.tick(0.01)
        self.reply("ok", RUN)
        self.assertEqual(self.m.stop(), {"stopped": "scan"})
        self.assertEqual(self.realtime(), b"!")         # and a move it made: the safe stop
        self.assertEqual(self.done(th, out), {"error": "Stopped"})

    def test_stop_right_after_a_moves_ok(self):
        """The move's ok has come, no report shows it running yet: it is moving all the same."""
        self.m.attach(scanner=FakeScanner())
        self.scanning()
        th, out = self.move(1, 2)
        self.tick(0.01)
        self.reply("ok")
        self.assertEqual(self.m.stop(), {"stopped": "scan"})
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual(self.done(th, out), {"error": "Stopped"})

    def test_stop_at_a_point_reached(self):
        """There (Idle at the point, after the move's ok): the probe may touch, and Stop has nothing to hold."""
        self.m.attach(scanner=FakeScanner())
        self.scanning()
        th, out = self.move(0, 0)
        self.tick(0.01)
        self.reply("ok")
        self.reply(IDLE, dt=0.06)                       # (0.07 s after the ok)
        self.assertEqual(self.done(th, out), {"ok": {"x": 0.0, "y": 0.0, "z": 0.0}})
        self.assertEqual(self.m.stop(), {"stopped": "scan"})
        self.assertEqual(self.realtime(), b"")

    def test_activity(self):
        self.refused(lambda: self.m.begin_activity("scan"), "Not connected")
        self.ready()
        self.refused(lambda: self.m.scan_move(1, 2), "No scan is running")
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>")
        self.refused(lambda: self.m.begin_activity("scan"), "ALARM: Unlock or Home first")
        self.reply(IDLE, dt=0.06)
        self.m.send("G4 P1")
        self.refused(lambda: self.m.begin_activity("scan"), "Waiting for FluidNC to finish 'G4 P1'")
        self.tick()
        self.sent()
        self.reply("ok", IDLE, dt=0.06)
        self.assertEqual(self.m.begin_activity("scan"), {})
        self.refused(lambda: self.m.begin_activity("scan"), SCANNING)
        for call in (lambda: self.m.send("G0 X1"), lambda: self.step(x=1), lambda: self.start(x=1), self.m.run,
                     lambda: self.m.zero("X"), lambda: self.m.goto("xy"), lambda: self.m.home(""), self.m.unlock):
            self.refused(call, SCANNING)
        for x, y in ((101, 0), (0, "1"), (None, 0)):
            self.refused(lambda: self.m.scan_move(x, y), "Scan move: X and Y must be numbers from -100 to 100 in", 400)
        self.refused(lambda: self.m.scan_move(1, 1, 0), "Scan move: the time limit must be a number of seconds above 0",
                     400)
        self.m.end_activity("probe")                    # (not that one: nothing)
        self.assertEqual(self.m.activity, "scan")
        self.m.release_after = 2.0                      # never let go of the port during a scan
        self.m.alone_since = self.t
        for _ in range(30):
            self.reply(IDLE, dt=0.2)
            self.tick(0)
        self.assertIsNone(self.link.stopping)
        self.m.end_activity("scan")
        self.m.send("G0 X1")
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G0 X1"])

    def test_job_lines_answered_clear_the_jog_cancel_mark(self):
        """A job line sent after a jog cancel and answered: FluidNC isn't stuck (Link.unstick), so a slow line
        later gets no cycle start."""
        self.ready()
        self.start_job("G0 X1\nG0 X2\n")
        self.m.realtime("jogcancel")                    # (allowed; FluidNC ignores it in Run)
        self.realtime()
        self.reply(RUN, dt=0.06)
        self.assertIsNone(self.link.cancel)
        self.load("G0 X3\n")                            # (the next job's line is sent after the cancel)
        self.reply("ok", "ok", IDLE, dt=0.06)
        self.assertEqual(self.job.state, "done")
        self.m.run()
        self.tick(0.01)
        self.assertEqual(self.sent(), ["G20 G90", "G0X3"])
        self.reply("ok", "ok", IDLE, dt=0.06)
        self.assertEqual(self.job.state, "done")
        self.assertIsNone(self.link.unstick_at)
        self.m.send("M3 S1000")                         # (a spindle that takes a while to spin up)
        self.tick(0.01)
        for _ in range(30):
            self.reply(IDLE, dt=0.1)
            self.tick(0)
        self.assertEqual(self.realtime(), b"")          # no '~'


# ------------------------------------------------------------------ the probe, scanner and pendant in the snapshot

class Extras(unittest.TestCase):
    def test_their_state_in_the_snapshot(self):
        m = machine.Machine()

        class Thing:
            def __init__(self, kind, state, emit_meanwhile=None):
                self.kind, self.value, self.meanwhile = kind, state, emit_meanwhile

            def state(self):
                if self.meanwhile:                      # an event of theirs just after state() was asked
                    m.emit({"type": self.kind, self.kind: self.meanwhile})
                return self.value

        snap = m.snapshot()
        self.assertEqual((snap["probe"], snap["scan"], snap["pendant"]), (None, None, None))
        m.attach(probe=Thing("probe", {"connected": False}), scanner=Thing("scan", {"running": False}, {"running": True}),
                 pendant=Thing("pendant", {"state": "off"}))
        snap = m.snapshot()
        self.assertEqual((snap["probe"], snap["scan"], snap["pendant"]),
                         ({"connected": False}, {"running": True}, {"state": "off"}))
        sub = m.subscribe()
        first = json.loads(sub.get(0).split(b"\n")[0])
        self.assertEqual((first["type"], first["pendant"], first["jogSettings"], first["program"], first["job"]),
                         ("snapshot", {"state": "off"}, {"step": 0.1, "feed": 150}, None, None))

        class Broken:
            def state(self):
                raise RuntimeError("bug")
        m.attach(probe=Broken())
        with mock.patch.object(machine.traceback, "print_exc"):
            self.assertIsNone(m.snapshot()["probe"])

    def test_test_ports_only(self):
        m = machine.Machine(extra_ports=["/dev/pts/77"], open_only_extra=True)
        self.assertEqual(m.check_port(" /dev/pts/77 "), "/dev/pts/77")
        for port in ("/dev/rfcomm1", "/dev/ttyACM0"):
            with self.assertRaises(MachineError) as cm:
                m.check_port(port)
            self.assertEqual(str(cm.exception), f"{port} is not one of the test ports this server may open")
        with self.assertRaises(MachineError):
            m.check_port("")
        self.assertEqual(machine.Machine().check_port("/dev/rfcomm1"), "/dev/rfcomm1")


if __name__ == "__main__":
    unittest.main()
