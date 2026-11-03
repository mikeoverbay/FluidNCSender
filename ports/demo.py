"""
The Demo machine: tools/fake_fluidnc.py's simulator in a thread of this process, on one end of a socket pair, the
other end being the "port" FluidSender talks to exactly as to FluidNC's serial port. Sockets (unlike a pseudo
terminal) are there on Windows too, and select() takes them everywhere. Connecting starts one (start_demo); closing
its port stops it. See ports/__init__.py for the interface.
"""

import importlib.util
import os
import select
import socket
import sys
import threading

from .common import PortError

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # (FluidSender's folder)
DEMO_PORT = "Demo machine"     # the Demo machine's "port", as the Connect dialog lists it and the pages show it
DEMO_LABEL = "Demo machine (simulated FluidNC)"
# the simulator's options: boots in ALARM, unhomed, like a machine with homing (Home it, or Unlock); soft limits on
# (the travel X 31.5, Y 23.6, Z 3.9 in); a PWM spindle whose speed shows a second after M3
MM = 25.4
# The Demo machine: a 50 x 38 x 4.5 in machine like the user's. X homes to the left end (X 0 to +50), Y to the far
# end (Y 0 to -38), Z to the top (Z 0 to -4.5); machine 0 is home on every axis. Its work zero sits front left,
# 10 in from the left, 5 in from the front, 3 in down, with the stock's top there (for the touch probe).
DEMO_OPTIONS = {"alarm": True, "soft_limits": True, "spinup": 1.0, "spindle": "pwm",
                "travel": [(0.0, 50 * MM), (-38 * MM, 0.0), (-4.5 * MM, 0.0)], "home_plus": [False, True, True],
                "wco": [10 * MM, -33 * MM, -3 * MM], "surface": (-3 * MM, 0.0, 0.0)}


def describe():
    return {"path": DEMO_PORT, "label": DEMO_LABEL, "kind": "demo", "vid": None, "pid": None, "product": None,
            "inUseBy": None}


class SocketPort:
    """One end of a connected socket pair (the other end: the simulator), and a second pair that wakes wait()
    (select() takes sockets on Windows too, never pipes). on_close: called once it is closed."""

    def __init__(self, sock, on_close=None):
        self.sock = sock
        sock.setblocking(False)
        self.wake_r, self.wake_w = socket.socketpair()
        self.wake_r.setblocking(False)
        self.wake_w.setblocking(False)
        self.on_close = on_close

    def wait(self, timeout=None):
        ready = select.select([self.sock, self.wake_r], [], [], timeout)[0]
        if self.wake_r in ready:
            try:
                self.wake_r.recv(512)
            except OSError:
                pass
        return self.sock in ready

    def read(self, n=4096):
        try:
            return self.sock.recv(n)
        except ConnectionError:         # (the simulator stopped: like a port that went away)
            return b""

    def write(self, data):
        return self.sock.send(data)

    def wake(self):
        try:
            self.wake_w.send(b"x")
        except OSError:
            pass

    def unread(self):
        return 0

    def unsent(self):
        return 0

    def close(self):
        for s in (self.sock, self.wake_r, self.wake_w):
            try:
                s.close()
            except OSError:
                pass
        on_close, self.on_close = self.on_close, None
        if on_close:
            on_close()


def _simulator():
    """tools/fake_fluidnc.py as a module (tools/ is no package)."""
    name = "fake_fluidnc"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "tools", "fake_fluidnc.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            del sys.modules[name]
            raise
    return sys.modules[name]


def start_demo(options=None):
    """Starts a Demo machine (DEMO_OPTIONS) in a thread of its own. Returns its port (a SocketPort). Closing the
    port stops the simulator (it sees its end closed), and waits for its thread a moment."""
    try:
        sim = _simulator()
    except Exception as e:
        raise PortError(f"The Demo machine could not start: tools/fake_fluidnc.py did not load ({e!r})")
    ours, theirs = socket.socketpair()
    stop = threading.Event()
    thread = threading.Thread(target=sim.serve_socket, args=(theirs, stop), kwargs=dict(options or DEMO_OPTIONS),
                              name="demo machine", daemon=True)

    def stopped():
        stop.set()
        if thread is not threading.current_thread():
            thread.join(2.0)
    port = SocketPort(ours, stopped)
    thread.start()
    return port
