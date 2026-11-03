"""
The Demo machine (a simulated FluidNC in the server: ports/demo.py) and the Windows ports (ports/windows.py) with
machine.py, as far as they can be tried on Linux: the DCB and timeouts a COM port is set to (on ctypes
structures made here), the COM port list from a pretend winreg, the port kinds and labels, WinPort's calls against a
pretend kernel32, and every module importing and running the Demo machine with Windows' os.name and without the
Linux-only modules (termios, fcntl, tty). No real serial port is opened.

    python3 -m unittest tests/test_windows_and_demo.py
"""

import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import machine  # noqa: E402
import ports  # noqa: E402
from ports import windows  # noqa: E402
from test_e2e import Server  # noqa: E402

DEMO = "Demo machine"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(what, check, timeout=10.0):
    end = time.monotonic() + timeout
    while True:
        got = check()
        if got:
            return got
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


class DemoInProcess(unittest.TestCase):
    """The Demo machine on a Machine of this process: connect, home, spindle, disconnect."""

    def setUp(self):
        self.m = machine.Machine(only_extra=True)       # (lists and opens no real port)

    def tearDown(self):
        self.m.shutdown()

    def status(self):
        return self.m.snapshot()["status"] or {}

    def home(self):
        try:
            self.m.home("")
            return True
        except machine.MachineError:
            return False

    def test_listed_last_never_default(self):
        data = self.m.ports(demo=True)
        self.assertEqual([p["path"] for p in data["ports"]], [DEMO])
        self.assertEqual((data["ports"][0]["kind"], data["ports"][0]["label"]), ("demo", ports.DEMO_LABEL))
        self.assertIsNone(data["default"])
        self.assertEqual(self.m.ports()["ports"], [])       # (the probe's and the pendant's lists: never)

    def test_connect_home_spindle_disconnect(self):
        threads = {t.name for t in __import__("threading").enumerate()}
        self.m.connect(DEMO)
        snap = self.m.snapshot()
        self.assertTrue(snap["connected"])
        self.assertEqual(snap["port"], DEMO)
        self.assertIn("FluidNC v4.1.1", snap["firmware"])
        wait_for("ALARM:14", lambda: (self.m.snapshot()["alarm"] or {}).get("code") == 14)   # boots unhomed
        self.assertEqual(self.m.ports(demo=True)["ports"][-1]["inUseBy"], "FluidSender (connected)")
        wait_for("Home taken (once the reads after connecting are done)", self.home)
        wait_for("homed and Idle", lambda: self.status().get("state") == "Idle" and self.status().get("mpos") == [0, 0, 0])
        self.m.send("M3 S12000")
        wait_for("the spindle at 12000", lambda: self.status().get("spindle") == 12000)
        self.m.send("M5")
        self.m.send("G91 G0 X0.1")             # (FluidNC's FS says 0 once a move ran with M5)
        self.m.send("G90")
        wait_for("the spindle at 0", lambda: self.status().get("spindle") == 0)
        self.m.disconnect()
        self.assertFalse(self.m.snapshot()["connected"])
        wait_for("the simulator thread to end",
                 lambda: "demo machine" not in {t.name for t in __import__("threading").enumerate()} - threads)

    def test_other_ports_still_refused_in_test_mode(self):
        with self.assertRaises(machine.MachineError):
            self.m.connect("/dev/ttyUSB99")


class DemoThroughServer(unittest.TestCase):
    """The page's calls: the Connect dialog's list, Connect, Home, M3 / M5, Disconnect, on a server of its own."""

    def test_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = Server(free_port(), [], tmp)
            try:
                st, data = server.call("/api/machine/ports")
                self.assertEqual(st, 200)
                self.assertEqual([p["path"] for p in data["ports"]], [DEMO])
                self.assertIsNone(data["default"])
                st, data = server.call("/api/pendant/ports")
                self.assertNotIn(DEMO, [p["path"] for p in data["ports"]])
                st, data = server.call("/api/machine/connect", {"port": DEMO, "baud": 115200})
                self.assertEqual(st, 200, data)

                def state():
                    return server.call("/api/machine/state")[1]
                s = state()
                self.assertEqual((s["connected"], s["port"]), (True, DEMO))
                self.assertTrue(any("Demo machine" in e["text"] for e in s["console"]))
                wait_for("ALARM:14", lambda: (state()["alarm"] or {}).get("code") == 14)
                wait_for("Home taken (once the reads after connecting are done)",
                         lambda: server.call("/api/machine/home", {"axes": ""})[0] == 200)
                wait_for("Idle after homing", lambda: (state()["status"] or {}).get("state") == "Idle")
                self.assertEqual(server.call("/api/machine/send", {"line": "M3 S8000"})[0], 200)
                wait_for("the spindle on", lambda: state()["status"]["spindle"] == 8000)
                self.assertEqual(server.call("/api/machine/send", {"line": "M5"})[0], 200)
                for line in ("G91 G0 X0.1", "G90"):    # (FluidNC's FS says 0 once a move ran with M5)
                    self.assertEqual(server.call("/api/machine/send", {"line": line})[0], 200)
                wait_for("the spindle off", lambda: state()["status"]["spindle"] == 0)
                wait_for("a jog taken", lambda: server.call("/api/machine/jog", {"step": {"x": 0, "y": -0.5, "z": 0},
                                                                                  "feed": 100})[0] == 200)
                wait_for("the jog done", lambda: state()["status"]["mpos"][1] == -0.5
                         and state()["status"]["state"] == "Idle")
                self.assertEqual(server.call("/api/machine/disconnect", {})[0], 200)
                s = state()
                self.assertFalse(s["connected"])
                self.assertEqual(s["console"][-1]["text"], "Disconnected from Demo machine")
            finally:
                server.stop()


class DcbAndTimeouts(unittest.TestCase):
    """What a COM port is set to (the structures as winbase.h lays them out, made with plain ctypes)."""

    def setUp(self):
        self.s = windows._structures(ctypes)

    def test_sizes(self):
        self.assertEqual(ctypes.sizeof(self.s["DCB"]), 28)
        self.assertEqual(ctypes.sizeof(self.s["COMMTIMEOUTS"]), 20)
        self.assertEqual(ctypes.sizeof(self.s["COMSTAT"]), 12)

    def test_dcb_115200_8n1(self):
        dcb = self.s["DCB"]()
        dcb.fOutX = dcb.fInX = dcb.fOutxCtsFlow = dcb.fParity = dcb.fAbortOnError = 1   # (as a port may be left)
        dcb.fRtsControl = 2                                                           # (RTS handshake)
        dcb.Parity, dcb.StopBits, dcb.ByteSize = 2, 2, 7
        windows.dcb_for(dcb, 115200)
        self.assertEqual((dcb.BaudRate, dcb.ByteSize, dcb.Parity, dcb.StopBits), (115200, 8, 0, 0))
        raw = bytes(dcb)
        self.assertEqual(int.from_bytes(raw[4:8], "little"), 115200)
        # the flag word: fBinary (bit 0), fDtrControl ENABLE (bits 4-5: 1), fTXContinueOnXoff (bit 7),
        # fRtsControl ENABLE (bits 12-13: 1); no parity, flow control, error char, null stripping, abort on error
        self.assertEqual(int.from_bytes(raw[8:12], "little"), 1 | 1 << 4 | 1 << 7 | 1 << 12)
        self.assertEqual(raw[18:21], bytes([8, 0, 0]))     # ByteSize, Parity, StopBits

    def test_timeouts(self):
        t = windows.timeouts_for(self.s["COMMTIMEOUTS"]())
        self.assertEqual(bytes(t), (0xFFFFFFFF).to_bytes(4, "little") + bytes(12)
                         + windows.WRITE_MS.to_bytes(4, "little"))


class WindowsNames(unittest.TestCase):
    REG = {"\\Device\\Silabser0": "COM4", "\\Device\\USBSER000": "COM10", "\\Device\\BthModem0": "COM7",
           "\\Device\\Serial0": "COM1", "\\Device\\VCP0": "COM12 ", "\\Device\\Weird": "LPT1"}

    def fake_winreg(self, values):
        items = list(values.items())

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def enum(key, i):
            if i >= len(items):
                raise OSError("no more")
            return items[i][0], items[i][1], 1
        return types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, OpenKey=lambda *a: Key(), EnumValue=enum)

    def test_win_ports_from_the_registry(self):
        with mock.patch.dict(sys.modules, {"winreg": self.fake_winreg(self.REG)}):
            ports = windows.win_ports()
        self.assertEqual(ports, {"COM4": "\\Device\\Silabser0", "COM10": "\\Device\\USBSER000",
                                 "COM7": "\\Device\\BthModem0", "COM1": "\\Device\\Serial0",
                                 "COM12": "\\Device\\VCP0"})
        self.assertEqual(windows.win_ports(), {})        # (Linux: no winreg)

    def test_no_serialcomm_key(self):
        def missing(*a):
            raise FileNotFoundError("no key")
        with mock.patch.dict(sys.modules, {"winreg": types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, OpenKey=missing)}):
            self.assertEqual(windows.win_ports(), {})

    def test_kinds_and_labels(self):
        self.assertEqual([windows.win_kind(d) for d in ("\\Device\\BthModem3", "\\Device\\USBSER001",
                                                           "\\Device\\Serial0", "\\Device\\Silabser0", None)],
                         ["bluetooth", "usb-acm", "other", "usb-serial", "usb-serial"])
        self.assertEqual(windows.win_label("\\Device\\Silabser0"), "USB serial (Silabser0)")
        self.assertEqual(windows.win_label("\\Device\\USBSER000"), "USB serial (ACM: probe?) (USBSER000)")
        self.assertEqual(windows.device_name("COM12"), "\\\\.\\COM12")
        self.assertEqual(windows.device_name("\\\\.\\COM3"), "\\\\.\\COM3")
        self.assertEqual(sorted(["COM10", "COM2", "COM1"], key=windows.com_number), ["COM1", "COM2", "COM10"])

    def test_open_errors(self):
        e = windows.win_error("COM3", 5)
        self.assertEqual(e.status, 409)
        self.assertIn("in use by another program", str(e))
        self.assertIn("not there", str(windows.win_error("COM3", 2)))
        self.assertIn("Windows error 87", str(windows.win_error("COM3", 87)))

    def test_machine_lists_com_ports_on_windows(self):
        with mock.patch.object(ports, "system", windows), \
                mock.patch.dict(sys.modules, {"winreg": self.fake_winreg(self.REG)}):
            data = machine.Machine().ports(demo=True)
            self.assertEqual([(p["path"], p["kind"]) for p in data["ports"]],
                             [("COM1", "other"), ("COM4", "usb-serial"), ("COM7", "bluetooth"), ("COM10", "usb-acm"),
                              ("COM12", "usb-serial"), (DEMO, "demo")])
            self.assertEqual(data["default"], "COM4")
            self.assertEqual(machine.port_kind("com10"), "usb-acm")
            self.assertEqual(machine.real_port("COM4"), "COM4")
            m = machine.Machine()
            with mock.patch.object(machine, "open_port", side_effect=machine.MachineError("opened", 418)) as op:
                with self.assertRaises(machine.MachineError) as cm:
                    m.connect("COM4")
                self.assertEqual(str(cm.exception), "opened")   # (past the checks: no /dev, no /proc)
                op.assert_called_once_with("COM4", 115200)
                with self.assertRaises(machine.MachineError) as cm:
                    m.connect("COM10")
                self.assertIn("looks like the Z probe", str(cm.exception))
                with self.assertRaises(machine.MachineError) as cm:
                    m.connect("COM1")
                self.assertIn("not a USB or Bluetooth serial port", str(cm.exception))


class FakeKernel32:
    """kernel32's COM port calls, as WinPort uses them: an input queue, what was written, and a failure to come."""

    def __init__(self):
        self.rx = b""
        self.written = b""
        self.fail = False
        self.closed = []

    def ClearCommError(self, handle, errors, stat):
        if self.fail:
            return 0
        stat._obj.cbInQue = len(self.rx)
        stat._obj.cbOutQue = 3
        return 1

    def ReadFile(self, handle, buf, n, got, overlapped):
        data, self.rx = self.rx[:n], self.rx[n:]
        ctypes.memmove(buf, data, len(data))
        got._obj.value = len(data)
        return 1

    def WriteFile(self, handle, data, n, sent, overlapped):
        take = min(n, 4)                        # (a write that takes only part of it)
        self.written += data[:take]
        sent._obj.value = take
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle.value)
        return 1


class WinPortCalls(unittest.TestCase):
    def setUp(self):
        self.k = FakeKernel32()
        api = types.SimpleNamespace(ctypes=ctypes, kernel32=self.k, **windows._structures(ctypes))
        with mock.patch.object(windows, "_win_api", return_value=api):
            self.port = windows.WinPort(1234, "COM4")

    def test_read_write_wait(self):
        p = self.port
        self.assertFalse(p.wait(0.02))
        with self.assertRaises(BlockingIOError):
            p.read()
        self.k.rx = b"ok\r\n"
        self.assertTrue(p.wait(1.0))
        self.assertEqual((p.unread(), p.unsent()), (4, 3))
        self.assertEqual(p.read(4096), b"ok\r\n")
        self.assertEqual(p.write(b"$I\n?"), 4)
        self.assertEqual(p.write(b"G20 G90\n"), 4)
        self.assertEqual(self.k.written, b"$I\n?G20 ")

    def test_wake(self):
        p = self.port
        t0 = time.monotonic()
        p.wake()
        self.assertFalse(p.wait(5.0))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_unplugged(self):
        self.k.fail = True
        with self.assertRaises(OSError):
            self.port.wait(1.0)
        self.assertEqual(self.port.unread(), 0)

    def test_close(self):
        self.port.close()
        self.port.close()
        self.assertEqual(self.k.closed, [1234])
        with self.assertRaises(OSError):
            self.port.wait(0.1)


class OpenSerialDispatch(unittest.TestCase):
    def test_windows_opens_com(self):
        with mock.patch.object(ports, "system", windows), \
                mock.patch.object(windows, "open_serial", return_value="port") as ow:
            self.assertEqual(ports.open_serial("COM3", 115200), "port")
            ow.assert_called_once_with("COM3", 115200)

    def test_bad_baud(self):
        with self.assertRaises(ports.PortError):
            ports.open_serial("/dev/ttyUSB99", 12345)


# run with Windows' os.name and no termios, fcntl or tty: every module imports, and the Demo machine works
LIKE_WINDOWS = r"""
import os, sys, time
import argparse, ctypes, importlib.util, json, select, signal, socket, subprocess, threading, traceback, weakref
import webbrowser, http.server, urllib.parse, decimal, struct, copy   # (the standard library first: it is Linux's)
for name in ("termios", "fcntl", "tty"):
    sys.modules[name] = None            # (import fails, as on Windows)
os.name = "nt"
os.getuid = None                        # (not on Windows)
sys.path.insert(0, sys.argv[1])
import machine, probe, pendant, scan, server, ports, hostos
assert ports.WINDOWS and ports.system.NAME == "windows" and hostos.GVFS is None
assert server.DEFAULT_DIR in (os.path.expanduser("~"), os.path.join(os.path.expanduser("~"), "Documents"))
assert not hostos.needs_mount(server.DEFAULT_DIR)
m = machine.Machine()
ports = m.ports(demo=True)["ports"]
assert [p["path"] for p in ports] == ["Demo machine"], ports
m.connect("Demo machine")
s = m.snapshot()
assert s["connected"] and "FluidNC" in s["firmware"], s
m.disconnect()
assert not m.snapshot()["connected"]
print("ok")
"""


class ImportsLikeWindows(unittest.TestCase):
    def test_modules_and_demo(self):
        out = subprocess.run([sys.executable, "-c", LIKE_WINDOWS, ROOT], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "ok", out.stderr)


class Launcher(unittest.TestCase):
    def test_bat_has_windows_line_ends(self):
        with open(os.path.join(ROOT, "FluidSender.bat"), "rb") as f:
            text = f.read()
        self.assertEqual(text.count(b"\n"), text.count(b"\r\n"))
        self.assertIn(b"--app=%URL%", text)
        self.assertIn(b"server.py --no-browser --port %PORT%", text)


if __name__ == "__main__":
    unittest.main()
