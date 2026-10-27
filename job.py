"""
Jobs for FluidSender: the loaded program, and the record of a job that runs it.

Python standard library only. The page uploads the G-code it shows; parse_program checks it and keeps
what FluidNC needs: each line without comments, spaces and '%' (FluidNC 4.1.1 refuses a '%' inside a line;
3.9.4 dropped it), empty lines left out, every kept line with its line number in the file (as the page's
editor counts them), so the job can show where it is. Lines FluidNC (3.9.4 and 4.1.1) would run otherwise
than they read (see line_warning) don't stop the program: they come back as warnings for the page to show.
The machine side (machine.py, Link) streams a Job: this module only holds its numbers and texts. Every
job starts with FluidSender's own "G20 G90" (FIRST), so a program without units runs in inches and in
absolute, as right after connecting. coolant_after says
what a line does to the vacuum and AUX outputs once FluidNC has run it (the switches follow it), and
coolant_words which of its M7/M8/M9 a vacuum/AUX click must not cross (see machine.py Machine.coolant).
"""

import math
import re
import struct
from array import array
from collections import deque
from itertools import accumulate

MAX_JOB_LINE = 127              # FluidNC 4.1.1 refuses G-code lines over 127 characters (error:14; 3.9.4 took 254)
CHUNK = 1 << 18                 # characters checked at a time (see parse_program)
LINE_END = re.compile(r"\r\n?|\n")                    # how the page's editor (CodeMirror) splits lines
# (...) comments, or to the end of the line when not closed (as FluidNC reads them), and ; to the end
COMMENTS = re.compile(r"\([^)\r\n]*\)?|;[^\r\n]*")
# whitespace FluidNC skips (a tab is fine in a file), and '%': FluidNC 4.1.1 drops a line of only '%' (a start or end
# mark) but refuses one inside a line (error:1), 3.9.4 dropped it anywhere: without it, both read it as 3.9.4 did
SPACES = str.maketrans("", "", " \t\f\v%")
# anything but printable ASCII and line ends, and the realtime commands ! (0x21), ? (0x3f), ~ (0x7e)
BAD = re.compile(r"[^\x20\x22-\x3e\x40-\x7d\r\n]")
ACTIVE = ("running", "paused", "stopping")
# The words line_warning() looks at (M1, M7, M8, also M01, M7.1, M8.1); a line of plain words, and its words
CHECK = re.compile(r"[Mm]0*[178](?![0-9])")
WORDS = re.compile(r"(?:[A-Za-z][-+]?(?:\d+\.?\d*|\.\d+))+")
WORD = re.compile(r"([A-Za-z])([-+]?(?:\d+\.?\d*|\.\d+))")
MAX_WARNINGS = 20               # warnings kept for a program (the page shows them; the rest are counted)
COOLANT = {7: "AUX (M7)", 8: "the vacuum (M8)"}
FIRST = "G20 G90"               # every job's first line, before the program's (see Job)


class ProgramError(ValueError):
    """A program that can't be run as it is; the message, with the line number, is for the user."""


class Program:
    """The lines to send, packed (a 64 MB file has millions of lines): program[i] -> (file line number, text)."""

    def __init__(self, name, total, numbers, texts, warnings=()):
        self.name = name
        self.total = total              # lines in the file, as the editor counts them
        self.warnings = list(warnings)  # what FluidNC would do otherwise than the lines read (texts for the user)
        self.pre_leveled = None         # why it is a saved leveled G-code (leveling.pre_leveled), or None
        self.numbers = array("I", numbers)
        self.data = "\n".join(texts)
        self.starts = array("I", accumulate(map(len, texts), lambda pos, n: pos + n + 1, initial=0))
        del self.starts[-1:]            # (where each line starts in data)

    def __len__(self):
        return len(self.numbers)

    def __getitem__(self, i):
        start = self.starts[i]
        end = self.starts[i + 1] - 1 if i + 1 < len(self.starts) else len(self.data)
        return self.numbers[i], self.data[start:end]

    def info(self):
        info = {"name": self.name, "lines": len(self), "total": self.total, "warnings": list(self.warnings)}
        if self.pre_leveled:
            info["preLeveled"] = self.pre_leveled
        return info


def _bad_char(c):
    if c in "?!~":
        return (f"'{c}' outside a comment, which FluidNC takes as a realtime command wherever it is "
                "(? status, ! feed hold, ~ resume)")
    return (f"the control or non-ASCII character {ord(c):#04x} outside a comment, which FluidNC could take "
            "as a realtime command")


def mantissa(value):
    """FluidNC's mantissa of a word's value, lroundf(100 * (value - truncf(value))) in single precision as
    GCode.cpp works it out for every word: 10 for 8.1 (and 1.0999), 0 for 8, -10 for -0.1."""
    try:
        f = struct.unpack("f", struct.pack("f", value))[0]
        m = struct.unpack("f", struct.pack("f", 100 * (f - math.trunc(f))))[0]
    except OverflowError:
        return 0
    return math.floor(m + 0.5) if m >= 0 else math.ceil(m - 0.5)


def line_warning(no, line):
    """What FluidNC (3.9.4 and 4.1.1) does with this line (comments and spaces taken out) that it doesn't read as,
    or None:
    - M7 and M8 take M7.1 / M8.1 (off) from the line's LAST word: the parser keeps the mantissa of the last
      word it read (a G word leaves 0), so "M8 F100.1" turns the vacuum OFF and "M8.1 X1" turns it ON;
    - M1, the optional stop, does nothing: the program doesn't stop there (only M0 does)."""
    if not WORDS.fullmatch(line):
        return None                     # (#parameters, expressions, or a bad line FluidNC refuses anyway)
    words = WORD.findall(line)
    shown = line if len(line) <= 40 else line[:37] + "..."
    ms = [(math.trunc(float(v)), mantissa(float(v))) for letter, v in words if letter in "Mm"]
    if sum(n in (7, 8, 9) for n, _ in ms) > 1:
        return None                     # (two coolant words: FluidNC refuses the line, error:21)
    for n, m in ms:
        if n == 1 and m == 0:
            return (f"Line {no} ({shown}): M1 (optional stop) does nothing in FluidNC: the program does not "
                    "stop there. Use M0 to stop")
        if n in (7, 8) and m in (0, 10):        # (M8.2 and the like: FluidNC refuses the line)
            last_letter, last_value = words[-1]
            off = (0 if last_letter in "Gg" else mantissa(float(last_value))) == 10
            if off == (m == 10):
                return None
            if off:
                return (f"Line {no} ({shown}): FluidNC turns {COOLANT[n]} OFF here: it takes M{n}.1 from the "
                        f"last number on the line, {last_letter.upper()}{last_value}. Put M{n} on a line of its own")
            return (f"Line {no} ({shown}): FluidNC turns {COOLANT[n]} ON here: M{n}.1 works only alone on a "
                    f"line. Put M{n}.1 (or M9) on a line of its own")
    return None


def coolant_after(line):
    """What FluidNC (3.9.4 and 4.1.1) has done to the coolant once it has run this line (comments and spaces taken
    out), as (flood, mist), each True (on), False (off) or None (not changed); None if the line doesn't switch it, or
    can't be read here (#parameters, expressions). M8 turns the flood (the vacuum) on and M7 the mist (AUX), or
    off when FluidNC takes them as M8.1 / M7.1, from the line's last word as line_warning says ("M8 F100.1" is
    off); M9 turns both off, and so does the program's end (M2, M30), which comes after the line's other words.
    FluidNC switches before it answers the line (it waits for the moves before it first)."""
    if line.startswith("$") or not WORDS.fullmatch(line):
        return None                     # ($ commands and $J= jogs never switch it)
    words = WORD.findall(line)
    ms = [(math.trunc(float(v)), mantissa(float(v))) for letter, v in words if letter in "Mm"]
    if any(n in (2, 30) and m == 0 for n, m in ms):
        return False, False
    coolant = [(n, m) for n, m in ms if n in (7, 8, 9)]
    if len(coolant) != 1:
        return None                     # (none, or two: FluidNC refuses the line, error:21)
    n, m = coolant[0]
    if n == 9:
        return (False, False) if m == 0 else None
    if m not in (0, 10):
        return None                     # (M8.2 and the like: refused)
    last_letter, last_value = words[-1]
    on = (0 if last_letter in "Gg" else mantissa(float(last_value))) != 10
    return (on, None) if n == 8 else (None, on)


def coolant_words(line):
    """The M7, M8 and M9 words of a line (comments and spaces taken out), as (number, mantissa) pairs: (8, 0) for
    M8, (8, 10) for M8.1, (9, 0) for M9; () if it has none ($ commands and $J= jogs have none). FluidNC sets the
    coolant mode such a word gives first, then waits for the moves before it, then switches: a vacuum/AUX toggle
    (0xA0/0xA1) in that wait flips the mode set already, which cancels the word. M2/M30 aren't among them: they
    wait first and switch everything off only then, whatever a toggle did meanwhile."""
    if line.startswith("$") or ("M" not in line and "m" not in line):
        return ()
    return tuple((n, m) for n, m in ((math.trunc(float(v)), mantissa(float(v))) for letter, v in WORD.findall(line)
                                     if letter in "Mm") if n in (7, 8, 9))


def _pieces(text):
    """(piece, last): text in pieces of about CHUNK characters, each cut just after a "\\n" (no line end is cut
    in two, and no comment: they end at the line end)."""
    start = 0
    while True:
        end = text.find("\n", start + CHUNK)
        if end < 0:
            yield text[start:], True
            return
        yield text[start:end + 1], False
        start = end + 1


def parse_program(name, text):
    """The Program for this G-code text; ProgramError (naming the file line) if FluidNC must not get it.
    The text goes through in pieces: the regular expressions hold Python's lock while they run, and the
    reader thread (a running job) must not wait a second for a big file."""
    lines, bad, bad_no, warnings = [], None, None, []
    for piece, last in _pieces(text):
        code = COMMENTS.sub("", piece).translate(SPACES)    # (neither removes a line end: the numbers stay)
        if bad is None:
            bad = BAD.search(code)
            if bad:
                bad_no = len(lines) + len(LINE_END.findall(code, 0, bad.start())) + 1
        part = LINE_END.split(code)
        if not last:
            part.pop()                  # ("" after the piece's last line end: the next piece starts there)
        if CHECK.search(code):          # (most pieces have no such word: their lines aren't looked at one by one)
            for no, line in enumerate(part, len(lines) + 1):
                warning = CHECK.search(line) and line_warning(no, line)
                if warning:
                    warnings.append(warning)
        lines += part
    if max(map(len, lines), default=0) > MAX_JOB_LINE:
        long_no = next(n for n, line in enumerate(lines, 1) if len(line) > MAX_JOB_LINE)
        if bad_no is None or long_no < bad_no:
            raise ProgramError(f"Line {long_no} is too long: {len(lines[long_no - 1])} characters without comments "
                               f"and spaces (FluidNC refuses G-code lines over {MAX_JOB_LINE} characters: error:14)")
    if bad:
        raise ProgramError(f"Line {bad_no}: {_bad_char(bad.group())}")
    keep = [bool(line) for line in lines]          # not empty ('%' start/end marks are gone too: see SPACES)
    numbers = [n for n, k in enumerate(keep, 1) if k]
    texts = [line for line, k in zip(lines, keep) if k]
    if len(warnings) > MAX_WARNINGS:
        more = len(warnings) - MAX_WARNINGS + 1
        warnings[MAX_WARNINGS - 1:] = [f"... and {more} more lines like these"]
    return Program(name, len(lines), numbers, texts, warnings)


def clock_text(seconds):
    """83 -> '1:23', 3725 -> '1:02:05'"""
    s = max(0, int(seconds + 0.5))
    h, m = divmod(s // 60, 60)
    return f"{h}:{m:02d}:{s % 60:02d}" if h else f"{m}:{s % 60:02d}"


class Job:
    """One run of a program. Machine streams it (Link.send_job); this keeps its state and counts.
    state: running, paused (Hold, M0, an error, no answer), stopping (the safe stop), then done, stopped
    or failed. A job keeps its own Program: editing or closing the G-code meanwhile doesn't change it.
    Its first line is FIRST ("G20 G90"): units and distance mode are as right after connecting whatever an
    earlier program or line left (M2/M30 keep the units), and the program's own G21 or G91 still win. That
    line counts in FluidNC's buffer and its reply like the program's lines, but not in sent, acked or total."""

    def __init__(self, program, now, wall):
        self.program = program
        self.name = program.name
        self.total = len(program)
        self.state = "running"
        self.first = FIRST              # FluidSender's line before the program's; None once sent
        self.next = 0                   # the index of the next line to send
        self.sent = 0
        self.acked = 0
        self.line = None                # the file line number of the last answered line
        self.started = wall             # unix time
        self.start = now                # (the reader thread's clock)
        self.end = None
        self.why = None                 # why it is paused, stopping or over
        self.error = None               # the error that paused or ended it
        self.inflight = deque()         # lines sent, not answered yet, oldest first
        self.bytes = 0                  # their size, each with its "\n"
        self.last_reply = None          # when FluidNC last answered one of its lines
        self.held_after = now           # Hold reports before this (+ a moment) are from before a resume
        self.timed_out = False          # the oldest line got no answer in time (paused; said once)
        self.emitted = -1e9             # when the last job event went out,
        self.dirty = False              # and whether the counts changed since

    @property
    def active(self):
        return self.state in ACTIVE

    def elapsed(self, now):
        return round((self.end if self.end is not None else now) - self.start, 1)

    def info(self, now):
        return {"name": self.name, "state": self.state, "total": self.total, "sent": self.sent,
                "acked": self.acked, "line": self.line, "started": round(self.started, 3),
                "elapsed": self.elapsed(now), "why": self.why, "error": self.error}
