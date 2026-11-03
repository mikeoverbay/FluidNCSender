r"""
The ports on Windows: COM ports, listed from the registry (HKLM\HARDWARE\DEVICEMAP\SERIALCOMM) and opened with
Win32's CreateFileW through ctypes (only when the user presses a Connect button). A COM port is exclusive anyway: one
another program has open answers "access denied", so who has it open can't be listed (scan_holders: {}) and is told
when opening it. 8N1 with DTR and RTS on, as a Linux open leaves them (an ESP32 board resets when only one of them
is on). wait() looks at the port's input queue every POLL s (Win32 has no select() for COM ports). winreg and
ctypes' kernel32 are only touched when called, so this module imports anywhere (the tests run its logic on Linux).
See ports/__init__.py for the interface.
"""

import errno
import re
import threading
import time

from .common import SPEEDS, PortError

NAME = "windows"
BAUDS = SPEEDS
POLL = 0.005            # s: wait() looks at the input queue this often
WRITE_MS = 100          # ms: a write gives up after this long (the callers try again)
COM = re.compile(r"^COM\d+$", re.I)


GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
OPEN_EXISTING = 3
MAXDWORD = 0xFFFFFFFF
PURGE_RXCLEAR = 0x0008
NOPARITY, ONESTOPBIT = 0, 0
DTR_CONTROL_ENABLE, RTS_CONTROL_ENABLE = 1, 1
ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, ERROR_ACCESS_DENIED = 2, 3, 5
_win = None             # (kernel32 and the structures, made the first time a COM port is opened)


def _win_api():
    """kernel32's functions and the DCB, COMMTIMEOUTS and COMSTAT structures (ctypes), made the first time a COM
    port is opened (Windows only). The structures are made with plain ctypes (_structures), so the tests can build
    a DCB on Linux too (dcb_for)."""
    global _win
    if _win is None:
        import ctypes
        from types import SimpleNamespace
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        for name in ("CloseHandle", "GetCommState", "SetCommState", "SetCommTimeouts", "SetupComm", "PurgeComm",
                     "ClearCommError", "ReadFile", "WriteFile"):
            getattr(k, name).restype = ctypes.c_int
        _win = SimpleNamespace(ctypes=ctypes, kernel32=k, **_structures(ctypes))
    return _win


def _structures(ctypes):
    DWORD, WORD, BYTE, CHAR = ctypes.c_uint32, ctypes.c_uint16, ctypes.c_uint8, ctypes.c_char

    class DCB(ctypes.Structure):        # (winbase.h)
        _fields_ = [("DCBlength", DWORD), ("BaudRate", DWORD),
                    ("fBinary", DWORD, 1), ("fParity", DWORD, 1), ("fOutxCtsFlow", DWORD, 1),
                    ("fOutxDsrFlow", DWORD, 1), ("fDtrControl", DWORD, 2), ("fDsrSensitivity", DWORD, 1),
                    ("fTXContinueOnXoff", DWORD, 1), ("fOutX", DWORD, 1), ("fInX", DWORD, 1),
                    ("fErrorChar", DWORD, 1), ("fNull", DWORD, 1), ("fRtsControl", DWORD, 2),
                    ("fAbortOnError", DWORD, 1), ("fDummy2", DWORD, 17),
                    ("wReserved", WORD), ("XonLim", WORD), ("XoffLim", WORD), ("ByteSize", BYTE), ("Parity", BYTE),
                    ("StopBits", BYTE), ("XonChar", CHAR), ("XoffChar", CHAR), ("ErrorChar", CHAR),
                    ("EofChar", CHAR), ("EvtChar", CHAR), ("wReserved1", WORD)]

    class COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [("ReadIntervalTimeout", DWORD), ("ReadTotalTimeoutMultiplier", DWORD),
                    ("ReadTotalTimeoutConstant", DWORD), ("WriteTotalTimeoutMultiplier", DWORD),
                    ("WriteTotalTimeoutConstant", DWORD)]

    class COMSTAT(ctypes.Structure):
        _fields_ = [("fFlags", DWORD), ("cbInQue", DWORD), ("cbOutQue", DWORD)]

    return {"DCB": DCB, "COMMTIMEOUTS": COMMTIMEOUTS, "COMSTAT": COMSTAT}


def dcb_for(dcb, baud):
    """Sets a DCB (as GetCommState read it) to baud 8N1, binary, no flow control (no XON/XOFF, CTS, DSR), no
    character replaced or dropped, DTR and RTS on. Returns it."""
    dcb.BaudRate = baud
    dcb.ByteSize = 8
    dcb.Parity = NOPARITY
    dcb.StopBits = ONESTOPBIT
    dcb.fBinary = 1
    dcb.fParity = 0
    dcb.fOutxCtsFlow = dcb.fOutxDsrFlow = dcb.fDsrSensitivity = 0
    dcb.fOutX = dcb.fInX = 0
    dcb.fTXContinueOnXoff = 1
    dcb.fErrorChar = dcb.fNull = 0
    dcb.fAbortOnError = 0           # (on, a line error would stop every read and write until ClearCommError)
    dcb.fDtrControl = DTR_CONTROL_ENABLE
    dcb.fRtsControl = RTS_CONTROL_ENABLE
    return dcb


def timeouts_for(t):
    """COMMTIMEOUTS: a read returns at once with what has come (MAXDWORD, 0, 0: Win32's "non-blocking" read);
    a write gives up after WRITE_MS. Returns it."""
    t.ReadIntervalTimeout = MAXDWORD
    t.ReadTotalTimeoutMultiplier = 0
    t.ReadTotalTimeoutConstant = 0
    t.WriteTotalTimeoutMultiplier = 0
    t.WriteTotalTimeoutConstant = WRITE_MS
    return t


def device_name(path):
    r"""COM3 -> \\.\COM3 (COM10 and up can only be opened that way; it works for COM1-COM9 too)."""
    return path if path.startswith("\\\\.\\") else "\\\\.\\" + path


def win_error(path, code):
    """A failed CreateFileW's error, for the user."""
    if code == ERROR_ACCESS_DENIED:
        return PortError(f"Port {path} is in use by another program (UGS? the Arduino IDE?). Disconnect it there "
                         "first.", 409)
    if code in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
        return PortError(f"{path} is not there (unplugged?)")
    return PortError(f"Could not open {path} (Windows error {code})")


class WinPort:
    """A COM port opened with CreateFileW (synchronous: reads return at once, see timeouts_for)."""

    def __init__(self, handle, path):
        self.handle = handle
        self.path = path
        self.woken = threading.Event()
        self.api = _win_api()

    def _call(self, name, *args):
        if not getattr(self.api.kernel32, name)(*args):
            code = getattr(self.api.ctypes, "get_last_error", lambda: 0)()    # (Windows only: tests run on Linux)
            raise OSError(errno.EIO, f"{name} failed (Windows error {code})")

    def _queues(self):
        """(bytes received, bytes waiting to go out). Raises OSError if the port is gone (unplugged)."""
        c = self.api.ctypes
        if self.handle is None:
            raise OSError(errno.EBADF, "the port was closed")
        errors, stat = c.c_uint32(), self.api.COMSTAT()
        self._call("ClearCommError", c.c_void_p(self.handle), c.byref(errors), c.byref(stat))
        return stat.cbInQue, stat.cbOutQue

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._queues()[0]:
                return True
            if self.woken.is_set():
                self.woken.clear()
                return False
            left = POLL if end is None else min(POLL, end - time.monotonic())
            if left <= 0:
                return False
            self.woken.wait(left)

    def read(self, n=4096):
        c = self.api.ctypes
        buf, got = c.create_string_buffer(n), c.c_uint32()
        self._call("ReadFile", c.c_void_p(self.handle), buf, n, c.byref(got), None)
        if not got.value:
            raise BlockingIOError(errno.EAGAIN, "nothing to read")
        return buf.raw[:got.value]

    def write(self, data):
        c = self.api.ctypes
        sent = c.c_uint32()
        self._call("WriteFile", c.c_void_p(self.handle), bytes(data), len(data), c.byref(sent), None)
        return sent.value

    def wake(self):
        self.woken.set()

    def unread(self):
        try:
            return self._queues()[0]
        except OSError:
            return 0

    def unsent(self):
        try:
            return self._queues()[1]
        except OSError:
            return 0

    def close(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            self.api.kernel32.CloseHandle(self.api.ctypes.c_void_p(handle))
        self.woken.set()


def open_serial(path, baud):
    """Opens a COM port (COM3...), baud 8N1. Returns a WinPort."""
    api = _win_api()
    c, k = api.ctypes, api.kernel32
    handle = k.CreateFileW(device_name(path), GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if handle is None or handle == c.c_void_p(-1).value:      # (INVALID_HANDLE_VALUE)
        raise win_error(path, c.get_last_error())
    port = WinPort(handle, path)
    try:
        h = c.c_void_p(handle)
        k.SetupComm(h, 4096, 4096)
        dcb = api.DCB()
        dcb.DCBlength = c.sizeof(dcb)
        port._call("GetCommState", h, c.byref(dcb))
        port._call("SetCommState", h, c.byref(dcb_for(dcb, baud)))
        port._call("SetCommTimeouts", h, c.byref(timeouts_for(api.COMMTIMEOUTS())))
        k.PurgeComm(h, PURGE_RXCLEAR)   # drop anything sent before we opened it
    except OSError as e:
        port.close()
        raise PortError(f"{path} is not a usable serial port ({e.strerror or e})")
    return port


def win_ports():
    r"""{"COM3": "\Device\Silabser0", ...}: the serial ports Windows has now (HKLM\HARDWARE\DEVICEMAP\SERIALCOMM:
    each value's name is the driver's device, its data the port). {} where there is no registry."""
    try:
        import winreg
    except ImportError:
        return {}
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            i = 0
            while True:
                try:
                    device, port, _ = winreg.EnumValue(key, i)
                except OSError:         # (no more values)
                    break
                i += 1
                if isinstance(port, str) and COM.match(port.strip()):
                    out[port.strip().upper()] = device
    except OSError:                     # (no serial port at all: the key isn't there)
        pass
    return out


def com_number(path):
    """COM3 -> 3 (for sorting); a name that isn't COMn sorts last."""
    m = re.match(r"^COM(\d+)$", path, re.I)
    return int(m.group(1)) if m else 1 << 30


def win_kind(device):
    r"""The kind of port a SERIALCOMM device name says: \Device\BthModem0 (a Bluetooth serial port: the pendant)
    "bluetooth"; \Device\USBSER000 (Windows' own CDC ACM driver: an Arduino Leonardo, the probe) "usb-acm"; a
    plain \Device\Serial0 (a port on the computer itself) "other"; any other driver (CH340, CP210x, FTDI...:
    FluidNC's kind) "usb-serial"."""
    d = (device or "").lower()
    if "bthmodem" in d:
        return "bluetooth"
    if "usbser" in d:
        return "usb-acm"
    if re.match(r"^\\device\\serial\d+$", d):
        return "other"
    return "usb-serial"


def win_label(device):
    r"""A Connect dialog label from the device name: \Device\Silabser0 -> "USB serial (Silabser0)"."""
    kind = win_kind(device)
    name = (device or "").rsplit("\\", 1)[-1] or "?"
    base = {"bluetooth": "Bluetooth (pendant?)", "usb-acm": "USB serial (ACM: probe?)",
            "other": "Serial port"}.get(kind, "USB serial")
    return f"{base} ({name})"


# ------------------------------------------------------------------ the interface's listing calls

def list_ports():
    return sorted(win_ports(), key=com_number)


def real_port(path):
    return path


def port_exists(path):
    return True                     # (opening it says whether it is there)


def port_kind(path):
    return win_kind(win_ports().get(path.strip().upper())) if COM.match(path.strip()) else "other"


def usb_info(name, sys_tty=None):
    return None, None, None         # (the registry's port list doesn't say: the probe needs force here)


def describe_port(path, sys_tty=None):
    device = win_ports().get(path.strip().upper())
    return {"path": path, "label": win_label(device), "kind": win_kind(device), "vid": None, "pid": None,
            "product": None, "inUseBy": None}


def scan_holders(paths):
    return {}                       # (Windows can't tell: opening a port another program has says so)


def process_name(pid):
    return f"pid {pid}"
