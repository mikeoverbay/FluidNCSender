"""
End to end: pretend FluidNCs (tools/fake_fluidnc.py), a pretend probe (tools/fake_probe.py) and a
pretend pendant (tools/fake_pendant.py) on pseudo terminals, a real server on port 8790
(FLUIDSENDER_TEST_PORT sets another), and the HTTP calls and event stream used the way the page
uses them: connecting, jogs, zero, home (stage 2); jobs, Hold / Resume / the safe stop, the
vacuum switch, Go to zero, the pendant's keys, the probe and the surface scan (stage 3 + 4); a line
FluidNC keeps through a reset, connecting while it holds lines, Disconnect during a job; FluidNC 4.1.1 (the
simulator's default, as the user's machine) and 3.9.4 (--fw 3.9.4), the coolant pins (a vacuum on a plain GPIO,
NO_PIN), the overrides and a critical alarm's Stop (stage 5).

No real serial port is ever opened: the server runs with FLUIDSENDER_ONLY_EXTRA_PORTS=1, so it
can list and open nothing but the simulators' ptys. Its settings file and scans folder are
temporary (FLUIDSENDER_SETTINGS, FLUIDSENDER_SCANS).

    python3 -m unittest discover -s tests        (about 3 minutes)
"""

import errno
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(ROOT, "tools", "fake_fluidnc.py")
PROBE = os.path.join(ROOT, "tools", "fake_probe.py")
PENDANT = os.path.join(ROOT, "tools", "fake_pendant.py")
SERVER = os.path.join(ROOT, "server.py")
PORT = int(os.environ.get("FLUIDSENDER_TEST_PORT") or 8790)
RELEASE = 2.0            # FLUIDSENDER_RELEASE_AFTER for the test server (10 s normally)
H = {"X-FluidSender": "1"}
WCO = (-150.0, -100.0, -30.0)   # the simulator's work offset, mm
# the settings FluidSender reads after $I and $G (quietly: not in the console), then $SS (its startup log: the
# spindle type). $T comes before them when FluidNC is in an alarm at connect
SETTINGS = ["$/axes/x/max_rate_mm_per_min", "$/axes/y/max_rate_mm_per_min", "$/axes/z/max_rate_mm_per_min",
            "$/axes/x/acceleration_mm_per_sec2", "$/axes/y/acceleration_mm_per_sec2",
            "$/axes/z/acceleration_mm_per_sec2", "$/planner_blocks",
            "$/axes/x/soft_limits", "$/axes/y/soft_limits", "$/axes/z/soft_limits",
            *(f"$/axes/{a}/{k}" for a in "xyz" for k in ("max_travel_mm", "homing/mpos_mm", "homing/positive_direction")),
            "$/coolant/flood_pin", "$/coolant/mist_pin", "$/coolant/delay_ms"]
READS = [*SETTINGS, "$SS"]
FW411 = "4.1 FluidNC v4.1.1 (esp32-noradio)"    # the simulator's default, as the user's machine
FW394 = "3.7 FluidNC v3.9.4"                    # (--fw 3.9.4)
NO_AXES = {a: {"maxRate": None, "accel": None, "softLimits": None, "travel": None} for a in "xyz"}
JOGGING = "Jogging: let go of the jog button first"
CRITICAL = "FluidNC needs a Soft Reset first ([MSG:Reset to continue])"
JOB_RUNNING = "A job is running"
OUTPUTS_ON = "ALARM: the spindle and vacuum/AUX may still be on. Soft Reset turns them off."
LEFTOVER = "FluidNC ran a line left from before the reset (a FluidNC quirk; 4.1.1 too); held it and resetting again"
OLD_HOLD = "FluidNC is in Hold, probably with lines from before: Soft Reset clears them (Resume would run them)"
IN_ALARM = ("FluidNC is in ALARM: Feed Hold/Resume do nothing useful there, and after a hard or soft limit a Feed "
            "Hold would let FluidNC out of the alarm without homing. Soft Reset (critical) or Unlock/Home")
HOLD_DECEL = 0.3                # the simulator's Hold:1 -> Hold:0 (slowing down to a stop)
RELEASED = "Released the port because no FluidSender window is open"
PROBE_LET_GO_DOWN = ("Released while not homed, the probe may be down: Home Probe before moving X or Y "
                     "(connect it first)")


class Sim:
    """A simulator process. Its --log file lists what it received: 'LINE <text>' and 'RT <byte>'."""

    def __init__(self, logdir, name, *args):
        self.log = os.path.join(logdir, name + ".log")
        self.proc = subprocess.Popen([sys.executable, SIM, "--log", self.log, *args], stdout=subprocess.PIPE, text=True)
        self.path = self.proc.stdout.readline().strip()
        if not self.path.startswith("/dev/pts/"):
            self.stop()
            raise RuntimeError(f"simulator did not start: {self.path!r}")

    def entries(self, since=0):
        return [e for _, e in self.timed(since)]

    def timed(self, since=0):
        """(unix time, entry) for what it received, from entry number since on."""
        try:
            with open(self.log) as f:
                rows = [line.rstrip("\n").split(" ", 1) for line in f]
        except FileNotFoundError:
            return []
        return [(float(t), e) for t, e in rows[since:]]

    def lines(self, since=0):
        return [e[5:] for e in self.entries(since) if e.startswith("LINE ")]

    def realtime(self, since=0):
        return [e[3:] for e in self.entries(since) if e.startswith("RT ")]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)   # in case a test left it stopped
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()


class Tool:
    """fake_probe.py or fake_pendant.py on its pty. Its --log file has one JSON object per line."""

    def __init__(self, logdir, name, script, *args):
        self.log = os.path.join(logdir, name + ".jsonl")
        self.proc = subprocess.Popen([sys.executable, script, "--log", self.log, *args], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
        self.path = self.proc.stdout.readline().strip()
        if not self.path.startswith("/dev/pts/"):
            self.stop()
            raise RuntimeError(f"{name} did not start: {self.path!r}")

    def entries(self, since=0):
        out = []
        try:
            with open(self.log) as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass                # (a line still being written)
        except FileNotFoundError:
            pass
        return out[since:]

    def control(self, text):
        """fake_pendant.py's controls ("key 5", "key 15 double" ...), on its stdin."""
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


class Server:
    def __init__(self, port, ports, workdir):
        env = dict(os.environ, FLUIDSENDER_EXTRA_PORTS=":".join(ports), FLUIDSENDER_ONLY_EXTRA_PORTS="1",
                   FLUIDSENDER_RELEASE_AFTER=str(RELEASE),
                   FLUIDSENDER_SETTINGS=os.path.join(workdir, "settings.json"),   # (never the real ones)
                   FLUIDSENDER_SCANS=os.path.join(workdir, "scans"))
        self.port = port
        self.proc = subprocess.Popen([sys.executable, SERVER, "--no-browser", "--port", str(port), "--dir", workdir],
                                     env=env, stdout=subprocess.DEVNULL)
        end = time.monotonic() + 10
        while True:
            try:
                if self.call("/api/machine/state")[0] == 200:
                    return
            except OSError:
                pass
            if time.monotonic() > end or self.proc.poll() is not None:
                self.stop()
                raise RuntimeError(f"the server on port {port} did not start")
            time.sleep(0.1)

    def call(self, path, body=None, headers=H):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            if body is None:
                c.request("GET", path, headers=headers)
            else:
                c.request("POST", path, body=json.dumps(body), headers={**headers, "Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class Events:
    """Reads /api/machine/events in a thread, like the page does."""

    def __init__(self, port):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self.conn.connect()
        self.sock = self.conn.sock          # (http.client hands it to the response and forgets it)
        self.conn.request("GET", "/api/machine/events", headers=H)
        self.resp = self.conn.getresponse()
        self.content_type = self.resp.getheader("Content-Type")
        self.list = []
        self.cond = threading.Condition()
        self.done = False
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        try:
            for raw in self.resp:
                ev = json.loads(raw)
                with self.cond:
                    self.list.append(ev)
                    self.cond.notify_all()
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            with self.cond:
                self.done = True
                self.cond.notify_all()

    def mark(self):
        with self.cond:
            return len(self.list)

    def wait(self, pred, timeout=5.0, since=0, what="an event"):
        """The first event from index `since` on that matches, and the index after it."""
        end = time.monotonic() + timeout
        i = since
        with self.cond:
            while True:
                while i < len(self.list):
                    ev = self.list[i]
                    i += 1
                    if pred(ev):
                        return ev, i
                left = end - time.monotonic()
                if left <= 0 or self.done:
                    tail = json.dumps(self.list[-8:])[:1500]
                    raise AssertionError(f"no {what} within {timeout} s; last events: {tail}")
                self.cond.wait(left)

    def find(self, pred, since=0):
        with self.cond:
            return [e for e in self.list[since:] if pred(e)]

    def close(self):
        """Closes the stream like a page that goes away."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(5)                 # the reader sees the end of the stream and stops
        self.resp.close()
        self.sock.close()


def console(kind, text=None, contains=None):
    return lambda e: (e["type"] == "console" and e["dir"] == kind and (text is None or e["text"] == text)
                      and (contains is None or contains in e["text"]))


def status(state=None, **fields):
    return lambda e: (e["type"] == "status" and (state is None or e["state"] == state)
                      and all(e.get(k) == v for k, v in fields.items()))


def conn(connected):
    return lambda e: e["type"] == "conn" and e["connected"] is connected


def modal(units):
    return lambda e: e["type"] == "modal" and e["units"] == units


def jog_event(ended=False):
    return lambda e: e["type"] == "jog" and (e["jog"] is None) == ended


def idle_at(axis, inches):
    """An Idle status event with that work position (wpos is null until FluidNC has sent its WCO)."""
    return lambda e: (e["type"] == "status" and e["state"] == "Idle" and e["wpos"] is not None
                      and abs(e["wpos"]["XYZ".index(axis)] - inches) < 1e-4)


def job_state(*states):
    return lambda e: e["type"] == "job" and e["job"] and e["job"]["state"] in states


def scan_phase(*phases):
    return lambda e: e["type"] == "scan" and e["scan"] and e["scan"]["phase"] in phases


# the probe's touches for the 3 x 2 scan, in the order probed (column by column, zigzag in Y): it reports
# each relative to the first
SURFACE = (0.5, 0.51, 0.49, 0.52, 0.5, 0.47)
SCAN_3X2 = {"minX": 0, "maxX": 0.2, "minY": 0, "maxY": 0.1, "xPoints": 3, "yPoints": 2, "zRetract": 0.2}
XYZ_3X2 = ("# minX=0.00000 maxX=0.20000\n# minY=0.00000 maxY=0.10000\n# xSamples=3 ySamples=2\n# units=INCH\n"
           "0.0 0.0 0.0\n0.0 0.1 0.01\n0.1 0.0 0.02\n0.1 0.1 -0.01\n0.2 0.0 0.0\n0.2 0.1 -0.03\n")


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sims = []
        cls.tools = []
        try:
            cls.sim = cls.start_sim("normal", "--banner-delay", "0.2")
            cls.alarm = cls.start_sim("alarm", "--alarm")
            cls.home = cls.start_sim("home", "--alarm")
            cls.stall = cls.start_sim("stall", "--stall-after", str(3 + len(READS)))
            cls.unplug = cls.start_sim("unplug")
            cls.s2 = cls.start_sim("stage2", "--soft-limits")     # (stage 2: jog, zero; reports mm)
            cls.s2in = cls.start_sim("inches", "--inches")        # report_inches on, like the real machine
            cls.nos = cls.start_sim("nosettings", "--no-settings")
            cls.homing = cls.start_sim("homing", "--alarm")
            cls.limit = cls.start_sim("limit", "--alarm")
            # stage 3 + 4: like the user's machine, they report inches
            cls.jobs = cls.start_sim("jobs", "--inches")
            cls.crash = cls.start_sim("crash", "--inches")
            cls.cool = cls.start_sim("coolant", "--inches", "--fw", "3.9.4")   # (3.9.4: Ov every 10th report)
            cls.cool411 = cls.start_sim("coolant411", "--inches")
            cls.noflood = cls.start_sim("noflood", "--inches", "--no-flood")
            cls.nopins = cls.start_sim("nopins", "--inches", "--no-flood", "--no-settings")   # (pins not known)
            # (--late-events: a line that comes with a toggle runs before it, as in FluidNC 4.1.1's main loop)
            cls.gpio = cls.start_sim("gpio", "--inches", "--no-readback", "--coolant-delay", "250", "--late-events")
            cls.ovr = cls.start_sim("overrides", "--inches")
            cls.vfd = cls.start_sim("vfd", "--inches", "--spindle", "vfd", "--vfd-sync", "1.0", "--late-events")
            cls.crit = cls.start_sim("critical", "--inches")
            cls.v394 = cls.start_sim("fw394", "--fw", "3.9.4", "--inches")
            cls.go = cls.start_sim("goto", "--inches")
            cls.pcnc = cls.start_sim("pendantcnc", "--inches")
            cls.scnc = cls.start_sim("scancnc", "--inches")
            cls.held = cls.start_sim("held", "--inches")      # left holding lines by another program (test_xn)
            cls.limitjob = cls.start_sim("limitjob", "--inches")    # a hard limit in a job, no window (test_xlb)
            cls.pendant = cls.start_tool("pendant", PENDANT, "--boot-delay", "0")
            # (the fixed firmware sends its Stop for a scan started on the page too: test_xla)
            cls.pendant2 = cls.start_tool("pendant2", PENDANT, "--boot-delay", "0", "--firmware", "fixed")
            cls.probe = cls.start_tool("probe", PROBE, "--values", ",".join(map(str, SURFACE)))
            cls.probe2 = cls.start_tool("probe2", PROBE, "--probe-time", "2")
            cls.pendant3 = cls.start_tool("pendant3", PENDANT, "--boot-delay", "0", "--firmware", "fixed")  # (xlb)
            cls.probe3 = cls.start_tool("probe3", PROBE)
            cls.server = cls.start_server()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def start_sim(cls, name, *args):
        sim = Sim(cls.tmp.name, name, *args)
        cls.sims.append(sim)
        return sim

    @classmethod
    def start_tool(cls, name, script, *args):
        tool = Tool(cls.tmp.name, name, script, *args)
        cls.tools.append(tool)
        return tool

    @classmethod
    def start_server(cls):
        return Server(PORT, [s.path for s in cls.sims] + [t.path for t in cls.tools], cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None):
            cls.server.stop()
        for sim in cls.sims:
            sim.stop()
        for tool in cls.tools:
            tool.stop()
        cls.tmp.cleanup()

    # -------------------------------------------------- helpers

    def call(self, path, body=None, headers=H):
        return self.server.call(path, body, headers)

    def events(self):
        ev = Events(PORT)
        self.addCleanup(ev.close)
        snap, _ = ev.wait(lambda e: e["type"] == "snapshot", what="snapshot")
        self.assertEqual(ev.content_type, "application/x-ndjson")
        return ev, snap

    def connect(self, ev, sim, inches=False):
        """Connects; with inches=True also waits until FluidSender's own G20 has been answered."""
        st, data = self.call("/api/machine/connect", {"port": sim.path, "baud": 115200})
        self.assertEqual((st, data), (200, {"ok": True}))
        self.addCleanup(self.call, "/api/machine/disconnect", {})
        c, i = ev.wait(conn(True), what="conn event")
        self.assertEqual(c["port"], sim.path)
        if inches:
            _, j = ev.wait(console("tx", "G20"), since=i, what="G20")
            ev.wait(console("rx", "ok"), since=j, what="ok for G20")
        return i

    def send(self, line):
        st, data = self.call("/api/machine/send", {"line": line})
        self.assertEqual((st, data), (200, {"ok": True}), line)

    def realtime(self, cmd):
        st, data = self.call("/api/machine/realtime", {"cmd": cmd})
        self.assertEqual((st, data), (200, {"ok": True}), cmd)

    def assertNear(self, got, want, tol=1e-4):
        self.assertEqual(len(got), len(want))
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, delta=tol, msg=f"{got} != {want}")

    def state(self):
        return self.call("/api/machine/state")[1]

    def jog(self, **body):
        return self.call("/api/machine/jog", body)

    def ready(self, ev, sim):
        """Connects and waits until FluidSender's own lines are done (G20 answered) and the work position
        is known (FluidNC sends WCO in every 10th report)."""
        i = self.connect(ev, sim, inches=True)
        # (FluidNC sends WCO in every 10th report, every 30th while busy: up to 6 s after a busy spell)
        ev.wait(lambda e: e["type"] == "status" and e["wpos"] is not None, since=i, timeout=8, what="a work position")
        return i

    def wait_idle_at(self, wpos=None, tol=0.0, timeout=6.0):
        """Waits until the latest report says Idle (at that work position, inches, if given)."""
        end = time.monotonic() + timeout
        while True:
            st = self.state()["status"]
            if (st and st["state"] == "Idle" and (wpos is None or st["wpos"]
                                                  and all(abs(g - w) <= tol for g, w in zip(st["wpos"], wpos)))):
                return st
            self.assertLess(time.monotonic(), end, f"not Idle at {wpos}: {st}")
            time.sleep(0.05)

    def to_work_zero(self, ev, tol):
        """G0 X0 Y0 Z0: an exact starting point (the work zero is a whole number of millimeters)."""
        k = ev.mark()
        self.send("G0 X0 Y0 Z0")
        _, k = ev.wait(console("tx", "G0 X0 Y0 Z0"), since=k)
        ev.wait(console("rx", "ok"), since=k, what="ok for G0 X0 Y0 Z0")
        return self.wait_idle_at([0, 0, 0], tol)

    def hold(self, ev, direction, feed, seconds):
        """A held jog button: start, keep every 100 ms for that long, then stop. Returns the start's reply
        and the console line that tells how far it moved."""
        k = ev.mark()
        st, data = self.jog(start=direction, feed=feed)
        self.assertEqual(st, 200, data)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.assertEqual(self.jog(keep=data["id"]), (200, {"ok": True}))
            time.sleep(0.1)
        self.assertEqual(self.jog(stop=data["id"]), (200, {"ok": True}))
        ev.wait(jog_event(ended=True), since=k, what="the jog's end")
        done, _ = ev.wait(console("info", contains="Jog stopped"), since=k, timeout=3, what="Jog stopped")
        self.wait_idle_at()
        return data, done

    def check_steps(self, sim, tol):
        """Step jogs move exactly what was asked: the DRO (work position, inches) after each one."""
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, tol)
        pos = [0.0, 0.0, 0.0]
        for step, line in (({"x": 0.1}, "$J=G20G91X0.1F150"),
                           ({"x": 0.001}, "$J=G20G91X0.001F150"),
                           ({"y": -0.1}, "$J=G20G91Y-0.1F150"),
                           ({"z": 0.01}, "$J=G20G91Z0.01F78.7"),      # (Z's max rate: 78.74 in/min)
                           ({"x": -0.101, "y": 0.1, "z": -0.01}, "$J=G20G91X-0.101Y0.1Z-0.01F150")):
            k = ev.mark()
            self.assertEqual(self.jog(step=step, feed=150), (200, {"ok": True, "line": line}))
            ev.wait(console("tx", line), since=k)
            pos = [p + step.get(a, 0) for p, a in zip(pos, "xyz")]
            self.wait_idle_at(pos, tol)
        return ev

    # -------------------------------------------------- tests (run in name order)

    def test_1_ports_holders_and_access(self):
        st, data = self.call("/api/machine/state")
        self.assertEqual(st, 200)
        self.assertFalse(data["connected"])               # the server never connects by itself
        self.assertEqual(set(data), {"connected", "port", "firmware", "status", "modal", "console",
                                     "axes", "jog", "zeroUndo", "alarm", "program", "job", "jogSettings",
                                     "probe", "scan", "pendant"})
        self.assertEqual((data["program"], data["job"], data["jogSettings"]), (None, None, {"step": 0.1, "feed": 150}))
        self.assertEqual((data["probe"]["connected"], data["scan"]["running"], data["pendant"]["state"]),
                         (False, False, "off"))

        st, data = self.call("/api/machine/ports")
        self.assertEqual(st, 200)
        self.assertEqual([p["path"] for p in data["ports"]],                # only the test ports
                         [s.path for s in self.sims] + [t.path for t in self.tools])
        self.assertIsNone(data["default"])                # no ttyUSB here
        p = data["ports"][0]
        self.assertEqual((p["kind"], p["label"], p["inUseBy"]), ("pty", "Pseudo terminal (simulator?)", None))

        fd = os.open(self.sim.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)   # this test process holds the port
        try:
            me = f"(pid {os.getpid()})"
            st, data = self.call("/api/machine/ports")
            self.assertIn(me, data["ports"][0]["inUseBy"])
            st, data = self.call("/api/machine/connect", {"port": self.sim.path, "baud": 115200})
            self.assertEqual(st, 409)
            self.assertIn("in use by python3", data["error"])
            self.assertIn(me, data["error"])
            self.assertTrue(data["error"].endswith("Disconnect it there first."))
        finally:
            os.close(fd)
        st, data = self.call("/api/machine/ports")
        self.assertIsNone(data["ports"][0]["inUseBy"])
        self.assertFalse(self.call("/api/machine/state")[1]["connected"])

        # the API answers only the page itself
        self.assertEqual(self.call("/api/machine/state", headers={})[0], 403)
        self.assertEqual(self.call("/api/machine/state", headers={**H, "Host": "evil.example:8790"})[0], 403)
        self.assertEqual(self.call("/api/machine/connect", {"port": self.sim.path}, headers={})[0], 403)
        self.assertEqual(self.call("/api/machine/nothing", {})[0], 404)
        st, data = self.call("/api/machine/connect", {"port": "/dev/ttyUSB99"})
        self.assertEqual(st, 400)
        self.assertIn("not one of the test ports", data["error"])
        self.assertEqual(self.call("/api/machine/send", {"line": "G0 X1"}), (409, {"error": "Not connected"}))
        self.assertEqual(self.call("/api/machine/realtime", {"cmd": "hold"}), (409, {"error": "Not connected"}))
        self.assertEqual(self.call("/api/machine/disconnect", {}), (200, {"ok": True}))   # harmless when not connected

    def test_2_session(self):
        sim = self.sim
        ev, snap = self.events()
        self.assertFalse(snap["connected"])
        log0 = len(sim.entries())
        i = self.connect(ev, sim)

        # connect sequence: $I (firmware), $G (modes), then G20 once FluidNC is Idle
        ev.wait(status("Idle"), since=i, what="first status")
        fw, _ = ev.wait(lambda e: e["type"] == "conn" and e["firmware"], since=i, what="firmware")
        self.assertEqual(fw["firmware"], FW411)
        ev.wait(modal("G21"), since=i, what="modal G21 from $G")
        _, j = ev.wait(console("info", "*** Setting units to inches (G20)"), since=i)
        ev.wait(console("tx", "G20"), since=j - 1)
        m, _ = ev.wait(modal("G20"), since=i, what="modal G20")
        self.assertEqual(m["distance"], "G90")
        self.assertIn("G20", m["gc"].split())
        time.sleep(1.0)
        self.assertEqual(len(ev.find(console("tx", "G20"))), 1)
        self.assertEqual(sim.lines(log0), ["$I", "$G", *READS, "G20"])             # all FluidSender sent by itself ...
        self.assertEqual(set(sim.realtime(log0)), {"?"})                  # ... plus status requests
        st = self.call("/api/machine/state")[1]
        self.assertTrue(st["connected"])
        self.assertEqual(st["port"], sim.path)
        self.assertEqual(st["modal"]["units"], "G20")
        self.assertNear(st["status"]["wpos"], [0, 0, 0])
        self.assertEqual([e["text"] for e in st["console"] if e["dir"] == "tx"][-3:], ["$I", "$G", "G20"])

        # the port is held exclusively
        with self.assertRaises(OSError) as cm:
            os.open(sim.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.assertEqual(cm.exception.errno, errno.EBUSY)
        self.assertEqual(self.call("/api/machine/connect", {"port": sim.path})[0], 409)   # already connected

        # $G now reports inches
        k = ev.mark()
        self.send("$G")
        gc, _ = ev.wait(console("rx", contains="[GC:"), since=k)
        self.assertIn("G20", gc["text"].split())

        # a move: Run, then Idle at the expected position, in inches
        k = ev.mark()
        self.send("G0 X1 Y2 Z0.5")
        _, k = ev.wait(status("Run"), since=k, what="Run")
        idle, _ = ev.wait(status("Idle"), since=k, timeout=5, what="Idle after the move")
        self.assertNear(idle["wpos"], [1, 2, 0.5])
        self.assertNear(idle["mpos"], [1 + WCO[0] / 25.4, 2 + WCO[1] / 25.4, 0.5 + WCO[2] / 25.4])
        self.assertEqual((idle["feed"], idle["spindle"], idle["stalled"], idle["abc"]), (0, 0, False, []))
        self.assertTrue(idle["raw"].startswith("<Idle|MPos:"))
        runs = ev.find(status("Run"), since=k - 1)
        self.assertTrue(all(abs(r["feed"] - 5000 / 25.4) < 0.01 for r in runs))   # G0 runs at 5000 mm/min

        # feed hold and resume during a slow move (2 in at 20 in/min)
        k = ev.mark()
        self.send("G1 X3 F20")
        run, k = ev.wait(status("Run"), since=k, what="Run")
        self.assertAlmostEqual(run["feed"], 20.0, places=3)
        self.realtime("hold")
        _, k = ev.wait(status("Hold"), since=k, what="Hold")
        _, k = ev.wait(status("Hold", sub="0"), since=k, what="Hold:0")
        x1 = self.call("/api/machine/state")[1]["status"]["wpos"][0]
        time.sleep(0.5)
        held = self.call("/api/machine/state")[1]["status"]
        self.assertEqual((held["state"], held["sub"], held["wpos"][0]), ("Hold", "0", x1))
        self.realtime("resume")
        _, k = ev.wait(status("Run"), since=k, what="Run after resume")
        time.sleep(0.3)
        self.assertGreater(self.call("/api/machine/state")[1]["status"]["wpos"][0], x1)

        # soft reset while held: banner -> reset handling -> G21 -> G20 again once Idle
        self.realtime("hold")
        _, k = ev.wait(status("Hold", sub="0"), since=k, what="Hold:0")
        log1 = len(sim.entries())
        self.realtime("reset")
        ev.wait(console("rx", contains="for help]"), since=k, what="banner")
        _, k2 = ev.wait(console("info", "*** Controller reset"), since=k)
        ev.wait(modal("G21"), since=k, what="modal G21 after reset")
        ev.wait(console("tx", "G20"), since=k2, what="G20 after reset")
        ev.wait(modal("G20"), since=k2, what="modal G20 after reset")
        ev.wait(status("Idle"), since=k2, what="Idle after reset")
        time.sleep(0.6)
        self.assertEqual(len(ev.find(console("tx", "G20"), since=k)), 1)
        self.assertEqual(sim.lines(log1), ["G20"])
        self.assertEqual(sim.realtime(log1).count("^X"), 1)

        # lines FluidNC must not get are refused; unknown G-code is FluidNC's business
        for bad in ("G0 X1 !", "G0 X1 ~", "G0 (why?)", "G0\tX1", "G0 X" + "1" * 300, "", "G0 X1 (°)"):
            st, data = self.call("/api/machine/send", {"line": bad})
            self.assertEqual(st, 400, bad)
            self.assertIn("error", data)
        self.assertEqual(self.call("/api/machine/send", {})[0], 400)
        self.assertEqual(self.call("/api/machine/realtime", {"cmd": "bogus"})[0], 400)
        k = ev.mark()
        self.send("G99")
        ev.wait(console("rx", "error:20"), since=k)
        self.send("G0 X0 Y0 Z0")
        _, k = ev.wait(status("Run"), since=k, what="Run")
        idle, _ = ev.wait(status("Idle"), since=k, what="Idle at zero")
        self.assertNear(idle["wpos"], [0, 0, 0])

        # disconnect: the port is free again, and nothing more can be sent
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/disconnect", {}), (200, {"ok": True}))
        c, _ = ev.wait(conn(False), since=k, what="disconnect event")
        self.assertEqual(c, {"type": "conn", "connected": False, "port": None, "firmware": None,
                             "message": f"Disconnected from {sim.path}"})
        st = self.call("/api/machine/state")[1]
        self.assertEqual((st["connected"], st["port"], st["status"], st["modal"]), (False, None, None, None))
        os.close(os.open(sim.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))
        self.assertEqual(self.call("/api/machine/send", {"line": "G0 X1"})[0], 409)

    def test_3_alarm_waits_for_unlock(self):
        sim = self.alarm
        ev, _ = self.events()
        log0 = len(sim.entries())
        i = self.connect(ev, sim)
        ev.wait(status("Alarm"), since=i, what="Alarm")
        ev.wait(console("info", "Machine is in ALARM: home ($HZ) or unlock ($X) from the console"), since=i - 2)
        ev.wait(modal("G21"), since=i, what="modal from $G")
        time.sleep(1.5)
        self.assertEqual(ev.find(console("tx", "G20")), [])              # not while in Alarm
        self.assertEqual(sim.lines(log0), ["$I", "$G", "$T", *READS])   # ($T: a plain alarm, not a critical one)
        ev.wait(console("rx", "State 1 (Alarm)"), since=i)
        self.assertIsNone(self.state()["alarm"])

        k = ev.mark()
        self.send("G0 X1")
        ev.wait(console("rx", "error:9"), since=k)
        self.send("$X")
        ev.wait(console("rx", "[MSG:INFO: Caution: Unlocked]"), since=k)
        _, k = ev.wait(status("Idle"), since=k, what="Idle after $X")
        ev.wait(console("tx", "G20"), since=k - 1, what="G20 after unlock")
        ev.wait(modal("G20"), since=k, what="modal G20")
        self.assertEqual(sim.lines(log0), ["$I", "$G", "$T", *READS, "G0 X1", "$X", "G20"])

    def test_3b_typed_ahead_gcode_waits_for_g20(self):
        """G-code typed before FluidSender's G20 must not run in millimeters: while homing after connecting
        in Alarm, right after a soft reset, and right after connecting."""
        sim = self.home
        ev, _ = self.events()
        log0 = len(sim.entries())
        i = self.connect(ev, sim)
        ev.wait(status("Alarm"), since=i, what="Alarm")
        ev.wait(modal("G21"), since=i, what="modal from $G")
        k = ev.mark()
        self.send("$H")                      # ($HZ alone leaves X and Y unhomed: still Alarm)
        _, k = ev.wait(status("Home"), since=k, what="Home")
        self.send("G0 Z0.5")                 # typed while Z homes
        ev.wait(console("info", "Waiting to send: G0 Z0.5  (after '$H')"), since=k)
        _, k = ev.wait(console("tx", "G0 Z0.5"), since=k, what="G0 Z0.5 sent")
        ev.wait(idle_at("Z", 0.5), since=k, what="Idle at Z 0.5 in")
        self.assertEqual(sim.lines(log0), ["$I", "$G", "$T", *READS, "$H", "G20", "G0 Z0.5"])

        log1 = len(sim.entries())
        k = ev.mark()
        self.realtime("reset")
        ev.wait(console("rx", contains="for help]"), since=k, what="banner")
        self.send("G0 X1")                   # FluidNC is in G21 again until G20 has gone out
        ev.wait(idle_at("X", 1), since=k, what="Idle at X 1 in")
        self.assertEqual(sim.lines(log1), ["G20", "G0 X1"])

        self.assertEqual(self.call("/api/machine/disconnect", {}), (200, {"ok": True}))
        ev.wait(conn(False), since=k, what="disconnected")
        log2 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/connect", {"port": sim.path, "baud": 115200}), (200, {"ok": True}))
        self.send("G0 X2")                   # before $I and $G are even answered
        ev.wait(idle_at("X", 2), since=k, what="Idle at X 2 in")
        self.assertEqual(sim.lines(log2), ["$I", "$G", *READS, "G20", "G0 X2"])

    def test_4_stall(self):
        sim = self.stall                     # answers $I, $G, the settings reads and G20, then no more lines
        ev, _ = self.events()
        self.connect(ev, sim, inches=True)
        time.sleep(0.3)
        log0 = len(sim.entries())
        k = ev.mark()
        t0 = time.monotonic()
        self.send("G0 X1")
        self.send("G0 X2")                   # waits behind the unanswered line
        stalled, k2 = ev.wait(status(stalled=True), since=k, timeout=8, what="stalled=true")
        self.assertGreaterEqual(time.monotonic() - t0, 4.9)
        self.assertEqual(stalled["state"], "Idle")                     # status still answers
        self.assertEqual(sim.lines(log0), ["G0 X1"])                   # nothing else was sent ...
        self.assertEqual(set(sim.realtime(log0)), {"?"})               # ... except status requests
        self.assertEqual([e["text"] for e in ev.find(console("tx"), since=k)], ["G0 X1"])

        err, k3 = ev.wait(console("error", contains="No reply to 'G0 X1' within 10 s"), since=k2, timeout=7)
        self.assertIn("dropped the 1 line waiting after it", err["text"])
        time.sleep(0.5)
        self.assertEqual(sim.lines(log0), ["G0 X1"])                   # the waiting line was dropped, not sent
        self.assertTrue(self.call("/api/machine/state")[1]["status"]["stalled"])   # and G0 X1 is still unanswered

        # lines sent now wait for that answer (a late ok must not answer them); a soft reset gives up on it
        self.send("G0 X3")
        ev.wait(console("info", "Waiting to send: G0 X3  (FluidNC has not answered 'G0 X1' yet; Soft Reset "
                        "drops it)"), since=k3)
        self.send("G0 X4")
        time.sleep(0.5)
        self.assertEqual(sim.lines(log0), ["G0 X1"])
        k = ev.mark()
        self.realtime("reset")
        ev.wait(console("info", "Soft reset: dropped 2 waiting lines"), since=k)
        ev.wait(console("info", "*** Controller reset"), since=k)
        ev.wait(console("tx", "G20"), since=k, what="G20 after reset")
        ev.wait(status(stalled=False), since=k, what="stalled=false after the reset")
        time.sleep(0.3)
        self.assertEqual(sim.lines(log0), ["G0 X1", "G20"])

    def test_5_released_when_no_page_is_open(self):
        sim = self.sim
        ev, _ = self.events()
        self.connect(ev, sim, inches=True)
        k = ev.mark()
        self.send("G91 G1 X1 F15")           # 4 s; FluidNC answers it as soon as it is planned
        ev.wait(status("Run"), since=k, what="Run")
        ev.close()                           # the last page goes away while the machine moves
        t0 = time.monotonic()
        while True:                          # kept while it moves: Feed Hold must stay possible
            st = self.call("/api/machine/state")[1]
            self.assertTrue(st["connected"], "the port was released while the machine was moving")
            if st["status"]["state"] != "Run":
                break
            self.assertLess(time.monotonic() - t0, 8, "the move did not end")
            time.sleep(0.1)
        self.assertGreater(time.monotonic() - t0, RELEASE + 0.5)
        t0 = time.monotonic()                # at rest from about now
        while self.call("/api/machine/state")[1]["connected"]:
            self.assertLess(time.monotonic() - t0, RELEASE + 4, "the port was not released")
            time.sleep(0.1)
        self.assertGreaterEqual(time.monotonic() - t0, RELEASE - 0.5)
        st = self.call("/api/machine/state")[1]
        self.assertIn("Released the port because no FluidSender window is open",
                      [e["text"] for e in st["console"] if e["dir"] == "info"])
        os.close(os.open(sim.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))   # free again
        time.sleep(1.0)
        self.assertFalse(self.call("/api/machine/state")[1]["connected"])  # and no reconnecting by itself

    def test_6_no_response(self):
        sim = self.sim
        ev, _ = self.events()
        i = self.connect(ev, sim, inches=True)
        last, _ = ev.wait(lambda e: e["type"] == "status" and e["wpos"], since=i, timeout=8,
                          what="a work position (first WCO)")
        log0 = len(sim.entries())
        k = ev.mark()
        sim.proc.send_signal(signal.SIGSTOP)        # the controller stops answering anything
        try:
            nr, k = ev.wait(status("No response"), since=k, timeout=4, what="No response")
            self.assertEqual((nr["wpos"], nr["mpos"], nr["stalled"]), (last["wpos"], last["mpos"], False))  # last known
        finally:
            sim.proc.send_signal(signal.SIGCONT)
        ev.wait(status("Idle"), since=k, what="Idle again")
        self.assertTrue(self.call("/api/machine/state")[1]["connected"])
        self.assertEqual(sim.lines(log0), [])       # nothing but '?' was sent meanwhile
        self.assertEqual(set(sim.realtime(log0)), {"?"})

    def test_7_unplugged(self):
        sim = self.unplug
        ev, _ = self.events()
        self.connect(ev, sim, inches=True)
        k = ev.mark()
        sim.proc.kill()                              # the pty disappears, like a pulled USB cable
        sim.proc.wait()
        c, _ = ev.wait(conn(False), since=k, what="lost connection")
        self.assertTrue(c["message"].startswith(f"Lost the connection to {sim.path}"), c["message"])
        ev.wait(console("error", c["message"]), since=k)
        time.sleep(1.5)
        st = self.call("/api/machine/state")[1]
        self.assertFalse(st["connected"])            # and no reconnecting by itself
        self.assertEqual(ev.find(conn(True), since=k), [])

    def test_8_a_line_behind_a_move_is_not_stalled(self):
        """FluidNC answers M5 only once the move before it is done: that is neither a stall nor a timeout."""
        sim = self.sim
        ev, _ = self.events()
        self.connect(ev, sim, inches=True)
        k = ev.mark()
        self.send("G91 G1 X1 F10")           # 6 s
        self.send("M5")
        _, k = ev.wait(console("tx", "M5"), since=k, what="M5 sent")
        t0 = time.monotonic()
        ev.wait(console("rx", "ok"), since=k, timeout=10, what="M5's ok")
        self.assertGreater(time.monotonic() - t0, 5.5)
        self.assertEqual(ev.find(status(stalled=True), since=k), [])
        self.assertEqual(ev.find(console("error"), since=k), [])
        self.send("G90")

    # -------------------------------------------------- stage 2: jog, zero, home, unlock

    def test_9a_settings_read_at_connect(self):
        sim = self.s2
        ev, snap = self.events()
        self.assertEqual((snap["axes"], snap["jog"], snap["zeroUndo"], snap["alarm"]), (NO_AXES, None, None, None))
        log0 = len(sim.entries())
        i = self.connect(ev, sim, inches=True)
        ax, _ = ev.wait(lambda e: e["type"] == "axes", since=i, what="axes event")
        want = {"x": {"maxRate": 196.85, "accel": 7.874, "softLimits": True, "travel": [-31.4961, 0.0]},       # 5000 / 2000 mm/min,
                "y": {"maxRate": 196.85, "accel": 7.874, "softLimits": True, "travel": [-23.622, 0.0]},       # 200 / 100 mm/s^2,
                "z": {"maxRate": 78.74, "accel": 3.937, "softLimits": True, "travel": [-3.937, 0.0]}}        # --soft-limits
        self.assertEqual(ax, {"type": "axes", "axes": want, "plannerBlocks": 16})
        ev.wait(console("info", "FluidNC max speeds: X 196.9 · Y 196.9 · Z 78.7 in/min"), since=i)
        self.wait_for(lambda: "G20" in sim.lines(log0), 3, "G20 at FluidNC")    # (the sim logs it a bit later)
        self.assertEqual(sim.lines(log0), ["$I", "$G", *READS, "G20"])
        # quiet: neither the reads nor their answers are in the console (only $I's, $G's and G20's oks)
        shown = [(e["dir"], e["text"]) for e in ev.find(lambda e: e["type"] == "console", since=i)]
        self.assertEqual([t for d, t in shown if d == "tx"], ["$I", "$G", "G20"])
        self.assertEqual([t for d, t in shown if d == "rx" and (t == "ok" or t.startswith("$/"))], ["ok"] * 3)
        st = self.state()
        self.assertEqual((st["axes"], st["jog"], st["zeroUndo"], st["alarm"]), (want, None, None, None))

        # disconnecting forgets them
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/disconnect", {}), (200, {"ok": True}))
        ax, _ = ev.wait(lambda e: e["type"] == "axes", since=k, what="axes event after disconnect")
        self.assertEqual(ax["axes"], NO_AXES)
        self.assertEqual(self.state()["axes"], NO_AXES)

    def test_9b_settings_fallback(self):
        """A FluidNC that answers no $/ query: the errors are shown with their lines, no speed cap here,
        and hold jogs use the fallback piece size (50 mm/s^2, 16 planner blocks)."""
        sim = self.nos
        ev, _ = self.events()
        i = self.ready(ev, sim)
        ax, _ = ev.wait(lambda e: e["type"] == "axes", since=i, what="axes event")
        self.assertEqual(ax, {"type": "axes", "axes": NO_AXES, "plannerBlocks": None})
        ev.wait(console("info", "Could not read FluidNC's max speeds: jog speeds are not capped here "
                        "(FluidNC still caps them)"), since=i)
        shown = [(e["dir"], e["text"]) for e in ev.find(lambda e: e["type"] == "console", since=i)]
        for q in SETTINGS:
            if "max_travel_mm" in q or "/homing/" in q:      # (optional: their errors are not shown)
                self.assertNotIn(("tx", q), shown)
                continue
            n = shown.index(("tx", q))
            self.assertEqual(shown[n + 1], ("rx", "error:3"), q)
        self.to_work_zero(ev, 3e-5)

        k = ev.mark()
        self.assertEqual(self.jog(step={"x": 0.1}, feed=300), (200, {"ok": True, "line": "$J=G20G91X0.1F300"}))
        self.wait_idle_at([0.1, 0, 0], 3e-5)

        log0 = len(sim.entries())
        data, done = self.hold(ev, {"y": -1}, 150, 0.6)
        self.assertTrue(done["text"].startswith("Jog stopped: moved "), done["text"])
        self.assertEqual(set(sim.lines(log0)), {"$J=G20G91Y-0.158F150"})   # 4.01 mm pieces
        self.assertIn("jog-cancel", sim.realtime(log0))
        self.assertEqual(ev.find(console("error"), since=k), [])

    def test_9c_step_jogs_are_exact_mm_reports(self):
        self.check_steps(self.s2, 3e-5)      # (3 decimals of mm: 0.00002 in)

    def test_9d_jog_speed_is_capped_at_the_max_rates(self):
        sim = self.s2
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 3e-5)
        pos = [0.0, 0.0, 0.0]
        for step, feed, line in (({"x": 0.1}, 500, "$J=G20G91X0.1F196.8"),         # X 196.85 in/min, rounded down
                                 ({"z": 0.1}, 500, "$J=G20G91Z0.1F78.7"),          # Z 78.74
                                 ({"x": 0.1, "y": 0.1}, 500, "$J=G20G91X0.1Y0.1F278.3"),    # 45°: each at 196.85
                                 ({"x": -0.1, "z": -0.1}, 500, "$J=G20G91X-0.1Z-0.1F111.3"),   # Z sets the pace
                                 ({"x": -0.1, "y": -0.1}, 150, "$J=G20G91X-0.1Y-0.1F150")):   # under every cap
            self.assertEqual(self.jog(step=step, feed=feed), (200, {"ok": True, "line": line}))
            pos = [p + step.get(a, 0) for p, a in zip(pos, "xyz")]
            self.wait_idle_at(pos, 3e-5)

        # a hold jog's speed is capped too, and so are its pieces
        log0 = len(sim.entries())
        k = ev.mark()
        st, data = self.jog(start={"z": 1}, feed=150)
        self.assertEqual(st, 200, data)
        j, _ = ev.wait(jog_event(), since=k, what="jog event")
        self.assertEqual(j["jog"], {"id": data["id"], "dir": {"x": 0, "y": 0, "z": 1}, "feed": 78.7})
        ev.wait(console("info", "Jog Z+ at 78.7 in/min (hold)"), since=k)
        end = time.monotonic() + 2
        while not sim.lines(log0):                   # the first piece has gone out
            self.assertLess(time.monotonic(), end, "no jog piece sent")
            time.sleep(0.02)
        self.assertEqual(self.jog(stop="all"), (200, {"ok": True}))
        ev.wait(jog_event(ended=True), since=k, what="the jog's end")
        # (the last report may still be the Idle from before the jog's cycle started: wait for the cancel)
        ev.wait(console("info", contains="Jog stopped: moved "), since=k, timeout=3, what="Jog stopped")
        self.wait_idle_at()
        self.assertEqual(set(sim.lines(log0)), {"$J=G20G91Z0.039F78.7"})

    def test_9e_hold_jog_moves_while_kept_and_stops(self):
        sim = self.s2
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 3e-5)
        log0 = len(sim.entries())
        k = ev.mark()
        st, data = self.jog(start={"x": 1}, feed=150)
        self.assertEqual(st, 200, data)
        jid = data["id"]
        j, _ = ev.wait(jog_event(), since=k, what="jog event")
        self.assertEqual(j["jog"], {"id": jid, "dir": {"x": 1, "y": 0, "z": 0}, "feed": 150})
        self.assertEqual(self.state()["jog"], j["jog"])
        ev.wait(console("info", "Jog X+ at 150 in/min (hold)"), since=k)
        # while it runs nothing else goes (FluidNC answers G-code with error:9 while it jogs)
        for call, body in (("send", {"line": "G0 X0"}), ("zero", {"axes": "X"}), ("home", {"axes": ""}),
                           ("unlock", {}), ("jog", {"step": {"x": 0.1}, "feed": 150})):
            self.assertEqual(self.call("/api/machine/" + call, body), (409, {"error": JOGGING}), call)
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            self.assertEqual(self.jog(keep=jid), (200, {"ok": True}))
            time.sleep(0.1)
        ev.wait(status("Jog"), since=k, what="Jog")
        log1 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.jog(stop=jid), (200, {"ok": True}))
        ev.wait(jog_event(ended=True), since=k, what="the jog's end")
        done, _ = ev.wait(console("info", contains="Jog stopped: moved "), since=k, timeout=3)
        idle = self.wait_idle_at()
        time.sleep(0.5)
        self.assertEqual(self.state()["status"]["wpos"], idle["wpos"])          # it stays stopped
        x = idle["wpos"][0]
        self.assertGreater(x, 1.5)                   # about 1.2 s at 2.5 in/s, plus the slow-down
        self.assertLess(x, 6)
        self.assertNear(idle["wpos"][1:], [0, 0], 3e-5)
        self.assertAlmostEqual(float(done["text"].split()[-2]), x, delta=1e-3)
        # FluidNC got short pieces only, then jog cancels, and no $J after the first cancel
        pieces = sim.lines(log0)
        self.assertGreater(len(pieces), 20)
        self.assertEqual(set(pieces), {"$J=G20G91X0.073F150"})        # 1.85 mm pieces
        after = sim.entries(log1)
        self.assertIn("RT jog-cancel", after)
        self.assertEqual([e for e in after[after.index("RT jog-cancel"):] if e.startswith("LINE ")], [])
        # that jog is over
        self.assertEqual(self.jog(keep=jid), (409, {"error": "That jog has ended"}))
        self.assertEqual(self.jog(stop=jid), (200, {"ok": True}))      # (stopping twice is fine)
        self.assertIsNone(self.state()["jog"])
        self.assertEqual(ev.find(console("error"), since=k), [])

    def test_9f_deadman_stops_a_jog_the_page_stopped_keeping(self):
        sim = self.s2
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 3e-5)
        log0 = len(sim.entries())
        k = ev.mark()
        st, data = self.jog(start={"x": -1}, feed=150)
        t0 = time.monotonic()
        self.assertEqual(st, 200, data)              # ... and then no keep comes
        ev.wait(jog_event(ended=True), since=k, timeout=3, what="the deadman's stop")
        self.assertGreater(time.monotonic() - t0, 0.3)
        self.assertLess(time.monotonic() - t0, 1.5)
        ev.wait(console("info", contains="Jog stopped (the page stopped answering): moved "), since=k, timeout=3)
        idle = self.wait_idle_at()
        self.assertLess(idle["wpos"][0], -0.2)
        self.assertIn("jog-cancel", sim.realtime(log0))
        self.assertEqual(self.jog(keep=data["id"]), (409, {"error": "That jog has ended"}))

    def test_9g_gcode_right_after_a_jog_waits_until_it_stopped(self):
        sim = self.s2
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 3e-5)
        log0 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.jog(step={"x": 1}, feed=60), (200, {"ok": True, "line": "$J=G20G91X1F60"}))  # 1 s
        self.send("G1 X0.5 F100")               # at once: in Jog FluidNC would answer error:9
        ev.wait(console("info", "Waiting to send: G1 X0.5 F100  (until the jog has stopped)"), since=k)
        jog, _ = ev.wait(console("tx", "$J=G20G91X1F60"), since=k)
        g1, _ = ev.wait(console("tx", "G1 X0.5 F100"), since=k, timeout=5)
        self.assertGreater(g1["t"] - jog["t"], 0.9)       # sent once the jog was done
        self.wait_idle_at([0.5, 0, 0], 3e-5)
        self.assertEqual(ev.find(console("rx", contains="error"), since=k), [])
        self.assertEqual(sim.lines(log0), ["$J=G20G91X1F60", "G1 X0.5 F100"])

    def test_9h_zero_set_and_undo(self):
        sim = self.s2
        ev, _ = self.events()
        self.ready(ev, sim)
        self.send("G0 X0.5 Y0 Z-0.3")
        before = self.wait_idle_at([0.5, 0, -0.3], 3e-5)

        k = ev.mark()
        self.assertEqual(self.call("/api/machine/zero", {"axes": "x"}),
                         (200, {"ok": True, "line": "G10 L20 P0 G20 X0"}))
        z, _ = ev.wait(lambda e: e["type"] == "zero", since=k, what="zero event")
        undo = {"axes": "X", "text": "X 0.5", "wcs": "G54", "source": "page", "t": None, "count": 1}
        self.assertEqual(dict(z["zeroUndo"], t=None), undo)
        ev.wait(console("info", "Zeroed X. Work position before: X 0.5 (in)"), since=k)
        self.wait_idle_at([0, 0, -0.3], 3e-5)

        k = ev.mark()
        self.assertEqual(self.call("/api/machine/zero", {"axes": "Z", "value": 1.25}),
                         (200, {"ok": True, "line": "G10 L20 P0 G20 Z1.25"}))
        z, _ = ev.wait(lambda e: e["type"] == "zero", since=k, what="zero event")
        self.assertEqual(dict(z["zeroUndo"], t=None), dict(undo, axes="Z", text="Z -0.3", count=2))
        ev.wait(console("info", "Set Z to 1.25 in. Work position before: Z -0.3 (in)"), since=k)
        st = self.wait_idle_at([0, 0, 1.25], 3e-5)
        self.assertNear(st["mpos"], before["mpos"], 3e-5)          # nothing moved
        self.assertEqual(self.state()["zeroUndo"], z["zeroUndo"])

        # Undo Zero puts back the work offset from before the last zero (Z: G54 Z -30 mm), then the one before (X)
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/zero", {"undo": True}),
                         (200, {"ok": True, "line": "G10 L2 P1 G20 Z-1.181102"}))
        z, _ = ev.wait(lambda e: e["type"] == "zero", since=k, what="zero event")
        self.assertEqual(dict(z["zeroUndo"], t=None), undo)
        ev.wait(console("info", "Zero undone: the G54 work offset of Z is back to what it was before that zero. Work "
                                "position now: Z -0.3 (in). 1 older zero can still be undone"), since=k)
        self.wait_idle_at([0, 0, -0.3], 3e-5)
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/zero", {"undo": True}),
                         (200, {"ok": True, "line": "G10 L2 P1 G20 X-5.905512"}))
        z, _ = ev.wait(lambda e: e["type"] == "zero", since=k, what="zero event")
        self.assertIsNone(z["zeroUndo"])
        ev.wait(console("info", "Zero undone: the G54 work offset of X is back to what it was before that zero. Work "
                                "position now: X 0.5 (in)"), since=k)
        st = self.wait_idle_at([0.5, 0, -0.3], 3e-5)
        self.assertNear(st["mpos"], before["mpos"], 3e-5)          # (nothing moved)
        self.assertEqual(self.call("/api/machine/zero", {"undo": True}), (409, {"error": "No zero to undo"}))
        self.assertIsNone(self.state()["zeroUndo"])
        self.assertEqual(ev.find(console("rx", contains="error"), since=k), [])

    def test_9i_home_z_then_home_all(self):
        sim = self.homing
        ev, _ = self.events()
        i = self.connect(ev, sim)
        ev.wait(status("Alarm"), since=i, what="Alarm")
        ev.wait(lambda e: e["type"] == "axes", since=i, what="axes event")     # (read in Alarm too)
        why = "ALARM: Unlock or Home first"
        self.assertEqual(self.jog(step={"x": 0.1}, feed=150), (409, {"error": why}))
        self.assertEqual(self.jog(start={"x": 1}, feed=150), (409, {"error": why}))
        self.assertEqual(self.call("/api/machine/zero", {"axes": "X"}), (409, {"error": why}))
        self.assertEqual(self.call("/api/machine/home", {"axes": "W"})[0], 400)
        log0 = len(sim.entries())

        k = ev.mark()
        self.assertEqual(self.call("/api/machine/home", {"axes": "Z"}), (200, {"ok": True, "line": "$HZ"}))
        ev.wait(console("tx", "$HZ"), since=k)
        _, k2 = ev.wait(status("Home"), since=k, what="Home")
        _, k3 = ev.wait(console("rx", "[MSG:Homed:Z]"), since=k, timeout=5)
        ev.wait(console("rx", "ok"), since=k3, what="$HZ answered")
        st, _ = ev.wait(lambda e: e["type"] == "status" and e["state"] == "Alarm" and e["mpos"][2] == 0,
                        since=k2, what="Alarm at Z 0")    # X and Y still unhomed: FluidNC stays in Alarm
        self.assertNear(st["mpos"][:2], [WCO[0] / 25.4, WCO[1] / 25.4])

        k = ev.mark()
        self.assertEqual(self.call("/api/machine/home", {}), (200, {"ok": True, "line": "$H"}))
        _, k2 = ev.wait(status("Home"), since=k, what="Home")
        idle, _ = ev.wait(status("Idle"), since=k2, timeout=5, what="Idle after $H")
        self.assertNear(idle["mpos"], [0, 0, 0])
        ev.wait(console("tx", "G20"), since=k, what="G20 once Idle")
        self.assertEqual(self.call("/api/machine/unlock", {}), (409, {"error": "Not in ALARM: nothing to unlock"}))
        self.wait_for(lambda: "G20" in sim.lines(log0), 3, "G20 at FluidNC")    # (the sim logs it a bit later)
        self.assertEqual(sim.lines(log0), ["$HZ", "$H", "G20"])

    def test_9j_unlock_then_a_hard_limit_needs_a_soft_reset(self):
        sim = self.limit
        ev, _ = self.events()
        i = self.connect(ev, sim)
        ev.wait(status("Alarm"), since=i, what="Alarm")
        ev.wait(lambda e: e["type"] == "axes", since=i, what="axes event")
        log0 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/unlock", {}), (200, {"ok": True, "line": "$X"}))
        ev.wait(console("tx", "$X"), since=k)
        ev.wait(console("rx", "[MSG:INFO: Caution: Unlocked]"), since=k)
        ev.wait(status("Idle"), since=k, what="Idle after $X")
        _, k2 = ev.wait(console("tx", "G20"), since=k, what="G20 once Idle")
        ev.wait(console("rx", "ok"), since=k2)

        # a limit switch: critical, only a Soft Reset gets out of it
        k = ev.mark()
        sim.proc.send_signal(signal.SIGUSR1)
        a, _ = ev.wait(lambda e: e["type"] == "alarm" and e["alarm"] and e["alarm"]["critical"], since=k,
                       what="critical alarm")
        self.assertEqual(a["alarm"], {"code": 1, "critical": True, "text": "ALARM:1", "outputs": False})
        ev.wait(status("Alarm"), since=k, what="Alarm")
        for call, body in (("unlock", {}), ("home", {"axes": ""}), ("zero", {"axes": "X"}),
                           ("jog", {"step": {"x": 0.1}, "feed": 150}), ("jog", {"start": {"x": 1}, "feed": 150})):
            self.assertEqual(self.call("/api/machine/" + call, body), (409, {"error": CRITICAL}), call)
        self.assertEqual(sim.lines(log0), ["$X", "G20"])

        # Soft Reset: FluidNC prints ALARM:14 (the limit unhomed every axis), then its banner; not critical now
        k = ev.mark()
        self.realtime("reset")
        ev.wait(console("rx", "ALARM:14"), since=k)
        ev.wait(console("info", "*** Controller reset"), since=k)
        a, _ = ev.wait(lambda e: e["type"] == "alarm" and e["alarm"] and e["alarm"]["code"] == 14, since=k,
                       what="ALARM:14")
        self.assertFalse(a["alarm"]["critical"])
        self.assertEqual(self.state()["alarm"], {"code": 14, "critical": False, "text": "ALARM:14", "outputs": False})
        self.assertEqual(self.jog(step={"x": 0.1}, feed=150), (409, {"error": "ALARM: Unlock or Home first"}))
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/unlock", {}), (200, {"ok": True, "line": "$X"}))
        ev.wait(console("rx", "[MSG:INFO: Caution: Unlocked]"), since=k)
        ev.wait(lambda e: e["type"] == "alarm" and e["alarm"] is None, since=k, what="alarm cleared")
        ev.wait(status("Idle"), since=k, what="Idle")
        ev.wait(console("tx", "G20"), since=k, what="G20 once Idle")
        self.assertEqual(sim.lines(log0), ["$X", "G20", "$X", "G20"])
        self.assertEqual(sim.realtime(log0).count("^X"), 1)

    def test_9k_report_inches(self):
        """report_inches on (the user's machine): steps exact to 0.0001 in, jog feed reported in in/min."""
        ev = self.check_steps(self.s2in, 1e-6)
        k = ev.mark()
        st, data = self.jog(start={"y": 1}, feed=120)
        self.assertEqual(st, 200, data)
        end = time.monotonic() + 0.8
        while time.monotonic() < end:
            self.assertEqual(self.jog(keep=data["id"]), (200, {"ok": True}))
            time.sleep(0.1)
        jog, _ = ev.wait(status("Jog"), since=k, what="Jog")
        self.assertAlmostEqual(jog["feed"], 120, delta=0.5)
        self.assertEqual(self.jog(stop=data["id"]), (200, {"ok": True}))
        done, _ = ev.wait(console("info", contains="Jog stopped: moved "), since=k, timeout=3)
        idle = self.wait_idle_at()
        self.assertAlmostEqual(float(done["text"].split()[-2]), idle["wpos"][1], delta=2e-4)
        self.assertEqual(ev.find(console("error"), since=k), [])


    # -------------------------------------------------- stage 3 + 4: helpers

    def wait_for(self, cond, timeout, what):
        """Polls cond() until it gives something true; returns that."""
        end = time.monotonic() + timeout
        while True:
            got = cond()
            if got:
                return got
            self.assertLess(time.monotonic(), end, f"no {what} within {timeout} s")
            time.sleep(0.05)

    def press(self, path, body, timeout=5.0):
        """A button that may answer 409 "... try again in a second" (FluidNC hasn't reported since its last
        reply yet): pressed again then, as the user would."""
        end = time.monotonic() + timeout
        while True:
            st, data = self.call(path, body)
            if not (st == 409 and "try again" in data.get("error", "")) or time.monotonic() > end:
                return st, data
            time.sleep(0.1)

    def load(self, text, name="t.nc"):
        st, data = self.call("/api/machine/program", {"name": name, "text": text})
        self.assertEqual(st, 200, data)
        return data

    def run_job(self, ev, text, name="t.nc"):
        """Loads the program and runs it; returns the event index after the job's first event."""
        k = ev.mark()
        self.load(text, name)
        st, data = self.press("/api/machine/run", {})
        self.assertEqual(st, 200, data)
        self.assertEqual(data["job"]["state"], "running")
        _, k = ev.wait(job_state("running"), since=k, what="the job running")
        return k

    @staticmethod
    def times(sim, since, *entries):
        """[(time, entry)] of those entries in the simulator's log."""
        return [(t, e) for t, e in sim.timed(since) if e in entries]

    @staticmethod
    def realtime_names(sim, since):
        """The realtime bytes it got, but the status requests."""
        return [e for e in sim.realtime(since) if e != "?"]

    # -------------------------------------------------- stage 3: jobs

    def test_xa_a_job_streams_in_order_without_overflow(self):
        """A few hundred short moves: FluidNC's serial buffer never overflows (what it has not answered stays
        within 128 bytes), every line arrives once and in order, and the job ends done."""
        sim = self.jobs
        ev, _ = self.events()
        self.ready(ev, sim)
        moves = [f"G1 X{0.01 * (i % 50 + 1):.2f} Y{0.01 * (i // 50):.2f} F60" for i in range(300)]
        text = "G20 G90\nG0 X0 Y0 Z0\n" + "\n".join(moves) + "\nG0 X0 Y0\n"
        log0 = len(sim.entries())
        k = self.run_job(ev, text, "many.nc")
        end, _ = ev.wait(job_state("done", "paused", "stopped", "failed"), since=k, timeout=90, what="the job's end")
        job = end["job"]
        self.assertEqual((job["state"], job["error"]), ("done", None))
        self.assertEqual((job["acked"], job["sent"], job["total"], job["line"]), (303, 303, 303, 303))
        self.assertEqual(sim.lines(log0), ["G20 G90"] + [line.replace(" ", "") for line in text.splitlines()])
        self.assertEqual([e for e in sim.entries(log0) if e.startswith("RX OVERFLOW")], [])
        ev.wait(console("info", contains="Job done: many.nc, 303 lines in "), since=k)
        self.assertEqual(ev.find(console("error"), since=k), [])
        self.wait_idle_at([0, 0, 0], 1e-4)
        self.assertEqual(self.state()["job"]["state"], "done")      # (kept for a page opened later)

    def test_xb_an_error_pauses_the_job_and_resume_finishes_it(self):
        sim = self.jobs
        ev, _ = self.events()
        self.ready(ev, sim)
        text = "G0 X0 Y0\nG1 X0.2 F60\nG1 X0.4 Q1\nG1 X0.6\nG1 X0\n"
        log0 = len(sim.entries())
        k0 = ev.mark()
        k = self.run_job(ev, text, "bad.nc")
        p, k = ev.wait(job_state("paused"), since=k, timeout=20, what="the pause")
        self.assertEqual(p["job"]["why"], "error on line 3")
        ev.wait(console("error", "Line 3 was refused by FluidNC (error:20 unsupported or invalid G-code) and won't "
                                 "run. Resume goes on with the next lines; Stop ends the job."), since=k0)
        # FluidNC's verbose_errors message (not a reply) gives the job's error its own words
        ev.wait(lambda e: e["type"] == "job" and e["job"]["error"] == "line 3: G1X0.4Q1: error:20 (Unsupported GCode "
                "command)", since=k0, what="FluidNC's words for the error")
        self.wait_for(lambda: "!" in sim.realtime(log0), 3, "the feed hold")
        self.assertEqual(self.realtime_names(sim, log0), ["!"])     # held at once, after the bad line
        entries = sim.entries(log0)
        self.assertLess(entries.index("LINE G1X0.4Q1"), entries.index("RT !"))
        ev.wait(status("Hold", sub="0"), since=k0, timeout=5, what="Hold:0")
        self.realtime("resume")
        d, _ = ev.wait(job_state("done", "failed", "stopped"), since=k, timeout=20, what="the job done")
        self.assertEqual((d["job"]["state"], d["job"]["acked"], d["job"]["total"]), ("done", 5, 5))
        self.assertEqual(sim.lines(log0), ["G20 G90"] + [line.replace(" ", "") for line in text.splitlines()])
        self.assertEqual([e for e in sim.realtime(log0) if e != "?"], ["!", "~"])
        self.wait_idle_at([0, 0, 0], 1e-4)

    def test_xc_hold_and_resume_and_what_is_refused_meanwhile(self):
        sim = self.jobs
        ev, _ = self.events()
        self.ready(ev, sim)
        k = self.run_job(ev, "G0 X0 Y0\nG1 X1 F60\nG1 X0 F60\n", "slow.nc")    # 2 s of moving
        ev.wait(status("Run"), since=k, timeout=5, what="Run")
        # a job has the machine: the user's lines and buttons wait for it to end; realtime is fine
        for call, body in (("send", {"line": "G0 X1"}), ("jog", {"step": {"x": 0.1}, "feed": 150}),
                           ("jog", {"start": {"x": 1}, "feed": 150}), ("zero", {"axes": "X"}),
                           ("zero", {"undo": True}), ("goto", {"to": "z"}), ("home", {"axes": ""}), ("unlock", {}),
                           ("run", {})):
            self.assertEqual(self.call("/api/machine/" + call, body), (409, {"error": JOB_RUNNING}), (call, body))
        log0 = len(sim.entries())
        self.realtime("hold")
        p, k = ev.wait(job_state("paused"), since=k, what="paused")
        self.assertEqual(p["job"]["why"], "Feed hold")
        self.realtime("resume")                         # at once, while FluidNC still slows down (Hold:1) ...
        ev.wait(job_state("running"), since=k, timeout=6, what="running again")
        rt = self.wait_for(lambda: (lambda r: len(r) >= 2 and r)(self.times(sim, log0, "RT !", "RT ~")), 3, "'~'")
        self.assertEqual([e for _, e in rt], ["RT !", "RT ~"])
        self.assertGreaterEqual(rt[1][0] - rt[0][0], HOLD_DECEL - 0.02)    # ... but '~' only once it had stopped
        d, _ = ev.wait(job_state("done", "failed", "stopped"), since=k, timeout=15, what="the job done")
        self.assertEqual((d["job"]["state"], d["job"]["acked"]), ("done", 3))
        self.wait_idle_at([0, 0, 0], 1e-4)
        self.send("G0 X0")                              # the user's lines go again

    def test_xd_stop_is_the_safe_stop(self):
        """Stop during a job: Hold, then Ctrl-X only once FluidNC has stopped, so it keeps its position (no
        ALARM:3); the reset drops the rest of the job; inches again afterwards. And a line FluidNC took while
        it held (typed then) runs after the reset in FluidNC (3.9.4 and 4.1.1), as in the simulator: FluidSender
        holds it at once, and resets again once it has stopped."""
        sim = self.jobs
        ev, _ = self.events()
        self.ready(ev, sim)
        k = self.run_job(ev, "G0 X0 Y0\nG1 X3 F30\nG1 X0 F30\n", "long.nc")   # 12 s of moving
        ev.wait(lambda e: e["type"] == "status" and e["state"] == "Run" and (e["wpos"] or [0])[0] > 0.05, since=k,
                timeout=8, what="moving")
        log0 = len(sim.entries())
        k0 = ev.mark()
        self.assertEqual(self.call("/api/machine/stop", {}), (200, {"ok": True, "stopped": "job"}))
        _, k = ev.wait(job_state("stopping"), since=k, what="stopping")
        s, k = ev.wait(job_state("stopped", "failed", "done"), since=k, timeout=15, what="stopped")
        self.assertEqual((s["job"]["state"], s["job"]["why"]), ("stopped", "Stopped by the user"))
        ev.wait(console("info", "*** Controller reset"), since=k0, timeout=5)
        ev.wait(console("tx", "G20"), since=k0, timeout=5, what="G20 after the reset")
        rt = self.times(sim, log0, "RT !", "RT ^X")
        self.assertEqual([e for _, e in rt], ["RT !", "RT ^X"])
        self.assertGreaterEqual(rt[1][0] - rt[0][0], HOLD_DECEL - 0.02)    # the reset once it stood still
        idle = self.wait_idle_at()
        self.assertGreater(idle["wpos"][0], 0.05)       # stopped part-way, where it was
        self.assertLess(idle["wpos"][0], 2.9)
        self.assertEqual(ev.find(console("rx", contains="ALARM"), since=k0), [])
        self.assertIsNone(self.state()["alarm"])
        self.assertEqual(sim.lines(log0), ["G20"])      # nothing more of the job
        self.assertEqual([e for e in sim.entries(log0) if e.startswith("LEFTOVER")], [])   # (it had taken none)

        # a line typed while FluidNC holds: it takes it, to run after a Resume; Stop then resets it, and it runs
        # after the banner, in millimeters (G0 X0.5: X 0.02 in)
        self.send("G0 X1")
        self.wait_idle_at([1, 0, 0], 1e-4)
        k = ev.mark()
        self.send("G1 X2 F20")                          # 3 s of moving
        ev.wait(status("Run"), since=k, what="Run")
        self.realtime("hold")
        ev.wait(status("Hold", sub="0"), since=k, timeout=3, what="Hold:0")
        log1 = len(sim.entries())
        self.send("G0 X0.5")
        self.wait_for(lambda: "LINE G0 X0.5" in sim.entries(log1), 3, "the line at FluidNC")
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/stop", {}), (200, {"ok": True, "stopped": "motion"}))
        ev.wait(console("error", LEFTOVER), since=k, timeout=5, what="the line from before held")
        ev.wait(console("tx", "G20"), since=k, timeout=5, what="G20 after the second reset")
        self.wait_for(lambda: "LINE G20" in sim.entries(log1), 3, "G20 at FluidNC")   # (the sim logs it a bit later)
        self.assertEqual([e for e in sim.entries(log1) if e != "RT ?"],
                         ["LINE G0 X0.5", "RT ^X", "LEFTOVER G0 X0.5", "RT !", "RT ^X", "LINE G20"])
        idle = self.wait_idle_at()
        self.assertGreater(idle["wpos"][0], 0.5)        # held at once: nowhere near X 0.5 mm
        self.assertEqual(ev.find(console("rx", contains="ALARM"), since=k), [])

    def test_xe_an_alarm_fails_the_job_and_nothing_more_is_sent(self):
        """A hard limit in a job with the spindle and vacuum on: the job fails at once, nothing more of it is
        sent, and the page is told the spindle and vacuum may still be on (FluidNC leaves them on) until
        Soft Reset turns them off."""
        sim = self.crash
        ev, _ = self.events()
        self.ready(ev, sim)
        text = "M3 S12000\nM8\nG1 X3 F30\n" + "".join(f"G1 Y{0.1 * i:.1f}\n" for i in range(1, 40))
        k = self.run_job(ev, text, "crash.nc")
        ev.wait(status("Run"), since=k, timeout=5, what="Run")
        k0 = ev.mark()
        sim.proc.send_signal(signal.SIGUSR1)            # a limit switch
        f, _ = ev.wait(job_state("failed", "stopped", "done"), since=k0, timeout=5, what="the job failed")
        self.assertEqual((f["job"]["state"], f["job"]["why"]),
                         ("failed", "ALARM:1 (hard limit: position likely lost, home again)"))
        a, _ = ev.wait(lambda e: e["type"] == "alarm" and e["alarm"] and e["alarm"]["critical"], since=k0,
                       what="the critical alarm")
        self.assertTrue(a["alarm"]["outputs"])
        ev.wait(console("error", OUTPUTS_ON), since=k0, what="the spindle and vacuum warning")
        time.sleep(1.5)
        trip = self.times(sim, 0, "HARD-LIMIT")[-1][0]
        late = [e for t, e in sim.timed(0) if e.startswith("LINE ") and t > trip + 0.5]
        self.assertEqual(late, [])                      # nothing more of it was sent
        self.assertEqual(self.state()["alarm"], {"code": 1, "critical": True, "text": "ALARM:1", "outputs": True})
        k = ev.mark()
        self.realtime("reset")                          # turns them off (FluidNC: ALARM:14, the limit unhomed all)
        a, _ = ev.wait(lambda e: e["type"] == "alarm" and e["alarm"] and e["alarm"]["code"] == 14, since=k,
                       what="ALARM:14")
        self.assertFalse(a["alarm"]["outputs"])
        ev.wait(lambda e: e["type"] == "status" and e["flood"] is False, since=k, what="the vacuum off")

    def test_xf_m0_pauses_the_job_until_resume(self):
        sim = self.jobs
        ev, _ = self.events()
        self.ready(ev, sim)
        k = self.run_job(ev, "G0 X0 Y0\nG0 X0.5\nM0 (change the bit)\nG0 X0\n", "m0.nc")
        p, k = ev.wait(job_state("paused"), since=k, timeout=10, what="the M0 pause")
        self.assertEqual(p["job"]["why"], "M0 in the program (line 3): press Resume to go on")
        time.sleep(0.6)
        st = self.state()
        self.assertEqual((st["job"]["state"], st["status"]["state"], st["status"]["sub"]), ("paused", "Hold", "0"))
        self.assertNear(st["status"]["wpos"], [0.5, 0, 0])
        self.realtime("resume")
        d, _ = ev.wait(job_state("done", "failed", "stopped"), since=k, timeout=10, what="the job done")
        self.assertEqual(d["job"]["state"], "done")
        self.wait_idle_at([0, 0, 0], 1e-4)

    # -------------------------------------------------- stage 3: the vacuum and AUX, Go to zero

    def test_xg_vacuum_and_aux_switches(self):
        sim = self.cool
        ev, _ = self.events()
        self.ready(ev, sim)
        st = self.state()["status"]
        self.assertEqual((st["flood"], st["mist"]), (False, False))     # ($G at connect: M9)
        log0 = len(sim.entries())
        k = ev.mark()
        t0 = time.monotonic()
        self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": True}),
                         (200, {"ok": True, "sent": True}))
        ev.wait(status(flood=True), since=k, timeout=2, what="the vacuum on")
        self.assertLess(time.monotonic() - t0, 0.5)
        ev.wait(console("info", "Vacuum on (M8 toggle)"), since=k)
        self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": True}),
                         (200, {"ok": True, "sent": False}))    # already on: nothing sent
        time.sleep(1.8)
        toggle = self.times(sim, log0, "RT flood-toggle")
        self.assertEqual(len(toggle), 1)
        polls = [t - toggle[0][0] for t, e in sim.timed(log0) if e == "RT ?" and t >= toggle[0][0]]
        # FluidNC says the coolant only in every 10th report: asked for every 15 ms until one does (every 200 ms
        # would be 2 in the first 0.3 s) ...
        self.assertGreaterEqual(len([t for t in polls if t < 0.3]), 3, polls)
        self.assertLessEqual(len([t for t in polls if 0.8 <= t < 1.8]), 7, polls)    # ... then every 200 ms
        self.assertTrue(self.state()["status"]["flood"])    # (FluidNC's reports say on too)
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/coolant", {"which": "aux", "on": True}),
                         (200, {"ok": True, "sent": True}))
        ev.wait(status(flood=True, mist=True), since=k, timeout=2, what="AUX on")
        for which, field in (("vacuum", "flood"), ("aux", "mist")):
            k = ev.mark()
            self.assertEqual(self.call("/api/machine/coolant", {"which": which, "on": False}),
                             (200, {"ok": True, "sent": True}))
            ev.wait(status(**{field: False}), since=k, timeout=2, what=f"{which} off")
        self.wait_for(lambda: len(self.realtime_names(sim, log0)) >= 4, 3, "the toggles")
        self.assertEqual(self.realtime_names(sim, log0), ["flood-toggle", "mist-toggle", "flood-toggle", "mist-toggle"])
        # a program's M8 and M9 show too (FluidNC reports them within 10 reports)
        k = self.run_job(ev, "M8\nG4 P2.5\nM9\nG4 P2.5\n", "vacuum.nc")
        _, k2 = ev.wait(status(flood=True), since=k, timeout=6, what="the program's M8")
        ev.wait(status(flood=False), since=k2, timeout=7, what="the program's M9")
        ev.wait(job_state("done"), since=k, timeout=15, what="the job done")

    def test_xh_no_flood_pin_is_refused_and_said(self):
        """FluidNC with no flood output ($/coolant/flood_pin=NO_PIN) drops M8 and its toggle: the switch is refused
        and says why (the page shows it from coolantMissing), AUX still works."""
        sim = self.noflood
        ev, _ = self.events()
        self.ready(ev, sim)
        why = "FluidNC has no flood_pin configured (the vacuum output)"
        self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": True}), (409, {"error": why}))
        st = self.state()["status"]
        self.assertEqual((st["coolantMissing"], st["coolantPins"]),
                         ({"vacuum": why, "aux": None}, {"vacuum": "NO_PIN", "aux": "I2SO.5"}))
        self.assertEqual(self.call("/api/machine/coolant", {"which": "aux", "on": True}),
                         (200, {"ok": True, "sent": True}))
        self.assertEqual(self.call("/api/machine/coolant", {"which": "aux", "on": False})[0], 200)

    def test_xh2_the_switch_shows_what_fluidnc_did_at_once(self):
        """FluidNC's pins couldn't be read (here: no $/ setting answers), and it has no flood output: it ignores the
        toggle; the switch shows on (as asked) and, from FluidNC's $G and reports asked for quickly after the
        toggle, off again within a fraction of a second (not 2 s)."""
        sim = self.nopins
        ev, _ = self.events()
        self.ready(ev, sim)
        k = ev.mark()
        t0 = time.monotonic()
        self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": True}),
                         (200, {"ok": True, "sent": True}))
        _, k = ev.wait(status(flood=True), since=k, timeout=2, what="the vacuum shown on")
        ev.wait(status(flood=False), since=k, timeout=3, what="FluidNC's word: off")
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_xg2_vacuum_and_aux_on_fluidnc_411(self):
        """The user's FluidNC 4.1.1 puts Ov in the next report after a toggle: the switch shows FluidNC's word at
        once, and the quick reports end soon after (3.9.4: see test_xg). A program's M8/M9 show at their ok."""
        sim = self.cool411
        ev, _ = self.events()
        self.ready(ev, sim)
        st = self.state()["status"]
        self.assertEqual((st["flood"], st["coolantFollows"], st["coolantDelayMs"], st["spindleType"]),
                         (False, {"vacuum": "report", "aux": "report"}, 0, "PWM"))
        log0 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": True}),
                         (200, {"ok": True, "sent": True}))
        ev.wait(status(flood=True), since=k, timeout=2, what="the vacuum on")
        time.sleep(1.0)
        toggle = self.times(sim, log0, "RT flood-toggle")
        polls = [t - toggle[0][0] for t, e in sim.timed(log0) if e == "RT ?" and t >= toggle[0][0]]
        self.assertGreaterEqual(len([t for t in polls if t < 0.3]), 3, polls)    # (asked quickly ...)
        self.assertLessEqual(len([t for t in polls if 0.5 <= t < 1.0]), 4, polls)     # (... and then not)
        self.assertTrue(self.state()["status"]["flood"])
        self.assertEqual(sim.lines(log0), [])           # (its reports say it: no $G)
        k = self.run_job(ev, "M8\nG4 P1.5\nM9\nG4 P0.5\n", "vacuum.nc")
        _, k2 = ev.wait(status(flood=False), since=k, timeout=6, what="the program's M9")
        ev.wait(job_state("done"), since=k, timeout=10, what="the job done")

    def test_xi_go_to_xy_and_z_zero(self):
        sim = self.go
        ev, _ = self.events()
        self.ready(ev, sim)
        self.send("G0 X1 Y1 Z0.1")
        self.wait_idle_at([1, 1, 0.1], 1e-4)
        log0 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.press("/api/machine/goto", {"to": "xy"}),         # Z below 0.2: up first
                         (200, {"ok": True, "lines": ["G20 G90 G0 Z0.2", "G20 G90 G0 X0 Y0"]}))
        self.wait_idle_at([0, 0, 0.2], 1e-4)
        self.assertEqual(self.press("/api/machine/goto", {"to": "z"}), (200, {"ok": True, "lines": ["G20 G90 G0 Z0"]}))
        self.wait_idle_at([0, 0, 0], 1e-4)
        self.send("G0 X1 Y-1 Z0.5")
        self.wait_idle_at([1, -1, 0.5], 1e-4)
        self.assertEqual(self.press("/api/machine/goto", {"to": "xy"}),          # high enough: no lift
                         (200, {"ok": True, "lines": ["G20 G90 G0 X0 Y0"]}))
        self.wait_idle_at([0, 0, 0.5], 1e-4)
        self.assertEqual(sim.lines(log0), ["G20 G90 G0 Z0.2", "G20 G90 G0 X0 Y0", "G20 G90 G0 Z0", "G0 X1 Y-1 Z0.5",
                                           "G20 G90 G0 X0 Y0"])
        self.assertEqual([e["text"] for e in ev.find(console("tx"), since=k)], sim.lines(log0))   # shown like typed

    # -------------------------------------------------- stage 4: the pendant

    def syncs(self, pend, since):
        return [e for e in pend.entries(since) if e["ev"] == "sync"]

    def test_xj_the_pendant(self):
        sim, pend = self.pcnc, self.pendant
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 1e-4)
        self.assertEqual(self.call("/api/machine/jogsettings", {"step": 0.1, "feed": 150}),
                         (200, {"ok": True, "step": 0.1, "feed": 150}))
        k = ev.mark()
        st, data = self.call("/api/pendant/connect", {"port": pend.path})
        self.assertEqual((st, data["pendant"]["port"]), (200, pend.path), data)
        self.addCleanup(self.call, "/api/pendant/disconnect", {})
        ev.wait(lambda e: e["type"] == "pendant" and e["pendant"]["state"] == "connected", since=k, timeout=5,
                what="the pendant connected")
        ev.wait(console("info", f"Pendant: connected on {pend.path}"), since=k)
        n = len(pend.entries())
        # the DRO, Idle, the jog settings. (G0 X0 Y0 Z0 was answered just now: until a report made a moment after
        # its ok has come, the pendant says MOVING, 11 (pendant.just_sent), and nothing else before IDLE)
        syncs = self.wait_for(lambda: any(e["status"] == 0 for e in self.syncs(pend, n)) and self.syncs(pend, n), 3,
                              "a SYNC saying IDLE")
        first = [e["status"] for e in syncs].index(0)
        self.assertEqual([e["status"] for e in syncs[:first]], [11] * first)
        sync = syncs[first]
        self.assertNear([sync["x"], sync["y"], sync["z"], sync["status"], sync["feed"], sync["step"]],
                        [0, 0, 0, 0, 150, 0.1], 1e-6)

        # a step key changes the shared jog step, a jog key moves one step, a zero key zeroes
        k = ev.mark()
        pend.control("key 9")
        js, _ = ev.wait(lambda e: e["type"] == "jogsettings", since=k, timeout=3, what="the step from the pendant")
        self.assertEqual((js["step"], js["feed"]), (0.01, 150))
        log0 = len(sim.entries())
        pend.control("key 5")
        ev.wait(console("tx", "$J=G20G91X0.01F150"), since=k, timeout=3, what="the jog")
        ev.wait(console("info", "Pendant: Jog X+ 0.01 in"), since=k)
        self.wait_idle_at([0.01, 0, 0], 1e-4)
        n = len(pend.entries())                         # (a SYNC made just before may still be on its way: 46 ms)
        sync = self.wait_for(lambda: [e for e in self.syncs(pend, n) if abs(e["x"] - 0.01) < 1e-6], 3, "a SYNC")[-1]
        self.assertNear([sync["x"], sync["step"]], [0.01, 0.01], 1e-6)
        pend.control("key 15 double")                   # Zero X: a double-tap key in the firmware
        ev.wait(console("tx", "G10 L20 P0 G20 X0"), since=k, timeout=3, what="the zero")
        self.wait_idle_at([0, 0, 0], 1e-4)
        self.assertEqual(sim.lines(log0), ["$J=G20G91X0.01F150", "G10 L20 P0 G20 X0"])
        pend.control("key 23 double")                   # (Idle: a stray double-tap must not home)
        ev.wait(console("error", "Pendant: Home all ($H) refused: FluidNC isn't in ALARM. Use Home on the page if "
                                 "you mean it."), since=k, timeout=3, what="Home refused")
        pend.control("key 19 double")                   # (the bit at the work zero: no rapid across the table)
        ev.wait(console("error", "Pendant: Machine X0 (G53 G0 X0) refused: Z is at 0.0000 in (work). Lift Z first "
                                 "(Machine Z0 key)."), since=k, timeout=3, what="Machine X0 refused")
        self.assertEqual(sim.lines(log0), ["$J=G20G91X0.01F150", "G10 L20 P0 G20 X0"])

        # the page's 10 in step: a jog key moves 1 in (the most the pendant's own step keys choose)
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/jogsettings", {"step": 10})[0], 200)
        pend.control("key 7")
        ev.wait(console("info", "Pendant: step 10 in is too big for the pendant; using 1 in"), since=k, timeout=3)
        ev.wait(console("tx", "$J=G20G91X-1F150"), since=k, timeout=3, what="a 1 in jog")
        self.wait_idle_at([-1, 0, 0], 1e-4, timeout=8)
        self.assertEqual(self.call("/api/machine/jogsettings", {"step": 0.1})[0], 200)

        # Run, Pause, Resume and Stop on a job
        self.load("G0 X0 Y0\nG1 X1 F15\nG1 X0 F15\n", "pendant.nc")       # 8 s of moving
        log0 = len(sim.entries())
        k = ev.mark()
        pend.control("key 25")
        _, k = ev.wait(job_state("running"), since=k, timeout=5, what="Run from the pendant")
        ev.wait(console("info", contains="Pendant: Run pendant.nc ("), since=k, timeout=3, what="Run's program")
        ev.wait(status("Run"), since=k, timeout=5, what="Run")
        n = len(pend.entries())
        sync = self.wait_for(lambda: [e for e in self.syncs(pend, n) if e["status"] == 2], 4, "a SYNC of the job")[-1]
        self.assertEqual(sync["total"], 3)
        k0 = ev.mark()
        pend.control("key 26")
        _, k = ev.wait(job_state("paused"), since=k, timeout=5, what="Pause from the pendant")
        ev.wait(console("info", "Pendant: Pause (feed hold)"), since=k0)
        pend.control("key 26")
        _, k = ev.wait(job_state("running"), since=k, timeout=6, what="Resume from the pendant")
        pend.control("key 27")
        s, k = ev.wait(job_state("stopped", "failed", "done"), since=k, timeout=15, what="Stop from the pendant")
        self.assertEqual(s["job"]["state"], "stopped")
        ev.wait(console("tx", "G20"), since=k, timeout=5, what="G20 after the reset")
        self.assertEqual(self.realtime_names(sim, log0), ["!", "~", "!", "^X"])
        self.wait_idle_at()

        # no page open: at rest the port goes although the pendant is connected (the user: "should it not
        # disconnet when I close the app?"), and then the pendant's port too
        ev.close()
        t0 = time.monotonic()
        while self.state()["connected"]:
            self.assertLess(time.monotonic() - t0, RELEASE + 4, "the port was not released with no page open")
            time.sleep(0.1)
        self.assertGreaterEqual(time.monotonic() - t0, RELEASE - 0.5)
        while self.state()["pendant"]["state"] != "off":
            self.assertLess(time.monotonic() - t0, RELEASE + 6, "the pendant's port was not released")
            time.sleep(0.1)
        texts = [e["text"] for e in self.state()["console"]]
        self.assertIn("Released the port because no FluidSender window is open", texts)
        self.assertIn("Released the pendant because no FluidSender window is open", texts)

    # -------------------------------------------------- stage 4: the probe and the surface scan

    def connect_probe(self, probe):
        st, data = self.call("/api/probe/connect", {"port": probe.path})
        self.assertEqual((st, data), (200, {"ok": True, "port": probe.path}))
        self.addCleanup(self.call, "/api/probe/disconnect", {})
        st, data = self.call("/api/probe/home", {})
        self.assertEqual(st, 200, data)
        self.assertEqual((data["probe"]["connected"], data["probe"]["ready"]), (True, True))

    def test_xk_a_surface_scan(self):
        """3 x 2 points: the moves (column by column, zigzag in Y), ZR and P at each point, H at the end, back to
        the first point; the result in UGS's .xyz format, written to scans/last-scan.xyz at once."""
        sim, probe = self.scnc, self.probe
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 1e-4)
        self.assertIn(probe.path, [p["path"] for p in self.call("/api/probe/ports")[1]["ports"]])
        st, data = self.call("/api/probe/connect", {"port": probe.path})
        self.assertEqual((st, data), (200, {"ok": True, "port": probe.path}))
        self.addCleanup(self.call, "/api/probe/disconnect", {})
        self.assertEqual(self.call("/api/scan/start", {"settings": SCAN_3X2}),
                         (409, {"error": "Probe not ready. Home the probe before scanning."}))
        st, data = self.call("/api/probe/home", {})
        self.assertEqual((st, data["probe"]["ready"]), (200, True))
        # the CNC's Z never moves during a scan: the bit must clear the work (0.2 in or more above its zero)
        self.assertEqual(self.call("/api/scan/start", {"settings": SCAN_3X2}),
                         (409, {"error": "Z is at 0.0000 in (work): raise Z so the bit clears the work (0.2 in or more "
                                         "above work zero), then Start Scan."}))
        self.send("G0 Z0.25")
        self.wait_idle_at([0, 0, 0.25], 1e-4)
        log0, plog0 = len(sim.entries()), len(probe.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/scan/start", {"settings": SCAN_3X2}), (200, {"ok": True, "points": 6}))
        self.assertEqual(self.call("/api/machine/send", {"line": "G0 X1"}), (409, {"error": "A scan is running"}))
        self.assertEqual(self.call("/api/machine/run", {})[1]["error"], "A scan is running")
        end, _ = ev.wait(scan_phase("done", "failed", "stopped"), since=k, timeout=60, what="the scan's end")
        sc = end["scan"]
        self.assertEqual((sc["phase"], sc["error"], sc["running"]), ("done", None, False))
        self.assertTrue(sc["message"].startswith("Scan complete: 6 points in "), sc["message"])
        text = sc["result"]["text"]                     # UGS's file, with FluidSender's setup line after its header
        lines = text.splitlines(keepends=True)          # (auto-level's same-setup check: leveling.py)
        self.assertRegex(lines[4], r"^# wcs=G54 wcoX=-5\.905500 wcoY=-3\.937000 wcoZ=-1\.181100 time=\d+\n$")
        self.assertEqual("".join(lines[:4] + lines[5:]), XYZ_3X2)
        self.assertEqual(sc["result"]["points"], [[0.0, 0.0, 0.0], [0.0, 0.1, 0.01], [0.1, 0.0, 0.02],
                                                  [0.1, 0.1, -0.01], [0.2, 0.0, 0.0], [0.2, 0.1, -0.03]])
        path = os.path.join(self.tmp.name, "scans", "last-scan.xyz")
        self.assertEqual(sc["savedTo"], path)
        with open(path) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(sim.lines(log0), ["G20 G90 G0 X0 Y0", "G20 G90 G0 X0 Y0", "G20 G90 G0 X0 Y0.1",
                                           "G20 G90 G0 X0.1 Y0.1", "G20 G90 G0 X0.1 Y0", "G20 G90 G0 X0.2 Y0",
                                           "G20 G90 G0 X0.2 Y0.1", "G20 G90 G0 X0 Y0"])
        self.assertEqual([e["cmd"] for e in probe.entries(plog0) if "cmd" in e], ["ZR:0.200", "P"] * 6 + ["H"])
        self.assertEqual(sorted({e["scan"]["point"] for e in ev.find(lambda e: e["type"] == "scan", since=k)}),
                         list(range(7)))                # the page sees it grow, point by point
        self.wait_idle_at([0, 0, 0.25], 1e-4)           # back at the first point (the CNC's Z never moved)
        out = os.path.join(self.tmp.name, "saved.xyz")
        self.assertEqual(self.call("/api/scan/save", {"path": out}),
                         (200, {"ok": True, "path": out, "size": len(text)}))
        with open(out) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(self.call("/api/scan/save", {"path": out[:-4] + ".txt"})[0], 400)

    def test_xl_stopping_a_scan(self):
        """Stop while the probe touches: it is stopped at once (A), and the CNC, standing still, gets nothing.
        Stop while the CNC moves to the next point: the probe is stopped and the CNC gets the safe stop."""
        sim, probe = self.scnc, self.probe2             # (2 s a touch)
        two = {"minX": 0, "maxX": 6, "xPoints": 2, "minY": 0, "maxY": 0, "yPoints": 1, "zRetract": 0.2}
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 1e-4)
        self.connect_probe(probe)
        self.send("G0 Z0.25")                           # (the bit clear of the work: see test_xk)
        self.wait_idle_at([0, 0, 0.25], 1e-4)
        log0, plog0 = len(sim.entries()), len(probe.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/scan/start", {"settings": two}), (200, {"ok": True, "points": 2}))
        self.wait_for(lambda: [e for e in probe.entries(plog0) if e.get("cmd") == "P"], 10, "a touch")
        self.assertEqual(self.call("/api/scan/stop", {}), (200, {"ok": True, "stopped": True}))
        s, _ = ev.wait(scan_phase("stopped", "failed", "done"), since=k, timeout=10, what="the scan stopped")
        self.assertEqual((s["scan"]["phase"], s["scan"]["error"]),
                         ("stopped", "Scan stopped. Probe retracted (homed)."))
        self.wait_for(lambda: [e for e in probe.entries(plog0) if e.get("abort") == "P"], 3, "the touch stopped")
        self.assertEqual(self.realtime_names(sim, log0), [])     # the CNC stood still: nothing to stop
        self.wait_for(lambda: [e for e in probe.entries(plog0) if e.get("cmd") == "H"], 3, "the probe homed")
        self.assertTrue(self.state()["probe"]["ready"])         # (the Stop homed it: nobody has to remember)

        st, data = self.call("/api/probe/home", {})
        self.assertEqual((st, data["probe"]["ready"]), (200, True))
        log0, plog0 = len(sim.entries()), len(probe.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/scan/start", {"settings": two}), (200, {"ok": True, "points": 2}))
        _, k2 = ev.wait(lambda e: e["type"] == "scan" and e["scan"]["phase"] == "moving" and (e["scan"]["at"] or {})
                        .get("n") == 2, since=k, timeout=15, what="the move to the second point")
        ev.wait(lambda e: e["type"] == "status" and e["state"] == "Run" and (e["wpos"] or [0])[0] > 0.1, since=k2,
                timeout=5, what="the CNC moving")
        k3 = ev.mark()
        self.assertEqual(self.call("/api/machine/stop", {}), (200, {"ok": True, "stopped": "scan"}))
        s, _ = ev.wait(scan_phase("stopped", "failed", "done"), since=k, timeout=15, what="the scan stopped")
        self.assertEqual(s["scan"]["phase"], "stopped")
        self.wait_for(lambda: [e for e in probe.entries(plog0) if e.get("cmd") == "A"], 3, "the probe stopped")
        self.wait_for(lambda: "^X" in sim.realtime(log0), 15, "the reset")
        rt = self.times(sim, log0, "RT !", "RT ^X")
        self.assertEqual([e for _, e in rt], ["RT !", "RT ^X"])
        self.assertGreaterEqual(rt[1][0] - rt[0][0], HOLD_DECEL - 0.02)    # held, and reset once it stood still
        ev.wait(console("tx", "G20"), since=k3, timeout=5, what="G20 after the reset")
        idle = self.wait_idle_at()
        self.assertGreater(idle["wpos"][0], 0.1)        # stopped on the way, where it was
        self.assertLess(idle["wpos"][0], 5.9)
        self.assertEqual(ev.find(console("rx", contains="ALARM"), since=k3), [])
        # a scan sends one move at a time and nothing after its stop: FluidNC had no line to keep through the
        # reset (one is in test_xd and test_xn), and one reset was enough
        self.assertEqual([e for e in sim.entries(log0) if e.startswith("LEFTOVER")], [])
        self.assertEqual(ev.find(console("error", LEFTOVER), since=k3), [])

    def test_xla_a_probe_let_go_of_while_it_may_be_down_still_warns(self):
        """The window closes during a scan: FluidNC, the pendant and the probe are all kept while it runs. The
        pendant's Stop stops it mid-touch (the probe aborted: not homed, its tip maybe down). At rest FluidNC is let
        go of, then the pendant and the probe, and the console says the probe may be down. A probe connect with no
        window open is not cut short (it is let go of once connected). A window opened later still gets the probe's
        mayBeDown, so the page warns, until the probe is connected and homed again. (The review's repros:
        e2e_probe_down*.py and e2e_probe_connect_race.py.)"""
        sim, probe, pend = self.scnc, self.probe2, self.pendant2    # (2 s a touch; the fixed firmware's Stop)
        three = {"minX": 0, "maxX": 0.2, "xPoints": 3, "minY": 0, "maxY": 0, "yPoints": 1, "zRetract": 0.2}
        ev, _ = self.events()
        self.ready(ev, sim)
        self.to_work_zero(ev, 1e-4)
        k = ev.mark()
        st, data = self.call("/api/pendant/connect", {"port": pend.path})
        self.assertEqual(st, 200, data)
        self.addCleanup(self.call, "/api/pendant/disconnect", {})
        ev.wait(lambda e: e["type"] == "pendant" and e["pendant"]["state"] == "connected", since=k, timeout=5,
                what="the pendant connected")
        self.connect_probe(probe)
        self.send("G0 Z0.25")                           # (the bit clear of the work: see test_xk)
        self.wait_idle_at([0, 0, 0.25], 1e-4)
        plog0 = len(probe.entries())

        def held():
            s = self.state()
            return s, (s["connected"], s["pendant"]["port"] is not None, s["probe"]["port"] is not None)

        def touches():
            return [e for e in probe.entries(plog0) if e.get("cmd") == "P"]
        self.assertEqual(self.call("/api/scan/start", {"settings": three}), (200, {"ok": True, "points": 3}))
        ev.close()                                      # the window closes during the scan
        t0 = time.monotonic()
        while len(touches()) < 3:                       # (2 s a touch: the third is more than RELEASE + 1 s later)
            s, h = held()
            self.assertEqual(h, (True, True, True), "let go of during a scan")
            self.assertTrue(s["scan"]["running"])
            self.assertLess(time.monotonic() - t0, 20, "no third touch")
            time.sleep(0.1)
        self.assertGreater(time.monotonic() - t0, RELEASE + 1)
        since = time.time()                             # (the console keeps 200 lines: the newer ones by their time)
        pend.control("key 27")                          # the pendant's Stop, mid-touch
        self.wait_for(lambda: (self.state()["scan"] or {}).get("phase") in ("stopped", "failed", "done"), 10,
                      "the scan's end")
        t_stop = time.monotonic()
        s = self.state()
        self.assertEqual((s["scan"]["phase"], s["scan"]["error"]),
                         ("stopped", "Scan stopped. Probe retracted (homed)."))
        self.wait_for(lambda: [e for e in probe.entries(plog0) if e.get("abort") == "P"], 3, "the touch stopped")
        self.assertEqual({k: s["probe"][k] for k in ("connected", "ready", "mayBeDown")},
                         {"connected": True, "ready": True, "mayBeDown": False})     # (the Stop homed it)

        # at rest: FluidNC first, then the pendant and the probe (its tip up: homed after the Stop)
        gone = {}
        while len(gone) < 3:
            s, h = held()
            for name, on in zip(("fluidnc", "pendant", "probe"), h):
                if not on:
                    gone.setdefault(name, time.monotonic() - t_stop)
            self.assertLess(time.monotonic() - t_stop, RELEASE + 6, f"not all let go of: {gone}")
            time.sleep(0.1)
        self.assertGreaterEqual(gone["fluidnc"], RELEASE - 0.5)
        self.assertLessEqual(gone["fluidnc"], min(gone["pendant"], gone["probe"]))

        def said(since, *want):
            """The console has these lines (dir, text), in this order, since then."""
            got = iter((e["dir"], e["text"]) for e in self.state()["console"] if e["t"] >= since)
            return all(w in got for w in want)
        self.wait_for(lambda: said(since, ("info", RELEASED), ("info", "Released the pendant and the probe because no "
                                                                       "FluidSender window is open")),
                      4, "the release's console lines")
        self.assertFalse(said(since, ("error", PROBE_LET_GO_DOWN)))
        s = self.state()
        self.assertEqual({k: s["probe"][k] for k in ("port", "connected", "ready", "mayBeDown")},
                         {"port": None, "connected": False, "ready": False, "mayBeDown": False})

        # no window open for longer than RELEASE: a probe connect is not cut short (then the probe is let go of)
        for _ in range(2):
            since = time.time()
            self.assertEqual(self.call("/api/probe/connect", {"port": probe.path}),
                             (200, {"ok": True, "port": probe.path}))
            self.wait_for(lambda: said(since, ("info", "Released the probe because no FluidSender window is open"),
                                       ("error", PROBE_LET_GO_DOWN)), 5, "the probe let go of again")
            s = self.state()
            self.assertEqual((s["probe"]["port"], s["probe"]["mayBeDown"]), (None, True))

        # a window opened later: the page still warns (static/jog.js probeMayBeDown: not connected -> mayBeDown)
        ev, snap = self.events()
        self.assertEqual({k: snap["probe"][k] for k in ("connected", "ready", "mayBeDown")},
                         {"connected": False, "ready": False, "mayBeDown": True})
        st, data = self.call("/api/probe/connect", {"port": probe.path})
        self.assertEqual((st, data), (200, {"ok": True, "port": probe.path}))
        self.addCleanup(self.call, "/api/probe/disconnect", {})
        s = self.state()
        self.assertEqual((s["probe"]["connected"], s["probe"]["ready"], s["probe"]["mayBeDown"]), (True, False, True))
        k = ev.mark()
        st, data = self.call("/api/probe/home", {})
        self.assertEqual((st, data["probe"]["ready"], data["probe"]["mayBeDown"]), (200, True, False))
        ev.wait(lambda e: e["type"] == "probe" and e["probe"]["mayBeDown"] is False, since=k, what="the probe homed")

    def test_xlb_an_alarm_that_may_have_left_the_spindle_on_keeps_all_three(self):
        """The verify of the release (release-verify/e2e_alarm_outputs.py): a job with M3 and M8, the window closed,
        a hard limit on its last move (every line answered). FluidNC leaves the spindle and the vacuum on in an
        alarm, and only a Soft Reset gets out of a hard limit: the pendant's Stop is that reset. FluidNC, then the
        pendant and the probe, were let go of RELEASE s after the alarm, and the pendant's Stop then reached nothing.
        Now all three stay; the pendant's Stop resets FluidNC (the spindle and the vacuum off), and then all three
        are let go of as at rest."""
        sim, pend, probe = self.limitjob, self.pendant3, self.probe3    # (the fixed firmware: its Stop always goes)
        ev, _ = self.events()
        self.ready(ev, sim)
        k = ev.mark()
        st, data = self.call("/api/pendant/connect", {"port": pend.path})
        self.assertEqual(st, 200, data)
        self.addCleanup(self.call, "/api/pendant/disconnect", {})
        ev.wait(lambda e: e["type"] == "pendant" and e["pendant"]["state"] == "connected", since=k, timeout=5,
                what="the pendant connected")
        self.connect_probe(probe)

        def held():
            s = self.state()
            return s, (s["connected"], s["pendant"]["port"] is not None, s["probe"]["port"] is not None)
        k = self.run_job(ev, "M3 S12000\nM8\nG1 X3 F30\n", "spindle.nc")      # (6 s of moving)
        ev.wait(status("Run"), since=k, timeout=5, what="Run")
        ev.close()                                      # the window closes during the job
        time.sleep(1.0)
        since = time.time()                             # (the console keeps 200 lines: the newer ones by their time)
        sim.proc.send_signal(signal.SIGUSR1)            # a limit switch on the job's last move
        self.wait_for(lambda: (self.state()["job"] or {}).get("state") == "failed", 5, "the job failed")
        t_alarm = time.monotonic()
        s = self.wait_for(lambda: (lambda s: s["alarm"] and s["alarm"]["critical"] and s)(self.state()), 3,
                          "the critical alarm")
        self.assertTrue(s["alarm"]["outputs"])
        self.assertIn(("error", OUTPUTS_ON), [(e["dir"], e["text"]) for e in s["console"] if e["t"] >= since])
        while time.monotonic() - t_alarm < RELEASE + 2:    # (FluidNC went RELEASE s after the alarm, the rest after)
            s, h = held()
            self.assertEqual(h, (True, True, True), "let go of in an alarm that may have left the spindle on")
            time.sleep(0.1)
        self.assertEqual((s["status"]["state"], s["status"]["spindle"], s["status"]["flood"]), ("Alarm", 12000, True))
        self.assertTrue(s["alarm"]["outputs"])
        log1 = len(sim.entries())
        pend.control("key 27")                          # the pendant's Stop, at the machine
        self.wait_for(lambda: "^X" in sim.realtime(log1), 3, "the pendant's Soft Reset")
        t_reset = time.monotonic()
        self.assertEqual(self.realtime_names(sim, log1), ["^X"])   # (no '!': see test_xr)
        s = self.wait_for(lambda: (lambda s: s["connected"] and s["status"]["spindle"] == 0
                                   and s["status"]["flood"] is False and s)(self.state()), 5, "the outputs off")
        self.assertFalse((s["alarm"] or {}).get("outputs"))
        self.wait_for(lambda: [e for e in self.state()["console"] if e["t"] >= since and e["dir"] == "info"
                               and e["text"].startswith("Pendant: Stop: Soft Reset")], 3, "the pendant's Stop said")

        # at rest now: FluidNC first, then the pendant and the probe
        gone = {}
        while len(gone) < 3:
            s, h = held()
            for name, on in zip(("fluidnc", "pendant", "probe"), h):
                if not on:
                    gone.setdefault(name, time.monotonic() - t_reset)
            self.assertLess(time.monotonic() - t_reset, RELEASE + 6, f"not all let go of: {gone}")
            time.sleep(0.1)
        self.assertGreaterEqual(gone["fluidnc"], RELEASE - 0.5)
        self.assertLessEqual(gone["fluidnc"], min(gone["pendant"], gone["probe"]))
        texts = [(e["dir"], e["text"]) for e in self.state()["console"] if e["t"] >= since]
        self.assertIn(("info", RELEASED), texts)
        self.assertIn(("info", "Released the pendant and the probe because no FluidSender window is open"), texts)
        self.assertNotIn(("error", PROBE_LET_GO_DOWN), texts)     # (homed)

    # -------------------------------------------------- stage 5: FluidNC 4.1.1, the coolant pins, overrides

    def test_xp_a_vacuum_on_a_gpio_pin_follows_fluidncs_modes(self):
        """The coolant outputs on plain ESP32 GPIOs (the simulator's --no-readback): FluidNC's reports never show
        them (it reads an output-only pad as 0). FluidSender follows FluidNC's M7/M8/M9 state: the toggle and a
        quiet $G after it, a program's M8/M9 at their ok and a $G when the job ends; the reports can't turn it off,
        and the console says why once. delay_ms is read for the page (R5)."""
        sim = self.gpio
        ev, _ = self.events()
        self.ready(ev, sim)
        st = self.state()["status"]
        self.assertEqual((st["coolantFollows"], st["coolantPins"], st["coolantDelayMs"]),
                         ({"vacuum": "modal", "aux": "modal"}, {"vacuum": "gpio.25", "aux": "gpio.26"}, 250))
        log0 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": True}),
                         (200, {"ok": True, "sent": True}))
        ev.wait(status(flood=True), since=k, timeout=2, what="the vacuum on")
        self.wait_for(lambda: "$G" in sim.lines(log0), 3, "the quiet $G")
        ev.wait(console("info", "FluidNC's reports don't show the coolant outputs; following its M7/M8/M9 state"),
                since=k, timeout=4)
        time.sleep(1.0)                                 # (more reports with Ov: none shows F)
        self.assertTrue(self.state()["status"]["flood"])
        self.assertEqual(ev.find(status(flood=False), since=k), [])
        self.assertEqual([e["text"] for e in ev.find(console("tx"), since=k)], [])     # (quiet)
        log1 = len(sim.entries())
        k = self.run_job(ev, "M9\nG4 P0.5\nM8\nG4 P0.5\nM9\n", "gpio.nc")
        _, k2 = ev.wait(status(flood=False), since=k, timeout=5, what="the program's first M9")
        _, k2 = ev.wait(status(flood=True), since=k2, timeout=5, what="its M8")
        ev.wait(status(flood=False), since=k2, timeout=5, what="its last M9")
        ev.wait(job_state("done"), since=k, timeout=10, what="the job done")
        self.wait_for(lambda: "$G" in sim.lines(log1), 3, "the $G after the job")
        self.assertEqual(sim.lines(log1), ["G20 G90", "M9", "G4P0.5", "M8", "G4P0.5", "M9", "$G"])
        self.assertFalse(self.state()["status"]["flood"])

    def test_xq_feed_rapid_and_spindle_overrides(self):
        """The Overrides block's calls: FluidNC's realtime bytes only, the value shown at once, FluidNC's word in
        its next report (4.1.1) and in the console; the feed override changes a running move's speed at once; the
        spindle goes at most 2 steps a report; reset puts all three back."""
        sim = self.ovr
        ev, _ = self.events()
        self.ready(ev, sim)
        self.wait_for(lambda: self.state()["status"]["ov"], 3, "the overrides")
        log0 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/override", {"which": "feed", "to": 120}),
                         (200, {"ok": True, "sent": 2, "expect": [120, 100, 100]}))
        ev.wait(console("info", "Feed override 120%"), since=k, timeout=2, what="FluidNC's word")
        self.assertEqual(self.realtime_names(sim, log0), ["feed+10", "feed+10"])
        k = ev.mark()
        self.send("G1 X1 F20")                          # 2.5 s at 24 in/min
        run, k = ev.wait(lambda e: e["type"] == "status" and e["state"] == "Run" and e["feed"], since=k, timeout=3,
                         what="Run")
        self.assertAlmostEqual(run["feed"], 24, delta=0.6)
        self.assertEqual(self.call("/api/machine/override", {"which": "feed", "step": -10})[1]["expect"],
                         [110, 100, 100])
        ev.wait(lambda e: e["type"] == "status" and e["state"] == "Run" and abs((e["feed"] or 0) - 22) < 0.6,
                since=k, timeout=2, what="the move at 110 %")
        self.wait_idle_at([1, 0, 0], 1e-4)
        self.send("M3 S10000")
        log1 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/override", {"which": "spindle", "to": 150}),
                         (200, {"ok": True, "sent": 2, "expect": [110, 100, 120]}))
        ev.wait(console("info", "Spindle override 150%"), since=k, timeout=4, what="the spindle at 150 %")
        rt = [e for e in sim.realtime(log1) if e.startswith("spindle") or e == "?"]
        self.assertEqual([e for e in rt if e != "?"], ["spindle+10"] * 5)
        runs = "".join("s" if e != "?" else " " for e in rt).split()
        self.assertLessEqual(max(map(len, runs)), 2, rt)    # (never more than 2 between two reports)
        st = self.wait_for(lambda: (lambda st: st if st["spindle"] == 15000 else None)(self.state()["status"]), 3,
                           "S 10000 x 150 %")
        self.assertEqual((st["ov"], st["ovTarget"], st["programmedS"]), ([110, 100, 150], None, 10000))
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/override", {"which": "rapid", "to": 25})[0], 200)
        ev.wait(console("info", "Rapid override 25%"), since=k, timeout=2)
        self.assertEqual(self.call("/api/machine/override", {"which": "rapid", "to": 5})[0], 400)
        self.assertEqual(self.call("/api/machine/override", {"which": "spindle", "step": 1, "to": 3})[0], 400)
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/override", {"which": "all", "reset": True}),
                         (200, {"ok": True, "sent": 3, "expect": [100, 100, 100]}))
        ev.wait(console("info", "Overrides: feed 100%, rapid 100%, spindle 100%"), since=k, timeout=2)
        self.send("M5")
        self.assertEqual(ev.find(console("error"), since=k), [])

    def test_xt_quick_vacuum_clicks_in_fluidncs_own_order(self):
        """FluidNC 4.1.1 runs a line that came in the same read as a toggle before the toggle (the simulator's
        --late-events: its main loop takes the line, then the events). A $G written with the toggle would show the
        modes from before it, and on a gpio pin nothing would correct the switch (the reviewer's B). Quick clicks:
        each one toggles, each $G comes a moment after the toggle before it, and the switch ends where FluidNC's
        own $G says, on and then off."""
        sim = self.gpio
        ev, _ = self.events()
        self.ready(ev, sim)
        self.wait_for(lambda: self.state()["status"]["flood"] is False, 3, "the vacuum off")
        log0 = len(sim.entries())
        for on, gap in ((True, 0.03), (False, 0.12), (True, 0.3), (False, 0.06), (True, 0.5)):
            self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": on}),
                             (200, {"ok": True, "sent": True}), on)
            time.sleep(gap)
        for want, mode in ((True, "M8"), (False, "M9")):
            time.sleep(0.5)
            self.assertIs(self.state()["status"]["flood"], want)
            k = ev.mark()
            self.send("$G")                             # (FluidNC's own word: the relay)
            gc, _ = ev.wait(console("rx", contains="[GC:"), since=k, what="FluidNC's $G")
            self.assertIn(f" {mode} ", gc["text"])
            if want:
                self.assertEqual(self.call("/api/machine/coolant", {"which": "vacuum", "on": False}),
                                 (200, {"ok": True, "sent": True}))
        last = None
        for t, e in sim.timed(log0):
            if e == "RT flood-toggle":
                last = t
            elif e == "LINE $G" and last is not None:      # (in a later read than the toggle: FluidSender holds it
                self.assertGreater(t - last, 0.005, "a $G came with a toggle")      # 50 ms, minus the sim's lag)
        self.assertEqual(self.realtime_names(sim, log0).count("flood-toggle"), 6)

    def test_xu_spindle_override_steps_wait_for_a_vfd(self):
        """A VFD spindle with speed feedback (the simulator's --spindle vfd --vfd-sync 1): FluidNC waits for its speed
        at every spindle override step while it stands, and the override bytes that come meanwhile wait for it.
        Three quick Spindle -10 clicks end at 70 % (the reviewer's A: 50 %), and a Feed +10 during that wait at 110 %:
        no override byte goes twice."""
        sim = self.vfd
        ev, _ = self.events()
        self.ready(ev, sim)
        self.send("M3 S10000")
        self.wait_for(lambda: (self.state()["status"] or {}).get("spindle") == 10000, 3, "the spindle on")
        self.wait_for(lambda: self.state()["status"]["ov"] == [100, 100, 100], 3, "the overrides")
        log0 = len(sim.entries())
        k = ev.mark()
        for n in range(3):
            st, data = self.call("/api/machine/override", {"which": "spindle", "step": -10})
            self.assertEqual(st, 200, data)
            time.sleep(0.3)
        ev.wait(console("info", "Spindle override 70%"), since=k, timeout=6, what="the spindle at 70 %")
        self.assertEqual(self.realtime_names(sim, log0), ["spindle-10"] * 3)
        st = self.wait_for(lambda: (lambda s: s if s["spindle"] == 7000 else None)(self.state()["status"]), 3,
                           "S 10000 x 70 %")
        self.assertEqual((st["ov"], st["ovTarget"]), ([100, 100, 70], None))
        log1 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/override", {"which": "spindle", "step": 10})[0], 200)
        time.sleep(0.05)                                # (FluidNC waits for the VFD for 1 s now)
        self.assertEqual(self.call("/api/machine/override", {"which": "feed", "step": 10})[1]["expect"],
                         [110, 100, 80])
        ev.wait(console("info", "Feed override 110%"), since=k, timeout=4, what="the feed at 110 %")
        self.wait_for(lambda: self.state()["status"]["ov"] == [110, 100, 80], 3, "feed 110 %, spindle 80 %")
        self.assertEqual([e for e in self.realtime_names(sim, log1) if e.startswith("feed")], ["feed+10"])
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/override", {"which": "all", "reset": True})[0], 200)
        ev.wait(console("info", contains="100%"), since=k, timeout=4, what="the overrides back to 100 %")
        k = ev.mark()
        self.send("M5")                                 # (it waits for the VFD too)
        _, k = ev.wait(console("tx", "M5"), since=k)
        ev.wait(console("rx", "ok"), since=k, timeout=4, what="the ok for M5")
        self.assertEqual(ev.find(console("error"), since=0), [])

    def test_xr_a_critical_alarm_is_reset_never_held(self):
        """A hard limit (FluidNC: critical, reported as Alarm). Connected after one: FluidNC 4.1.1 answers $I, so
        $T tells it ("State 11 (Critical)"). Feed Hold and Resume are refused, and Stop with a job's lines still in
        FluidNC's hands is the Soft Reset at once, with no '!' (a '!' would let FluidNC out of the alarm without
        homing)."""
        sim = self.crit
        sim.proc.send_signal(signal.SIGUSR1)            # tripped before FluidSender connects
        time.sleep(0.8)
        ev, _ = self.events()
        log0 = len(sim.entries())
        i = self.connect(ev, sim)
        ev.wait(console("rx", "State 11 (Critical)"), since=i, timeout=3, what="$T's answer")
        a, _ = ev.wait(lambda e: e["type"] == "alarm" and e["alarm"] and e["alarm"]["critical"], since=i,
                       what="the critical alarm")
        self.assertEqual(a["alarm"]["text"], "$T: State 11 (Critical)")
        ev.wait(console("info", "FluidNC is in a critical alarm (hard or soft limit): only a Soft Reset gets out of "
                                "it"), since=i)
        ev.wait(lambda e: e["type"] == "axes", since=i, what="the reads done")
        self.assertEqual(sim.lines(log0), ["$I", "$G", "$T", *READS])
        for cmd in ("hold", "resume"):
            self.assertEqual(self.call("/api/machine/realtime", {"cmd": cmd}), (409, {"error": IN_ALARM}), cmd)
        k = ev.mark()
        self.realtime("reset")                          # (all axes unhomed: ALARM:14)
        ev.wait(console("rx", "ALARM:14"), since=k, timeout=3)
        ev.wait(console("info", "*** Controller reset"), since=k, timeout=3)
        self.assertEqual(self.press("/api/machine/unlock", {}), (200, {"ok": True, "line": "$X"}))
        _, k = ev.wait(console("tx", "G20"), since=k, timeout=5, what="G20 once Idle")
        ev.wait(console("rx", "ok"), since=k)
        k = self.run_job(ev, "G0 X0 Y0\nG1 X3 F30\n" + "".join(f"G1 Y{0.1 * i:.1f}\n" for i in range(1, 30)),
                         "crash.nc")
        ev.wait(status("Run"), since=k, timeout=5, what="Run")
        sim.proc.send_signal(signal.SIGUSR1)            # a limit switch mid-job
        ev.wait(job_state("failed"), since=k, timeout=5, what="the job failed")
        ev.wait(lambda e: e["type"] == "alarm" and e["alarm"] and e["alarm"]["critical"], since=k, timeout=3)
        log1 = len(sim.entries())
        self.assertEqual(self.call("/api/machine/realtime", {"cmd": "hold"}), (409, {"error": IN_ALARM}))
        self.assertEqual(self.call("/api/machine/stop", {}), (200, {"ok": True, "stopped": "motion"}))
        self.wait_for(lambda: "^X" in sim.realtime(log1), 3, "the reset")
        self.assertNotIn("!", sim.realtime(log1))
        ev.wait(console("rx", "ALARM:14"), since=k, timeout=3, what="ALARM:14 (unhomed: not let out)")
        ev.wait(console("info", "Stop: FluidNC is in ALARM: Soft Reset at once, no Feed Hold"), since=k)

    def test_xs_fluidnc_394_still_works(self):
        """FluidNC 3.9.4 (--fw 3.9.4): its banner and $I, and connected in a critical alarm it refuses $I
        (error:8), which tells it. Lines FluidNC 4.1.1 would refuse (over 127 characters of G-code) are refused
        here for both versions, when typed and when loaded."""
        sim = self.v394
        ev, _ = self.events()
        self.ready(ev, sim)
        self.assertEqual(self.state()["firmware"], FW394)
        line = "G1 X0.5 F20 (" + "x" * 114 + ")"
        self.assertEqual(self.call("/api/machine/send", {"line": line}), (400, {
            "error": "Line too long for FluidNC (128 characters; it refuses G-code lines over 127, spaces and comments "
                     "included)"}))
        st, data = self.call("/api/machine/program", {"name": "long.nc", "text": "G0 X0\nG1X" + "1" * 125 + "\n"})
        self.assertEqual((st, data), (400, {"error": "Program not loaded: Line 2 is too long: 128 characters without "
                                                     "comments and spaces (FluidNC refuses G-code lines over 127 "
                                                     "characters: error:14)"}))
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/disconnect", {}), (200, {"ok": True}))
        ev.wait(conn(False), since=k, what="disconnected")
        sim.proc.send_signal(signal.SIGUSR1)            # a hard limit while FluidSender isn't connected
        time.sleep(0.8)
        log0 = len(sim.entries())
        ev, _ = self.events()                           # (connect() looks for the first conn event of a stream)
        i = self.connect(ev, sim)
        ev.wait(console("info", "FluidNC refused $I: it is in a critical alarm (hard or soft limit), so only a Soft "
                                "Reset gets out of it"), since=i, timeout=3)
        ev.wait(console("rx", "State 10 (Critical)"), since=i, timeout=3)
        self.assertTrue(self.state()["alarm"]["critical"])
        self.assertEqual(self.call("/api/machine/realtime", {"cmd": "hold"}), (409, {"error": IN_ALARM}))
        k = ev.mark()
        self.realtime("reset")
        ev.wait(console("rx", "ALARM:14"), since=k, timeout=3)
        ev.wait(console("rx", contains="FluidNC v3.9.4"), since=k, timeout=3, what="3.9.4's banner")
        self.assertEqual(sim.lines(log0)[:3], ["$I", "$G", "$T"])

    # -------------------------------------------------- a page opened later

    def test_xm_a_page_opened_later_gets_the_program(self):
        """GET /api/machine/program: the G-code a page opened later shows, exactly as a page sent it, the
        running job's while one runs (with who started it), nothing once it is unloaded; only for the page."""
        sim = self.jobs
        ev, _ = self.events()
        self.ready(ev, sim)
        text = "(view.nc)\r\nG90 G20\r\n\r\nG0 X0.1 ; there\r\nG0 X0\r\n"
        loaded = self.load(text, "view.nc")
        want = {"name": "view.nc", "lines": loaded["lines"], "total": loaded["total"], "warnings": [], "job": None}
        self.assertEqual(self.call("/api/machine/program"), (200, {**want, "text": text}))
        self.assertEqual(self.call("/api/machine/program?text=0"), (200, want))
        self.assertEqual(self.call("/api/machine/program", headers={}), (403, {"error": "forbidden"}))
        # A job runs (held at its M0): its program, though another one has been loaded since
        job = "G0 X0 Y0\nM0 (wait)\nG0 X0\n"
        k = self.run_job(ev, job, "job.nc")
        _, k = ev.wait(job_state("paused"), since=k, timeout=10, what="the M0 pause")
        self.load(text, "view.nc")
        st, data = self.call("/api/machine/program")
        self.assertEqual((st, data["name"], data["text"]), (200, "job.nc", job))
        self.assertEqual(data["job"], {"started": self.state()["job"]["started"], "source": "page"})
        time.sleep(0.6)
        self.realtime("resume")
        ev.wait(job_state("done", "failed", "stopped"), since=k, timeout=10, what="the job done")
        self.assertEqual(self.call("/api/machine/program?text=0"), (200, want))
        self.load("", "")
        self.assertEqual(self.call("/api/machine/program"), (200, {"name": None}))

    # -------------------------------------------------- lines from before, disconnect during a job

    def test_xn_connected_while_fluidnc_holds_lines_from_before(self):
        """Left holding lines by another program, one of them handed to its main loop already: FluidSender asks
        and sends nothing, refuses Resume (it would run them), and Soft Reset clears them. FluidNC (4.1.1 too) still
        runs that one line after the reset (the simulator too): it is held at once, then reset again, and only
        then go the connect reads and G20. First of all FluidSender turns FluidNC's UART echo off (^L)."""
        sim = self.held
        fd = os.open(sim.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)     # the other program, before us
        try:
            os.write(fd, b"G20 G1 X1 F10\n")            # 6 s of moving
            time.sleep(0.3)
            os.write(fd, b"!")
            time.sleep(0.5)                             # held: Hold:0
            os.write(fd, b"G0 X0.5\nG0 X0.6\n")         # read during the hold, the first one by its main loop
            time.sleep(0.2)
        finally:
            os.close(fd)
        log0 = len(sim.entries())
        ev, _ = self.events()
        k = ev.mark()
        self.connect(ev, sim)
        ev.wait(console("error", OLD_HOLD), since=k, timeout=3, what="the hold from before")
        time.sleep(0.5)
        self.assertEqual(sim.entries(log0)[:2], ["CTRL ^L", "RT ?"])   # the echo off, before the first '?'
        self.assertEqual(sim.lines(log0), [])           # nothing asked, nothing sent
        self.assertEqual(self.call("/api/machine/realtime", {"cmd": "resume"}), (409, {"error": OLD_HOLD}))
        k = ev.mark()
        self.realtime("reset")
        ev.wait(console("error", LEFTOVER), since=k, timeout=5, what="the line from before held")
        ev.wait(console("tx", "G20"), since=k, timeout=8, what="G20 after the second reset")
        self.wait_for(lambda: "G20" in sim.lines(log0), 3, "G20 at FluidNC")         # (the sim logs it a bit later)
        entries = [e for e in sim.entries(log0) if e not in ("RT ?", "CTRL ^L")]
        self.assertEqual(entries[:4], ["RT ^X", "LEFTOVER G0 X0.5", "RT !", "RT ^X"])
        self.assertEqual(sim.lines(log0), ["$I", "$G", *READS, "G20"])
        self.wait_idle_at()
        self.assertEqual(self.state()["firmware"], FW411)
        self.assertEqual(ev.find(console("rx", contains="ALARM"), since=k), [])

    def test_xo_disconnect_during_a_job_stops_it_safely_first(self):
        """Disconnect (or stopping the server) during a job: the safe stop first, and the port closes only after
        its reset. Connected again, FluidNC is Idle where it stopped: not holding the job's lines, the spindle
        on, with nobody watching."""
        sim = self.jobs
        ev, _ = self.events()
        self.ready(ev, sim)
        k = self.run_job(ev, "G0 X0 Y0\nG1 X3 F30\nG1 X0 F30\n", "long.nc")   # 12 s of moving
        ev.wait(lambda e: e["type"] == "status" and e["state"] == "Run" and (e["wpos"] or [0])[0] > 0.05, since=k,
                timeout=8, what="moving")
        log0 = len(sim.entries())
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/disconnect", {}), (200, {"ok": True}))
        ev.wait(conn(False), since=k, timeout=3, what="disconnected")
        rt = self.times(sim, log0, "RT !", "RT ^X")
        self.assertEqual([e for _, e in rt], ["RT !", "RT ^X"])
        self.assertGreaterEqual(rt[1][0] - rt[0][0], HOLD_DECEL - 0.02)    # the reset once it stood still
        self.assertEqual(ev.find(job_state("stopped", "failed", "done"), since=k)[-1]["job"]["state"], "stopped")
        ev.wait(console("info", "Disconnect during the job: the port closes once FluidNC has stopped and been "
                                "reset"), since=k)
        log1 = len(sim.entries())
        ev2, _ = self.events()                          # (connect() looks for the first conn event of a stream)
        self.ready(ev2, sim)
        st = self.wait_idle_at()
        self.assertGreater(st["wpos"][0], 0.05)         # where it stopped
        self.wait_for(lambda: "G20" in sim.lines(log1), 3, "G20 at FluidNC")    # (the sim logs it a bit later)
        self.assertEqual(sim.lines(log1), ["$I", "$G", *READS, "G20"])
        self.assertEqual(ev.find(console("rx", contains="ALARM"), since=k), [])

    # -------------------------------------------------- the settings file

    def test_zz_settings_kept_across_a_restart(self):
        """The jog step and speed, the scan settings and the last probe and pendant ports: in settings.json (a
        temporary one here), read again by the next server."""
        self.assertEqual(self.call("/api/machine/jogsettings", {"step": 0.01, "feed": 75}),
                         (200, {"ok": True, "step": 0.01, "feed": 75}))
        scan = {"minX": 0.25, "minY": 0.5, "maxX": 4.0, "maxY": 3.0, "xPoints": 7, "yPoints": 4, "zRetract": 0.15,
                "probeOffX": 0.0, "probeOffY": -3.0}       # (the probe offset from the spindle is kept too)
        st, data = self.call("/api/scan/settings", scan)
        self.assertEqual((st, data), (200, {"ok": True, "settings": scan}))
        st, data = self.call("/api/pendant/connect", {"port": self.pendant.path})
        self.assertEqual(st, 200, data)
        self.assertEqual(self.call("/api/pendant/disconnect", {}), (200, {"ok": True}))
        self.assertEqual(self.call("/api/probe/connect", {"port": self.probe.path})[0], 200)
        self.assertEqual(self.call("/api/probe/disconnect", {}), (200, {"ok": True}))
        undo = self.state()["zeroUndo"]                 # (Undo Zero's records, if the tests before made any)
        cls = type(self)
        cls.server.stop()
        cls.server = cls.start_server()
        snap = self.state()
        self.assertEqual((snap["jogSettings"], snap["scan"]["settings"]), ({"step": 0.01, "feed": 75}, scan))
        self.assertEqual(snap["zeroUndo"], undo)
        self.assertEqual(self.call("/api/pendant/ports")[1]["default"], self.pendant.path)
        self.assertEqual(self.call("/api/probe/ports")[1]["default"], self.probe.path)
        with open(os.path.join(self.tmp.name, "settings.json")) as f:
            saved = json.load(f)
        self.assertEqual(len(saved.pop("zeros")), undo["count"] if undo else 0)
        self.assertEqual(saved, {"jog": {"step": 0.01, "feed": 75}, "scan": scan, "pendantPort": self.pendant.path,
                                 "probePort": self.probe.path})


if __name__ == "__main__":
    unittest.main()
