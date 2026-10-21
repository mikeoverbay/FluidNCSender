#!/usr/bin/env python3
"""
A pretend surface probe ("the nano": an Arduino Leonardo with its own Z) on a pseudo terminal, to try and
test FluidSender's probe link and surface scan without the real one.

    python3 tools/fake_probe.py [--values 0,0.012,-0.004] [--overtravel-at N] [--home-time S]
                                [--probe-time S] [--reply CMD#N=TEXT] [--log FILE]

The first line it prints is the port to connect to (e.g. /dev/pts/9). To use it from the page:

    FLUIDSENDER_EXTRA_PORTS=/dev/pts/7:/dev/pts/9 FLUIDSENDER_ONLY_EXTRA_PORTS=1 python3 server.py --port 8790

Like the nano (115200, raw, replies end in "\\r\\n"):

    H        -> "homed" after --home-time (0.3 s); the next touch reads 0.0
    ZR:<x>   -> "OK ZR=<x, 3 decimals>" at once
    P        -> after --probe-time (0.5 s): the surface height at this touch minus the height at the first
                touch after the last H (so that one reads 0.0), with 4 decimals. The heights are --values
                (one per touch in order, the last one repeated) or a gentle built-in surface.
                --overtravel-at N: the Nth P (counting from the start) answers "E" instead.
    A        -> stops a P in progress, which then never answers; A itself has no reply

Commands are handled one at a time, as the firmware does: one that comes while H or P is still moving
waits for it (except A). Anything else is logged and gets no reply.

    --reply CMD#N=TEXT   (repeatable) the Nth H, ZR or P (counting from the start) answers TEXT instead,
                         at its usual time; TEXT "none" means no reply at all (a probe that hangs)
    --log FILE           appends one JSON object per line: {"t", "cmd", "n"} for each command received,
                         {"t", "reply", "to"} for each reply, {"t", "abort": "P"} when an A stopped a P

Python standard library only. Ctrl+C quits.
"""

import argparse
import errno
import json
import math
import os
import re
import select
import sys
import time
import tty
from collections import deque

TICK = 0.01
REPLY = re.compile(r"^(H|ZR|P)#(\d+)=(.*)$", re.S)


def surface(k):
    """The built-in surface: the height (in) at the kth touch, a gentle wave of about +-0.03 in."""
    return 0.02 * math.sin(k * 0.45) + 0.01 * math.cos(k * 0.21)


class FakeProbe:
    def __init__(self, fd, values, overtravel_at, home_time, probe_time, replies, log):
        self.fd = fd
        self.values = values
        self.overtravel_at = overtravel_at
        self.home_time = home_time
        self.probe_time = probe_time
        self.replies = replies              # {("P", 3): "abc", ...}
        self.log = log
        self.rx = b""
        self.waiting = deque()              # commands that came while one was still moving
        self.doing = None                   # (cmd, when it answers, reply or None)
        self.counts = {"H": 0, "ZR": 0, "P": 0}
        self.ref = None                     # the height at the first touch after the last H

    def note(self, **kw):
        if self.log:
            self.log.write(json.dumps(dict(t=round(time.time(), 3), **kw)) + "\n")

    def send(self, text):
        try:
            os.write(self.fd, (text + "\r\n").encode())
        except OSError:
            pass                            # nobody has the port open

    def received(self, data, now):
        self.rx += data
        while b"\n" in self.rx:
            raw, self.rx = self.rx.split(b"\n", 1)
            line = raw.decode("ascii", "replace").strip()
            if not line:
                continue
            if line.upper() == "A":         # handled at once, even while moving
                self.note(cmd="A")
                if self.doing and self.doing[0] == "P":
                    self.note(abort="P")
                    self.doing = None
                continue
            self.waiting.append(line)
        self.tick(now)

    def tick(self, now):
        if self.doing and now >= self.doing[1]:
            cmd, _, reply = self.doing
            self.doing = None
            if reply is not None:
                self.note(reply=reply, to=cmd)
                self.send(reply)
        while self.waiting and not self.doing:
            self.start(self.waiting.popleft(), now)

    def start(self, line, now):
        up = line.upper()
        cmd = "H" if up == "H" else "P" if up == "P" else "ZR" if up.startswith("ZR:") else None
        if cmd is None:
            self.note(cmd=line, unknown=True)
            return
        self.counts[cmd] += 1
        n = self.counts[cmd]
        self.note(cmd=line, n=n)
        if cmd == "H":
            self.ref = None
            reply, delay = "homed", self.home_time
        elif cmd == "ZR":
            try:
                reply = f"OK ZR={float(line[3:]):.3f}"
            except ValueError:
                reply = "ERROR"
            delay = 0.0
        else:
            delay = self.probe_time
            if n == self.overtravel_at:
                reply = "E"
            else:
                h = self.values[min(n, len(self.values)) - 1] if self.values else surface(n)
                if self.ref is None:
                    self.ref = h
                reply = f"{h - self.ref:.4f}"
        if (cmd, n) in self.replies:
            reply = self.replies[cmd, n]
            reply = None if reply.lower() == "none" else reply
        self.doing = (cmd, now + delay, reply)
        self.tick(now)


def main():
    ap = argparse.ArgumentParser(description="A pretend surface probe on a pseudo terminal")
    ap.add_argument("--values", help="comma separated surface heights, one per touch (in)")
    ap.add_argument("--overtravel-at", type=int, metavar="N", help='the Nth P answers "E" (over travel)')
    ap.add_argument("--home-time", type=float, default=0.3, metavar="S", help="seconds H takes (0.3)")
    ap.add_argument("--probe-time", type=float, default=0.5, metavar="S", help="seconds P takes (0.5)")
    ap.add_argument("--reply", action="append", default=[], metavar="CMD#N=TEXT",
                    help='the Nth H, ZR or P answers TEXT ("none": no reply)')
    ap.add_argument("--log", metavar="FILE", help="append what is received and sent to FILE (JSON lines)")
    args = ap.parse_args()
    try:
        values = [float(v) for v in args.values.split(",")] if args.values else []
    except ValueError:
        ap.error("--values: comma separated numbers")
    replies = {}
    for r in args.reply:
        m = REPLY.match(r)
        if not m:
            ap.error(f"--reply {r!r}: use CMD#N=TEXT, CMD one of H, ZR, P")
        replies[m.group(1), int(m.group(2))] = m.group(3)

    master, slave = os.openpty()
    tty.setraw(slave)            # the port side: no echo, no line editing (kept after it is closed)
    path = os.ttyname(slave)
    os.close(slave)              # FluidSender won't open a port another program has open
    os.set_blocking(master, False)
    log = open(args.log, "a", buffering=1) if args.log else None
    probe = FakeProbe(master, values, args.overtravel_at, args.home_time, args.probe_time, replies, log)
    print(path, flush=True)
    if sys.stderr.isatty():
        print(f"Fake probe on {path}  (Ctrl+C quits)", file=sys.stderr)
    try:
        while True:
            ready = select.select([master], [], [], TICK)[0]
            now = time.monotonic()
            if ready:
                try:
                    data = os.read(master, 1024)
                except BlockingIOError:
                    data = b""
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    data = b""               # no one has the port open
                    time.sleep(TICK)
                if data:
                    probe.received(data, now)
            probe.tick(time.monotonic())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
