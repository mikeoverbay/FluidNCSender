"""
Unit tests for pendant.py and tools/fake_pendant.py: the SYNC packet and the status the screen shows, the
handshake, every key's Machine call (Run resuming, no resume after an error), refused keys, the shared jog step,
the REQ timeout, the firmware's own rule for Run, Pause and Stop (original and fixed), the holder rules (another
program, Blueman's rfcomm watcher), disconnecting and losing the port. The Machine is a mock (and, at the end, the
real one: in test mode, where with tools/fake_fluidnc.py a key really jogs, and in test_jobs.py's harness for the
status and the keys during a job); the pendant is played byte by byte on a pty made here, or by fake_pendant.py
(either firmware's logic and timing) on its own pty. Real serial ports are never opened.

    python3 -m unittest tests.test_pendant -v
"""

import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
import machine  # noqa: E402
import pendant  # noqa: E402
import test_jobs as jobs  # noqa: E402
from machine import MachineError  # noqa: E402

FAKE = os.path.join(HERE, "tools", "fake_pendant.py")
CALLS = ("jog", "zero", "goto", "home", "run", "pause_toggle", "stop", "set_jog_settings", "jog_settings",
         "pendant_status", "status_now")
AHEAD = {"max_steps": 2}    # a jog key is refused while 2 step jogs are still waiting or moving
ERR = "line 2: G0X2Q.02: error:20 (unsupported or invalid G-code)"
PAUSED_BY = {"hold": "paused", "resume": "running"}     # a job's state after Machine.pause_toggle()'s "did"
LIMIT_REPORT = "<Alarm|MPos:1.000,0.000,0.000|FS:0,0>"  # (a critical alarm: FluidNC reports it as Alarm)
ON_ERR = (f"The job stopped on an error ({ERR}). See it on the page, then Resume there to go on, or Stop")


class MockMachine:
    """The Machine calls the pendant makes, recorded as (name, args, kwargs) (jog_settings, pendant_status and
    status_now aren't). refuse[name] = message: that call raises MachineError; block[name] = Event: it waits for
    that first. now: what status_now() says (None: not connected). The job, a stop under way and a scan are the
    real Machine's fields (see set_job; link stays None otherwise, as the FluidNC port check wants it)."""

    def __init__(self, extra=()):
        self.lock = threading.RLock()
        self.extra = list(extra)
        self.open_only_extra = True
        self.link = None
        self.activity = None                # "scan" while a scan runs
        self.events, self.console, self.calls = [], [], []
        self.refuse, self.block = {}, {}
        self.js = {"step": 0.1, "feed": 150.0}
        self.st = {"x": 1.5, "y": -2.25, "z": 0.125, "status": 0, "line": 0, "total": 0, "live": 0.0}
        self.now = {"state": "Idle", "wpos": [1.5, -2.25, 0.5]}     # (Z clear of the work)
        self.did, self.stopped = "hold", "job"      # what pause_toggle() and stop() say they did
        self.listed = []                    # what ports() lists

    def check_port(self, path):
        return machine.Machine.check_port(self, path)      # (the real rule, on extra and open_only_extra)

    def emit(self, ev):
        self.events.append(json.loads(json.dumps(ev, allow_nan=False)))     # (the real one sends it as JSON)

    def log(self, kind, text):
        self.console.append((kind, text))

    def _call(self, name, args, kwargs, out=None):
        self.calls.append((name, args, kwargs))
        if name in self.block:
            self.block[name].wait(10)
        if name in self.refuse:
            raise MachineError(self.refuse[name], 409)
        return out

    def jog(self, *a, **k):
        return self._call("jog", a, k, {"line": "$J=G20G91X0.1F150"})

    def zero(self, *a, **k):
        return self._call("zero", a, k, {"line": "G10 L20 P0 G20 Z0"})

    def goto(self, *a, **k):
        return self._call("goto", a, k, {"lines": ["G20 G90 G0 Z0"]})

    def home(self, *a, **k):
        return self._call("home", a, k, {"line": "$H"})

    def run(self, *a, **k):
        return self._call("run", a, k, {"job": {"name": "t.nc", "total": 35, "state": "running"}})

    def pause_toggle(self, *a, **k):
        out = self._call("pause_toggle", a, k, {"did": self.did})
        job = getattr(self.link, "job", None)
        if job is not None and self.did in PAUSED_BY:
            job.state = PAUSED_BY[self.did]     # (as the real one: the job's state changes at once)
        return out

    def stop(self, *a, **k):
        return self._call("stop", a, k, {"stopped": self.stopped})

    def realtime(self, *a, **k):
        return self._call("realtime", a, k)

    def set_job(self, state=None, error=None, halt=None, reset_at=None, name="t.nc", **link):
        """The FluidNC link's job (state None: no job), the safe stop (halt) and its reset (reset_at), as
        machine_view() reads them; link: more of the link's fields (alarm, after_reset, reset_clean, resume_at)."""
        job = SimpleNamespace(state=state, error=error, name=name) if state else None
        self.link = SimpleNamespace(path="/dev/fluidnc-mock", job=job, halt=halt, reset_at=reset_at, **link)

    def set_jog_settings(self, *a, **k):
        self._call("set_jog_settings", a, k)
        self.js.update({key: v for key, v in k.items() if v is not None})
        return dict(self.js)

    def jog_settings(self):
        return dict(self.js)

    def pendant_status(self):
        return dict(self.st, step=self.js["step"], feed=self.js["feed"])

    def status_now(self):
        return dict(self.now) if self.now else None

    def ports(self):
        return {"ports": [dict(p) for p in self.listed], "default": None}


class MockSettings:
    def __init__(self, **data):
        self.data = dict(data)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


def packet(**st):
    return pendant.sync_packet(st)


def packet_now(m):
    """The SYNC the pendant gets now: pendant_status()'s numbers, with the status its screen should say."""
    with m.lock:
        return pendant.sync_packet(dict(m.pendant_status(), status=pendant.screen_status(pendant.machine_view(m))))


class Waits(unittest.TestCase):
    def wait(self, cond, timeout=3.0, what="it"):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = cond()
            if got:
                return got
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}")

    def said(self, m, text):
        """The console lines (kind, text) that contain text."""
        return [c for c in m.console if text in c[1]]


# ------------------------------------------------------------------ the packet (no port)

class SyncPacket(unittest.TestCase):
    def test_known_values(self):
        st = {"x": 1.5, "y": -2.25, "z": 0.125, "status": 2, "feed": 150, "line": 12, "total": 345, "step": 0.001,
              "live": 87.5}
        got = pendant.sync_packet(st)
        self.assertEqual(len(got), 44)
        self.assertEqual(got, b"SYNC" + struct.pack("<10f", 1.5, -2.25, 0.125, 0.0, 2.5, 150, 12, 345, 0.001, 87.5))
        self.assertEqual(got[:24].hex(), "53594e43" "0000c03f" "000010c0" "0000003e" "00000000" "00002040")
        self.assertEqual(got[24:].hex(), "00001643" "00004041" "0080ac43" "6f12833a" "0000af42")

    def test_bad_numbers_are_zero(self):
        """NaN, infinity, None, text and numbers too big for a float32 go as 0 (as UGS does with NaN); a field left
        out is 0; a status that isn't one of the screen's (0-13) is 3 (BUSY: the pendant never says IDLE then)."""
        got = pendant.sync_packet({"x": float("nan"), "y": float("inf"), "z": -float("inf"), "feed": None,
                                   "line": "12", "total": 1e39, "step": "abc", "status": 1})
        self.assertEqual(got, b"SYNC" + struct.pack("<10f", 0, 0, 0, 0, 1.5, 0, 0, 0, 0, 0))
        for status in range(14):            # (each + 0.5: FluidSender's mark; the firmware keeps the whole number)
            self.assertEqual(struct.unpack("<10f", pendant.sync_packet({"status": status})[4:])[4], status + 0.5)
        for status in (None, float("nan"), 14, -1, 2.5, True, "2"):
            vals = struct.unpack("<10f", pendant.sync_packet({"status": status})[4:])
            self.assertEqual(vals, (0, 0, 0, 0, 3.5, 0, 0, 0, 0, 0), status)
        self.assertEqual(pendant.sync_packet(None), b"SYNC" + struct.pack("<10f", 0, 0, 0, 0, 3.5, 0, 0, 0, 0, 0))
        self.assertEqual(struct.unpack("<10f", pendant.sync_packet({"status": 2.0})[4:])[4], 2.5)
        self.assertEqual(pendant.MARK, 0.5)

    def test_key_names(self):
        self.assertEqual([pendant.key_name(k) for k in (0, 5, 8, 11, 13, 17, 22, 23, 25, 26, 27, 1, 16, 20)],
                         ["Jog Z-", "Jog X+", "Jog step 0.001", "Jog step 1", "Zero Z", "Machine Z0 (G53 G0 Z0)",
                          "Return to zero", "Home all ($H)", "Run", "Pause/Resume", "Stop", "A-", "Home A", "Key 20"])


# ------------------------------------------------------------------ the test plays the pendant on a pty

class RawPty(Waits):
    """A pty pair made here: the test writes the pendant's lines on the master and reads what comes back."""

    def setUp(self):
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        os.close(slave)
        os.set_blocking(self.master, False)
        self.m = MockMachine([self.path])
        self.settings = MockSettings()
        self.p = pendant.PendantLink(self.m, self.settings)

    def tearDown(self):
        self.p.disconnect()
        if self.master is not None:
            os.close(self.master)

    def say(self, *lines):
        os.write(self.master, b"".join(line.encode() + b"\r\n" for line in lines))

    def read(self, n, timeout=2.0):
        data = b""
        end = time.monotonic() + timeout
        while len(data) < n:
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.master], [], [], left)[0]:
                break
            try:
                data += os.read(self.master, n - len(data))
            except BlockingIOError:
                continue
            except OSError:
                break
        return data

    def quiet(self, wait=0.3):
        """True if nothing comes back for a while."""
        return self.read(1, wait) == b""

    def handshake(self):
        self.p.connect(self.path)
        self.say("CONREQ")
        self.assertEqual(self.read(7), b"CONACK\n")
        self.wait(lambda: self.p.state()["state"] == "connected", what="connected")

    def key(self, n):
        """KEY:n (the REQ after it is left out); returns the SYNC sent once it is done."""
        self.say(f"KEY:{n}")
        got = self.read(44)
        self.assertEqual(len(got), 44, f"no SYNC after KEY:{n}")
        return got


class Handshake(RawPty):
    def test_conreq_gets_exactly_conack_every_time(self):
        self.p.connect(self.path)
        self.assertEqual(self.p.state()["state"], "waiting")
        self.assertEqual(self.m.console, [("info", f"Pendant: listening on {self.path}; waiting for the pendant to "
                                                   "connect")])
        self.say("CONREQ")
        self.assertEqual(self.read(7), b"CONACK\n")         # no "\r": the firmware compares the whole line
        self.assertTrue(self.quiet())
        self.wait(lambda: self.p.state()["state"] == "connected", what="connected")
        self.assertEqual(self.m.console[-1], ("info", f"Pendant: connected on {self.path}"))
        self.assertEqual([e["pendant"]["state"] for e in self.m.events], ["waiting", "connected"])
        self.say("CONREQ", "CONREQ")                        # every one is answered, however many
        self.assertEqual(self.read(14), b"CONACK\nCONACK\n")
        self.assertFalse(self.said(self.m, "connected again"))   # (no REQ came between: the same handshake)
        self.say("REQ")
        self.assertEqual(len(self.read(44)), 44)
        self.say("CONREQ")                                  # it had a session, and asks again: it lost the link
        self.assertEqual(self.read(7), b"CONACK\n")
        self.wait(lambda: self.said(self.m, "Pendant: connected again"), what="the note")
        self.assertEqual(self.p.state()["state"], "connected")

    def test_req_gets_the_sync(self):
        self.handshake()
        self.say("REQ")
        self.assertEqual(self.read(44), b"SYNC" + struct.pack("<10f", 1.5, -2.25, 0.125, 0, 0.5, 150, 0, 0, 0.1, 0))
        self.m.st.update(x=float("nan"), line=7, total=90, live=88.0)
        self.m.set_job("running")
        self.m.js["step"] = 0.01
        self.say("REQ")
        self.assertEqual(self.read(44), packet(x=0, y=-2.25, z=0.125, status=2, feed=150, line=7, total=90, step=0.01,
                                               live=88.0))
        self.assertTrue(self.quiet())                       # one SYNC per REQ, nothing else

    def test_the_status_is_what_the_screen_should_say(self):
        """pendant_status()'s 0-3 made finer from the Machine's state: the original firmware shows 0 IDLE, 1 HOLD,
        2 RUN and BUSY for the rest (so 4 and up are BUSY there, as 3 was), the fixed one names each."""
        self.handshake()
        m = self.m
        cases = [({"state": "Idle"}, {}, None, pendant.IDLE), ({"state": "Run"}, {}, None, pendant.MOVING),
                 ({"state": "Jog"}, {}, None, pendant.JOG), ({"state": "Home"}, {}, None, pendant.HOMING),
                 ({"state": "Hold"}, {}, None, pendant.PAUSED), ({"state": "Alarm"}, {}, None, pendant.ALARM),
                 ({"state": "Door"}, {}, None, pendant.DOOR), ({"state": "Check"}, {}, None, pendant.BUSY),
                 ({"state": "Sleep"}, {}, None, pendant.BUSY), ({"state": "Starting"}, {}, None, pendant.BUSY),
                 ({"state": "No response"}, {"state": "running"}, None, pendant.OFFLINE),
                 (None, {}, None, pendant.OFFLINE),
                 ({"state": "Run"}, {"state": "running"}, None, pendant.RUNNING),
                 ({"state": "Idle"}, {"state": "running"}, None, pendant.RUNNING),     # (between lines)
                 ({"state": "Hold"}, {"state": "paused"}, None, pendant.PAUSED),       # (Feed Hold, M0)
                 ({"state": "Hold"}, {"state": "paused", "error": ERR}, None, pendant.ERROR),
                 ({"state": "Door"}, {"state": "paused"}, None, pendant.DOOR),
                 ({"state": "Door", "sub": "1"}, {"state": "paused"}, None, pendant.DOOR),     # (open, or parking)
                 ({"state": "Door", "sub": "0"}, {"state": "paused"}, None, pendant.PAUSED),   # (closed: Run goes on)
                 ({"state": "Door", "sub": "0"}, {}, None, pendant.PAUSED),
                 ({"state": "Door", "sub": "0"}, {"state": "paused", "error": ERR}, None, pendant.ERROR),
                 ({"state": "Alarm"}, {"alarm": {"code": 1, "critical": True}}, None, pendant.LIMIT),
                 ({"state": "Alarm"}, {"alarm": {"code": 14, "critical": False}}, None, pendant.ALARM),
                 ({"state": "Hold"}, {"state": "stopping", "halt": {"at": 1}}, None, pendant.STOPPING),
                 ({"state": "Hold"}, {"halt": {"at": 1}}, None, pendant.STOPPING),    # (Stop of a move: no job)
                 ({"state": "Hold"}, {"reset_at": 5.0}, None, pendant.STOPPING),      # (its reset, until the banner)
                 ({"state": "Alarm"}, {"state": "running"}, None, pendant.ALARM),
                 ({"state": "Idle"}, {}, "scan", pendant.SCAN), ({"state": "Run"}, {}, "scan", pendant.SCAN),
                 ({"state": "Idle"}, {"state": "done"}, None, pendant.IDLE)]          # (a job over: not active)
        for now, job, activity, want in cases:
            m.now, m.activity = now, activity
            m.set_job(**job)
            self.say("REQ")
            got = struct.unpack("<10f", self.read(44)[4:])[4]
            self.assertEqual(got, want + pendant.MARK, (now, job, activity))
        self.assertEqual(pendant.SCREEN, ("IDLE", "PAUSED", "RUNNING", "BUSY", "ALARM", "STOPPING", "ERROR", "OFFLINE",
                                          "JOG", "HOMING", "SCAN", "MOVING", "DOOR", "LIMIT"))  # (the fixed getState())

    def test_a_req_is_answered_even_if_the_numbers_fail(self):
        """No answer would make the pendant give up on us: pendant_status() failing still gets a SYNC (status 3)."""
        self.handshake()
        with mock.patch.object(self.m, "pendant_status", side_effect=MachineError("no")):
            self.say("REQ")
            self.assertEqual(self.read(44), packet(status=3))
        with mock.patch.object(self.m, "pendant_status", side_effect=RuntimeError("bug")), \
                mock.patch.object(pendant.traceback, "print_exc") as printed:
            self.say("REQ", "REQ")
            self.assertEqual(self.read(88), packet(status=3) * 2)
        self.assertEqual(printed.call_count, 1)             # (a bug is printed once, not 4 times a second)
        self.say("REQ")
        self.assertEqual(self.read(44), packet_now(self.m))

    def test_nothing_is_answered_before_it_asks_to_connect(self):
        """Waiting: REQ and keys get nothing (a key from before may be old); the pendant then asks again."""
        self.p.connect(self.path)
        self.say("REQ", "KEY:5")
        self.assertTrue(self.quiet(0.4))
        self.assertEqual(self.m.calls, [])
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Jog X+ ignored: the pendant hasn't asked to connect yet "
                                                      "(it may be an old key)"))
        self.assertEqual(self.p.state()["state"], "waiting")
        self.say("CONREQ")
        self.assertEqual(self.read(7), b"CONACK\n")

    def test_no_req_for_a_while_is_waiting(self):
        with mock.patch.object(pendant, "REQ_TIMEOUT", 0.5):
            self.handshake()
            self.say("REQ")
            self.assertEqual(len(self.read(44)), 44)
            t0 = time.monotonic()
            self.wait(lambda: self.p.state()["state"] == "waiting", what="waiting")
            self.assertGreater(time.monotonic() - t0, 0.3)
            self.assertEqual(self.m.console[-1], ("info", f"Pendant: no word from it for 0.5 s (switched off, or out of "
                                                          f"range?). Still listening on {self.path}: it connects again "
                                                          "by itself."))
            self.assertEqual(self.m.events[-1]["pendant"]["state"], "waiting")
            self.assertEqual(self.p.state()["port"], self.path)  # (the port stays open)
            self.say("REQ", "KEY:5")
            self.assertTrue(self.quiet(0.3))
            self.assertEqual(self.m.calls, [])
            self.say("CONREQ")
            self.assertEqual(self.read(7), b"CONACK\n")
            self.wait(lambda: self.p.state()["state"] == "connected", what="connected again")

    def test_unreadable_lines_are_told_now_and_then(self):
        self.handshake()
        n = len(self.m.console)
        self.say("hello", "KEY:x", "KEY:", "\x00\xffREQ", "xKEY:5")
        self.say("REQ")                                     # (and the lines after them still work)
        self.assertEqual(len(self.read(44)), 44)
        self.assertEqual(self.m.console[n:], [("info", f"Pendant: unreadable line from {self.path}: 'hello'")])
        self.assertEqual(self.m.calls, [])

    def test_state_and_events(self):
        self.assertEqual(self.p.state(), {"port": None, "state": "off", "since": mock.ANY, "lastKey": None,
                                          "stopPause": None})
        self.handshake()
        self.key(5)
        st = self.p.state()
        self.assertEqual(set(st), {"port", "state", "since", "lastKey", "stopPause"})
        self.assertEqual((st["port"], st["state"]), (self.path, "connected"))
        self.assertLess(abs(st["since"] - time.time()), 5)
        self.assertEqual(st["lastKey"], {"key": 5, "name": "Jog X+", "ok": True, "text": "Pendant: Jog X+ 0.1 in",
                                         "t": mock.ANY})
        self.assertEqual(self.m.events[-1], {"type": "pendant", "pendant": st})
        self.assertEqual(self.settings.data, {"pendantPort": self.path})   # remembered for the next Connect


class Keys(RawPty):
    def setUp(self):
        super().setUp()
        self.handshake()

    def test_every_key(self):
        """Each key as the user's UGS maps it, with the shared jog step and speed; A keys and unused keys only a
        note. A SYNC follows each one."""
        jog = [(0, {"z": -0.1}, "Z-"), (3, {"z": 0.1}, "Z+"), (4, {"y": -0.1}, "Y-"), (5, {"x": 0.1}, "X+"),
               (6, {"y": 0.1}, "Y+"), (7, {"x": -0.1}, "X-")]
        want = [(k, ("jog", ({"step": step, "feed": 150.0},), AHEAD), f"Pendant: Jog {name} 0.1 in")
                for k, step, name in jog]
        want += [(8, ("set_jog_settings", (), {"step": 0.001}), "Pendant: Jog step 0.001 in"),
                 (9, ("set_jog_settings", (), {"step": 0.01}), "Pendant: Jog step 0.01 in"),
                 (10, ("set_jog_settings", (), {"step": 0.1}), "Pendant: Jog step 0.1 in"),
                 (11, ("set_jog_settings", (), {"step": 1.0}), "Pendant: Jog step 1 in"),
                 (13, ("zero", (), {"axes": "Z", "value": 0, "source": "pendant"}), "Pendant: Zero Z"),
                 (14, ("zero", (), {"axes": "Y", "value": 0, "source": "pendant"}), "Pendant: Zero Y"),
                 (15, ("zero", (), {"axes": "X", "value": 0, "source": "pendant"}), "Pendant: Zero X"),
                 (17, ("goto", (), {"to": "mz"}), "Pendant: Machine Z0 (G53 G0 Z0)"),
                 (18, ("goto", (), {"to": "my"}), "Pendant: Machine Y0 (G53 G0 Y0)"),
                 (19, ("goto", (), {"to": "mx"}), "Pendant: Machine X0 (G53 G0 X0)"),
                 (22, ("goto", (), {"to": "zero"}), "Pendant: Return to zero"),
                 (23, ("home", (), {"axes": ""}), "Pendant: Home all ($H)"),       # (in ALARM: see below)
                 (25, ("run", (), {}), "Pendant: Run t.nc (35 lines)"),
                 (26, ("pause_toggle", (), {}), "Pendant: Pause (feed hold)"),
                 (27, ("stop", (), {}), "Pendant: Stop: stopping the job (feed hold, then a reset once it stands "
                                        "still)"),
                 (1, None, "Pendant: A- ignored: this machine has no A axis"),
                 (2, None, "Pendant: A+ ignored: this machine has no A axis"),
                 (12, None, "Pendant: Zero A ignored: this machine has no A axis"),
                 (16, None, "Pendant: Home A ignored: this machine has no A axis"),
                 (20, None, "Pendant: key 20 does nothing"),
                 (21, None, "Pendant: key 21 does nothing"),
                 (24, None, "Pendant: key 24 does nothing"),
                 (99, None, "Pendant: key 99 does nothing")]
        for key, call, text in want:
            self.m.js["step"] = 0.1
            self.m.now["state"] = "Alarm" if key == 23 else "Idle"     # (Home only gets out of an ALARM)
            self.m.calls.clear()
            got = self.key(key)
            self.assertEqual(self.m.calls, [call] if call else [], key)
            self.assertEqual(self.m.console[-1], ("info", text), key)
            self.assertEqual(self.p.state()["lastKey"]["ok"], True if call else None, key)
            self.assertEqual(got, packet_now(self.m), key)
        self.assertTrue(self.quiet())

    def test_pause_and_stop_say_what_they_did(self):
        for did, text in (("hold", "Pendant: Pause (feed hold)"), ("resume", "Pendant: Resume"),
                          (None, "Pendant: Pause/Resume")):
            self.m.did = did
            self.key(26)
            self.assertEqual(self.m.console[-1], ("info", text))
        for stopped, text in (("scan", "scan stopped"), ("jog", "jog cancelled"),
                              ("motion", "stopping (feed hold, then a reset once it stands still)"),
                              (None, "nothing to stop")):
            self.m.stopped = stopped
            self.key(27)
            self.assertEqual(self.m.console[-1], ("info", "Pendant: Stop: " + text))
            self.assertIs(self.p.state()["lastKey"]["ok"], None if stopped is None else True)
        self.assertEqual([c[0] for c in self.m.calls], ["pause_toggle"] * 3 + ["stop"] * 4)

    def test_step_keys_set_the_shared_step(self):
        got = self.key(8)
        self.assertAlmostEqual(struct.unpack("<f", got[36:40])[0], 0.001, places=6)   # the pendant shows it at once
        self.key(5)
        got = self.key(11)
        self.assertEqual(struct.unpack("<f", got[36:40])[0], 1.0)
        self.key(7)
        self.assertEqual(self.m.calls, [("set_jog_settings", (), {"step": 0.001}),
                                        ("jog", ({"step": {"x": 0.001}, "feed": 150.0},), AHEAD),
                                        ("set_jog_settings", (), {"step": 1.0}),
                                        ("jog", ({"step": {"x": -1.0}, "feed": 150.0},), AHEAD)])
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Jog X- 1 in"))

    def test_a_refused_key_is_never_done_later(self):
        self.m.refuse["jog"] = "ALARM: Unlock or Home first"
        self.key(5)
        self.assertEqual(self.m.console[-1], ("error", "Pendant: Jog X+ refused: ALARM: Unlock or Home first"))
        self.assertIs(self.p.state()["lastKey"]["ok"], False)
        self.assertTrue(self.quiet(0.5))
        self.assertEqual(len(self.m.calls), 1)
        del self.m.refuse["jog"]
        self.key(3)
        self.assertEqual([c[1][0]["step"] for c in self.m.calls], [{"x": 0.1}, {"z": 0.1}])   # X+ never came back
        self.m.now = None                   # (not connected: the Machine's own reasons)
        for name, key, why in (("zero", 13, "Work offset not known yet; try again in a second"),
                               ("goto", 22, "A job is running"), ("home", 23, "Not connected"),
                               ("stop", 27, "Nothing to stop"), ("pause_toggle", 26, "Nothing to pause"),
                               ("set_jog_settings", 9, "bad step")):
            self.m.refuse[name] = why
            self.key(key)
            self.assertEqual(self.m.console[-1], ("error", f"Pendant: {pendant.key_name(key)} refused: {why}"))

    def test_refused_run_says_how_to_get_run_back(self):
        """The firmware sends Run once, then only after its Stop key: a refused Run says so."""
        self.m.refuse["run"] = "No program loaded"
        self.key(25)
        self.assertEqual(self.m.console[-1], ("error", "Pendant: Run refused: No program loaded. The pendant sends Run "
                                                       "again only after its Stop key"))

    def test_run_resumes_a_paused_job_or_a_hold(self):
        """Run is cycle start: a job paused (Feed Hold, M0) or FluidNC in Hold resumes, as the page's Resume."""
        self.m.set_job("paused")
        self.key(25)
        self.assertEqual(self.m.calls, [("realtime", ("resume",), {})])
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Run: Resume t.nc"))
        self.assertIs(self.p.state()["lastKey"]["ok"], True)
        self.m.set_job()
        self.m.now["state"] = "Hold"                        # (a move held, no job)
        self.key(27)                                        # (Stop first: the original firmware's Run flag off)
        self.m.calls.clear()
        self.key(25)
        self.assertEqual(self.m.calls, [("realtime", ("resume",), {})])
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Run: Resume (FluidNC was in Hold)"))
        self.m.refuse["realtime"] = "Stopping: FluidNC is being held, then reset"
        self.key(25)                                        # (the Machine's own refusals still count)
        self.assertEqual(self.m.console[-1][0], "error")
        self.assertIn("Pendant: Run refused: Stopping: FluidNC is being held, then reset", self.m.console[-1][1])

    def test_run_with_a_job_or_scan_running_does_nothing(self):
        """Nothing to do, and not refused: on the original firmware this press is what lets its Pause and Stop
        through for a job started on the page."""
        self.m.set_job("running")
        self.key(25)
        self.assertEqual(self.m.calls, [])
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Run: t.nc is running already (the pendant's Pause "
                                                      "and Stop work for it)"))
        self.assertIsNone(self.p.state()["lastKey"]["ok"])
        self.assertIs(self.p.state()["stopPause"], True)
        self.m.set_job()
        self.m.activity = "scan"
        self.key(27)
        self.m.calls.clear()
        self.key(25)
        self.assertEqual(self.m.calls, [])
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Run: a scan is running (the pendant's Stop stops it)"))

    def test_run_refused_while_stopping(self):
        """The safe stop's own Hold (or its reset) never gets a '~' from Run."""
        self.m.now["state"] = "Hold"
        self.m.set_job(halt={"at": 1.0})
        self.key(25)
        self.assertEqual(self.m.calls, [])
        self.assertEqual(self.m.console[-1], ("error", "Pendant: Run refused: Stopping: FluidNC is being held, then "
                                                       "reset. The pendant sends Run again only after its Stop key"))
        self.key(27)
        self.m.calls.clear()
        self.m.set_job("stopping")                          # (no halt: the job's own state says it too)
        self.key(25)
        self.assertEqual(self.m.calls, [])
        self.assertIn("Pendant: Run refused: The job is being stopped", self.m.console[-1][1])

    def test_no_resume_from_the_pendant_after_an_error(self):
        """A job an error paused is resumed on the page, where the error is told: Run and Pause refuse, Stop
        stops."""
        self.m.now["state"] = "Hold"
        self.m.set_job("paused", error=ERR)
        self.key(25)
        self.assertEqual(self.m.console[-1], ("error", f"Pendant: Run refused: {ON_ERR}. The pendant sends Run again "
                                                       "only after its Stop key"))
        self.key(26)
        self.assertEqual(self.m.console[-1], ("error", f"Pendant: Pause/Resume refused: {ON_ERR}"))
        self.assertEqual(self.m.calls, [])
        self.key(27)
        self.assertEqual(self.m.calls, [("stop", (), {})])  # (an M0 or a Feed Hold, no error: see the next test)

    def test_pause_resumes_only_as_the_original_firmwares_toggle(self):
        """The original firmware's Pause toggles: after its own hold the next Pause resumes, and so does one
        pressed once the screen has shown PAUSED for SEEN. But FluidNC held by itself (an M0, its button, the page)
        while the screen still showed RUNNING: the press was meant to hold, so it holds again ('!' in Hold changes
        nothing). Nor does it resume a hold whose Resume waits for FluidNC to stop: it cancels that Resume."""
        def last():
            return self.m.calls[-1], self.m.console[-1][1]
        self.m.set_job("running")
        self.key(26)                                        # holds (the SYNC after it says PAUSED)
        self.m.set_job("paused")
        self.m.did = "resume"
        self.key(26)                                        # its own hold: the toggle back
        self.assertEqual(last(), (("pause_toggle", (), {}), "Pendant: Resume"))
        self.m.set_job("running")
        self.say("REQ")                                     # RUNNING on its screen
        self.read(44)
        self.m.set_job("paused")                            # FluidNC held by itself (the screen shows it soon)
        self.key(26)
        self.assertEqual(last(), (("realtime", ("hold",), {}),
                                  "Pendant: Pause (feed hold): t.nc was held already (the pendant didn't show it yet); "
                                  "Pause again resumes it"))
        self.key(26)                                        # (and now the toggle back)
        self.assertEqual(last(), (("pause_toggle", (), {}), "Pendant: Resume"))
        self.m.set_job("running")
        self.say("REQ")
        self.read(44)
        self.m.set_job("paused")
        self.say("REQ")                                     # PAUSED on its screen, just now: not seen yet
        self.read(44)
        self.key(26)
        self.assertEqual(self.m.calls[-1], ("realtime", ("hold",), {}))
        self.m.set_job("running")
        self.say("REQ")
        self.read(44)
        self.m.set_job("paused")
        self.say("REQ")
        self.read(44)
        time.sleep(pendant.SEEN + 0.05)                     # shown long enough: the toggle
        self.key(26)
        self.assertEqual(self.m.calls[-1], ("pause_toggle", (), {}))
        self.m.set_job("paused", resume_at=1005.0)          # Run (or the page) asked to resume while it slows down
        self.key(26)
        self.assertEqual(last(), (("realtime", ("hold",), {}), "Pendant: Pause (feed hold): the Resume waiting for "
                                                             "FluidNC to stop is cancelled"))
        self.m.set_job()
        self.m.now["state"] = "Hold"                        # (a move held, no job: the same rules)
        self.say("REQ")
        self.read(44)
        time.sleep(pendant.SEEN + 0.05)
        self.key(26)
        self.assertEqual(self.m.calls[-1], ("pause_toggle", (), {}))

    def test_the_fixed_firmwares_pause_only_holds(self):
        """FW:2: its Pause never resumes (it isn't even sent in PAUSED: Run resumes), whatever the screen showed."""
        self.say("FW:2")
        self.wait(lambda: self.p.fixed, what="the fixed firmware")
        self.m.set_job("paused")
        self.say("REQ")
        self.read(44)
        time.sleep(pendant.SEEN + 0.05)
        self.key(26)
        self.assertEqual((self.m.calls, self.m.console[-1][1]),
                         ([("realtime", ("hold",), {})], "Pendant: Pause (feed hold): t.nc was held already; the "
                                                         "pendant's Run resumes it"))
        self.m.set_job("running")
        self.key(26)
        self.assertEqual(self.m.calls[-1], ("pause_toggle", (), {}))    # (a job running: it holds)

    def test_no_pause_into_the_stop_or_its_reset(self):
        """The safe stop's Hold, its reset until the banner, and the moment after it (a report from before the reset
        still says Hold:0): a '~' then would let a move run on, or a line left from before the reset."""
        self.m.now["state"] = "Hold"
        for link in ({"halt": {"at": 1.0}}, {"reset_at": 5.0}, {"after_reset": 6.0, "reset_clean": False}):
            self.m.set_job("paused", **link)
            self.key(26)
            self.assertEqual(self.m.console[-1], ("error", f"Pendant: Pause/Resume refused: {machine.NO_RESUME}"), link)
        self.assertEqual(self.m.calls, [])
        self.m.set_job("paused", after_reset=6.0, reset_clean=True)     # (clean: the stop is over)
        self.key(26)
        self.assertEqual(self.m.calls, [("realtime", ("hold",), {})])

    def test_stop_in_a_critical_alarm_resets(self):
        """A hard or soft limit: only a Soft Reset gets out of it, and nothing moves. Stop resets first, so no '!'
        goes even if Machine.stop() would hold (a '!' there lets FluidNC out of the alarm unhomed)."""
        self.m.now["state"] = "Alarm"
        self.m.stopped = None                               # (Machine.stop(): nothing moving)
        self.m.set_job(alarm={"code": 1, "critical": True})
        got = self.key(27)
        self.assertEqual(self.m.calls, [("realtime", ("reset",), {}), ("stop", (), {})])
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Stop: " + pendant.LIMIT_RESET))
        self.assertEqual(struct.unpack("<10f", got[4:])[4], pendant.LIMIT + pendant.MARK)  # (the mock stays so)
        self.m.calls.clear()
        self.m.set_job(alarm={"code": 14, "critical": False})       # (an ordinary alarm: Stop as before)
        self.key(27)
        self.assertEqual((self.m.calls, self.m.console[-1][1]), ([("stop", (), {})], "Pendant: Stop: nothing to stop"))
        self.m.calls.clear()
        self.m.set_job(alarm={"code": 1, "critical": True}, reset_at=9.0)    # (its reset under way: not again)
        self.key(27)
        self.assertEqual(self.m.calls, [("stop", (), {})])

    def test_stop_or_pause_while_a_key_is_being_done_comes_right_after_it(self):
        """Two lines read at once (the Bluetooth link bunches them): a jog key, then Stop. Stop isn't refused: it is
        done right after the jog key, never before it. A Pause likewise; one of them at most waits (a Stop replaces
        a Pause), and a Stop while a Stop is being done is that one."""
        self.m.block["jog"] = threading.Event()
        try:
            self.say("KEY:5", "KEY:26", "KEY:27", "KEY:26", "KEY:3", "KEY:27")
            self.wait(lambda: self.m.calls, what="the jog")
            self.wait(lambda: len(self.m.console) >= 5, what="the refusals")
            self.assertEqual([c[0] for c in self.m.calls], ["jog"])         # (the rest waits for it)
        finally:
            self.m.block["jog"].set()
        self.wait(lambda: len(self.m.calls) == 2, what="the stop")
        self.assertEqual(len(self.read(5 * 44)), 5 * 44)    # (a SYNC for each key: 3 refused, the jog, the Stop)
        self.assertEqual([c[0] for c in self.m.calls], ["jog", "stop"])
        texts = [c[1] for c in self.m.console]
        for text in ("Pendant: Pause/Resume dropped: Stop came right after it",
                     "Pendant: Pause/Resume refused: still doing Jog X+", "Pendant: Jog Z+ refused: still doing Jog X+",
                     "Pendant: Stop: being done already", "Pendant: Jog X+ 0.1 in"):
            self.assertIn(text, texts)
        self.assertEqual(texts[-1], "Pendant: Stop: stopping the job (feed hold, then a reset once it stands still)")
        self.m.block["stop"] = threading.Event()
        try:
            self.say("KEY:27", "KEY:26")                    # (a Pause after a Stop: refused, never done later)
            self.wait(lambda: self.said(self.m, "Pendant: Pause/Resume refused: still doing Stop"), what="refused")
        finally:
            self.m.block["stop"].set()
        self.wait(lambda: [c[0] for c in self.m.calls] == ["jog", "stop", "stop"], what="the second stop")
        time.sleep(0.2)
        self.assertEqual([c[0] for c in self.m.calls], ["jog", "stop", "stop"])

    def test_a_key_while_one_is_being_done_is_refused(self):
        """Stop can take seconds (the safe stop): REQs are still answered meanwhile, and a key pressed then is
        refused, never done when Stop is over."""
        self.m.block["stop"] = threading.Event()
        try:
            self.say("KEY:27")
            self.wait(lambda: self.m.calls, what="stop")
            self.say("REQ")
            self.assertEqual(len(self.read(44, 0.5)), 44)     # the reader isn't held up
            got = self.key(5)
            self.assertEqual(self.m.console[-1], ("error", "Pendant: Jog X+ refused: still doing Stop"))
            self.assertEqual(got, packet_now(self.m))
        finally:
            self.m.block["stop"].set()
        self.assertEqual(len(self.read(44)), 44)              # Stop's SYNC, once it is done
        self.assertIn("Pendant: Stop: stopping the job", self.m.console[-1][1])
        self.assertEqual(self.m.calls, [("stop", (), {})])
        self.key(5)
        self.assertEqual([c[0] for c in self.m.calls], ["stop", "jog"])

    def test_a_bug_in_a_key_is_told_and_the_pendant_goes_on(self):
        with mock.patch.object(self.m, "jog_settings", return_value={"feed": 150}), \
                mock.patch.object(pendant.traceback, "print_exc"):
            self.key(5)
        self.assertEqual(self.m.console[-1], ("error", "Pendant: Jog X+ failed: internal error in FluidSender "
                                                       "(KeyError('step'))"))
        self.key(5)
        self.assertEqual(self.m.console[-1], ("info", "Pendant: Jog X+ 0.1 in"))


class FirmwareRule(RawPty):
    """The original firmware sends Run only while its own Run flag is off (and sets it), Pause and Stop only while
    it is on (Stop clears it); the fixed one sends them by the status it shows. pendant.py follows the flag through
    the keys, and knows the fixed firmware by a key the original would not send."""

    def setUp(self):
        super().setUp()
        self.handshake()

    def stop_pause(self):
        return self.p.state()["stopPause"]

    def test_the_original_firmwares_flag(self):
        self.assertIsNone(self.stop_pause())                # (not known: its flag may be on from before)
        for key, flag in ((25, True), (26, True), (26, True), (27, False), (25, True), (27, False)):
            self.key(key)
            self.assertIs(self.stop_pause(), flag, key)
        self.assertFalse(self.said(self.m, "the fixed firmware"))
        self.assertEqual(self.m.events[-1]["pendant"]["stopPause"], False)
        self.say("CONREQ")                                  # it may have been switched off and on: not known
        self.assertEqual(self.read(7), b"CONACK\n")
        self.wait(lambda: self.m.events[-1]["pendant"]["stopPause"] is None, what="the event")
        self.assertIsNone(self.stop_pause())
        self.key(27)                                        # (after a CONREQ, Stop is no news: its flag was on)
        self.key(27)                                        # Stop after Stop: never from the original firmware
        self.assertEqual(self.said(self.m, "the fixed firmware"), [("info", pendant.FIXED)])
        self.assertIs(self.stop_pause(), True)

    def test_the_fixed_firmware_and_run(self):
        self.m.refuse["run"] = "No program loaded"
        self.key(25)
        self.assertTrue(self.m.console[-1][1].endswith(pendant.RUN_AGAIN))
        self.key(25)                                        # Run twice without Stop: the fixed firmware
        self.assertEqual(self.m.console[-2:], [("info", pendant.FIXED),
                                                ("error", "Pendant: Run refused: No program loaded")])
        self.key(27)
        self.key(27)
        self.key(25)
        self.assertEqual(len(self.said(self.m, "the fixed firmware")), 1)     # (said once)
        self.assertIs(self.stop_pause(), True)
        self.p.disconnect()                                 # another connect: another pendant, maybe
        self.handshake()
        self.assertIsNone(self.stop_pause())

    def test_the_fixed_firmware_says_which_it_is(self):
        """FW:2 after CONACK: the fixed firmware, known at once (said once, also after it connects again), so the
        original firmware's notes never come (RUN_AGAIN, AGAIN) and the page's Stop/Pause note neither. FW:1 or
        less is no news; anything else is an unreadable line."""
        self.say("FW:1")
        self.say("REQ")
        self.read(44)
        self.assertIsNone(self.stop_pause())
        self.say("FW:2")
        self.wait(lambda: self.stop_pause() is True, what="the fixed firmware")
        self.assertEqual(self.said(self.m, "the fixed firmware"), [("info", pendant.FIXED)])
        self.assertIs(self.m.events[-1]["pendant"]["stopPause"], True)
        self.say("CONREQ")                                  # (it connects again: FW:2 again, said once)
        self.assertEqual(self.read(7), b"CONACK\n")
        self.say("FW:2", "FW:x")
        self.say("REQ")
        self.read(44)
        self.assertEqual(len(self.said(self.m, "the fixed firmware")), 1)
        self.assertEqual(self.said(self.m, "unreadable line"), [("info", f"Pendant: unreadable line from {self.path}: "
                                                                         "'FW:x'")])
        self.m.refuse["run"] = "No program loaded"
        self.key(25)
        self.assertEqual(self.m.console[-1], ("error", "Pendant: Run refused: No program loaded"))
        self.m.set_job("running")
        self.key(25)                                        # (Run during a job: the original's flag doesn't count)
        self.m.set_job("done")
        self.say("REQ")
        self.read(44)
        self.assertFalse(self.said(self.m, "the job is over"))
        self.assertIs(self.stop_pause(), True)

    def test_the_end_of_a_job_says_how_to_run_again(self):
        """Original firmware, its Run flag on (its Run or Pause key pressed): once the job is over, the console says
        that Run needs its Stop key first. Not after its Stop key, nor with the fixed firmware."""
        self.m.set_job("running")
        self.key(25)                                        # (Run during a page job: its flag is on now)
        self.m.set_job("done")
        self.say("REQ")
        self.read(44)
        self.assertEqual(self.m.console[-1], ("info", pendant.AGAIN))
        self.say("REQ")
        self.read(44)
        self.assertEqual(len(self.said(self.m, "the job is over")), 1)
        self.m.set_job("running")
        self.key(27)                                        # Stop: its flag off, Run works again
        self.m.set_job()
        self.say("REQ")
        self.read(44)
        self.assertEqual(len(self.said(self.m, "the job is over")), 1)
        self.key(27)                                        # (fixed now: Stop after Stop)
        self.key(26)
        self.m.set_job("running")
        self.say("REQ")
        self.read(44)
        self.m.set_job()
        self.say("REQ")
        self.read(44)
        self.assertEqual(len(self.said(self.m, "the job is over")), 1)


class Disconnect(RawPty):
    def test_disconnect_closes_our_fd(self):
        self.handshake()
        n = len(self.m.console)
        self.assertEqual(self.p.disconnect(), {})
        self.assertEqual(self.p.state(), {"port": None, "state": "off", "since": mock.ANY, "lastKey": None,
                                          "stopPause": None})
        self.assertEqual(self.m.console[n:], [("info", f"Pendant: disconnected from {self.path}")])
        self.assertEqual(self.m.events[-1]["pendant"]["state"], "off")
        os.close(os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))   # not held, not exclusive any more
        self.p.disconnect()
        self.assertEqual(len(self.m.console), n + 1)          # (twice is fine)
        self.say("CONREQ")
        self.assertTrue(self.quiet())
        self.handshake()                                    # and it can connect again

    def test_port_gone(self):
        self.handshake()
        os.close(self.master)
        self.master = None
        self.wait(lambda: self.p.state()["state"] == "off", what="off")
        self.assertEqual(self.m.console[-1], ("error", f"Pendant: lost {self.path} (the port was closed). Bluetooth "
                                                       "link dropped? Connect it again."))

    def test_disconnect_while_a_key_is_being_done(self):
        self.handshake()
        self.m.block["stop"] = threading.Event()
        try:
            self.say("KEY:27")
            self.wait(lambda: self.m.calls, what="stop")
            self.p.disconnect()
            self.assertEqual(self.p.state()["state"], "off")
        finally:
            self.m.block["stop"].set()
        self.wait(lambda: self.said(self.m, "Pendant: Stop"), what="Stop done")
        self.assertTrue(self.quiet())                       # (its SYNC goes nowhere)


class PortRules(RawPty):
    def test_only_test_ports_in_test_mode(self):
        for port in ("/dev/rfcomm97", "/dev/ttyUSB97"):     # (ports that don't exist, just in case)
            with self.assertRaises(MachineError) as cm:
                self.p.connect(port)
            self.assertEqual(str(cm.exception), f"{port} is not one of the test ports this server may open")
        with self.assertRaises(MachineError):
            self.p.connect("  ")
        self.assertEqual(self.p.state()["state"], "off")

    def test_port_kinds(self):
        """Not test ports (our own pty, its kind made up): Bluetooth is the pendant's; a USB ACM port (the
        probe's kind) needs force; a USB serial port (FluidNC's kind) never; anything else is refused."""
        self.m.extra, self.m.open_only_extra = [], False
        with self.assertRaises(MachineError) as cm:
            self.p.connect(self.path)                       # a pty that isn't a test port
        self.assertEqual(str(cm.exception), f"{self.path} is not a Bluetooth serial port like the pendant's "
                                            "(/dev/rfcomm...)")
        with mock.patch.object(pendant, "port_kind", return_value="usb-serial"):
            for force in (False, True):
                with self.assertRaises(MachineError) as cm:
                    self.p.connect(self.path, force)
                self.assertIn("is a USB serial port like FluidNC's, not the pendant", str(cm.exception))
        with mock.patch.object(pendant, "port_kind", return_value="usb-acm"):
            with self.assertRaises(MachineError) as cm:
                self.p.connect(self.path)
            self.assertIn("looks like the Z probe (Arduino Leonardo), not the pendant", str(cm.exception))
            self.assertEqual(self.p.state()["state"], "off")
            self.p.connect(self.path, force=True)
        self.assertEqual(self.p.state()["state"], "waiting")
        self.p.disconnect()
        with mock.patch.object(pendant, "port_kind", return_value="bluetooth"):
            self.p.connect(self.path)
        self.assertEqual(self.p.state()["state"], "waiting")

    def test_not_the_fluidnc_port(self):
        self.m.link = SimpleNamespace(path=self.path)
        with self.assertRaises(MachineError) as cm:
            self.p.connect(self.path)
        self.assertEqual((str(cm.exception), cm.exception.status), (f"{self.path} is the FluidNC port", 409))

    def test_once(self):
        self.p.connect(self.path)
        with self.assertRaises(MachineError) as cm:
            self.p.connect(self.path)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("already connected", str(cm.exception))

    def test_ports_for_the_dialog(self):
        """Blueman's watcher doesn't make a port busy; the default: the last pendant port, else /dev/rfcomm1, else
        the first Bluetooth port. (Paths made up, never opened; the holders are mocked.)"""
        def port(path, kind, used=None):
            return {"path": path, "label": "", "kind": kind, "vid": None, "pid": None, "product": None, "inUseBy": used}
        self.m.listed = [port("/dev/ttyUSB7", "usb-serial"),
                         port("/dev/rfcomm8", "bluetooth", "blueman-rfcomm-watcher (pid 5)"),
                         port("/dev/rfcomm9", "bluetooth", "UGS (java, pid 6)")]
        holders = {"/dev/rfcomm8": [5], "/dev/rfcomm9": [6, 5]}
        names = {5: "blueman-rfcomm-watcher (pid 5)", 6: "UGS (java, pid 6)"}
        with mock.patch.object(pendant, "scan_holders", return_value=holders), \
                mock.patch.object(pendant, "process_name", side_effect=names.get):
            data = self.p.ports()
            self.assertEqual([p["inUseBy"] for p in data["ports"]], [None, None, "UGS (java, pid 6)"])
            self.assertEqual(data["default"], "/dev/rfcomm8")
            self.settings.data["pendantPort"] = "/dev/rfcomm9"
            self.assertEqual(self.p.ports()["default"], "/dev/rfcomm9")
            self.m.listed.append(port("/dev/rfcomm1", "bluetooth"))
            self.settings.data["pendantPort"] = "/dev/rfcomm4"     # (not listed now)
            self.assertEqual(self.p.ports()["default"], "/dev/rfcomm1")
            self.m.listed = [port("/dev/ttyUSB7", "usb-serial")]
            self.assertIsNone(self.p.ports()["default"])


class Holders(RawPty):
    """Child processes hold our own pty open: another program is refused, Blueman's rfcomm watcher is not."""

    def setUp(self):
        super().setUp()
        self.children = []
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        for child in self.children:
            child.kill()
            child.wait()
            child.stdout.close()
        super().tearDown()
        self.dir.cleanup()

    def hold(self, name=None):
        """A process that has the pty open (read only, like Blueman's watcher), as python3 or as a script with
        that name, like /usr/libexec/blueman-rfcomm-watcher (comm is then its first 15 letters)."""
        code = ("import os, sys, time\nos.open(sys.argv[1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)\n"
                "print('open', flush=True)\ntime.sleep(30)\n")
        if name:
            script = os.path.join(self.dir.name, name)
            with open(script, "w") as f:
                f.write(f"#!{sys.executable}\n{code}")
            os.chmod(script, 0o755)
            args = [script, self.path]
        else:
            args = [sys.executable, "-c", code, self.path]
        child = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "open")
        return child

    def holds(self, child):
        fds = f"/proc/{child.pid}/fd"
        return any(os.readlink(os.path.join(fds, fd)) == self.path for fd in os.listdir(fds))

    def test_another_program_is_refused(self):
        child = self.hold()
        with self.assertRaises(MachineError) as cm:
            self.p.connect(self.path)
        self.assertEqual(cm.exception.status, 409)
        self.assertRegex(str(cm.exception), rf"^Port {self.path} is in use by python3[.\d]* \(pid {child.pid}\)\. "
                                            r"Disconnect it there first\.$")
        self.assertEqual(self.p.state()["state"], "off")

    def test_blueman_watcher_is_fine_and_left_alone(self):
        watcher = self.hold("blueman-rfcomm-watcher")
        self.assertEqual(machine.process_name(watcher.pid), f"blueman-rfcomm-watcher (pid {watcher.pid})")
        self.handshake()
        self.say("REQ")
        self.assertEqual(len(self.read(44)), 44)
        self.p.disconnect()
        self.assertIsNone(watcher.poll())                   # still running, its fd still open
        self.assertTrue(self.holds(watcher))
        os.close(os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))   # ours closed, not exclusive

    def test_blueman_and_another_program(self):
        self.hold("blueman-rfcomm-watcher")
        child = self.hold()
        with self.assertRaises(MachineError) as cm:
            self.p.connect(self.path)
        self.assertRegex(str(cm.exception), rf"in use by python3[.\d]* \(pid {child.pid}\)\. Disconnect")
        self.assertNotIn("blueman", str(cm.exception))

    def test_names(self):
        """The watcher by its full name, or by the 15 letters the kernel keeps (UGS is someone else)."""
        names = {5: "blueman-rfcomm-watcher (pid 5)", 6: "blueman-rfcomm- (pid 6)", 7: "UGS (java, pid 7)",
                 8: "blueman-applet (pid 8)"}
        with mock.patch.object(pendant, "process_name", side_effect=names.get), \
                mock.patch.object(machine, "process_name", side_effect=names.get):     # (holders_text's)
            self.assertEqual([pendant.is_blueman_watcher(p) for p in (5, 6, 7, 8)], [True, True, False, False])
            with mock.patch.object(pendant, "scan_holders", return_value={"/dev/rfcomm8": [5, 6, os.getpid()]}):
                self.assertEqual(pendant.other_programs("/dev/rfcomm8"), "")
            with mock.patch.object(pendant, "scan_holders", return_value={"/dev/rfcomm8": [5, 7]}):
                self.assertEqual(pendant.other_programs("/dev/rfcomm8"), "UGS (java, pid 7)")


class MachineApi(unittest.TestCase):
    def test_the_calls_fit_machine_py(self):
        """Every key against the real Machine's signatures (an autospec mock: a call that doesn't fit is a
        TypeError). Skipped until machine.py has the stage-3 calls."""
        missing = [name for name in CALLS if not hasattr(machine.Machine, name)]
        if missing:
            self.skipTest(f"machine.py has no {', '.join(missing)} yet")
        m = mock.create_autospec(machine.Machine, instance=True)
        m.lock = threading.RLock()
        m.jog_settings.return_value = {"step": 0.1, "feed": 150.0}
        m.pendant_status.return_value = {"x": 0, "y": 0, "z": 0, "status": 0, "feed": 150, "line": 0, "total": 0,
                                         "step": 0.1, "live": 0}
        m.status_now.return_value = {"state": "Alarm", "wpos": [0, 0, 1]}  # (Home: only in ALARM)
        p = pendant.PendantLink(m, None)
        for key in range(28):
            ok, text = p._action(key)
            self.assertIsNot(ok, False, text)
        self.assertEqual(set(CALLS) - {"pendant_status"} - {c[0] for c in m.method_calls}, set())
        self.assertEqual(len(pendant.sync_packet(m.pendant_status())), 44)     # (the SYNC's call, no arguments)
        self.assertEqual(p._numbers()["status"], pendant.ALARM)
        m.status_now.return_value = {"state": "Hold", "wpos": [0, 0, 1]}
        self.assertEqual(p._action(25), (True, "Pendant: Run: Resume (FluidNC was in Hold)"))
        m.realtime.assert_called_once_with("resume")
        m.check_port("/dev/pts/0")                          # and what connect(), ports() and the events use
        m.ports()
        m.emit({"type": "pendant", "pendant": None})
        m.log("info", "text")
        real = machine.Machine()
        self.assertEqual([hasattr(real, a) for a in ("lock", "extra", "open_only_extra", "link")], [True] * 4)


# ------------------------------------------------------------------ fake_pendant.py plays the pendant

class FakePendant(Waits):
    """tools/fake_pendant.py on its pty: the firmware's handshake, timing, double-tap keys and Run flag."""

    def start(self, *args):
        fd, self.logpath = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.fake = subprocess.Popen([sys.executable, FAKE, "--log", self.logpath, "--boot-delay", "0", *args],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.path = self.fake.stdout.readline().strip()
        self.assertTrue(self.path.startswith("/dev/pts/"), self.path)
        self.m = MockMachine([self.path])
        self.p = pendant.PendantLink(self.m, MockSettings())
        self.p.connect(self.path)

    def tearDown(self):
        fake, self.fake = getattr(self, "fake", None), None
        if fake is None:
            return
        self.p.disconnect()
        fake.kill()
        fake.wait()
        fake.stdin.close()
        fake.stdout.close()
        os.remove(self.logpath)

    def ctl(self, text):
        self.fake.stdin.write(text + "\n")
        self.fake.stdin.flush()

    def log(self, ev=None):
        out = []
        with open(self.logpath) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass                    # (a line still being written)
        return [e for e in out if ev is None or e["ev"] == ev]

    def connected(self):
        self.wait(lambda: self.p.state()["state"] == "connected", 3.0, "connected")
        self.wait(lambda: self.log("sync"), 2.0, "the first SYNC")

    def jogs(self):
        return [c for c in self.m.calls if c[0] == "jog"]

    def test_handshake_and_req_rhythm(self):
        self.start()
        self.connected()
        self.assertEqual(len(self.log("conack")), 1)
        n = len(self.log("sync"))
        time.sleep(1.5)
        self.assertGreaterEqual(len(self.log("sync")) - n, 4)   # REQ every 250 ms, each answered
        self.m.set_job("running")                               # a job runs: it asks every 1 s
        time.sleep(1.2)
        n = len(self.log("sync"))
        time.sleep(3.0)
        self.assertIn(len(self.log("sync")) - n, (2, 3, 4))
        self.assertEqual(self.log("bad") + self.log("short") + self.log("timeout"), [])
        last = self.log("sync")[-1]
        self.assertEqual((last["x"], last["y"], last["z"], last["status"], last["feed"]), (1.5, -2.25, 0.125, 2, 150))
        self.assertEqual(last["shown"], "RUN")                  # (the original firmware's name for 2)
        self.assertAlmostEqual(last["step"], 0.1, places=6)

    def test_keys_through_the_firmware(self):
        self.start()
        self.connected()
        self.ctl("key 5")
        self.wait(self.jogs, what="the jog")
        self.ctl("key 13")                                      # double-tap: the first press only arms it
        self.wait(lambda: [e for e in self.log("key") if e["key"] == 13], what="key 13")
        time.sleep(0.4)
        self.assertFalse([c for c in self.m.calls if c[0] == "zero"])
        self.ctl("key 13")
        self.wait(lambda: [c for c in self.m.calls if c[0] == "zero"], what="the zero")
        self.m.now["state"] = "Alarm"                           # (Home only gets out of an ALARM)
        self.ctl("key 23 double")
        self.wait(lambda: [c for c in self.m.calls if c[0] == "home"], what="home")
        self.ctl("key 26")                                      # Pause before Run on the pendant: not even sent
        self.ctl("key 25")
        self.wait(lambda: [c for c in self.m.calls if c[0] == "run"], what="run")
        self.ctl("key 25")                                      # Run again before Stop: not sent
        self.ctl("key 26")
        self.wait(lambda: [c for c in self.m.calls if c[0] == "pause_toggle"], what="pause")
        self.ctl("key 27")
        self.wait(lambda: [c for c in self.m.calls if c[0] == "stop"], what="stop")
        self.ctl("key 25")
        self.wait(lambda: len([c for c in self.m.calls if c[0] == "run"]) == 2, what="run after stop")
        self.assertEqual([c[0] for c in self.m.calls], ["jog", "zero", "home", "run", "pause_toggle", "stop", "run"])
        self.assertEqual([e["line"] for e in self.log("sent") if e["line"].startswith("KEY")],
                         ["KEY:5", "KEY:13", "KEY:23", "KEY:25", "KEY:26", "KEY:27", "KEY:25"])
        self.m.calls.clear()
        self.ctl("hold 4 1.5")                                  # held: again every 600 ms
        self.wait(lambda: len(self.jogs()) == 3, what="3 jogs")
        time.sleep(1.0)
        self.assertEqual(self.jogs(), [("jog", ({"step": {"y": -0.1}, "feed": 150.0},), AHEAD)] * 3)
        self.assertEqual(self.log("bad") + self.log("short") + self.log("timeout"), [])

    def test_it_connects_again_after_its_timeout(self):
        """It hears nothing for 3 s: back to CONREQ (the first CONACK is lost too), then connected again."""
        self.start()
        self.connected()
        self.ctl("deaf 3.5")
        self.wait(lambda: self.log("timeout"), 5.0, "its timeout")
        self.wait(lambda: len(self.log("connected")) == 2, 4.0, "connected again")
        self.wait(lambda: self.said(self.m, "Pendant: connected again"), what="the note")
        n = len(self.log("sync"))
        self.wait(lambda: len(self.log("sync")) > n + 2, what="SYNCs again")
        self.assertEqual([e["pendant"]["state"] for e in self.m.events], ["waiting", "connected"])
        self.assertEqual(self.log("bad") + self.log("short"), [])
        self.ctl("key 5")
        self.wait(self.jogs, what="a key after it")

    def test_switched_off_and_on(self):
        with mock.patch.object(pendant, "REQ_TIMEOUT", 1.0):
            self.start()
            self.connected()
            self.ctl("off")
            self.wait(lambda: self.p.state()["state"] == "waiting", 3.0, "waiting")
            self.assertIn("Pendant: no word from it for 1 s", self.m.console[-1][1])
            self.ctl("on")
            self.wait(lambda: self.p.state()["state"] == "connected", 3.0, "connected again")
            self.assertEqual(self.m.console[-1], ("info", f"Pendant: connected on {self.path}"))
            self.assertEqual([e["pendant"]["state"] for e in self.m.events], ["waiting", "connected", "waiting",
                                                                              "connected"])

    def test_controls_from_a_fifo_or_a_file(self):
        """--control: a FIFO it makes, or a file it follows; keys work the same as from stdin."""
        with tempfile.TemporaryDirectory() as d:
            fifo, text = os.path.join(d, "keys.fifo"), os.path.join(d, "keys.txt")
            with open(text, "w") as f:
                f.write("key 7\n")                              # read from its start: before it is connected
            for path, jog in ((fifo, {"x": 0.1}), (text, {"y": 0.1})):
                self.start("--control", path)
                self.connected()
                with open(path, "a") as f:
                    f.write("key 5\n" if path == fifo else "key 6\n")
                self.wait(self.jogs, what="the jog")
                time.sleep(0.3)
                self.assertEqual([c[1][0]["step"] for c in self.jogs()], [jog])
                if path == text:                                # (keys aren't even scanned while connecting)
                    self.assertEqual(self.log("key")[0], {"t": mock.ANY, "ev": "key", "key": 7, "sent": False,
                                                          "why": "not connected"})
                self.tearDown()

    def test_every_conreq_is_answered(self):
        self.start("--no-ack-check")
        self.wait(lambda: len(self.log("conack")) >= 3, 4.5, "3 CONACKs")
        asked = [e["t"] for e in self.log("sent") if e["line"] == "CONREQ"]
        answered = [e["t"] for e in self.log("conack")]
        self.assertTrue(all(not e["accepted"] for e in self.log("conack")))
        self.assertEqual(self.log("line"), [])                  # nothing else came
        for t in answered:                                      # each right after a CONREQ
            self.assertTrue(any(0 <= t - a < 0.2 for a in asked), (t, asked))
        self.assertEqual(self.p.state()["state"], "connected")

    def keys_sent(self):
        return [e["key"] for e in self.log("key") if e["sent"]]

    def synced(self, **want):
        """Waits for a SYNC with those values (one made after the change)."""
        return self.wait(lambda: [e for e in self.log("sync") if all(e[k] == v for k, v in want.items())], 3.0,
                         f"a SYNC with {want}")[-1]

    def test_fixed_firmware_run_pause_and_stop(self):
        """By the status in the last SYNC: Stop always goes, Pause unless it says IDLE, Run only in IDLE with a job
        loaded (in PAUSED, Run resumes too: FluidSender's status)."""
        self.start("--firmware", "fixed")
        self.connected()
        self.assertEqual(self.log("sync")[-1]["shown"], "IDLE")
        for key in (26, 27, 25):                                # IDLE, no job loaded (total 0)
            self.ctl(f"key {key}")
        self.wait(lambda: [e for e in self.log("key") if e["key"] == 25], what="key 25")
        self.assertEqual([(e["key"], e.get("why")) for e in self.log("key")],
                         [(26, "PAUSE:nothing running"), (27, None), (25, "RUN: no job loaded")])
        self.m.st["total"] = 35                                 # (a program loaded)
        self.synced(total=35)
        self.ctl("key 25")
        self.wait(lambda: [c for c in self.m.calls if c[0] == "run"], what="run")
        self.m.set_job("running")
        self.assertEqual(self.synced(status=2)["shown"], "RUNNING")
        self.ctl("key 25")                                      # running: not sent
        self.ctl("key 26")
        self.wait(lambda: [c for c in self.m.calls if c[0] == "pause_toggle"], what="the pause")
        self.m.set_job("paused")
        self.assertEqual(self.synced(status=1)["shown"], "PAUSED")
        self.ctl("key 25")                                      # paused: Run resumes (FluidSender's status)
        self.wait(lambda: [c for c in self.m.calls if c[0] == "realtime"], what="the resume")
        self.ctl("key 26")                                      # but Pause only holds: not sent in PAUSED
        self.wait(lambda: [e for e in self.log("key") if e.get("why") == "PAUSE: RUN resumes"], what="Pause kept")
        self.assertEqual([c[0] for c in self.m.calls], ["stop", "run", "pause_toggle", "realtime"])
        self.assertEqual(self.m.calls[3][1], ("resume",))
        self.assertEqual(self.keys_sent(), [27, 25, 26, 25])
        whys = [e.get("why") for e in self.log("key")]
        self.assertIn("RUN: job running", whys)
        self.assertNotIn("PAUSE resumes", whys)
        self.m.now["state"] = "Alarm"
        self.m.set_job()
        self.assertEqual(self.synced(status=pendant.ALARM)["shown"], "ALARM")
        self.ctl("key 25")
        self.ctl("key 26")                                      # (not IDLE: Pause goes, the host says why not)
        self.wait(lambda: self.keys_sent().count(26) == 2, what="Pause sent")
        self.assertIn("RUN: not in ALARM", [e.get("why") for e in self.log("key")])
        self.m.set_job(alarm={"code": 1, "critical": True})     # a hard or soft limit: its own name
        self.assertEqual(self.synced(status=pendant.LIMIT)["shown"], "LIMIT")
        sent = [e["line"] for e in self.log("sent")]
        self.assertEqual(sent[sent.index("FW:2") - 1:][:3], ["CONREQ", "FW:2", "REQ"])     # (right after CONACK)
        self.assertEqual(self.said(self.m, "the fixed firmware"), [("info", pendant.FIXED)])
        self.assertEqual(self.log("bad") + self.log("short") + self.log("timeout"), [])

    def test_fixed_firmware_pause_goes_without_a_fresh_status(self):
        """A feed hold is never lost to an old status: with no SYNC for a while Pause and Stop still go, Run not."""
        self.start("--firmware", "fixed")
        self.connected()
        self.ctl("deaf 1\nage 3\nkey 25\nkey 26\nkey 27")          # (no SYNC comes meanwhile: deaf)
        self.wait(lambda: len([e for e in self.log("key") if e["key"] in (25, 26, 27)]) == 3, what="the keys")
        self.assertEqual([(e["key"], e["sent"], e.get("why")) for e in self.log("key")],
                         [(25, False, "RUN: no status yet"), (26, True, None), (27, True, None)])

    def test_fixed_firmware_bounce_after_a_double_tap(self):
        """A bounce right after the second tap is no tap either: it doesn't arm the key again, so a single tap soon
        after it doesn't zero a second time."""
        self.start("--firmware", "fixed")
        self.connected()
        self.ctl("key 13 double")                               # Zero Z ...
        self.ctl("key 13 double 0.4")                           # ... a bounce at once, then a first tap 0.4 s later
        self.wait(lambda: len(self.log("key")) == 4, what="the 4 presses")
        time.sleep(0.5)
        self.assertEqual([c[2]["axes"] for c in self.m.calls if c[0] == "zero"], ["Z"])
        self.assertEqual([(e["key"], e["sent"], e.get("why")) for e in self.log("key")],
                         [(13, False, "armed: press it again within 1 s"), (13, True, None),
                          (13, False, "contact bounce: not a tap"), (13, False, "armed: press it again within 1 s")])

    def test_double_tap_window(self):
        """The fixed firmware takes the second tap 0.12 to 1 s after the first, with no other key between (sooner
        is contact bounce: the first tap still waits); the original takes it whenever it comes (only keys 20, 21
        and 24 disarm it)."""
        def zeros():
            return [c[2]["axes"] for c in self.m.calls if c[0] == "zero"]
        for firmware, want in (("original", ["Z", "Y", "X", "Z"]), ("fixed", ["Z"])):
            self.start("--firmware", firmware)
            self.connected()
            self.ctl("key 13 double 0.02")                      # 20 ms apart: bounce for the fixed firmware
            time.sleep(1.3)
            self.ctl("key 14")
            time.sleep(1.3)
            self.ctl("key 14")                                  # 1.3 s later: too late for the fixed firmware
            self.ctl("key 15")
            self.ctl("key 5")                                   # another key between
            self.ctl("key 15")
            self.ctl("key 13 double")                           # 0.2 s apart: a double tap for both
            self.wait(lambda: zeros()[-1:] == ["Z"] and len(zeros()) == len(want), what=f"the zeros ({firmware})")
            time.sleep(0.3)
            self.assertEqual(zeros(), want, firmware)
            if firmware == "fixed":
                self.assertIn("contact bounce: the first tap still waits", [e.get("why") for e in self.log("key")])
            self.tearDown()

# ------------------------------------------------------------------ with the real Machine

class Stream:
    """An event stream on the real Machine, like a page's: the events, decoded."""

    def __init__(self, m):
        self.m = m
        self.got = []
        with m.lock:
            m.subs.add(self)

    def put(self, data):
        self.got.append(json.loads(data))

    def close(self):
        with self.m.lock:
            self.m.subs.discard(self)


class FixedFirmwareWithUgs(Waits):
    """The fixed firmware (fake_pendant.py --firmware fixed) with UGS as its host: the test answers as
    serialControl.java does (CONACK, then a SYNC for each REQ, its status a whole number). Run then goes only in
    IDLE with a file loaded, and once until the pendant has seen that job end in UGS's SYNCs (IDLE with line 0 right
    after RUN with rows sent, after BUSY with total 0, or after an IDLE with rows sent when the job's last other
    state was RUN; the SYNC before it more than 0.5 s after the Run), or until Stop: UGS says IDLE in the middle of
    its own job whenever FluidNC reports Idle, and its Run would start the file again from line 1. FluidSender's
    statuses (code + 0.5) have no such lock."""

    def setUp(self):
        fd, self.logpath = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.fake = subprocess.Popen([sys.executable, FAKE, "--log", self.logpath, "--boot-delay", "0", "--firmware",
                                      "fixed"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.fd = os.open(self.fake.stdout.readline().strip(), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.status, self.line, self.total = 0.0, 0.0, 12.0     # UGS idle, a file of 12 rows loaded, no job yet
        self.keys = []
        self.on_key = {}                        # key: (status, line, total) UGS says from that KEY on (at once)
        self.lines = []                         # the other lines it sent (UGS ignores them)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.ugs, daemon=True)
        self.thread.start()
        self.wait(lambda: self.log("sync"), 4.0, "the first SYNC")

    def tearDown(self):
        self.done.set()
        self.thread.join(2)
        os.close(self.fd)
        self.fake.kill()
        self.fake.wait()
        self.fake.stdin.close()
        self.fake.stdout.close()
        os.remove(self.logpath)

    def ugs(self):
        """serialControl.java's side: CONACK for CONREQ, a SYNC for each REQ (and the keys, kept)."""
        rx = b""
        while not self.done.is_set():
            if not select.select([self.fd], [], [], 0.05)[0]:
                continue
            try:
                rx += os.read(self.fd, 4096)
            except OSError:
                continue
            *lines, rx = rx.split(b"\n")
            for line in lines:
                text = line.strip().decode(errors="replace")
                if text == "CONREQ":
                    os.write(self.fd, b"CONACK\n")
                elif text == "REQ":
                    sync = struct.pack("<10f", 0, 0, 0, 0, self.status, 150, self.line, self.total, 0.1, 0)
                    os.write(self.fd, b"SYNC" + sync)
                elif text.startswith("KEY:"):
                    key = int(text[4:])
                    if key in self.on_key:              # (before it is kept: press() returns with UGS's new state)
                        self.status, self.line, self.total = self.on_key.pop(key)
                    self.keys.append(key)
                elif text:
                    self.lines.append(text)

    def log(self, ev):
        out = []
        with open(self.logpath) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return [e for e in out if e["ev"] == ev]

    def says(self, status, total=12.0, line=0.0):
        """UGS's status (and its line count: rows sent) from now on, once the pendant has it in a SYNC that came
        after the call (so after the keys pressed before it)."""
        n = len(self.log("sync"))
        self.status, self.line, self.total = status, line, total
        marked = status != int(status)
        self.wait(lambda: any(e["status"] == int(status) and e["total"] == total and e["line"] == line
                              and e["marked"] is marked for e in self.log("sync")[n:]), 3.0,
                  f"a SYNC with status {status}")

    def ctl(self, text):
        self.fake.stdin.write(text + "\n")
        self.fake.stdin.flush()

    def settled(self):
        """The last Run as sent 1 s earlier (fake_pendant.py's "age run"): the SYNCs from now on come more than
        0.5 s after it, so they tell the end of its job."""
        self.ctl("age run 1")

    def press(self, key):
        """Key pressed: the KEY it sent, or why not."""
        n, k = len(self.log("key")), len(self.keys)
        self.ctl(f"key {key}")
        ev = self.wait(lambda: self.log("key")[n:], 3.0, f"key {key}")[0]
        if ev["sent"]:
            self.wait(lambda: len(self.keys) > k, 3.0, f"KEY:{key} at UGS")
            self.assertEqual(self.keys[k:], [key])
            return "sent"
        time.sleep(0.1)
        self.assertEqual(len(self.keys), k, key)
        return ev["why"]

    def test_run_once_until_its_end_or_stop_and_only_in_idle(self):
        self.assertEqual(self.log("sync")[-1]["shown"], "IDLE")
        self.assertIs(self.log("sync")[-1]["marked"], False)
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.press(25), "RUN: press STOP first")      # (no end seen: UGS still says IDLE, line 0)
        self.assertEqual(self.press(27), "sent")                       # Stop always goes, and lets Run go again
        self.assertEqual(self.press(25), "sent")
        self.says(0.0)                                                 # (UGS said IDLE: that Stop is over)
        self.assertEqual(self.press(27), "sent")
        self.assertEqual(self.lines, ["FW:2"])                         # (UGS ignores it: only KEY and REQ count)
        for status, shown, why, pause in ((2.0, "RUN", "RUN: job running", "sent"),
                                          (1.0, "HOLD", "PAUSE resumes", "sent"),
                                          (3.0, "BUSY", "RUN: not in BUSY", "PAUSE: use STOP")):   # (a jog, maybe)
            self.says(0.0)                                             # (IDLE since the last Stop: Pause may go)
            self.says(status)
            self.assertEqual(self.log("sync")[-1]["shown"], shown)  # (UGS's states: their old names)
            self.assertEqual(self.press(25), why)
            self.assertEqual(self.press(26), pause)
            self.assertEqual(self.press(27), "sent")
        self.says(0.0, total=0.0)
        self.assertEqual(self.press(25), "RUN: no job loaded")         # (no file in UGS)
        self.assertEqual(self.press(26), "PAUSE:nothing running")
        self.says(0.0)
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.keys, [25, 27, 25, 27, 26, 27, 26, 27, 27, 25])

    def test_run_again_once_its_job_has_ended(self):
        """UGS drops a job's stream at its last row's ok: its line count (rows sent) falls to 0. So an IDLE with
        line 0 right after RUN with rows sent is the end of the job the pendant's Run started, and Run goes again
        with no Stop first (then locked again by that Run). Not right after RUN with line 0: the job's first
        moves, no row counted yet."""
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(2.0)                                                 # its first moves: no row counted yet ...
        self.says(0.0)                                                 # ... then a dwell, say: no end
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(2.0, line=11)
        self.says(0.0)                                                 # the end: the count back to 0
        self.assertEqual(self.press(25), "sent")                       # (the file again from line 1, as asked)
        self.assertEqual(self.press(25), "RUN: press STOP first")      # (that Run's job: no end seen yet ...)
        self.settled()
        self.says(0.0, line=3)
        self.says(0.0)                                                 # (... and the last job's RUN isn't its)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.assertEqual(self.keys, [25, 25])

    def test_run_again_after_a_short_job_seen_only_in_its_last_moves(self):
        """A short job may show the pendant only its last moves: BUSY with total 0 (UGS has dropped its stream,
        FluidNC still runs what it planned), then IDLE with line 0. That is its end too. BUSY with the file's rows
        is not (a jog or homing: after a Run UGS refused, say)."""
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(3.0, total=0.0)                                      # its last moves
        self.says(0.0)
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(3.0)                                                 # BUSY, total the file's rows: a jog, say
        self.says(0.0)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.assertEqual(self.keys, [25, 25])

    def test_run_again_after_an_idle_with_rows_sent_at_the_end(self):
        """FluidNC may run dry waiting for the last rows, or dwell in them: UGS says IDLE with rows sent, then IDLE
        with line 0 once the last one is answered. That is the end if the job's last other state since the Run was
        RUN. After a HOLD it may have been cancelled in the UGS window: no end."""
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(2.0, line=6)
        self.says(0.0, line=12)                                        # all rows sent, FluidNC idle
        self.says(0.0)                                                 # the last one answered
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(2.0, line=4)
        self.says(1.0, line=4)                                         # held in the UGS window ...
        self.says(0.0, line=4)
        self.says(0.0)                                                 # ... then IDLE: no end after a HOLD
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.assertEqual(self.keys, [25, 25])

    def test_no_run_in_a_dwell_of_its_job(self):
        """In a dwell (or a spindle spin-up, the planner run dry) UGS says IDLE in the middle of its job, with rows
        sent (line 1 or more): no end, Run stays locked. Also with all rows sent (line = total), where the line
        count alone wouldn't keep Run back. The job's end then unlocks it."""
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(2.0, line=3)
        self.says(0.0, line=3)                                         # a dwell
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(2.0, line=12)
        self.says(0.0, line=12)                                        # a dwell in its last rows, all sent
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(3.0, total=0.0)                                      # its last moves
        self.says(0.0)
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.keys, [25, 25])

    def test_no_end_from_the_syncs_right_after_a_run(self):
        """UGS fills its row count late: its SYNCs within 0.5 s of a Run may predate it. So no end from them: an
        IDLE with line 0 whose SYNC before came that soon after the Run doesn't unlock Run (the IDLE itself comes
        later: UGS's RUN is asked every 1 s), and a RUN that soon isn't the job's last state either. Later SYNCs
        tell."""
        n = len(self.log("sync"))
        self.on_key[25] = (2.0, 5.0, 12.0)                             # at once on the Run: RUN, an old count
        self.assertEqual(self.press(25), "sent")
        run = [e for e in self.log("key") if e["sent"]][-1]["t"]
        self.wait(lambda: [e for e in self.log("sync")[n:] if e["status"] == 2], 3.0, "UGS's RUN")
        self.says(0.0)
        syncs = self.log("sync")[n:]
        i = next(i for i, e in enumerate(syncs) if e["status"] == 2)
        self.assertEqual(syncs[i + 1]["status"], 0, "timing: a SYNC between RUN and IDLE")
        self.assertLess(syncs[i]["t"] - run, 0.5, "timing: RUN came late")
        self.assertGreater(syncs[i + 1]["t"] - run, 0.5)               # (the SYNC before the IDLE is what counts)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(0.0, line=3)
        self.says(0.0)                                                 # (no RUN counted since the Run)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(2.0, line=3)                                         # its job now: RUN, then the end
        self.says(0.0)
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.keys, [25, 25])

    def test_run_still_needs_stop_after_an_end_it_missed(self):
        """An end the pendant didn't see as one keeps Run locked ("RUN: press STOP first"): IDLE with line 0 after
        an IDLE with rows sent but no RUN seen since the Run (a short job between two SYNCs), or in the first SYNCs
        after it connected again (the status before them unknown). Stop still lets Run go, and the end of the job
        that Run starts unlocks it again."""
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(0.0, line=3)                                         # rows sent, but no RUN seen
        self.says(0.0)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.assertEqual(self.press(27), "sent")                       # (UGS does nothing with it in IDLE)
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(2.0, line=5)
        self.ctl("deaf 2")                                             # a SYNC lost: no REQ until its timeout
        self.status, self.line = 0.0, 0.0                              # meanwhile the job ends
        self.wait(lambda: len(self.log("connected")) == 2, 6.0, "connected again")
        self.says(0.0)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.assertEqual(self.press(27), "sent")
        self.assertEqual(self.press(25), "sent")
        self.settled()
        self.says(2.0, line=2)
        self.says(0.0)
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.keys, [25, 27, 25, 27, 25, 25])

    def test_fluidsenders_statuses_end_no_ugs_job(self):
        """FluidSender's statuses (code + 0.5) have no Run lock, as before: Run goes there, and leaves the UGS lock
        as it was. Nor do they end the job of a UGS Run or count as its state: an end needs two SYNCs from UGS."""
        self.assertEqual(self.press(25), "sent")                       # UGS: locked
        self.settled()
        self.says(pendant.RUNNING + pendant.MARK, line=5)
        self.says(0.0)                                                 # (the SYNC before it was FluidSender's)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(0.0, line=3)
        self.says(0.0)                                                 # (FluidSender's RUNNING isn't UGS's RUN)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(2.0, line=5)
        self.says(pendant.IDLE + pendant.MARK)                         # (this one is FluidSender's)
        self.assertEqual(self.press(25), "sent")                       # FluidSender: no lock
        self.says(0.0)
        self.assertEqual(self.press(25), "RUN: press STOP first")      # (the UGS lock as it was)
        self.says(2.0, line=2)
        self.says(0.0)                                                 # UGS's own end
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.keys, [25, 25, 25])

    def test_no_pause_after_stop_until_ugs_says_idle(self):
        """UGS's Stop holds the job, waits for Hold:0, then resets it: its Pause in that HOLD would resume the job
        (and FluidNC run on the lines it still has). So after Stop, Pause goes only once UGS has said IDLE. With
        FluidSender's statuses (its own Stop refuses a Pause) it goes."""
        self.says(2.0)                                                 # a UGS job running
        self.assertEqual(self.press(27), "sent")
        self.says(1.0)                                                 # (its Stop: the job held, then reset)
        self.assertEqual(self.press(26), "PAUSE: STOP under way")
        self.assertEqual(self.log("sync")[-1]["shown"], "HOLD")
        self.says(0.0)                                                 # reset: IDLE, the Stop is over
        self.says(1.0)                                                 # a hold from the UGS window
        self.assertEqual(self.press(26), "sent")                       # (it resumes)
        self.says(pendant.RUNNING + pendant.MARK)
        self.assertEqual(self.press(27), "sent")
        self.says(pendant.STOPPING + pendant.MARK)
        self.assertEqual(self.press(26), "sent")
        self.assertEqual(self.keys, [27, 26, 27, 26])

    def test_stop_not_twice_while_ugs_stops(self):
        """Each Stop in UGS's RUN or HOLD starts one more FluidNCController.cancelSend task that waits for Hold:0, then
        resets FluidNC; one that misses that moment resets a later hold. So not again within 2 s while UGS hasn't
        said IDLE since; then it goes (a Stop that is stuck: a door), and at once after IDLE. FluidSender: always."""
        self.says(2.0, line=5)                                         # a UGS job running
        self.assertEqual(self.press(27), "sent")
        self.assertEqual(self.press(27), "STOP under way")             # (pressed twice in a hurry)
        self.says(1.0, line=5)                                         # (its cancel: the job held, waiting)
        self.assertEqual(self.press(27), "STOP under way")
        time.sleep(2.0)
        self.assertEqual(self.press(27), "sent")                       # (2 s on: still no IDLE, so it goes)
        self.says(0.0, line=5)                                         # (reset: IDLE, the Stop is over)
        self.assertEqual(self.press(27), "sent")
        self.says(pendant.RUNNING + pendant.MARK)
        self.assertEqual(self.press(27), "sent")
        self.assertEqual(self.press(27), "sent")
        self.assertEqual(self.keys, [27, 27, 27, 27, 27])

    def test_no_run_while_a_ugs_job_is_part_way(self):
        """UGS's line count (rows sent) is part-way from a job's first rows until it ends, and stays so after a
        cancel; 0 after a normal end. Run (its send(): the file again from line 1) doesn't go in an IDLE with the
        count part-way, unless Stop was pressed since UGS last said RUN or HOLD with no Stop under way."""
        self.says(2.0, line=3)                                         # a job started in the UGS window
        self.says(0.0, line=3)                                         # ... in a dwell, or the planner run dry
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.says(1.0, line=5)                                         # stopped in the UGS window: held, reset
        self.says(0.0, line=5)
        self.assertEqual(self.press(25), "RUN: press STOP first")
        self.assertEqual(self.press(27), "sent")                       # (UGS does nothing with it in IDLE)
        self.says(0.0, line=5)
        self.assertEqual(self.press(25), "sent")                       # Run: the file from line 1, as asked
        self.assertEqual(self.press(27), "sent")
        self.says(0.0, line=5)
        self.says(2.0, line=4)                                         # that job: stopped from the pendant
        self.assertEqual(self.press(27), "sent")
        self.says(1.0, line=4)                                         # (its cancel: HOLD, then IDLE)
        self.says(0.0, line=4)
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.press(27), "sent")
        self.says(0.0, line=4)
        self.says(2.0, line=11)
        self.says(0.0, line=0)                                         # a job done: the count back to 0
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.keys, [27, 25, 27, 27, 25, 27, 25])

    def test_pause_in_the_last_moves_of_a_ugs_job(self):
        """UGS ends a job's stream at the last row's ok: then BUSY (RUN with no stream: total 0) while FluidNC runs
        what it has planned. Pause holds those, and resumes them from UGS's HOLD. Not in other BUSY (a jog, or
        homing after a job: total is the file's rows there), nor once another key went (a move may start)."""
        self.says(2.0, line=11)
        self.says(3.0, total=0.0)                                      # the job's last moves
        self.assertEqual(self.press(26), "sent")
        self.says(1.0)                                                 # held (total: the file's rows again)
        self.assertEqual(self.press(26), "sent")                       # resumes
        self.says(3.0, total=0.0)                                      # (after HOLD: still the last moves)
        self.assertEqual(self.press(26), "sent")
        self.assertEqual(self.press(5), "sent")                        # (a jog key: a move may start now)
        self.assertEqual(self.press(26), "PAUSE: use STOP")
        self.says(0.0)
        self.says(3.0)                                                 # a jog or homing: total the file's rows
        self.assertEqual(self.press(26), "PAUSE: use STOP")
        self.says(0.0)
        self.says(3.0, total=0.0)                                      # a move with no file loaded, from IDLE
        self.assertEqual(self.press(26), "PAUSE: use STOP")
        self.assertEqual(self.keys, [26, 26, 26, 5])

    def test_fluidsenders_status_has_no_run_flag(self):
        self.says(pendant.IDLE + pendant.MARK)
        self.assertIs(self.log("sync")[-1]["marked"], True)
        self.assertEqual(self.log("sync")[-1]["status"], pendant.IDLE)
        self.assertEqual(self.press(25), "sent")
        self.assertEqual(self.press(25), "sent")                       # (FluidSender knows a job is on: RUNNING)
        self.says(pendant.RUNNING + pendant.MARK)
        self.assertEqual(self.log("sync")[-1]["shown"], "RUNNING")
        self.assertEqual(self.press(25), "RUN: job running")
        self.says(pendant.PAUSED + pendant.MARK)
        self.assertEqual(self.log("sync")[-1]["shown"], "PAUSED")
        self.assertEqual(self.press(25), "sent")                       # (FluidSender resumes; UGS's HOLD: not sent)
        self.says(pendant.ALARM + pendant.MARK)
        self.assertEqual(self.log("sync")[-1]["shown"], "ALARM")
        self.assertEqual(self.press(26), "sent")                       # (FluidSender says why not itself)
        self.says(4.0)                                                 # (4 as a whole number: no host sends it)
        self.assertEqual(self.log("sync")[-1]["shown"], "BUSY")


class RealMachine(RawPty):
    """The pendant on the real Machine (machine.py in test mode: only these ptys may be opened), first with no
    FluidNC, then with tools/fake_fluidnc.py, where a key really moves."""

    def setUp(self):
        super().setUp()
        self.fluidnc = None
        self.m = machine.Machine(extra_ports=[self.path], only_extra=True)
        self.p = pendant.PendantLink(self.m, self.m.settings)
        self.m.attach(pendant=self.p)
        self.events = Stream(self.m).got

    def tearDown(self):
        self.m.shutdown()
        if self.fluidnc:
            self.fluidnc.kill()
            self.fluidnc.wait()
            self.fluidnc.stdout.close()
        super().tearDown()

    def position(self):
        self.say("REQ")
        got = self.read(44)
        self.assertEqual(len(got), 44)
        return struct.unpack("<10f", got[4:])

    def test_without_fluidnc(self):
        self.handshake()
        self.assertEqual(self.position(), (0, 0, 0, 0, pendant.OFFLINE + pendant.MARK, 150, 0, 0,
                                           struct.unpack("<f", struct.pack("<f", 0.1))[0], 0))
        self.key(5)
        self.assertEqual(self.m.console[-1]["text"], "Pendant: Jog X+ refused: Not connected")
        self.key(8)                                         # the shared step: no FluidNC needed
        self.assertEqual(self.m.settings.get("jog"), {"step": 0.001, "feed": 150})
        self.assertIn({"type": "jogsettings", "step": 0.001, "feed": 150}, self.events)
        self.assertAlmostEqual(self.position()[8], 0.001, places=6)
        for key in (25, 26, 27, 13, 23, 22):                # (Run, Pause, Stop: as the original firmware sends them)
            self.key(key)
            again = ". The pendant sends Run again only after its Stop key" if key == 25 else ""
            self.assertEqual((self.m.console[-1]["dir"], self.m.console[-1]["text"]),
                             ("error", f"Pendant: {pendant.key_name(key)} refused: Not connected{again}"))
        snap = self.m.snapshot()
        self.assertEqual(snap["pendant"], self.p.state())
        self.assertEqual((snap["pendant"]["lastKey"]["key"], snap["pendant"]["stopPause"]), (22, False))
        self.assertEqual(self.m.settings.get("pendantPort"), self.path)
        self.m.shutdown()                                   # lets go of the pendant too
        self.assertEqual(self.p.state()["state"], "off")

    def test_a_key_moves_the_fake_fluidnc(self):
        self.fluidnc = subprocess.Popen([sys.executable, os.path.join(HERE, "tools", "fake_fluidnc.py")],
                                        stdout=subprocess.PIPE, text=True)
        port = self.fluidnc.stdout.readline().strip()
        self.m.extra.append(port)
        self.m.connect(port)
        self.wait(lambda: (self.m.link and not self.m.link.inch and not self.m.link.queue and not self.m.link.inflight
                           and (self.m.snapshot()["status"] or {}).get("wpos")), 5.0, "FluidNC ready, in inches")
        self.handshake()
        self.assertEqual(self.position()[:5], (0, 0, 0, 0, pendant.MARK))     # Idle at work zero
        self.key(5)
        self.assertIn("Pendant: Jog X+ 0.1 in", [c["text"] for c in self.m.console])
        self.wait(lambda: "$J=G20G91X0.1F150" in [c["text"] for c in self.m.console], what="the jog line")
        self.wait(lambda: abs(self.position()[0] - 0.1) < 1e-5 and self.position()[4] == pendant.MARK, 5.0,
                  "X at 0.1, Idle")
        self.key(25)
        self.assertEqual(self.m.console[-1]["text"], "Pendant: Run refused: No program loaded: open a G-code file "
                                                     "first. The pendant sends Run again only after its Stop key")



class FixedFirmwareOnTheRealMachine(Waits):
    """fake_pendant.py with the fixed firmware, the real Machine (test mode) and tools/fake_fluidnc.py: its Run goes
    once a program is loaded (the SYNC's total) and again once the job is over (no Stop key needed first), its
    Pause not while Idle, its Stop always."""

    def setUp(self):
        self.procs = []
        fd, self.logpath = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        cnc = self.spawn([sys.executable, os.path.join(HERE, "tools", "fake_fluidnc.py"), "--inches"])
        port = cnc.stdout.readline().strip()
        self.fake = self.spawn([sys.executable, FAKE, "--log", self.logpath, "--boot-delay", "0", "--firmware",
                                "fixed"], stdin=subprocess.PIPE)
        self.path = self.fake.stdout.readline().strip()
        self.m = machine.Machine(extra_ports=[port, self.path], only_extra=True)
        self.p = pendant.PendantLink(self.m, self.m.settings)
        self.m.attach(pendant=self.p)
        self.m.connect(port)
        self.wait(lambda: (self.m.link and not self.m.link.inch and not self.m.link.queue and not self.m.link.inflight
                           and (self.m.snapshot()["status"] or {}).get("wpos")), 5.0, "FluidNC ready, in inches")
        self.p.connect(self.path)
        self.wait(lambda: self.syncs(), 5.0, "the first SYNC")

    def spawn(self, args, stdin=None):
        proc = subprocess.Popen(args, stdin=stdin, stdout=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def tearDown(self):
        self.m.shutdown()                                   # (lets go of the pendant too)
        for proc in self.procs:
            proc.kill()
            proc.wait()
            for f in (proc.stdin, proc.stdout):
                if f:
                    f.close()
        os.remove(self.logpath)

    def ctl(self, text):
        self.fake.stdin.write(text + "\n")
        self.fake.stdin.flush()

    def entries(self, ev):
        out = []
        with open(self.logpath) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return [e for e in out if e["ev"] == ev]

    def syncs(self, **want):
        return [e for e in self.entries("sync") if all(e[k] == v for k, v in want.items())]

    def console(self):
        return [c["text"] for c in self.m.console]

    def test_run_pause_and_stop(self):
        self.assertEqual(self.syncs()[-1]["total"], 0)
        self.ctl("key 25")                                  # nothing loaded: Run isn't even sent
        self.wait(lambda: self.entries("key"), what="key 25")
        self.assertEqual(self.entries("key")[-1]["why"], "RUN: no job loaded")
        self.m.load_program("short.nc", "G0 X0.05\nG0 X0\n")
        self.wait(lambda: self.syncs(total=2, status=pendant.IDLE), 3.0, "a SYNC with the program's 2 lines")
        self.ctl("key 26")                                  # Idle: Pause isn't sent
        self.ctl("key 25")
        self.wait(lambda: "Pendant: Run short.nc (2 lines)" in self.console(), 3.0, "Run")
        self.wait(lambda: self.m.job.state == "done", 8.0, "the job done")
        self.wait(lambda: self.syncs(status=pendant.IDLE, line=2), 3.0, "a SYNC after it")
        self.ctl("key 25")                                  # again, without Stop first
        self.wait(lambda: self.console().count("Pendant: Run short.nc (2 lines)") == 2, 3.0, "Run again")
        self.wait(lambda: self.m.job.state == "done", 8.0, "the second job done")
        time.sleep(0.6)                                     # (Stop right after a move's ok holds and resets: S1)
        self.ctl("key 27")
        self.wait(lambda: "Pendant: Stop: nothing to stop" in self.console(), 3.0, "Stop")
        keys = [(e["key"], e["sent"], e.get("why")) for e in self.entries("key")]
        self.assertEqual(keys, [(25, False, "RUN: no job loaded"), (26, False, "PAUSE:nothing running"),
                                (25, True, None), (25, True, None), (27, True, None)])
        said = self.console()
        self.assertEqual(said.count(pendant.FIXED), 1)      # (its FW:2, right after connecting)
        self.assertLess(said.index(pendant.FIXED), said.index("Pendant: Run short.nc (2 lines)"))
        self.assertEqual(said.count(pendant.AGAIN), 0)      # (the original firmware's note: never with it)
        self.assertIs(self.p.state()["stopPause"], True)
        self.assertEqual(self.entries("bad") + self.entries("short") + self.entries("timeout"), [])


class RealMachineJob(jobs.Stage3):
    """The screen's status and keys 25-27 on the real Machine, in test_jobs.py's harness (no port, made-up time):
    what the keys write to FluidNC is seen as it goes."""

    def setUp(self):
        super().setUp()
        self.p = pendant.PendantLink(self.m, None)

    def status(self):
        return self.p._numbers()["status"]

    def key(self, n):
        """Key n done with the real Machine's calls: (ok, console line, the realtime bytes it wrote)."""
        ok, text = self.p._action(n)
        self.tick(0.01)
        return ok, text, self.realtime()

    def test_the_status(self):
        self.m.link = None
        self.assertEqual(self.status(), pendant.OFFLINE)
        self.m.link = self.link
        self.ready()
        self.assertEqual(self.status(), pendant.IDLE)
        for report, want in ((jobs.RUN, pendant.MOVING), (jobs.base.JOG, pendant.JOG), (jobs.HOLD1, pendant.PAUSED),
                             (jobs.HOLD0, pendant.PAUSED), ("<Home|MPos:0,0,0|FS:0,0>", pendant.HOMING),
                             ("<Door:1|MPos:0,0,0|FS:0,0>", pendant.DOOR), ("<Alarm|MPos:0,0,0|FS:0,0>", pendant.ALARM),
                             (jobs.IDLE, pendant.IDLE)):
            self.reply(report, dt=0.06)
            self.assertEqual(self.status(), want, report)
        self.t += 2.1
        self.assertEqual(self.status(), pendant.OFFLINE)       # no report for 2 s: no answer
        self.reply(jobs.IDLE, dt=0.06)
        self.m.begin_activity("scan")
        self.assertEqual(self.status(), pendant.SCAN)
        self.m.end_activity("scan")
        self.assertEqual(self.status(), pendant.IDLE)

    def test_keys_during_a_job(self):
        """Run starts it, does nothing while it runs, and resumes it after a Feed Hold; after an error neither Run
        nor Pause resumes it; Stop stops it, STOPPING until a report after FluidNC's reset."""
        self.ready()
        self.load(jobs.LONG)
        self.assertEqual(self.key(25), (True, "Pendant: Run t.nc (30 lines)", b""))
        self.assertEqual(self.sent()[0], "G20 G90")
        self.reply("ok")
        self.sent()
        self.reply(jobs.RUN)
        self.assertEqual(self.status(), pendant.RUNNING)
        self.assertEqual(self.key(25), (None, "Pendant: Run: t.nc is running already (the pendant's Pause and Stop "
                                              "work for it)", b""))
        self.assertEqual(self.key(26), (True, "Pendant: Pause (feed hold)", b"!"))
        self.assertEqual((self.job.state, self.status()), ("paused", pendant.PAUSED))
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.key(25), (True, "Pendant: Run: Resume t.nc", b"~"))     # cycle start
        self.assertEqual((self.job.state, self.status()), ("running", pendant.RUNNING))
        self.reply("error:20")                              # a line refused: paused at once
        self.assertEqual(self.realtime(), b"!")
        self.assertEqual(self.status(), pendant.ERROR)
        self.reply(jobs.HOLD0, dt=0.06)
        for key in (25, 26):
            ok, text, rt = self.key(key)
            self.assertEqual((ok, rt), (False, b""), text)
            self.assertIn("refused: The job stopped on an error (line 1: G0X1.0001: error:20 (unsupported or invalid "
                          "G-code)). See it on the page, then Resume there to go on, or Stop", text)
        self.assertEqual(self.job.state, "paused")
        self.assertEqual(self.key(27), (True, "Pendant: Stop: stopping the job (feed hold, then a reset once it stands "
                                              "still)", b""))     # (held already: no second '!')
        self.assertEqual(self.status(), pendant.STOPPING)
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")          # stopped: the reset
        self.assertEqual((self.job.state, self.status()), ("stopped", pendant.STOPPING))
        self.reply(jobs.BANNER)
        self.assertEqual(self.status(), pendant.STOPPING)   # (the last report is still the Hold from before it)
        ok, text, rt = self.key(25)
        self.assertEqual((ok, rt), (False, b""), text)      # (no '~' into the reset)
        self.reply(jobs.IDLE, dt=0.06)
        self.assertEqual(self.status(), pendant.IDLE)

    def test_the_sync_total_is_the_loaded_programs(self):
        """While idle the SYNC's total is the loaded program's lines (as UGS sends its file's rows): the fixed
        firmware's Run goes only with 1 or more. During and after a job of it, the job's lines."""
        self.ready()

        def numbers():
            st = self.p._numbers()
            return st["status"], st["line"], st["total"]
        self.assertEqual(numbers(), (pendant.IDLE, 0, 0))     # no program
        self.load("(only a comment)\n")
        self.assertEqual(numbers(), (pendant.IDLE, 0, 0))     # (nothing to run: the host refuses it too)
        self.load("G0 X1\n(comment)\n\nG0 X0\nG0 Y1\n")
        self.assertEqual(numbers(), (pendant.IDLE, 0, 3))
        self.assertEqual(self.key(25)[:2], (True, "Pendant: Run t.nc (3 lines)"))
        self.assertEqual(self.sent(), ["G20 G90", "G0X1", "G0X0", "G0Y1"])
        self.reply("ok", "ok")
        self.assertEqual(numbers(), (pendant.RUNNING, 1, 3))
        self.reply("ok", "ok", jobs.IDLE, dt=0.06)
        self.assertEqual(self.job.state, "done")
        self.assertEqual(numbers(), (pendant.IDLE, 3, 3))     # (done: L3/3, and Run may go again)
        self.load("G0 X2\n")
        self.assertEqual(numbers(), (pendant.IDLE, 0, 1))
        self.load("")                                       # closed
        self.assertEqual(numbers(), (pendant.IDLE, 0, 0))

    def test_the_sync_total_after_a_stop(self):
        """Stopped part-way, Idle again: the SYNC's total is still the program's lines, so the fixed firmware's Run
        goes (FluidSender's IDLE has no Run flag, and its line count where the job stopped doesn't matter)."""
        self.ready()
        self.load(jobs.LONG)

        def numbers():
            st = self.p._numbers()
            return st["status"], st["line"], st["total"]
        self.assertEqual(numbers(), (pendant.IDLE, 0, 30))
        self.assertEqual(self.key(25)[:2], (True, "Pendant: Run t.nc (30 lines)"))
        self.assertEqual(self.sent()[0], "G20 G90")
        self.reply("ok", "ok", "ok")                        # (G20 G90 and the first 2 lines)
        self.reply(jobs.RUN)
        self.assertEqual(numbers(), (pendant.RUNNING, 2, 30))
        self.assertEqual(self.key(27)[0], True)
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(jobs.BANNER)
        self.reply(jobs.IDLE, dt=0.06)
        self.assertEqual(self.job.state, "stopped")
        self.assertEqual(numbers(), (pendant.IDLE, 2, 30))

    def running(self):
        """A job of jobs.LONG started from the pendant, running (its first lines sent), the screen RUNNING."""
        self.ready()
        self.load(jobs.LONG)
        self.assertEqual(self.key(25)[0], True)
        self.assertEqual(self.sent()[0], "G20 G90")
        self.reply("ok")
        self.sent()
        self.reply(jobs.RUN)
        self.assertEqual(self.status(), pendant.RUNNING)

    def test_stop_in_a_critical_alarm(self):
        """A hard limit in a job: FluidNC goes Critical (reported as Alarm), the lines it was sent unanswered. LIMIT on
        the screen; Home and Pause refused; Stop is the Soft Reset at once, never a '!' (in Critical a '!' makes a
        Hold that the reset leaves as Idle, unhomed, with no alarm). Then ALARM:14: ALARM, and Home All goes."""
        self.running()
        self.reply("ALARM:1", "[MSG:ERR: Reset to continue]")
        self.reply(LIMIT_REPORT, dt=0.8)
        self.assertTrue(self.link.job.inflight)             # (Machine.stop() would take it as maybe moving)
        self.assertEqual(self.status(), pendant.LIMIT)
        for key in (23, 26):
            ok, text, rt = self.key(key)
            self.assertEqual((ok, rt), (False, b""), text)
        self.assertEqual(self.key(27), (True, "Pendant: Stop: " + pendant.LIMIT_RESET, b"\x18"))
        self.assertEqual(self.status(), pendant.STOPPING)
        self.reply("ALARM:14", jobs.BANNER)                 # (FluidNC's reset from Critical, the axes unhomed)
        self.reply(LIMIT_REPORT, dt=0.6)
        self.tick(0.5)
        self.assertEqual(self.realtime(), b"")              # (no '!' after it either)
        self.assertEqual(self.status(), pendant.ALARM)
        self.assertEqual(self.key(23)[:2], (True, "Pendant: Home all ($H)"))

    def test_stop_in_a_critical_alarm_with_nothing_to_stop(self):
        """A hard limit with nothing of ours unanswered (a jog's end, say): Machine.stop() has nothing to stop and
        Home needs a reset first, so the pendant's Stop is that reset."""
        self.ready()
        self.reply("ALARM:1", "[MSG:ERR: Reset to continue]")
        self.reply(LIMIT_REPORT, dt=0.8)
        self.assertEqual(self.status(), pendant.LIMIT)
        ok, text, rt = self.key(23)
        self.assertEqual((ok, rt), (False, b""), text)
        self.assertIn(machine.CRITICAL, text)
        self.assertEqual(self.key(27), (True, "Pendant: Stop: " + pendant.LIMIT_RESET, b"\x18"))

    def test_stop_while_homing_is_the_reset_at_once(self):
        """Homing ignores a feed hold ("use Reset instead"): Stop is the Soft Reset at once (Link.safe_stop), and the
        console says so (not "feed hold, then a reset")."""
        self.ready()
        self.reply("<Alarm|MPos:0,0,0|FS:0,0>", dt=0.06)
        self.assertEqual(self.key(23)[:2], (True, "Pendant: Home all ($H)"))
        self.assertEqual(self.sent(), ["$H"])
        self.reply("<Home|MPos:1.000,0.000,0.000|FS:0,0>", dt=0.06)
        self.assertEqual(self.status(), pendant.HOMING)
        self.assertEqual(self.key(27), (True, "Pendant: Stop: " + pendant.RESET_NOW["motion"], b"\x18"))

    def test_pause_never_resumes_into_the_stop(self):
        """After Stop: its reset until the banner, and the moment after it (the last report still says Hold:0), and a
        line left from before the reset that FluidSender held (contain(): "Press Soft Reset when ready"). Pause is
        refused there: a '~' would let that line run, in the reset's modes."""
        self.running()
        self.assertEqual(self.key(27)[2], b"!")
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        ok, text, rt = self.key(26)                         # its reset: no banner yet
        self.assertEqual((ok, rt), (False, b""), text)
        self.assertIn(machine.NO_RESUME, text)
        self.reply(jobs.BANNER)
        self.assertEqual(self.status(), pendant.STOPPING)   # (the last report still says Hold:0)
        ok, text, rt = self.key(26)
        self.assertEqual((ok, rt), (False, b""), text)
        self.reply(jobs.RUN, dt=0.06)                       # a line from before the reset runs: held, reset again
        self.assertEqual(self.realtime(), b"!")
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")
        self.reply(jobs.BANNER)
        self.reply(jobs.RUN, dt=0.06)                       # and again: held, and no reset by itself
        self.assertEqual(self.realtime(), b"!")
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.status(), pendant.STOPPING)
        for key in (25, 26):
            ok, text, rt = self.key(key)
            self.assertEqual((ok, rt), (False, b""), text)
        self.assertEqual(self.key(27)[::2], (True, b""))    # Stop gets out of it: FluidNC holds already, so ...
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.realtime(), b"\x18")          # ... the reset once a report after it says so

    def test_pause_cancels_a_resume_waiting(self):
        """Pause, Run while FluidNC still slows down (Hold:1: the '~' waits for Hold:0), Pause again: that Pause
        cancels the Resume waiting, and the job stays held."""
        self.running()
        self.assertEqual(self.key(26), (True, "Pendant: Pause (feed hold)", b"!"))
        self.reply(jobs.HOLD1, dt=0.06)
        self.assertEqual(self.key(25), (True, "Pendant: Run: Resume t.nc", b""))      # (it waits for Hold:0)
        self.assertIsNotNone(self.link.resume_at)
        self.assertEqual(self.key(26), (True, "Pendant: Pause (feed hold): the Resume waiting for FluidNC to stop is "
                                              "cancelled", b"!"))
        self.assertIsNone(self.link.resume_at)
        self.reply(jobs.HOLD0, dt=0.1)
        self.tick(0.1)
        self.assertEqual((self.realtime(), self.job.state), (b"", "paused"))

    def test_pause_when_fluidnc_held_by_itself_holds(self):
        """FluidNC holds by itself (its hold button, an M0, the page's Hold) while the pendant still shows RUNNING
        (it asks only once a second then): a Pause pressed to hold doesn't resume the job."""
        self.running()                                      # (the last SYNC: RUNNING)
        self.reply(jobs.HOLD1, dt=0.06)
        self.reply(jobs.HOLD0, dt=0.1)
        self.assertEqual(self.job.state, "paused")
        ok, text, rt = self.key(26)
        self.assertEqual((ok, rt, self.job.state), (True, b"!", "paused"), text)
        self.assertIn("was held already (the pendant didn't show it yet)", text)

    def test_the_door(self):
        """The door opened: DOOR, and Run waits for it to be closed. Closed again (Door:0, ready to go on): PAUSED,
        and Run resumes."""
        self.running()
        self.reply("<Door:1|MPos:1.000,0.000,0.000|FS:0,0>", dt=0.06)
        self.assertEqual((self.status(), self.job.state), (pendant.DOOR, "paused"))
        ok, text, rt = self.key(25)
        self.assertEqual((ok, rt), (False, b""), text)
        self.assertIn("The safety door is open: close it, then Run resumes", text)
        self.reply("<Door:0|MPos:1.000,0.000,0.000|FS:0,0>", dt=0.06)
        self.assertEqual(self.status(), pendant.PAUSED)
        self.assertEqual(self.key(25), (True, "Pendant: Run: Resume t.nc", b"~"))
        self.assertEqual(self.job.state, "running")

    def test_pause_right_after_a_goto(self):
        """Return to zero (or a jog key) just sent: FluidNC still says Idle. MOVING on the screen, and Pause holds it;
        once a report after its last ok says Idle, IDLE again."""
        self.ready()
        self.assertEqual(self.key(22)[:2], (True, "Pendant: Return to zero"))
        self.assertEqual(self.sent(), ["G20 G90 G0 Z0.2"])
        self.assertEqual(self.status(), pendant.MOVING)     # (in flight)
        self.assertEqual(self.key(26), (True, "Pendant: Pause (feed hold)", b"!"))
        self.reply(jobs.HOLD0, dt=0.06)
        self.assertEqual(self.status(), pendant.PAUSED)
        self.assertEqual(self.key(25)[1:], ("Pendant: Run: Resume (FluidNC was in Hold)", b"~"))
        self.reply(jobs.RUN, dt=0.06)
        self.assertEqual(self.status(), pendant.MOVING)     # (FluidNC says Run: a move outside a job)
        for _ in range(3):                                  # the lift, X Y 0, Z 0: each answered, the next sent
            self.reply("ok")
            self.tick(0.01)
            self.sent()
        self.reply(jobs.IDLE, dt=0.01)
        self.assertEqual(self.status(), pendant.MOVING)     # (a report right after the last ok: may not show it)
        self.reply(jobs.IDLE, dt=0.06)                      # (Idle a moment after it: done)
        self.assertEqual(self.status(), pendant.IDLE)

    def test_run_resumes_a_hold_without_a_job(self):
        """As the page's Resume: '~' once FluidNC has stopped (Hold:0), not while it slows down (Hold:1)."""
        self.ready()
        self.reply(jobs.HOLD1, dt=0.06)
        self.assertEqual(self.key(25), (True, "Pendant: Run: Resume (FluidNC was in Hold)", b""))
        self.assertEqual(self.log("info")[-1], "Resume: FluidNC is still slowing down; Resume goes once it has stopped")
        self.reply(jobs.HOLD0, dt=0.1)
        self.assertEqual(self.realtime(), b"~")
        self.reply(jobs.IDLE, dt=0.06)
        self.assertEqual(self.key(25)[0], False)            # Idle, no program: Run refused, nothing written
        self.assertEqual((self.realtime(), self.sent()), (b"", []))


if __name__ == "__main__":
    unittest.main()
