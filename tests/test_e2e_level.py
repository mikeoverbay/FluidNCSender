"""
End to end for auto-level: pretend FluidNCs (tools/fake_fluidnc.py, 4.1.1, reporting inches like the user's machine)
and a pretend probe (tools/fake_probe.py) on pseudo terminals, a real server on port 18820
(FLUIDSENDER_LEVEL_TEST_PORT sets another; else FLUIDSENDER_TEST_PORT + 1, next to test_e2e's, so that a whole run
with FLUIDSENDER_TEST_PORT set stays in its own ports), and the HTTP calls and event stream the way the page uses them: a scan
writes its setup line; POST /api/machine/level, GET /api/machine/program with "leveled", Run sends exactly the
leveled lines and says so, Leveling off; what turns it off (another program, a new scan, another FluidNC); a moved
X/Y zero (a warning, never a refusal); the refusals (a job, a scan, a line the leveler refuses, bad calls); Save
leveled G-code.

No real serial port is ever opened: the server runs with FLUIDSENDER_ONLY_EXTRA_PORTS=1, so it can list and open
nothing but the simulators' ptys. Its settings file and scans folder are temporary.

    python3 -m unittest tests.test_e2e_level        (about a minute)
"""

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import leveler  # noqa: E402
from job import parse_program  # noqa: E402

SIM = os.path.join(ROOT, "tools", "fake_fluidnc.py")
PROBE = os.path.join(ROOT, "tools", "fake_probe.py")
SERVER = os.path.join(ROOT, "server.py")
PORT = int(os.environ.get("FLUIDSENDER_LEVEL_TEST_PORT")
           or (int(os.environ["FLUIDSENDER_TEST_PORT"]) + 1 if os.environ.get("FLUIDSENDER_TEST_PORT") else 18820))
H = {"X-FluidSender": "1"}
# the probe's touches, in the order probed (column by column, zigzag in Y), each relative to the first
SURFACE = (0.5, 0.51, 0.49, 0.52, 0.5, 0.47)
SCAN = {"minX": 0, "maxX": 0.2, "minY": 0, "maxY": 0.1, "xPoints": 3, "yPoints": 2, "zRetract": 0.2}
UGS_HEADER = ["# minX=0.00000 maxX=0.20000", "# minY=0.00000 maxY=0.10000", "# xSamples=3 ySamples=2", "# units=INCH"]
POINTS = ["0.0 0.0 0.0", "0.0 0.1 0.01", "0.1 0.0 0.02", "0.1 0.1 -0.01", "0.2 0.0 0.0", "0.2 0.1 -0.03"]
# the simulator's work offset (-150, -100, -30 mm) as it reports it in inches (4 decimals)
SETUP = re.compile(r"# wcs=G54 wcoX=-5\.905500 wcoY=-3\.937000 wcoZ=-1\.181100 time=(\d+)")
PROGRAM = ("(lev.nc)\nG20 G90 G54\nG0 Z0.1\nG0 X0 Y0\nG1 Z-0.01 F100 (plunge)\nG1 X0.2 Y0.1\nG1 X0 Y0.1\n"
           "G1 X0.2 Y0\nG0 Z0.1\n")
LABEL = "last-scan.xyz (3 x 2 points, Z surface 0)"


def lines_of(text):
    p = parse_program("x.nc", text)
    return [p[i][1] for i in range(len(p))]


class Sim:
    """A simulator process; its --log file lists what it received ('LINE <text>', 'RT <byte>')."""

    def __init__(self, logdir, name, *args):
        self.log = os.path.join(logdir, name + ".log")
        self.proc = subprocess.Popen([sys.executable, SIM, "--log", self.log, *args], stdout=subprocess.PIPE, text=True)
        self.path = self.proc.stdout.readline().strip()
        if not self.path.startswith("/dev/pts/"):
            self.stop()
            raise RuntimeError(f"simulator did not start: {self.path!r}")

    def entries(self, since=0):
        try:
            with open(self.log) as f:
                return [line.rstrip("\n").split(" ", 1)[1] for line in f][since:]
        except FileNotFoundError:
            return []

    def lines(self, since=0):
        return [e[5:] for e in self.entries(since) if e.startswith("LINE ")]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()


class Probe:
    """fake_probe.py on its pty."""

    def __init__(self, logdir, *args):
        self.log = os.path.join(logdir, "probe.jsonl")
        self.proc = subprocess.Popen([sys.executable, PROBE, "--log", self.log, *args], stdout=subprocess.PIPE,
                                     text=True)
        self.path = self.proc.stdout.readline().strip()
        if not self.path.startswith("/dev/pts/"):
            self.stop()
            raise RuntimeError(f"probe did not start: {self.path!r}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()


class Server:
    def __init__(self, port, ports, workdir):
        env = dict(os.environ, FLUIDSENDER_EXTRA_PORTS=":".join(ports), FLUIDSENDER_ONLY_EXTRA_PORTS="1",
                   FLUIDSENDER_RELEASE_AFTER="5", FLUIDSENDER_SETTINGS=os.path.join(workdir, "settings.json"),
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

    def call(self, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            if body is None:
                c.request("GET", path, headers=H)
            else:
                c.request("POST", path, body=json.dumps(body), headers={**H, "Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")
        finally:
            c.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class Events:
    """/api/machine/events read in a thread, like the page does."""

    def __init__(self, port):
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        self.conn.connect()
        self.sock = self.conn.sock
        self.conn.request("GET", "/api/machine/events", headers=H)
        self.resp = self.conn.getresponse()
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

    def wait(self, pred, timeout=10.0, since=0, what="an event"):
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
                    tail = json.dumps(self.list[-6:])[:1500]
                    raise AssertionError(f"no {what} within {timeout} s; last events: {tail}")
                self.cond.wait(left)

    def find(self, pred, since=0):
        with self.cond:
            return [e for e in self.list[since:] if pred(e)]

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(5)
        self.resp.close()
        self.sock.close()


def console(kind, text=None, start=None):
    return lambda e: (e["type"] == "console" and e["dir"] == kind and (text is None or e["text"] == text)
                      and (start is None or e["text"].startswith(start)))


def program_event(leveled):
    return lambda e: e["type"] == "program" and e["program"] is not None and ("leveled" in e["program"]) == leveled


def job_state(*states):
    return lambda e: e["type"] == "job" and e["job"] and e["job"]["state"] in states


def scan_phase(*phases):
    return lambda e: e["type"] == "scan" and e["scan"] and e["scan"]["phase"] in phases


class AutoLevel(unittest.TestCase):
    """One server for the whole class; the tests run in name order and build on each other."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.sims, cls.server, cls.probe = [], None, None
        try:
            cls.sim = cls.start_sim("lev", "--inches")
            cls.other = cls.start_sim("other", "--inches")
            cls.probe = Probe(cls.tmp.name, "--values", ",".join(map(str, SURFACE)), "--probe-time", "0.05",
                              "--home-time", "0.05")
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

    def ready(self, ev, sim):
        """Connected, FluidSender's G20 answered, the work position known."""
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/connect", {"port": sim.path, "baud": 115200}), (200, {"ok": True}))
        self.addCleanup(self.call, "/api/machine/disconnect", {})
        _, k = ev.wait(console("tx", "G20"), since=k, what="G20")
        ev.wait(console("rx", "ok"), since=k, what="ok for G20")
        self.wait_for(lambda: (self.state()["status"] or {}).get("wpos") is not None, "a work position", 8)

    def disconnect(self):
        self.assertEqual(self.call("/api/machine/disconnect", {}), (200, {"ok": True}))
        self.wait_for(lambda: not self.state()["connected"], "disconnected")

    def send(self, line):
        self.assertEqual(self.call("/api/machine/send", {"line": line}), (200, {"ok": True}), line)

    def wait_for(self, cond, what, timeout=10.0):
        end = time.monotonic() + timeout
        while not cond():
            self.assertLess(time.monotonic(), end, f"no {what}")
            time.sleep(0.05)

    def wait_idle_at(self, wpos, tol=1e-4, timeout=10.0):
        def there():
            st = self.state()["status"]
            return (st and st["state"] == "Idle" and st["wpos"]
                    and all(abs(g - w) <= tol for g, w in zip(st["wpos"], wpos)))
        self.wait_for(there, f"Idle at {wpos}", timeout)

    def go(self, line, wpos):
        self.send(line)
        self.wait_idle_at(wpos)

    def probe_ready(self):
        if not self.state()["probe"]["connected"]:
            self.assertEqual(self.call("/api/probe/connect", {"port": self.probe.path}),
                             (200, {"ok": True, "port": self.probe.path}))
        if not self.state()["probe"]["ready"]:
            self.ok("/api/probe/home", {})

    def scan(self, ev):
        """A 3 x 2 scan (the bit 0.25 in above the work zero first); returns its state at the end."""
        self.go("G0 X0 Y0 Z0.25", [0, 0, 0.25])
        self.probe_ready()
        k = ev.mark()
        self.assertEqual(self.call("/api/scan/start", {"settings": SCAN}), (200, {"ok": True, "points": 6}))
        end, _ = ev.wait(scan_phase("done", "failed", "stopped"), since=k, timeout=60, what="the scan's end")
        self.assertEqual((end["scan"]["phase"], end["scan"]["error"]), ("done", None))
        self.wait_idle_at([0, 0, 0.25])
        return end["scan"]

    def level(self, ev, **body):
        k = ev.mark()
        out = self.ok("/api/machine/level", {"scan": "last", **body})
        ev.wait(program_event(True), since=k, what="the leveled program's event")
        return out

    def run_job(self, ev):
        """Run, until done. Returns (the run's answer, the lines FluidNC got, the console lines of the run)."""
        self.wait_for(lambda: self.state()["status"]["state"] == "Idle", "Idle")
        log0, k = len(self.sim.entries()), ev.mark()
        out = self.ok("/api/machine/run", {})
        ev.wait(job_state("done", "failed", "stopped"), since=k, timeout=30, what="the job's end")
        self.assertEqual(self.state()["job"]["state"], "done", self.state()["job"])
        info = [e["text"] for e in ev.find(console("info"), since=k)]
        return out, self.sim.lines(log0), info

    # -------------------------------------------------- tests (in name order)

    def test_1_a_scan_writes_its_setup(self):
        """The .xyz has UGS's four header lines, then FluidSender's setup line (FluidNC's work coordinate system and
        offset, in inches, and the time), then UGS's points; leveler.py (as UGS) reads it as the same scan."""
        ev = self.events()
        self.ready(ev, self.sim)
        sc = self.scan(ev)
        lines = sc["result"]["text"].splitlines()
        self.assertEqual(lines[:4], UGS_HEADER)
        found = SETUP.fullmatch(lines[4])
        self.assertTrue(found, lines[4])
        self.assertAlmostEqual(int(found.group(1)), time.time(), delta=120)
        self.assertEqual(lines[5:], POINTS)
        with open(os.path.join(self.tmp.name, "scans", "last-scan.xyz")) as f:
            self.assertEqual(f.read(), sc["result"]["text"])
        mesh = leveler.parse_scan(sc["result"]["text"])
        self.assertEqual((mesh.header["wcs"], mesh.header["wcoX"]), ("G54", "-5.905500"))
        self.assertEqual(mesh.z, leveler.parse_scan("\n".join(UGS_HEADER + POINTS)).z)
        type(self).scan_text = sc["result"]["text"]

    def test_2_level_run_and_off(self):
        ev = self.events()
        self.ready(ev, self.sim)
        mesh = leveler.parse_scan(self.scan_text)
        want = leveler.level_text(PROGRAM, mesh, {"z_surface": 0, "arc_segment_mm": 0.1}).text()
        self.ok("/api/machine/program", {"name": "lev.nc", "text": PROGRAM})
        k = ev.mark()
        out = self.level(ev, zSurface=0, arcSegmentMm=0.1)
        self.assertEqual(set(out), {"ok", "lines", "total", "warnings", "stats", "scan"})
        self.assertEqual((out["lines"], out["total"], out["warnings"]), (len(lines_of(want)), want.count("\n") + 1, []))
        self.assertEqual(out["scan"], mesh.info())
        self.assertEqual((out["stats"]["outside"], out["stats"]["lines"]), (0, PROGRAM.count("\n")))
        ev.wait(console("info", f"Auto-level: lev.nc LEVELED with {LABEL}: {out['lines']} lines to send (from 8)"),
                since=k, what="the leveling's console line")
        # what a page gets
        st, got = self.call("/api/machine/program")
        self.assertEqual(st, 200)
        self.assertEqual((got["name"], got["lines"], got["total"], got["text"], got["original"], got["job"]),
                         ("lev.nc", out["lines"], out["total"], want, PROGRAM, None))
        info = got["leveled"]
        self.assertEqual((info["scan"], info["zSurface"], info["arcSegmentMm"], info["wcs"], info["warnings"]),
                         ("last-scan.xyz", 0.0, 0.1, "G54", []))
        self.assertEqual(info["source"], list(leveler.level_text(PROGRAM, mesh, {"arc_segment_mm": 0.1}).source))
        snap = self.state()
        self.assertEqual(snap["program"]["leveled"], {k: v for k, v in info.items() if k != "source"})
        self.assertEqual(snap["level"], {"zSurface": 0.0, "arcSegmentMm": 0.1})
        self.assertEqual(self.call("/api/machine/program?text=0")[1],
                         {"name": "lev.nc", "lines": out["lines"], "total": out["total"], "warnings": [], "job": None,
                          "leveled": snap["program"]["leveled"]})
        # Run sends exactly the leveled lines, and says so
        ran, lines, said = self.run_job(ev)
        self.assertEqual(ran["job"]["leveled"], True)
        self.assertEqual(lines, ["G20 G90"] + lines_of(want))
        self.assertIn(f"Job started: lev.nc, LEVELED with {LABEL}, {out['lines']} lines", said)
        self.assertTrue(all(e["job"]["leveled"] for e in ev.find(lambda e: e["type"] == "job", since=k)))
        # Leveling off: the original back
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/level", {"off": True}),
                         (200, {"ok": True, "lines": 8, "total": 10, "warnings": []}))
        ev.wait(program_event(False), since=k, what="the original's event")
        ev.wait(console("info", "Auto-level off: lev.nc runs as it is (not leveled) again"), since=k)
        st, got = self.call("/api/machine/program")
        self.assertEqual((got["text"], got["lines"], "leveled" in got, "original" in got), (PROGRAM, 8, False, False))
        ran, lines, said = self.run_job(ev)
        self.assertEqual((ran["job"]["leveled"], lines), (False, ["G20 G90"] + lines_of(PROGRAM)))
        self.assertIn("Job started: lev.nc, not leveled, 8 lines", said)
        with open(os.path.join(self.tmp.name, "settings.json")) as f:
            self.assertEqual(json.load(f)["level"], {"zSurface": 0.0, "arcSegmentMm": 0.1})

    def test_3_what_turns_it_off(self):
        ev = self.events()
        self.ready(ev, self.sim)
        self.ok("/api/machine/program", {"name": "lev.nc", "text": PROGRAM})
        self.level(ev)
        # another program (or an edit)
        k = ev.mark()
        self.ok("/api/machine/program", {"name": "other.nc", "text": "G0 X0.1 Y0.1 Z0.2\n"})
        ev.wait(console("info", "Auto-level off: the G-code changed"), since=k)
        self.assertEqual(self.call("/api/machine/program")[1]["name"], "other.nc")
        # a new scan
        self.ok("/api/machine/program", {"name": "lev.nc", "text": PROGRAM})
        self.level(ev)
        k = ev.mark()
        self.scan(ev)
        ev.wait(console("info", "Auto-level off: a new scan was made (Level again to use it)"), since=k)
        ev.wait(program_event(False), since=k, what="the original's event")
        self.assertNotIn("leveled", self.state()["program"])
        # another FluidNC (leveled with the scan's file this time)
        path = os.path.join(self.tmp.name, "scans", "last-scan.xyz")
        k = ev.mark()
        self.ok("/api/machine/level", {"scan": {"path": path}})
        ev.wait(program_event(True), since=k, what="the leveled program's event")
        self.assertEqual(self.state()["program"]["leveled"]["scan"], path)
        self.disconnect()
        self.assertIn("leveled", self.state()["program"])      # (a disconnect alone keeps it)
        k = ev.mark()
        self.assertEqual(self.call("/api/machine/connect", {"port": self.other.path}), (200, {"ok": True}))
        ev.wait(console("info", f"Auto-level off: connected to another FluidNC ({self.other.path}); level the G-code "
                                "again for it"), since=k)
        self.assertNotIn("leveled", self.state()["program"])
        self.disconnect()

    def test_4_refusals(self):
        ev = self.events()
        self.ready(ev, self.sim)
        self.ok("/api/machine/program", {"name": "lev.nc", "text": PROGRAM})
        # X zero changed since the scan: leveled all the same, with a warning (the user moves the table after a scan)
        self.go("G0 X0.05 Y0 Z0.25", [0.05, 0, 0.25])
        self.ok("/api/machine/zero", {"axes": "X", "value": 0})
        self.wait_for(lambda: abs(self.state()["status"]["wpos"][0]) < 1e-4, "the new X zero")
        k = ev.mark()
        st, data = self.call("/api/machine/level", {"scan": "last"})
        self.assertEqual(st, 200, data)
        ev.wait(program_event(True), since=k, what="the leveled program's event")
        self.assertIn("X/Y zero moved since the scan (X by +0.0500 in): fine if you shifted it for the "
                      "probe and put it back; otherwise the leveling is off by that much",
                      self.state()["program"]["leveled"]["warnings"])
        self.ok("/api/machine/level", {"off": True})
        self.ok("/api/machine/zero", {"undo": True})
        self.wait_for(lambda: abs(self.state()["status"]["wpos"][0] - 0.05) < 1e-4, "X back")
        self.level(ev)
        # a job: neither Level nor Leveling off
        self.ok("/api/machine/level", {"off": True})
        self.go("G0 X0 Y0 Z0.25", [0, 0, 0.25])
        self.ok("/api/machine/program", {"name": "m0.nc", "text": "G0 X0 Y0 Z0.3\nM0 (wait)\nG0 Z0.25\n"})
        k = ev.mark()
        self.ok("/api/machine/run", {})
        ev.wait(job_state("paused"), since=k, timeout=10, what="the M0 pause")
        for body in ({"scan": "last"}, {"off": True}):
            self.assertEqual(self.call("/api/machine/level", body), (409, {"error": "A job is running"}))
        time.sleep(0.6)
        self.ok("/api/machine/realtime", {"cmd": "resume"})
        ev.wait(job_state("done", "failed", "stopped"), since=k, timeout=10, what="the job's end")
        # a scan
        self.ok("/api/machine/program", {"name": "lev.nc", "text": PROGRAM})
        self.wait_idle_at([0, 0, 0.25])
        self.probe_ready()
        k = ev.mark()
        self.assertEqual(self.call("/api/scan/start", {"settings": SCAN}), (200, {"ok": True, "points": 6}))
        self.assertEqual(self.call("/api/machine/level", {"scan": "last"}), (409, {"error": "A scan is running"}))
        ev.wait(scan_phase("done", "failed", "stopped"), since=k, timeout=60, what="the scan's end")
        # what the leveler refuses (its line), and bad calls
        self.wait_idle_at([0, 0, 0.25])
        self.ok("/api/machine/program", {"name": "g92.nc", "text": "G0 X0 Y0 Z0.1\nG92 X0\nG1 X0.1 F10\n"})
        self.assertEqual(self.call("/api/machine/level", {"scan": "last"}),
                         (409, {"error": "Line 2: G92 changes the work coordinates: the scan would no longer line up "
                                         "with the program"}))
        missing = os.path.join(self.tmp.name, "nope", "scan.xyz")
        self.assertEqual(self.call("/api/machine/level", {"scan": {"path": missing}}),
                         (404, {"error": f"{missing}: No such file or directory"}))
        for body, why in (({"scan": 5}, 'scan: "last" or {"path": "/path/to/scan.xyz"}'),
                          ({"scan": "last", "arcSegmentMm": 5}, "Arc segment: a number from 0.01 to 2 mm"),
                          ({"scan": "last", "zsurface": 0}, "Unknown auto-level setting: zsurface")):
            self.assertEqual(self.call("/api/machine/level", body), (400, {"error": why}))
        self.assertNotIn("leveled", self.state()["program"])

    def test_5_save_leveled_gcode(self):
        """The page's Save leveled G-code…: POST /api/save with the leveled text as <name>-autoleveled.gcode."""
        ev = self.events()
        self.ready(ev, self.sim)
        self.ok("/api/machine/program", {"name": "lev.nc", "text": PROGRAM})
        self.level(ev, zSurface=0.005, arcSegmentMm=0.25)
        got = self.ok("/api/machine/program")
        path = os.path.join(self.tmp.name, "lev-autoleveled.gcode")
        self.assertEqual(self.call("/api/save", {"path": path, "text": got["text"]}),
                         (200, {"ok": True, "path": path, "size": len(got["text"])}))
        with open(path, newline="") as f:
            self.assertEqual(f.read(), got["text"])
        self.assertEqual(got["text"], leveler.level_text(PROGRAM, leveler.parse_scan(self.state()["scan"]["result"]
                                                                                       ["text"]),
                                                         {"z_surface": 0.005, "arc_segment_mm": 0.25}).text())
        self.assertEqual(self.state()["level"], {"zSurface": 0.005, "arcSegmentMm": 0.25})


if __name__ == "__main__":
    unittest.main()
