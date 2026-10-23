#!/usr/bin/env python3
"""
FluidSender: a small local web server for the sender's page.

Python standard library only. Serves the page in static/, lets it list, read and save
G-code (.nc, .gcode, .ngc, .tap) and scan (.xyz) files, and holds the FluidNC serial
connection (machine.py), the surface probe (probe.py), the surface scan (scan.py) and the
Bluetooth jog pendant (pendant.py). Listens on this computer only.

    python3 server.py              then open http://localhost:8765
    python3 server.py --dir PATH   start the file list in PATH

The /api/ calls only answer the page itself: they need the X-FluidSender header (other
websites can't send it) and a localhost Host header (blocks DNS rebinding).

The serial ports are opened only by the page's Connect buttons, never by the server itself.
GET /api/machine/events streams the state to the page, one JSON object per line (a snapshot
first). GET /api/machine/program is the G-code a page opened later shows: the running job's while a
job runs, else the loaded program, as the page sent it: {"name", "lines", "total", "warnings", "text",
"job": {"started", "source": "page" | "pendant"} or null} ({"name": null}: none; ?text=0 leaves the
text out). A leveled program (auto-level, leveling.py) has the leveled text, and "leveled": {"scan" (its name,
"last-scan.xyz", or the .xyz path), "time", "zSurface", "arcSegmentMm", "stats", "warnings", "wcs", "scanned",
"source" (the program line of each leveled line; not with ?text=0)} and "original" (the text as loaded); the
program event and the snapshot's "program" carry the same "leveled" (without "source"), and the job "leveled":
true | false (false: not leveled, though a scan exists in this session). The other calls are POST, JSON in and
out; a refused one answers 409 (400 for a bad call) with {"error": reason}:

  /api/machine/...
    connect {"port", "baud", "force"}   disconnect {}            send {"line": "G0 X1"}
    realtime {"cmd": "hold" | "resume" | "reset" | "status" | "jogcancel"}
    jog {"step": {"x": 0.1, "y": 0, "z": 0}, "feed": 150}     -> {"ok": true, "line": "$J=..."}
    jog {"start": {"x": 1, "y": -1, "z": 0}, "feed": 150}     -> {"ok": true, "id": 7}  (a hold jog)
    jog {"keep": 7} every 100 ms while held, then {"stop": 7} (or {"stop": "all"})
    zero {"axes": "XY", "value": 0} or {"undo": true}         -> {"ok": true, "line": "G10 L20 P0 ..."} (undo: "G10 L2 P1 ...")
    home {"axes": ""} ($H; "X", "Y" or "Z" for one axis)      unlock {} ($X)
    coolant {"which": "vacuum" | "aux", "on": true}           -> {"ok": true, "sent": true}
    override {"which": "feed" | "spindle", "to": 10..200} or {"which": "feed" | "spindle", "step": 1|-1|10|-10},
      {"which": "rapid", "to": 100 | 50 | 25}, {"which": "all", "reset": true}
                                                              -> {"ok": true, "sent": 3, "expect": [110, 100, 100]}
    goto {"to": "xy" | "z" | "zero" | "mx" | "my" | "mz"}     -> {"ok": true, "lines": [...]}
    program {"name": "a.nc", "text": "..."} (up to 64 MB; "text": "" unloads) -> {"ok", "lines", "total",
      "warnings": [...]} (lines FluidNC runs otherwise than they read: they still run)
    level {"scan": "last" | {"path": "/abs/x.xyz"}, "zSurface": 0, "arcSegmentMm": 0.1} -> {"ok", "lines",
      "total", "warnings", "stats", "scan"}: the loaded program leveled (it becomes the loaded one; 409 with
      the reason, naming the line); {"off": true}: back to the original. Not during a job or a scan
    run {}  -> {"ok": true, "job": {...}}       stop {} (the safe stop) -> {"ok": true, "stopped": "job"}
    jogsettings {"step": 0.1, "feed": 150}    -> {"ok": true, "step": 0.1, "feed": 150}
  /api/pendant/...   GET ports; connect {"port", "force"}; disconnect {}
  /api/probe/...     GET ports; connect {"port", "force"}; disconnect {}; home {}
  /api/scan/...      start {"settings": {...}}; stop {}; settings {...} (saves them); save {"path": "a.xyz"}

Distances are in inches, speeds in inches per minute. The probe, scan and pendant calls answer
503 if their module could not be loaded (the rest of FluidSender works without them).

For tests: FLUIDSENDER_EXTRA_PORTS (colon separated) adds
ports to the list, FLUIDSENDER_ONLY_EXTRA_PORTS=1 lists and opens nothing else,
FLUIDSENDER_OPEN_ONLY_EXTRA_PORTS=1 still lists the real ports but opens nothing else (to try
the Connect dialog safely), FLUIDSENDER_RELEASE_AFTER sets the seconds before an unwatched
port is released (10), FLUIDSENDER_SETTINGS the settings file (settings.json here) and
FLUIDSENDER_SCANS the folder the last scan is written to (scans/ here).
"""

import argparse
import importlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import traceback
import weakref
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from machine import RELEASE_AFTER, Machine, MachineError
from settings import Settings, SettingsError

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
GCODE_EXT = (".nc", ".gcode", ".ngc", ".tap")
EXTENSIONS = GCODE_EXT + (".xyz",)
GVFS = f"/run/user/{os.getuid()}/gvfs/"
SHARE_URL = "smb://10.0.0.31/mikes_creations"     # MIKESMACHINE's share; gvfs shows it at DEFAULT_DIR's top
DEFAULT_DIR = GVFS + "smb-share:server=10.0.0.31,share=mikes_creations/!_CarveMaker"
MOUNT_RETRY = 30        # s between tries to mount the share while it isn't there
MAX_FILE = 64 * 1024 * 1024
MAX_BIG_CALL = 2 * MAX_FILE + 64 * 1024     # a 64 MB file as JSON text (its escapes make it longer)
MAX_CALL = 64 * 1024
BIG_CALLS = ("/api/save", "/api/machine/program")
MACHINE_CALLS = ("connect", "disconnect", "send", "realtime", "jog", "zero", "home", "unlock",
                 "coolant", "override", "goto", "program", "level", "run", "stop", "jogsettings")
EXTRA_CALLS = {"pendant": ("connect", "disconnect"), "probe": ("connect", "disconnect", "home"),
               "scan": ("start", "stop", "settings", "save")}
PING_EVERY = 10.0
PING = b'{"type":"ping"}\n'


def load_extras(machine, settings):
    """The probe, the scanner and the pendant, from probe.py, scan.py and pendant.py if they are there and
    load (a missing or broken one only turns its calls off). Imported here, not at the top: the server
    runs without them."""
    def module(name):
        try:
            return importlib.import_module(name)
        except Exception as e:
            print(f"FluidSender: {name}.py not available ({e!r}); its calls answer 503", flush=True)
            if not isinstance(e, ImportError):
                traceback.print_exc()
            return None

    def make(what, build):
        try:
            return build()
        except Exception:
            print(f"FluidSender: could not start the {what}; its calls answer 503", flush=True)
            traceback.print_exc()
            return None

    probe_mod, scan_mod, pendant_mod = module("probe"), module("scan"), module("pendant")
    probe = scanner = pendant = None
    if probe_mod:
        def new_probe():
            try:                        # (in test mode it may open the test ports only)
                return probe_mod.Probe(machine.log, machine.emit, test_ports=machine.extra,
                                       only_test_ports=machine.open_only_extra)
            except TypeError:
                return probe_mod.Probe(machine.log)
        probe = make("probe", new_probe)
    if scan_mod and probe:
        scanner = make("surface scan", lambda: scan_mod.Scanner(machine, probe, settings, machine.emit, machine.log))
    if pendant_mod:
        pendant = make("pendant", lambda: pendant_mod.PendantLink(machine, settings))
    machine.attach(probe=probe, scanner=scanner, pendant=pendant)
    return probe_mod, probe, scanner, pendant


class Handler(SimpleHTTPRequestHandler):
    start_dir = DEFAULT_DIR
    allowed_hosts = ()
    machine = None      # the FluidNC connection (machine.Machine), shared by every page
    settings = None     # settings.Settings
    probe_mod = None    # probe.py, and what was made from it and scan.py and pendant.py (None: not available)
    probe = None
    scanner = None
    pendant = None
    # The G-code text of each program a page uploaded (the machine keeps only the lines it sends), for a page
    # opened later: GET /api/machine/program. One upload at a time, so the text kept is the program loaded.
    texts = weakref.WeakKeyDictionary()
    upload_lock = threading.Lock()
    # Who started the job running (that call says too): the pendant's Stop and Pause keys work only for a job its
    # own Run key started. page_job: the "started" of the last job a page's Run started; page_run: since when a
    # page's Run is starting one (a page may ask about the new job before page_job is set)
    page_job = None
    page_run = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC, **kwargs)

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet

    def end_headers(self):
        # Always load the newest page files after an update
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def api_allowed(self):
        if self.headers.get("Host", "") not in self.allowed_hosts or self.headers.get("X-FluidSender") != "1":
            self.send_json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            if not self.api_allowed():
                return
            q = parse_qs(url.query)
            if url.path == "/api/list":
                return self.list_dir(q.get("dir", [""])[0])
            if url.path == "/api/file":
                return self.read_file(q.get("path", [""])[0])
            if url.path == "/api/machine/ports":
                return self.send_json(self.machine.ports())
            if url.path == "/api/machine/state":
                return self.send_json(self.machine.snapshot())
            if url.path == "/api/machine/events":
                return self.machine_events()
            if url.path == "/api/machine/program":
                return self.program_text(q.get("text", ["1"])[0] != "0")
            if url.path in ("/api/pendant/ports", "/api/probe/ports"):
                return self.extra_ports(url.path.split("/")[2])
            return self.send_json({"error": "unknown call"}, 404)
        return super().do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        if not self.api_allowed():
            return
        kind, _, call = url.path.removeprefix("/api/").partition("/")
        known = (url.path == "/api/save" or (kind == "machine" and call in MACHINE_CALLS)
                 or call in EXTRA_CALLS.get(kind, ()))
        if not known:
            return self.send_json({"error": "unknown call"}, 404)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > (MAX_BIG_CALL if url.path in BIG_CALLS else MAX_CALL):
                what = "file" if url.path in BIG_CALLS else "request"
                return self.send_json({"error": f"{what} is too big"}, 400)
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad request"}, 400)
        if url.path in BIG_CALLS and isinstance(data.get("text"), str) and len(data["text"]) > MAX_FILE:
            return self.send_json({"error": "file is too big (64 MB at most)"}, 400)
        if url.path == "/api/save":
            return self.save_file(str(data.get("path", "")), str(data.get("text", "")))
        if kind == "machine":
            return self.machine_call(call, data)
        return self.extra_call(kind, call, data)

    # ------------------------------------------------------------------ machine

    def machine_call(self, call, data):
        m = self.machine
        out = None
        try:
            if call == "connect":
                m.connect(data.get("port"), data.get("baud"), bool(data.get("force")))
            elif call == "disconnect":
                m.disconnect()
            elif call == "send":
                m.send(data.get("line"))
            elif call == "jog":
                out = m.jog(data)
            elif call == "zero":
                out = m.zero(data.get("axes"), data.get("value"), data.get("undo") is True)
            elif call == "home":
                out = m.home(data.get("axes", ""))
            elif call == "unlock":
                out = m.unlock()
            elif call == "coolant":
                out = m.coolant(data.get("which"), data.get("on"))
            elif call == "override":
                out = m.override(data)
            elif call == "goto":
                out = m.goto(data.get("to"))
            elif call == "program":
                with self.upload_lock:
                    out = m.load_program(data.get("name"), data.get("text"))
                    if m.program is not None:
                        self.texts[m.program] = data.get("text")
            elif call == "level":
                out = m.level(data)
            elif call == "run":
                Handler.page_run = time.time()
                try:
                    out = m.run()
                    Handler.page_job = (out or {}).get("job", {}).get("started")
                finally:
                    Handler.page_run = None
            elif call == "stop":
                out = m.stop()
            elif call == "jogsettings":
                out = m.set_jog_settings(data.get("step"), data.get("feed"))
            else:
                m.realtime(data.get("cmd"))
        except MachineError as e:
            return self.send_json({"error": str(e)}, e.status)
        self.send_json({"ok": True, **(out or {})})

    def program_text(self, with_text):
        """GET /api/machine/program: what a page with no G-code of its own shows (see the top)."""
        m = self.machine
        job = m.job
        running = job is not None and job.active
        program = job.program if running else m.program
        if program is None:
            return self.send_json({"name": None})
        out = {"name": program.name, "lines": len(program), "total": program.total,
               "warnings": list(program.warnings), "job": None}
        if running:
            started, since = round(job.started, 3), self.page_run
            page = started == self.page_job or (since is not None and job.started >= since)
            out["job"] = {"started": started, "source": "page" if page else "pendant"}
        leveled = m.leveling.program_fields(program, with_text)    # (auto-level: "leveled", the leveled text ...)
        if leveled:
            out.update(leveled)
        elif with_text:
            out["text"] = self.texts.get(program)
        return self.send_json(out)

    # ------------------------------------------------------------------ pendant, probe, scan

    def extra(self, kind):
        """The pendant, probe or scanner; MachineError 503 if its module isn't there."""
        obj = {"pendant": self.pendant, "probe": self.probe, "scan": self.scanner}[kind]
        if obj is None:
            name = {"pendant": "The pendant", "probe": "The probe", "scan": "The surface scan"}[kind]
            raise MachineError(f"{name} is not available in this FluidSender ({kind}.py could not be loaded)", 503)
        return obj

    def extra_ports(self, kind):
        """The port list for the Connect Pendant / Connect Probe dialogs, with the port to offer first."""
        try:
            obj = self.extra(kind)
            if hasattr(obj, "ports"):
                return self.send_json(obj.ports())
            data = self.machine.ports()
            last = self.settings.get("probePort") if kind == "probe" else None
            paths = [p["path"] for p in data["ports"]]
            pick = getattr(self.probe_mod, "default_port", None) if kind == "probe" else None
            data["default"] = last if last in paths else (pick(data["ports"]) if pick else None)
            return self.send_json(data)
        except MachineError as e:
            return self.send_json({"error": str(e)}, e.status)

    def extra_call(self, kind, call, data):
        m = self.machine
        out = None
        try:
            obj = self.extra(kind)
            if call == "connect":           # (in test mode only the test ports, whatever the module checks)
                port = m.check_port(data.get("port"))
                out = obj.connect(port, bool(data.get("force")))
                if kind == "probe":
                    self.settings.set("probePort", port)    # (offered first next time)
            elif call == "disconnect":
                out = obj.disconnect()
            elif call == "home":            # (Home Probe: H, then "homed"; up to 9 s)
                if m.activity == "scan":
                    raise MachineError("A scan is running", 409)
                obj.home()
                out = {"probe": obj.state()}
            elif call == "start":
                out = obj.start(data.get("settings"))
            elif call == "stop":
                out = {"stopped": bool(obj.stop())}
            elif call == "settings":
                out = obj.save_settings(data)
            else:
                out = obj.save(data.get("path"))
        except (MachineError, SettingsError) as e:
            return self.send_json({"error": str(e)}, getattr(e, "status", 400))
        except Exception as e:              # a bug there: say so, keep serving
            traceback.print_exc()
            return self.send_json({"error": f"Internal error in FluidSender ({e!r})"}, 500)
        self.send_json({"ok": True, **(out if isinstance(out, dict) else {})})

    def machine_events(self):
        """The event stream: one JSON object per line, a snapshot first, a ping every 10 s."""
        sub = self.machine.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            last = time.monotonic()
            while True:
                data = sub.get(0.5)
                if not data:
                    if sub.closed or self.client_gone():
                        break
                    if time.monotonic() - last < PING_EVERY:
                        continue
                    data = PING
                self.wfile.write(data)
                self.wfile.flush()
                last = time.monotonic()
        except OSError:
            pass                    # the page went away
        finally:
            self.machine.unsubscribe(sub)

    def client_gone(self):
        """True once the page has closed the stream (it sends nothing else on it)."""
        try:
            if select.select([self.connection], [], [], 0)[0]:
                return not self.connection.recv(4096)
        except OSError:
            return True
        return False

    def list_dir(self, path):
        asked = path
        path = os.path.abspath(os.path.expanduser(path or self.start_dir))
        if path.startswith(GVFS) and not os.path.isdir(path):
            mount_share()       # not mounted yet (the server can start before the desktop mounts it)
        if not asked and not os.path.isdir(path):
            path = os.path.expanduser("~")      # the share is out of reach: start at home this time
        try:
            names = sorted(os.listdir(path), key=str.lower)
        except OSError as e:
            return self.send_json({"error": f"{path}: {e.strerror}"}, 404)
        dirs, files = [], []
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(path, n)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if os.path.isdir(full):
                dirs.append(n)
            elif n.lower().endswith(EXTENSIONS):
                files.append({"name": n, "size": st.st_size, "mtime": st.st_mtime})
        self.send_json({"dir": path, "parent": os.path.dirname(path), "dirs": dirs, "files": files})

    def read_file(self, path):
        path = os.path.abspath(path)
        if not path.lower().endswith(EXTENSIONS):
            return self.send_json({"error": "not a G-code or scan file"}, 400)
        try:
            if os.path.getsize(path) > MAX_FILE:
                return self.send_json({"error": "file is too big"}, 400)
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            return self.send_json({"error": f"{path}: {e.strerror}"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def save_file(self, path, text):
        """Saves G-code only. Writes a temporary file first so a failed save can't cut the file short."""
        path = os.path.abspath(os.path.expanduser(path))
        if not path.lower().endswith(GCODE_EXT):
            return self.send_json({"error": "only G-code files (.nc .gcode .ngc .tap) can be saved"}, 400)
        if not os.path.isdir(os.path.dirname(path)):
            return self.send_json({"error": f"folder not found: {os.path.dirname(path)}"}, 404)
        tmp = path + ".saving"
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return self.send_json({"error": f"{path}: {e.strerror}"}, 500)
        self.send_json({"ok": True, "path": path, "size": os.path.getsize(path)})


_mount_lock = threading.Lock()
_mount_tried = 0.0


def mount_share():
    """Mounts the Windows share with gio (its saved login, never a prompt), at most every MOUNT_RETRY s."""
    global _mount_tried
    with _mount_lock:
        if time.monotonic() - _mount_tried < MOUNT_RETRY:
            return
        _mount_tried = time.monotonic()
        try:
            subprocess.run(["gio", "mount", SHARE_URL], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass


def main():
    ap = argparse.ArgumentParser(description="FluidSender local server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dir", default=DEFAULT_DIR, help="folder the file list starts in")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    args = ap.parse_args()

    Handler.start_dir = args.dir        # (checked on each list: the share may be mounted later)
    Handler.allowed_hosts = (f"localhost:{args.port}", f"127.0.0.1:{args.port}")
    Handler.settings = Settings()
    Handler.machine = Machine(
        extra_ports=os.environ.get("FLUIDSENDER_EXTRA_PORTS", "").split(":"),
        only_extra=os.environ.get("FLUIDSENDER_ONLY_EXTRA_PORTS") == "1",
        open_only_extra=os.environ.get("FLUIDSENDER_OPEN_ONLY_EXTRA_PORTS") == "1",
        release_after=float(os.environ.get("FLUIDSENDER_RELEASE_AFTER") or RELEASE_AFTER),
        settings=Handler.settings,
    )
    Handler.probe_mod, Handler.probe, Handler.scanner, Handler.pendant = load_extras(Handler.machine,
                                                                                     Handler.settings)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"FluidSender: {url}   (Ctrl+C to quit)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    def release_watch():                    # closing the window lets go of the pendant and the probe too
        while True:
            time.sleep(1.0)
            try:
                Handler.machine.release_extras(time.monotonic())
            except Exception:
                traceback.print_exc()
    threading.Thread(target=release_watch, name="release-watch", daemon=True).start()

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)     # kill: release the serial ports properly too
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.machine.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
