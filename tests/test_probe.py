"""
Unit tests for probe.py: the probe link against tools/fake_probe.py (or the test itself playing the probe)
on pseudo terminals made here, and the port checks. Real serial ports are never opened: the checks that
refuse /dev/ttyUSB*, /dev/rfcomm* and other boards run with open_port replaced by a trap.

    python3 -m unittest tests.test_probe -v
"""

import contextlib
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import machine  # noqa: E402
import probe  # noqa: E402
from machine import MachineError  # noqa: E402
from probe import Probe, ProbeAborted, ProbeError, ProbeOverTravel  # noqa: E402

FAKE = os.path.join(ROOT, "tools", "fake_probe.py")


def fast():
    """Short waits for the tests (the real ones are for the real board)."""
    return mock.patch.multiple(probe, SETTLE=0.05, QUIET=0.005)


class FakeProbeCase(unittest.TestCase):
    """A fake probe on a pty and a Probe connected to it (homed if self.home)."""
    args = ()
    home = True

    def setUp(self):
        self.patch = fast()
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.dir = self.enterContext(tempfile.TemporaryDirectory())
        self.proc = None
        self.start_fake(*self.args)
        self.logs = []
        self.events = []
        self.probe = Probe(self.log, self.events.append, test_ports=[self.path], only_test_ports=True)
        self.addCleanup(self.cleanup)
        self.probe.connect(self.path)
        if self.home:
            self.probe.home()

    def start_fake(self, *args):
        self.fakes = getattr(self, "fakes", 0) + 1     # (each fake its own log)
        self.fake_log = os.path.join(self.dir, f"fake{self.fakes}.log")
        self.proc = subprocess.Popen([sys.executable, FAKE, "--home-time", "0.02", "--probe-time", "0.05",
                                      "--log", self.fake_log, *args], stdout=subprocess.PIPE, text=True)
        self.path = self.proc.stdout.readline().strip()
        self.assertTrue(self.path.startswith("/dev/pts/"), self.path)

    def cleanup(self):
        self.probe.disconnect(force=True)
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc.stdout.close()

    def log(self, kind, text):
        self.logs.append((kind, text))

    def received(self):
        """What the fake probe got and said, from its log."""
        time.sleep(0.05)
        with open(self.fake_log) as f:
            return [json.loads(line) for line in f]

    def commands(self):
        return [e["cmd"] for e in self.received() if "cmd" in e]

    def restart(self, *args):
        """A new fake probe with these arguments, connected (not homed)."""
        self.probe.disconnect()
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.start_fake(*args)
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)

    def wait_for(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                self.fail("timed out")
            time.sleep(0.01)


class Commands(FakeProbeCase):
    args = ("--values", "0.1,0.112,0.095,0.1")

    def test_home_makes_it_ready(self):
        self.assertEqual(self.probe.state(), {"port": self.path, "connected": True, "ready": True, "busy": False,
                                              "last": "H -> homed", "connecting": False, "mayBeDown": False})
        self.assertIn(("info", "Auto Probe homed successfully."), self.logs)
        self.assertEqual(self.commands(), ["H"])
        self.assertEqual(self.events[-1], {"type": "probe", "probe": self.probe.state()})

    def test_retract_is_sent_like_ugs(self):
        self.assertEqual(self.probe.set_retract(0.2), "OK ZR=0.200")
        self.probe.set_retract(0.1235)          # Java rounds the shortest digits half up: 0.124 (Python: 0.123)
        self.assertEqual(self.commands()[1:], ["ZR:0.200", "ZR:0.124"])
        for bad in (0, -1, float("nan"), True, "0.2", 11):
            with self.assertRaises(MachineError):
                self.probe.set_retract(bad)

    def test_first_touch_reads_zero_then_relative(self):
        self.assertEqual([self.probe.probe() for _ in range(3)], [0.0, 0.012, -0.005])
        self.probe.home()                       # a new reference: the next touch reads 0.0 again
        self.assertEqual(self.probe.probe(), 0.0)
        self.assertTrue(self.probe.state()["ready"])

    def test_negative_zero_is_stored_as_zero(self):
        self.probe.disconnect()
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.start_fake("--reply", "P#1=-0.0000")
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)
        self.probe.home()
        z = self.probe.probe()
        self.assertEqual((z, str(z)), (0.0, "0.0"))

    def test_abort_during_a_touch(self):
        self.probe.disconnect()
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.start_fake("--probe-time", "5")
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)
        self.probe.home()
        got = {}

        def touch():
            try:
                got["z"] = self.probe.probe()
            except Exception as e:
                got["error"] = e
        t = threading.Thread(target=touch)
        t.start()
        self.wait_for(lambda: self.probe.state()["busy"] and "P" in self.commands())
        t0 = time.monotonic()
        self.assertTrue(self.probe.abort())
        t.join(2)
        self.assertLess(time.monotonic() - t0, 0.5)     # the waiting command gives up at once
        self.assertIsInstance(got.get("error"), ProbeAborted)
        received = self.received()
        self.assertEqual([e.get("cmd") for e in received if "cmd" in e], ["H", "P", "A"])
        self.assertIn({"abort": "P"}, [{k: v for k, v in e.items() if k != "t"} for e in received])
        st = self.probe.state()
        self.assertEqual((st["ready"], st["busy"], st["last"]), (False, False, "P -> aborted"))
        with self.assertRaises(ProbeError) as cm:          # not ready: home it first
            self.probe.probe()
        self.assertEqual(str(cm.exception), "Probe not ready. Home the probe first.")
        self.assertIn(("info", "Probe abort sent (during P)"), self.logs)

    def test_abort_first_means_the_touch_is_never_sent(self):
        self.probe.abort()
        with self.assertRaises(ProbeError):
            self.probe.probe()
        self.assertEqual(self.commands(), ["H", "A"])

    def test_abort_wins_over_a_command_about_to_be_sent(self):
        """abort() between the drain and the write: the command is not written."""
        real = self.probe.drain

        def drain_then_abort(gen):
            real(gen)
            self.probe.abort()
        with mock.patch.object(self.probe, "drain", drain_then_abort):
            with self.assertRaises(ProbeAborted):
                self.probe.set_retract(0.2)
        self.assertEqual(self.commands(), ["H", "A"])

    def test_one_command_at_a_time(self):
        self.probe.disconnect()
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.start_fake("--probe-time", "1")
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)
        self.probe.home()
        t = threading.Thread(target=self.probe.probe)
        t.start()
        self.wait_for(lambda: self.probe.state()["busy"])
        with self.assertRaises(MachineError) as cm:     # refused, not queued
            self.probe.home()
        self.assertEqual((cm.exception.status, str(cm.exception)), (409, "The probe is busy: wait until it is done"))
        with self.assertRaises(MachineError) as cm:     # nor can the scan claim it meanwhile
            self.probe.claim("scan")
        self.assertEqual(str(cm.exception), "The probe is busy: wait until it is done")
        t.join(3)
        self.assertEqual(self.commands(), ["H", "P"])
        self.assertIsNone(self.probe.owner)

    def test_claimed_probe_takes_only_its_owner(self):
        self.probe.claim("scan")
        with self.assertRaises(MachineError) as cm:
            self.probe.home()
        self.assertIn("The scan is using the probe", str(cm.exception))
        with self.assertRaises(MachineError):
            self.probe.disconnect()
        self.probe.set_retract(0.2, owner="scan")
        self.assertEqual(self.probe.probe(owner="scan"), 0.0)
        self.probe.release("scan")
        self.probe.home()

    def test_disconnect_aborts_a_touch(self):
        self.probe.disconnect()
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()
        self.start_fake("--probe-time", "5")
        self.probe.test_ports = [self.path]
        self.probe.connect(self.path)
        self.probe.home()
        got = {}

        def touch():
            try:
                self.probe.probe()
            except Exception as e:
                got["error"] = e
        t = threading.Thread(target=touch)
        t.start()
        self.wait_for(lambda: "P" in self.commands())
        self.probe.disconnect()
        t.join(2)
        self.assertIsInstance(got["error"], ProbeError)
        self.assertEqual(self.commands(), ["H", "P", "A"])
        self.assertEqual(self.probe.state()["port"], None)
        self.assertIn(("info", f"Probe disconnected from {self.path}"), self.logs)

    def test_lost_port(self):
        self.proc.kill()
        self.proc.wait()
        self.wait_for(lambda: any(k == "error" and t.startswith(f"Lost the connection to the probe on {self.path}")
                                  for k, t in self.logs))
        self.assertFalse(self.probe.state()["connected"])
        self.assertEqual(self.probe.state()["port"], None)
        with self.assertRaises(MachineError):
            self.probe.home()
        self.assertFalse(self.events[-1]["probe"]["connected"])

    def test_connect_twice_refused(self):
        with self.assertRaises(MachineError) as cm:
            self.probe.connect(self.path)
        self.assertEqual(cm.exception.status, 409)


class BadReplies(FakeProbeCase):
    """Each test restarts the fake with its own replies."""
    home = False

    def home_error(self, reply):
        self.restart("--reply", f"H#1={reply}")
        with mock.patch.object(probe, "REPLY_TIMEOUT", 0.3):
            with self.assertRaises(ProbeError) as cm:
                self.probe.home()
        self.assertFalse(self.probe.state()["ready"])
        self.assertEqual(self.logs[-1], ("error", str(cm.exception)))
        return str(cm.exception)

    def test_home_errors_say_what_ugs_says(self):
        self.assertEqual(self.home_error("E"), "Probe error during homing. Check for overtravel.")
        self.assertEqual(self.home_error("ERROR"), "Probe error during homing. Check for overtravel.")
        self.assertEqual(self.home_error("what"), "Unexpected probe response: what")
        t0 = time.monotonic()
        self.assertEqual(self.home_error("none"), "No response from Auto Probe. Is it connected?")
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(self.probe.state()["last"], "H -> no reply")

    def touch_error(self, reply, exc=ProbeError):
        self.restart("--reply", f"P#1={reply}")
        self.probe.home()
        with mock.patch.object(probe, "REPLY_TIMEOUT", 0.3):
            with self.assertRaises(exc) as cm:
                self.probe.probe()
        self.assertFalse(self.probe.state()["ready"])
        return str(cm.exception)

    def test_touch_errors(self):
        self.assertEqual(self.touch_error("E", ProbeOverTravel), "Probe reported over travel while probing.")
        self.assertEqual(self.touch_error("error", ProbeOverTravel), "Probe reported over travel while probing.")
        self.assertEqual(self.touch_error("none"), "No response from probe to P.")
        self.assertEqual(self.touch_error("abc"), "Invalid probe numeric response: abc")
        self.assertEqual(self.touch_error("1e999"), "Non-finite probe value: 1e999")
        self.assertEqual(self.touch_error("NaN"), "Non-finite probe value: NaN")

    def test_over_travel_needs_a_home(self):
        self.restart("--overtravel-at", "2", "--values", "0.5,0.6,0.7")
        self.probe.home()
        self.assertEqual(self.probe.probe(), 0.0)
        with self.assertRaises(ProbeOverTravel):
            self.probe.probe()
        with self.assertRaises(ProbeError) as cm:
            self.probe.probe()
        self.assertEqual(str(cm.exception), "Probe not ready. Home the probe first.")
        self.probe.home()
        self.assertEqual(self.probe.probe(), 0.0)

    def retract_error(self, reply):
        self.restart("--reply", f"ZR#1={reply}")
        with mock.patch.object(probe, "REPLY_TIMEOUT", 0.3):
            with self.assertRaises(ProbeError) as cm:
                self.probe.set_retract(0.2)
        return str(cm.exception)

    def test_retract_errors(self):
        self.assertEqual(self.retract_error("E"), "Probe reported an error on Z retract (ZR).")
        self.assertEqual(self.retract_error("ERR 3"), "Probe reported an error on Z retract (ZR).")
        self.assertEqual(self.retract_error("ZR?"), "Unexpected probe response to Z retract (ZR): ZR?")
        self.assertEqual(self.retract_error("none"), "No response from probe to Z retract (ZR).")
        self.restart("--reply", "ZR#1=ok zr")
        self.assertEqual(self.probe.set_retract(0.2), "ok zr")     # (UGS: any reply starting with OK)


class MayBeDown(FakeProbeCase):
    """state()["mayBeDown"]: the tip may be down. On from the moment a P or an H is sent and with an abort, off only
    when an H comes back "homed"; it outlives the port, so a page opened after the probe was let go of (no window
    open: Machine.release_extras) or disconnected still warns before X / Y moves."""
    args = ("--values", "0.1,0.112")
    home = False

    def down(self):
        return self.probe.state()["mayBeDown"]

    def test_on_with_a_touch_off_with_a_good_home(self):
        self.assertEqual((self.probe.state()["ready"], self.down()), (False, False))   # (just connected: never moved)
        with self.assertRaises(ProbeError):             # (refused, not homed: nothing sent)
            self.probe.probe()
        self.probe.set_retract(0.2)                     # (ZR moves nothing)
        self.assertFalse(self.down())
        self.probe.home()
        self.assertEqual((self.probe.state()["ready"], self.down()), (True, False))
        self.assertEqual(self.probe.probe(), 0.0)       # it backed off ZR after the touch, but it is not homed
        self.assertEqual((self.probe.state()["ready"], self.down()), (True, True))     # (a scan goes on probing)
        self.assertTrue(self.events[-1]["probe"]["mayBeDown"])                          # (and the page hears it)
        self.probe.home()
        self.assertFalse(self.down())
        self.assertEqual(self.events[-1]["probe"]["mayBeDown"], False)
        self.assertEqual(self.commands(), ["ZR:0.200", "H", "P", "H"])

    def test_an_abort_turns_it_on_and_it_outlives_the_port(self):
        self.probe.home()
        self.assertTrue(self.probe.abort())             # (a scan Stop aborts the probe)
        self.assertTrue(self.down())
        self.probe.disconnect()
        st = self.probe.state()
        self.assertEqual((st["port"], st["connected"], st["ready"], st["mayBeDown"]), (None, False, False, True))
        self.assertTrue(self.events[-1]["probe"]["mayBeDown"])
        self.probe.connect(self.path)                   # connected again: still on until it is homed
        self.assertEqual((self.probe.state()["ready"], self.down()), (False, True))
        self.probe.home()
        self.assertEqual((self.probe.state()["ready"], self.down()), (True, False))

    def test_a_failed_home_turns_it_on(self):
        for reply in ("E", "what", "none"):
            self.restart("--reply", f"H#2={reply}")
            self.probe.home()
            self.assertFalse(self.down(), reply)
            with mock.patch.object(probe, "REPLY_TIMEOUT", 0.3):
                with self.assertRaises(ProbeError):
                    self.probe.home()
            self.assertEqual((self.probe.state()["ready"], self.down()), (False, True), reply)

    def test_an_abort_after_the_homed_reply_wins(self):
        self.probe.home()
        self.probe.probe()
        real = self.probe.set_ready

        def abort_first(gen, ready):
            self.probe.abort()
            real(gen, ready)
        with mock.patch.object(self.probe, "set_ready", abort_first):
            self.probe.home()
        self.assertEqual((self.probe.state()["ready"], self.down()), (False, True))

    def test_a_refused_command_changes_nothing(self):
        self.probe.home()
        self.probe.claim("scan")
        with self.assertRaises(MachineError):           # (claimed: refused before anything is sent)
            self.probe.probe()
        self.assertFalse(self.down())
        self.probe.release("scan")
        self.assertEqual(self.commands(), ["H"])

    def test_a_port_lost_during_a_touch_leaves_it_on(self):
        self.restart("--probe-time", "5")
        self.probe.home()
        got = {}

        def touch():
            try:
                self.probe.probe()
            except Exception as e:
                got["error"] = e
        t = threading.Thread(target=touch)
        t.start()
        self.wait_for(lambda: "P" in self.commands())
        self.proc.kill()
        self.proc.wait()
        t.join(3)
        self.assertIsInstance(got.get("error"), ProbeError)
        st = self.probe.state()
        self.assertEqual((st["port"], st["connected"], st["mayBeDown"]), (None, False, True))


class LetGo(FakeProbeCase):
    """Machine.release_extras with a real probe link, no window open for longer than release_after and FluidNC not
    connected: a connect still settling is left to finish (the review: each such connect was cut short with 400);
    a probe let go of while its tip may be down keeps saying so, and the console says it too; so does one connected
    and never homed (Probe.let_go: the page warned for it)."""
    home = False

    def setUp(self):
        super().setUp()
        self.m = machine.Machine(release_after=2.0)
        self.m.attach(probe=self.probe)
        self.m.alone_since = time.monotonic() - 5

    def console(self):
        return [(e["dir"], e["text"]) for e in self.m.console]

    def test_a_connect_still_settling_is_left_to_finish(self):
        self.probe.disconnect()
        got = {}

        def connect():
            try:
                got["value"] = self.probe.connect(self.path)
            except Exception as e:
                got["error"] = e
        with mock.patch.object(probe, "SETTLE", 0.6):
            t = threading.Thread(target=connect)
            t.start()
            self.wait_for(lambda: self.probe.state()["connecting"])
            for _ in range(3):
                self.assertEqual(self.m.release_extras(time.monotonic()), [])
                time.sleep(0.1)
            t.join(3)
        self.assertEqual(got, {"value": {"port": self.path}})
        self.assertTrue(self.probe.state()["connected"])
        self.assertEqual(self.console(), [])
        self.assertEqual(self.m.release_extras(time.monotonic()), ["probe"])     # (connected now: it goes)
        self.assertIsNone(self.probe.state()["port"])

    def test_let_go_of_while_it_may_be_down(self):
        self.probe.home()
        self.assertTrue(self.probe.abort())             # (a scan Stop)
        self.assertEqual(self.m.release_extras(time.monotonic()), ["probe"])
        st = self.probe.state()
        self.assertEqual((st["port"], st["connected"], st["ready"], st["mayBeDown"]), (None, False, False, True))
        self.assertEqual(self.console(), [("info", "Released the probe because no FluidSender window is open"),
                                          ("error", machine.PROBE_LET_GO_DOWN)])
        self.assertIn("the probe may be down: Home Probe before moving X or Y", machine.PROBE_LET_GO_DOWN)
        self.probe.connect(self.path)                   # the next window: connect it and Home Probe
        self.probe.home()
        self.assertFalse(self.probe.state()["mayBeDown"])
        self.assertEqual(self.m.release_extras(time.monotonic()), ["probe"])     # homed: no warning this time
        self.assertEqual(self.console()[2:], [("info", "Released the probe because no FluidSender window is open")])

    def test_connected_and_never_homed_keeps_the_warning_too(self):
        """The verify of the release: a probe connected and never homed (the page warns while it is connected: just
        connected, its tip may still be down) lost that warning when the release let go of it, so a window opened
        later said nothing. let_go keeps it on, the console says so, and Home Probe clears it as always."""
        st = self.probe.state()
        self.assertEqual((st["connected"], st["ready"], st["mayBeDown"]), (True, False, False))   # (the page warns)
        self.assertEqual(self.m.release_extras(time.monotonic()), ["probe"])
        st = self.probe.state()
        self.assertEqual((st["port"], st["connected"], st["ready"], st["mayBeDown"]), (None, False, False, True))
        self.assertEqual(self.events[-1]["probe"]["mayBeDown"], True)                  # (and the page hears it)
        self.assertEqual(self.console(), [("info", "Released the probe because no FluidSender window is open"),
                                          ("error", machine.PROBE_LET_GO_DOWN)])
        self.assertEqual(self.commands(), [])           # (nothing sent for it: the probe moves only when told to)
        self.probe.connect(self.path)
        self.probe.home()
        self.assertFalse(self.probe.state()["mayBeDown"])
        self.assertEqual(self.m.release_extras(time.monotonic()), ["probe"])     # homed: no warning this time
        self.assertEqual(self.console()[2:], [("info", "Released the probe because no FluidSender window is open")])

    def test_the_users_own_disconnect_leaves_it_as_it_is(self):
        """Only the release keeps that warning: the user's own Disconnect of a probe never homed (the page warned
        them) doesn't turn mayBeDown on, as before."""
        self.probe.disconnect()
        self.assertFalse(self.probe.state()["mayBeDown"])

    def test_a_failed_home_is_still_said_after_the_release(self):
        """The Help: a Home Probe that fails leaves the probe not homed, its tip maybe down, also once the window
        closed and let go of it."""
        self.restart("--reply", "H#1=E")
        with self.assertRaises(ProbeError):
            self.probe.home()
        self.assertEqual(self.m.release_extras(time.monotonic()), ["probe"])
        self.assertTrue(self.probe.state()["mayBeDown"])
        self.assertEqual(self.console()[-1], ("error", machine.PROBE_LET_GO_DOWN))

    def test_a_claimed_probe_is_not_let_go_of(self):
        """let_go is refused like disconnect() while the probe is claimed (the scan), and then changes nothing."""
        self.probe.claim("scan")
        with self.assertRaises(MachineError):
            self.probe.let_go()
        st = self.probe.state()
        self.assertEqual((st["connected"], st["mayBeDown"]), (True, False))
        self.probe.release("scan")


class PlayedProbe(unittest.TestCase):
    """The test itself is the probe, on the master side of a pty: exact bytes and timing."""

    def setUp(self):
        self.patch = fast()
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        os.close(slave)
        self.logs = []
        self.probe = Probe(lambda k, t: self.logs.append((k, t)), test_ports=[self.path], only_test_ports=True)
        self.probe.connect(self.path)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.probe.disconnect(force=True)
        os.close(self.master)

    def read_line(self, timeout=2.0):
        buf = b""
        end = time.monotonic() + timeout
        while not buf.endswith(b"\n"):
            left = end - time.monotonic()
            if left <= 0:
                self.fail(f"nothing came: {buf!r}")
            if select.select([self.master], [], [], left)[0]:
                buf += os.read(self.master, 1)
        return buf

    def answer(self, call, *replies, noise=()):
        """Runs call in a thread while answering its command with replies (written byte by byte)."""
        got = {}

        def run():
            try:
                got["value"] = call()
            except Exception as e:
                got["error"] = e
        for n in noise:                     # stale lines before the command
            os.write(self.master, n)
        t = threading.Thread(target=run)
        t.start()
        got["sent"] = self.read_line()
        for r in replies:
            for b in r:
                os.write(self.master, bytes([b]))
        t.join(3)
        return got

    def test_crlf_split_replies_and_empty_lines(self):
        got = self.answer(self.probe.home, b"\r\n", b"\r\nhom", b"ed\r\n")
        self.assertEqual((got["sent"], got.get("value")), (b"H\n", {"message": "Auto Probe homed successfully."}))

    def test_stale_lines_are_drained(self):
        """Noise right before a command (a late reply to an aborted one) must not be taken as its answer."""
        got = self.answer(self.probe.home, b"homed\r\n", noise=[b"0.123\r\n", b"E\r\n", b"homed\r\n"])
        self.assertEqual(got["sent"], b"H\n")
        self.assertEqual(got.get("value"), {"message": "Auto Probe homed successfully."})
        self.assertTrue(self.probe.state()["ready"])

        def late_noise():
            time.sleep(0.02)
            for _ in range(5):              # still talking when the command starts: it waits for quiet
                os.write(self.master, b"-0.5\r\n")
                time.sleep(0.003)
        threading.Thread(target=late_noise).start()
        time.sleep(0.03)
        got = self.answer(self.probe.probe, b"0.0000\r\n")
        self.assertEqual((got["sent"], got.get("value")), (b"P\n", 0.0))

    def test_abort_goes_out_after_the_touch(self):
        got = {}

        def touch():
            try:
                self.probe.probe()
            except Exception as e:
                got["error"] = e
        self.answer(self.probe.home, b"homed\r\n")
        t = threading.Thread(target=touch)
        t.start()
        self.assertEqual(self.read_line(), b"P\n")
        self.probe.abort()
        self.assertEqual(self.read_line(), b"A\n")
        t.join(2)
        self.assertIsInstance(got["error"], ProbeAborted)
        os.write(self.master, b"0.0100\r\n")    # a late reply: logged, never taken for the next command
        time.sleep(0.1)
        self.assertIn(("info", "Probe says: 0.0100"), self.logs)
        got = self.answer(self.probe.home, b"homed\r\n")
        self.assertEqual(got.get("value"), {"message": "Auto Probe homed successfully."})


class Ports(unittest.TestCase):
    """Which ports the probe link opens. Every refusal must come before anything is opened."""

    def setUp(self):
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        os.close(slave)
        self.addCleanup(os.close, self.master)
        self.probe = Probe(lambda k, t: None)
        self.addCleanup(self.probe.disconnect, True)
        self.enterContext(fast())

    def refused(self, path, force=False, **patches):
        trap = mock.Mock(side_effect=AssertionError("must not open"))
        os_trap = mock.Mock(side_effect=AssertionError("must not open"))
        with mock.patch.object(probe, "open_port", trap), mock.patch("os.open", os_trap), \
                mock.patch.multiple(probe, **patches) if patches else contextlib.nullcontext():
            with self.assertRaises(MachineError) as cm:
                self.probe.connect(path, force=force)
        trap.assert_not_called()
        os_trap.assert_not_called()
        self.assertIsNone(self.probe.state()["port"])
        return str(cm.exception)

    def test_fluidnc_and_pendant_ports_are_never_opened(self):
        # (names no device has here: the refusal goes by the kind of name, before anything is opened)
        for force in (False, True):
            self.assertIn("is a USB serial port like FluidNC's", self.refused("/dev/ttyUSB97", force))
            self.assertIn("is a Bluetooth port (the jog pendant)", self.refused("/dev/rfcomm97", force))
            self.assertIn("is a USB serial port like FluidNC's",
                          self.refused(self.path, force, port_kind=mock.Mock(return_value="usb-serial")))
        self.assertIn("is not a USB serial port like the probe's", self.refused("/dev/ttyS97"))
        self.assertIn("is not a USB serial port like the probe's", self.refused(self.path))   # (a pty, not a test one)
        self.assertIn("is not there", self.refused("/dev/ttyACM97"))
        self.assertEqual(self.refused("  "), "No probe port given")

    def test_another_board_needs_force(self):
        msg = self.refused(self.path, port_kind=mock.Mock(return_value="usb-acm"),
                           usb_info=mock.Mock(return_value=("1a86", "55d4", "USB Single Serial")),
                           describe_port=mock.Mock(return_value={"label": "USB Single Serial (1a86:55d4)"}))
        self.assertEqual(msg, f"{self.path} looks like USB Single Serial (1a86:55d4), not the probe (Arduino "
                              "Leonardo). Not connected.")
        with mock.patch.multiple(probe, port_kind=mock.Mock(return_value="usb-acm"),
                                 usb_info=mock.Mock(return_value=("1a86", "55d4", "USB Single Serial"))):
            self.probe.connect(self.path, force=True)
        self.assertTrue(self.probe.state()["connected"])

    def test_the_leonardo_needs_no_force(self):
        with mock.patch.multiple(probe, port_kind=mock.Mock(return_value="usb-acm"),
                                 usb_info=mock.Mock(return_value=("2341", "8036", "Arduino Leonardo"))):
            self.probe.connect(self.path)
        self.assertEqual(self.probe.state()["port"], self.path)

    def test_a_port_another_program_has_is_refused(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import os, sys, time; os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY); "
             "print('open', flush=True); time.sleep(30)", self.path], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "open")
            msg = self.refused(self.path, port_kind=mock.Mock(return_value="usb-acm"),
                               usb_info=mock.Mock(return_value=("2341", "8036", "Arduino Leonardo")))
            self.assertRegex(msg, rf"^Port {self.path} is in use by python3[.\d]* \(pid {child.pid}\)\. "
                                  r"Disconnect it there first\.$")
        finally:
            child.kill()
            child.wait()
            child.stdout.close()

    def test_only_test_ports_in_test_mode(self):
        p = Probe(lambda k, t: None, test_ports=["/dev/pts/999999"], only_test_ports=True)
        trap = mock.Mock(side_effect=AssertionError("must not open"))
        with mock.patch.object(probe, "open_port", trap):
            for path in (self.path, "/dev/ttyACM97", "/dev/ttyUSB97"):
                with self.assertRaises(MachineError) as cm:
                    p.connect(path)
                self.assertIn("is not one of the test ports", str(cm.exception))
        trap.assert_not_called()

    def test_default_port(self):
        ports = [{"path": "/dev/ttyUSB0", "kind": "usb-serial", "vid": "1a86", "pid": "7523", "inUseBy": None},
                 {"path": "/dev/ttyACM0", "kind": "usb-acm", "vid": "2341", "pid": "8036",
                  "inUseBy": "UGS (java, pid 7)"},
                 {"path": "/dev/ttyACM1", "kind": "usb-acm", "vid": "2341", "pid": "8036", "inUseBy": None}]
        self.assertEqual(probe.default_port(ports), "/dev/ttyACM1")
        self.assertIsNone(probe.default_port(ports[:2]))


class JavaFixed(unittest.TestCase):
    def test_like_java_string_format(self):
        # (each checked against Java 17's String.format)
        cases = [(0.2, 3, "0.200"), (0.1235, 3, "0.124"), (0.0125, 3, "0.013"), (2.0, 3, "2.000"),
                 (8.5, 5, "8.50000"), (12.0, 5, "12.00000"), (1.234565, 5, "1.23457"), (-0.0, 3, "-0.000"),
                 (-0.0001, 3, "-0.000"), (1e16, 3, "10000000000000000.000")]
        for v, places, want in cases:
            self.assertEqual(probe.java_fixed(v, places), want, (v, places))
        self.assertEqual("%.3f" % 0.1235, "0.123")     # (why Python's own % won't do)


if __name__ == "__main__":
    unittest.main()
