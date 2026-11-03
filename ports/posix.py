"""
The ports on Linux: /dev/ttyUSB* (FluidNC's kind), /dev/ttyACM* (the probe's), /dev/rfcomm* (the pendant's). Only
/dev, /sys and /proc are read to list them and to see who has one open; a port is opened (open_serial) only when
the user presses a Connect button, and then exclusively (TIOCEXCL), raw 8N1 with termios. See ports/__init__.py for
the interface.
"""

import errno
import fcntl
import os
import re
import select
import struct
import termios

from .common import SPEEDS, PortError

NAME = "posix"
BAUDS = tuple(b for b in SPEEDS if hasattr(termios, f"B{b}"))
KNOWN_USB = {("1a86", "7523"): "CH340 USB serial (FluidNC?)", ("2341", "8036"): "Arduino Leonardo (Z probe)"}
SYS_TTY = "/sys/class/tty"


# ------------------------------------------------------------------ listing (only /dev, /sys and /proc are read)

def list_ports():
    paths = []
    for pattern in ("ttyUSB", "ttyACM", "rfcomm"):
        names = [n for n in os.listdir("/dev") if n.startswith(pattern) and n[len(pattern):].isdigit()]
        paths += ["/dev/" + n for n in sorted(names, key=lambda n: int(n[len(pattern):]))]
    return paths


def real_port(path):
    return os.path.realpath(path)


def port_exists(path):
    return os.path.exists(path)


def port_kind(path):
    name = os.path.basename(path)
    if name.startswith("ttyUSB"):
        return "usb-serial"
    if name.startswith("ttyACM"):
        return "usb-acm"
    if name.startswith("rfcomm"):
        return "bluetooth"
    if path.startswith("/dev/pts/"):
        return "pty"
    return "other"


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace").strip()
    except OSError:
        return ""


def usb_info(name, sys_tty=SYS_TTY):
    """(vid, pid, product) of the USB device behind /dev/<name>, from /sys; Nones if it isn't USB."""
    d = os.path.realpath(os.path.join(sys_tty, name, "device"))
    for _ in range(4):            # tty -> interface -> USB device
        if os.path.isfile(os.path.join(d, "idVendor")):
            return (_read(os.path.join(d, "idVendor")) or None, _read(os.path.join(d, "idProduct")) or None,
                    _read(os.path.join(d, "product")) or None)
        d = os.path.dirname(d)
    return None, None, None


def describe_port(path, sys_tty=SYS_TTY):
    real = os.path.realpath(path)
    kind = port_kind(real)
    vid = pid = product = None
    if kind in ("usb-serial", "usb-acm"):   # (an rfcomm port's /sys parent is the Bluetooth adapter: not useful)
        vid, pid, product = usb_info(os.path.basename(real), sys_tty)
    label = KNOWN_USB.get((vid, pid))
    if not label:
        label = {"bluetooth": "Bluetooth (pendant)", "pty": "Pseudo terminal (simulator?)"}.get(kind)
    if not label:
        base = product or {"usb-serial": "USB serial", "usb-acm": "USB serial (ACM)"}.get(kind, "Serial port")
        label = f"{base} ({vid}:{pid})" if vid else base
    return {"path": path, "label": label, "kind": kind, "vid": vid, "pid": pid, "product": product, "inUseBy": None}


def scan_holders(paths):
    """{device path: [pid, ...]} of the processes that have these devices open, from /proc/*/fd.
    Other users' processes can't be read; those are caught when the open fails (EBUSY)."""
    want = set(paths)
    found = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            fds = os.listdir(f"/proc/{d}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{d}/fd/{fd}")
            except OSError:
                continue
            if target in want and int(d) not in found.setdefault(target, []):
                found[target].append(int(d))
    return found


def process_name(pid):
    """'UGS (java, pid 4242)', 'python3 (pid 77)'..."""
    comm = _read(f"/proc/{pid}/comm") or "?"
    args = [a for a in _read(f"/proc/{pid}/cmdline").split("\0") if a]
    if re.search(r"ugsplatform|universal-g-code-sender", " ".join(args), re.I):
        return f"UGS ({comm}, pid {pid})"
    if len(comm) == 15:        # the kernel cuts names to 15 characters: look for the whole one
        comm = next((os.path.basename(a) for a in args[:2] if os.path.basename(a).startswith(comm)), comm)
    return f"{comm} (pid {pid})"


# ------------------------------------------------------------------ an open port: a file descriptor

class FdPort:
    """A serial port (or a pseudo terminal) as a file descriptor, with the pipe that wakes wait()."""

    def __init__(self, fd):
        self.fd = fd
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_r, False)
        os.set_blocking(self.wake_w, False)

    def wait(self, timeout=None):
        ready = select.select([self.fd, self.wake_r], [], [], timeout)[0]
        if self.wake_r in ready:
            try:
                os.read(self.wake_r, 512)
            except BlockingIOError:
                pass
        return self.fd in ready

    def read(self, n=4096):
        return os.read(self.fd, n)

    def write(self, data):
        return os.write(self.fd, data)

    def wake(self):
        try:
            os.write(self.wake_w, b"x")
        except OSError:             # (full: it is woken anyway; closed: nobody waits)
            pass

    def unread(self):
        return self._ioctl_int(termios.FIONREAD)

    def unsent(self):
        return self._ioctl_int(termios.TIOCOUTQ)

    def _ioctl_int(self, request):
        try:
            return struct.unpack("i", fcntl.ioctl(self.fd, request, b"\0" * 4))[0]
        except (OSError, TypeError):
            return 0

    def close(self):
        fd, self.fd = self.fd, None
        if fd is not None and fd >= 0:      # (-1: no port, the tests' Link without one)
            _close_fd(fd)
        wake, self.wake_r, self.wake_w = (self.wake_r, self.wake_w), None, None
        for w in wake:
            if w is not None:
                try:
                    os.close(w)
                except OSError:
                    pass


def open_serial(path, baud):
    """Opens a serial port exclusively, raw 8N1 at baud. Returns an FdPort (non-blocking)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.EBUSY:
            raise PortError(f"Port {path} is busy: another program has it open exclusively. "
                            "Disconnect it there first.", 409)
        if e.errno == errno.EACCES:
            raise PortError(f"No permission to open {path}. Add yourself to the dialout group "
                            "(sudo usermod -aG dialout $USER) and log in again.")
        raise PortError(f"Could not open {path}: {e.strerror}")
    speed = getattr(termios, f"B{baud}")
    try:
        fcntl.ioctl(fd, termios.TIOCEXCL)   # nobody else can open it now
        a = termios.tcgetattr(fd)
        a[0] = 0                                                   # iflag: no flow control or CR/LF changes
        a[1] = 0                                                   # oflag: send bytes as they are
        a[2] = (a[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
                | termios.CS8 | termios.CREAD | termios.CLOCAL)   # 8N1, ignore modem lines (HUPCL left as it was)
        a[3] = 0                                                   # lflag: raw, no echo
        a[4] = a[5] = speed
        a[6][termios.VMIN] = 1
        a[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, a)
        termios.tcflush(fd, termios.TCIFLUSH)                      # drop anything sent before we opened it
    except (OSError, termios.error) as e:
        _close_fd(fd)
        raise PortError(f"{path} is not a usable serial port ({e.args[-1] if e.args else e})")
    try:
        return FdPort(fd)
    except OSError as e:
        _close_fd(fd)
        raise PortError(f"Could not open {path}: {e.strerror or e}")


def _close_fd(fd):
    try:
        fcntl.ioctl(fd, termios.TIOCNXCL)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
