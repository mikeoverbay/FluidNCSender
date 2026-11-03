"""
FluidSender's ports: the one interface machine.py, probe.py and pendant.py use to list, check, open and talk over
serial ports, whatever the computer. Python standard library only.

The computer's own part is chosen once, here, at import: ports/windows.py on Windows (Win32 COM ports through
ctypes, the registry's port list), else ports/posix.py (Linux: termios, /dev, /sys and /proc). ports/demo.py is the
Demo machine (tools/fake_fluidnc.py's simulator in a thread of this process, over a socket pair), the same on both.
Each implementation module has the same functions; the ones here add the Demo machine (DEMO_PORT) to them.

    list_ports()            the serial ports there now (paths: "/dev/ttyUSB0", "COM3"...), in the order to offer them
    describe_port(path)     {"path", "label", "kind", "vid", "pid", "product", "inUseBy": None} for the Connect dialogs
    port_kind(path)         "usb-serial" (FluidNC's kind), "usb-acm" (the probe's), "bluetooth" (the pendant's), "pty",
                            "demo" or "other"
    real_port(path)         the port's own name (Linux: a /dev/serial/by-id link's target)
    port_exists(path)       whether it is there (Windows: always True; opening it says so)
    usb_info(name)          (vid, pid, product) of a Linux port's USB device; Nones where that can't be read
    scan_holders(paths)     {path: [pid, ...]}: the processes that have these ports open ({} where that can't be seen)
    process_name(pid)       'UGS (java, pid 4242)', 'python3 (pid 77)'...
    open_serial(path, baud) the open port (exclusive, raw 8N1); PortError with the user's message if it can't be
    start_demo()            a new Demo machine's port

An open port (each module's own class: FdPort, WinPort, demo.SocketPort) has the same calls everywhere:

    port.wait(timeout)  waits until there may be bytes to read (True), or wake() is called or timeout s pass
                        (False); timeout None: no limit
    port.read(n)        the bytes there now: b"" when the other end is gone, BlockingIOError when there are none yet
    port.write(data)    the number of bytes taken now (0 or BlockingIOError: none, try again)
    port.wake()         wakes a wait() in another thread
    port.unread()       bytes received but not read yet; port.unsent(): written but not gone out yet (0: can't say)
    port.close()
"""

import os

from . import demo
from .common import PortError
from .demo import DEMO_LABEL, DEMO_PORT, start_demo

if os.name == "nt":
    from . import windows as system
else:
    from . import posix as system

WINDOWS = system.NAME == "windows"
BAUDS = system.BAUDS            # the baud rates this computer's serial ports take

__all__ = ["BAUDS", "DEMO_LABEL", "DEMO_PORT", "PortError", "WINDOWS", "demo", "describe_port", "fd_port",
           "list_ports", "open_serial", "port_exists", "port_kind", "process_name", "real_port", "scan_holders",
           "start_demo", "system", "usb_info"]


def list_ports():
    return system.list_ports()


def describe_port(path, sys_tty=None):
    """(sys_tty: the tests' pretend /sys/class/tty, Linux only)"""
    if path == DEMO_PORT:
        return demo.describe()
    return system.describe_port(path) if sys_tty is None else system.describe_port(path, sys_tty)


def port_kind(path):
    return "demo" if path == DEMO_PORT else system.port_kind(path)


def real_port(path):
    return path if path == DEMO_PORT else system.real_port(path)


def port_exists(path):
    return path == DEMO_PORT or system.port_exists(path)


def usb_info(name, sys_tty=None):
    return system.usb_info(name) if sys_tty is None else system.usb_info(name, sys_tty)


def scan_holders(paths):
    return system.scan_holders(paths)


def process_name(pid):
    return system.process_name(pid)


def fd_port(fd):
    """A port object for a file descriptor already open (Linux: the tests' pseudo terminals)."""
    return system.FdPort(fd)


def open_serial(path, baud):
    """Opens a serial port at baud (never the Demo machine: start_demo). Raises PortError."""
    if baud not in system.BAUDS:
        raise PortError(f"Unsupported baud rate {baud}")
    return system.open_serial(path, baud)
